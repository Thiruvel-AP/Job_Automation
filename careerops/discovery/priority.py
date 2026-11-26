"""Priority = what to look at first. Fit weighted by freshness; sponsorship is a weight on the order, never a filter.

  order  =  P x W, highest first      (ties: the surer employer S, then the id)       v3.5.5: no longer tier first

  P = F x S x L x R, below. W is the freshness weight of the job's tier, `priority.freshness_weight` in settings.yaml (t0 1.00, t1 1.00, t2 0.95, t3 0.85 by
  default): a week-old job with a much better fit now beats a day-old one with a poor fit (before, a tier-0 job always came first, whatever its P), while a
  tier-3 job still has to be about 18% better than a tier-0 one to go ahead of it. The tier is still shown, and still what `--fresh` selects on.

  tier  0 posted at most 24 h ago, 1 at most 72 h, 2 at most 7 days, 3 older or undated (discovery/freshness.py). The posting time is the
        source's own (Greenhouse first_published, Lever createdAt, Ashby publishedAt, Reed date, Adzuna created; a Gmail alert's arrival
        time, an upper bound); anything else counts from the day discover first saw the job. Nothing is dropped for being old.
  L  level fit, from the title words (the ATS score cannot see seniority), `priority.level_factor` in settings.yaml:
       manager|director|head 0.3 · lead|principal|staff|architect 0.4 · senior|sr 0.6 · anything else 1.0 (the highest level word wins)
  R  role fit, from the family the title matches (roles.families, matched in list order) and the user's ranking of it:
       R = max(0.5, 1 - priority.role_step x (priority - 1)); 1.00 for priority 1, 0.88 for 5, 0.58 for 15 with the step 0.03.
       A title that matches no family is given no R (it counts as 1). The ranking never decides which family a title belongs to.
  F  the honest ATS ceiling, 0-1 (the LLM-checked ceiling once tailored, else the triage-time one; 0 when not known yet)
  S  how sure the employer is to sponsor, from `priority.sponsor_weights` in settings.yaml:
       exact    on the sponsor register by name                                  1.00
       fuzzy    on the register by a near match                                  0.90
       unknown  not on the register, a recruitment agency, or no register yet    0.75
       no_sponsorship   the JD itself says it does not sponsor / needs the right to work without sponsorship   0.65
       non_home   the work is not where you live (W is not `visa.home`: a restriction to another country or region, an office abroad,
                  a job listed under another country)                                                                      0.50
     When several apply the lowest one is used. A holder of the UK Graduate visa can take any of these jobs, so none of them
     removes a job; they only move it down.

There is no recency factor in P: the old R = exp(-days / 14) pushed a 64-day-old posting that was still open down to 0.01, and the
tiers do the same job without hiding anything. It orders the `live` inbox, `discover`'s listing and `status`; the review screen shows P,
its two factors and the posting age in hours. Nothing here skips, drops or hides a job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from . import countries, freshness, workcountry

DEFAULT_WEIGHTS = {"exact": 1.00, "fuzzy": 0.90, "unknown": 0.75, "no_sponsorship": 0.65, "non_home": 0.50}
DEFAULT_LEVELS = {"manager": 0.3, "lead": 0.4, "senior": 0.6, "other": 1.0}
DEFAULT_FRESHNESS = {0: 1.00, 1: 1.00, 2: 0.95, 3: 0.85}     # W by freshness tier: T1 1.00, T2 0.95, T3 0.85 (11 Nov 2026); T0, fresher still, is not below T1
DEFAULT_ROLE_STEP = 0.03
ROLE_FLOOR = 0.5
# Title words per level, highest level first: "Senior Manager" is a manager, "Manager, Applied AI Architects" too. Whole words only.
LEVEL_WORDS = [("manager", re.compile(r"\b(?:manager|director|head|chief|vp)\b", re.I)),            # v3.1: chief and vp are levels too
               ("lead", re.compile(r"\b(?:lead|principal|staff|architect)\b", re.I)),
               ("senior", re.compile(r"\b(?:senior|sr)\b", re.I))]

# ------------------------------------------------------------------ what the JD says about sponsorship (an info line, not a flag)
_NEG = (r"(?:do(?:es)? not|don[’']t|doesn[’']t|cannot|can[’']t|can not|unable to|not able to|will not|won[’']t|"
        r"not in a position to|not willing to|(?:are|is) not (?:able|willing|currently able) to|not currently able to)")
WORDING: list[tuple[str, str]] = [
    # (regex, kind). kind: no_sponsorship | right_to_work | sponsorship_offered. Tried in this order; the first kind that has a hit per
    # sentence is the sentence's kind.
    (r"\bwithout (?:the need for |requiring |needing |any )?(?:visa |employer |company )?sponsorship", "no_sponsorship"),
    (rf"{_NEG}\s+(?:currently\s+)?sponsor(?:ing)?\b", "no_sponsorship"),
    (rf"{_NEG}\s+(?:currently\s+)?(?:offer|provid|support|consider)\w*\s+(?:\w+\s+){{0,3}}?sponsorship", "no_sponsorship"),
    (r"\bno\s+(?:visa\s+|uk\s+visa\s+)?sponsorship\b", "no_sponsorship"),
    (r"\bsponsorship\s+(?:is\s+|will\s+be\s+)?(?:not|unavailable)\b", "no_sponsorship"),
    (r"\b(?:not|n[’']t)\s+(?:need|require)\s+(?:\w+\s+){0,2}?sponsorship", "no_sponsorship"),
    (r"\b(?:must|should)\s+not\s+require\s+(?:\w+\s+){0,2}?sponsorship", "no_sponsorship"),
    (r"\b(?:must|need to)\s+(?:already\s+)?(?:have|hold|possess)\s+(?:the\s+)?(?:full\s+|existing\s+|unrestricted\s+)?right to work\s+"
     r"in the (?:uk|united kingdom)", "right_to_work"),
    (r"\blicen[cs]ed sponsor\b", "sponsorship_offered"),
    (r"\b(?:visa\s+)?sponsorship\s+(?:is\s+|may\s+be\s+|can\s+be\s+)?(?:available|offered|provided|possible)\b", "sponsorship_offered"),
    (r"\b(?:who|that)\s+(?:need|require)s?\s+(?:visa\s+)?sponsorship\b", "sponsorship_offered"),
    (r"\b(?:can|will|able to|happy to|willing to)\s+(?:\w+\s+){0,2}?sponsor\b", "sponsorship_offered"),
]
_COMPILED = [(re.compile(p, re.I), kind) for p, kind in WORDING]
SNIPPET_CHARS = 200


@dataclass
class Wording:
    kind: str          # no_sponsorship | right_to_work | sponsorship_offered
    text: str          # the sentence the JD says it in, one line, cut to SNIPPET_CHARS


def _sentences(jd: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", jd or "") if s.strip()]


def sponsor_wording(jd: str) -> list[Wording]:
    """Every sentence of the JD that speaks of sponsorship or the right to work, with what it says, in the order of the text
    (at most one entry per sentence; the first matching pattern in WORDING decides its kind)."""
    out: list[Wording] = []
    for sentence in _sentences(jd):
        for rx, kind in _COMPILED:
            if rx.search(sentence):
                one_line = " ".join(sentence.split())
                out.append(Wording(kind, one_line if len(one_line) <= SNIPPET_CHARS else one_line[: SNIPPET_CHARS - 1] + "…"))
                break
    return out


def says_no_sponsorship(jd: str) -> bool:
    return any(w.kind == "no_sponsorship" for w in sponsor_wording(jd))


def wording_line(jd: str) -> str | None:
    """One info line for the review screen, or None when the JD says nothing about sponsorship."""
    found = sponsor_wording(jd)
    if not found:
        return None
    label = {"no_sponsorship": "says no sponsorship", "right_to_work": "asks for the right to work",
             "sponsorship_offered": "offers or welcomes sponsorship"}
    return "Sponsorship wording in the JD (information only): " + " | ".join(f"\"{w.text}\" ({label[w.kind]})" for w in found[:3])


# ------------------------------------------------------------------ the score
@dataclass
class Priority:
    p: float               # F x S x L
    f: float
    s: float
    s_reason: str          # why S is what it is
    hours: float | None    # hours since the job was posted (freshness.py), None when unknown
    tier: int              # 0 <= 24 h, 1 <= 72 h, 2 <= 7 d, 3 older or undated: the first key of the order
    posted_via: str = ""   # where the posting time comes from (freshness.SOURCES), "" when unknown
    l: float = 1.0         # level fit from the title words (level_factor); 1.0 for a title without any
    level: str = "other"   # manager | lead | senior | other
    r: float | None = None  # role fit from the family's priority (role_factor); None = no family matched, no R
    family: str | None = None
    rank: int | None = None  # the family's priority (1-15)
    w: float = 1.0         # freshness weight of the tier (freshness_weights)
    f_known: bool = True   # False = F is the triage-time estimate of an ad too thin to read (ceiling_estimate.Estimate.reliable): it is neither skipped on F nor read as zero

    @property
    def order(self) -> float:
        """The number the order is made on: P x W."""
        return self.p * self.w

    def order_text(self) -> str:
        """'x W 0.95 (T2) = order 0.60' for the inbox listing and the final table."""
        return f"x W {self.w:.2f} (T{self.tier}) = order {self.order:.2f}"

    def role_text(self) -> str:
        """'x R 0.88 (forward_deployed_engineer, #5)' for the inbox, the final table and the review screen; '' without an R."""
        return f"x R {self.r:.2f} ({self.family}, #{self.rank})" if self.r is not None else ""

    def flag(self) -> str:
        """'LEVEL: manager' (and so on) for the review screen and the final table; '' for a title without a level word."""
        return f"LEVEL: {self.level}" if self.level != "other" else ""

    def age(self) -> str:
        return "date unknown" if self.hours is None else freshness.age_text(self.hours)

    def line(self) -> str:
        return (f"Priority P={self.p:.2f} = F {self.f:.2f} (ATS ceiling) x S {self.s:.2f} ({self.s_reason}) x L {self.l:.2f} ({self.level})"
                + (f" {self.role_text()}" if self.r is not None else "") + " | "
                f"age {self.age()} (tier {self.tier}, {freshness.TIER_NAMES[self.tier]}"
                + (f", {self.posted_via}" if self.posted_via else "") + f") | order {self.order:.2f} = P x W {self.w:.2f}")


def weights(settings) -> dict[str, float]:
    given = ((settings.raw.get("priority") or {}).get("sponsor_weights") or {}) if settings is not None else {}
    return {k: float(given.get(k, given.get("non_uk", v) if k == "non_home" else v)) for k, v in DEFAULT_WEIGHTS.items()}   # non_uk: its old name


def freshness_weights(settings=None) -> dict[int, float]:
    """{tier: W} from `priority.freshness_weight` (keys t0 to t3); a key that is missing uses DEFAULT_FRESHNESS."""
    given = ((settings.raw.get("priority") or {}).get("freshness_weight") or {}) if settings is not None else {}
    return {t: float(given.get(f"t{t}", v)) for t, v in DEFAULT_FRESHNESS.items()}


def level_of(title: str) -> str:
    """manager | lead | senior | other, from whole words in the title; the highest level that has a word wins."""
    for level, rx in LEVEL_WORDS:
        if rx.search(title or ""):
            return level
    return "other"


def level_factor(title: str, settings=None) -> tuple[float, str]:
    """(L, level) for a title. The values are `priority.level_factor` in settings.yaml; one that is missing uses its default."""
    given = ((settings.raw.get("priority") or {}).get("level_factor") or {}) if settings is not None else {}
    level = level_of(title)
    return float(given.get(level, DEFAULT_LEVELS[level])), level


@dataclass
class Role:
    family: str
    rank: int
    r: float


def role_factor(title: str, settings=None) -> Role | None:
    """R for a title: the first family (list order) its title matches and that has a `priority`, else None. The step is
    `priority.role_step` in settings.yaml. Needs settings: without them no family is known."""
    if settings is None:
        return None
    from . import gates     # lazy: gates imports the tailoring package

    fam = gates.match_family(str(title or ""), settings)
    if fam is None or fam.priority is None:
        return None
    step = float(((settings.raw.get("priority") or {}).get("role_step", DEFAULT_ROLE_STEP)))
    return Role(fam.name, fam.priority, max(ROLE_FLOOR, 1 - step * (fam.priority - 1)))


def _get(job: Mapping[str, Any], key: str) -> Any:
    return job[key] if key in job.keys() else None            # dicts and sqlite3.Row


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sponsor_factor(job: Mapping[str, Any], settings=None) -> tuple[float, str]:
    """(S, why). The lowest of the applicable weights; every weight is above zero, so a job is only ever moved down."""
    w = weights(settings)
    options: list[tuple[float, str]] = []
    match = str(_get(job, "sponsor_match") or "")
    if match.startswith("exact"):
        options.append((w["exact"], "exact register match"))
    elif match.startswith("fuzzy"):
        options.append((w["fuzzy"], "fuzzy register match"))
    elif match == "alias":
        options.append((w["fuzzy"], "register match through a known alias"))
    elif match == "none":
        options.append((w["unknown"], "not on the register"))
    else:
        options.append((w["unknown"], "register not checked"))
    if _get(job, "agency"):
        options.append((w["unknown"], "agency"))
    if says_no_sponsorship(str(_get(job, "description") or "")):
        options.append((w["no_sponsorship"], "JD says no sponsorship"))
    # W (where the work is done) when it is known, else the country an ad was listed under; home is visa.home (GB when the file has none)
    home = (workcountry.rules_from(workcountry.applicant_visa(settings)).home if settings is not None else "") or "GB"
    place = countries.place_code(str(_get(job, "work_country") or "")) or countries.code(str(_get(job, "country") or ""))
    if place and place != home:
        if _get(job, "region_target"):            # a region outside the UK was chosen: the UK register cannot say, so S = unknown (discovery/region.py)
            options.append((w["unknown"], f"non-UK region: visa not checked ({countries.place_name(place)})"))
        else:
            options.append((w["non_home"], f"work location {countries.place_name(place)} is not home"))
    return min(options, key=lambda o: o[0])


def ats_factor(job: Mapping[str, Any], tailored: bool = True) -> float:
    c = _get(job, "ceiling") if tailored else None
    c = c if c is not None else _get(job, "ceiling_pre")
    return min(max(float(c), 0.0), 1.0) if c is not None else 0.0


def priority(job: Mapping[str, Any], settings=None, tailored: bool = True, today: datetime | None = None) -> Priority:
    """P = F x S x L x R for a job row (or dict with the same keys), with its age in hours and its freshness tier (freshness.py).
    R is left out (counts as 1) for a title that matches no role family."""
    f = ats_factor(job, tailored)
    s, why = sponsor_factor(job, settings)
    l, level = level_factor(str(_get(job, "title") or ""), settings)
    role = role_factor(str(_get(job, "title") or ""), settings)
    when, via = freshness.posted(job)
    hours = freshness.age_hours(job, today)
    tier = freshness.tier_of(hours)
    return Priority(f * s * l * (role.r if role else 1.0), f, s, why, hours, tier,
                    freshness.SOURCES.get(via, "first seen" if via else ""), l, level,
                    role.r if role else None, role.family if role else None, role.rank if role else None,
                    freshness_weights(settings)[tier], _get(job, "ceiling_known") is not False)


def rank_key(pr: Priority, tie: str = "") -> tuple:
    """Ascending key for a Priority: highest P x W first, ties to the surer employer S (so jobs with no ceiling yet still order by how sure they are), then to `tie`
    (the id or the file name) for a stable order."""
    return (-round(pr.order, 9), -pr.s, tie)


def sort_key(job: Mapping[str, Any], settings=None, tailored: bool = True, today: datetime | None = None) -> tuple:
    """Ascending key: highest P x W first (rank_key)."""
    return rank_key(priority(job, settings, tailored, today), str(_get(job, "id") or ""))


# ------------------------------------------------------------------ jobs that are not in the database yet
def file_job(path, settings, fb: dict, idx) -> dict | None:
    """A job-like dict for an inbox .txt file (its Company / Title / Posted / Agency headers and its text), with the register
    match and the triage-time ceiling filled in, so that `priority()` can rank it. None when the file cannot be read as a JD."""
    from . import gates, manual     # lazy: both import modules that import this one's neighbours

    try:
        text, _ = manual.read_jd(path)
    except manual.JDError:
        return None
    hdr = manual.header_fields(text)
    match = idx.lookup(hdr.get("company", ""), settings["sponsors"].get("fuzzy_threshold", 0.92)).label if idx is not None and hdr.get("company") else None
    try:
        est = gates.pre_estimate(text, fb, settings)
        ceiling_pre, known = gates.pre_ceiling(text, fb, settings), est.reliable      # pre_ceiling is the number; the estimate says whether an ad had terms to read
    except Exception:  # noqa: BLE001 — a file the taxonomy cannot read is ranked last, not refused
        ceiling_pre, known = None, False
    work = workcountry.resolve(location=hdr.get("location", ""), description=text, title=hdr.get("title", ""),
                               override=hdr.get("country", ""), rules=workcountry.rules_from(workcountry.applicant_visa(settings)))
    try:
        written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")   # no First seen: header: when the file came
    except OSError:
        written = None
    return {"id": str(path.name), "company": hdr.get("company", ""), "title": hdr.get("title", ""), "description": text,
            "work_country": work.country, "seen_on": hdr.get("first_seen"), "first_seen": written,
            "open_listing": (hdr.get("listed") or "").lower() in ("yes", "true", "1"),
            "sponsor_match": match, "agency": (hdr.get("agency") or "").lower() in ("yes", "true", "1"),
            "posted_at": hdr.get("posted"), "posted_at_source": hdr.get("posted_source"), "ceiling_pre": ceiling_pre, "ceiling_known": known,
            "country": hdr.get("country")}


def order_files(files, settings, db, fb: dict, today: datetime | None = None) -> list[tuple[Any, Priority | None]]:
    """[(path, Priority or None)] best first: highest P x W, ties to the surer S, then by file name. A file that
    cannot be read as a JD has no priority and goes last (live then rejects it with its reason). Nothing is dropped."""
    from . import register as sponsors

    idx = sponsors.load_index(db)
    ranked = []
    for f in files:
        job = file_job(f, settings, fb, idx)
        ranked.append((f, priority(job, settings, tailored=False, today=today) if job else None))
    return sorted(ranked, key=lambda fp: (fp[1] is None, *(rank_key(fp[1], fp[0].name) if fp[1] else (0, 0, fp[0].name))))
