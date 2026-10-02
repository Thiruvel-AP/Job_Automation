"""One ordering key for every queue (tailor, review, apply, Excel) and the tier-3 run quota (M4b).

  k(j) = ( 1[c_j < floor],  tier_j,  −c_j,  −posted_or_first_seen_j )

Every job that can honestly reach the floor comes first; then tier 1 before 2 before 3; then best honest fit;
ties go to the freshest posting. c_j is `ceiling_pre` (taxonomy-only, set at triage) for the tailor queue and
COALESCE(ceiling, ceiling_pre) once tailored — the ceiling stored after LLM keyword analysis is more honest,
since the taxonomy-only one overestimates domain-heavy JDs.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, TypeVar

J = TypeVar("J", bound=Mapping[str, Any])


def _get(job: Mapping[str, Any], key: str) -> Any:
    return job[key] if key in job.keys() else None     # works for dicts and sqlite3.Row


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def honest_ceiling(job: Mapping[str, Any], tailored: bool = False) -> float | None:
    c = _get(job, "ceiling") if tailored else None
    return c if c is not None else _get(job, "ceiling_pre")


def queue_key(job: Mapping[str, Any], floor: float, tailored: bool = False) -> tuple:
    """Ascending sort key. Rows without a ceiling or tier (not yet retriaged) sort last."""
    c = honest_ceiling(job, tailored)
    c = -math.inf if c is None else c
    tier = _get(job, "tier")
    fresh = _ts(_get(job, "posted_at") or _get(job, "first_seen"))
    return (c < floor, math.inf if tier is None else tier, -c, -fresh)


def order(jobs: Sequence[J], settings, tailored: bool = False) -> list[J]:
    floor = settings["roles"]["floor"]
    return sorted(jobs, key=lambda j: queue_key(j, floor, tailored))


def select_for_run(jobs: Sequence[J], n: int, settings) -> list[J]:
    """Pick up to n jobs for one tailoring run. Tier 3 fills at most ⌊tier3_max_share·n⌋ slots in key order;
    slots that tiers 1–2 cannot fill go to the remaining tier-3 jobs. Result is in key order."""
    queue = order(jobs, settings)
    cap3 = math.floor(settings["roles"]["tier3_max_share"] * n)
    picked, held = [], []
    for j in queue:
        if len(picked) == n:
            break
        if _get(j, "tier") == 3 and sum(_get(p, "tier") == 3 for p in picked) >= cap3:
            held.append(j)
        else:
            picked.append(j)
    picked += held[: n - len(picked)]
    return order(picked, settings)
