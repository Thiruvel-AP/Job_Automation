"""Apply assistant: opens the form, fills the basics, uploads PDFs, highlights what you must answer,
and STOPS. It never clicks submit — you do, then paste the confirmation into the terminal.

Why stop: visa/salary/EEO answers must be yours, ATS terms generally forbid automated submission,
and a reviewed form is the whole point of the human-in-the-loop gate.

Visa questions are answered from the plan for the job's work location W (apply/visa.py): the three booleans of
applicant.yaml `visa.by_country[W]` (else `.default`) and nothing else; a job whose W is unknown is refused before a browser opens. Every
visa field is highlighted and printed as `CHECK VISA ANSWER [W=XX]: question -> answer`; for a remote role at home XX is the remote label
(`visa.remote.label`), never the home country.

Order (apply/verify.py): files first -- the CV only to the real Resume / CV field, never to an "Autofill from resume" control --, then a wait until
the form has not changed for 2 s, then the text fields, then every filled field is read back: `VERIFIED` / `LOST` per field, a LOST one is listed
under REQUIRED + EMPTY.

Every other field is filled by apply/filler.py, the same code the gated auto-apply uses: the standard fields from applicant.yaml (full
name, first name, last name, e-mail, phone, links, location), then the user's answers.yaml. Every free-text question, required or not, goes to the
form loop (apply/formloop.py): the form is scanned and its fields printed before anything is filled; an answer you approved for a similar question
is filled as it is, any other gets a draft from your fact bank in the field at once, marked NEW DRAFT (and WEAK EVIDENCE when the bank does not cover
what is asked), and you are asked `[y] use / [e] edit` for it; an optional question of no kind a draft can answer ("anything else?") stays empty.

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
from . import route, answers as answers_mod
from . import ats_vendor, attest, dryrun, eo, failures as failures_mod, filler, formfind, formloop, handoff as handoff_mod, knockout, letter_demand, requiredgate, shortfields, verify
from .filler import LABEL_JS, QUESTION_JS, _matches, _norm
from ..discovery.workcountry import Work, rules_from
from .errors import AccountRequired, BrowserClosed, is_closed_error
from . import stale
from .guard import check_job_files
from .visa import VisaPlan, plan_for, require_plan, work_for

# A field is a visa question when its label says so. These are answered from applicant.yaml -> visa.by_country, never from never_fill.
VISA_FIELD = re.compile(r"right to work|working rights|visa|sponsor|immigration|work permit|work authori[sz]ation|share code", re.I)
VISA_COLOUR = "3px solid magenta"


def is_visa_question(text: str) -> bool:
    """A visa / right-to-work question the visa plan answers. The visa TYPE ("please specify the type of visa you hold") is not one: it is a short
    answer of its own that only answers.yaml `visa_type` gives (apply/shortfields.py), never the plan's Graduate-visa paragraph."""
    return bool(VISA_FIELD.search(text)) and not shortfields.is_visa_type(text)


EO_COLOUR = "3px solid blue"
TEXT_TYPES = ("text", "textarea", "search", "")          # the boxes a free-text answer is typed into
APPLY_BUTTON = formfind.APPLY_BUTTON
OPEN_FORM_LINK = APPLY_BUTTON          # the text of the one control assist may press: the "Apply" link that opens the form (never a submit or a sign-in)


attestation_lines = attest.lines
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


COMBINED_SPONSORSHIP = re.compile(r"\b(?:now|currently|at\s+present|at\s+this\s+time)\b.{0,25}\b(?:or|and)\b.{0,25}\b(?:in\s+(?:the\s+)?future|future|later|going\s+forward)\b", re.I)


def sponsorship_answer(plan: VisaPlan | None, question: str) -> str | None:
    """The Yes / No for a visa question. "Now or in the future" asks both at once: Yes when either sponsorship_now or sponsorship_future is Yes, No when
    both are No, None when neither is Yes and one is not a boolean. Any other question is classify_visa + visa_value."""
    kind = classify_visa(question)
    if kind == "sponsorship_future" and COMBINED_SPONSORSHIP.search(question) and plan is not None:
        values = [plan.answers.get("sponsorship_now"), plan.answers.get("sponsorship_future")]
        if any(v is True for v in values):
            return "Yes"
        return "No" if all(v is False for v in values) else None
    return visa_value(plan, kind)


# A working-rights question ("What are your working rights in the country where this role is advertised for?") with a list of statuses to choose from
# (Citizen / Permanent Resident / Work Visa / Require sponsorship). It is answered from applicant.yaml (visa.by_country: right_to_work, sponsorship_now and
# the summary) and from nothing else; the visa TYPE question ("please specify the type of visa you hold") is a different target (apply/shortfields.py).
WORKING_RIGHTS = re.compile(r"\bworking rights\b|\bright to work\b|\b(?:eligible|eligibility|entitled|permitted|authori[sz]ed|allowed) to work\b|"
                            r"\bwork(?:ing)? (?:eligibility|authori[sz]ation|status)\b", re.I)
_STATUS_NEEDS_SPONSOR = re.compile(r"sponsor|require|need|no right|not (?:have|entitled|eligible)", re.I)
_STATUS_HOLDS_VISA = re.compile(r"visa|permit", re.I)
_STATUS_NOT_A_HOLDER = re.compile(r"citizen|national|settled|permanent|indefinite|british|irish|sponsor|require|need|\bnot\b|\bno\b", re.I)
_PLACEHOLDER = ("select", "choose", "please", "--")


def rights_pick(plan: VisaPlan | None, question: str, options: list[str]) -> tuple[str | None, str]:
    """(the option to choose, why not) for a working-rights drop-down or radio group. Only applicant.yaml answers it:
    - a Yes / No list and the words "working rights": right_to_work. (A plain "do you have the right to work" Yes / No stays with `classify_visa`:
      without "now" or "currently" it is unclear and is left to you.)
    - a list of statuses: right_to_work false or sponsorship_now true -> the one option that asks for sponsorship; right_to_work true and
      sponsorship_now false -> the one option that names a visa / permit and is neither a citizen, settled, permanent nor a sponsorship option, and only
      when the plan's own summary says you hold a visa (otherwise a citizen and a visa holder cannot be told apart).
    None, with the reason, whenever the plan, a boolean or exactly one fitting option is missing: nothing is guessed."""
    if plan is None:
        return None, "no visa plan for this job (applicant.yaml visa.by_country)"
    if not WORKING_RIGHTS.search(question):
        return None, "not a working-rights question"
    other = about_another_country(question, plan)
    if other:
        return None, f"the question is about {other}, W is {plan.work.shown}"
    opts = [o for o in (_norm(x) for x in options) if o and not o.lower().startswith(_PLACEHOLDER)]
    low = {o.lower(): o for o in opts}
    rtw, now = plan.answers.get("right_to_work"), plan.answers.get("sponsorship_now")
    if not isinstance(rtw, bool):
        return None, "applicant.yaml right_to_work is not a Yes / No"
    if {"yes", "no"} <= set(low):
        if re.search(r"\bworking rights\b", question, re.I):
            return low["yes" if rtw else "no"], ""
        return None, "a Yes / No 'right to work' question without 'now' or 'currently' is left to you"
    if not isinstance(now, bool):
        return None, "applicant.yaml sponsorship_now is not a Yes / No"
    if not rtw or now:
        hits = [o for o in opts if _STATUS_NEEDS_SPONSOR.search(o)]
        basis = "applicant.yaml says you need sponsorship or have no right to work"
    elif not _STATUS_HOLDS_VISA.search(plan.summary or ""):
        return None, "applicant.yaml's visa summary does not say you hold a visa, so Citizen / Settled / Work Visa cannot be told apart"
    else:
        hits = [o for o in opts if _STATUS_HOLDS_VISA.search(o) and not _STATUS_NOT_A_HOLDER.search(o)]
        basis = "applicant.yaml says you have the right to work, need no sponsorship now, and hold a visa"
    if len(hits) == 1:
        return hits[0], ""
    return None, f"{basis}, but {len(hits)} of the options fit ({', '.join(opts[:6]) or 'none listed'})"


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
    value = None if about_another_country(question, plan) else sponsorship_answer(plan, question)
    if value is None and typ == "select":                                   # a working-rights drop-down: from applicant.yaml, by its own options
        value = rights_pick(plan, question, [o.inner_text() for o in el.query_selector_all("option")])[0]
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


UNCHECKED_NO = "left unchecked (answer: No)"          # what _answer_eo returns for a single check box whose answer is No


def _answer_eo(page, el, typ: str, entry: "eo.Entry | None", track=None) -> tuple[str | None, list[str], str]:
    """Pick the option of an equal-opportunities field: (the option chosen or None, the option texts, why nothing was chosen). Only a
    listed question is ever answered, and only with an option that equals one of its `match_any` texts. A single check box whose answer is
    No stays unchecked and returns UNCHECKED_NO (an answer, not an empty field). `track(element, kind, value)` notes what was filled."""
    options = _eo_options(page, el, typ)
    texts = [t for t, _ in options]
    if entry is None:
        return None, texts, "not listed in equal_opportunities: left blank"
    if not options:
        if typ in TEXT_TYPES and entry.key.startswith(eo.BOOK_PREFIX) and entry.answer.strip():
            if (el.input_value() or "").strip():
                return None, texts, "the text box already holds an answer: left as it is"
            el.fill(entry.answer.strip())                       # a free-text question: the answers.yaml answer, as written
            if track:
                track(el, "text", entry.answer.strip())
            return entry.answer.strip(), texts, ""
        return None, texts, "no options to choose from (a text box): left blank"
    if typ == "checkbox" and len(options) == 1 and eo.answers_no(entry):
        return UNCHECKED_NO, texts, ""
    pick = eo.choose(texts, entry)
    if pick is None:
        return None, texts, "no option matched match_any: left blank"
    if typ in ("radio", "checkbox") and filler.lever_page(page):
        return None, texts, f"Lever: radio buttons and check boxes are never set by code, you click it; recommended: {pick}"
    target = next(e for t, e in options if t == pick)
    if typ == "select":
        el.select_option(label=target.inner_text())
        if track:
            track(el, "select", pick)
    else:
        target.check()
        if track:
            track(target, "check", pick)
    return pick, texts, ""


class _Filled:
    """The `filled` / `blank` lists the shared filler writes to."""

    def __init__(self):
        self.filled: list[str] = []
        self.blank: list[str] = []


def _upload_files(page, cv_pdf: str | None, letter_pdf: str | None, report: dict) -> bool:
    """Pass 1 of fill_page: every file field, before any text field (an upload can make the form re-render and wipe what was typed). The
    "Autofill from resume" control is skipped; the CV goes only to the real Resume / CV field, the letter PDF to a cover-letter field.
    True when a file was set."""
    sent = False
    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        if tag != "input" or (el.get_attribute("type") or "").lower() != "file":
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or "file"
        if verify.is_autofill(el):
            print(f"  skipped the autofill upload control (never used): {short}")
            report.setdefault("skipped", []).append(f"file: {short} (autofill)")
        elif re.search(r"cover", label, re.I) and letter_pdf:
            el.set_input_files(letter_pdf); report["uploaded"].append(f"{short} ← letter"); sent = True
        elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and cv_pdf:
            el.set_input_files(cv_pdf); report["uploaded"].append(f"{short} ← CV"); sent = True
        else:
            report["manual"].append(f"file: {short}")
    return sent


def fill_page(page, applicant: dict, cv_pdf: str | None, letter_pdf: str | None,
              letter_text: str | None = None, job=None, plan: VisaPlan | None = None,
              book: "answers_mod.AnswerBook | None" = None, history=None, freetext=None, quiet_ms: int = verify.QUIET_MS,
              knock_ask=None, fact_bank=None, ask=None, letter_note: str = "", record: bool = False) -> dict:
    """Fill what we safely can on the current page. Returns a report of filled / manual / uploaded fields.

    Order (apply/verify.py): the files first (never the "Autofill from resume" control); then, if a file was uploaded, wait until the form has
    not changed for `quiet_ms`; then every text field, select and box; then each filled field is read back, an empty one is filled once more,
    and `VERIFIED` / `LOST` is printed per field. A LOST field is listed in `required_empty` (and `lost`).

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
    report = {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": [], "recommended": [], "attestations": []}
    seen_groups: set = set()
    letter_file = bool(letter_pdf) and any(                 # a cover-letter file field takes the PDF: then the text box (if any) is not pasted into too
        e.get_attribute("type") == "file" and re.search(r"cover", (e.evaluate(LABEL_JS) or ""), re.I)
        for e in page.query_selector_all("input[type=file]"))
    listed_eo = eo.entries(applicant, book)               # applicant.yaml first, then the eo_* entries of answers.yaml
    never = applicant.get("never_fill", [])
    tracker = verify.Tracker()
    ctx = filler.Ctx(dict(job) if job is not None else {}, applicant, book or answers_mod.AnswerBook(), Path("answers.yaml"), plan, cv_pdf, letter_pdf,
                     letter_text, ask=ask or (lambda prompt: ""), out=print, history=history or (lambda company: False), result=_Filled(), strict=False,
                     freetext=freetext, tracker=tracker)
    ctx.letter_note = letter_note
    ctx.combobox = shortfields.generic_combobox                           # a script-built drop-down: its options are read, then the answers.yaml one is picked
    loop = ctx.loop = formloop.FormLoop(ctx, formloop.output_folder(ctx.job, cv_pdf))
    fields = loop.begin(page)                                            # every field of the form, printed before anything is filled
    knocks = knockout.scan([(f.question, f.options) for f in fields], applicant, book, ctx.job, plan, fact_bank)       # before anything is uploaded or drafted
    if knocks and knock_ask is not None and not knockout.confirm(knocks, knock_ask, print):
        raise knockout.SkipJob(knockout.stop_reason(knocks))
    if knocks and knock_ask is None:
        for k in knocks:
            print(f"  {k.line()}")

    if _upload_files(page, cv_pdf, letter_pdf, report):                  # pass 1: the files, then the form is left to finish re-rendering
        if not verify.wait_quiet(page, quiet_ms):
            print(f"  the form was still changing {verify.QUIET_MAX_MS // 1000} s after the upload; filling it anyway (everything is read back below)")

    def fill_element(el) -> None:                                       # one element of the form (also used for a field that appears after an answer)
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset", "file") or not el.is_visible():
            return
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or typ
        question = _norm(el.evaluate(QUESTION_JS) if typ == "radio" else label.split("|")[0]).rstrip(" *")   # no "required" star

        if letter_text and not letter_file and typ == "textarea" and re.search(r"cover\s*letter", label, re.I):
            # Before the never_fill check on purpose: 'cover letter' is in never_fill so that a *question* about
            # one is left to the user, but the letter text is ours and was reviewed.
            if not (el.input_value() or "").strip():
                el.fill(letter_text)
                tracker.add(el, short, "text", letter_text)
                report["filled"].append(f"{short} = cover letter text")
            return

        lever_note = ""
        if is_visa_question(f"{question} {label}"):
            # Before never_fill on purpose: visa/sponsor/right-to-work are in never_fill so that an unrecognised variant is
            # left to the user, but a recognised one is answered from the visa block. Every one is highlighted and printed.
            if typ == "radio":
                name = el.get_attribute("name")
                if name and name in seen_groups:
                    return                                   # the rest of a group already answered
                seen_groups.add(name)
                group = page.query_selector_all(f'input[type=radio][name="{name}"]') if name else [el]
                value = None if about_another_country(question, plan) else sponsorship_answer(plan, question)
                if value is None:                                                # a working-rights group of statuses: applicant.yaml, by its own labels
                    value = rights_pick(plan, question, [_norm((r.evaluate(LABEL_JS) or "").split("|")[0]) for r in group])[0]
                lever = filler.lever_page(page)                  # never set by code on Lever: the answer is recommended, you click it
                if lever:
                    filler.hand_over(ctx, question, value)
                    lever_note = f"   (Lever: radio buttons are never set by code; you click it, recommended: {value or 'your answer'})"
                done = None
                for r in group:
                    r.evaluate(f"e => e.style.outline = '{VISA_COLOUR}'")
                    opt = _norm((r.evaluate(LABEL_JS) or "").split("|")[0]).lower()
                    if value and not lever and done is None and value.lower() in (opt, (r.get_attribute("value") or "").lower()):
                        r.check()
                        tracker.add(r, question, "check")
                        done = value
                held = done
            else:
                el.evaluate(f"e => e.style.outline = '{VISA_COLOUR}'")
                combo = shortfields.is_combobox(el, typ)
                was_empty = typ in TEXT_TYPES and not combo and not (el.input_value() or "").strip()
                if combo:                                                        # a script-built drop-down: open it, read its options, pick one from applicant.yaml
                    held, why = shortfields.visa_combobox(page, el, question, plan, ctx)
                    if not held:
                        ctx.needs_user[question] = why
                else:
                    held = _answer_visa(el, typ, question, plan)
                if held and typ == "select":
                    tracker.add(el, question, "select", held)
                elif held and was_empty:                       # a text box this run typed into (one that already held the answer is not ours)
                    tracker.add(el, question, "text", held)
            other = about_another_country(question, plan)
            print(f"CHECK VISA ANSWER [W={where}]: {question[:150]} -> {held or 'EMPTY'}"
                  + (f"   (the question is about {other}, W is {plan.work.shown}: left to you)" if other else "") + lever_note)
            report["visa"].append({"question": question, "value": held, "work_location": where})
            report["filled" if held else "manual"].append(f"{short} = visa" if held else short)
            return

        if eo.is_eo_field(f"{question} {label}"):
            # Before never_fill on purpose (its gender / ethnic / disab ... labels are left empty by default): only a question listed in
            # equal_opportunities is answered, with an option you wrote down; all of them are highlighted and printed.
            if typ in ("radio", "checkbox"):
                name = el.get_attribute("name")
                if name and ("eo", name) in seen_groups:
                    return
                seen_groups.add(("eo", name))
            entry = eo.entry_for(f"{question} {label}", listed_eo)
            group_els = [e for _, e in _eo_options(page, el, typ)] if typ in ("radio", "checkbox") else [el]
            for g in group_els:
                g.evaluate(f"e => e.style.outline = '{EO_COLOUR}'")
            picked, texts, why = _answer_eo(page, el, typ, entry, track=lambda e, kind, value, q=question: tracker.add(e, q, kind, value))
            if why.startswith("Lever"):
                ctx.recommended.append(f"{question[:100]} -> {why.split('recommended: ')[-1]}")
            if picked == UNCHECKED_NO:                       # a single check box and the answer is No: nothing to tick, and nothing is missing
                ctx.source(question, "answered No (equal opportunities)")
                print(f"CHECK EO ANSWER [{entry.key}]: {question[:150]} -> {picked}")
                report["eo"].append({"question": question, "key": entry.key, "value": "No", "options": texts})
                return
            shown = (" (" + why + (f"; options: {' | '.join(texts[:12])}" if why.startswith("no option matched") else "") + ")") if not picked else ""
            print(f"CHECK EO ANSWER [{entry.key if entry else 'not listed'}]: {question[:150]} -> {picked or 'EMPTY'}{shown}")
            report["eo"].append({"question": question, "key": entry.key if entry else None, "value": picked, "options": texts})
            report["filled" if picked else "manual"].append(f"{short} = eo" if picked else short)
            return

        before = len(ctx.result.filled)
        filler.fill_field(page, el, tag, typ, label, short, question, filler.is_required(el, label.split("|")[0]), ctx, seen_groups)
        if len(ctx.result.filled) > before:                                        # applicant.yaml or answers.yaml answered it
            report["filled"].extend(ctx.result.filled[before:])
            return
        if question in ctx.attestations:                                           # an attestation: outlined magenta, never answered, the human sets it
            if filler.is_required(el, label.split("|")[0]):
                report["required_empty"].append(short)
            return

        if _matches(label, never):
            el.evaluate("e => e.style.outline = '3px solid orange'")
            report["manual"].append(short)
            return

        required = el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"
        empty = typ in ("checkbox", "radio") and not el.is_checked() or \
            typ not in ("checkbox", "radio") and not (el.input_value() or "").strip()
        if required and empty:
            el.evaluate("e => e.style.outline = '3px solid red'")
            report["required_empty"].append(short)

    loop.handle_new = fill_element
    for el in page.query_selector_all("input, textarea, select"):       # pass 2: fresh handles, the form may have replaced its nodes
        fill_element(el)
    loop.run(page)                                                       # pass 2b: the free-text fields, one by one, the form scanned again after each
    report["filled"].extend(loop.added)
    report["manual"].extend(f"{d.short or d.question[:60]} (NEW DRAFT: check it)" for d in loop.drafts if d.status == "NEW DRAFT (unapproved)")
    report["required_empty"] = [x for x in report["required_empty"] if x not in loop.filled_shorts]
    report["recommended"] = list(ctx.recommended)                        # radios and check boxes code may not set (Lever): the human sets them
    report["manual"].extend(ctx.recommended)
    report["attestations"] = list(ctx.attestations)
    report["drafts"] = loop.paste_drafts()

    report["verified"], report["lost"] = [], []
    if tracker.fills:                                                    # pass 3: read back everything this run filled
        print("\nReading back every field that was filled:")
        report["verified"], report["lost"] = tracker.verify(page)
        report["required_empty"].extend(x for x in report["lost"] if x not in report["required_empty"])
    rows = requiredgate.check(page, ctx, report["verified"], report["lost"], loop.status, getattr(ctx, "letter_note", ""))        # F4: every required field, one status
    report["required_table"] = [r.as_dict() for r in rows]
    report["needs_user"] = dict(ctx.needs_user)
    report["sources"] = dict(ctx.sources)
    if record:                                                           # the dry-run record (apply/dryrun.py) wants every field, not only the required ones
        report["all_fields"] = [x.as_dict() for x in requiredgate.check(page, ctx, report["verified"], report["lost"], loop.status,
                                                                          getattr(ctx, "letter_note", ""), required_only=False)]
    print()
    for line in requiredgate.render(rows):
        print(line)
    report["required_empty"].extend(r.label[:60] for r in requiredgate.blocking(rows)
                                    if r.status != "NO FORM" and r.label[:60] not in report["required_empty"])      # "no application form found" is printed, not a field
    return report


def playwright_launch(profile_dir: Path):
    """(page, close) of a headed Chromium with a persistent profile. `close` never raises a Playwright error: a window the person has
    already closed (they submitted and closed it) is not a failure."""
    from playwright.sync_api import Error as PlaywrightError

    from .browser import CHROMIUM_ARGS, start_playwright

    pw = start_playwright()
    ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False, args=CHROMIUM_ARGS)
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


STANDARD_VENDORS = ("greenhouse", "lever", "ashby")         # the forms apply/filler.py was built for; any other vendor goes to the form agent


def make_agent(page, applicant: dict, book, job: dict, plan, cv: str | None, letter: str | None, letter_text: str | None, history, freetext):
    """The form agent (apply/formagent.py) for this job: values only from applicant.yaml, answers.yaml, the visa plan and this job's CV and letter; a
    free-text question goes to `freetext`, which drafts it from the fact bank for the person to approve."""
    from . import formagent as fa

    bank = fa.ValueBank.build(applicant, book, job, plan, cv_pdf=cv, letter_pdf=letter, letter_text=letter_text)
    planner = fa.RulePlanner(applicant, book, job, plan, cv, letter, history=history or (lambda company: False), letter_text=letter_text, freetext=freetext)
    return fa.FormAgent(page, planner, bank, cv_pdf=cv, letter_pdf=letter, folder=formloop.output_folder(job, cv), out=print)


def agent_report(res) -> dict:
    """An AgentResult as the report fill_page returns."""
    return {"filled": list(res.filled), "manual": list(res.manual) + list(res.recommended), "uploaded": list(res.uploaded),
            "required_empty": list(res.required_empty), "visa": [], "eo": [], "recommended": list(res.recommended), "attestations": list(res.attestations),
            "lost": list(res.lost), "submit_controls": list(res.submit_controls), "agent": res.outcome, "agent_reason": res.reason,
            "pasted": list(res.pasted), "drafts": []}


def write_unpasted(job: dict, rep: dict, applicant: dict, book, plan, url: str, pasted: list[tuple[str, str]], filled: list[str]) -> Path | None:
    """answers_to_paste.txt for the answers the form would neither take typed nor pasted (apply/formagent.py): the person pastes them."""
    folder = job.get("out_dir") or (Path(rep["cv_pdf"]).parent if rep.get("cv_pdf") else None)
    if not folder:
        return None
    drafts = [{"question": q, "answer": a, "status": "the form would not take this: paste it yourself"} for q, a in pasted]
    path = Path(folder) / handoff_mod.PASTE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(handoff_mod.paste_text(job, rep, applicant, book, plan, url, "the form would not keep some answers (typed, then pasted)", filled, None, drafts),
                        encoding="utf-8", newline="\n")
    except OSError:
        return None
    return path


def closed_browser(job: dict, rep: dict, applicant: dict, book, plan, url: str, filled: list[str], drafts=None, paste: Path | None = None) -> Path | None:
    """`BROWSER CLOSED: nothing submitted` (v2.5): the page, its context or the browser went away while the form was read or filled. Code submitted
    nothing; answers_to_paste.txt holds every answer (written from the plan before the browser opened, else now); the confirmation is never asked."""
    if paste is None:
        folder = job.get("out_dir") or (Path(rep["cv_pdf"]).parent if rep.get("cv_pdf") else None)
        if folder:
            paste = Path(folder) / handoff_mod.PASTE_FILE
            try:
                paste.parent.mkdir(parents=True, exist_ok=True)
                paste.write_text(handoff_mod.paste_text(job, rep, applicant, book, plan, url, "the browser was closed before the form was submitted",
                                                        filled, None, drafts), encoding="utf-8", newline="\n")
            except OSError:
                paste = None
    print("\nBROWSER CLOSED: nothing submitted." + (f" Every answer is in {paste}" if paste else "") +
          f"\n  The job is marked not submitted. Apply later with careerops apply --id {job.get('id')} (or paste the answers into {url}).")
    return paste


def account_required(r: dict, why: str, url: str, ask=input, announce_only: bool = False) -> dict:
    """A sign-in page (apply/route.py): nothing typed, status `manual_apply_account_required`, the listing URL printed. Returns the result `r`."""
    print("\n  " + route.account_line(why, url))
    r["account_required"], r["listing_url"], r["confirmation"] = why, url, "x"
    return r


def assist(job, rep: dict, applicant: dict, profile_dir: Path, *, launch=None, ask=input, book=None, history=None, freetext=None,
           handoff=None, failures=None, fact_bank=None, tokens=(), letter_maker=None, record=False, planner=None) -> dict:
    """Open the apply page, fill what we safely can and ask for the confirmation. The browser teardown cannot raise a Playwright error. A browser
    that closes before the form is submitted ends the job at once (`BROWSER CLOSED: nothing submitted`, `browser_closed` in the result, no
    confirmation prompt: v2.5); `live` then marks the job not submitted.
    `planner` (apply/planfirst.Planner, cli.py builds it): plan first, then fill (v2.5): the questions, the letter, the drafts and the whole answer
    plan are made and printed before any browser opens, and the form is filled from the plan in a fresh browser. Without it, the earlier fill
    (fill_page) runs; the letter is still written before the browser opens.
    `book` is the user's answers.yaml (answers.load), `history(company)` whether they applied there before (autoapply.default_history).
    `handoff` (apply/handoff.Handoff): where a bot check or sign-in wall sends the page; a host it already handed over, or one on
    `apply.handoff_hosts`, opens no browser at all. `failures` (apply/failures.py) counts the failed attempts of each job (FORM NOT FOUND, a LOST
    field, an upload the form refused, "error verifying your application"): the second offers `[h] hand off`, a job that has used them up goes
    straight to the hand-off, and the result then has `handoff` set (`live` ends the job `handed_off`)."""
    from playwright.sync_api import Error as PlaywrightError

    job = dict(job)                                        # a sqlite3.Row has no .get (hand-off and the guards read the job as a mapping)
    url = job["apply_url"] or job["url"]
    cv = rep.get("cv_pdf")
    letter = rep.get("letter_pdf") if rep.get("letter_pdf") and Path(rep["letter_pdf"]).is_file() else None
    plan = require_plan(job, applicant)                    # W unknown / no answers for it: WorkCountryError, nothing is opened
    check_job_files(job, cv, letter)                       # refuse before a browser even opens
    stale.require_fresh(job, cv, letter)                   # a CV or letter older than the tailoring code: run with --retailor (9 Nov 2026)
    print(plan.work.line() + f"\n{plan.describe()}")
    letter_text = "\n\n".join((rep.get("letter") or {}).get("paragraphs") or []) or None
    if freetext is not None:
        freetext.ask = ask
    r = {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []}

    def hand_off(why: str, remember: bool = True) -> None:
        r["handoff"] = why
        if handoff is not None:
            handoff.run(job, rep, applicant, book, plan, url, why, r["filled"], remember=remember, drafts=r.get("drafts"))
        else:
            print(f"\n  HAND-OFF: {why}\n  Open this in your own browser: {url}\n  CV: {cv}")

    why = route.auth_url_reason(url)
    if why:                                                 # the address itself is a sign-in page: no browser is opened at all
        return account_required(r, why, url, ask)
    if route.is_aggregator(job):                            # a job board (Reed, CV-Library, Indeed, LinkedIn ...): its form is never entered
        employer = route.employer_url(job)
        if employer:
            print(f"  {route.listing_url(job)} is a job board; the employer's own application address was found: {employer}")
            url = job["apply_url"] = employer
        else:
            why = "aggregator listing: no employer application page was found; the apply pack is ready and the listing is opened for you"
            r["aggregator"] = True
            hand_off(why, remember=False)                   # CV, letter, answers_to_paste.txt, the listing opened; no form is opened by code
            r["confirmation"] = "x"
            return r
    why = route.auth_url_reason(url)                        # the employer's address may itself be a sign-in page
    if why:
        return account_required(r, why, url, ask)
    used_up = failures.gate(job["id"]) if failures is not None else None
    known = (handoff.why(url) if handoff is not None else "") or used_up
    if known:                                               # a host that turned the automated browser away, or a job out of attempts: no browser is opened
        hand_off(known, remember=False)
        r["confirmation"] = ask("After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
        return r
    if planner is not None:
        from . import planned
        return planned.run(job, rep, applicant, profile_dir, plan=plan, cv=cv, letter=letter, letter_text=letter_text, launch=launch, ask=ask, book=book,
                           history=history, freetext=freetext, handoff=handoff, failures=failures, fact_bank=fact_bank, tokens=tokens,
                           letter_maker=letter_maker, record=record, planner=planner, url=url)
    if letter is None and letter_maker is not None:                     # the letter is written BEFORE any browser opens (v2.5), never inside one
        from . import planfirst
        rep, letter, letter_text, _ = planfirst.letter_first(rep, letter, letter_text, letter_maker, True, print)
        if letter:
            check_job_files(job, cv, letter)
    page, close = (launch or playwright_launch)(profile_dir)
    closed = False

    def count(text: str) -> bool:
        """File this attempt's failure (one of the four kinds) and say whether the hand-off is to be offered now."""
        kind = failures_mod.kind_of(text)
        if failures is None or not kind:
            return False
        n = failures.record(job["id"], url, kind, text)
        if n < failures.limit:
            return False
        print(f"\n  Failed attempt {n} of {failures.limit} on this job ({text}): there will be no third attempt.")
        return ask("  [h] hand off to your own browser (answers_to_paste.txt, CV and letter paths), or Enter to leave it > ").strip().lower() in (
            "h", "hand off", "handoff")
    try:
        try:
            direct = ats_vendor.direct_url(url, kind=job.get("apply_kind"), job=job, tokens=tokens)
            vendor = ats_vendor.vendor_of(direct)
            other = bool(vendor) and vendor not in STANDARD_VENDORS and job.get("apply_kind") not in STANDARD_VENDORS      # a real host that is not one of the three
            can_agent = callable(getattr(page, "evaluate", None))       # a real browser page (the fake pages of some tests have no evaluate)
            if vendor == "ashby" and history is not None and history(str(job.get("company") or "")):
                print(f"  WARNING: Ashby merges candidates by email; an earlier application to {job.get('company')} exists.")
            agent, by_agent = None, None
            if not other or not can_agent:                              # order: the direct ATS address, then the existing filler ...
                problem = formfind.open_form(page, url, job.get("apply_kind"), follow=open_form_link, job=job, tokens=tokens)
                if problem and can_agent:                               # ... and the form agent when no form is found
                    agent = make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext)
                    if agent.run("reach").outcome == "reached_form":
                        problem = None
            else:                                                       # any other vendor: the form agent
                page.goto(direct, wait_until="domcontentloaded")
                page.wait_for_timeout(formfind.SETTLE_MS)
                agent = make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext)
                reached = agent.run("reach")
                problem = None if reached.outcome == "reached_form" else f"FORM NOT FOUND {direct}"
                if problem is None:
                    by_agent = agent.run("fill")
            if problem:                                     # no form field after 15 s: say so, never print an empty "Filled: []"
                print(f"\n{problem}\nThe page opened but shows no application form; apply by hand.")
                r["not_found"] = problem
                if count(problem):
                    raise formfind.HandOff(f"{failures.limit} failed attempts on this job: {problem}")
            else:
                note = ""
                if by_agent is None:                        # letter.mode if_field: a form that asks for a cover letter gets one, written now if need be
                    from ..tailoring.letter_reason import letter_reason

                    prov = letter_demand.deliver(page, rep, letter, letter_text, print, reason=letter_reason(rep.get("letter")))
                    letter, letter_text, note = prov.pdf, prov.text, prov.note
                    if prov.handoff:
                        raise formfind.HandOff(prov.handoff)
                r = agent_report(by_agent) if by_agent is not None else fill_page(
                    page, applicant, cv, letter, letter_text=letter_text, job=job, plan=plan, book=book, history=history, freetext=freetext,
                    knock_ask=ask, fact_bank=fact_bank, ask=ask, letter_note=note, record=record)
                if record and by_agent is None and r.get("all_fields"):
                    try:
                        dryrun.record(dryrun.folder_for(rep, job), page, job, rep, [requiredgate.Row(**d) for d in r.pop("all_fields")],
                                      cv=cv, letter=letter, letter_text=letter_text, freetext=freetext)
                    except OSError as e:
                        print(f"  dry-run record not written: {e}")
                r.pop("all_fields", None)
                refused = verify.upload_refused(page)
                trouble = (f"the form refused an upload: {refused}" if refused else
                           f"the form emptied field(s) after they were filled: {'; '.join(r['lost'][:5])}" if r.get("lost") else "")
                if trouble and count(trouble):
                    raise formfind.HandOff(f"{failures.limit} failed attempts on this job: {trouble}")
                stop = formfind.blocker(page)
                if stop:
                    raise formfind.HandOff(stop)
                print(f"\nFilled: {r['filled']}\nUploaded: {r['uploaded']}")
                print(f"YOU ANSWER (orange; visa fields magenta): {r['manual']}\nREQUIRED + EMPTY (red): {r['required_empty']}")
                for line in attest.lines(r.get("attestations") or []):
                    print(line)
                if r.get("agent"):
                    print(f"Form agent: {r['agent']}" + (f" ({r['agent_reason']})" if r.get("agent_reason") else "")
                          + f"\nSubmit controls (listed, never clicked by the agent): {r.get('submit_controls')}")
                if r.get("pasted"):
                    saved = write_unpasted(job, rep, applicant, book, plan, url, r["pasted"], r["filled"])
                    print(f"The form would not take {len(r['pasted'])} answer(s), typed or pasted: " + (f"they are in {saved}" if saved else "paste them yourself"))
                print("\nReview everything in the browser and press Submit yourself.")
        except AccountRequired as e:                        # a sign-in page: nothing typed, the browser is closed below, the person applies by hand
            account_required(r, e.reason, e.url or url, ask, announce_only=True)
        except knockout.SkipJob as e:                       # the person chose [s]kip at a knock-out: nothing was filled and nothing is submitted
            print(f"\n  Skipped at a knock-out: {e}")
            r["skipped"] = str(e)
        except formfind.HandOff as e:                       # a bot check or a sign-in wall: nothing on it is touched; the person's own browser takes over
            if failures is not None and failures_mod.kind_of(str(e)) == failures_mod.VERIFICATION_ERROR:
                failures.record(job["id"], url, failures_mod.VERIFICATION_ERROR, str(e))
            hand_off(str(e))
            try:
                close()
            except PlaywrightError:
                pass
            closed = True
        except (BrowserClosed, PlaywrightError) as e:       # the window or the page went away: nothing was submitted by code
            if not isinstance(e, BrowserClosed) and not is_closed_error(e, page):
                print(f"\n(the page stopped answering: {str(e).splitlines()[0][:120]}). If you submitted the form, paste the confirmation below; "
                      "'x' = did not submit.")
            else:
                r["browser_closed"] = str(e).splitlines()[0][:200] if str(e) else "the browser was closed"
                r["paste_file"] = str(closed_browser(job, rep, applicant, book, plan, url, r.get("filled") or [], r.get("drafts")) or "")
        conf = "x" if (r.get("skipped") or r.get("browser_closed") or r.get("account_required")) else ask(
            "After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
    finally:
        if not closed:
            try:
                close()
            except PlaywrightError:                         # already closed by the person: nothing left to tear down
                pass
    r["confirmation"] = conf
    return r
