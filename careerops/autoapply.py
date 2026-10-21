"""Gated auto-apply: fill a Greenhouse, Ashby or Lever form from the user's own files, photograph it, and (only when told to) submit it.

This is the ONLY module that may click a submit control, and `live` calls it only for a job that passed every gate in apply/autogate.py.
What it will not do, whatever a form asks for:

  * Solve, bypass or hand off a CAPTCHA or bot check. A challenge on the page (reCAPTCHA, hCaptcha, Turnstile, "verify you are human") stops
    the run before anything is clicked; the browser stays open for the user and the job becomes `needs_user`. Nothing on the challenge is touched.
  * Invent an answer. A field is filled from applicant.yaml (name, e-mail, phone, links, location, and the visa booleans for the job's work
    country) or from the user's answers.yaml (apply/answers.py). A required question with no exact answer stops the run and asks in the terminal;
    the typed answer is used once, and written to answers.yaml only if the user then types `save`. An optional question with no answer stays empty.
  * Click anything else than the submit button: the only other click is an "Apply" link that opens the form, and the options of a drop-down.

Order of a run: wrong-file guard and visa plan -> open the apply page (one request per second per host) -> challenge check -> fill ->
challenge check -> full-page screenshot of the filled form -> [`--no-submit`: stop] -> [first N submits ever, or `--confirm`: `Submit? [y/N]`] ->
challenge check -> click -> wait up to 30 s for the confirmation page -> screenshot of it. Every outcome is one row of data/live/applied_auto.csv.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .apply import answers as answers_mod
from .apply import autogate, autolog, eo
from .apply import visa as visa_mod
from .apply.assist import (APPLY_BUTTON, LABEL_JS, QUESTION_JS, VISA_FIELD, _answer_eo, _answer_visa, _matches, _norm,
                           about_another_country, classify_visa, visa_value)
from .apply.errors import ArchiveError
from .apply.guard import check_job_files
from .discovery import discover

CONFIRM_SECONDS = 30                 # how long to wait for the confirmation page after the click
REQUEST_GAP_S = 1.0                  # at most one request per second per host
SETTLE_MS = 2500                     # a page's own scripts after it loads, as in the assist

CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"]', 'iframe[title*="recaptcha" i]', ".g-recaptcha", 'iframe[src*="hcaptcha"]', ".h-captcha",
    'iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="turnstile"]', ".cf-turnstile", 'iframe[src*="arkoselabs"]',
    'iframe[src*="funcaptcha"]', "[data-sitekey]")
CAPTCHA_TEXT = re.compile(r"verify (?:that )?you(?:'re| are) (?:a )?human|confirm (?:that )?you(?:'re| are) (?:a )?human|"
                          r"i['’]m not a robot|are you a robot|checking (?:if|that) you(?:'re| are) (?:a )?human|human verification", re.I)
CONFIRMATION = re.compile(r"thank you for (?:applying|your application|submitting)|application (?:has been |was )?(?:submitted|received|sent)|"
                          r"(?:we(?:'ve| have)|we['’]ve) received your application|successfully submitted|your application is (?:in|complete)|"
                          r"application (?:complete|confirmation)", re.I)
CONFIRMATION_URL = re.compile(r"confirm|thank|submitted|success", re.I)
SUBMIT_TEXT = re.compile(r"^\s*(submit( application)?|apply( now)?|send application|submit your application)\s*$", re.I)
SUBMIT_SELECTORS = ('[data-qa="btn-submit"]', "#btn-submit", "button[type=submit]", "input[type=submit]")
TEXT_TYPES = ("text", "email", "tel", "url", "textarea", "search", "number", "")
YES = ("yes", "y", "true", "tick", "ticked", "agree", "i agree", "accept", "i accept", "consent")


class NeedsUser(Exception):
    """The run cannot go on without the person: a challenge on the page, or a question nobody has answered."""


@dataclass
class Session:
    """A browser page and how to close it (tests hand in their own)."""
    page: Any
    close: Callable[[], None]


def playwright_launch(profile_dir: Path) -> Session:
    """A headed Chromium with a persistent profile under data/secrets/browser/ (never committed)."""
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def close() -> None:
        try:
            ctx.close()
        finally:
            pw.stop()
    return Session(page, close)


class Throttle:
    """One request per `gap` seconds per host."""

    def __init__(self, gap: float = REQUEST_GAP_S, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self.last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlparse(url or "").hostname or "").lower()
        before = self.last.get(host)
        if before is not None and self.clock() - before < self.gap:
            self.sleep(self.gap - (self.clock() - before))
        self.last[host] = self.clock()


@dataclass
class AutoResult:
    status: str                                       # autolog: submitted | submit_unconfirmed | not_submitted | needs_user | declined | error
    reason: str = ""
    screenshots: list[str] = field(default_factory=list)
    confirmation: str = ""
    filled: list[str] = field(default_factory=list)   # "question -> value", for the log and the terminal
    blank: list[str] = field(default_factory=list)    # optional questions left empty because nothing answers them


# ------------------------------------------------------------------ challenge, submit button, confirmation
def captcha_reason(page) -> str | None:
    """What kind of challenge the page shows, or None. Only looks (a selector query and the visible text); never touches it."""
    for sel in CAPTCHA_SELECTORS:
        try:
            if page.query_selector(sel):
                return f"a CAPTCHA / bot check is on the page ({sel})"
        except Exception:  # noqa: BLE001  (the page is navigating)
            continue
    try:
        text = page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        text = ""
    m = CAPTCHA_TEXT.search(text)
    return f"the page asks you to verify you are human ({m.group(0)!r})" if m else None


def _find_submit(page):
    for sel in SUBMIT_SELECTORS:
        for el in page.query_selector_all(sel):
            if el.is_visible() and el.is_enabled():
                return el
    for el in page.query_selector_all("button, a[role=button]"):
        if el.is_visible() and el.is_enabled() and SUBMIT_TEXT.match(el.inner_text() or ""):
            return el
    return None


def _click_submit(button) -> None:
    """The one place where an application is submitted."""
    button.click()


def _confirmed(page) -> bool:
    try:
        if CONFIRMATION.search(page.inner_text("body") or ""):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(CONFIRMATION_URL.search(urlparse(page.url or "").path))


# ------------------------------------------------------------------ the context of one form
@dataclass
class Ctx:
    job: Mapping[str, Any]
    applicant: Mapping[str, Any]
    book: answers_mod.AnswerBook
    book_path: Path
    plan: visa_mod.VisaPlan | None
    cv_pdf: str | None
    letter_pdf: str | None
    letter_text: str | None
    ask: Callable[[str], str]
    out: Callable[[str], None]
    history: Callable[[str], bool]                    # has the user applied to this company before?
    result: AutoResult = field(default_factory=lambda: AutoResult(autolog.ERROR, "the run was interrupted before it finished"))

    def typed(self, prompt: str) -> str:
        try:
            return (self.ask(prompt) or "").strip()
        except EOFError:
            return ""

    def unknown(self, question: str, why: str, options: list[str] | None = None) -> str:
        """A required question with no exact answer: say so, ask, and return what the user typed. Blank = stop. The answer is saved to
        answers.yaml only when the user then types `save`."""
        self.out(f"\n  QUESTION WITH NO ANSWER: {question}\n  ({why})" + (f"\n  Options: {' | '.join(options[:15])}" if options else ""))
        typed = self.typed("  Your answer for this form (blank = stop here; nothing is guessed): ")
        if not typed:
            raise NeedsUser(f"no answer for the required question: {question}")
        return typed

    def offer_save(self, question: str, typed: str) -> None:
        if self.typed("  Type `save` to keep this answer in answers.yaml for next time (anything else: used on this form only): ").lower() == "save":
            try:
                answers_mod.save(self.book_path, question, typed)
            except answers_mod.AnswersError as e:
                self.out(f"  not saved: {e}")
            else:
                self.out(f"  saved to {self.book_path.name}")
                self.book = answers_mod.load(self.book_path)


def _decide(ctx: Ctx, question: str, required: bool, *, options: list[str] | None = None, numeric: bool = False) -> str | None:
    """The text to enter (or the option to pick) for a question, from answers.yaml only; None = leave it empty (optional and unanswered).
    A required question with no exact answer asks the user (Ctx.unknown) and raises NeedsUser if they give none."""
    r = ctx.book.resolve(question, ctx.job)
    why = "no entry in answers.yaml matches this question"
    if r is not None and not r.known and r.entry_id == "previously_applied" and not ctx.history(str(ctx.job.get("company") or "")):
        r = answers_mod.Resolved(r.entry_id, r.pattern, "No", r.numeric, r.options)          # no earlier application to this company in the database or archive
    if r is not None and r.known:
        if options is not None:
            pick = answers_mod.pick_option(options, r)
            if pick is not None:
                return pick
            why = f"answers.yaml says {r.text!r} but no option matches it"
        elif numeric and not r.numeric:
            why = "the field takes a number and the answers.yaml entry has no answer_numeric"
        else:
            return r.numeric if numeric else r.text
    elif r is not None:
        why = f"the entry {r.entry_id!r} in answers.yaml is blank"
    if not required:
        ctx.result.blank.append(question)
        return None
    typed = ctx.unknown(question, why, options)
    if options is not None:
        pick = answers_mod.pick_option(options, answers_mod.Resolved("typed", "", typed, None, ()))
        if pick is None:
            raise NeedsUser(f"the answer typed for {question!r} matches none of the options: {' | '.join(options[:15])}")
        ctx.offer_save(question, typed)
        return pick
    ctx.offer_save(question, typed)
    return typed


# ------------------------------------------------------------------ the fields
def _is_required(el, label: str) -> bool:
    return el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true" or label.rstrip().endswith("*")


def _numeric_only(el, typ: str, label: str) -> bool:
    return typ == "number" or (el.get_attribute("inputmode") or "") in ("numeric", "decimal") or \
        bool(re.search(r"\b(?:numbers? only|digits only|numeric|figure)\b|£|\bGBP\b", label, re.I))


def _group(page, el, typ: str) -> list:
    name = el.get_attribute("name")
    return page.query_selector_all(f'input[type={typ}][name="{name}"]') if name else [el]


def _label_of(el) -> str:
    return _norm((el.evaluate(LABEL_JS) or "").split("|")[0])


def _fill_text(el, value: str) -> None:
    el.fill(value)


def _visa_field(page, el, typ: str, question: str, ctx: Ctx, seen: set, required: bool) -> bool:
    """A right-to-work / sponsorship field, answered from the visa booleans of the job's work country. True = handled."""
    plan = ctx.plan
    if typ == "radio":
        name = el.get_attribute("name")
        if name and ("visa", name) in seen:
            return True
        seen.add(("visa", name))
        group = _group(page, el, "radio")
        value = None if about_another_country(question, plan) else visa_value(plan, classify_visa(question))
        want = (value or "").lower()
        for r in group:
            if want and want in (_norm(r.evaluate(LABEL_JS).split("|")[0]).lower(), (r.get_attribute("value") or "").lower()):
                r.check()
                ctx.result.filled.append(f"{question[:80]} -> {value}")
                return True
        if any(_is_required(r, question) for r in group):
            labels = [_label_of(r) for r in group]
            pick = _decide(ctx, question, True, options=labels)
            next(r for r in group if _label_of(r) == pick).check()
            ctx.result.filled.append(f"{question[:80]} -> {pick} (typed)")
        return True
    held = _answer_visa(el, typ, question, plan)
    if held:
        ctx.result.filled.append(f"{question[:80]} -> {held}")
    elif required:
        options = [_norm(o.inner_text()) for o in el.query_selector_all("option") if _norm(o.inner_text())] if typ == "select" else None
        value = _decide(ctx, question, True, options=options)
        el.select_option(label=value) if typ == "select" else _fill_text(el, value)
        ctx.result.filled.append(f"{question[:80]} -> {value} (typed)")
    return True


def _choice(page, el, typ: str, question: str, ctx: Ctx, seen: set, required: bool) -> None:
    """A radio group, a group of check boxes, or one check box (a consent line)."""
    name = el.get_attribute("name")
    group = _group(page, el, typ)
    if typ == "radio" or len(group) > 1:
        if name and ("choice", name) in seen:
            return
        seen.add(("choice", name))
        q = _norm(el.evaluate(QUESTION_JS)).rstrip(" *") or question
        labels = [_label_of(g) for g in group]
        req = any(_is_required(g, q) for g in group)
        pick = _decide(ctx, q, req, options=labels)
        if pick is not None:
            next(g for g in group if _label_of(g) == pick).check()
            ctx.result.filled.append(f"{q[:80]} -> {pick}")
        return
    if el.is_checked():
        return
    value = _decide(ctx, question, required)
    if value is not None and value.strip().lower() in YES:
        el.check()
        ctx.result.filled.append(f"{question[:80]} -> ticked")
    elif value is not None:
        ctx.result.blank.append(f"{question} (answer {value!r}: left unticked)")


def _select(el, question: str, ctx: Ctx, required: bool) -> None:
    options = [_norm(o.inner_text()) for o in el.query_selector_all("option") if _norm(o.inner_text())]
    pick = _decide(ctx, question, required, options=options)
    if pick is not None:
        el.select_option(label=next(o.inner_text() for o in el.query_selector_all("option") if _norm(o.inner_text()) == pick))
        ctx.result.filled.append(f"{question[:80]} -> {pick}")


def _combobox(page, el, question: str, ctx: Ctx, required: bool) -> None:
    """A script-built drop-down (an input with role=combobox): type the answer, then pick the listed option that equals it."""
    want = _decide(ctx, question, required)
    if want is None:
        return
    el.fill(want)
    page.wait_for_timeout(400)
    options = [(_norm(o.inner_text()), o) for o in page.query_selector_all("[role=option]") if o.is_visible()]
    pick = answers_mod.pick_option([t for t, _ in options], answers_mod.Resolved("combobox", "", want, None, ()))
    if pick is None:
        raise NeedsUser(f"the drop-down for {question!r} lists no option equal to {want!r}")
    next(o for t, o in options if t == pick).click()
    ctx.result.filled.append(f"{question[:80]} -> {pick}")


def fill_form(page, ctx: Ctx) -> None:
    """Fill every visible field the way the module docstring says; raises NeedsUser at the first required question nobody has answered."""
    applicant = ctx.applicant
    never, lmap = applicant.get("never_fill", []), applicant.get("label_map", {})
    listed_eo = eo.entries(applicant)
    seen: set = set()
    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset") or (not el.is_visible() and typ != "file"):
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or typ
        question = _norm(el.evaluate(QUESTION_JS) if typ == "radio" else label.split("|")[0]).rstrip(" *")
        required = _is_required(el, label.split("|")[0])

        if typ == "file":
            if re.search(r"cover", label, re.I):
                if ctx.letter_pdf:
                    el.set_input_files(ctx.letter_pdf)
                    ctx.result.filled.append(f"{short} <- cover letter")
                elif required:
                    raise NeedsUser(f"a cover letter file is required but this job has no letter PDF ({short})")
            elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and ctx.cv_pdf:
                el.set_input_files(ctx.cv_pdf)
                ctx.result.filled.append(f"{short} <- CV")
            elif required:
                raise NeedsUser(f"a required file upload that is neither the CV nor a cover letter: {short}")
            continue

        if ctx.letter_text and typ == "textarea" and re.search(r"cover\s*letter", label, re.I):
            if not (el.input_value() or "").strip():
                el.fill(ctx.letter_text)
                ctx.result.filled.append(f"{short} <- cover letter text")
            continue

        if VISA_FIELD.search(f"{question} {label}"):
            _visa_field(page, el, typ, question, ctx, seen, required)
            continue

        if typ in ("radio", "checkbox"):
            _choice(page, el, typ, question, ctx, seen, required)
            continue

        if typ in TEXT_TYPES and not _matches(label, never):
            key = next((k for k, pats in lmap.items() if _matches(label, pats)), None)
            if key and applicant.get(key):
                if not (el.input_value() or "").strip():
                    el.fill(str(applicant[key]))
                    ctx.result.filled.append(f"{short} <- {key}")
                continue

        if tag == "input" and (el.get_attribute("role") == "combobox") and typ in TEXT_TYPES:
            _combobox(page, el, question, ctx, required)
            continue
        if typ in TEXT_TYPES and (el.input_value() or "").strip():
            continue                                                            # the form (or a saved profile) already holds something

        r = ctx.book.resolve(question, ctx.job)
        if (r is None or not r.known) and eo.is_eo_field(f"{question} {label}") and tag == "select":
            entry = eo.entry_for(f"{question} {label}", listed_eo)
            picked, _, _ = _answer_eo(page, el, typ, entry)
            if picked:
                ctx.result.filled.append(f"{question[:80]} -> {picked} (applicant.yaml)")
                continue
        if tag == "select":
            _select(el, question, ctx, required)
        elif typ in TEXT_TYPES:
            value = _decide(ctx, question, required, numeric=_numeric_only(el, typ, label))
            if value is not None:
                el.fill(value)
                ctx.result.filled.append(f"{question[:80]} -> {value[:60]}")
        elif required:
            raise NeedsUser(f"a required field this module does not fill ({typ}): {short}")


def still_empty(page) -> list[str]:
    """Labels of the required fields that hold nothing now (a check made after filling, before the screenshot)."""
    out, seen = [], set()
    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset") or (not el.is_visible() and typ != "file"):
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        if not _is_required(el, label.split("|")[0]):
            continue
        if typ in ("radio", "checkbox"):
            name = el.get_attribute("name")
            if name in seen:
                continue
            seen.add(name)
            if not any(g.is_checked() for g in _group(page, el, typ)):
                out.append(_norm(el.evaluate(QUESTION_JS)) or label)
        elif not (el.input_value() or "").strip():
            out.append(label.split("|")[0].strip()[:80] or typ)
    return out


def open_form(page, url: str, throttle: Throttle) -> None:
    throttle.wait(url)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(SETTLE_MS)
    if not page.query_selector("input[type=file], input[type=email]"):
        for a in page.query_selector_all("a, button"):
            if APPLY_BUTTON.match(a.inner_text() or ""):
                throttle.wait(url)
                a.click()                               # the "Apply" link that opens the form: not the submit button
                page.wait_for_timeout(SETTLE_MS)
                break


# ------------------------------------------------------------------ one job
def _shot(page, folder: Path, stem: str, stamp: str, result: AutoResult) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"auto-{stamp}-{stem}.png"
    page.screenshot(path=str(path), full_page=True)
    result.screenshots.append(str(path))
    return path


def default_history(db, settings=None) -> Callable[[str], bool]:
    """True when an application to this company is on record: a job of that company marked applied, or a company folder in the archive
    (a directory listing, nothing is opened). An empty company name counts as seen, so the question is asked rather than answered."""
    in_archive = {answers_mod.norm(c) for c, _ in discover.archive_pairs(settings.path("applications"))} if settings is not None else set()

    def seen(company: str) -> bool:
        key = answers_mod.norm(company)
        if not key or key in in_archive:
            return True
        for r in db.conn.execute("SELECT company FROM jobs WHERE status IN ('applied','applied (archive incomplete)','submit_unconfirmed') "
                                 "OR applied_at IS NOT NULL"):
            if answers_mod.norm(r["company"]) == key:
                return True
        return False
    return seen


def run(job: Mapping[str, Any], rep: Mapping[str, Any], applicant: Mapping[str, Any], settings, *, submit: bool = True, confirm: bool = False,
        ask: Callable[[str], str] = input, out: Callable[[str], None] = print, launch: Callable[[Path], Session] | None = None,
        history: Callable[[str], bool] | None = None, throttle: Throttle | None = None, ats_strict: float | None = None,
        now: Callable[[], datetime] = datetime.now) -> AutoResult:
    """Fill the form of one gated job; submit it unless `submit` is False. Always appends one row to applied_auto.csv. Raises ArchiveError
    (wrong files, unknown work country) before a browser is opened."""
    job = dict(job)
    cv = rep.get("cv_pdf")
    letter = rep.get("letter_pdf") if rep.get("letter_pdf") and Path(rep["letter_pdf"]).is_file() and \
        (rep.get("letter") or {}).get("status") not in ("none", "off", "declined") else None
    plan = visa_mod.require_plan(job, applicant)                          # W unknown / no answers for it: nothing is opened
    check_job_files(job, cv, letter)                                      # before a browser even opens
    url = job.get("apply_url") or job.get("url")
    if not url:
        raise ArchiveError(f"{job['id']} has no application URL")
    book_path = answers_mod.path_for(settings)
    ctx = Ctx(job, applicant, answers_mod.load(book_path), book_path, plan, cv, letter,
              "\n\n".join((rep.get("letter") or {}).get("paragraphs") or []) or None, ask, out,
              history or (lambda company: True))
    log = autolog.rows(settings)
    cfg = autogate.config(settings)
    ask_to_confirm = confirm or autolog.clicked_total(log) < cfg.confirm_first_n
    throttle = throttle or Throttle()
    stamp = f"{now():%Y%m%d-%H%M%S}"
    folder = Path(job["out_dir"])
    result = ctx.result
    session = (launch or playwright_launch)(settings.path("secrets") / "browser")
    keep_open = False
    try:
        page = session.page
        open_form(page, url, throttle)
        stop = captcha_reason(page)
        if stop:
            raise NeedsUser(stop)
        fill_form(page, ctx)
        empty = still_empty(page)
        if empty:
            raise NeedsUser("required fields still empty: " + "; ".join(empty[:5]))
        stop = captcha_reason(page)
        if stop:
            raise NeedsUser(stop)
        shot = _shot(page, folder, "form", stamp, result)
        out(f"  Filled form screenshot: {shot}")
        if not submit:
            result.status, result.reason = autolog.NOT_SUBMITTED, "--no-submit: filled and photographed, never clicked"
            return result
        if ask_to_confirm:
            why = "--confirm" if confirm else f"one of the first {cfg.confirm_first_n} auto-submits"
            if ctx.typed(f"  Submit? [y/N] ({why}; screenshot above) ").lower() not in ("y", "yes"):
                result.status, result.reason = autolog.DECLINED, "you answered N at Submit?"
                return result
        button = _find_submit(page)
        if button is None:
            raise NeedsUser("no submit button was found on the form")
        stop = captcha_reason(page)
        if stop:
            raise NeedsUser(stop)
        throttle.wait(url)
        _click_submit(button)
        for _ in range(CONFIRM_SECONDS):
            page.wait_for_timeout(1000)
            if _confirmed(page):
                result.status, result.reason = autolog.SUBMITTED, "the confirmation page appeared"
                result.confirmation = f"auto-submitted {now():%Y-%m-%d %H:%M}: {page.url}"
                break
            stop = captcha_reason(page)
            if stop:
                result.status, result.reason = autolog.NEEDS_USER, stop + " after the click; the form may not have gone through"
                keep_open = True
                break
        else:
            result.status = autolog.UNCONFIRMED
            result.reason = f"no confirmation page within {CONFIRM_SECONDS} s: check the form (or your e-mail) yourself"
        _shot(page, folder, "confirmation", stamp, result)
        return result
    except NeedsUser as e:
        result.status, result.reason = autolog.NEEDS_USER, str(e)
        keep_open = True
        try:
            _shot(session.page, folder, "stopped", stamp, result)
        except Exception:  # noqa: BLE001  (the page is gone; the reason is what matters)
            pass
        return result
    except ArchiveError:
        raise
    except Exception as e:  # noqa: BLE001  (a timeout, a page that changed under us)
        result.status, result.reason = autolog.ERROR, f"{type(e).__name__}: {str(e)[:300]}"
        return result
    finally:
        if result.status == autolog.NEEDS_USER and keep_open:
            out(f"\n  STOPPED, hand-over: {result.reason}\n  The browser is left open for you; nothing on the page has been touched by this program.")
            ctx.typed("  Finish by hand if you want to, then press Enter to close the browser: ")
        session.close()
        autolog.append(settings, job=job, ats_strict=ats_strict, screenshots=result.screenshots, result=result.status, when=now())
