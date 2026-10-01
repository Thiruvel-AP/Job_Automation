"""Rule gates: cheap, deterministic triage that runs before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sponsors import SponsorIndex

YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*years?['’]?\s+(?:of\s+)?(?:[\w/&-]+\s+){0,4}?experience",
    re.I,
)


@dataclass
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why skipped
    flags: list[str] = field(default_factory=list)     # warnings shown at review
    sponsor_name: str | None = None
    sponsor_score: float = 0.0
    needs_full_jd: bool = False


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return None


def min_years_required(jd: str) -> int | None:
    found = [int(m.group(1)) for m in YEARS.finditer(jd)]
    found = [y for y in found if 0 < y < 20]
    return min(found) if found else None


def triage(job: dict, settings, sponsors: SponsorIndex | None) -> Verdict:
    g = settings["gates"]
    v = Verdict(passed=True)
    title, loc, jd = job.get("title", ""), job.get("location", "") or "", job.get("description", "") or ""

    if not _any(g["title_include"], title):
        v.reasons.append("title not DS/AI")
    hit = _any(g["title_exclude"], title)
    if hit:
        v.reasons.append(f"title excluded ({hit})")
    if loc and not _any(g["locations_ok"], loc):
        v.reasons.append(f"location '{loc}' not in allow-list")

    for pattern, action, why in g.get("jd_rules", []):
        if re.search(pattern, jd, re.I):
            (v.reasons if action == "skip" else v.flags).append(why)

    yrs = min_years_required(jd)
    if yrs is not None and yrs > g.get("max_years_required", 3):
        v.flags.append(f"JD asks for {yrs}+ years' experience")

    if sponsors is not None and len(sponsors):
        name, score, method = sponsors.match(job.get("company", ""), settings["sponsors"].get("fuzzy_threshold", 0.92))
        v.sponsor_name, v.sponsor_score = name, score
        if name is None and g.get("require_sponsor_match", True):
            v.reasons.append("employer not on Skilled Worker sponsor register")
        elif method in ("fuzzy", "prefix"):
            v.flags.append(f"Sponsor match is {method} ({score:.2f}): '{name}' — verify it is the same entity")
    elif g.get("require_sponsor_match", True):
        v.flags.append("Sponsor register not loaded — sponsorship unchecked")

    if len(jd) < 800:   # Adzuna returns ~500-char snippets; real JDs are thousands of chars
        v.needs_full_jd = True
        v.flags.append("Job description looks truncated — fetch/paste the full JD before tailoring")

    v.passed = not v.reasons
    return v
