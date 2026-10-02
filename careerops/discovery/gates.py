"""Rule gates: cheap, deterministic triage that runs before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tailoring.scorer import ceiling, taxonomy_keywords
from .register import SponsorIndex

FAMILY_REASON = "title not in target families"

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
    family: str | None = None
    tier: int | None = None
    ceiling_pre: float | None = None


@dataclass
class Family:
    name: str
    tier: int
    headline: str
    title: list[str]
    projects: list[str]


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return None


def min_years_required(jd: str) -> int | None:
    found = [int(m.group(1)) for m in YEARS.finditer(jd)]
    found = [y for y in found if 0 < y < 20]
    return min(found) if found else None


# ---------------------------------------------------------------- role families (M4b)
def load_roles(settings, fb: dict | None = None) -> list[Family]:
    """Families from `settings.roles`, sorted by tier (stable, so config order breaks ties).
    With a fact bank, every preferred project id must exist in it — the user may rename ids."""
    fams = sorted((Family(f["name"], int(f["tier"]), f["headline"], list(f["title"]), list(f.get("projects", [])))
                   for f in settings["roles"]["families"]), key=lambda f: f.tier)
    if fb is not None:
        known = [p["id"] for p in fb.get("projects", [])]
        for f in fams:
            for pid in f.projects:
                if pid not in known:
                    raise ValueError(f"settings.yaml roles.families[{f.name}].projects: unknown project id '{pid}' "
                                     f"(fact_bank.yaml has: {', '.join(known)})")
    return fams


def family(settings, name: str | None) -> Family | None:
    return next((f for f in load_roles(settings) if f.name == name), None)


def classify_role(title: str, settings) -> tuple[str, int] | None:
    """(family, tier) of the first family, in tier order, whose title pattern matches; None if out of scope."""
    f = next((f for f in load_roles(settings) if _any(f.title, title)), None)
    return (f.name, f.tier) if f else None


def level_excluded(title: str, settings) -> str | None:
    """The level pattern (senior, lead, intern, ...) that excludes this title, if any."""
    return _any(settings["roles"]["levels"]["exclude"], title)


def pre_ceiling(jd: str, fb: dict, settings) -> float:
    """Honest ceiling from taxonomy keywords only — no LLM, cheap enough to run at triage to order the queue."""
    from ..tailoring.tailor import fact_text   # lazy: tailor imports this module

    sc = settings["scoring"]
    return ceiling(fact_text(fb), taxonomy_keywords(jd, sc["required_weight"], sc["preferred_weight"]))[0]


def triage(job: dict, settings, sponsors: SponsorIndex | None, fb: dict | None = None) -> Verdict:
    """Rule verdict for one job. With a fact bank, also computes `ceiling_pre`."""
    g = settings["gates"]
    v = Verdict(passed=True)
    title, loc, jd = job.get("title", ""), job.get("location", "") or "", job.get("description", "") or ""

    role = classify_role(title, settings)
    if role is None:
        v.reasons.append(FAMILY_REASON)
    else:
        v.family, v.tier = role
    hit = level_excluded(title, settings)
    if hit:
        v.reasons.append(f"level excluded ({hit})")
    if fb is not None and jd:
        v.ceiling_pre = pre_ceiling(jd, fb, settings)
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
