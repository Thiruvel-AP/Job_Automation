"""The form-question loop: scan EVERY field of an application form, answer the free-text ones, and say what happened to each.

Evidence (the 23 Oct ElevenLabs Ashby form): three OPTIONAL text areas ("Describe an agent or LLM-powered system you have shipped...", "Tell us about
an automation or agent you took from prototype to production...", "Describe a project you built with non-engineering stakeholders...") were never
looked at: only required free-text questions reached apply/freetext.py, and the assist printed `YOU ANSWER: []`. This module is what the assist
(apply/assist.py) and the gated auto-apply (autoapply.py) now run around their field pass:

  begin(page)   scan every visible input, textarea, select, radio group and check-box group, required or not, with its label (the label element,
                aria-label, or the nearest text before it), type, maxlength and options, and PRINT THE TABLE before anything is filled;
  ... the caller's pass fills what applicant.yaml, answers.yaml, the visa plan and the equal-opportunities entries answer ...
  run(page)     for each free-text field still empty, observe -> classify -> retrieve -> draft -> check -> fill -> verify:
                  retrieve   only from answers.yaml, the answer library (an approved answer for a question at least 0.85 similar), the fact bank and the
                             job ad (apply/freetext.py: nothing else is a source);
                  draft/check  the existing guards (numbers, tools, organisations and names must be in the chosen entries; a thing the question asks
                             for that the bank never mentions is not claimed: the draft is marked WEAK EVIDENCE: <asked> vs <entry ids>);
                  fill/verify  the text goes into the field AT ONCE and is read back; at most MAX_PASSES (3) passes per field;
                after every fill the form is scanned again (an answer can reveal a new question; the caller's routine fills what is new);
                an OPTIONAL field of class "other" ("anything else?", hobbies) is skipped, a REQUIRED one is asked of the person;
  approve       each NEW DRAFT is shown with `[y] use · [e] edit · [r] redraft · [s] I'll write it` (apply/freetext.py). `y` or an edit stores it in the
                answer library; an answer reused from the library fills directly and asks nothing. In the gated auto-apply a draft nobody approved
                stops the run (NeedsUser) BEFORE any submit; in the assist it stays in the field marked NEW DRAFT for the person who submits;
  report        the table again, with the status of every field (FILLED, LIBRARY, NEW DRAFT, WEAK EVIDENCE, BLANK ...).

Every step of every field is appended to formloop.jsonl in the job's output folder. Nothing here clicks, submits or writes to answers.yaml,
fact_bank.yaml or applicant.yaml.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import filler
from . import freetext as ft
from .filler import LABEL_JS, QUESTION_JS, NeedsUser, _norm

FILE = "formloop.jsonl"
MAX_PASSES = 3                  # draft/check/fill/verify passes for one field
MAX_FIELDS = 60                 # fields worked on in one form (a form that keeps growing is not followed for ever)
SKIP_TYPES = ("hidden", "submit", "button", "image", "reset")
STAR = re.compile(r"\s*(?:\*|✱|\(\s*required\s*\)|\(\s*optional\s*\))\s*", re.I)

# The text that names a field: its <label>, aria-labelledby, aria-label; failing those, the nearest text before it (a sibling of the field or of
# the block around it, stopping at another field); failing that, placeholder / name / id.
LABEL_SCAN_JS = r"""el => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const own = [];
  if (el.labels) [...el.labels].forEach(l => own.push(l.innerText));
  const lb = el.getAttribute('aria-labelledby');
  if (lb) lb.split(' ').forEach(i => { const n = document.getElementById(i); if (n) own.push(n.innerText); });
  own.push(el.getAttribute('aria-label') || '');
  const named = own.map(clean).filter(Boolean)[0];
  if (named) return named;
  walk: for (let n = el, i = 0; n && i < 6; n = n.parentElement, i++) {
    for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
      if (s.matches('input, textarea, select') || s.querySelector('input, textarea, select')) break walk;      // another field: the text before it is not ours
      const t = clean(s.innerText);
      if (t) return t.slice(0, 300);
    }
  }
  return clean(el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '');
}"""
# One call for the whole form: for every input / textarea / select the facts `scan` needs, in document order (the same order as query_selector_all).
SCAN_JS = "() => { const labelOf = " + LABEL_SCAN_JS + "; const shortOf = " + LABEL_JS + "; const questionOf = " + QUESTION_JS + r""";
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('input, textarea, select')].map(el => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    const d = {tag, type, name: el.getAttribute('name') || '', id: el.id || '', maxlength: el.getAttribute('maxlength') || '',
               role: el.getAttribute('role') || '', required: el.hasAttribute('required'), ariaRequired: el.getAttribute('aria-required') === 'true',
               visible: el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden'};
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return d;
    d.label = labelOf(el);
    d.short = clean(shortOf(el).split('|')[0]).slice(0, 60);
    if (type === 'radio' || type === 'checkbox') { d.question = clean(questionOf(el)); d.checked = el.checked; d.optionLabel = clean(shortOf(el).split('|')[0]); }
    else if (type === 'file') d.value = [...(el.files || [])].map(f => f.name).join(', ');
    else if (tag === 'select') {
      d.options = [...el.options].map(o => clean(o.textContent)).filter(Boolean);
      d.value = el.selectedOptions.length ? clean(el.selectedOptions[0].textContent) : '';
    } else d.value = el.value || '';
    return d;
  });
}"""


# ------------------------------------------------------------------ the scan
@dataclass
class Field:
    key: str
    kind: str                       # text | textarea | select | radio | checkbox | combobox | file
    label: str                      # as the form shows it
    question: str                   # the label without its star / "(optional)"
    required: bool
    maxlength: str
    options: list[str]
    value: str
    el: Any
    short: str = ""                 # the label the caller's own report uses (filler.LABEL_JS, first 60 characters)

    @property
    def free_text(self) -> bool:
        return self.kind == "textarea" or (self.kind == "text" and ft.is_free_text("text", self.maxlength))


def _q(label: str) -> str:
    """A label as a question: without its star, "(required)" or "(optional)"."""
    return _norm(STAR.sub(" ", label))


def _required(d: dict, label: str) -> bool:
    return bool(d.get("required") or d.get("ariaRequired") or label.rstrip().endswith("*"))


def scan(page) -> list[Field]:
    """Every visible field of the page, required or not (a file input counts even when the form hides it behind a button). A radio or check-box
    group with one name is one field. A page that cannot be asked gives an empty list."""
    handles, data = [], []
    for _ in range(2):                                    # the form may change between the two reads: try twice
        try:
            handles = page.query_selector_all("input, textarea, select")
            data = page.evaluate(SCAN_JS) or []
        except Exception:  # noqa: BLE001  (the page is navigating, or not a browser page)
            return []
        if len(handles) == len(data):
            break
    else:
        return []
    members: dict[str, list[int]] = {}
    for n, d in enumerate(data):
        if d["type"] in ("radio", "checkbox") and d["name"]:
            members.setdefault(f"{d['type']}:{d['name']}", []).append(n)
    out: list[Field] = []
    used: dict[str, int] = {}
    done: set[str] = set()
    for n, d in enumerate(data):
        typ = d["type"]
        if typ in SKIP_TYPES or (not d["visible"] and typ != "file"):
            continue
        label = _norm(d.get("label") or "")
        if typ in ("radio", "checkbox"):
            gkey = f"{typ}:{d['name']}"
            if d["name"] and gkey in done:
                continue
            done.add(gkey)
            group = members.get(gkey, [n]) if d["name"] else [n]
            question = _norm(d.get("question") or "") if (typ == "radio" or len(group) > 1) else label
            chosen = [data[g]["optionLabel"] for g in group if data[g].get("checked")]
            f = Field("", typ, question, _q(question), any(_required(data[g], question) for g in group), "",
                      [data[g].get("optionLabel", "") for g in group], ", ".join(chosen), handles[n], d.get("short", ""))
        elif typ == "file":
            f = Field("", "file", label, _q(label), _required(d, label), "", [], d.get("value", ""), handles[n], d.get("short", ""))
        elif d["tag"] == "select":
            f = Field("", "select", label, _q(label), _required(d, label), "", list(d.get("options") or []), d.get("value", ""), handles[n], d.get("short", ""))
        else:
            kind = "textarea" if d["tag"] == "textarea" else "combobox" if d["role"] == "combobox" else typ if typ not in ("text", "") else "text"
            f = Field("", kind, label, _q(label), _required(d, label), d["maxlength"], [], d.get("value", ""), handles[n], d.get("short", ""))
        stem = f"{f.kind}:{d['name'] or d['id'] or f.question[:50]}"
        used[stem] = used.get(stem, 0) + 1
        f.key = stem if used[stem] == 1 else f"{stem}#{used[stem]}"
        out.append(f)
    return out


def table(fields: list[Field], status: dict[str, str] | None = None) -> list[str]:
    """The lines of the field table: number, type, required, maxlength and the whole label; under it the options (before filling) or the status of the
    field (after). A long question is printed whole: it is what you need to read."""
    lines = [f"  {'#':>2}  {'type':<9} {'req':<3} {'max':<5} label"]
    for i, f in enumerate(fields, 1):
        lines.append(f"  {i:>2}  {f.kind:<9} {'yes' if f.required else 'no':<3} {f.maxlength or '-':<5} {f.question}")
        if status is not None:
            lines.append(f"{'':>25}-> {status.get(f.key, '')}")
        elif f.options:
            lines.append(f"{'':>25}options: " + " | ".join(f.options[:6]) + (" ..." if len(f.options) > 6 else ""))
    return lines


def output_folder(job: dict, cv_pdf: str | None) -> Path | None:
    """Where formloop.jsonl goes: the job's output folder (`out_dir`, else the folder of its CV)."""
    if job.get("out_dir"):
        return Path(str(job["out_dir"]))
    return Path(cv_pdf).parent if cv_pdf else None


@dataclass
class Draft:
    key: str
    question: str
    label: str
    proposal: ft.Proposal
    text: str
    status: str = "NEW DRAFT"       # NEW DRAFT | approved | edited | written | NEW DRAFT (unapproved)
    short: str = ""


# ------------------------------------------------------------------ the loop
class FormLoop:
    """One form's scan and free-text loop. `ctx` is the filler's Ctx (applicant, answers.yaml, the FreeText of this job, `strict`, `out`, `result`);
    `handle_new(element)` is the caller's own routine for one element, used for a field that appears after an answer."""

    def __init__(self, ctx: filler.Ctx, folder: Path | None = None, handle_new: Callable[[Any], None] | None = None,
                 now: Callable[[], datetime] = datetime.now):
        self.ctx, self.folder, self.handle_new, self.now = ctx, folder, handle_new, now
        self.first: dict[str, str] = {}                 # key -> value when the form was first scanned
        self.status: dict[str, str] = {}                # key -> LIBRARY | NEW DRAFT | APPROVED | ...
        self.skipped: dict[str, str] = {}               # key -> why a field was left empty
        self.drafts: list[Draft] = []
        self.added: list[str] = []                      # what this loop put in `result.filled`
        self.filled_shorts: set[str] = set()            # `Field.short` of every field the loop filled (the assist's required-empty list drops them)
        self._page: Any = None

    # ---- the log
    def log(self, f: Field | None, step: str, **info: Any) -> None:
        if self.folder is None:
            return
        row = {"time": self.now().isoformat(timespec="seconds"), "field": f.question[:160] if f else "", "key": f.key if f else "", "step": step, **info}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / FILE, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass                                          # the log is a record, never a reason to stop a form

    def log_note(self, question: str, step: str, **info: Any) -> None:
        """A row of formloop.jsonl for something the caller did to a field that is not a free-text one (a city picked, the how-heard option chosen)."""
        if self.folder is None:
            return
        row = {"time": self.now().isoformat(timespec="seconds"), "field": question[:160], "key": "", "step": step, **info}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / FILE, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _note(self, text: str) -> None:
        self.ctx.result.filled.append(text)
        self.added.append(text)

    # ---- before filling
    def begin(self, page) -> list[Field]:
        """Scan the form and print the table of ALL its fields. Called before anything is filled."""
        self._page = page
        fields = scan(page)
        self.first = {f.key: f.value for f in fields}
        self.ctx.out(f"\n  FORM FIELDS ({len(fields)}): every visible input, textarea, select, radio and check-box group, required or not")
        for line in table(fields):
            self.ctx.out(line)
        for f in fields:
            self.log(f, "observe", type=f.kind, required=f.required, maxlength=f.maxlength, options=f.options[:20], value=f.value[:80], phase="scan")
        return fields

    # ---- after the caller's pass
    def run(self, page) -> None:
        self._page = page
        done: set[str] = set()
        seen = set(self.first)
        for _ in range(MAX_FIELDS):
            fields = scan(page)
            fresh = [f for f in fields if f.key not in seen]
            for f in fresh:                                # answers can reveal new questions
                seen.add(f.key)
                self.log(f, "observe", type=f.kind, required=f.required, maxlength=f.maxlength, options=f.options[:20], phase="appeared after a fill")
                self.ctx.out(f"  form loop: a new field appeared: {f.question[:70]}")
                if self.handle_new is not None:
                    self.handle_new(f.el)                  # applicant.yaml, answers.yaml, visa, equal opportunities: the caller's own routine
            if fresh:
                fields = scan(page)
            todo = next((f for f in fields if f.free_text and not f.value.strip() and f.key not in done), None)
            if todo is None:
                break
            done.add(todo.key)
            self.work(todo)
        else:
            self.ctx.out(f"  form loop: stopped after {MAX_FIELDS} fields")
        self.approve()
        self.report()

    # ---- one field
    def _put(self, f: Field, text: str) -> bool:
        """Fill the field and read it back; True when it holds the text."""
        try:
            f.el.fill(text)
            ok = _norm(f.el.input_value() or "") == _norm(text)
        except Exception as e:  # noqa: BLE001  (the field is gone or refuses the value)
            self.log(f, "fill", ok=False, error=str(e)[:200])
            return False
        if ok:
            self.ctx.track(f.el, f.short or f.question[:60], "text", text)
            self.filled_shorts.add(f.short)
            try:
                f.el.evaluate("e => { if (e.style.outline) e.style.outline = ''; }")
            except Exception:  # noqa: BLE001
                pass
        return ok

    def work(self, f: Field) -> None:
        ctx, fs = self.ctx, self.ctx.freetext
        q = f.question
        cls = ft.classify(q, str(ctx.job.get("company") or ""))
        self.log(f, "classify", cls=cls, required=f.required, limit=ft.read_limits(f"{q} {f.label}", f.maxlength).as_dict())
        r = ctx.book.resolve(q, ctx.job)
        if r is not None and r.known:                       # a short, stable answer of answers.yaml
            self.log(f, "retrieve", source="answers.yaml", entry=r.entry_id)
            ok = self._put(f, r.text)
            self.log(f, "fill", ok=ok)
            self.log(f, "verify", ok=ok)
            if ok:
                self.status[f.key] = "FILLED (answers.yaml)"
                self._note(f"{q[:80]} -> {r.text[:60]}")
                return
        if f.required and not filler.readable_question(q):
            ctx.out(f"  free text: the question of this required box cannot be read (its label says {q[:60]!r}): no draft is offered, it is yours")
            self.skipped[f.key] = "required: the question cannot be read: yours"
            self.log(f, "skip", why="required, the question cannot be read")
            ctx.stop(f"cannot read the question of a required text box ({f.short or q[:60]}): it is yours to answer")
            return
        if not f.required and (cls == "other" or fs is None):
            why = "optional, class other: skipped" if cls == "other" else "optional, no drafting available: skipped"
            self.skipped[f.key] = why
            self.log(f, "skip", why=why)
            ctx.result.blank.append(q)
            return
        if fs is None:                                      # a required box and nothing that can draft: answers.yaml, else the person (as before)
            value = filler.decide(ctx, q, f.required)
            ok = value is not None and self._put(f, value)
            self.log(f, "fill", ok=bool(ok), source="answers.yaml or the person")
            if ok:
                self.status[f.key] = "FILLED"
                self._note(f"{q[:80]} -> {value[:60]}")
            return

        prior: list[str] = []
        p: ft.Proposal | None = None
        for n in range(1, MAX_PASSES + 1):
            p = fs.propose(q, f.label, f.maxlength, problems=prior, tries=1)
            self.log(f, "retrieve", **{"pass": n, "origin": p.origin or "none", "class": p.cls, "sources": p.plan.ids, "plan": p.plan.why,
                                       "library": p.reused.question if p.reused else ""})
            if p.origin == "draft" or p.problems:
                self.log(f, "draft", **{"pass": n, "text": p.text})
                self.log(f, "check", **{"pass": n, "ok": bool(p.text), "problems": p.problems, "weak": p.weak})
            if p.text:
                ok = self._put(f, p.text)
                self.log(f, "fill", **{"pass": n, "ok": ok, "chars": len(p.text)})
                self.log(f, "verify", **{"pass": n, "ok": ok})
                if ok:
                    self._filled(f, p)
                    return
                prior = [f"the form did not keep the text (it holds {f.maxlength} characters at most)" if f.maxlength
                         else "the form did not keep the text that was filled in"]
                continue
            if not p.problems or any(x.startswith("the model could not draft it") for x in p.problems):
                break                                       # nothing was drafted, so another pass cannot change it
            prior = p.problems
        self._give_up(f, p)

    def _filled(self, f: Field, p: ft.Proposal) -> None:
        ctx, fs = self.ctx, self.ctx.freetext
        if p.origin == "reused":
            fs.last = "reused"
            self.status[f.key] = "LIBRARY"
            ctx.out(f"  free text: reused the answer you approved on {p.reused.date} for \"{p.reused.question[:70]}\" ({p.cls})")
            self._note(f"{f.question[:80]} -> free text (reused)")
            return
        self.drafts.append(Draft(f.key, f.question, f.label, p, p.text, short=f.short))
        self.status[f.key] = "NEW DRAFT" + (f"; {p.weak}" if p.weak else "")
        ctx.out(f"  form loop: NEW DRAFT in the field: {f.question[:70]}" + (f"\n    {p.weak}" if p.weak else ""))
        self._note(f"{f.question[:80]} -> free text (NEW DRAFT)")

    def _give_up(self, f: Field, p: ft.Proposal) -> None:
        ctx, fs = self.ctx, self.ctx.freetext
        q = f.question
        if p is not None and p.handoff and f.required:                 # no JD to draft a motivation answer from: nothing is drafted, the job is handed off
            ctx.needs_user[q] = p.handoff
            self.skipped[f.key] = "required: " + p.handoff
            self.log(f, "give_up", required=True, why=p.handoff)
            from .formfind import HandOff

            raise HandOff(p.handoff)
        if f.required:                                      # the person writes it (strict) or is told (assist), as before
            text = fs._write(q, p.cls, p.plan, p.limit, ctx.strict, p.why_not)
            if text and self._put(f, text):
                self.status[f.key] = "WRITTEN"
                self._note(f"{q[:80]} -> free text (written)")
                return
            self.skipped[f.key] = "required: no answer: yours"
            self.log(f, "give_up", required=True, why=p.why_not)
            ctx.stop(f"no answer for the required question: {q}")
            return
        why = p.why_not or "no draft"
        ctx.out(f"  form loop: optional question left empty ({q[:60]!r}): {why}")
        self.skipped[f.key] = "optional: no draft: left empty"
        self.log(f, "give_up", required=False, why=why)
        ctx.result.blank.append(q)

    # ---- the person's y / e
    def _find(self, d: Draft) -> Any:
        for f in scan(self._page):
            if f.key == d.key:
                return f.el
        return None

    def _set(self, d: Draft, text: str) -> None:
        el = self._find(d)
        if el is None:
            return
        try:
            el.fill(text)
            self.ctx.track(el, d.short or d.question[:60], "text", text)
        except Exception:  # noqa: BLE001
            pass

    def approve(self) -> None:
        """Ask about each NEW DRAFT. Strict (the gated auto-apply): a draft nobody approved stops the run before any submit."""
        ctx, fs = self.ctx, self.ctx.freetext
        pending = [d for d in self.drafts if d.status == "NEW DRAFT"]
        for d in pending:
            text = fs.review(d.proposal, ctx.strict, on_text=lambda t, d=d: self._set(d, t))
            self.log(None, "approve", question=d.question[:160], result=fs.last if text else "not approved")
            if not text:
                d.status = "NEW DRAFT (unapproved)"
                self.status[d.key] = "NEW DRAFT (unapproved)" + (f"; {d.proposal.weak}" if d.proposal.weak else "")
                continue
            if text != d.text:
                self._set(d, text)
            d.text, d.status = text, fs.last
            self.status[d.key] = fs.last.upper()
            old = f"{d.question[:80]} -> free text (NEW DRAFT)"
            if old in ctx.result.filled:
                ctx.result.filled[ctx.result.filled.index(old)] = f"{d.question[:80]} -> free text ({fs.last})"
                self.added[self.added.index(old)] = f"{d.question[:80]} -> free text ({fs.last})"
        left = [d for d in self.drafts if d.status == "NEW DRAFT (unapproved)"]
        if left and ctx.strict:
            raise NeedsUser("new draft not approved (nothing is submitted): " + "; ".join(d.question[:70] for d in left))
        for d in left:
            ctx.result.blank.append(f"{d.question} (NEW DRAFT, not approved: check it in the browser)")
            el = self._find(d)
            if el is not None:
                try:
                    el.evaluate("e => e.style.outline = '3px solid orange'")
                except Exception:  # noqa: BLE001
                    pass

    # ---- after everything
    def report(self) -> None:
        fields = scan(self._page)
        status: dict[str, str] = {}
        for f in fields:
            if f.key in self.status:
                status[f.key] = self.status[f.key]
            elif f.kind == "file":
                status[f.key] = f"FILE: {f.value}" if f.value else ("BLANK (required)" if f.required else "BLANK")
            elif f.value.strip():
                status[f.key] = "PRE-FILLED" if self.first.get(f.key, "").strip() else "FILLED"
            else:
                status[f.key] = self.skipped.get(f.key) or ("BLANK (required)" if f.required else "BLANK")
        self.ctx.out("\n  FORM FIELDS AFTER FILLING:")
        for line in table(fields, status):
            self.ctx.out(line)
        for f in fields:
            self.log(f, "status", status=status[f.key], value=f.value[:80])

    # ---- the hand-off
    def paste_drafts(self) -> list[dict]:
        """The free-text answers drafted for this form, for answers_to_paste.txt (apply/handoff.py)."""
        return [{"question": d.question, "answer": d.text, "status": "NEW DRAFT" if d.status.startswith("NEW") else "approved by you",
                 "weak": d.proposal.weak} for d in self.drafts]
