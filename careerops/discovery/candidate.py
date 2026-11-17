"""A job found by a discovery source, before it becomes an inbox file: the shape every source produces, the filters
(target role family + UK / remote-UK) and the key that tells two sightings of one job apart (company family + title + place)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..apply.naming import folder_key
from . import freshness, gates
from .intake import Ad

UK_WORDS = re.compile(r"\b(united kingdom|uk|u\.k\.|great britain|britain|england|scotland|wales|northern ireland)\b", re.I)
# Parts of a place that say nothing about which place it is.
_PLACE_NOISE = re.compile(r"\b(united kingdom|uk|u\.k\.|england|scotland|wales|great britain|gb|hybrid|remote|on[- ]?site|office|area|greater)\b")


@dataclass
class Candidate:
    source: str                        # greenhouse | lever | lever_eu | ashby | reed | cvlibrary | adzuna ...
    company: str
    title: str
    location: str
    url: str
    description: str
    posted: str = ""                   # an ISO date or datetime, when the source says
    posted_source: str = ""            # where `posted` comes from, one of freshness.TRUSTED ("greenhouse.first_published"); "" = not trusted
    agency: bool = False
    salary: str = ""
    ext_id: str = ""
    closed: bool = False               # the source says the posting is no longer open
    country: str = ""                  # ISO-ish country name or code when the source gives one
    origin: str = ""                   # how it was found when not directly: "gmail:linkedin"
    board: str = ""                    # the job board it was read from, "ashby:openai" (which registry source it counts for)
    listed_only: bool = False          # outside the UK: written for the record (Country: / Status: listed_only), never applied to
    seen: str = ""                     # YYYY-MM-DD an alert about it reached the mailbox, when it was found through one
    seen_at: str = ""                  # the same moment as an ISO datetime (the email's arrival): an upper bound for when it was posted
    first_seen: str = ""               # set by discover when the job is kept: the earliest of `seen` and the first run that saw it
    open_listing: bool = False         # still listed on the employer's own board (Greenhouse / Lever / Ashby) when it was read
    apply_url: str = ""                # where to apply: the employer's page for a posting read off its board ("" = the url)
    apply_kind: str = ""               # greenhouse | lever | ashby | workday | employer_other | aggregator_only (applykind.py)
    linkedin_job_id: str = ""          # v3.8: the LinkedIn job number of the email card this job was found through (written as a `LinkedIn job id:` header)

    def ad_id(self) -> str:
        return self.ext_id or hashlib.sha1(self.url.encode()).hexdigest()[:10]

    def to_ad(self) -> Ad:
        posted = (self.posted or "").strip()
        place = self.location                                     # the country a source gave rides with the place ("Hull, GB"): the shortlist's region check reads the same text
        if self.country and not self.listed_only and self.country.strip().lower() not in (place or "").lower():
            place = f"{place}, {self.country.strip()}" if place else self.country.strip()
        return Ad(company=self.company, title=self.title, url=self.url, location=place, text=self.description,
                  source=self.source, ad_id=self.ad_id(), salary=self.salary, agency=self.agency,
                  posted=posted if freshness.is_iso(posted) else "", posted_source=self.posted_source if freshness.is_iso(posted) else "",
                  country=self.country if self.listed_only else "", listed_only=self.listed_only,
                  first_seen=self.first_seen, open_listing=self.open_listing, apply_url=self.apply_url, apply_kind=self.apply_kind,
                  linkedin_job_id=self.linkedin_job_id)


def norm_title(title: str) -> str:
    t = re.sub(r"\((?:[^)]*)\)|\[[^\]]*\]", " ", title.lower())
    t = re.sub(r"\b(m\s*/\s*f\s*/?\s*[dx]?|f\s*/\s*m\s*/?\s*[dx]?)\b", " ", t)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", t)).strip()


def norm_place(place: str) -> str:
    """The town or region only: 'London, UK' and 'London (Hybrid)' are both 'london'; '' when it names no place."""
    first = re.split(r"[,;|/(–-]", place.lower().strip(), maxsplit=1)[0]
    return re.sub(r"\s+", " ", _PLACE_NOISE.sub(" ", first)).strip()


def places_agree(a: str, b: str) -> bool:
    """Do two places not contradict each other? A missing place agrees with anything; otherwise the towns are the same, or one place is
    named inside the other ('Manchester' is inside 'London, UK or Manchester, UK')."""
    na, nb = norm_place(a or ""), norm_place(b or "")
    if not na or not nb or na == nb:
        return True
    return na in (b or "").lower() or nb in (a or "").lower()


# What a board adds to an employer's name: "Graphcore Early Careers", "Acme Careers", "Acme UK Ltd". Stripped from the end, one phrase at a time.
_FAMILY_NOISE = re.compile(r"[\s,/-]+(?:early[ -]careers?|graduate (?:programmes?|schemes?)|careers?|jobs|recruitment|u\.?k\.?|gb|great britain|"
                           r"united kingdom|ltd|limited|plc|llp|lp|inc|incorporated|llc|corp|corporation|gmbh|sa|bv|ag)\.?$")


def family_key(name: str) -> str:
    """The employer behind a board's company name, for telling one job on two boards apart: lower case, `&` -> `and`, `(...)` dropped, the
    trailing "Early Careers", "Careers", "UK", "Ltd" ... stripped, only [a-z0-9] kept. A name that is nothing but such words keeps them."""
    s = re.sub(r"\([^)]*\)", " ", (name or "").lower().replace("&", " and ")).strip()
    while True:
        t = _FAMILY_NOISE.sub("", s).strip(" ,/-")
        if t == s or not t:
            break
        s = t
    return re.sub(r"[^a-z0-9]", "", s) or folder_key(name)


def dedupe_key(company: str, title: str, location: str) -> tuple[str, str, str]:
    return (family_key(company), norm_title(title), norm_place(location))


def family_of(c: Candidate, settings) -> str | None:
    hit = gates.classify_role(c.title, settings)
    return hit[0] if hit else None


def uk_ok(c: Candidate, settings) -> bool:
    """UK or remote-UK. A place that matches the gate's UK locations (cities, UK, England ...) is in; a bare 'remote' is in only
    when the place, the title or the start of the ad also says UK / United Kingdom / Britain; a source-given country decides when
    there is one; no place at all is in only when the start of the ad names the UK."""
    country = c.country.strip().lower()
    if country:
        return country in {"gb", "uk", "gbr", "united kingdom", "great britain"} or bool(UK_WORDS.search(country))
    patterns = [p for p in settings["gates"]["locations_ok"] if p != "remote"]
    loc = c.location or ""
    if loc.strip() and any(re.search(p, loc, re.I) for p in patterns):
        return True
    head = f"{loc} {c.title} {c.description[:600]}"
    if re.search(r"\bremote\b", loc, re.I) or not loc.strip():
        return bool(UK_WORDS.search(head))
    return False


def posted_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y",):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


LEVEL_WORDS = frozenset({"senior", "sr", "lead", "staff", "principal", "junior", "jr", "graduate", "grad", "head", "intern", "internship",
                         "associate", "entry", "trainee", "apprentice", "director", "manager", "placement"})


def level_words(title: str) -> frozenset[str]:
    """The seniority words in a title ('Senior Data Scientist' -> {'senior'}): two titles of one role at different levels are two jobs."""
    return frozenset(norm_title(title).split()) & LEVEL_WORDS


def title_relevance(title: str, settings) -> float:
    """How close a title is to what you are looking for, 0-1: 1.0 for a tier-1 family (roles.families), 0.8 for tier 2, 0.6 for tier 3
    and so on down to 0.4; halved when the title carries a level the triage excludes (senior, lead ...); 0.1 for no family at all."""
    hit = gates.classify_role(title, settings)
    if hit is None:
        return 0.1
    r = max(0.4, 1.0 - 0.2 * (hit[1] - 1))
    return r / 2 if gates.level_excluded(title, settings) else r
