"""Apply assistant: opens the form, fills the basics, uploads PDFs, highlights what you must answer,
and STOPS. It never clicks submit — you do, then paste the confirmation into the terminal.

Why stop: visa/salary/EEO answers must be yours, ATS terms generally forbid automated submission,
and a reviewed form is the whole point of the human-in-the-loop gate.

Visa questions are answered from the plan for the job's work location W (apply/visa.py): the three booleans of
applicant.yaml `visa.by_country[W]` (else `.default`) and nothing else; a job whose W is unknown is refused before a browser opens. Every
visa field is highlighted and printed as `CHECK VISA ANSWER [W=XX]: question -> answer`; for a remote role at home XX is the remote label
(`visa.remote.label`), never the home country.

Every other field is filled by apply/filler.py, the same code the gated auto-apply uses: the standard fields from applicant.yaml (full
name, first name, last name, e-mail, phone, links, location), then the user's answers.yaml. A free-text question nobody has an answer for
("Describe an LLM feature ...") stays empty and is marked for you.

Equal-opportunities questions (apply/eo.py) are filled ONLY when applicant.yaml `equal_opportunities` or an `eo_*` entry of answers.yaml lists
them (ethnicity first), with the first form option that equals one of the `match_any` / `options` texts you wrote (case- and dash-insensitive);
a free-text box for an `eo_*` question gets that entry's `answer` as written ("Male"); no match, or a question that is not listed, stays empty. Every such field, filled or empty, is outlined blue and printed as `CHECK EO ANSWER [key]: question -> option`.
Nothing about equal opportunities is ever inferred from anything else.

The form is found by apply/formfind.py (Ashby `<job url>/application`, Lever `<job url>/apply`, Greenhouse: wait for it). No form field after
15 s prints `FORM NOT FOUND <url>` and no `Filled:` line. A bot check, "error verifying your application" or a Google / Microsoft sign-in page
ends the automated browsing: apply/handoff.py opens the page in your own browser and writes answers_to_paste.txt (see there).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..discovery import countries
from . import answers as answers_mod
from . import eo, filler, formfind
from .filler import LABEL_JS, QUESTION_JS, _matches, _norm
from ..discovery.workcountry import Work, rules_from
from .guard import check_job_files
from .visa import VisaPlan, plan_for, require_plan, work_for

# A field is a visa question when its label says so. These are answered from applicant.yaml -> visa.by_country, never from never_fill.
VISA_FIELD = re.compile(r"right to work|visa|sponsor|immigration|work permit|work authori[sz]ation|share code", re.I)
VISA_COLOUR = "3px solid magenta"
EO_COLOUR = "3px solid blue"
TEXT_TYPES = ("text", "textarea", "search", "")          # the boxes a free-text answer is typed into
APPLY_BUTTON = formfind.APPLY_BUTTON
OPEN_FORM_LINK = APPLY_BUTTON          # the text of the one control assist may press: the "Apply" link that opens the form (never a submit or a sign-in)


KIND_KEY = {"right_to_work_now": "right_to_work", "sponsorship_now": "sponsorship_now", "sponsorship_future": "sponsorship_future"}


def classify_visa(question: str) -> str | None:
    """Which kind of answer a Yes/No or dropdown visa question asks for; None when unclear (then it is
    left empty). "now or in the future" / "future" -> sponsorship_future; sponsorship now / currently / require ->
    sponsorship_now; "currently"/"now" + "right to work" -> right_to_work_now."""
    q = question.lower()
    if "share code" in q:
        return None
    if "right to work" in q:
        return "right_to_work_now" if re.search(r"\b(currently|now)\b", q) else None
    if "sponsor" in q:
        if "future" in q:
            return "sponsorship_future"
        if re.search(r"\b(now|currently|at this time|at present|require|need)\b", q):
            return "sponsorship_now"
    return None


def visa_value(plan: VisaPlan | None, kind: str | None) -> str | None:
    """'Yes' / 'No' from the plan's booleans for `kind` (a classify_visa result); None when there is no plan, no such answer, or it is
    not a boolean (the question is left to the user)."""
    key = KIND_KEY.get(kind) if kind else None
    return plan.answer(key) if plan is not None and key else None


def about_another_country(question: str, plan: VisaPlan | None) -> str | None:
    """The country a visa question names when it is not W ("right to work in the UK?" on an Irish job): the answers are for W, so
    the field is left empty rather than answered for the wrong country."""
    named = countries.mentions(question)
    return named[0] if plan is not None and named and plan.work.name not in named else None


def _answer_visa(el, typ: str, question: str, plan: VisaPlan | None) -> str | None:
    """Fill one visa field from the plan; returns what it now holds, None when it is left empty. Free text gets the plan's summary
    (a share-code box is left empty: it wants the code, not prose). Yes/No and dropdowns get the classified answer; radios are picked
    within their group."""
    if typ in ("text", "textarea", "search", ""):
        if "share code" in question.lower():
            return None
        summary = plan.summary if plan is not None else ""
        if summary and not (el.input_value() or "").strip():
            el.fill(summary)
        return _norm(el.input_value() or "") or None
    value = None if about_another_country(question, plan) else visa_value(plan, classify_visa(question))
    if value is None:
        return None
    want = value.lower()
    if typ == "select":
        for o in el.query_selector_all("option"):
            if _norm(o.inner_text()).lower() == want or (o.get_attribute("value") or "").lower() == want:
                el.select_option(label=o.inner_text())
                return value
        return None
    return None


def _eo_options(page, el, typ: str) -> list[tuple[str, object]]:
    """(text, element) of every choice of an equal-opportunities field: a select's options, a radio / checkbox group's labels. A text
    box has none."""
    if typ == "select":
        return [(_norm(o.inner_text()), o) for o in el.query_selector_all("option") if _norm(o.inner_text())]
    if typ in ("radio", "checkbox"):
        name = el.get_attribute("name")
        group = page.query_selector_all(f'input[type={typ}][name="{name}"]') if name else [el]
        return [(_norm((r.evaluate(LABEL_JS) or "").split("|")[0]), r) for r in group]
    return []


def _answer_eo(page, el, typ: str, entry: "eo.Entry | None") -> tuple[str | None, list[str], str]:
    """Pick the option of an equal-opportunities field: (the option chosen or None, the option texts, why nothing was chosen). Only a
    listed question is ever answered, and only with an option that equals one of its `match_any` texts."""
    options = _eo_options(page, el, typ)
    texts = [t for t, _ in options]
    if entry is None:
        return None, texts, "not listed in equal_opportunities: left blank"
    if not options:
        if typ in TEXT_TYPES and entry.key.startswith(eo.BOOK_PREFIX) and entry.answer.strip():
            if (el.input_value() or "").strip():
                return None, texts, "the text box already holds an answer: left as it is"
            el.fill(entry.answer.strip())                       # a free-text question: the answers.yaml answer, as written
            return entry.answer.strip(), texts, ""
        return None, texts, "no options to choose from (a text box): left blank"
    pick = eo.choose(texts, entry)
    if pick is None:
        return None, texts, "no option matched match_any: left blank"
    target = next(e for t, e in options if t == pick)
    if typ == "select":
        el.select_option(label=target.inner_text())
    else:
        target.check()
    return pick, texts, ""


class _Filled:
    """The `filled` / `blank` lists the shared filler writes to."""

    def __init__(self):
        self.filled: list[str] = []
        self.blank: list[str] = []


def fill_page(page, applicant: dict, cv_pdf: str | None, letter_pdf: str | None,
              letter_text: str | None = None, job=None, plan: VisaPlan | None = None,
              book: "answers_mod.AnswerBook | None" = None, history=None, freetext=None) -> dict:
    """Fill what we safely can on the current page. Returns a report of filled / manual / uploaded fields.

    With `job`, the wrong-file guard runs first: nothing is uploaded unless the CV and letter belong to that job.
    A cover-letter file field gets the letter PDF; a cover-letter text box gets `letter_text` (when still empty).
    Every other field is filled by apply/filler.py: applicant.yaml first, then `book` (the user's answers.yaml; none = no answers);
    `history(company)` says whether the user applied to that company before (answers.yaml's "previously applied" question).
    Visa fields are answered from `plan` (the visa plan of W); without one it is worked out from `job`, and with neither, from the
    home block (`visa.home`). A job whose W is unknown has no plan: every visa field stays empty.
    """
    if job is not None:
        check_job_files(job, cv_pdf, letter_pdf)
    if plan is None:
        home = rules_from(applicant).home or None
        plan = plan_for(applicant, work_for(job, applicant) if job is not None else
                        Work(home, "explicit", "no job given: the home block", shown=countries.place_name(home) if home else ""))
    where = (plan.work.shown if plan.work.home_remote else plan.work.country) if plan is not None else "?"
    report = {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []}
    seen_groups: set = set()
    letter_file = bool(letter_pdf) and any(                 # a cover-letter file field takes the PDF: then the text box (if any) is not pasted into too
        e.get_attribute("type") == "file" and re.search(r"cover", (e.evaluate(LABEL_JS) or ""), re.I)
        for e in page.query_selector_all("input[type=file]"))
    listed_eo = eo.entries(applicant, book)               # applicant.yaml first, then the eo_* entries of answers.yaml
    never = applicant.get("never_fill", [])
    ctx = filler.Ctx(dict(job) if job is not None else {}, applicant, book or answers_mod.AnswerBook(), Path("answers.yaml"), plan, cv_pdf, letter_pdf,
                     letter_text, ask=lambda prompt: "", out=print, history=history or (lambda company: False), result=_Filled(), strict=False,
                     freetext=freetext)

    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset") or not el.is_visible() and typ != "file":
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or typ
        question = _norm(el.evaluate(QUESTION_JS) if typ == "radio" else label.split("|")[0]).rstrip(" *")   # no "required" star

        if typ == "file":
            if re.search(r"cover", label, re.I) and letter_pdf:
                el.set_input_files(letter_pdf); report["uploaded"].append(f"{short} ← letter")
            elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and cv_pdf:
                el.set_input_files(cv_pdf); report["uploaded"].append(f"{short} ← CV")
            else:
                report["manual"].append(f"file: {short}")
            continue

        if letter_text and not letter_file and typ == "textarea" and re.search(r"cover\s*letter", label, re.I):
            # Before the never_fill check on purpose: 'cover letter' is in never_fill so that a *question* about
            # one is left to the user, but the letter text is ours and was reviewed.
            if not (el.input_value() or "").strip():
                el.fill(letter_text)
                report["filled"].append(f"{short} = cover letter text")
            continue

        if VISA_FIELD.search(f"{question} {label}"):
            # Before never_fill on purpose: visa/sponsor/right-to-work are in never_fill so that an unrecognised variant is
            # left to the user, but a recognised one is answered from the visa block. Every one is highlighted and printed.
            if typ == "radio":
                name = el.get_attribute("name")
                if name and name in seen_groups:
                    continue                                   # the rest of a group already answered
                seen_groups.add(name)
                group = page.query_selector_all(f'input[type=radio][name="{name}"]') if name else [el]
                value = None if about_another_country(question, plan) else visa_value(plan, classify_visa(question))
                done = None
                for r in group:
                    r.evaluate(f"e => e.style.outline = '{VISA_COLOUR}'")
                    opt = _norm((r.evaluate(LABEL_JS) or "").split("|")[0]).lower()
                    if value and done is None and value.lower() in (opt, (r.get_attribute("value") or "").lower()):
                        r.check()
                        done = value
                held = done
            else:
                el.evaluate(f"e => e.style.outline = '{VISA_COLOUR}'")
                held = _answer_visa(el, typ, question, plan)
            other = about_another_country(question, plan)
            print(f"CHECK VISA ANSWER [W={where}]: {question[:150]} -> {held or 'EMPTY'}"
                  + (f"   (the question is about {other}, W is {plan.work.shown}: left to you)" if other else ""))
            report["visa"].append({"question": question, "value": held, "work_location": where})
            report["filled" if held else "manual"].append(f"{short} = visa" if held else short)
            continue

        if eo.is_eo_field(f"{question} {label}"):
            # Before never_fill on purpose (its gender / ethnic / disab ... labels are left empty by default): only a question listed in
            # equal_opportunities is answered, with an option you wrote down; all of them are highlighted and printed.
            if typ in ("radio", "checkbox"):
                name = el.get_attribute("name")
                if name and ("eo", name) in seen_groups:
                    continue
                seen_groups.add(("eo", name))
            entry = eo.entry_for(f"{question} {label}", listed_eo)
            group_els = [e for _, e in _eo_options(page, el, typ)] if typ in ("radio", "checkbox") else [el]
            for g in group_els:
                g.evaluate(f"e => e.style.outline = '{EO_COLOUR}'")
            picked, texts, why = _answer_eo(page, el, typ, entry)
            shown = (" (" + why + (f"; options: {' | '.join(texts[:12])}" if why.startswith("no option matched") else "") + ")") if not picked else ""
            print(f"CHECK EO ANSWER [{entry.key if entry else 'not listed'}]: {question[:150]} -> {picked or 'EMPTY'}{shown}")
            report["eo"].append({"question": question, "key": entry.key if entry else None, "value": picked, "options": texts})
            report["filled" if picked else "manual"].append(f"{short} = eo" if picked else short)
            continue

        before = len(ctx.result.filled)
        filler.fill_field(page, el, tag, typ, label, short, question, filler.is_required(el, label.split("|")[0]), ctx, seen_groups)
        if len(ctx.result.filled) > before:                                        # applicant.yaml or answers.yaml answered it
            report["filled"].extend(ctx.result.filled[before:])
            continue

        if _matches(label, never):
            el.evaluate("e => e.style.outline = '3px solid orange'")
            report["manual"].append(short)
            continue

        required = el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"
        empty = typ in ("checkbox", "radio") and not el.is_checked() or \
            typ not in ("checkbox", "radio") and not (el.input_value() or "").strip()
        if required and empty:
            el.evaluate("e => e.style.outline = '3px solid red'")
            report["required_empty"].append(short)
    return report


def playwright_launch(profile_dir: Path):
    """(page, close) of a headed Chromium with a persistent profile. `close` never raises a Playwright error: a window the person has
    already closed (they submitted and closed it) is not a failure."""
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def close() -> None:
        for stop in (ctx.close, pw.stop):
            try:
                stop()
            except PlaywrightError:
                pass
    return page, close


def open_form_link(link) -> None:
    """formfind's `follow`: press `link` only when its whole text is OPEN_FORM_LINK; anything else (a submit, a sign-in, a bot check) is left alone."""
    if OPEN_FORM_LINK.match(link.inner_text() or ""): link.click()


def assist(job, rep: dict, applicant: dict, profile_dir: Path, *, launch=None, ask=input, book=None, history=None, freetext=None,
           handoff=None) -> dict:
    """Open the apply page, fill what we safely can and ask for the confirmation. Whatever happens to the browser (the person closed it
    after submitting, the page went away), the confirmation prompt is always reached and the browser teardown cannot raise a Playwright
    error, so the application record is never lost: `live` goes on to "Did YOU submit?" and mark-applied.
    `book` is the user's answers.yaml (answers.load), `history(company)` whether they applied there before (autoapply.default_history).
    `handoff` (apply/handoff.Handoff): where a bot check or sign-in wall sends the page; a host it already handed over opens no browser at all."""
    from playwright.sync_api import Error as PlaywrightError

    job = dict(job)                                        # a sqlite3.Row has no .get (hand-off and the guards read the job as a mapping)
    url = job["apply_url"] or job["url"]
    cv = rep.get("cv_pdf")
    letter = rep.get("letter_pdf") if rep.get("letter_pdf") and Path(rep["letter_pdf"]).is_file() else None
    plan = require_plan(job, applicant)                    # W unknown / no answers for it: WorkCountryError, nothing is opened
    check_job_files(job, cv, letter)                       # refuse before a browser even opens
    print(plan.work.line() + f"\n{plan.describe()}")
    letter_text = "\n\n".join((rep.get("letter") or {}).get("paragraphs") or []) or None
    if freetext is not None:
        freetext.ask = ask
    r = {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []}

    def hand_off(why: str, remember: bool = True) -> None:
        r["handoff"] = why
        if handoff is not None:
            handoff.run(job, rep, applicant, book, plan, url, why, r["filled"], remember=remember)
        else:
            print(f"\n  HAND-OFF: {why}\n  Open this in your own browser: {url}\n  CV: {cv}")

    known = handoff.known(url) if handoff is not None else None
    if known:                                               # this host turned the automated browser away before: no browser is opened
        hand_off(f"{formfind.host_of(url)} handed a job over before ({known})", remember=False)
        r["confirmation"] = ask("After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
        return r
    page, close = (launch or playwright_launch)(profile_dir)
    closed = False
    try:
        try:
            problem = formfind.open_form(page, url, job.get("apply_kind"),
                                         follow=open_form_link)
            if problem:                                     # no form field after 15 s: say so, never print an empty "Filled: []"
                print(f"\n{problem}\nThe page opened but shows no application form; apply by hand.")
                r["not_found"] = problem
            else:
                r = fill_page(page, applicant, cv, letter, letter_text=letter_text, job=job, plan=plan, book=book, history=history, freetext=freetext)
                stop = formfind.blocker(page)
                if stop:
                    raise formfind.HandOff(stop)
                print(f"\nFilled: {r['filled']}\nUploaded: {r['uploaded']}")
                print(f"YOU ANSWER (orange; visa fields magenta): {r['manual']}\nREQUIRED + EMPTY (red): {r['required_empty']}")
                print("\nReview everything in the browser and press Submit yourself.")
        except formfind.HandOff as e:                       # a bot check or a sign-in wall: nothing on it is touched; the person's own browser takes over
            hand_off(str(e))
            try:
                close()
            except PlaywrightError:
                pass
            closed = True
        except PlaywrightError as e:                        # the window is gone or the page failed: the prompt below is still asked
            print(f"\n(the browser or the page stopped answering: {str(e).splitlines()[0][:120]}). If you submitted the form, paste the "
                  "confirmation below; 'x' = did not submit.")
        conf = ask("After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
    finally:
        if not closed:
            try:
                close()
            except PlaywrightError:                         # already closed by the person: nothing left to tear down
                pass
    r["confirmation"] = conf
    return r
