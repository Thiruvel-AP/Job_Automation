"""data/discover/blocked_hosts.json: the hosts that answered a script with HTTP 401 / 403 / 429, and when.

http.py stops a host for the rest of the run the moment it says no (nothing is retried or worked around); this file is the memory between
runs. For each host: the status, the time (UTC), how many requests to it came before the refusal, how many the run had made in all, and (v3.1) the exact
URL that got the refusal (`url`).
For MEMORY_DAYS afterwards the next runs keep at least SLOW_GAP_S seconds between requests to that host (http.Fetcher reads it), and the
health table (health.py) says when the host was last seen refusing. The file is rewritten whole (discovery code deletes and moves nothing);
one that cannot be read is an empty log, never an error.

A 429 from a host in http.LONG_BACKOFF_HOSTS (Workable) also carries `until`: the time before which no run asks that host at all (24 h, or
longer when the Retry-After header says so). `backoff_until` / `active_backoffs` read it; an `until` in the past is no backoff.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

FILE = "blocked_hosts.json"
SLOW_GAP_S = 2.0           # seconds between requests to a host that refused us recently
MEMORY_DAYS = 7            # how long after a refusal the slower pace lasts
LONG_BACKOFF_S = 24 * 3600     # a 429 from a host that gets a long backoff: no request to it for at least this long
MAX_BACKOFF_S = MEMORY_DAYS * 24 * 3600     # ... and never longer than this, whatever Retry-After asks (a host cannot lock the tool out for good)


def parse_retry_after(value: object, now: datetime) -> int | None:
    """The seconds a `Retry-After` header asks for (a number of seconds, or an HTTP date); None when absent, unreadable or not in the future."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) or None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    secs = int((when - now).total_seconds())
    return secs if secs > 0 else None


def backoff_seconds(retry_after: object, now: datetime) -> int:
    """How long a 429 keeps the host quiet: 24 h, or the Retry-After when that is longer, never more than MAX_BACKOFF_S."""
    return min(max(LONG_BACKOFF_S, parse_retry_after(retry_after, now) or 0), MAX_BACKOFF_S)


class BlockLog:
    def __init__(self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path, self.now = Path(path), now

    def hosts(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")).get("hosts")
        except (OSError, ValueError, AttributeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, host: str, status: int, requests_before: int, run_requests: int, until: datetime | None = None, url: str = "") -> None:
        hosts = self.hosts()
        hosts[host] = {"status": status, "at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                       "requests_before": requests_before, "run_requests": run_requests, **({"url": url} if url else {}),
                       **({"until": until.astimezone(timezone.utc).isoformat(timespec="seconds")} if until is not None else {})}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 1, "hosts": hosts}, indent=2), encoding="utf-8")

    def recent(self, host: str) -> dict | None:
        """The entry of `host` when it refused within MEMORY_DAYS of now, else None."""
        entry = self.hosts().get(host)
        try:
            at = datetime.fromisoformat(str(entry["at"]))
        except (TypeError, KeyError, ValueError):
            return None
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return entry if self.now() - at <= timedelta(days=MEMORY_DAYS) else None

    def slow_hosts(self) -> list[str]:
        return [h for h in self.hosts() if self.recent(h) is not None]

    def backoff_until(self, host: str) -> datetime | None:
        """When the host may be asked again, if its last refusal set a backoff that has not ended yet; else None."""
        entry = self.hosts().get(host)
        try:
            until = datetime.fromisoformat(str((entry or {})["until"]))
        except (KeyError, TypeError, ValueError):
            return None
        until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
        return until if self.now() < until else None

    def active_backoffs(self) -> dict[str, tuple[int, datetime]]:
        """host -> (the status it answered, when it may be asked again) for every host still in a backoff."""
        out = {}
        for host, entry in self.hosts().items():
            until = self.backoff_until(host)
            if until is not None:
                out[host] = (int(entry.get("status") or 429), until)
        return out

    def describe(self, host: str) -> str:
        """A health-table sentence for a host that refused recently, '' otherwise."""
        e = self.recent(host)
        if e is None:
            return ""
        until = self.backoff_until(host)
        later = (f"not asked at all until {until.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC, then requests to it are at least {SLOW_GAP_S:g} s apart for {MEMORY_DAYS} days"
                 if until is not None else f"requests to it are at least {SLOW_GAP_S:g} s apart for {MEMORY_DAYS} days")
        where = f" (to {e['url']})" if e.get("url") else ""
        return f"{host} last answered HTTP {e.get('status')}{where} at {e.get('at')} after {e.get('requests_before')} request(s) that run; {later}"
