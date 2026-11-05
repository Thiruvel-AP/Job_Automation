"""The public list of job-board names (v3.0): which companies have a Greenhouse, Lever or Ashby board, and which Workday sites exist.

Source: github.com/Feashliaa/job-board-aggregator, the `data/` folder (greenhouse_companies.json, lever_companies.json, ashby_companies.json,
workday_companies.json), read from raw.githubusercontent.com (robots.txt: none, 404). Licence, read on 5 Nov 2026 from that repository's README:
"The curated company datasets in `data/` are licensed under CC BY-NC 4.0 ... You're free to use, modify, and share the data for non-commercial purposes.
Commercial use of the datasets requires permission". This is a personal job search, which is non-commercial use; the attribution the licence asks for is this
paragraph and the "source" line the run prints. Switch it off with `discover.dataset.enabled: false`.

What it changes: a board name is looked up here first. When a company's name is a slug in the list, the tool asks that one provider once to check it (an exact,
verified question) and guesses nothing else for that name. A name that is not in the list is still guessed, within the per-provider limit (boards.py). The list is
partial (about half of the boards found by name so far are in it), so "not in the list" proves nothing. Workday sites are only SUGGESTED (their name is not
checked by anything), in `search-plan` and the run report: you put one in careers_urls.yaml if it is the company's.

The four files are kept in paths.discover/dataset/ and asked again after `discover.dataset.refresh_days` (7): at most four requests a week. A refusal (401 / 403 /
429) stops the download for the run and the cached copy, if any, is used.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .http import Blocked, FetchError, Fetcher
from .workday import Site

BASE = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data"
SOURCE = "github.com/Feashliaa/job-board-aggregator (data/), CC BY-NC 4.0"
FILES = {"greenhouse": "greenhouse_companies.json", "lever": "lever_companies.json", "ashby": "ashby_companies.json", "workday": "workday_companies.json"}
REFRESH_DAYS = 7
TOKEN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")


class Dataset:
    def __init__(self, settings, fetcher: Fetcher | None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        cfg = settings["discover"].get("dataset")
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = cfg.get("enabled", True) is not False
        self.days = float(cfg.get("refresh_days", REFRESH_DAYS))
        self.dir: Path = settings.path("discover") / "dataset"
        self.fetcher, self.clock = fetcher, clock
        self.slugs: dict[str, dict[str, str]] = {"greenhouse": {}, "lever": {}, "ashby": {}}       # provider -> {lower-case slug: the slug as written}
        self.workday: dict[str, list[Site]] = {}                                                  # tenant (lower case) -> sites
        self.ready = False
        self._stopped = False
        self.requests = 0
        self.notes: list[str] = []
        self.refreshed: list[str] = []

    # ---- loading
    def _path(self, name: str) -> Path:
        return self.dir / FILES[name]

    def _fresh(self, name: str) -> bool:
        try:
            age = self.clock().timestamp() - self._path(name).stat().st_mtime
        except OSError:
            return False
        return age < self.days * 86400

    def _read(self, name: str) -> list | None:
        try:
            data = json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def _download(self, name: str) -> list | None:
        if self.fetcher is None or self._stopped:
            return None
        self.requests += 1
        try:
            data = self.fetcher.json(f"{BASE}/{FILES[name]}", what=f"board list {FILES[name]}")
        except Blocked as e:
            self._stopped = True
            self.notes.append(f"dataset: stopped for this run: {e}")
            return None
        except FetchError as e:
            self.notes.append(f"dataset: {e}")
            return None
        if not isinstance(data, list):
            self.notes.append(f"dataset: {FILES[name]} is not a list; ignored")
            return None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(name).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        self.refreshed.append(FILES[name])
        return data

    def ensure(self) -> None:
        """Load the four lists (from disk when fresh, else asked once each). Called by the first lookup; never raises."""
        if self.ready or not self.enabled:
            self.ready = True
            return
        self.ready = True
        for name in FILES:
            data = self._read(name) if self._fresh(name) else None
            if data is None:
                data = self._download(name)
            if data is None:
                data = self._read(name)                    # an older copy is better than none
            for item in data or []:
                self._add(name, item)
        if not any(self.slugs.values()) and not self.workday:
            self.enabled = False                          # nothing to look up (offline, or the files are gone): a run guesses as before

    def _add(self, name: str, item: object) -> None:
        s = str(item or "").strip()
        if not s:
            return
        if name == "workday":
            tenant, _, rest = s.partition("|")
            inst, _, site = rest.partition("|")
            if tenant and inst.startswith("wd") and site and TOKEN_OK.match(tenant):
                self.workday.setdefault(tenant.lower(), []).append(Site(f"{tenant.lower()}.{inst}.myworkdayjobs.com", tenant.lower(), site))
        elif TOKEN_OK.match(s):
            self.slugs[name].setdefault(s.lower(), s)

    # ---- lookups
    def hits(self, token: str) -> dict[str, str]:
        """{provider: slug as the list writes it} for the providers (greenhouse, lever, ashby) whose list holds this token, ignoring case. {} when not listed or off."""
        if not self.enabled:
            return {}
        self.ensure()
        t = token.lower()
        return {p: m[t] for p, m in self.slugs.items() if t in m}

    def workday_urls(self, token: str) -> list[str]:
        """The Workday careers addresses the list holds for a tenant spelled like this token (a suggestion only: nothing checks whose they are)."""
        if not self.enabled:
            return []
        self.ensure()
        return [f"https://{s.host}/{s.site}" for s in self.workday.get(token.lower(), [])][:3]

    def summary(self) -> str:
        n = {p: len(m) for p, m in self.slugs.items()}
        return (f"dataset: {n.get('greenhouse', 0)} Greenhouse, {n.get('lever', 0)} Lever, {n.get('ashby', 0)} Ashby board names, "
                f"{sum(len(v) for v in self.workday.values())} Workday sites; {self.requests} request(s) this run; source {SOURCE}")
