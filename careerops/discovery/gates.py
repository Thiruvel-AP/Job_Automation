"""Rule gates: cheap, deterministic triage that runs before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tailoring.scorer import ceiling, taxonomy_keywords
from .register import CompanyIndex, SponsorIndex

FAMILY_REASON = "title not in target families"
NOT_ON_REGISTER = "Not on sponsor register"
# The notes about the sponsor register are information (a weight on priority, see priority.py), not warnings: the review screen
# shows them on the Sponsor line and the judge gate does not treat them as a reason to ask you.
SPONSOR_FLAG_STARTS = (NOT_ON_REGISTER, "Sponsor match is fuzzy", "Sponsor register not loaded")


def is_sponsor_flag(flag: str) -> bool:
    return str(flag).startswith(SPONSOR_FLAG_STARTS)

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
    sponsor_match: str | None = None       # exact | fuzzy(0.95) | none; None = the register is not loaded
    sponsor_route: str | None = None
    sponsor_rating: str | None = None
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


def verdict_status(v: Verdict) -> str:
    return "skipped" if not v.passed else "needs_jd" if v.needs_full_jd else "triaged"


def verdict_fields(v: Verdict) -> dict:
    """The jobs-table columns a verdict sets."""
    return {"status": verdict_status(v), "reason": "; ".join(v.reasons) or None, "flags": v.flags,
            "sponsor_name": v.sponsor_name, "sponsor_score": v.sponsor_score, "sponsor_match": v.sponsor_match,
            "sponsor_route": v.sponsor_route, "sponsor_rating": v.sponsor_rating,
            "family": v.family, "tier": v.tier, "ceiling_pre": v.ceiling_pre}


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


def triage(job: dict, settings, sponsors: "CompanyIndex | SponsorIndex | None", fb: dict | None = None) -> Verdict:
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

    # An employer off the register is flagged for the reviewer, never skipped: the register lags, and a name can differ.
    if sponsors is not None:
        m = sponsors.lookup(job.get("company", ""), settings["sponsors"].get("fuzzy_threshold", 0.92))
        v.sponsor_name, v.sponsor_score, v.sponsor_match = m.name, m.score, m.label
        v.sponsor_route, v.sponsor_rating = m.route, m.rating
        if m.method == "none":
            if g.get("require_sponsor_match", True):
                v.flags.append(NOT_ON_REGISTER)
        elif m.method == "fuzzy":
            v.flags.append(f"Sponsor match is fuzzy ({m.score:.2f}): '{m.name}' — verify it is the same entity")
    elif g.get("require_sponsor_match", True):
        v.flags.append("Sponsor register not loaded — sponsorship unchecked")

    if len(jd) < 800:   # Adzuna returns ~500-char snippets; real JDs are thousands of chars
        v.needs_full_jd = True
        v.flags.append("Job description looks truncated — fetch/paste the full JD before tailoring")

    v.passed = not v.reasons
    return v
