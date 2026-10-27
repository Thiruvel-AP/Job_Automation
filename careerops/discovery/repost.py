"""The repost / ghost-job signal: information only, never a score, never a filter.

  possible repost (n listings since <date>)   same company, same title identity, 2 or more URLs, 2 or more distinct first-seen dates inside the last 90
                                              days, a span of more than 0 days. Title identity is the SET of the title's words, so "Engineer - Munich" and
                                              "Engineer - Berlin" are two jobs, and one sweep that finds the same title in two cities is not a repost.
  evergreen?                                  a posting whose own posted date is more than 120 days old (inbox ads of 456 to 1123 days exist).

The first-seen day is the day this tool first saw the listing (discovery/firstseen.py, or the jobs table), not the board's publish date.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

from . import candidate as cand

WINDOW_DAYS = 90
EVERGREEN_DAYS = 120
_GENDER = re.compile(r"\(?\b(?:m\s*/\s*f\s*/?\s*[dx]?|f\s*/\s*m\s*/?\s*[dx]?|m\s*/\s*w\s*/?\s*[dx]?)\b\)?", re.I)


def title_identity(title: str) -> frozenset[str]:
    """The set of words of a title (lower case, gender tags dropped): the same set is the same job."""
    t = _GENDER.sub(" ", str(title or "").lower())
    return frozenset(re.findall(r"[a-z0-9+#]+", t))


def day(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def url_key(url: str) -> str:
    p = urlparse(str(url or "").strip())
    return f"{(p.hostname or '').lower()}{p.path.rstrip('/')}"


@dataclass(frozen=True)
class Listing:
    company: str
    title: str
    url: str
    first_seen: str = ""          # YYYY-MM-DD (or an ISO timestamp): the day this tool first saw it
    posted: str = ""              # the board's own posted date, when it gave one
    id: str = ""

    @property
    def key(self) -> str:
        return self.id or url_key(self.url) or f"{self.company}|{self.title}|{self.first_seen}"


def age_days(posted: object, today: date) -> int | None:
    d = day(posted)
    return (today - d).days if d else None


def evergreen(posted: object, today: date) -> bool:
    age = age_days(posted, today)
    return age is not None and age > EVERGREEN_DAYS


def groups(listings: list[Listing], today: date) -> list[list[Listing]]:
    """The sets of listings that look like one job posted again: each has 2+ URLs and 2+ first-seen dates inside the window."""
    since = today - timedelta(days=WINDOW_DAYS)
    by: dict[tuple, dict[str, Listing]] = defaultdict(dict)
    for item in listings:
        seen, family = day(item.first_seen), cand.family_key(item.company)
        words = title_identity(item.title)
        if seen is None or seen < since or seen > today or not family or not words:
            continue
        slot = by[(family, words)]
        k = url_key(item.url) or item.key
        if k not in slot or day(slot[k].first_seen) > seen:             # one listing per URL, at the day it was first seen
            slot[k] = item
    out = []
    for slot in by.values():
        items = list(slot.values())
        dates = {day(i.first_seen) for i in items}
        if len(items) >= 2 and len(dates) >= 2 and (max(dates) - min(dates)).days > 0:
            out.append(sorted(items, key=lambda i: (day(i.first_seen), i.url)))
    return out


def analyse(listings: list[Listing], today: date) -> dict[str, list[str]]:
    """{listing.key: [flags]} for every listing: `possible repost (n listings since <date>)` and `evergreen?`. Information only."""
    flags: dict[str, list[str]] = {item.key: [] for item in listings}
    for g in groups(listings, today):
        text = f"possible repost ({len(g)} listings since {day(g[0].first_seen).isoformat()})"
        keys = {url_key(i.url) for i in g}
        for item in listings:
            if url_key(item.url) in keys and cand.family_key(item.company) == cand.family_key(g[0].company) and title_identity(item.title) == title_identity(g[0].title):
                if text not in flags[item.key]:
                    flags[item.key].append(text)
    for item in listings:
        if evergreen(item.posted, today):
            flags[item.key].append("evergreen?")
    return flags


@dataclass(frozen=True)
class Summary:
    total: int
    repost_groups: int
    repost_listings: int
    evergreen: int

    def line(self) -> str:
        return f"{self.repost_groups} possible repost(s) ({self.repost_listings} listings), {self.evergreen} evergreen? of {self.total} listings"


def summary(listings: list[Listing], today: date) -> Summary:
    g = groups(listings, today)
    return Summary(len(listings), len(g), sum(len(x) for x in g), sum(1 for i in listings if evergreen(i.posted, today)))
