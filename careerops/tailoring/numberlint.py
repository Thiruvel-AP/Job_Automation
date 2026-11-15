"""v3.7 number gate: lints only. Nothing here writes, generates or suggests a number; the fix for both lints is "add a number to the fact bank".

  bullet-no-number    info  a bullet printed on the CV has no digit and its fact-bank entry has no `result`
  letter-few-numbers  warn  the rendered letter has fewer than MIN_LETTER_NUMBERS distinct numbers ("20 minutes" and "180 Degrees" do not count)

A warn lint moves an auto-band job into the review band (`bands.demote`, and the `number lints` gate in apply/autogate.py).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

MIN_LETTER_NUMBERS = 3
FIX = "add a number to the fact bank"
_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_NOT_COUNTED = re.compile(r"\b20\s+minutes\b|\b180\s+Degrees\b", re.I)


def letter_numbers(text: str) -> set[str]:
    """The distinct numbers of a letter, commas closed up, without the ask ("20 minutes") and the employer's name ("180 Degrees")."""
    return {n.replace(",", "") for n in _NUM.findall(_NOT_COUNTED.sub(" ", text or ""))}


def bullets_without_number(ctx: Mapping[str, Any], fb: Mapping[str, Any]) -> list[str]:
    """Ids of the printed bullets with no digit whose fact-bank bullet has no `result`, in CV order."""
    results = {str(b.get("id")): bool(str(b.get("result") or "").strip())
               for sec in ("experience", "projects") for e in fb.get(sec) or [] for b in e.get("bullets") or []}
    out: list[str] = []
    for sec in ("experience", "projects"):
        for e in ctx.get(sec) or []:
            for bid, text in zip(e.get("bullet_ids") or [], e.get("bullets") or []):
                if not re.search(r"\d", str(text)) and not results.get(str(bid)):
                    out.append(str(bid))
    return list(dict.fromkeys(out))


def lints(ctx: Mapping[str, Any], fb: Mapping[str, Any], letter_text: str | None) -> list[dict[str, Any]]:
    """The number lints of one tailored job: [{code, severity, ids | numbers, message, fix}]. `letter_text` None = no letter was drafted (no letter lint)."""
    out: list[dict[str, Any]] = []
    ids = bullets_without_number(ctx, fb)
    if ids:
        out.append({"code": "bullet-no-number", "severity": "info", "ids": ids, "fix": FIX,
                    "message": f"bullet(s) with no number and no result: {', '.join(ids)}"})
    if letter_text is not None and letter_text.strip():
        nums = sorted(letter_numbers(letter_text))
        if len(nums) < MIN_LETTER_NUMBERS:
            out.append({"code": "letter-few-numbers", "severity": "warn", "numbers": nums, "fix": FIX,
                        "message": f"the letter has {len(nums)} distinct number(s) (at least {MIN_LETTER_NUMBERS})"})
    return out


def has_warn(found: list[Mapping[str, Any]] | None) -> bool:
    return any(x.get("severity") == "warn" for x in found or [])


def lines(found: list[Mapping[str, Any]] | None) -> list[str]:
    """The lines of the `[r]` review screen."""
    return [f"  NUMBER LINT ({x['severity']}) {x['code']}: {x['message']} -> {x.get('fix', FIX)}" for x in found or []]
