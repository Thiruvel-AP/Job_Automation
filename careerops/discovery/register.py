"""UK Register of Licensed Sponsors: daily snapshots, diffs, and matching company names against the register.

`careerops sponsors` (M1) finds the CSV link on the gov.uk page, downloads it only when the link is new, stores it as
data/register/YYYY-MM-DD.csv.gz with a `register_snapshots` row and records what changed since the last snapshot in
`companies` / `company_events`. The column names are read from the downloaded file's header (never assumed); a missing
field stops the run. It carries no industry or size, so it is used as a *join* on discovered jobs, not as a source of jobs.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

from ..db import DB

log = logging.getLogger(__name__)

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

    def lookup(self, company: str, threshold: float = 0.92) -> "SponsorMatch":
        """The same answer shape as CompanyIndex.lookup (this older index knows no route or rating)."""
        name, score, method = self.match(company, threshold)
        return SponsorMatch(name, score, "none" if method == "none" else "exact" if method == "exact" else "fuzzy")




# ---------------------------------------------------------------- matching a job's company to the register
TRADING_AS = re.compile(r"\s+t/a\s+|\s+trading as\s+", re.I)


def name_parts(name: str) -> list[str]:
    """The normalised names a company goes by: "Acme Ltd T/A Widgets" -> ["acme", "widgets"] (both sides of T/A)."""
    return list(dict.fromkeys(n for n in (normalise(p) for p in TRADING_AS.split(name)) if n))


@dataclass
class SponsorMatch:
    name: str | None = None            # the register's organisation name
    score: float = 0.0
    method: str = "none"               # exact | fuzzy | none  (fuzzy is never promoted to exact)
    route: str | None = None
    rating: str | None = None
    town: str | None = None

    @property
    def label(self) -> str:
        """What is stored on the job: 'exact', 'fuzzy(0.95)' or 'none'."""
        return f"fuzzy({self.score:.2f})" if self.method == "fuzzy" else self.method


def _lookup(company: str, exact: dict[str, list[tuple]], by_first: dict[str, list[tuple[str, tuple]]],
            threshold: float) -> SponsorMatch:
    """Match on any side of T/A in `company` against any side of a register name. `exact` maps a normalised name to the
    register rows (name, town, rating, route) carrying it; `by_first` maps a first word to (normalised name, row)."""
    parts = name_parts(company)
    if not parts:
        return SponsorMatch()

    def pick(rows: list[tuple]) -> tuple:                      # A rating before B, then by name and town: deterministic
        return min(rows, key=lambda r: (r[2] != "A", r[0], r[1]))

    for p in parts:
        if p in exact:
            r = pick(exact[p])
            return SponsorMatch(r[0], 1.0, "exact", r[3], r[2], r[1])
    best, best_s = None, 0.0
    for p in parts:
        for norm, row in by_first.get(p.split(" ")[0], []):
            sc = SequenceMatcher(None, p, norm).ratio()
            if sc > best_s or (sc == best_s and best is not None and row < best):
                best, best_s = row, sc
    if best is not None and best_s >= threshold:
        return SponsorMatch(best[0], round(best_s, 4), "fuzzy", best[3], best[2], best[1])
    for p in parts:                                             # "Monzo" -> "Monzo Bank Ltd": one unambiguous extension
        if len(p) >= 5:
            pref = [row for norm, row in by_first.get(p.split(" ")[0], []) if norm.startswith(p + " ")]
            if len(set(pref)) == 1:
                r = pref[0]
                return SponsorMatch(r[0], 0.9, "fuzzy", r[3], r[2], r[1])
    return SponsorMatch(score=best_s)


class CompanyIndex:
    """The sponsors on today's register, from the `companies` table (rows not marked removed). Built only once a
    snapshot exists, so "register not loaded" means there is none."""

    def __init__(self, db: DB):
        self.exact: dict[str, list[tuple]] = {}
        self.by_first: dict[str, list[tuple[str, tuple]]] = {}
        self._n = 0
        for name, town, rating, route in db.conn.execute(
                "SELECT register_name, town, rating, route FROM companies WHERE removed_on IS NULL"):
            self._n += 1
            row = (name, town or "", rating or "", route or "")
            for part in name_parts(name):
                self.exact.setdefault(part, []).append(row)
                self.by_first.setdefault(part.split(" ")[0], []).append((part, row))

    def __len__(self) -> int:
        return self._n

    def lookup(self, company: str, threshold: float = 0.92) -> SponsorMatch:
        return _lookup(company, self.exact, self.by_first, threshold)


def has_snapshot(db: DB) -> bool:
    return db.conn.execute("SELECT 1 FROM register_snapshots LIMIT 1").fetchone() is not None


def load_index(db: DB) -> "CompanyIndex | SponsorIndex | None":
    """The index triage matches against: the snapshot-based one once `careerops sponsors` has run, else the older
    `sponsors` table when it was filled by other means, else None ("register not loaded")."""
    if has_snapshot(db):
        return CompanyIndex(db)
    return SponsorIndex(db) if db.sponsor_count() else None


# ---------------------------------------------------------------- M1: daily snapshots + diff
# Key = (normalise(name), normalise(town)): the name alone collides (two "Data Lab"s in different towns).
EXPECTED_FIELDS = {                 # field -> words its header must contain; the position is read from the file
    "name": ("organisation",), "town": ("town",), "county": ("county",), "rating": ("rating",), "route": ("route",),
}
RATING = re.compile(r"\(([AB])\s*rating\)", re.I)
DATE_IN_URL = re.compile(r"\d{4}-\d{2}-\d{2}")
Row = tuple[str, str, str, str, str]         # register_name, town, county, rating, route


class RegisterFormatError(RuntimeError):
    """The downloaded register does not have a field the pipeline needs."""


@dataclass
class Diff:
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    rating_changed: list[tuple[str, str, str, str]] = field(default_factory=list)   # norm, town, old, new


@dataclass
class Snapshot:
    day: str
    path: Path
    csv_url: str
    rows: int
    diff: Diff
    baseline: bool                  # the first snapshot ever: nothing to compare with


@dataclass
class Register:
    rows: dict[tuple[str, str], Row]
    duplicates: int = 0             # later rows with a key already seen (the first is kept)
    header: list[str] = field(default_factory=list)


def _columns(header: list[str]) -> dict[str, int]:
    """Column index of each expected field, found by the words in the file's own header. A field that is not there is an
    error naming it and showing the header the file really has."""
    cols = {}
    for name, needles in EXPECTED_FIELDS.items():
        i = next((i for i, h in enumerate(header) if all(n in h.lower() for n in needles)), None)
        if i is None:
            raise RegisterFormatError(f"the register has no '{' '.join(needles)}' field (needed for {name}); its header "
                                      f"is {header}")
        cols[name] = i
    return cols


def read_register(data: bytes, route: str | None = "Skilled Worker") -> Register:
    """Parse register CSV bytes (UTF-8, BOM tolerated) into {(norm, town_norm): (name, town, county, rating, route)},
    only rows whose route contains `route` (case-insensitive). County 'Not set' becomes ''; the rating is the letter in
    'Worker (A rating)'. Duplicate keys keep the first row and are counted."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    header = next(reader, None)
    if not header:
        raise RegisterFormatError("the register file is empty")
    cols = _columns(header)
    out: dict[tuple[str, str], Row] = {}
    dups = 0
    for rec in reader:
        if len(rec) <= max(cols.values()):
            continue
        if route and route.lower() not in rec[cols["route"]].lower():
            continue
        name, town = rec[cols["name"]].strip(), rec[cols["town"]].strip()
        key = (normalise(name), normalise(town))
        if not key[0]:
            continue
        if key in out:
            dups += 1
            continue
        county = rec[cols["county"]].strip()
        rating = rec[cols["rating"]].strip()
        m = RATING.search(rating)
        out[key] = (name, town, "" if county.lower() == "not set" else county, m.group(1).upper() if m else rating,
                    rec[cols["route"]].strip())
    if dups:
        log.warning("register: %d duplicate (name, town) rows ignored, the first of each kept", dups)
    return Register(out, dups, header)


def _read_bytes(path: Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return f.read()
    return path.read_bytes()


def load_snapshot(path: Path, route: str | None = "Skilled Worker") -> dict[tuple[str, str], tuple]:
    """{(norm, town_norm): (register_name, town, county, rating, route)} from a .csv or .csv.gz register file."""
    return read_register(_read_bytes(path), route).rows


def diff(prev: dict, cur: dict) -> Diff:
    """added = K₂∖K₁, removed = K₁∖K₂, rating_changed = {k ∈ K₁∩K₂ : r₁(k) ≠ r₂(k)}; every list sorted."""
    p, c = set(prev), set(cur)
    return Diff(added=sorted(c - p), removed=sorted(p - c),
                rating_changed=sorted((*k, prev[k][3], cur[k][3]) for k in p & c if prev[k][3] != cur[k][3]))


FLAGGED_STATUSES = ("triaged", "needs_review", "approved", "letter_pending")


def _flag_jobs(db: DB, register_name: str, flag: str) -> None:
    """Append `flag` (once) to the flags of this sponsor's jobs that are still in play. No commit: the caller's transaction."""
    marks = ",".join("?" * len(FLAGGED_STATUSES))
    for jid, flags in db.conn.execute(f"SELECT id, flags FROM jobs WHERE sponsor_name = ? AND status IN ({marks})",
                                      (register_name, *FLAGGED_STATUSES)).fetchall():
        fl = json.loads(flags or "[]")
        if flag not in fl:
            db.conn.execute("UPDATE jobs SET flags = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(fl + [flag]), datetime.now(timezone.utc).isoformat(timespec="seconds"), jid))


def apply_diff(db: DB, day: str, d: Diff, cur: dict, *, snapshot: tuple | None = None) -> None:
    """Upsert `companies`, stamp first_seen / last_seen / removed_on, write `company_events` and flag affected jobs, all in
    ONE transaction (with the `register_snapshots` row `snapshot` = (day, csv_url, path, rows) when given, so a crash never
    leaves half a day recorded). The first snapshot ever writes one `baseline` event instead of one `added` per sponsor.
    A day already recorded is a no-op."""
    with db.conn:
        if db.conn.execute("SELECT 1 FROM register_snapshots WHERE day = ?", (day,)).fetchone():
            return
        baseline = db.conn.execute("SELECT 1 FROM register_snapshots LIMIT 1").fetchone() is None

        def event(key, kind, detail=""):
            db.conn.execute("INSERT INTO company_events (norm, town_norm, day, kind, detail) VALUES (?,?,?,?,?)",
                            (*key, day, kind, detail))

        def upsert(key):
            name, town, county, rating, route = cur[key]
            db.conn.execute(
                "INSERT INTO companies (norm, town_norm, register_name, town, county, rating, route, first_seen, last_seen,"
                " removed_on) VALUES (?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(norm, town_norm) DO UPDATE SET "
                "register_name=excluded.register_name, town=excluded.town, county=excluded.county, "
                "rating=excluded.rating, route=excluded.route, last_seen=excluded.last_seen, removed_on=NULL",
                (*key, name, town, county, rating, route, day, day))

        if baseline:
            for key in cur:
                upsert(key)
            db.conn.execute("INSERT INTO company_events (norm, town_norm, day, kind, detail) VALUES ('', '', ?, 'baseline', ?)",
                            (day, str(len(cur))))
        else:
            known = {(n, t): removed for n, t, removed in db.conn.execute("SELECT norm, town_norm, removed_on FROM companies")}
            added = set(d.added)
            for key in d.added:
                restored = known.get(key) is not None             # on the register before, removed, now back
                upsert(key)
                event(key, "readded" if restored else "added", cur[key][0])
            db.conn.executemany("UPDATE companies SET last_seen = ? WHERE norm = ? AND town_norm = ? AND removed_on IS NULL",
                                [(day, *k) for k in cur if k not in added])
            for key in d.removed:
                name = db.conn.execute("SELECT register_name FROM companies WHERE norm=? AND town_norm=?", key).fetchone()[0]
                db.conn.execute("UPDATE companies SET removed_on = ? WHERE norm = ? AND town_norm = ?", (day, *key))
                event(key, "removed", name)
                _flag_jobs(db, name, f"Sponsor licence no longer on register (since {day})")
            for norm, town, old, new in d.rating_changed:
                db.conn.execute("UPDATE companies SET rating = ? WHERE norm = ? AND town_norm = ?", (new, norm, town))
                event((norm, town), "rating", f"{old}→{new}")
                if new != "A":
                    _flag_jobs(db, cur[(norm, town)][0], f"Sponsor rating now {new} (since {day})")
        if snapshot:
            db.conn.execute("INSERT INTO register_snapshots (day, csv_url, path, rows) VALUES (?,?,?,?)", snapshot)


def csv_date(url: str) -> tuple[str, bool]:
    """(YYYY-MM-DD from the file name, True) or (today UTC, False) when the name carries no date. The name pattern has
    changed before, so the date is looked for anywhere in the file name."""
    m = DATE_IN_URL.search(url.rsplit("/", 1)[-1])
    if m:
        return m.group(0), True
    log.warning("register: no date in %s, using today", url)
    return datetime.now(timezone.utc).date().isoformat(), False


def _previous(db: DB) -> dict[tuple[str, str], Row]:
    """Yesterday's register as the `companies` table holds it: the rows not marked removed."""
    return {(n, t): (name, town or "", county or "", rating or "", route or "") for n, t, name, town, county, rating, route
            in db.conn.execute("SELECT norm, town_norm, register_name, town, county, rating, route FROM companies "
                               "WHERE removed_on IS NULL")}


def take_snapshot(db: DB, settings) -> Snapshot | None:
    """Fetch the gov.uk page, find the CSV link and, only if that link (or its date) is not already in
    `register_snapshots`, download it, check its header, save it as <register_dir>/YYYY-MM-DD.csv.gz, diff it against the
    register as `companies` holds it and apply the diff. None = nothing new. Two GETs, to two different hosts."""
    cfg = settings["sponsors"]
    url = find_csv_url(cfg["page_url"])
    day, _ = csv_date(url)
    if db.conn.execute("SELECT 1 FROM register_snapshots WHERE csv_url = ? OR day = ?", (url, day)).fetchone():
        return None
    r = requests.get(url, headers=UA, timeout=300)
    r.raise_for_status()
    reg = read_register(r.content, cfg.get("route"))                     # a bad header stops here, before anything is saved
    dest = settings.path("register_dir") / f"{day}.csv.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wb") as f:
        f.write(r.content)
    baseline = not has_snapshot(db)
    d = diff({} if baseline else _previous(db), reg.rows)
    apply_diff(db, day, d, reg.rows, snapshot=(day, url, str(dest), len(reg.rows)))
    return Snapshot(day, dest, url, len(reg.rows), d, baseline)


def snapshot(db: DB, settings) -> Path | None:
    """`take_snapshot`, returning just the .csv.gz path (None when the register has not changed)."""
    snap = take_snapshot(db, settings)
    return snap.path if snap else None


def summary_line(snap: Snapshot | None, db: DB | None = None) -> str:
    """The one line `careerops sponsors` prints."""
    if snap is None:
        row = db.conn.execute("SELECT MAX(day) FROM register_snapshots").fetchone() if db else None
        return f"Register unchanged (already have {row[0]})" if row and row[0] else "Register unchanged"
    if snap.baseline:
        return f"{snap.day}: {snap.rows} Skilled Worker sponsors (first snapshot, baseline)"
    d = snap.diff
    return (f"{snap.day}: {snap.rows} Skilled Worker sponsors · +{len(d.added)} added · −{len(d.removed)} removed · "
            f"{len(d.rating_changed)} rating changes")
