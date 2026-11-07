"""Stage 1 of the agent's decision (v3.2): the career-ops quick score, built from our zero-token triage factors. No model, no network.

    global = archetype x 0.35 + comp x 0.25 + location x 0.25 + cv_match x 0.15 - 0.5 per soft red flag      rounded to 0.1
    a hard disqualifier caps it at 2.5

    archetype   A of triage.py (the role family's priority)
    comp        P of triage.py (the top of the ad's band against your floor: 5 / no band 3 / below 1)
    location    5 onsite or hybrid within commuting distance of Bristol or London (COMMUTE_PLACES, or agent.commute_places) · 4 remote in the UK
                · 3 anywhere else in the UK, or the place is not known · 1 the work is outside your home country (3 when you chose a region abroad)
    cv_match    E of triage.py (fact-bank coverage of the ad's required keywords); an alert with no ad text ("E ?") counts as 3

Soft red flags (-0.5 each): the level is senior (when roles.levels.exclude does not exclude it) · the posting is older than 60 days · `evergreen?` or
`possible repost` · the company is not on the sponsor register · more than 3 required skills the fact bank lacks. (v3.2.1: "no salary given" is no flag:
an unknown pay already makes comp 3, so it counted twice.)
Hard disqualifiers (cap 2.5): a fee-charging training scheme · an excluded level (roles.levels.exclude and gates.ALWAYS_EXCLUDED; "Sr." / "Snr" are read
as "Senior", gates.canonical_level) · the ad says no sponsorship and the work is outside the UK · a blacklisted company · a start date the ad states more
than 9 months away · the top of the stated band below your floor · v3.2.1: security clearance or a UK-residence period ("SC clearance", "DV clearance",
"Security Check (SC) ... clearance", "eligible for security clearance", "resided in the UK for the last 5 years", "BPSS" with "5 years"), reason
`security clearance / residency`. Each one is a fact an ad SENTENCE (or your own list) states, quoted; a sentence that denies it or calls it optional is
no hit (dealbreakers.py's reading); nothing is inferred.

Verdicts (career-ops modes/triage.md): PASS at or above agent.triage_threshold (default 3.5) · MARGINAL from 3.0 · FAIL below 3.0. A company listed in
targeting.priority_companies PASSES whatever its score (the list is read, never written).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from . import blacklist, dealbreakers, gates, priority
from . import triage as triage_mod
from .candidate import family_key

WEIGHTS = {"archetype": 0.35, "comp": 0.25, "location": 0.25, "cv_match": 0.15}
RED_FLAG = 0.5
HARD_CAP = 2.5
PASS_DEFAULT = 3.5                       # agent.triage_threshold when settings say nothing (career-ops pipeline.triage_threshold)
MARGINAL_FROM = 3.0
OLD_DAYS = 60                            # a posting older than this is a soft red flag
MAX_LACK = 3                             # more required skills missing than this is a soft red flag
START_MONTHS = 9                         # a stated start date further away than this is a hard disqualifier
PASS, MARGINAL, FAIL = "PASS", "MARGINAL", "FAIL"

# Places within commuting distance of the two cities you would work from (onsite or hybrid). agent.commute_places replaces the list.
COMMUTE_PLACES = (
    "london", "city of london", "canary wharf", "croydon", "stratford", "wembley", "harrow", "uxbridge", "brentford", "richmond", "kingston",
    "sutton", "bromley", "romford", "reading", "slough", "watford", "guildford", "woking", "st albans", "stevenage", "luton", "chelmsford",
    "staines", "bracknell", "maidenhead", "hemel hempstead", "crawley", "epsom", "weybridge", "leatherhead", "high wycombe", "dartford",
    "bristol", "bath", "filton", "aztec west", "almondsbury", "portishead", "clevedon", "keynsham", "yate", "thornbury", "weston-super-mare",
    "newport", "cardiff", "chippenham", "swindon", "gloucester", "cheltenham",
)
REMOTE = re.compile(r"\b(?:remote|work from home|wfh|home[- ]based)\b", re.I)
HYBRID_ONSITE = re.compile(r"\b(?:hybrid|on[- ]?site|in[- ]office|office[- ]based)\b", re.I)

_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                                        "november", "december"), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MON = r"(?P<m>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
CLEARANCE_LABEL = "security clearance / residency"
CLEARANCE = (
    re.compile(r"\b(?:SC|DV)\s+(?:level\s+)?(?:security\s+)?clear(?:ance|ed)\b", re.I),
    re.compile(r"\b(?:security\s+check|developed\s+vetting)\s*(?:\(\s*(?:SC|DV)\s*\)\s*)?(?:level\s+)?(?:security\s+)?clear(?:ance|ed)\b", re.I),
    re.compile(r"\beligible\s+(?:for|to\s+(?:obtain|gain|hold))\s+(?:(?:SC|DV|UK)\s+)?(?:security\s+)?clearance\b", re.I),
    re.compile(r"\b(?:resided|resident|lived|living|residing|residence|residency)\b[^.]{0,40}?\bin\s+the\s+(?:UK|U\.K\.|United\s+Kingdom)\b[^.]{0,50}?"
               r"\b(?:\d{1,2}|three|five|ten)\s+years\b", re.I),
    re.compile(r"\bBPSS\b[^.]{0,80}?\b(?:5|five)\s+years\b|\b(?:5|five)\s+years\b[^.]{0,80}?\bBPSS\b", re.I),
)
START = (re.compile(r"\bstart(?:ing|s)?(?:\s+date)?\s*(?:[:\-–]|is|in|from|of|on)?\s*(?:early|mid|late)?\s*(?:-\s*)?" + _MON + r"\.?\s+(?P<y>20\d\d)\b", re.I),
         re.compile(r"\b" + _MON + r"\.?\s+(?P<y>20\d\d)\s+(?:start|intake|cohort)\b", re.I),
         re.compile(r"\bcommenc\w*\s+(?:in|on|from)?\s*" + _MON + r"\.?\s+(?P<y>20\d\d)\b", re.I))


@dataclass
class Quick:
    archetype: float
    comp: float
    location: float
    cv_match: float
    flags: list[str] = field(default_factory=list)          # soft red flags, -0.5 each
    hard: list[str] = field(default_factory=list)           # hard disqualifiers (each caps the score at 2.5)
    score: float = 0.0
    verdict: str = FAIL
    priority: bool = False                                  # on targeting.priority_companies: PASS whatever the score
    location_why: str = ""

    def text(self) -> str:
        """`4.3 PASS = arch5 comp3 loc5 cv3.8 (-0.5: no salary)`"""
        f = triage_mod._fmt
        tail = (f" (-{RED_FLAG * len(self.flags):g}: {'; '.join(self.flags)})" if self.flags else "") + \
               (f" [hard: {'; '.join(self.hard)}]" if self.hard else "") + (" [priority company]" if self.priority else "")
        return f"{f(self.score)} {self.verdict} = arch{f(self.archetype)} comp{f(self.comp)} loc{f(self.location)} cv{f(self.cv_match)}{tail}"


def round1(x: float) -> float:
    """Half up to one decimal (career-ops: nearest 0.1); 4.25 -> 4.3, never banker's rounding."""
    return math.floor(x * 10 + 0.5 + 1e-9) / 10


def formula(archetype: float, comp: float, location: float, cv_match: float, flags: int = 0, hard: bool = False) -> float:
    """The career-ops quick score: the weighted sum minus 0.5 a soft red flag, rounded to 0.1, capped at 2.5 when a hard disqualifier applies."""
    raw = (WEIGHTS["archetype"] * archetype + WEIGHTS["comp"] * comp + WEIGHTS["location"] * location + WEIGHTS["cv_match"] * cv_match
           - RED_FLAG * flags)
    s = round1(max(raw, 0.0))
    return min(s, HARD_CAP) if hard else s


def _agent(settings) -> dict:
    raw = (getattr(settings, "raw", None) or {}).get("agent") or {}
    return raw if isinstance(raw, dict) else {}


def threshold(settings) -> float:
    v = _agent(settings).get("triage_threshold", PASS_DEFAULT)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 5 else PASS_DEFAULT


def verdict(score: float, settings) -> str:
    return PASS if score >= threshold(settings) else MARGINAL if score >= MARGINAL_FROM else FAIL


def priority_companies(settings) -> list[str]:
    """targeting.priority_companies (read only; [] when absent)."""
    raw = ((getattr(settings, "raw", None) or {}).get("targeting") or {}).get("priority_companies") or []
    return [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []


def is_priority(company: str, settings) -> bool:
    key = family_key(company or "")
    return bool(key) and any(family_key(c) == key for c in priority_companies(settings))


def commute_places(settings) -> tuple[str, ...]:
    given = _agent(settings).get("commute_places")
    return tuple(str(x).lower().strip() for x in given if str(x).strip()) if isinstance(given, list) and given else COMMUTE_PLACES


def location_score(job: Mapping[str, Any], settings) -> tuple[float, str]:
    loc = str(job.get("location") or "")
    if triage_mod.outside_home(job, settings):
        return (3.0, "a region abroad you chose") if job.get("region_target") else (1.0, f"outside your home country ({loc or 'work country'})")
    low = loc.lower()
    near = next((p for p in commute_places(settings) if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low)), None)
    remote, office = bool(REMOTE.search(loc)), bool(HYBRID_ONSITE.search(loc))
    if near and (office or not remote):
        return 5.0, f"{'hybrid/onsite ' if office else ''}within commuting distance ({near})"
    if remote:
        return 4.0, "remote in the UK"
    return 3.0, f"elsewhere in the UK ({loc})" if loc else "location not stated"


def start_months_away(jd: str, now: datetime) -> tuple[float, str] | None:
    """(months from now, the ad's words) of a start date the ad states ("start date: September 2027", "a June 2027 intake"); None when it states none."""
    best = None
    for rx in START:
        for m in rx.finditer(jd or ""):
            month = _MONTHS.get(m.group("m").lower().rstrip("."))
            if not month:
                continue
            away = (int(m.group("y")) - now.year) * 12 + (month - now.month)
            if best is None or away > best[0]:
                best = (float(away), " ".join(m.group(0).split()))
    return best


def clearance_sentence(jd: str) -> str | None:
    """The first ad sentence that STATES a security clearance or a UK-residence period (CLEARANCE), else None. A sentence with a negation before the words,
    or one that calls them optional ("SC clearance desirable"), states nothing (dealbreakers._states)."""
    for s in dealbreakers.sentences(jd):
        if any(dealbreakers._states(rx, s) for rx in CLEARANCE):
            return s
    return None


def hard_disqualifiers(job: Mapping[str, Any], t: triage_mod.Triage, settings, *, now: datetime) -> list[str]:
    """Every hard disqualifier the ad (or your own blacklist) states. Each line names the fact."""
    company, title, jd = str(job.get("company") or ""), str(job.get("title") or ""), str(job.get("description") or "")
    out: list[str] = []
    if (scheme := blacklist.training_scheme(company, jd)):
        out.append(f"training scheme (fees): {scheme}")
    if (lvl := gates.level_excluded(title, settings)):
        out.append(f"level excluded ({lvl})")
    if priority.says_no_sponsorship(jd) and triage_mod.outside_home(job, settings):
        out.append("the ad says no sponsorship and the work is outside the UK")
    try:
        hit = blacklist.load(blacklist.path_for(settings)).hit(company, str(job.get("url") or ""), str(job.get("apply_url") or ""))
    except blacklist.BlacklistError:
        hit = None
    if hit is not None:
        out.append(f"{company} is on your blacklist" + (f": {hit.reason}" if hit.reason else ""))
    if (start := start_months_away(jd, now)) is not None and start[0] > START_MONTHS:
        out.append(f"start date {start[0]:.0f} months away (\"{start[1]}\")")
    if t.pay_top is not None and t.floor is not None and t.pay_top < t.floor:
        out.append(f"pay below your floor (top £{t.pay_top:,} < £{t.floor:,})")
    if (s := clearance_sentence(jd)):
        out.append(f'{CLEARANCE_LABEL} ("{s}")')
    return out


def soft_flags(job: Mapping[str, Any], t: triage_mod.Triage, *, repost_flags: Sequence[str] = ()) -> list[str]:
    title = str(job.get("title") or "")
    out: list[str] = []
    if priority.level_of(title) == "senior":
        out.append("senior level")
    if t.age_days is not None and t.age_days > OLD_DAYS:
        out.append(f"posted {t.age_days:.0f} days ago")
    if any(f.startswith(("evergreen?", "possible repost")) for f in repost_flags):
        out.append(next(f for f in repost_flags if f.startswith(("evergreen?", "possible repost"))))
    if str(job.get("sponsor_match") or "") == "none":
        out.append("not on the sponsor register")
    if len(t.lack) > MAX_LACK:
        out.append(f"{len(t.lack)} required skills the fact bank lacks")
    return out


def quick(job: Mapping[str, Any], t: triage_mod.Triage, settings, *, now: datetime, repost_flags: Sequence[str] = (),
          e_unknown: bool = False) -> Quick:
    """The quick score of one ad from its triage factors (`t`, triage.score). `e_unknown`: an alert with no ad text, cv_match counts as 3."""
    loc, why = location_score(job, settings)
    cv = triage_mod.NO_REQUIRED if e_unknown else t.e
    q = Quick(t.a, t.p, loc, cv, soft_flags(job, t, repost_flags=repost_flags), hard_disqualifiers(job, t, settings, now=now), location_why=why)
    q.score = formula(q.archetype, q.comp, q.location, q.cv_match, len(q.flags), bool(q.hard))
    q.priority = is_priority(str(job.get("company") or ""), settings)
    q.verdict = PASS if q.priority and not q.hard else verdict(q.score, settings)
    return q
