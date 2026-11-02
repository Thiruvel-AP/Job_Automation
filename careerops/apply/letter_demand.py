"""A cover letter when the form asks for one (letter.mode `if_field`, v2.4.1).

RightShip's Greenhouse form has a Cover Letter block (Attach / Dropbox / Google Drive / Enter manually) and the run said "no letter": the letter
had been rejected at tailoring time and nothing tried again when the form turned out to want one. Now, once the form is open and before anything is
uploaded:

  find      the form's cover-letter control: a file input (its id / name / label says cover letter), an "Enter manually" button of the block headed
            "Cover Letter", or a cover-letter text area; and whether the block is required;
  provide   a letter that already exists is used as it is. Without one, `maker` writes it NOW under the same v2.3 rules and guards as at tailoring
            time (review.workflow.regenerate_letter; cli.py builds the maker). If it still fails its guards the exact reason is printed (never a bare
            "no letter"): an optional field stays empty, a required one is handed off.
  deliver   a file input gets the letter PDF through the input itself (apply/assist.py and autoapply.py upload it); a form with only "Enter
            manually" gets that button clicked and `letter.txt` pasted into the text area that appears. The Dropbox and Google Drive buttons (and any
            control that names the geolocation or could submit) are never clicked: `shortfields.clickable_text` refuses them before a click.

Nothing here writes to answers.yaml, fact_bank.yaml or applicant.yaml, and nothing submits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import shortfields

FIND_JS = """() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const cover = t => /cover[\\s_-]*letter/i.test(t || '');
  const out = {file: false, manual: false, textarea: false, required: false};
  const own = el => [...(el.labels || [])].map(l => l.innerText).join(' ') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + el.id + ' ' + (el.getAttribute('name') || '');
  for (const el of document.querySelectorAll('input[type=file]')) {
    if (cover(own(el))) { out.file = true; out.required = out.required || el.required || el.getAttribute('aria-required') === 'true'; }
  }
  for (const el of document.querySelectorAll('textarea')) {
    if (cover(own(el))) { out.textarea = true; out.required = out.required || el.required || el.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(clean(own(el))); }
  }
  for (const b of document.querySelectorAll('button')) {
    if (!/^enter manually$/i.test(clean(b.innerText))) continue;
    for (let n = b.parentElement, i = 0; n && i < 6; n = n.parentElement, i++) {
      const t = clean(n.innerText);
      if (cover(t) && !/\\b(resume|cv)\\b/i.test(t.slice(0, 40))) { out.manual = true; out.required = out.required || /cover\\s*letter\\s*\\*/i.test(t.replace(/\\u2731/g, '*')); break; }
    }
  }
  return out;
}"""

# Click the "Enter manually" button of the Cover Letter block (and nothing else); returns whether one was found. The caller has already checked the
# text with shortfields.clickable_text.
ENTER_MANUALLY_JS = """() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  for (const b of document.querySelectorAll('button')) {
    if (!/^enter manually$/i.test(clean(b.innerText))) continue;
    for (let n = b.parentElement, i = 0; n && i < 6; n = n.parentElement, i++) {
      const t = clean(n.innerText);
      if (/cover[\\s_-]*letter/i.test(t) && !/\\b(resume|cv)\\b/i.test(t.slice(0, 40))) { b.click(); return true; }
    }
  }
  return false;
}"""
MANUAL_TEXTAREA_JS = """() => {
  const cover = t => /cover[\\s_-]*letter/i.test(t || '');
  const own = el => [...(el.labels || [])].map(l => l.innerText).join(' ') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + el.id + ' ' + (el.getAttribute('name') || '');
  const all = [...document.querySelectorAll('textarea')].filter(t => t.offsetParent !== null);
  return all.findIndex(t => cover(own(t)));
}"""


@dataclass(frozen=True)
class Control:
    file_input: bool = False
    manual_button: bool = False
    textarea: bool = False
    required: bool = False

    @property
    def any(self) -> bool:
        return self.file_input or self.manual_button or self.textarea


@dataclass
class Made:
    """What a letter maker returns: the PDF and the text of the letter it wrote, or `reason` (the exact guard reason) when it could not."""
    pdf: str | None = None
    text: str | None = None
    reason: str = ""
    rep: dict | None = None


@dataclass
class Provided:
    pdf: str | None
    text: str | None
    handoff: str = ""                      # set when the form REQUIRES a letter and none could be written: the reason
    made: bool = False                     # the letter was written now, at fill time
    note: str = ""


def find(page) -> Control:
    """The cover-letter control of the open form; an empty Control when the page cannot be asked."""
    try:
        d = page.evaluate(FIND_JS) or {}
    except Exception:  # noqa: BLE001  (a fake page, or one that is navigating)
        return Control()
    return Control(bool(d.get("file")), bool(d.get("manual")), bool(d.get("textarea")), bool(d.get("required")))


def enter_manually(page) -> bool:
    """Click "Enter manually" of the Cover Letter block so that its text area appears. Refused (False) when the control's text is one the code never
    clicks; the Dropbox and Google Drive buttons are not looked for, let alone clicked."""
    if not shortfields.clickable_text("Enter manually"):
        return False
    try:
        return bool(page.evaluate(ENTER_MANUALLY_JS))
    except Exception:  # noqa: BLE001
        return False


def paste_manual(page, text: str) -> bool:
    """Put `text` into the cover-letter text area (after enter_manually); True when it holds the text."""
    try:
        areas = [t for t in page.query_selector_all("textarea") if t.is_visible()]
        idx = page.evaluate(MANUAL_TEXTAREA_JS)
        el = areas[idx] if isinstance(idx, int) and 0 <= idx < len(areas) else (areas[-1] if areas else None)
        if el is None:
            return False
        if not (el.input_value() or "").strip():
            el.fill(text)
        return " ".join((el.input_value() or "").split()) == " ".join(text.split())
    except Exception:  # noqa: BLE001
        return False


def letter_text_of(rep: Mapping[str, Any], pdf: str | None) -> str | None:
    """letter.txt beside the letter PDF (the same words as the PDF), else the report's paragraphs joined."""
    if pdf:
        txt = Path(pdf).with_name("letter.txt")
        if txt.is_file():
            body = txt.read_text(encoding="utf-8").strip()
            if body:
                return body
    paras = (rep.get("letter") or {}).get("paragraphs") or []
    return "\n\n".join(paras) or None


def provide(page, rep: Mapping[str, Any], pdf: str | None, text: str | None, maker: Callable[[Control], Made] | None,
            out: Callable[[str], None] = print, reason: str = "") -> Provided:
    """The letter for this form. `pdf` / `text` are the job's letter when it has one; `maker(control)` writes one now (None = nothing can); `reason` is
    why the job has none (tailor.letter_reason of its report), printed when no letter can be had."""
    ctl = find(page)
    if not ctl.any:
        return Provided(pdf, text)
    made = False
    if not pdf:
        res = maker(ctl) if maker is not None else None
        if res is not None and res.pdf:
            pdf, text, made = res.pdf, res.text or letter_text_of(res.rep or rep, res.pdf), True
            if res.rep is not None:
                rep = res.rep
            out(f"  cover letter: written now because the form asks for one ({Path(pdf).name})")
        else:
            why = (res.reason if res is not None and res.reason else reason) or "the job has no letter and no letter can be written here"
            out(f"  cover letter: NONE. The form asks for one ({'required' if ctl.required else 'optional'}). Reason: {why}")
            if ctl.required:
                return Provided(None, None, handoff=f"the form requires a cover letter and none could be written: {why}", note=why)
            out("  the cover letter is optional: left empty")
            return Provided(None, None, note=why)
    if not ctl.file_input and ctl.manual_button and text:           # only "Enter manually": click it and paste letter.txt
        if enter_manually(page):
            page.wait_for_timeout(300)
            ok = paste_manual(page, text)
            out("  cover letter: 'Enter manually' clicked and letter.txt pasted" if ok else "  cover letter: could not paste into the text area; paste letter.txt yourself")
    return Provided(pdf, text, made=made)
