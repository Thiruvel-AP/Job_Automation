"""A job found by a discovery source, before it becomes an inbox file: the shape every source produces, the filters
(target role family + UK / remote-UK) and the key that tells two sightings of one job apart (company + title + place)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..apply.naming import folder_key
from . import gates
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
    agency: bool = False
    salary: str = ""
    ext_id: str = ""
    closed: bool = False               # the source says the posting is no longer open
    country: str = ""                  # ISO-ish country name or code when the source gives one
    origin: str = ""                   # how it was found when not directly: "gmail:linkedin"

    def ad_id(self) -> str:
        return self.ext_id or hashlib.sha1(self.url.encode()).hexdigest()[:10]

    def to_ad(self) -> Ad:
        posted = (self.posted or "")[:10]
        return Ad(company=self.company, title=self.title, url=self.url, location=self.location, text=self.description,
                  source=self.source, ad_id=self.ad_id(), salary=self.salary, agency=self.agency,
                  posted=posted if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted) else "")


def norm_title(title: str) -> str:
    t = re.sub(r"\((?:[^)]*)\)|\[[^\]]*\]", " ", title.lower())
    t = re.sub(r"\b(m\s*/\s*f\s*/?\s*[dx]?|f\s*/\s*m\s*/?\s*[dx]?)\b", " ", t)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", t)).strip()


def norm_place(place: str) -> str:
    """The town or region only: 'London, UK' and 'London (Hybrid)' are both 'london'; '' when it names no place."""
    first = re.split(r"[,;|/(–-]", place.lower().strip(), maxsplit=1)[0]
    return re.sub(r"\s+", " ", _PLACE_NOISE.sub(" ", first)).strip()


def dedupe_key(company: str, title: str, location: str) -> tuple[str, str, str]:
    return (folder_key(company), norm_title(title), norm_place(location))


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
