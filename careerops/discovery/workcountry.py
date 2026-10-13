"""W(job): where the work would be done, which decides the visa answers (apply/visa.py) and priority S. Generic around `visa.home`.

  on-site / hybrid          the office country: the place in the `Location:` header, else the one place the ad names for its office.
  remote, unrestricted      `visa.home`: the ad names no place it is restricted to (or says anywhere / worldwide).
  remote, restricted        a place that includes `visa.home` (the country itself, or a region such as EMEA / Europe that covers it) is
                            home; a restriction to one place that excludes home ("US only", "EU only") is that place; several such
                            places are ambiguous.
  set by you                a `Country:` header in the JD file, or `careerops set-country`, wins over everything.
  None                      "ambiguous": the job gets the status `needs_country`, is not tailored and never reaches apply.

Plain rules over the `Location:` header and the ad text, no model. Every answer carries the evidence it rests on, a header or a sentence of
the ad, so the review screen can show it and you can disagree with it. A sentence that says "not", "cannot", "outside", "except" ... decides
nothing.

**A remote role never shows the home country.** When the work is remote and W is home, what is printed for it (review screen, CHECK VISA
ANSWER, form free text, the work_location column) is `visa.remote.label` ("Remote"), and the home country's names are hidden in the
evidence line as "[home]". A restriction to another place ("US only") prints that place: it is not home.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from . import countries

SET_BY_YOU = "set by you"             # work_evidence that marks a country you gave (it survives a re-triage)
HIDDEN = "[home]"
DEFAULT_LABEL = "Remote"

REMOTE_WORD = re.compile(r"\bremote(?:ly)?\b|work(?:ing)? from home|\bwfh\b|home[- ]?based|distributed team", re.I)
REMOTE_AD = re.compile(
    r"(?:fully|100%|completely|entirely|totally)[ -]remote|remote[- ](?:first|only|role|position|job|working)\b"
    r"|\bthis (?:is a |role is |position is |job is )?(?:fully |entirely )?remote\b"
    r"|\b(?:role|position|job) (?:is|can be) (?:done |worked )?(?:fully |entirely )?remote(?:ly)?\b"
    r"|work(?:ing)? remotely|work from (?:home|anywhere)", re.I)
ONSITE_AD = re.compile(
    r"\bhybrid\b|on[- ]?site|in[- ]office|office[- ]based|\d\s*days?\s*(?:a|per|each)\s*week\s*(?:in|at)\s*(?:the|our)"
    r"|days? in the office", re.I)
HYBRID_LOC = re.compile(r"hybrid|on[- ]?site|in[- ]office", re.I)
OFFICE_WORD = re.compile(r"\b(?:office|based in|located in|headquarter\w*|hq|on[- ]?site|hybrid)\b", re.I)
SCOPE_WORD = re.compile(
    r"remote|work(?:ing)? from|based in|located in|\blocation\b|eligible|right to work|hir(?:e|es|ing)|live in|living in|reside"
    r"|candidates?|residents?|time ?zone|apply from|open to|anywhere|worldwide|globally", re.I)
ANYWHERE = re.compile(
    r"\b(?:work from anywhere|from anywhere|anywhere in the world|worldwide|world-wide|globally|global remote"
    r"|location[- ]independent|any location|across the globe)\b|\banywhere\b(?!\s+(?:in|within)\b)", re.I)
NEGATION = re.compile(r"\b(?:not|cannot|can't|unable|outside|excluding|except|ineligible|unfortunately)\b", re.I)


@dataclass(frozen=True)
class Rules:
    """What applicant.yaml `visa` says: where home is, and how a remote role is called on anything you read or send."""
    home: str = ""                        # ISO code of visa.home ("GB"); "" = not set (a remote role then cannot be placed)
    label: str = DEFAULT_LABEL            # visa.remote.label


def rules_from(applicant: Mapping[str, Any] | None) -> Rules:
    visa = (applicant or {}).get("visa") or {}
    label = str(((visa.get("remote") or {}).get("label")) or "").strip() or DEFAULT_LABEL
    return Rules(home=countries.code(visa.get("home")), label=label)


@dataclass(frozen=True)
class Work:
    country: str | None            # code of a country ("IE") or key of a region ("EU"); None = cannot be told
    mode: str                      # onsite | remote | explicit | unknown
    evidence: str = ""             # the header or sentence the answer rests on (home hidden for a remote role)
    reason: str = ""               # when country is None: why
    shown: str = ""                # what is printed for the place: the remote label for a remote role at home, else the name
    home_remote: bool = False      # remote, and W is home

    @property
    def name(self) -> str:
        return countries.place_name(self.country) if self.country else ""

    def line(self) -> str:
        if self.country:
            where = self.shown if self.home_remote else f"{self.shown} ({self.country}), {self.mode}"
            return f"Work location: {where}" + (f" — evidence: \"{self.evidence}\"" if self.evidence else "")
        return f"Work location: UNKNOWN ({self.reason or 'not stated'}) — status needs_country, it is not tailored or applied to"


def _redact(text: str, home: str) -> str:
    rx = countries.home_words(home)
    return rx.sub(HIDDEN, text) if rx else text


def _mk(rules: Rules, country: str | None, mode: str, evidence: str = "", reason: str = "") -> Work:
    if not country:
        return Work(None, mode, "", reason)
    home_remote = mode == "remote" and country == rules.home
    shown = rules.label if home_remote else countries.place_name(country)
    return Work(country, mode, _redact(evidence, rules.home) if home_remote else evidence, "", shown, home_remote)


_cache: dict[tuple[str, int], dict] = {}


def applicant_visa(settings) -> dict:
    """applicant.yaml → the whole applicant mapping ({} when the file is missing or unreadable). Read only, cached per file version."""
    from ..config import load_yaml

    path = settings.path("applicant")
    try:
        key = (str(path), os.stat(path).st_mtime_ns)
        if key not in _cache:
            _cache.clear()
            _cache[key] = load_yaml(path) or {}
        return _cache[key]
    except (OSError, ValueError, KeyError):
        return {}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+|\s[•·▪]\s", text or "")
    return [" ".join(p.split()) for p in parts if p and p.strip()]


def _clip(s: str, n: int = 220) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _onsite(loc: str, sents: list[str], rules: Rules) -> Work:
    place = countries.detect(loc)
    if place:
        return _mk(rules, countries.code(place), "onsite", f"Location: {loc}")
    found: list[tuple[str, str]] = []
    for s in sents:
        if OFFICE_WORD.search(s):
            for c in countries.mentions(s, cities=True):
                if c not in [x for x, _ in found]:
                    found.append((c, s))
    if len(found) == 1:
        return _mk(rules, countries.code(found[0][0]), "onsite", _clip(found[0][1]))
    if found:
        return _mk(rules, None, "unknown", reason="the ad names offices in several countries: " + ", ".join(c for c, _ in found))
    return _mk(rules, None, "unknown", reason="no place in the Location header and no office named in the ad")


def _remote(loc: str, sents: list[str], title: str, rules: Rules) -> Work:
    if not rules.home:
        return _mk(rules, None, "remote", reason="visa.home is not set in applicant.yaml, so a remote role cannot be placed")
    places: list[tuple[str, str]] = []
    anywhere: str | None = None
    scopes: list[tuple[str, str]] = []
    if loc and (countries.detect(loc) or ANYWHERE.search(loc) or countries.mentions(loc, regions=True)):
        scopes.append((loc, f"Location: {loc}"))
    scopes += [(s, _clip(s)) for s in sents if SCOPE_WORD.search(s) and not NEGATION.search(s)]
    for text, evidence in scopes:
        for p in countries.mentions(text, cities=text is loc, regions=True):
            if p not in [x for x, _ in places]:
                places.append((p, evidence))
        if ANYWHERE.search(text) and not anywhere:
            anywhere = evidence
    if anywhere or not places:                                       # no restriction: the work is wherever home is
        said = next((s for s in sents if REMOTE_AD.search(s)), "")
        return _mk(rules, rules.home, "remote", anywhere or (f"Location: {loc}" if loc else _clip(said) if said else f"Title: {title}"))
    covering = next(((p, ev) for p, ev in places if countries.covers(p, rules.home)), None)
    if covering:                                                      # restricted to a place that includes home
        return _mk(rules, rules.home, "remote", covering[1])
    if len(places) == 1:                                              # restricted to one place that excludes home
        return _mk(rules, countries.place_code(places[0][0]), "remote", places[0][1])
    return _mk(rules, None, "remote", reason="remote work is open to several places, none of them home: "
                                              + ", ".join(countries.place_name(p) for p, _ in places))


def resolve(*, location: str = "", description: str = "", title: str = "", override: str = "", rules: Rules = Rules()) -> Work:
    """W for one job from its `Location:` header, its ad text and its title (see the module docstring)."""
    if override and str(override).strip():
        c = countries.place_code(override)
        if c:
            return _mk(rules, c, "explicit", f"{SET_BY_YOU}: {override}")
        return _mk(rules, None, "unknown", reason=f"{override!r} is not a country or region this tool knows (use a name or code such as DE, Ireland, EU)")
    loc = " ".join(str(location or "").split())
    sents = _sentences(description)
    if HYBRID_LOC.search(loc):
        remote = False
    elif REMOTE_WORD.search(loc) or REMOTE_WORD.search(title or ""):
        remote = True
    else:
        remote = bool(any(REMOTE_AD.search(s) for s in sents)) and not any(ONSITE_AD.search(s) for s in sents)
    return _remote(loc, sents, title or "", rules) if remote else _onsite(loc, sents, rules)


def for_job(job: Mapping[str, Any] | Any, applicant: Mapping[str, Any] | None) -> Work:
    """W of a job row / dict: the country you set (work_evidence starting "set by you"), else resolved afresh from its location and ad."""
    def get(k):
        try:
            return job[k] if k in job.keys() else None
        except AttributeError:
            return job.get(k)
    stored, ev = get("work_country"), str(get("work_evidence") or "")
    return resolve(location=str(get("location") or ""), description=str(get("description") or ""), title=str(get("title") or ""),
                   override=str(stored or "") if ev.startswith(SET_BY_YOU) else "", rules=rules_from(applicant))


def shown_location(work: Work, location: str | None) -> str | None:
    """The job's location as it may be printed: the remote label for a remote role at home (never the ad's own words, which may name
    the home country), else the location as given."""
    return work.shown if work.home_remote else location
