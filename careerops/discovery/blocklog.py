"""data/discover/blocked_hosts.json: the hosts that answered a script with HTTP 401 / 403 / 429, and when.

http.py stops a host for the rest of the run the moment it says no (nothing is retried or worked around); this file is the memory between
runs. For each host: the status, the time (UTC), how many requests to it came before the refusal, and how many the run had made in all.
For MEMORY_DAYS afterwards the next runs keep at least SLOW_GAP_S seconds between requests to that host (http.Fetcher reads it), and the
health table (health.py) says when the host was last seen refusing. The file is rewritten whole (discovery code deletes and moves nothing);
one that cannot be read is an empty log, never an error.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

FILE = "blocked_hosts.json"
SLOW_GAP_S = 2.0           # seconds between requests to a host that refused us recently
MEMORY_DAYS = 7            # how long after a refusal the slower pace lasts


class BlockLog:
    def __init__(self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path, self.now = Path(path), now

    def hosts(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")).get("hosts")
        except (OSError, ValueError, AttributeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, host: str, status: int, requests_before: int, run_requests: int) -> None:
        hosts = self.hosts()
        hosts[host] = {"status": status, "at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                       "requests_before": requests_before, "run_requests": run_requests}
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

    def describe(self, host: str) -> str:
        """A health-table sentence for a host that refused recently, '' otherwise."""
        e = self.recent(host)
        if e is None:
            return ""
        return (f"{host} last answered HTTP {e.get('status')} at {e.get('at')} after {e.get('requests_before')} request(s) that run; "
                f"requests to it are at least {SLOW_GAP_S:g} s apart for {MEMORY_DAYS} days")
