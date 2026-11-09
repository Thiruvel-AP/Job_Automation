"""Gated auto-apply: fill a Greenhouse, Ashby or Lever form from the user's own files, photograph it, and (only when told to) submit it.

This is the ONLY module that may click a submit control, and `live` calls it only for a job that passed every gate in apply/autogate.py.
What it will not do, whatever a form asks for:

  * Solve, bypass or outsource a CAPTCHA or bot check. A challenge on the page (reCAPTCHA, hCaptcha, Turnstile, "verify you are human"), the
    message "error verifying your application", or a Google / Microsoft sign-in page stops the run; nothing on the page is touched and no
    credential is typed. The job becomes `needs_user` and apply/handoff.py opens the apply URL in the user's own browser, prints the CV path,
    writes answers_to_paste.txt in the job's output folder and records the host, so that later jobs on it go straight to the hand-off.
  * Invent an answer. A field is filled from applicant.yaml (name, e-mail, phone, links, location, and the visa booleans for the job's work
    country) or from the user's answers.yaml (apply/answers.py); the fields are filled by apply/filler.py, the same code the assist uses. A required question with no exact answer stops the run and asks in the terminal;
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
from .apply import autogate, autolog, dryrun, eo, failures as failures_mod, filler, formfind, formloop, knockout, letter_demand, requiredgate, shortfields, verify
from .apply import visa as visa_mod
from .apply.assist import APPLY_BUTTON, VISA_FIELD, _answer_eo, _answer_visa, about_another_country, classify_visa, is_visa_question, rights_pick, sponsorship_answer, visa_value
from .apply.filler import (LABEL_JS, QUESTION_JS, TEXT_TYPES, Ctx, NeedsUser, _norm, decide as _decide, group_of as _group,
                           is_required as _is_required, label_of as _label_of)
from .apply.errors import ArchiveError
from .apply.formfind import CAPTCHA_SELECTORS, CAPTCHA_TEXT, HandOff, captcha_reason   # noqa: F401  (the challenge checks live in formfind)
from .apply import stale
from .apply.guard import check_job_files
from .discovery import discover

CONFIRM_SECONDS = 30                 # how long to wait for the confirmation page after the click
REQUEST_GAP_S = 1.0                  # at most one request per second per host
SETTLE_MS = 2500                     # a page's own scripts after it loads, as in the assist

CONFIRMATION = re.compile(r"thank you for (?:applying|your application|submitting)|application (?:has been |was )?(?:submitted|received|sent)|"
                          r"(?:we(?:'ve| have)|we['’]ve) received your application|successfully submitted|your application is (?:in|complete)|"
                          r"application (?:complete|confirmation)", re.I)
CONFIRMATION_URL = re.compile(r"confirm|thank|submitted|success", re.I)
SUBMIT_TEXT = re.compile(r"^\s*(submit( application)?|apply( now)?|send application|submit your application)\s*$", re.I)
SUBMIT_SELECTORS = ('[data-qa="btn-submit"]', "#btn-submit", "button[type=submit]", "input[type=submit]")


@dataclass
class Session:
    """A browser page and how to close it (tests hand in their own)."""
    page: Any
    close: Callable[[], None]


def fresh_launch(settings) -> Callable[[Path], Session]:
    """v2.5 default (`apply.browser: fresh`, apply/browser.py): a new non-persistent context of the run's shared Chromium for each job, the window
    off-screen while the form is filled. `apply.browser: profile` keeps the persistent profile below (after waiting for its lock)."""
    from .apply import browser as browser_mod

    def launch(profile_dir: Path) -> Session:
        if browser_mod.mode_of(settings) == browser_mod.PROFILE:
            browser_mod.wait_for_lock(profile_dir)
            return playwright_launch(profile_dir)
        page, close = browser_mod.fresh_page(headless=False, offscreen=False)
        return Session(page, close)
    return launch


def playwright_launch(profile_dir: Path) -> Session:
    """A headed Chromium with a persistent profile under data/secrets/browser/ (never committed): `apply.browser: profile` only."""
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
    handed_off: bool = False                          # the job went to the person's own browser (apply/handoff.py): its status is `handed_off`


# ------------------------------------------------------------------ challenge, submit button, confirmation
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


# ------------------------------------------------------------------ the fields
def _visa_field(page, el, typ: str, question: str, ctx: Ctx, seen: set, required: bool) -> bool:
    """A right-to-work / sponsorship field, answered from the visa booleans of the job's work country. True = handled."""
    plan = ctx.plan
    if typ == "radio":
        name = el.get_attribute("name")
        if name and ("visa", name) in seen:
            return True
        seen.add(("visa", name))
        group = _group(page, el, "radio")
        value = None if about_another_country(question, plan) else sponsorship_answer(plan, question)
        if value is None:                                                 # a working-rights group of statuses: applicant.yaml, by its own labels
            value = rights_pick(plan, question, [_label_of(r) for r in group])[0]
        if filler.lever_page(page):                                       # never set by code on Lever: the human answers it (still_empty stops the run)
            filler.hand_over(ctx, question, value)
            return True
        want = (value or "").lower()
        for r in group:
            if want and want in (_norm(r.evaluate(LABEL_JS).split("|")[0]).lower(), (r.get_attribute("value") or "").lower()):
                r.check()
                ctx.track(r, question, "check")
                ctx.result.filled.append(f"{question[:80]} -> {value}")
                return True
        if any(_is_required(r, question) for r in group):
            labels = [_label_of(r) for r in group]
            pick = _decide(ctx, question, True, options=labels)
            chosen = next(r for r in group if _label_of(r) == pick)
            chosen.check()
            ctx.track(chosen, question, "check")
            ctx.result.filled.append(f"{question[:80]} -> {pick} (typed)")
        return True
    if shortfields.is_combobox(el, typ):                                 # a script-built drop-down: applicant.yaml picks the option, or the person does
        held, why = shortfields.visa_combobox(page, el, question, plan, ctx)
        if not held:
            ctx.needs_user[question] = why
            if required:
                ctx.stop(f"{question[:80]}: {why}")
        return True
    held = _answer_visa(el, typ, question, plan)
    if held:
        ctx.track(el, question, "select" if typ == "select" else "text", held)
        ctx.result.filled.append(f"{question[:80]} -> {held}")
    elif required:
        options = [_norm(o.inner_text()) for o in el.query_selector_all("option") if _norm(o.inner_text())] if typ == "select" else None
        value = _decide(ctx, question, True, options=options)
        el.select_option(label=value) if typ == "select" else el.fill(value)
        ctx.result.filled.append(f"{question[:80]} -> {value} (typed)")
    return True


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


def upload_files(page, ctx: Ctx) -> bool:
    """Pass 1 of fill_form: every file field, before any text field (an upload can make the form re-render and wipe what was typed). The
    "Autofill from resume" control is never used: the CV goes only to the real Resume / CV field. True when a file was set."""
    sent = False
    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        if tag != "input" or (el.get_attribute("type") or "").lower() != "file":
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or "file"
        required = _is_required(el, label.split("|")[0])
        if verify.is_autofill(el):
            ctx.out(f"  skipped the autofill upload control (never used): {short}")
        elif re.search(r"cover", label, re.I):
            if ctx.letter_pdf:
                el.set_input_files(ctx.letter_pdf)
                ctx.result.filled.append(f"{short} <- cover letter")
                sent = True
            elif required:
                raise NeedsUser(f"a cover letter file is required but this job has no letter PDF ({short})")
        elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and ctx.cv_pdf:
            el.set_input_files(ctx.cv_pdf)
            ctx.result.filled.append(f"{short} <- CV")
            sent = True
        elif required:
            raise NeedsUser(f"a required file upload that is neither the CV nor a cover letter: {short}")
    return sent


def fill_form(page, ctx: Ctx, quiet_ms: int = verify.QUIET_MS) -> None:
    """Fill every visible field the way the module docstring says; raises NeedsUser at the first required question nobody has answered.
    The files go first, then the form is left to settle (`quiet_ms` without a DOM change), then the other fields (apply/verify.py);
    `verify_filled` reads them back."""
    listed_eo = eo.entries(ctx.applicant, ctx.book)        # applicant.yaml first, then the eo_* entries of answers.yaml
    ctx.combobox = _combobox if ctx.combobox is None else ctx.combobox
    if ctx.tracker is None:
        ctx.tracker = verify.Tracker()
    loop = ctx.loop = formloop.FormLoop(ctx, formloop.output_folder(ctx.job, ctx.cv_pdf))
    fields = loop.begin(page)                              # every field of the form, printed before anything is filled
    knocks = knockout.scan([(f.question, f.options) for f in fields], ctx.applicant, ctx.book, ctx.job, ctx.plan, getattr(ctx.freetext, "fb", None))
    if knocks:                                             # in auto mode a knock-out always goes to the human: nothing is uploaded, drafted or submitted
        for k in knocks:
            ctx.out(f"  {k.line()}")
        raise NeedsUser(knockout.stop_reason(knocks))
    if upload_files(page, ctx):
        if not verify.wait_quiet(page, quiet_ms):
            ctx.out(f"  the form was still changing {verify.QUIET_MAX_MS // 1000} s after the upload; filling it anyway (everything is read back after)")
        refused = verify.upload_refused(page)
        if refused:                                          # a failed attempt (apply/failures.py), not something to try again
            raise NeedsUser(f"the form refused an upload: {refused}")
    seen: set = set()

    def fill_element(el) -> None:                         # one element of the form (also used for a field that appears after an answer)
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset", "file") or not el.is_visible():
            return
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or typ
        question = _norm(el.evaluate(QUESTION_JS) if typ == "radio" else label.split("|")[0]).rstrip(" *")
        required = _is_required(el, label.split("|")[0])

        if ctx.letter_text and typ == "textarea" and re.search(r"cover\s*letter", label, re.I):
            if not (el.input_value() or "").strip():
                el.fill(ctx.letter_text)
                ctx.track(el, short, "text", ctx.letter_text)
                ctx.result.filled.append(f"{short} <- cover letter text")
            return

        if is_visa_question(f"{question} {label}"):
            _visa_field(page, el, typ, question, ctx, seen, required)
            return

        r = ctx.book.resolve(question, ctx.job)
        if (r is None or not r.known) and eo.is_eo_field(f"{question} {label}") and tag == "select":
            entry = eo.entry_for(f"{question} {label}", listed_eo)
            picked, _, _ = _answer_eo(page, el, typ, entry, track=lambda e, kind, value, q=question: ctx.track(e, q, kind, value))
            if picked:
                ctx.result.filled.append(f"{question[:80]} -> {picked} (applicant.yaml)")
                return
        filler.fill_field(page, el, tag, typ, label, short, question, required, ctx, seen)       # standard fields, then answers.yaml

    loop.handle_new = fill_element
    for el in page.query_selector_all("input, textarea, select"):
        fill_element(el)
    loop.run(page)                                         # the free-text fields, one by one, the form scanned again after each; then the person's y / e


def verify_filled(page, ctx: Ctx) -> list[str]:
    """Read back every field `fill_form` filled (apply/verify.py): an empty one is filled once more, and prints VERIFIED or LOST.
    Returns the labels of the LOST ones, which stop the run: nothing is submitted with a field the form swallowed."""
    if ctx.tracker is None or not ctx.tracker.fills:
        return []
    ctx.out("  Reading back every field that was filled:")
    ctx.verified, ctx.lost = ctx.tracker.verify(page, ctx.out)
    return ctx.lost


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


def open_form(page, url: str, throttle: Throttle, kind: str | None = None) -> None:
    """Open the application form (formfind.open_form: Ashby `/application`, Lever `/apply`, Greenhouse: wait for it). Raises NeedsUser
    `FORM NOT FOUND <url>` when no form field appears within 15 s, HandOff for a bot check or a sign-in wall."""
    problem = formfind.open_form(page, url, kind, wait=throttle.wait, follow=lambda a: a.click())      # the "Apply" link that opens the form
    if problem:
        raise NeedsUser(problem)


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
    in_archive = {answers_mod.norm(c) for c, _ in discover.archive_pairs(settings.archive_roots())} if settings is not None else set()

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
        now: Callable[[], datetime] = datetime.now, freetext=None, handoff=None, failures=None, letter_maker=None, record: bool = False) -> AutoResult:
    """Fill the form of one gated job; submit it unless `submit` is False. Always appends one row to applied_auto.csv. Raises ArchiveError
    (wrong files, unknown work country) before a browser is opened.

    `failures` (apply/failures.py) counts the failed attempts of each job in data/live/submit_failures.csv. A job that has used them up, and a host
    on `apply.handoff_hosts` or that handed a job over before, goes straight to the hand-off with no browser: never a third attempt. The second
    failure of a job offers `[h] hand off`."""
    job = dict(job)
    cv = rep.get("cv_pdf")
    letter = rep.get("letter_pdf") if rep.get("letter_pdf") and Path(rep["letter_pdf"]).is_file() and \
        (rep.get("letter") or {}).get("status") not in ("none", "off", "declined") else None
    plan = visa_mod.require_plan(job, applicant)                          # W unknown / no answers for it: nothing is opened
    check_job_files(job, cv, letter)                                      # before a browser even opens
    stale.require_fresh(job, cv, letter)                                  # older than the tailoring code: run with --retailor
    url = job.get("apply_url") or job.get("url")
    if not url:
        raise ArchiveError(f"{job['id']} has no application URL")
    book_path = answers_mod.path_for(settings)
    ctx = Ctx(job, applicant, answers_mod.load(book_path), book_path, plan, cv, letter,
              "\n\n".join((rep.get("letter") or {}).get("paragraphs") or []) or None, ask, out,
              history or (lambda company: True),
              AutoResult(autolog.ERROR, "the run was interrupted before it finished"))
    if freetext is not None:                                              # free-text questions: drafted, guarded, used only on the person's `y`
        freetext.ask, freetext.out = ask, out
        ctx.freetext = freetext
    log = autolog.rows(settings)
    cfg = autogate.config(settings)
    ask_to_confirm = confirm or autolog.clicked_total(log) < cfg.confirm_first_n
    throttle = throttle or Throttle()
    stamp = f"{now():%Y%m%d-%H%M%S}"
    folder = Path(job["out_dir"])
    result = ctx.result
    used_up = failures.gate(job["id"]) if failures is not None else None
    known = (handoff.why(url) if handoff is not None else "") or used_up   # a host that turned the automated browser away, or a job out of attempts
    if known:                                             # no browser is opened at all
        if handoff is not None:
            handoff.run(job, rep, applicant, ctx.book, plan, url, known, remember=False)
            result.handed_off = True
        else:
            out(f"\n  HAND-OFF: {known}\n  Open this in your own browser: {url}\n  CV: {cv}")
        result.status, result.reason = autolog.NEEDS_USER, "HAND-OFF: " + known
        autolog.append(settings, job=job, ats_strict=ats_strict, screenshots=[], result=result.status, when=now())
        return result
    letter_why = ""
    if ctx.letter_pdf is None and letter_maker is not None:               # v2.5: the letter is written BEFORE the browser opens, never inside it
        from .apply import planfirst
        new_rep, ctx.letter_pdf, ctx.letter_text, letter_why = planfirst.letter_first(dict(rep), None, None, letter_maker, True, out)
        if ctx.letter_pdf:
            rep = new_rep
            check_job_files(job, cv, ctx.letter_pdf)
    session = (launch or fresh_launch(settings))(settings.path("secrets") / "browser")
    keep_open = False
    failed, offer = 0, False
    shown_table = False

    def hand_off(why: str) -> None:
        result.handed_off = True
        if handoff is not None:
            handoff.run(job, rep, applicant, ctx.book, plan, url, why, result.filled, drafts=ctx.loop.paste_drafts() if ctx.loop else None)
        else:
            out(f"\n  HAND-OFF: {why}\n  Open this in your own browser: {url}\n  CV: {cv}")

    def failure(text: str) -> tuple[int, bool]:
        """Count this attempt's failure (when it is one of the four kinds); (failures so far, is the hand-off to be offered)."""
        kind = failures_mod.kind_of(text)
        if failures is None or not kind:
            return 0, False
        n = failures.record(job["id"], url, kind, text)
        return n, n >= failures.limit
    try:
        page = session.page
        open_form(page, url, throttle, job.get("apply_kind"))
        from .tailoring.letter_reason import letter_reason

        prov = letter_demand.deliver(page, rep, ctx.letter_pdf, ctx.letter_text, out, reason=letter_why or letter_reason(rep.get("letter")))
        ctx.letter_pdf, ctx.letter_text, ctx.letter_note = prov.pdf, prov.text, prov.note       # written before the browser opened; "Enter manually" pasted here
        if prov.handoff:
            raise HandOff(prov.handoff)
        fill_form(page, ctx)
        lost = verify_filled(page, ctx)
        rows = requiredgate.check(page, ctx, ctx.verified, lost, ctx.loop.status if ctx.loop else None, ctx.letter_note)
        for line in requiredgate.render(rows):
            out(line)
        shown_table = True
        if record:
            try:
                dryrun.record(dryrun.folder_for(rep, job, now().date()), page, job, rep,
                              requiredgate.check(page, ctx, ctx.verified, lost, ctx.loop.status if ctx.loop else None, ctx.letter_note, required_only=False),
                              cv=cv, letter=ctx.letter_pdf, letter_text=ctx.letter_text, freetext=ctx.freetext, out=out)
            except OSError as e:
                out(f"  dry-run record not written: {e}")
        if lost:
            raise NeedsUser("the form emptied field(s) after they were filled: " + "; ".join(lost[:5]))
        waiting = requiredgate.blocking(rows)
        if waiting:
            raise NeedsUser("required fields still empty or waiting for you: " + "; ".join(f"{r.label[:60]} ({r.status}{': ' + r.reason[:80] if r.reason else ''})" for r in waiting[:6]))
        empty = still_empty(page)
        if empty:
            raise NeedsUser("required fields still empty: " + "; ".join(empty[:5]))
        stop = formfind.blocker(page)
        if stop:
            raise HandOff(stop)
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
        stop = formfind.blocker(page)
        if stop:
            raise HandOff(stop)
        throttle.wait(url)
        _click_submit(button)
        for _ in range(CONFIRM_SECONDS):
            page.wait_for_timeout(1000)
            if _confirmed(page):
                result.status, result.reason = autolog.SUBMITTED, "the confirmation page appeared"
                result.confirmation = f"auto-submitted {now():%Y-%m-%d %H:%M}: {page.url}"
                break
            stop = formfind.blocker(page)
            if stop:
                result.status, result.reason = autolog.NEEDS_USER, "HAND-OFF: " + stop + " after the click; the form may not have gone through"
                failure(stop)
                hand_off(stop + " after the click")
                break
        else:
            result.status = autolog.UNCONFIRMED
            result.reason = f"no confirmation page within {CONFIRM_SECONDS} s: check the form (or your e-mail) yourself"
        _shot(page, folder, "confirmation", stamp, result)
        return result
    except HandOff as e:                                  # a bot check or a sign-in wall: never touched, never worked around
        result.status, result.reason = autolog.NEEDS_USER, "HAND-OFF: " + str(e)
        failure(str(e))                                   # "error verifying your application" is a counted failure; a plain bot check is not
        try:
            _shot(session.page, folder, "stopped", stamp, result)
        except Exception:  # noqa: BLE001  (the page is gone; the reason is what matters)
            pass
        hand_off(str(e))
        return result
    except NeedsUser as e:
        result.status, result.reason = autolog.NEEDS_USER, str(e)
        failed, offer = failure(str(e))
        keep_open = True
        if not shown_table:                                  # stopped early (a question nobody answered): the table of what the form holds now
            try:
                for line in requiredgate.render(requiredgate.check(session.page, ctx, ctx.verified, ctx.lost, ctx.loop.status if ctx.loop else None, ctx.letter_note)):
                    out(line)
            except Exception:  # noqa: BLE001  (the page is gone; the reason is what matters)
                pass
        try:
            _shot(session.page, folder, "stopped", stamp, result)
        except Exception:  # noqa: BLE001  (the page is gone; the reason is what matters)
            pass
        return result
    except ArchiveError:
        raise
    except Exception as e:  # noqa: BLE001  (a timeout, a page that changed under us)
        from .apply.errors import is_closed_error

        if is_closed_error(e, getattr(session, "page", None)):           # v2.5: the window went away; code submitted nothing, nothing is asked
            result.status, result.reason = autolog.NOT_SUBMITTED, "BROWSER CLOSED: nothing submitted"
            out("\n  BROWSER CLOSED: nothing submitted. The answers are in answers_to_paste.txt beside the CV when they were planned.")
            return result
        result.status, result.reason = autolog.ERROR, f"{type(e).__name__}: {str(e)[:300]}"
        return result
    finally:
        if result.status == autolog.NEEDS_USER and keep_open:
            out(f"\n  STOPPED, hand-over: {result.reason}\n  The browser is left open for you; nothing on the page has been touched by this program.")
            if offer and ctx.typed(f"  Failed attempt {failed} of {failures.limit} on this job: there will be no third attempt. "
                                   "[h] hand off to your own browser (answers_to_paste.txt, CV and letter paths), or Enter to leave it > "
                                   ).lower() in ("h", "hand off", "handoff"):
                hand_off(f"{failed} failed attempts on this job: {result.reason}")
            else:
                ctx.typed("  Finish by hand if you want to, then press Enter to close the browser: ")
        session.close()
        autolog.append(settings, job=job, ats_strict=ats_strict, screenshots=result.screenshots, result=result.status, when=now())
