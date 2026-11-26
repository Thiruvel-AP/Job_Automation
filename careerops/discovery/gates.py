"""Rule gates: cheap, deterministic triage that runs before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tailoring.scorer import ceiling, taxonomy_keywords
from . import countries, workcountry
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
    needs_country: bool = False            # passed every rule, but the country the work is in cannot be told (workcountry.py)
    work_country: str | None = None        # W, ISO code
    work_evidence: str | None = None       # the header / sentence W rests on (the reason when it is unknown)
    work_location: str | None = None       # the remote label when the role is remote and at home (never the ad's words), else None
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
    priority: int | None = None        # the user's ranking, 1 (best) to 15: feeds the role factor R of the priority, never the match
    skills_from: str | None = None     # the fact-bank family whose skill_lines this role family prints (a bank family of its own name wins)


def is_excluded_reason(reason: str | None) -> bool:
    """Is a skipped job's `reason` (the verdict's reasons joined by '; ') about the title: no role family, or an excluded level word?"""
    return any(r.strip() == FAMILY_REASON or r.strip().startswith("level excluded") for r in (reason or "").split(";"))


def verdict_status(v: Verdict) -> str:
    return ("skipped" if not v.passed else "needs_jd" if v.needs_full_jd else "needs_country" if v.needs_country else "triaged")


def verdict_fields(v: Verdict) -> dict:
    """The jobs-table columns a verdict sets."""
    reason = "; ".join(v.reasons) or (f"work country unknown: {v.work_evidence}" if v.needs_country else None)
    return {"status": verdict_status(v), "reason": reason, "flags": v.flags,
            "work_country": v.work_country, "work_evidence": None if v.needs_country else v.work_evidence,
            "work_location": v.work_location,
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
    """Families from `settings.roles`, in LIST order: the list is ordered most specific first on purpose ("Data Science Consultant" must
    reach data_consultant before 'data scien' catches it). `priority` ranks the families; it never decides a match.
    With a fact bank, every preferred project id must exist in it — the user may rename ids."""
    fams = [Family(f["name"], int(f["tier"]), f["headline"], list(f["title"]), list(f.get("projects", [])),
                   int(f["priority"]) if f.get("priority") is not None else None, f.get("skills_from"))
            for f in settings["roles"]["families"]]
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


def match_family(title: str, settings) -> Family | None:
    """The first family, in settings list order, whose title pattern matches; None if out of scope."""
    return next((f for f in load_roles(settings) if _any(f.title, title)), None)


def classify_role(title: str, settings) -> tuple[str, int] | None:
    """(family, tier) of the first family, in list order, whose title pattern matches; None if out of scope."""
    f = match_family(title, settings)
    return (f.name, f.tier) if f else None


def skills_from(settings, name: str | None) -> str | None:
    """The fact-bank family whose skill_lines the role family `name` prints (settings roles.families[*].skills_from), or None."""
    f = family(settings, name)
    return f.skills_from if f else None


SENIOR_WORD = re.compile(r"\b(?:sr|snr)\b(\.?)", re.I)


def canonical_level(title: str) -> str:
    """The title with `Sr`, `Sr.` and `Snr` written as `Senior`: they are one level and the level rule treats them alike ("Sr. X" and "Senior X"
    both match a `senior` pattern in roles.levels.exclude, or neither does). A list that names only `sr` therefore excludes nothing."""
    return SENIOR_WORD.sub(lambda m: "Senior" + (" " if m.group(1) else ""), title or "")


# v3.1: levels that are never a junior / mid role, whatever roles.levels.exclude lists ("Chief AI Engineer" scored L5 on 5 Nov 2026). Same rule as the list:
# a whole word in the title, case-insensitive.
ALWAYS_EXCLUDED = (r"\bchief\b", r"\bhead of\b", r"\bvp\b", r"\bdirector\b", r"\bprincipal\b", r"\bstaff\b")


def level_excluded(title: str, settings) -> str | None:
    """The level pattern (senior, lead, intern, ...) that excludes this title, if any: roles.levels.exclude, then ALWAYS_EXCLUDED (chief, head of, vp, director,
    principal, staff). `Sr` / `Sr.` count as `Senior` (canonical_level)."""
    t = canonical_level(title)
    return _any(settings["roles"]["levels"]["exclude"], t) or _any(list(ALWAYS_EXCLUDED), t)


def pre_estimate(jd: str, fb: dict, settings):
    """The zero-token estimate of the honest ceiling with its parts (discovery/ceiling_estimate.Estimate)."""
    from . import ceiling_estimate

    return ceiling_estimate.estimate(jd, fb, settings)


def pre_ceiling(jd: str, fb: dict, settings) -> float:
    """The zero-token estimate of the honest ceiling (discovery/ceiling_estimate.py: the terms the ad states, taxonomy terms and its own bullet labels, against the
    CV-able fact bank, plus the terms the model is expected to add; v3.5.5). No LLM, cheap enough to run at triage to order the queue."""
    return pre_estimate(jd, fb, settings).ceiling


def visa_countries(settings) -> tuple[str, ...]:
    """The countries (canonical names) applicant.yaml has a non-empty `visa.by_country.<country>` block for (not `default`): a job in
    one of them passes the location rule. () when the file or the key is not there. READ ONLY."""
    applicant = workcountry.applicant_visa(settings)
    found = (countries.canon(k) for k in (((applicant.get("visa") or {}).get("by_country")) or {}))
    return tuple(dict.fromkeys(c for c in found if c and countries.visa_block(applicant, c)))


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
    if loc and not _any(g["locations_ok"], loc) and countries.detect(loc) not in visa_countries(settings):
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
        elif m.method == "alias":
            v.flags.append(f"Sponsor matched via alias '{m.via}': register name '{m.name}' — verify it is the employer of this job")
    elif g.get("require_sponsor_match", True):
        v.flags.append("Sponsor register not loaded — sponsorship unchecked")

    if len(jd) < 800:   # Adzuna returns ~500-char snippets; real JDs are thousands of chars
        v.needs_full_jd = True
        v.flags.append("Job description looks truncated — fetch/paste the full JD before tailoring")

    v.passed = not v.reasons
    work = workcountry.for_job({"location": loc, "description": jd, "title": title, "work_country": job.get("work_country"),
                                "work_evidence": job.get("work_evidence")}, workcountry.applicant_visa(settings))
    v.work_country = work.country
    v.work_evidence = work.evidence if work.country else work.reason
    v.work_location = work.shown if work.home_remote else None
    v.needs_country = v.passed and not v.needs_full_jd and work.country is None
    return v
