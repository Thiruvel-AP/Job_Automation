"""ATS resolution (M3). Spec: docs/IMPLEMENTATION.md §4.

For each in-scope company: guess board slugs from its name, probe the public Greenhouse / Lever / Ashby
APIs, verify the hit really is this company, and fall back to a manual queue + careers-page detector.
Precision over recall: a wrong board means applying to the wrong company.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..db import DB

# Careers-page signatures → (ats, slug group). Greenhouse/Lever/Ashby are fetchable; the rest are recorded only.
SIGNATURES: dict[str, str] = {
    "greenhouse": r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)",
    "lever": r"jobs(?:\.eu)?\.lever\.co/([\w-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([\w-]+)",
    "workday": r"([\w-]+)\.wd\d+\.myworkdayjobs\.com",
    "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)",
    "workable": r"apply\.workable\.com/([\w-]+)",
    "teamtailor": r"([\w-]+)\.teamtailor\.com",
    "recruitee": r"([\w-]+)\.recruitee\.com",
    "pinpoint": r"([\w-]+)\.pinpointhq\.com",
    "bamboohr": r"([\w-]+)\.bamboohr\.com",
}
FETCHABLE = {"greenhouse", "lever", "ashby"}


@dataclass
class Hit:
    ats: str
    slug: str
    board_name: str | None = None
    uk_jobs: int = 0
    total_jobs: int = 0


@dataclass
class ResolveStats:
    probed: int = 0
    verified: int = 0
    unverified: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)


def slug_candidates(register_name: str, ch_name: str | None = None) -> list[str]:
    """≤4 deduped guesses, e.g. 'Monzo Bank Ltd' → ['monzobank', 'monzo-bank', 'monzo']."""
    raise NotImplementedError("M3 — ats.slug_candidates")


def probe(slug: str) -> list[Hit]:
    """Query Greenhouse, Lever (global + EU) and Ashby for `slug`; 404 = not there."""
    raise NotImplementedError("M3 — ats.probe")


def verify(hit: Hit, company_norm: str, slug_is_full_name: bool) -> bool:
    """Accept if board name ≈ company name (ratio ≥ 0.9), or full-name slug with ≥1 UK posting."""
    raise NotImplementedError("M3 — ats.verify")


def resolve(db: DB, settings, limit: int = 300, only_new: bool = False) -> ResolveStats:
    """Probe unresolved in-scope companies, newest sponsors first, at ≤1 request/second."""
    raise NotImplementedError("M3 — ats.resolve")


def detect(url: str) -> tuple[str, str] | None:
    """Fetch a careers page once and return (ats, slug) from the first matching signature."""
    raise NotImplementedError("M3 — ats.detect")
