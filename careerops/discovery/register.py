"""UK Register of Licensed Sponsors: download, parse, and match company names against it.

The register's columns (as published): Organisation Name, Town/City, County, Type & Rating, Route.
It carries no industry or size, so it is used as a *join* on discovered jobs, not as a source of jobs.
"""
from __future__ import annotations

import csv
import io
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

from ..db import DB

UA = {"User-Agent": "career-ops/2.0 (personal job search; contact via GitHub)"}

LEGAL = r"\b(limited|ltd|plc|llp|lp|inc|incorporated|corp|corporation|co|company|gmbh|sa|bv|uk|u\.k\.)\b"


def normalise(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(LEGAL, " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def find_csv_url(page_url: str) -> str:
    html = requests.get(page_url, headers=UA, timeout=60).text
    m = re.search(r'https://assets\.publishing\.service\.gov\.uk/[^"\']+?\.csv', html)
    if not m:
        raise RuntimeError(f"No CSV link found on {page_url} — page layout may have changed")
    return m.group(0)


def download(page_url: str, dest: Path, max_age_days: float = 3) -> Path:
    if dest.exists() and (time.time() - dest.stat().st_mtime) < max_age_days * 86400:
        return dest
    url = find_csv_url(page_url)
    r = requests.get(url, headers=UA, timeout=300)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _col(header: list[str], *needles: str) -> int:
    for i, h in enumerate(header):
        hl = h.lower()
        if all(n in hl for n in needles):
            return i
    raise KeyError(f"column {needles} not in {header}")


def parse(csv_path: Path, route: str | None = "Skilled Worker") -> list[tuple[str, str, str, str, str, str]]:
    text = csv_path.read_bytes().decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    i_name, i_town = _col(header, "organisation"), _col(header, "town")
    i_county, i_rating, i_route = _col(header, "county"), _col(header, "rating"), _col(header, "route")
    rows = []
    for rec in reader:
        if len(rec) <= i_route:
            continue
        if route and route.lower() not in rec[i_route].lower():
            continue
        name = rec[i_name].strip()
        # "Acme Ltd T/A Widgets" → index both the legal and the trading name
        for part in re.split(r"\s+t/a\s+|\s+trading as\s+", name, flags=re.I):
            n = normalise(part)
            if n:
                rows.append((n, name, rec[i_town], rec[i_county], rec[i_rating], rec[i_route]))
    return rows


class SponsorIndex:
    def __init__(self, db: DB):
        self.exact: dict[str, str] = {}
        self.by_first: dict[str, list[tuple[str, str]]] = {}
        for norm, name in db.conn.execute("SELECT norm, name FROM sponsors"):
            self.exact.setdefault(norm, name)
            self.by_first.setdefault(norm.split(" ")[0], []).append((norm, name))

    def __len__(self) -> int:
        return len(self.exact)

    def match(self, company: str, threshold: float = 0.92) -> tuple[str | None, float, str]:
        """Return (registered name, score, method). method ∈ exact | fuzzy | prefix | none.
        'prefix' (e.g. 'Monzo' → 'Monzo Bank Ltd') is accepted but flagged for you to verify."""
        n = normalise(company)
        if not n:
            return None, 0.0, "none"
        if n in self.exact:
            return self.exact[n], 1.0, "exact"
        cands = self.by_first.get(n.split(" ")[0], [])
        best, best_s = None, 0.0
        for norm, name in cands:
            s = SequenceMatcher(None, n, norm).ratio()
            if s > best_s:
                best, best_s = name, s
        if best and best_s >= threshold:
            return best, best_s, "fuzzy"
        if len(n) >= 5:
            pref = [(norm, name) for norm, name in cands if norm.startswith(n + " ")]
            if len(pref) == 1:          # unambiguous only
                return pref[0][1], 0.9, "prefix"
        return None, best_s, "none"


def refresh(db: DB, settings) -> int:
    cfg = settings["sponsors"]
    path = download(cfg["page_url"], settings.path("sponsors_csv"), cfg.get("max_age_days", 3))
    rows = parse(path, cfg.get("route"))
    db.replace_sponsors(rows)
    return len(rows)


# ---------------------------------------------------------------- M1: daily snapshots + diff
# Spec: docs/IMPLEMENTATION.md §2. Key = (normalise(name), normalise(town)) — name alone collides.
from dataclasses import dataclass, field  # noqa: E402


@dataclass
class Diff:
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    rating_changed: list[tuple[str, str, str, str]] = field(default_factory=list)   # norm, town, old, new


def snapshot(db: DB, settings) -> Path | None:
    """Download today's register if its CSV URL is new; store gzipped under data/register/YYYY-MM-DD.csv.gz."""
    raise NotImplementedError("M1 — register.snapshot")


def load_snapshot(path: Path, route: str | None = "Skilled Worker") -> dict[tuple[str, str], tuple]:
    """{(norm, town_norm): (register_name, town, county, rating, route)}."""
    raise NotImplementedError("M1 — register.load_snapshot")


def diff(prev: dict, cur: dict) -> Diff:
    """added = K₂∖K₁, removed = K₁∖K₂, rating_changed = {k ∈ K₁∩K₂ : r₁(k) ≠ r₂(k)}."""
    raise NotImplementedError("M1 — register.diff")


def apply_diff(db: DB, day: str, d: Diff, cur: dict) -> None:
    """Upsert `companies`, stamp first_seen/last_seen/removed_on, write `company_events`, flag affected jobs."""
    raise NotImplementedError("M1 — register.apply_diff")
