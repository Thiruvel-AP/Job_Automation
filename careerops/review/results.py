"""`careerops results` (v3.5.2): the fact-bank bullets listed under "Needs a result" (no `result:` field; tailor.needs_result is the same rule
for one CV), one at a time. You type the result as you want it kept, or `s` to skip, `q` to stop; nothing is drafted for you and no model is
called. What you typed is saved as that bullet's `result:` only after a `y` for that exact text, through factbank_append.add_field (a dated
backup first; the file must still load and validate, else the backup is put back). The bullet's own text never changes.

    az1  AstraZeneca  (no result)
      text: Built a tool-calling LLM agent ... for a multi-omics cancer research platform ...
      Result as you want it kept (a number or an outcome), [s] skip, [q] quit > used by the whole research team
      Save exactly this as `result:` on az1? [y/N] y
      saved (backup fact_bank.20261110-141233.yaml)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import factbank_append as F
from ..config import load_yaml

PROMPT = "    Result as you want it kept (a number or an outcome), [s] skip, [q] quit > "
CONFIRM = "    Save exactly this as `result:` on {id}? [y/N] "


def needing(fb: dict, ids: set[str] | None = None) -> list[tuple[dict, dict]]:
    """[(entry, bullet)] for every bullet with no `result:`, in fact-bank order (experience, then projects); `ids` keeps only those."""
    out = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                if not str(b.get("result") or "").strip() and (not ids or str(b.get("id")) in ids):
                    out.append((e, b))
    return out


def _replaced_by(fb: dict) -> dict[str, str]:
    return {str(r): str(b.get("id")) for sec in ("experience", "projects") for e in fb.get(sec) or [] for b in e.get("bullets") or []
            for r in b.get("replaces") or []}


def run(fb_path: Path, *, ask: Callable[[str], str] = input, out: Callable[[str], None] = print, ids: set[str] | None = None,
        limit: int | None = None, now: datetime | None = None) -> dict:
    """Walk the bullets; returns {"saved": [ids], "skipped": [ids], "left": n (not reached)}."""
    fb = load_yaml(fb_path)
    todo = needing(fb, ids)
    if limit is not None:
        todo = todo[:limit]
    stats: dict = {"saved": [], "skipped": [], "left": 0}
    out(f"results: {len(todo)} bullet(s) with no `result:`; type the result yourself (nothing is drafted), [s] skips, [q] stops")
    replaced = _replaced_by(fb)
    for n, (entry, bullet) in enumerate(todo):
        bid = str(bullet["id"])
        where = str(entry.get("org") or entry.get("name") or entry.get("id") or "").split(",")[0][:50]
        note = f"; on most CVs {replaced[bid]} replaces it" if bid in replaced else ""
        out(f"\n  {bid}  {where}  (no result{note})\n    text: {bullet.get('text')}")
        typed = " ".join((ask(PROMPT) or "").split())
        if typed.lower() == "q":
            stats["left"] = len(todo) - n
            break
        if not typed or typed.lower() == "s":
            stats["skipped"].append(bid)
            continue
        if (ask(CONFIRM.format(id=bid)) or "").strip().lower() != "y":
            out("    not saved")
            stats["skipped"].append(bid)
            continue
        try:
            done = F.add_field(Path(fb_path), bid, "result", typed, confirmed="y", now=now)
        except F.AppendError as e:
            out(f"    {e}")
            stats["skipped"].append(bid)
            continue
        out(f"    saved as `result:` on {bid} (backup {done.backup.name})")
        stats["saved"].append(bid)
    out(f"\nresults: saved {len(stats['saved'])}, skipped {len(stats['skipped'])}" + (f", {stats['left']} not reached" if stats["left"] else ""))
    return stats
