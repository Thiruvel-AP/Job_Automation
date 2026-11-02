"""Why a job has no letter, in the guards' own words (v2.4.1). A bare "no letter" told the person nothing on the RightShip run (31 Oct 2026): every
place that says a job has no letter says this instead. Pure function over the report's `letter` dict; imports nothing from the package."""
from __future__ import annotations

from typing import Any, Mapping

FALLBACK_MARK = "template fallback"      # tailor.FALLBACK_MARK: a problem entry that is a marker, not a reason


def letter_reason(letter: Mapping[str, Any] | None) -> str:
    """The exact reason the letter is not there: the report's `reason`, then each rejected draft's guard problems (up to 900 characters), else the
    recorded problems. "" for a letter that exists (status ok / fallback). Never a bare "no letter": a report that records nothing says so."""
    letter = letter or {}
    status = str(letter.get("status") or "")
    if status in ("ok", "fallback"):
        return ""
    reason = str(letter.get("reason") or "").strip()
    fails = (letter.get("retry") or {}).get("failures") or []
    problems = [p for p in letter.get("problems") or [] if p != FALLBACK_MARK]
    parts = [reason] if reason else []
    if fails:
        drafts = "; ".join(f"draft {f.get('attempt', '?')}: " + " | ".join(str(p) for p in f.get("problems") or [])[:300] for f in fails)[:900]
        if drafts and drafts not in reason:
            parts.append(drafts)
    elif problems and not any(problems[0][:60] in p for p in parts):
        parts.append("; ".join(problems)[:300])
    if not parts:
        parts = [f"the report holds no letter (status {status or 'missing'}) and records no reason"]
    return " | ".join(parts)
