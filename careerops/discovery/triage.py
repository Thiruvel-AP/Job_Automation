"""The zero-token triage score T (1-5) of an inbox ad, and the hard disqualifiers that come before it. No model, no network (liveness is passed in).

    T = 0.30 A + 0.25 E + 0.15 V + 0.10 L + 0.10 Fr + 0.10 P          printed as  `T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3`

    A  role           the family's priority: 1-3 -> 5, 4-7 -> 4, 8-11 -> 3, 12-15 -> 2, no family -> 1
    E  evidence       1 + 4c, c = the share of the ad's REQUIRED keywords (the taxonomy scan the pre-tailoring ceiling uses: those under its Requirements
                      heading, or, in an ad without one, every keyword outside a "nice to have") that appear anywhere in the fact
                      bank, found as the scorer finds them (literal or stemmed, the term or the ad's own alternatives; keyword-group synonyms do not count).
                      A required keyword the bank lacks stays in the denominator, so repeating a term in the ad cannot raise E. No required keyword: 3.
    V  visa           exact register match 5 · fuzzy 4.5 · not on the register or unchecked 3.5 (an agency counts as unchecked) · the ad says no sponsorship 1.5
                      · the work is not in your home country 1; the lowest that applies
    L  level          no level word 5 · senior 2 · lead or manager 1
    Fr freshness      at most 3 days old 5 · 7 days 4 · 30 days 3 · 120 days 2 · older 1 · no date known 3
    P  pay            the top of the ad's salary band at or above the floor in answers.yaml 5 · no band, or no floor on file 3 · the top below the floor 1

E replaces the pre-tailoring "estimate F" in this role: it counts only terms the fact bank really has, so the ad alone cannot inflate it. Information and
ordering only: T decides what `careerops shortlist` OFFERS; a human still approves.

`disqualify` runs the hard stops in order: liveness (when a check is passed in), the blacklist, the same role under another channel or an agency ad that hides
the employer, the level exclude list, then your dealbreakers (each needs a quoted ad sentence, dealbreakers.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..apply import answers as answers_mod
from ..tailoring.scorer import Keyword, _contains, has_literal, norm_literal, norm_stem, taxonomy_keywords
from . import blacklist, channel, countries, dealbreakers, freshness, gates, manual, priority, workcountry
from .. import targeting as targeting_mod

WEIGHTS = {"A": 0.30, "E": 0.25, "V": 0.15, "L": 0.10, "Fr": 0.10, "P": 0.10}
RANK_SCALE = ((3, 5.0), (7, 4.0), (11, 3.0), (15, 2.0))                    # (highest priority number, A)
LEVEL_SCALE = {"other": 5.0, "senior": 2.0, "lead": 1.0, "manager": 1.0}
FRESH_SCALE = ((3, 5.0), (7, 4.0), (30, 3.0), (120, 2.0))                  # (days, Fr); older: 1
VISA = {"exact": 5.0, "fuzzy": 4.5, "unknown": 3.5, "no_sponsorship": 1.5, "non_home": 1.0}
NO_REQUIRED, NO_DATE, NO_BAND = 3.0, 3.0, 3.0
MIN_SALARY, MAX_SALARY = 15_000, 1_000_000                                   # an annual figure: less is a day rate or a bonus, more is not a salary
MONEY = re.compile(r"(?:£|gbp\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?(k)?", re.I)
BARE_MONEY = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,7})(?:\.\d+)?\s?(k)?|(\d{2,3})\s?k\b", re.I)
NOT_ANNUAL = re.compile(r"\s*(?:/|per\s+|a\s+|p\.?\s?)\s*(?:day|hour|hr|week|month|diem)|\s*(?:daily|hourly)", re.I)


def _fmt(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


@dataclass
class Triage:
    a: float
    e: float
    v: float
    l: float
    fr: float
    p: float
    t: float
    have: list[str] = field(default_factory=list)        # the ad's required keywords the fact bank has, heaviest first
    lack: list[str] = field(default_factory=list)        # ... and those it lacks
    visa_why: str = ""
    age_days: float | None = None
    pay_top: int | None = None
    floor: int | None = None

    def text(self) -> str:
        """`T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3`"""
        return (f"T {_fmt(self.t)} = A{_fmt(self.a)} E{_fmt(self.e)} V{_fmt(self.v)} L{_fmt(self.l)} Fr{_fmt(self.fr)} P{_fmt(self.p)}")

    def factors(self) -> dict[str, float]:
        return {"A": self.a, "E": self.e, "V": self.v, "L": self.l, "Fr": self.fr, "P": self.p}


def _get(job: Mapping[str, Any], key: str) -> Any:
    try:
        return job[key]
    except (KeyError, IndexError, TypeError):
        return None


# ------------------------------------------------------------------ the factors
def role_score(title: str, settings) -> float:
    role = priority.role_factor(title, settings)
    if role is None:
        return 1.0
    return next((s for top, s in RANK_SCALE if role.rank <= top), 2.0 if role.rank <= 15 else 1.0)


def evidence(jd: str, fb: Mapping[str, Any], settings) -> tuple[float, list[str], list[str]]:
    """(E, the required keywords the fact bank has, those it lacks)."""
    from ..tailoring.tailor import fact_text           # lazy: tailor imports the discovery package

    sc = settings["scoring"]
    found = taxonomy_keywords(jd or "", sc["required_weight"], sc["preferred_weight"])
    # the keywords under the ad's own requirements heading; an ad without one is read as the scorer reads it (a keyword outside a "nice to have" is required)
    required = [k for k in found if k.weight >= sc["required_weight"]] or [k for k in found if k.required]
    if not required:
        return NO_REQUIRED, [], []
    text = fact_text(dict(fb))
    lit, stem = norm_literal(text), norm_stem(text)
    have, lack = [], []
    for k in sorted(required, key=lambda k: -k.weight):                  # a stable sort: the ad's order within a weight
        found = any(has_literal(lit, norm_literal(w)) or _contains(stem, norm_stem(w)) for w in k.members())
        (have if found else lack).append(k.term)
    return 1 + 4 * len(have) / len(required), have, lack


def visa_score(job: Mapping[str, Any], settings) -> tuple[float, str]:
    options: list[tuple[float, str]] = []
    match = str(_get(job, "sponsor_match") or "")
    options.append((VISA["exact"], "exact register match") if match.startswith("exact") else
                   (VISA["fuzzy"], "fuzzy register match") if match.startswith("fuzzy") else
                   (VISA["unknown"], "not on the register" if match == "none" else "register not checked"))
    if _get(job, "agency"):
        options.append((VISA["unknown"], "agency"))
    if priority.says_no_sponsorship(str(_get(job, "description") or "")):
        options.append((VISA["no_sponsorship"], "ad says no sponsorship"))
    if outside_home(job, settings):
        place = countries.place_code(str(_get(job, "work_country") or "")) or countries.code(str(_get(job, "country") or ""))
        if _get(job, "region_target"):            # you chose a region outside the UK: the sponsor register is UK-only, so it is "unknown", not "not home"
            options.append((VISA["unknown"], f"non-UK region: visa not checked ({countries.place_name(place)})"))
        else:
            options.append((VISA["non_home"], f"work location {countries.place_name(place)} is not home"))
    return min(options, key=lambda o: o[0])


def outside_home(job: Mapping[str, Any], settings) -> bool:
    """Is the work done outside your home country (the same reading the visa factor uses)?"""
    home = (workcountry.rules_from(workcountry.applicant_visa(settings)).home if settings is not None else "") or "GB"
    place = countries.place_code(str(_get(job, "work_country") or "")) or countries.code(str(_get(job, "country") or ""))
    return bool(place) and place != home


def level_score(title: str) -> float:
    return LEVEL_SCALE[priority.level_of(title)]


def age_days(job: Mapping[str, Any], now: datetime | None = None) -> float | None:
    hours = freshness.age_hours(job, now)
    return None if hours is None else hours / 24.0


def fresh_score(days: float | None) -> float:
    if days is None:
        return NO_DATE
    return next((s for limit, s in FRESH_SCALE if days <= limit), 1.0)


def pay_top(salary_header: str, text: str) -> int | None:
    """The top of the salary band the ad states: the `Salary:` header (bare numbers allowed), else any £ figure in the text that is a yearly amount (a day
    or hour rate, a figure under £15,000 or over £1,000,000 is not). None when it states none."""
    def amounts(s: str, rx: re.Pattern) -> list[int]:
        out = []
        for m in rx.finditer(s or ""):
            if rx is MONEY:
                n = int(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            else:
                n = int(m.group(3)) * 1000 if m.group(3) else int(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            if not NOT_ANNUAL.match(s[m.end():m.end() + 14]) and MIN_SALARY <= n <= MAX_SALARY:
                out.append(n)
        return out
    found = amounts(salary_header, MONEY) or amounts(salary_header, BARE_MONEY) or amounts(text, MONEY)
    return max(found) if found else None


def salary_floor(settings) -> int | None:
    """Your yearly salary figure from answers.yaml (`answer_numeric` of the salary-expectation entry), None when the file has none. Read only."""
    try:
        book = answers_mod.load(answers_mod.path_for(settings))
    except answers_mod.AnswersError:
        return None
    r = book.resolve("expected salary", {})
    digits = str(r.numeric).replace(",", "") if r is not None and r.numeric else ""
    return int(digits) if digits.isdigit() else None


def pay_score(top: int | None, floor: int | None) -> float:
    if top is None or floor is None:
        return NO_BAND
    return 5.0 if top >= floor else 1.0


def score(job: Mapping[str, Any], settings, fb: Mapping[str, Any], *, floor: int | None = None, now: datetime | None = None) -> Triage:
    """T for one ad (a job-like dict: title, description, sponsor_match, agency, work_country, posted / first_seen ..., salary)."""
    title, jd = str(_get(job, "title") or ""), str(_get(job, "description") or "")
    a, l = role_score(title, settings), level_score(title)
    e, have, lack = evidence(jd, fb, settings)
    v, why = visa_score(job, settings)
    days = age_days(job, now)
    fr = fresh_score(days)
    top = pay_top(str(_get(job, "salary") or ""), jd)
    p = pay_score(top, floor)
    t = sum(WEIGHTS[k] * x for k, x in (("A", a), ("E", e), ("V", v), ("L", l), ("Fr", fr), ("P", p)))
    return Triage(a, e, v, l, fr, p, t, have, lack, why, days, top, floor)


# ------------------------------------------------------------------ an inbox file as a job
def inbox_job(path: Path, settings, fb: Mapping[str, Any], idx) -> dict | None:
    """The job-like dict of an inbox .txt file (priority.file_job plus the header fields triage needs). None when the file cannot be read as a JD."""
    job = priority.file_job(path, settings, dict(fb), idx)
    if job is None:
        return None
    hdr = manual.header_fields(job["description"])
    job.update(url=hdr.get("url"), apply_url=hdr.get("apply_url"), apply_kind=hdr.get("apply_kind"), location=hdr.get("location"),
               salary=hdr.get("salary"), file=str(path))
    return job


# ------------------------------------------------------------------ hard disqualifiers
@dataclass(frozen=True)
class Disqualified:
    stage: str            # liveness | blacklist | channel | level | dealbreaker
    reason: str

    def line(self) -> str:
        return f"{self.stage}: {self.reason}"


def disqualify(job: Mapping[str, Any], settings, db, *, archive: list[tuple[str, str]] | None = None, others: list[dict] | None = None,
               alive: Callable[[Mapping[str, Any]], str | None] | None = None) -> Disqualified | None:
    """The first hard stop that applies, or None. `alive(job)` returns the reason a posting is closed, or None (it is how liveness reaches the network;
    without it that step is skipped). `archive` is the (company folder, role folder) pairs of your archives."""
    cfg = targeting_mod.config(settings)
    company, title = str(_get(job, "company") or ""), str(_get(job, "title") or "")
    if alive is not None and (why := alive(job)):
        return Disqualified("liveness", why)
    try:
        hit = blacklist.load(blacklist.path_for(settings)).hit(company, str(_get(job, "url") or ""), str(_get(job, "apply_url") or ""))
    except blacklist.BlacklistError as e:
        return Disqualified("blacklist", f"{e} (fix config/profile/blacklist.yaml)")
    if hit is not None:
        return Disqualified("blacklist", f"{company} is on your blacklist" + (f" since {hit.since}" if hit.since else "") + (f": {hit.reason}" if hit.reason else ""))
    if channel.hidden_employer(job):
        return Disqualified("channel", "an agency ad that does not name the employer")
    others = [r for r in (others if others is not None else (dict(x) for x in db.jobs())) if r["id"] != _get(job, "id")]
    matches = channel.find(job, others, archive or [])
    if matches:
        return Disqualified("channel", f"the same role is already on record under another channel ({matches[0].label})")
    if (lvl := gates.level_excluded(title, settings)):
        return Disqualified("level", f"level excluded ({lvl})")
    if cfg.dealbreakers:
        top = pay_top(str(_get(job, "salary") or ""), str(_get(job, "description") or ""))
        hit2 = dealbreakers.find(cfg.dealbreakers, str(_get(job, "description") or ""), top_pay=top, outside_home=outside_home(job, settings))
        if hit2 is not None:
            return Disqualified("dealbreaker", hit2.line())
    return None
