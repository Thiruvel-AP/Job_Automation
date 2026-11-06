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

v3.1: `match(company)` is how a company is matched to the list: its normalised name (register.normalise, then letters and digits only; "M&G plc" is `mandg` or `mg`)
equals a board name written the same way, or is at least NAME_MATCH (0.90) like one. `workday_match(company)` does the same with the Workday tenants. Only a
provider that matches is asked about the company (boards.py: one verifying request; Workday through its CXS endpoint), and nothing is guessed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .http import Blocked, FetchError, Fetcher
from .workday import Site

BASE = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data"
SOURCE = "github.com/Feashliaa/job-board-aggregator (data/), CC BY-NC 4.0"
FILES = {"greenhouse": "greenhouse_companies.json", "lever": "lever_companies.json", "ashby": "ashby_companies.json", "workday": "workday_companies.json"}
REFRESH_DAYS = 7
TOKEN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
NAME_MATCH = 0.90                  # v3.1: a company name and a board name in the list, both written as letters and digits only, are one at this ratio
MIN_FUZZY_LEN = 6                  # ... a shorter name must match exactly ("mg" is not "mgm")


def compact(text: str) -> str:
    """Letters and digits only, lower case: "Jazz-Pharmaceuticals" -> "jazzpharmaceuticals"."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def name_forms(company: str) -> list[str]:
    """The ways a company name is written as a board name: the normalised name (no Ltd / plc, "&" as "and") and the same with the "and" dropped."""
    from .register import normalise                         # imported here: register imports much of the package
    norm = normalise(company or "")
    forms = [compact(norm), compact(re.sub(r"\band\b", " ", norm))]
    return [f for f in dict.fromkeys(forms) if f]


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
        self._compact: dict[str, dict[str, str]] = {}                                            # provider -> {compact slug: slug as written} (v3.1)
        self._wd_compact: dict[str, str] = {}                                                    # compact tenant -> tenant (v3.1)
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
                self._wd_compact.setdefault(compact(tenant), tenant.lower())
        elif TOKEN_OK.match(s):
            self.slugs[name].setdefault(s.lower(), s)
            self._compact.setdefault(name, {}).setdefault(compact(s), s)

    # ---- lookups
    def hits(self, token: str) -> dict[str, str]:
        """{provider: slug as the list writes it} for the providers (greenhouse, lever, ashby) whose list holds this token, ignoring case. {} when not listed or off."""
        if not self.enabled:
            return {}
        self.ensure()
        t = token.lower()
        return {p: m[t] for p, m in self.slugs.items() if t in m}

    @staticmethod
    def _best(forms: list[str], table: dict[str, str]) -> tuple[str, float] | None:
        """(the entry of `table` the name forms match, the ratio): an exact form first, else the best one at NAME_MATCH or more. None when none."""
        for f in forms:
            if f in table:
                return table[f], 1.0
        best: tuple[str, float] | None = None
        for f in forms:
            if len(f) < MIN_FUZZY_LEN:
                continue
            for key, value in table.items():
                if key[:1] != f[:1] or abs(len(key) - len(f)) > max(2, len(f) // 5):
                    continue
                sm = SequenceMatcher(None, f, key)
                if sm.real_quick_ratio() < NAME_MATCH or sm.quick_ratio() < NAME_MATCH:
                    continue
                r = sm.ratio()
                if r >= NAME_MATCH and (best is None or r > best[1]):
                    best = (value, r)
        return best

    def match(self, company: str) -> dict[str, tuple[str, float]]:
        """{provider: (slug as the list writes it, ratio)} for the providers (greenhouse, lever, ashby) whose list has a board named like this company:
        the same name in letters and digits, or one at NAME_MATCH or more. {} when none matches or the list is off."""
        if not self.enabled:
            return {}
        self.ensure()
        forms = name_forms(company)
        out = {}
        for prov in ("greenhouse", "lever", "ashby"):
            got = self._best(forms, self._compact.get(prov, {}))
            if got is not None:
                out[prov] = got
        return out

    def workday_match(self, company: str) -> list[Site]:
        """The Workday sites of the tenant named like this company (same rule as `match`), at most 3; [] when none."""
        if not self.enabled:
            return []
        self.ensure()
        got = self._best(name_forms(company), self._wd_compact)
        return list(self.workday.get(got[0], []))[:3] if got else []

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
