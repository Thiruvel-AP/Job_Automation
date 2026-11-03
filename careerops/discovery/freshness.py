"""How old a posting is, and which freshness tier it is in. Fresh jobs go first; nothing is ever dropped for being old.

`posted_at` is the time the SOURCE says the job was posted, kept with `posted_at_source` (where it came from):

    Greenhouse   first_published          (not updated_at: that resets when a job is reposted, and made a 69-day-old ad look 10 days old)
    Lever        createdAt
    Ashby        publishedAt
    Reed         date
    Adzuna       created
    Gmail alert  the time the alert email arrived (an upper bound: the job was posted at or before it)
    anything else  first_seen: the day discover first saw the job (or, for a hand-added JD, when it was added)

Only the sources above are trusted for the age; a `Posted:` line from any other page is kept as it is but the age counts from
`first_seen`. The age in hours (now - posted_at) puts a job in a tier:

    tier 0   at most 24 hours old
    tier 1   at most 72 hours
    tier 2   at most 7 days
    tier 3   older, or with no date at all

The order is lexicographic, never a filter: tier first, then P = F x S inside the tier (priority.py). There is no recency factor
in P any more: the old R = exp(-days / 14) pushed a 64-day-old posting that was still open down to 0.01.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

TIER_LIMITS_H = (24.0, 72.0, 168.0)           # upper bounds in hours of tiers 0, 1, 2; anything older is tier 3
TIER_NAMES = ("<= 24 h", "<= 72 h", "<= 7 d", "older")
FIRST_SEEN = "first_seen"
GMAIL = "gmail.received"
# the source label stored in `posted_at_source` -> where in the source's answer the time is
SOURCES = {"greenhouse.first_published": "Greenhouse first_published", "lever.createdAt": "Lever createdAt",
           "ashby.publishedAt": "Ashby publishedAt", "reed.date": "Reed date", "adzuna.created": "Adzuna created",
           "workday.postedOn": "Workday postedOn (to the day; \"30+ Days Ago\" is dated 30 days back)",
           "workable.published_on": "Workable published_on", "smartrecruiters.releasedDate": "SmartRecruiters releasedDate",
           "teamtailor.pubDate": "Teamtailor pubDate", "recruitee.published_at": "Recruitee published_at",
           "personio.createdAt": "Personio createdAt", "eploy.datePosted": "Eploy datePosted (JSON-LD)",
           GMAIL: "alert email received (an upper bound)"}
TRUSTED = frozenset(SOURCES)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def is_iso(value) -> bool:
    """Is this an ISO date (2026-10-15) or datetime (2026-10-15T09:30:00Z, with or without seconds / an offset)?"""
    return bool(_ISO.match(str(value or "").strip()))


def parse(value) -> datetime | None:
    """A timezone-aware UTC datetime from an ISO date or datetime (a date is midnight UTC), else None."""
    text = str(value or "").strip()
    if not is_iso(text):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_millis(ms) -> str | None:
    """An ISO UTC datetime from epoch milliseconds (Lever's createdAt), None when it is not a number."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def label(source: str, field: str) -> str:
    """The `posted_at_source` of a time read from `field` of `source`'s answer: "greenhouse.first_published"."""
    return f"{source}.{field}"


def _get(job: Mapping[str, Any], key: str) -> Any:
    return job[key] if key in job.keys() else None            # dicts and sqlite3.Row


def posted(job: Mapping[str, Any]) -> tuple[datetime | None, str]:
    """(when the job was posted, where that comes from). The source's own time when it is one of TRUSTED; otherwise the day (or time)
    it was first seen (`seen_on`, then `first_seen`) with the source label `first_seen`; (None, "") when nothing is known."""
    src = str(_get(job, "posted_at_source") or "")
    if src in TRUSTED:
        dt = parse(_get(job, "posted_at"))
        if dt is not None:
            return dt, src
    dt = parse(_get(job, "seen_on")) or parse(_get(job, "first_seen"))
    return (dt, FIRST_SEEN) if dt is not None else (None, "")


def age_hours(job: Mapping[str, Any], now: datetime | None = None) -> float | None:
    """Hours since the job was posted (never negative), None when no time is known."""
    dt, _ = posted(job)
    if dt is None:
        return None
    return max(((now or datetime.now(timezone.utc)) - dt).total_seconds() / 3600.0, 0.0)


def tier_of(hours: float | None) -> int:
    """0 = at most 24 h old, 1 = at most 72 h, 2 = at most 7 days, 3 = older or no date."""
    if hours is None:
        return 3
    for t, limit in enumerate(TIER_LIMITS_H):
        if hours <= limit:
            return t
    return 3


def tier(job: Mapping[str, Any], now: datetime | None = None) -> int:
    return tier_of(age_hours(job, now))


def age_text(hours: float | None) -> str:
    """"5 h", "30 h", "4 d" (from 72 hours on, days), "date unknown"."""
    if hours is None:
        return "date unknown"
    return f"{hours:.0f} h" if hours < 72 else f"{hours / 24:.0f} d"


def describe(job: Mapping[str, Any], now: datetime | None = None) -> str:
    """One line for the review header, the age in hours first: "posted 5 h ago (Greenhouse first_published), tier 0 (<= 24 h)"; from
    72 hours on the days follow in brackets ("posted 1656 h ago (69 d) ..."); an upper bound says so."""
    dt, src = posted(job)
    hours = age_hours(job, now)
    t = tier_of(hours)
    if dt is None:
        return f"posting date unknown, tier {t} ({TIER_NAMES[t]})"
    how = SOURCES.get(src, "first seen by discover: the source gave no trusted posting time")
    days = f" ({hours / 24:.0f} d)" if hours >= 72 else ""
    return f"posted {hours:.0f} h ago{days} ({how}), tier {t} ({TIER_NAMES[t]})"


def tier_counts(jobs, now: datetime | None = None) -> list[int]:
    """How many of `jobs` are in tiers 0, 1, 2 and 3."""
    counts = [0, 0, 0, 0]
    for j in jobs:
        counts[tier(j, now)] += 1
    return counts


def counts_line(counts: list[int]) -> str:
    return " · ".join(f"tier {t} ({TIER_NAMES[t]}): {n}" for t, n in enumerate(counts))
