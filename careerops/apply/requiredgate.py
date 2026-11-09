"""The required-field table (v2.4.1): every REQUIRED field of the filled form, with one status, printed before the submit gate and at the end of a dry run.

  VERIFIED     filled by this run and read back from the form (apply/verify.py)
  FILLED       holds a value this run did not read back: the form's own, a library answer, a draft in the field, a file attached
  LOST         filled by this run, and the form emptied it again
  EMPTY        nothing in it, and no reason recorded
  NEEDS USER   only the person can answer it: an attestation (never ticked), a visa type nobody wrote, a question answers.yaml cannot answer, a letter
               that failed its guards, no JD to draft from ...
  COVERED      (9 Nov 2026) a field the form's schema lists that is not on the page, whose answer a VERIFIED page field already holds (RightShip's
               schema "Location" and the page's "Location (City)"): it does not block

LOST, EMPTY and NEEDS USER block an auto-submit (autoapply.py raises NeedsUser before any submit control is reached) and are printed in red with
their label. A table is never an approval: it only reports what is on the page. Reads the page; clicks nothing.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from . import answers as answers_mod
from . import attest, formloop, shortfields

STATUSES = ("FILLED", "VERIFIED", "LOST", "EMPTY", "NEEDS USER", "COVERED")
CHOICE_KINDS = ("combobox", "select", "radio")
BLOCKING = ("LOST", "EMPTY", "NEEDS USER")
RED, RESET = "\033[31m", "\033[0m"


@dataclass
class Row:
    label: str
    key: str
    kind: str
    required: bool
    status: str
    value: str = ""
    source: str = ""
    reason: str = ""
    options: list[str] = field(default_factory=list)          # a choice field's options, listed for an empty one (fields.md, report.md)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def blocks(self) -> bool:
        return self.status in BLOCKING


def _k(text: str) -> str:
    return answers_mod.norm(text)[:60]


def _value_of(f: formloop.Field) -> str:
    """What the field holds now: a drop-down's chosen text for a combobox, the file names for a file input, else its value."""
    if f.kind == "combobox":
        return shortfields.combo_value(f.el)
    return (f.value or "").strip()


def _reason_for(needs: Mapping[str, str], f: formloop.Field) -> str:
    for q, why in needs.items():
        if _k(q) == _k(f.question) or _k(q) == _k(f.short) or _k(q).startswith(_k(f.question)[:30]) and _k(f.question)[:30]:
            return why
    return ""


def build(fields: list[formloop.Field], *, verified: list[str], lost: list[str], needs_user: Mapping[str, str],
          attestations: list[str], sources: Mapping[str, str], loop_status: Mapping[str, str] | None = None,
          letter_note: str = "", required_only: bool = True) -> list[Row]:
    """One Row per field of `fields` (a scan of the filled page), required ones only unless `required_only` is False."""
    ver = {_k(x) for x in verified}
    gone = {_k(x) for x in lost}
    att = {_k(x) for x in attestations}
    rows: list[Row] = []
    for f in fields:
        if required_only and not f.required:
            continue
        value = _value_of(f)
        names = {_k(f.short), _k(f.question), answers_mod.norm(f.question)[:80], answers_mod.norm(f.short)[:80]} - {""}      # Ctx.source files a question under norm()[:80]
        source = next((sources[n] for n in names if n in sources), "")
        if not source and loop_status and loop_status.get(f.key):
            source = "freetext"
        reason = _reason_for(needs_user, f)
        if f.kind in ("checkbox", "radio", "combobox", "select") and (attest.is_attestation(f.question) or names & att) and not value:
            status, reason = "NEEDS USER", reason or "attestation: you set it yourself, code never does"
        elif names & gone:
            status = "LOST"
        elif names & ver:
            status = "VERIFIED"
        elif value:
            status = "FILLED"
        elif f.kind in ("checkbox", "radio") and source.startswith("answered No"):
            status, value = "FILLED", "(left unticked: the answer is No)"
        elif reason:
            status = "NEEDS USER"
        elif f.kind == "file" and "cover" in f.question.lower() and letter_note:
            status, reason = "NEEDS USER", letter_note
        else:
            status = "EMPTY"
        if status == "FILLED" and f.kind == "file":
            source = source or ("cv" if re.search(r"resume|cv|curriculum", f.question, re.I) else "letter" if "cover" in f.question.lower() else "")
        rows.append(Row(f.question[:200], f.key, f.kind, f.required, status, value[:400], source, reason,
                        list(f.options or []) if f.kind in CHOICE_KINDS else []))
    return rows


def check(page, ctx, verified: list[str], lost: list[str], loop_status: Mapping[str, str] | None = None, letter_note: str = "",
          required_only: bool = True) -> list[Row]:
    """The table for the page as it is now (scans it again). `required_only` False: every field, for the dry-run record."""
    fields = formloop.scan(page)
    return build(fields, verified=verified, lost=lost, needs_user=getattr(ctx, "needs_user", {}), attestations=list(getattr(ctx, "attestations", []) or []),
                 sources=getattr(ctx, "sources", {}), loop_status=loop_status, letter_note=letter_note, required_only=required_only)


def blocking(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.blocks]


def empty_choices(rows: list[Row]) -> list[tuple[int, Row]]:
    """(row number, row) of every choice field left empty for the person (EMPTY, or NEEDS USER but not an attestation) that has options."""
    return [(i, r) for i, r in enumerate(rows, 1) if r.kind in CHOICE_KINDS and r.options and not r.value
            and (r.status == "EMPTY" or (r.status == "NEEDS USER" and not r.reason.startswith("attestation")))]


def red(text: str, color: bool | None = None) -> str:
    """`text` in red when the terminal can show it (or FORCE_COLOR is set); plain otherwise."""
    on = color if color is not None else (bool(os.environ.get("FORCE_COLOR")) or (sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)()))
    return f"{RED}{text}{RESET}" if on else text


def render(rows: list[Row], color: bool | None = None) -> list[str]:
    """The table as lines: one per required field, a blocking one in red with its reason, and a closing line that says whether submitting is blocked."""
    lines = ["  REQUIRED FIELDS (status of each before the submit gate)"]
    for r in rows:
        line = f"  {r.status:<10}  {r.label[:90]}" + (f"  [{r.value[:50]}]" if r.value and r.status != "VERIFIED" else "") + (f"  -> {r.reason}" if r.reason else "")
        lines.append(red(line, color) if r.blocks else line)
    block = blocking(rows)
    lines.append(red(f"  BLOCKS AUTO-SUBMIT: {len(block)} required field(s): " + "; ".join(f"{r.label[:50]} ({r.status})" for r in block), color) if block
                 else "  every required field is FILLED or VERIFIED: nothing here blocks an auto-submit")
    return lines
