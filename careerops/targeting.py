"""`targeting:` in settings.yaml: how many jobs are offered, how many are applied to, and what is never offered.

    shortlist_size              jobs `careerops shortlist` offers at a time
    min_triage_score            1-5; a job whose zero-token triage score T is below this is never offered
    daily_max_applications      submitted (by you or auto) per calendar day; `live` stops at the cap
    per_company_max_open        applications at one company with no reply yet, across all its roles
    per_company_cooldown_days   after a rejection or withdrawal at a company, no new application there for this many days
    max_posting_age_days        a posting older than this is offered only with T >= OLD_POSTING_MIN_T
    dealbreakers                your phrases; an ad that STATES one (a quoted sentence) is never offered

A block that is absent means these defaults (an old settings file keeps working). A value that cannot be used is refused with its name: the numbers are
yours, and nothing here ever changes one.
"""
from __future__ import annotations

from dataclasses import dataclass

OLD_POSTING_MIN_T = 4.5          # an ad older than max_posting_age_days needs at least this T to be offered


class TargetingError(ValueError):
    """A `targeting:` value that cannot be used (one line, fit to print)."""


@dataclass(frozen=True)
class Targeting:
    shortlist_size: int = 8
    min_triage_score: float = 3.5
    daily_max_applications: int = 5
    per_company_max_open: int = 2
    per_company_cooldown_days: int = 30
    max_posting_age_days: int = 60
    dealbreakers: tuple[str, ...] = ()


def _number(raw: dict, key: str, default, lo, hi, whole: bool):
    v = raw.get(key, default)
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi and (not whole or float(v).is_integer())
    if not ok:
        raise TargetingError(f"targeting.{key} is {v!r}; it must be {'a whole number' if whole else 'a number'} from {lo} to {hi}")
    return int(v) if whole else float(v)


def config(settings) -> Targeting:
    """settings.yaml -> targeting, with the defaults for what is missing. Raises TargetingError for a value that cannot be used."""
    raw = (getattr(settings, "raw", None) or {}).get("targeting")
    if raw is None:
        return Targeting()
    if not isinstance(raw, dict):
        raise TargetingError(f"targeting is {raw!r}; it must be a mapping (shortlist_size, min_triage_score, ...)")
    d = Targeting()
    given = raw.get("dealbreakers", [])
    if given is None:
        given = []
    if not isinstance(given, list) or not all(isinstance(x, str) and x.strip() for x in given):
        raise TargetingError("targeting.dealbreakers must be a list of phrases, e.g. [\"people management\", \"SC clearance\"]")
    return Targeting(
        _number(raw, "shortlist_size", d.shortlist_size, 1, 50, True),
        _number(raw, "min_triage_score", d.min_triage_score, 1, 5, False),
        _number(raw, "daily_max_applications", d.daily_max_applications, 1, 100, True),
        _number(raw, "per_company_max_open", d.per_company_max_open, 1, 20, True),
        _number(raw, "per_company_cooldown_days", d.per_company_cooldown_days, 0, 3650, True),
        _number(raw, "max_posting_age_days", d.max_posting_age_days, 1, 3650, True),
        tuple(x.strip() for x in given))
