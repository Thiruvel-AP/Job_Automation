"""Apply assistant: opens the form, fills the basics, uploads PDFs, highlights what you must answer,
and STOPS. It never clicks submit — you do, then paste the confirmation into the terminal.

Why stop: visa/salary/EEO answers must be yours, ATS terms generally forbid automated submission,
and a reviewed form is the whole point of the human-in-the-loop gate.
"""
from __future__ import annotations

import re
from pathlib import Path

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

APPLY_BUTTON = re.compile(r"^\s*(apply( for this job| now)?|i'?m interested)\s*$", re.I)


def _matches(label: str, patterns: list[str]) -> bool:
    return any(re.search(p, label, re.I) for p in patterns)


def fill_page(page, applicant: dict, cv_pdf: str | None, letter_pdf: str | None) -> dict:
    """Fill what we safely can on the current page. Returns a report of filled / manual / uploaded fields."""
    report = {"filled": [], "manual": [], "uploaded": [], "required_empty": []}
    never, lmap = applicant.get("never_fill", []), applicant.get("label_map", {})

    for el in page.query_selector_all("input, textarea, select"):
        tag = el.evaluate("e => e.tagName").lower()
        typ = (el.get_attribute("type") or ("text" if tag == "input" else tag)).lower()
        if typ in ("hidden", "submit", "button", "image", "reset") or not el.is_visible() and typ != "file":
            continue
        label = (el.evaluate(LABEL_JS) or "").strip()
        short = label.split("|")[0].strip()[:60] or typ

        if typ == "file":
            if re.search(r"cover", label, re.I) and letter_pdf:
                el.set_input_files(letter_pdf); report["uploaded"].append(f"{short} ← cover letter")
            elif re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I) and cv_pdf:
                el.set_input_files(cv_pdf); report["uploaded"].append(f"{short} ← CV")
            else:
                report["manual"].append(f"file: {short}")
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
        r = fill_page(page, applicant, rep.get("cv_pdf"), rep.get("letter_pdf"))
        print(f"\nFilled: {r['filled']}\nUploaded: {r['uploaded']}")
        print(f"YOU ANSWER (orange): {r['manual']}\nREQUIRED + EMPTY (red): {r['required_empty']}")
        print("\nReview everything in the browser and press Submit yourself.")
        conf = input("After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): ").strip()
        ctx.close()
    r["confirmation"] = conf
    return r
