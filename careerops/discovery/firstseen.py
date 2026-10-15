"""The day discover first saw each job: what the recency factor R counts from (priority.py), not the board's own publish date.

A board keeps showing a posting for months, and the date it was first published says nothing about how long YOU have known of it; an
alert says when the mail arrived. So the first sighting is the earlier of the alert's date and the first discover run that read
the job, kept in paths.discover/first_seen.json as {"company|title|place": "YYYY-MM-DD"}. The key is the same company + normalised
title + place that tells two sightings of one job apart (candidate.dedupe_key). A dry run records nothing.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import candidate as cand

MAX_KEYS = 20000           # the oldest sightings are dropped past this, so the file cannot grow without end


class FirstSeen:
    def __init__(self, settings, *, persist: bool = True):
        self.path = settings.path("discover") / "first_seen.json"
        self.persist = persist
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data: dict[str, str] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    @staticmethod
    def key(company: str, title: str, location: str) -> str:
        return "|".join(cand.dedupe_key(company, title, location))

    def record(self, company: str, title: str, location: str, seen: str) -> str:
        """Note a sighting on `seen` (YYYY-MM-DD) and return the first day this job was seen: the earlier of that and any earlier one."""
        k = self.key(company, title, location)
        have = self.data.get(k)
        first = min(have, seen) if have else seen
        if first != have:
            self.data[k] = first
            self.dirty = True
        return first

    def save(self) -> None:
        if not (self.dirty and self.persist):
            return
        if len(self.data) > MAX_KEYS:
            self.data = dict(sorted(self.data.items(), key=lambda kv: kv[1])[-MAX_KEYS:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
            self.dirty = False
        except OSError:
            pass


def day(value: str | datetime | None) -> str:
    """YYYY-MM-DD of a date or datetime, or of the first ten characters of an ISO string; '' when it is not one."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value or "")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text
