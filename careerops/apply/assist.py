"""Apply assistant: opens the form, fills the basics, uploads PDFs, highlights what you must answer,
and STOPS. It never clicks submit — you do, then paste the confirmation into the terminal.

Why stop: visa/salary/EEO answers must be yours, ATS terms generally forbid automated submission,
and a reviewed form is the whole point of the human-in-the-loop gate.

Visa questions are answered from the plan for the job's work location W (apply/visa.py): the three booleans of
applicant.yaml `visa.by_country[W]` (else `.default`) and nothing else; a job whose W is unknown is refused before a browser opens. Every
visa field is highlighted and printed as `CHECK VISA ANSWER [W=XX]: question -> answer`; for a remote role at home XX is the remote label
(`visa.remote.label`), never the home country.

Equal-opportunities questions (apply/eo.py) are filled ONLY when applicant.yaml `equal_opportunities` lists them (ethnicity first), with
the first form option that equals one of the `match_any` texts you wrote (case- and dash-insensitive); no match, or a question that is not
listed, stays empty. Every such field, filled or empty, is outlined blue and printed as `CHECK EO ANSWER [key]: question -> option`.
Nothing about equal opportunities is ever inferred from anything else.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..discovery import countries
from . import eo
from ..discovery.workcountry import Work, rules_from
from .guard import check_job_files
from .visa import VisaPlan, plan_for, require_plan, work_for

LABEL_JS = """el => {
  const bits = [];
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) bits.push(l.innerText); }
  const wrap = el.closest('label'); if (wrap) bits.push(wrap.innerText);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) lb.split(' ').forEach(i => { const n = document.getElementById(i); if (n) bits.push(n.innerText); });
  ['aria-label','placeholder','name','id'].forEach(a => { const v = el.getAttribute(a); if (v) bits.push(v); });
  if (!bits.length) { const p = el.closest('div,fieldset,li'); if (p) bits.push(p.innerText.slice(0,120)); }
  return bits.join(' | ');
}"""

# The group's question for a radio button, whose own label is only its option ("Yes").
QUESTION_JS = """el => {
  const g = el.closest('fieldset, [role=radiogroup], [role=group]');
  if (g) {
    const l = g.querySelector('legend'); if (l) return l.innerText;
    const lb = g.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb.split(' ')[0]); if (n) return n.innerText; }
    return g.getAttribute('aria-label') || g.innerText.slice(0, 200);
  }
  const p = el.closest('div,li,p'); return p ? p.innerText.slice(0, 200) : '';
}"""

# A field is a visa question when its label says so. These are answered from applicant.yaml -> visa.by_country, never from never_fill.
VISA_FIELD = re.compile(r"right to work|visa|sponsor|immigration|work permit|work authori[sz]ation|share code", re.I)
VISA_COLOUR = "3px solid magenta"
EO_COLOUR = "3px solid blue"
APPLY_BUTTON = re.compile(r"^\s*(apply( for this job| now)?|i'?m interested)\s*$", re.I)


def _matches(label: str, patterns: list[str]) -> bool:
    return any(re.search(p, label, re.I) for p in patterns)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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


def fill_page(page, applicant: dict, cv_pdf: str | None, letter_pdf: str | None,
              letter_text: str | None = None, job=None, plan: VisaPlan | None = None) -> dict:
    """Fill what we safely can on the current page. Returns a report of filled / manual / uploaded fields.

    With `job`, the wrong-file guard runs first: nothing is uploaded unless the CV and letter belong to that job.
    A cover-letter file field gets the letter PDF; a cover-letter text box gets `letter_text` (when still empty).
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
    listed_eo = eo.entries(applicant)
    never, lmap = applicant.get("never_fill", []), applicant.get("label_map", {})

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
                el.set_input_files(letter_pdf); report["uploaded"].append(f"{short} ← cover letter")
            elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and cv_pdf:
                el.set_input_files(cv_pdf); report["uploaded"].append(f"{short} ← CV")
            else:
                report["manual"].append(f"file: {short}")
            continue

        if letter_text and typ == "textarea" and re.search(r"cover\s*letter", label, re.I):
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

        if _matches(label, never):
            el.evaluate("e => e.style.outline = '3px solid orange'")
            report["manual"].append(short)
            continue

        key = next((k for k, pats in lmap.items() if _matches(label, pats)), None)
        if key and applicant.get(key) and typ in ("text", "email", "tel", "url", "textarea", "search", ""):
            if not (el.input_value() or "").strip():
                el.fill(str(applicant[key]))
                report["filled"].append(f"{short} = {key}")
            continue

        required = el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"
        empty = typ in ("checkbox", "radio") and not el.is_checked() or \
            typ not in ("checkbox", "radio") and not (el.input_value() or "").strip()
        if required and empty:
            el.evaluate("e => e.style.outline = '3px solid red'")
            report["required_empty"].append(short)
    return report


def assist(job, rep: dict, applicant: dict, profile_dir: Path) -> dict:
    from playwright.sync_api import sync_playwright

    url = job["apply_url"] or job["url"]
    cv = rep.get("cv_pdf")
    letter = rep.get("letter_pdf") if rep.get("letter_pdf") and Path(rep["letter_pdf"]).is_file() else None
    plan = require_plan(job, applicant)                    # W unknown / no answers for it: WorkCountryError, nothing is opened
    check_job_files(job, cv, letter)                       # refuse before a browser even opens
    print(plan.work.line() + f"\n{plan.describe()}")
    letter_text = "\n\n".join((rep.get("letter") or {}).get("paragraphs") or []) or None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if not page.query_selector("input[type=file], input[type=email]"):
            for a in page.query_selector_all("a, button"):
                if APPLY_BUTTON.match(a.inner_text() or ""):
                    a.click()
                    page.wait_for_timeout(2500)
                    break
        r = fill_page(page, applicant, cv, letter, letter_text=letter_text, job=job, plan=plan)
        print(f"\nFilled: {r['filled']}\nUploaded: {r['uploaded']}")
        print(f"YOU ANSWER (orange; visa fields magenta): {r['manual']}\nREQUIRED + EMPTY (red): {r['required_empty']}")
        print("\nReview everything in the browser and press Submit yourself.")
        conf = input("After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
        ctx.close()
    r["confirmation"] = conf
    return r
