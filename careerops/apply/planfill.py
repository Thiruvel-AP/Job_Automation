"""Fill an application form from its answer plan (v2.5), field by field, reading each one back.

The plan (apply/answerplan.py) was made and shown in the terminal before the browser opened; here it is only carried out. The page's fields are the
source of truth (apply/formscan.py tags each with `data-co-field`): every planned row is matched to one of them by the schema `name` (== the DOM id),
then the name attribute, then the label. A field the plan did not know (the schema missed it) is planned now with the same rules, without drafting.

  files      first: the CV only to the upload whose label is a resume / CV label, the cover-letter PDF only to the one whose label is "Cover Letter"
             (`set_input_files`; Attach / Dropbox / Google Drive / Locate me are never clicked); then a pause until the form stops changing;
  text       `fill`; a single-line box never gets a line break;
  select     the option by its label;
  combobox   (react-select) the control is clicked, the text typed with `press_sequentially(delay=15)`, 300 ms later the open menu's options are read
             and the planned one is CLICKED (apply/option_match.py, or the city / country / how-heard rule over the live options). No match: Escape,
             NEEDS USER. Enter is never pressed anywhere in a form (it can submit it);
  check box  `set_checked`, else a click on its `label[for]`, else left for you; never an attestation or consent box (the plan never has one READY),
             never on Lever;
  radio      the option whose label is the planned one.

After every field its value is read back and one line printed: `[ 7/18] Location (City) … VERIFIED "Bristol, England, United Kingdom"`. The URL
path of the form's frame is compared before and after each field: a change stops the run with `PAGE NAVIGATED DURING FILL: check whether anything
was submitted`. A closed page, context or browser raises errors.BrowserClosed at once. The validation messages the page shows are printed at the end,
and the required-field table (apply/requiredgate.py) is built from the read-back. Nothing here clicks a submit control.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import answerplan as AP
from . import option_match, requiredgate, shortfields, verify
from .errors import BrowserClosed, raise_if_closed
from .formscan import OPTIONS_FOR_JS, DomField, path_of

MENU_SEL = ".select__menu .select__option, .select__menu-list [role=option], [class*=menu i] [role=option], [role=listbox] [role=option]"
ANY_OPTION = "[role=option]:visible"            # a list built by some other script: any visible option (the one open list)
TYPE_DELAY_MS = 15
MENU_WAIT_MS = 300
MENU_TRIES = 8                      # a place list that loads its options from a server: read again every 300 ms, up to ~2.4 s
VERIFIED, LOST, NOT_FOUND, NAVIGATED = "VERIFIED", "LOST", "NOT ON THE PAGE", "NAVIGATED"
COVERED = "COVERED"                 # a schema field not on the page whose answer a VERIFIED page field holds (9 Nov 2026): it does not block

ERRORS_JS = r"""() => {
  const out = [];
  for (const e of document.querySelectorAll('[aria-invalid="true"], [role="alert"], [class*="error" i]:not([class*="clear" i])')) {
    const t = (e.textContent || '').replace(/\s+/g, ' ').trim();
    if (t && t.length > 2 && t.length < 160 && e.offsetParent !== null) out.push(t);
  }
  return [...new Set(out)].slice(0, 8);
}"""
SELECTED_JS = "el => (el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].textContent : '').replace(/\\s+/g, ' ').trim()"
FILES_JS = r"""([el, name]) => {
  if (el.files && el.files.length) return [...el.files].map(f => f.name).join(', ');
  for (let c = el.parentElement, i = 0; c && i < 6; c = c.parentElement, i++) {      // a form that takes the file and shows its name instead
    const t = (c.innerText || '').replace(/\s+/g, ' ');
    if (name && t.includes(name)) return name;
  }
  return '';
}"""


class Navigated(RuntimeError):
    """The form's page went somewhere else while it was being filled."""


@dataclass
class Done:
    row: AP.Row
    dom: DomField | None
    status: str                     # VERIFIED | LOST | NEEDS USER | BLANK | NOT ON THE PAGE | COVERED | DRAFT (filled, not approved)
    value: str = ""
    why: str = ""


@dataclass
class Result:
    done: list[Done] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    navigated: bool = False

    def required_rows(self, required_only: bool = True) -> list[requiredgate.Row]:
        """The F4 table rows: VERIFIED / LOST / NEEDS USER / EMPTY for each (required) field."""
        rows = []
        for d in self.done:
            q = d.row.q
            if required_only and not q.required:
                continue
            status = {VERIFIED: "VERIFIED", LOST: "LOST", AP.NEEDS_USER: "NEEDS USER", NOT_FOUND: "EMPTY", AP.BLANK: "EMPTY",
                      NAVIGATED: "LOST"}.get(d.status, d.status)
            source = d.row.source + (" (DRAFT, not approved)" if d.row.status == AP.DRAFT else "")
            options = list(q.options or (d.dom.options if d.dom is not None else []) or []) if q.type in requiredgate.CHOICE_KINDS else []
            rows.append(requiredgate.Row(q.label, q.key, q.type, q.required, status, d.value, source, d.why or d.row.why, options))
        return rows

    def lines(self) -> list[str]:
        return requiredgate.render(self.required_rows())

    def as_dict(self) -> dict:
        return {"fields": [{**d.row.as_dict(), "fill": d.status, "value": d.value, "why": d.why} for d in self.done], "errors": self.errors,
                "navigated": self.navigated}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def covered_by(done: Sequence[Done]) -> dict[int, str]:
    """{plan row number: label of the VERIFIED page field that holds its answer} for each schema field that is not on the page (9 Nov 2026:
    RightShip's schema lists "Location", the page shows only "Location (City)", VERIFIED "Bristol, England, United Kingdom"). The page field
    covers it when its label starts with the schema label ("Location" -> "Location (City)"), or when both were answered from the same source and
    the page field holds that answer."""
    verified = [d for d in done if d.status == VERIFIED and d.dom is not None]
    out: dict[int, str] = {}
    for d in done:
        q = d.row.q
        if d.status != NOT_FOUND or q.origin != "schema" or q.type == "file":
            continue
        lab = _norm(q.label)
        for v in verified:
            vl = _norm(v.row.q.label)
            prefix = bool(lab) and vl != lab and re.match(re.escape(lab) + r"(?![\w])", vl) is not None
            same = (bool(d.row.source) and v.row.source == d.row.source and bool(_norm(d.row.answer))
                    and _norm(d.row.answer) in _norm(v.value or v.row.answer))
            if prefix or same:
                out[d.row.n] = v.row.q.label
                break
    return out


def match(rows: Sequence[AP.Row], fields: Sequence[DomField]) -> tuple[dict[int, DomField], list[DomField]]:
    """{row number: page field} and the page fields no row matched. By the field's data-co-field (a plan made from this very page), the schema
    name == the element's id / name, then the label."""
    free = list(fields)
    got: dict[int, DomField] = {}

    def take(row: AP.Row, test: Callable[[DomField], bool]) -> bool:
        hit = next((f for f in free if test(f)), None)
        if hit is None:
            return False
        got[row.n] = hit
        free[:] = [f for f in free if f is not hit]                 # taken: no other row matches it (a list filter, nothing is deleted anywhere)
        return True

    for row in rows:
        q = row.q
        if q.dom_id and take(row, lambda f: f.id == q.dom_id and _norm(f.label) == _norm(q.label)):
            continue
        if q.key and take(row, lambda f: q.key in (f.native_id, f.native_name)):
            continue
    for row in rows:
        if row.n in got:
            continue
        q = row.q
        take(row, lambda f: _norm(f.label) == _norm(q.label) and (f.type == q.type or {f.type, q.type} <= {"combobox", "select"}))
    return got, free


class Filler:
    def __init__(self, page, frame, *, job: Mapping[str, Any], cv: str | None, letter: str | None, out: Callable[[str], None] = print,
                 quiet_ms: int = verify.QUIET_MS):
        self.page, self.frame, self.job, self.cv, self.letter, self.out, self.quiet_ms = page, frame, job, cv, letter, out, quiet_ms
        self.start = path_of(frame, page)

    # ---- one page call, a closed page raising BrowserClosed
    def _do(self, fn: Callable[[], Any], default: Any = None, swallow: bool = True) -> Any:
        try:
            return fn()
        except BrowserClosed:
            raise
        except Exception as e:  # noqa: BLE001
            raise_if_closed(e, self.page)
            if not swallow:
                raise
            return default

    def loc(self, dom: DomField, option: str | None = None):
        sel = f'[data-co-field="{dom.id}"]' + (f'[data-co-option="{_css(option)}"]' if option is not None else "")
        return self.frame.locator(sel).first

    def _moved(self) -> None:
        now = path_of(self.frame, self.page)
        if now and self.start and now != self.start:
            raise Navigated(f"{self.start} -> {now}")

    # ---- the read-back
    def read(self, dom: DomField, expect: str = "") -> str:
        el = self.loc(dom)
        t = dom.type
        if t == "file":
            got = self._do(lambda: el.evaluate("(e, name) => (" + FILES_JS + ")([e, name])", expect), "") or ""
            if not got and expect:
                got = self._do(lambda: self.frame.evaluate("n => (document.body.innerText || '').includes(n) ? n : ''", expect), "") or ""
            return got
        if t == "checkbox":
            return "true" if self._do(lambda: el.is_checked(), False) else ""
        if t == "radio":
            for o in dom.options:
                if self._do(lambda: self.loc(dom, o).is_checked(), False):
                    return o
            return ""
        if t == "select":
            return self._do(lambda: el.evaluate(SELECTED_JS), "") or ""
        if t == "combobox":
            return self._do(lambda: el.evaluate(shortfields.COMBO_VALUE_JS), "") or ""
        return self._do(lambda: el.input_value(), "") or ""

    @staticmethod
    def holds(value: str, row: AP.Row, dom: DomField) -> bool:
        v = _norm(value)
        if not v:
            return False
        if dom.type in ("file", "checkbox"):
            return True
        want = _norm(row.answer)
        if dom.type in ("text", "email", "tel", "url", "number", "date", "textarea"):
            return v == _norm(_one_line(row.answer) if dom.type != "textarea" else row.answer)
        if v == want or (row.pick and _norm(row.typed) in v) or want in v:
            return True
        return dom.type == "combobox" and re.search(r"(?:^|\s)" + re.escape(v) + r"(?:$|\s)", want) is not None

    # ---- the fills
    def _combobox(self, row: AP.Row, dom: DomField) -> tuple[bool, str]:
        el = self.loc(dom)
        self._do(lambda: el.scroll_into_view_if_needed())
        self._do(lambda: el.click(), swallow=False)
        self._do(lambda: self.page.wait_for_timeout(150))
        typed = _one_line(row.typed or ("" if row.pick == "hear" else row.answer))
        if typed:
            self._do(lambda: el.press_sequentially(typed, delay=TYPE_DELAY_MS), swallow=False)
        texts: list[str] = []
        for _ in range(MENU_TRIES):
            self._do(lambda: self.page.wait_for_timeout(MENU_WAIT_MS))
            texts = [" ".join(str(t).split()) for t in (self._do(lambda: el.evaluate(OPTIONS_FOR_JS), []) or [])]     # this drop-down's own list
            if texts:
                break
        if row.pick == "city":
            want = shortfields.pick_city_option(texts, row.typed) or option_match.choose(texts, row.typed)
        elif row.pick == "country":
            want = shortfields.pick_country_option(texts, row.typed)
        elif row.pick == "hear":
            want = shortfields.hear_choice(texts, self.job)[0]
        else:
            want = option_match.choose(texts, row.answer)
        i = texts.index(want) if want in texts else -1
        if i < 0 or not shortfields.clickable_text(texts[i]) or shortfields.NO_ITEM.search(texts[i]):
            self._do(lambda: self.page.keyboard.press("Escape"))
            shown = " | ".join(texts[:8]) or "none"
            return False, f"no listed option matches {(row.typed or row.answer)!r} (options: {shown})"
        self._do(lambda: self.frame.locator(f'[data-co-opt="{i}"]').first.click(), swallow=False)
        row.answer = texts[i]
        return True, ""

    def _checkbox(self, dom: DomField) -> tuple[bool, str]:
        el = self.loc(dom)
        try:
            el.set_checked(True, timeout=3000)
            return True, ""
        except Exception as e:  # noqa: BLE001
            raise_if_closed(e, self.page)
        if self._do(lambda: el.is_checked(), False):
            return True, ""
        nid = dom.native_id
        if nid:
            lab = self.frame.locator(f'label[for="{_css(nid)}"]').first
            if self._do(lambda: lab.count(), 0):
                self._do(lambda: lab.click())
                return bool(self._do(lambda: el.is_checked(), False)), ""
        return False, "the check box could not be set: tick it yourself"

    def _radio(self, row: AP.Row, dom: DomField) -> tuple[bool, str]:
        opt = option_match.choose(dom.options, row.answer)
        if opt is None:
            return False, f"no option {row.answer!r} ({' | '.join(dom.options[:8])})"
        r = self.loc(dom, opt)
        try:
            r.check(timeout=3000)
            return True, ""
        except Exception as e:  # noqa: BLE001
            raise_if_closed(e, self.page)
        rid = self._do(lambda: r.get_attribute("id"), "")
        if rid:
            self._do(lambda: self.frame.locator(f'label[for="{_css(rid)}"]').first.click())
            return bool(self._do(lambda: r.is_checked(), False)), ""
        return False, "the option could not be chosen: choose it yourself"

    def put(self, row: AP.Row, dom: DomField) -> tuple[bool, str]:
        """Fill one field; (done, why not)."""
        el = self.loc(dom)
        t = dom.type
        if t == "file":
            path = self.cv if row.source == "cv" else self.letter if row.source == "letter" else None
            if not path or not Path(path).is_file():
                return False, "the file is missing"
            self._do(lambda: el.set_input_files(path), swallow=False)
            return True, ""
        if t == "combobox":
            return self._combobox(row, dom)
        if t == "select":
            self._do(lambda: el.select_option(label=row.answer), swallow=False)
            return True, ""
        if t == "checkbox":
            return self._checkbox(dom)
        if t == "radio":
            return self._radio(row, dom)
        text = row.answer if t == "textarea" else _one_line(row.answer)
        self._do(lambda: el.fill(text), swallow=False)
        return True, ""

    def one(self, row: AP.Row, dom: DomField | None, i: int, total: int) -> Done:
        label = row.q.label[:60]
        tag = f"[{i:>2}/{total}] {label} …"
        if dom is None:
            d = Done(row, None, NOT_FOUND if row.status in (AP.READY, AP.DRAFT) else row.status, why="no such field on the page")
            self.out(f"{tag} {d.status}" + (f" ({row.why})" if row.why and row.status not in (AP.READY, AP.DRAFT) else ""))
            return d
        if row.status not in (AP.READY, AP.DRAFT):
            self.out(f"{tag} {row.status}" + (f" ({row.why})" if row.why else ""))
            return Done(row, dom, row.status, self.read(dom), row.why)
        held = self.read(dom)
        if held and self.holds(held, row, dom):
            self.out(f'{tag} {VERIFIED} "{held[:70]}" (already there)')
            return Done(row, dom, VERIFIED, held)
        try:
            ok, why = self.put(row, dom)
        except BrowserClosed:
            raise
        except Exception as e:  # noqa: BLE001  (a field that refused the value: reported, the next field goes on)
            raise_if_closed(e, self.page)
            ok, why = False, f"could not be filled ({str(e).splitlines()[0][:100]})"
        self._moved()
        if not ok:
            self.out(f"{tag} {AP.NEEDS_USER} ({why})")
            return Done(row, dom, AP.NEEDS_USER, self.read(dom), why)
        if dom.type == "file":
            self._do(lambda: self.page.wait_for_timeout(800))                      # the form takes the file and shows its name
        value = self.read(dom, row.answer if dom.type == "file" else "")
        good = self.holds(value, row, dom)
        status = VERIFIED if good else LOST
        self.out(f'{tag} {status} "{value[:70]}"' + (" (a draft you have not approved)" if row.status == AP.DRAFT else ""))
        return Done(row, dom, status, value, "" if good else f"filled, then the form held {value[:40]!r}")

    def run(self, plan: AP.Plan, fields: Sequence[DomField], src: AP.Sources | None = None) -> Result:
        got, extra = match(plan.rows, fields)
        rows = list(plan.rows)
        if src is not None and extra:                                         # fields the plan did not know: planned now (no drafting in the browser)
            later = AP.Sources(**{**src.__dict__, "freetext": None})
            has_cover = any(r.q.type == "file" and AP.COVER.search(f"{r.q.label} {r.q.key}") for r in rows)
            state: dict = {}
            for f in extra:
                if f.type in ("hidden",) or (not f.visible and f.type not in ("file", "radio", "checkbox")):
                    continue
                q = AP.questions_from_page([f])[0]
                row = AP.resolve(q, len(rows) + 1, later, state, has_cover)
                rows.append(row)
                got[row.n] = f
        order = sorted(rows, key=lambda r: (0 if r.q.type == "file" else 1, r.n))
        res = Result()
        total = len(order)
        uploaded = False
        try:
            for i, row in enumerate(order, 1):
                if row.q.type != "file" and uploaded:
                    if not verify.wait_quiet(self.page, self.quiet_ms):
                        self.out("  the form was still changing after the upload; filling it anyway (everything is read back)")
                    uploaded = False
                d = self.one(row, got.get(row.n), i, total)
                res.done.append(d)
                if row.q.type == "file" and d.status == VERIFIED:
                    uploaded = True
        except Navigated as e:
            res.navigated = True
            res.done.append(Done(row, got.get(row.n), NAVIGATED, why=f"the page moved after this field: {e}"))      # the field that moved it
            self.out(f"\nPAGE NAVIGATED DURING FILL: check whether anything was submitted ({e})")
            return res
        cover = covered_by(res.done)
        for d in res.done:                                                     # a schema field the page does not show, held by a page field
            label = cover.get(d.row.n)
            if label:
                d.status, d.why = COVERED, f"not on the page; '{label[:60]}' holds the answer (VERIFIED)"
                self.out(f"  {d.row.q.label[:60]}: {d.why}")
        res.errors = list(self._do(lambda: self.frame.evaluate(ERRORS_JS), []) or [])
        for msg in res.errors:
            self.out(f"  the form shows: {msg}")
        return res


def _one_line(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _css(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')


def write_record(folder: Path, plan: AP.Plan, res: Result, page) -> None:
    """The dry-run record of a plan-first fill: plan.json, fields.json, fields.md and form_full.png (apply/dryrun.py writes the rest)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(json.dumps({"origin": plan.origin, "url": plan.url, "rows": [r.as_dict() for r in plan.rows]}, indent=2,
                                                 ensure_ascii=False), encoding="utf-8")
    (folder / "plan.txt").write_text("\n".join(plan.table()) + "\n", encoding="utf-8")


def needs_user(res: Result) -> list[str]:
    return [d.row.q.label for d in res.done if d.row.q.required and d.status in (AP.NEEDS_USER, LOST, NOT_FOUND)]


RE_SPACE = re.compile(r"\s+")
