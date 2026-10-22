"""What the form does to a fill, and how the assist and the auto-apply guard against it (round of 22 Oct, the loveholidays Ashby form).

An Ashby form parses an uploaded CV and re-renders: whatever was typed before the upload is wiped. So, in both callers:

  1. FILES FIRST. Every file field is handled before any text field. The "Autofill from resume" control is never used (`is_autofill`): the
     CV goes only to the real Resume / CV field.
  2. WAIT until the form stops changing: no DOM mutation for QUIET_MS (2 s), at most QUIET_MAX_MS (`wait_quiet`). Only when a file was uploaded.
  3. Then the text fields, selects and boxes are filled; each fill is noted in a `Tracker`.
  4. VERIFY: every noted field is read back (`Tracker.verify`). An empty one is filled once more; then it prints `VERIFIED` or `LOST`. A LOST
     field is listed under REQUIRED + EMPTY by the assist and stops the auto-apply (nothing is submitted with a field the form swallowed).

Nothing here clicks or submits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

QUIET_MS = 2000                  # the form must not change at all for this long after the uploads before a text field is filled
QUIET_MAX_MS = 15000             # a form that never settles (a spinner that never stops) is filled anyway after this long

AUTOFILL = re.compile(r"auto-?fill", re.I)

# An upload the form turned down: the error text of an alert / error container that names a file or an upload. Only error containers are read,
# so the form's own helper text ("PDF, DOC; max 5 MB") is not mistaken for a refusal.
UPLOAD_ERROR = re.compile(r"(?:upload|file|resume|cv|attachment)[^.]{0,50}(?:fail|error|refus|reject|not (?:allowed|supported|accepted)|too (?:large|big)|invalid)|"
                          r"(?:fail|unable|could not|couldn['’]t|cannot|can['’]t)[^.]{0,30}upload|invalid file|file type", re.I)
UPLOAD_ERROR_JS = """() => [...document.querySelectorAll('[role=alert], [aria-live=assertive], [class*=error i], [class*=invalid i]')]
  .filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 10)"""


def upload_refused(page) -> str | None:
    """The error text of an upload the form refused (visible error containers only), or None. A page that cannot be asked says None."""
    try:
        texts = page.evaluate(UPLOAD_ERROR_JS) or []
    except Exception:  # noqa: BLE001
        return None
    return next((" ".join(t.split())[:160] for t in texts if isinstance(t, str) and UPLOAD_ERROR.search(t)), None)

# True when this file input belongs to an "Autofill from resume" block: its own id / name / label says so, or the text of the smallest
# container around it does (a container is not climbed past the point where it also holds another field or another file input).
AUTOFILL_JS = """el => {
  const re = /auto-?fill/i;
  const own = [el.id, el.name, el.getAttribute('aria-label'), el.getAttribute('data-testid'), el.getAttribute('data-qa')].join(' ');
  if (re.test(own)) return true;
  if (el.labels && re.test([...el.labels].map(l => l.innerText).join(' '))) return true;
  let node = el.parentElement;
  for (let i = 0; node && i < 6; i++, node = node.parentElement) {
    if (node.querySelectorAll('input[type=file]').length > 1) break;
    if (node.querySelectorAll('input:not([type=file]):not([type=hidden]), textarea, select').length) break;
    if (re.test(node.innerText || '')) return true;
  }
  return false;
}"""

# Resolves true once `quiet` ms have passed with no DOM mutation, false when `limit` ms ran out first.
QUIET_JS = """([quiet, limit]) => new Promise(resolve => {
  const start = Date.now(); let last = start;
  const seen = new MutationObserver(() => { last = Date.now(); });
  seen.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  const tick = () => {
    const now = Date.now();
    if (now - last >= quiet) { seen.disconnect(); resolve(true); }
    else if (now - start >= limit) { seen.disconnect(); resolve(false); }
    else setTimeout(tick, 50);
  };
  tick();
})"""

# A selector that finds this element again after the form re-rendered it ('' when it has no id and no unique name).
SELECTOR_JS = """el => {
  if (el.id) return '#' + CSS.escape(el.id);
  const name = el.getAttribute('name');
  if (!name) return '';
  const sel = el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\\\"') + '"]';
  return document.querySelectorAll(sel).length === 1 ? sel : '';
}"""


def is_autofill(el) -> bool:
    """Is this file input the "Autofill from resume" control? It is never given a file."""
    try:
        return bool(el.evaluate(AUTOFILL_JS))
    except Exception:  # noqa: BLE001  (a detached element: not provably the autofill one)
        return False


def wait_quiet(page, quiet_ms: int = QUIET_MS, limit_ms: int = QUIET_MAX_MS) -> bool:
    """Block until the page has not changed (no DOM mutation) for `quiet_ms`; False when it kept changing for `limit_ms` (or cannot be asked)."""
    try:
        return bool(page.evaluate(QUIET_JS, [quiet_ms, limit_ms]))
    except Exception:  # noqa: BLE001  (the page navigated or is gone: the caller goes on and the read-back tells what survived)
        return False


@dataclass
class Fill:
    """One field the program filled: what to read back and how to fill it once more."""
    label: str
    el: Any
    kind: str                       # "text" | "select" (value = the option text) | "check"
    value: str = ""
    selector: str = ""


@dataclass
class Tracker:
    fills: list[Fill] = field(default_factory=list)

    def add(self, el, label: str, kind: str = "text", value: str = "") -> None:
        try:
            selector = el.evaluate(SELECTOR_JS) if kind != "check" else ""
        except Exception:  # noqa: BLE001
            selector = ""
        self.fills.append(Fill(" ".join((label or "").split())[:80], el, kind, value or "", selector))

    # ------------------------------------------------------------------ read back
    @staticmethod
    def _handle(page, f: Fill):
        """A live handle for the field: found again by its selector when it has one (the form may have replaced the node), else the one held."""
        if f.selector:
            try:
                found = page.query_selector(f.selector)
            except Exception:  # noqa: BLE001
                found = None
            if found is not None:
                return found
        return f.el

    @staticmethod
    def _holds(el, f: Fill) -> bool:
        if f.kind == "check":
            return bool(el.is_checked())
        if f.kind == "select":
            chosen = " ".join((el.evaluate("e => e.selectedOptions.length ? e.selectedOptions[0].textContent : ''") or "").split())
            return chosen != "" and chosen.casefold() == " ".join(f.value.split()).casefold()
        return bool((el.input_value() or "").strip())

    @staticmethod
    def _again(el, f: Fill) -> None:
        if f.kind == "check":
            el.check()
        elif f.kind == "select":
            el.select_option(label=f.value)
        else:
            el.fill(f.value)

    def verify(self, page, out: Callable[[str], None] = print) -> tuple[list[str], list[str]]:
        """(verified, lost) labels. Each filled field is read back; an empty one is filled once more and read again. Prints one line each:
        `VERIFIED  label` (`VERIFIED  label (filled again)`) or `LOST      label`."""
        verified, lost = [], []
        for f in self.fills:
            again = False
            ok = self._read(page, f)
            if not ok:
                again = True
                try:
                    self._again(self._handle(page, f), f)
                except Exception:  # noqa: BLE001  (the field is gone or refuses the value: it is read below and reported lost)
                    pass
                ok = self._read(page, f)
            if ok:
                verified.append(f.label)
                out(f"  VERIFIED  {f.label}" + (" (filled again)" if again else ""))
            else:
                lost.append(f.label)
                out(f"  LOST      {f.label}")
                try:
                    self._handle(page, f).evaluate("e => e.style.outline = '3px solid red'")
                except Exception:  # noqa: BLE001
                    pass
        return verified, lost

    def _read(self, page, f: Fill) -> bool:
        try:
            return self._holds(self._handle(page, f), f)
        except Exception:  # noqa: BLE001  (a detached node cannot be read: nothing proves it still holds the value)
            return False
