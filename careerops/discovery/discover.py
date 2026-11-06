"""`careerops discover --ats | --reed | --adzuna | --gmail-alerts`: find open jobs and write each one as an inbox file.

Every source produces `Candidate`s. A candidate is kept when its title is in a target role family (settings roles.families) and its
place is the UK or remote-UK; it is dropped when its posting is closed or the same job (company + normalised title + place) is
already in the database, in the inbox / done / rejected folders, in the application archive (read only) or earlier in this run.
What is kept is written to paths.jds/inbox/<company>-<title>-<id>.txt with Company / Title / URL / Source headers (an existing file is
never overwritten), which `careerops live` then works through best-priority first. Nothing is added to the database here and nothing
is applied to.

Sources
  ats      Greenhouse / Lever / Ashby boards of the seed companies (seeds.py): the folder names of the application archive, then
           discover.sources, then register companies that are also one of those. A board is found by guessing tokens from the
           name (boards.py; every answer cached for 30 days); the boards named token by token in sources: are read as well
  reed     the Reed Jobseeker API (REED_API_KEY): search, then the full ad of every job worth keeping; agency posts are flagged
  adzuna   the Adzuna API: its descriptions are snippets, so each result is a LINK, resolved to the employer's own board; what cannot be
           resolved goes to the manual queue
  gmail    the job-alert emails of every source in config/sources.yaml that lists sender domains (LinkedIn, CV-Library, Indeed, Reed
           and the rest) through the read-only Gmail API; the same resolution; LinkedIn and Indeed pages are never fetched
  jobbank  Canada's Job Bank open-data CSV (jobbank.py): rows are only LISTED (no employer, no ad text), in listed_only.csv

The source registry (config/sources.yaml, registry.py) says which sources exist and how each is read; health.py checks each of them
after every run. Outside the selected region (the UK when none is chosen): a job whose place is another country (the ad's own country, else read from its place)
is not dropped and not written to the inbox either: it is PARKED, one row in paths.discover/parked.csv (company, title, place, link; at most
discover.listed_only.max_per_run a run), so the shortlist never offers it and `live` never meets it. Choose its region (`--region`) and the next run
writes it like any other ad. Inbox files from before this (`Status: listed_only`) are moved to paths.discover/parked/ by `careerops shortlist`
(park_inbox_listed). Jobs already applied to through AIApply (paths.aiapply_applied, company + title) are skipped as duplicates.

Resolving an alert (or an Adzuna link): the company's name gives up to four board tokens (boards.py), each asked of Greenhouse, Lever
and Ashby (answers, hits and misses, cached 30 days; a board found only through a shortened token must prove it is this company); the
alert's title is then matched to an open posting on the board (discover.title_match, 0.9, the same level words, and a place that agrees).
Nothing is ever requested from linkedin.com, indeed, cv-library or a Workday host (applykind.py): a Reed link goes to the Reed API, the
rest to the boards. What is found carries apply_url and apply_kind (greenhouse | lever | ashby | workday | employer_other |
aggregator_only); what is not goes to the manual queue with its apply_kind. Per source and per step, the report says how many
alerts were resolved.

v2.8: every alert that could not be resolved keeps a trace (what each step tried, why it failed, run.unresolved). The report names five of them when
board_link or board_known resolved none ("Why board_link and board_known resolved 0 ..."), five CV-Library alerts with the reason each stayed unresolved (a
CV-Library job page is "agency or board-only"), and the slug guesses and requests per provider (boards.py: at most discover.ats.max_guesses_per_provider a run).

The manual queue (paths.jds/manual_queue.csv, append-only, one row per link) lists what could not be resolved, for
`careerops live --clipboard`: the best discover.manual_queue_max (10) of a run by title relevance x S, the rest in
manual_queue_overflow.csv. Every title the role or place filter turned away is in paths.discover/filtered_<date>.csv with the reason (one
file per day, holding the titles first turned away that day: the weekly look for a wrongly filtered role).
Sponsorship is never a filter here: the output is ordered by freshness tier, then by priority P = F x S (priority.py). The tier counts
from the source's own posting time (freshness.py: Greenhouse first_published, Lever createdAt, Ashby publishedAt, Reed date, Adzuna
created, a Gmail alert's arrival time), else from the day discover first saw the job (firstseen.py). Old jobs are never dropped.
"""
from __future__ import annotations

import csv
import functools
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse

from ..apply.naming import folder_key
from ..db import DB
from . import applykind
from . import ats as ats_mod
from . import candidate as cand
from ..apply import decided as decided_mod
from ..apply import listing_keys as LK
from . import boards as boards_mod
from . import countries, firstseen, freshness, gates, gmail, intake, jobbank, manual, more_ats, priority, reed, registry as registry_mod, seeds as seeds_mod, workcountry
from . import region as region_mod
from . import workday as workday_mod
from . import careers as careers_mod
from . import dataset as dataset_mod
from . import register as sponsors
from .candidate import Candidate
from .http import Blocked, FetchError, Fetcher
from .seeds import Seed
from .sources import html_to_text

MANUAL_COLUMNS = ["ts", "source", "company", "title", "location", "link", "reason", "apply_kind"]
OVERFLOW_COLUMNS = [*MANUAL_COLUMNS, "score"]
FILTERED_COLUMNS = ["ts", "source", "company", "title", "location", "filter", "reason", "link"]
ALERT_EMAIL_RELEVANCE = 0.5        # an alert email nothing could be read from: ranked mid-queue, below a clear title and above a stray one
INTEREST = re.compile(r"data|machine learning|\bml\b|\bai\b|analy|scien|llm|nlp|statist|intelligence|model", re.I)
LISTED_COLUMNS = ["ts", "source", "country", "company", "title", "location", "posted", "salary", "ref", "link", "note"]
PARKED_COLUMNS = ["ts", "source", "country", "company", "title", "location", "posted", "link", "reason", "file"]
ALERT_SUBJECT = re.compile(r"job|vacanc|stellen|vacature|alert|match|recommend|new roles?|positions?|offres?|opportunit", re.I)
COMPANY_COLUMNS = ("company", "company_name", "employer", "employer_name", "organisation", "organization")
TITLE_COLUMNS = ("title", "job_title", "job title", "position", "role", "job")
SNIPPET_MAX_CHARS = 1500           # an Adzuna description shorter than this (or cut with an ellipsis) is a snippet, not the ad
TITLE_MATCH = 0.9                  # default of discover.title_match: similarity at which an alert's title is taken for a posting's title
COUNTERS = ("alerts", "seen", "role_filtered", "place_filtered", "closed", "already_decided", "duplicate", "written", "listed_only", "listed_parked", "listed_capped",
            "already_in_inbox", "bad_header", "resolved", "to_manual_queue", "to_manual_queue_overflow", "board_unverified", "possible_repost")
RESOLVED_BY = ("reed", "board_link", "board_known", "board_guessed")       # the steps that can resolve an alert (counted as resolved_via_<step>)
OLD_PROVIDERS = (*ats_mod.PROVIDERS, "workday")                 # the boards read before v2.6: the "before" of the v2.6 report lines


def role_reason(title: str) -> str:
    """Why a title is outside every role family, with the words that make it worth a second look ('mentions: data, scien')."""
    words = sorted({m.group(0).lower() for m in INTEREST.finditer(title or "")})
    return "no roles.families title pattern matches" + (f" (title mentions: {', '.join(words)})" if words else "")


def _finishing(fn):
    """A source step ends by writing what it collected: the manual queue rows, the filtered titles, the first-seen days."""
    @functools.wraps(fn)
    def run(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        finally:
            self.finish()
    return run


# ------------------------------------------------------------------ the run record
@dataclass
class Run:
    started: str = ""
    sources: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))      # by the step that found it (ats, gmail ...)
    by_source: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))    # by registry source id: new, listed_only, alerts
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)       # a source that could not be read at all (a lapsed Gmail sign-in, an error): the command exits 1
    written: list[tuple[Candidate, Path]] = field(default_factory=list)
    dry_run: bool = False
    tiers: list[int] = field(default_factory=lambda: [0, 0, 0, 0])      # the freshness tiers (0 <= 24 h ... 3 older) of the ads written
    no_job_subjects: dict[str, str] = field(default_factory=dict)      # alert source id -> the first subject of a mail it read no job from
    asked: list[str] = field(default_factory=list)                     # the source steps this run was asked for (ats, gmail_alerts ...): `shortlist` says when the alerts were last read
    refused: dict[str, int] = field(default_factory=dict)              # provider -> requests it refused this run (boards.BoardIndex.refused_counts)
    guesses: dict[str, dict[str, int]] = field(default_factory=dict)   # provider -> guesses / requests / cached / over_cap this run (boards.BoardIndex.guess_counts)
    guess_cap: int = 0                                                 # discover.ats.max_guesses_per_provider
    slug_rejected: dict = field(default_factory=dict)                  # {"count": n, "examples": [...]}: tokens never asked because they are not company names
    unresolved: list[dict] = field(default_factory=list)               # every link resolve_link could not resolve: what was tried and why not (unresolved_row)

    def count(self, source: str, what: str, n: int = 1) -> None:
        self.sources[source][what] += n

    def credit(self, source_id: str, what: str, n: int = 1) -> None:
        self.by_source[source_id][what] += n

    def new_jobs(self, source_id: str) -> int:
        """Ads written to the inbox plus rows listed for this source in this run."""
        c = self.by_source.get(source_id) or {}
        return int(c.get("new", 0)) + int(c.get("listed_only", 0))

    def as_json(self) -> dict:
        return {"ts": self.started, "dry_run": self.dry_run, "tiers": list(self.tiers), "failures": self.failures[:20], "sources": {s: dict(c) for s, c in self.sources.items()},
                "by_source": {s: dict(c) for s, c in self.by_source.items()}, "notes": self.notes[:50],
                "no_job_subjects": dict(self.no_job_subjects), "asked": list(self.asked), "refused": dict(self.refused),
                "guesses": {a: dict(v) for a, v in self.guesses.items()}, "guess_cap": self.guess_cap, "slug_rejected": dict(self.slug_rejected),
                "unresolved_total": len(self.unresolved), "unresolved_kinds": dict(Counter(r.get("category") or "?" for r in self.unresolved)),
                "unresolved": example_rows(self.unresolved),
                "written": [{"company": c.company, "title": c.title, "file": p.name} for c, p in self.written]}


# ------------------------------------------------------------------ what is already known
@dataclass
class Waiting:
    """An inbox file nobody has worked on yet, and when its ad was posted: what a newer copy of the same job on another board replaces."""
    path: Path
    name: str                  # the company name as this board spells it (folder_key)
    posted: str


class Known:
    """Jobs already seen: in the database, in the inbox / done / rejected folders, in the application archive and in this run.
    A job is the same when its company family (candidate.family_key: "Graphcore Early Careers" is "Graphcore"), normalised title and place
    agree; a place that is missing on either side matches any. The same job under another board's spelling of the employer is the same
    whatever its place says (the two boards of one employer write the place differently)."""

    def __init__(self):
        self.full: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)       # (company family, title) -> {place: where}
        self.spellings: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)  # (company family, title) -> {folder_key(company): where}
        self.waiting: dict[tuple[str, str], list[Waiting]] = defaultdict(list)     # inbox files still to be worked on
        self.notes: list[str] = []                                                 # what could not be read while loading
        self.keys: dict[str, str] = {}                                             # id: / ATS key / URL key of every file already filed -> where

    def add_keys(self, keys: Iterable[str], where: str) -> None:
        for k in keys:
            if k:
                self.keys.setdefault(k, where)

    def key_where(self, company: str, title: str, url: str | None, apply_url: str | None = None, location: str | None = None) -> str | None:
        """Where a file with the same id, ATS key or URL key already is (inbox, approved/<date>/, jds_done/, done, rejected), else None. The
        fingerprint (company + title + country) is not used here: it only ever labels a possible repost."""
        jid = manual.job_id_for(company, title, url)
        lk = LK.keys_for(company, title, url, apply_url, location)
        return next((self.keys[k] for k in (f"id:{jid}", *lk.strong()) if k in self.keys), None)

    def add(self, company: str, title: str, location: str, where: str, *, path: Path | None = None, posted: str = "") -> None:
        c, t, p = cand.dedupe_key(company, title, location)
        if c and t:
            self.full[(c, t)].setdefault(p, where)
            self.spellings[(c, t)].setdefault(folder_key(company), where)
            if path is not None:
                self.waiting[(c, t)].append(Waiting(path, folder_key(company), posted))

    def where(self, company: str, title: str, location: str) -> str | None:
        c, t, p = cand.dedupe_key(company, title, location)
        places = self.full.get((c, t))
        if not places:
            return None
        if p in places:
            return places[p]
        if p == "" or "" in places:
            return next(iter(places.values()))
        return next((w for k, w in self.spellings[(c, t)].items() if k != folder_key(company)), None)

    def older_copy(self, company: str, title: str, posted: str) -> Waiting | None:
        """An inbox file of the same job under another board's name that was posted before `posted` (both times must be read): the copy a
        newer one replaces."""
        c, t, _ = cand.dedupe_key(company, title, "")
        mine, new = folder_key(company), freshness.parse(posted)
        if new is None:
            return None
        for w in self.waiting.get((c, t), []):
            old = freshness.parse(w.posted)
            if w.name != mine and old is not None and old < new:
                return w
        return None

    def forget(self, w: Waiting) -> None:
        for lst in self.waiting.values():
            lst[:] = [x for x in lst if x is not w]


def _header_of(path: Path) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(10))
    except OSError:
        return {}
    return manual.header_fields(head)


def mark_duplicate(path: Path, of_name: str) -> None:
    """Add `Duplicate of: <of_name>` to the header block of an inbox file this discovery wrote. The ad below it is not touched. (Written in place:
    discovery code deletes and moves nothing; a file cut short by a crash is rejected by `live` as too short.)"""
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n\n")
    if re.search(r"^Duplicate of:", head, re.M):
        return
    path.write_text(f"{head}\nDuplicate of: {of_name}{sep}{body}", encoding="utf-8", newline="\n")


def mark_repost(path: Path, label: str) -> None:
    """Add `Possible repost: <label>` to the header block of an inbox file this discovery wrote (v2.5: only the company + title + country of a job you
    decided matched; it is shown to you with this label, never skipped on that alone). The ad below it is not touched."""
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n\n")
    if re.search(r"^Possible repost:", head, re.M):
        return
    path.write_text(f"{head}\nPossible repost: {label}{sep}{body}", encoding="utf-8", newline="\n")


def archive_pairs(root: Path | Iterable[Path]) -> list[tuple[str, str]]:
    """(company folder, role folder) names in the application archive (one folder, or several: `Settings.archive_roots()`). READ ONLY:
    directory listings, nothing is opened, written, renamed or moved. A role folder 'AI Engineer' or a version folder below it ('v2')
    both name the role."""
    out: list[tuple[str, str]] = []
    for one in ([root] if isinstance(root, (str, os.PathLike)) else root):
        try:
            companies = [e for e in os.scandir(one) if e.is_dir()]
        except OSError:
            continue
        for c in companies:
            try:
                for r in os.scandir(c.path):
                    if r.is_dir():
                        out.append((c.name, r.name))
            except OSError:
                continue
    return out


def load_known(settings, db: DB) -> Known:
    k = Known()
    for j in db.jobs():
        k.add(j["company"], j["title"], j["location"] or "", "already in the database")
    jds = settings.path("jds")
    for sub in ("inbox", "done", "rejected"):
        d = jds / sub
        if d.is_dir():
            for p in d.glob("*.txt"):
                h = _header_of(p)
                if h.get("company") and h.get("title"):
                    waits = sub == "inbox" and not h.get("duplicate_of")
                    k.add(h["company"], h["title"], h.get("location", ""), f"already in jds/{sub}",
                          path=p if waits else None, posted=h.get("posted", ""))
    for d in decided_mod.approved_dirs(settings):                   # approved at the shortlist, not yet filed: as good as in the inbox
        for p in d.glob("*.txt"):
            h = _header_of(p)
            if h.get("company") and h.get("title"):
                k.add(h["company"], h["title"], h.get("location", ""), f"already in jds/approved/{d.name}")
    for where, folder in file_folders(settings):
        for p in folder.glob("*.txt"):
            jid = decided_mod.file_job_id(p)
            k.add_keys(([f"id:{jid}"] if jid else []) + decided_mod.file_keys(p).strong(), where)
    for company, role in archive_pairs(settings.archive_roots()):
        k.add(company, role, "", "already applied (application archive)")
    add_aiapply(settings, k)
    return k


def file_folders(settings) -> list[tuple[str, Path]]:
    """Every folder a JD file can already sit in: the inbox, done/, rejected/, approved/<date>/ and each folder of jds_done/ (skipped, applied, closed)."""
    jds, out = settings.path("jds"), []
    out += [(f"already in jds/{sub}", jds / sub) for sub in ("inbox", "done", "rejected")]
    out += [(f"already in jds/approved/{d.name}", d) for d in decided_mod.approved_dirs(settings)]
    try:
        out += [(f"already in jds_done/{d.name}", d) for d in sorted(settings.path("jds_done").iterdir()) if d.is_dir()]
    except OSError:
        pass
    return [(w, f) for w, f in out if f.is_dir()]


def read_aiapply(path: Path) -> tuple[list[tuple[str, str]], str]:
    """(company, title) of every row of the AIApply export (paths.aiapply_applied), and a note when it cannot be used. The column
    names are matched case-insensitively (company / employer / ..., title / job title / position / ...). READ ONLY."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rdr = csv.DictReader(f)
            names = {(n or "").strip().lower(): n for n in rdr.fieldnames or []}
            c_col = next((names[n] for n in COMPANY_COLUMNS if n in names), None)
            t_col = next((names[n] for n in TITLE_COLUMNS if n in names), None)
            if not (c_col and t_col):
                return [], (f"{path.name}: no company and title columns (found: {', '.join(names) or 'none'}); "
                            "nothing was skipped because of it")
            return [((r.get(c_col) or "").strip(), (r.get(t_col) or "").strip()) for r in rdr], ""
    except FileNotFoundError:
        return [], ""
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        return [], f"{path.name} cannot be read ({type(e).__name__}); nothing was skipped because of it"


def add_aiapply(settings, k: Known) -> int:
    """Jobs AIApply applied to (company + normalised title, any place) count as already applied. Returns how many rows were read."""
    key = (settings.raw.get("paths") or {}).get("aiapply_applied")
    if not key:
        return 0
    pairs, note = read_aiapply(settings.path("aiapply_applied"))
    if note:
        k.notes.append(note)
    for company, title in pairs:
        k.add(company, title, "", "already applied (AIApply)")
    return len(pairs)


# ------------------------------------------------------------------ the manual queue (append-only)
def manual_queue_path(settings) -> Path:
    return settings.path("jds") / "manual_queue.csv"


def overflow_path(settings) -> Path:
    return settings.path("jds") / "manual_queue_overflow.csv"


def filtered_path(settings, day: str | None = None) -> Path:
    """The role / place filter audit of one day: paths.discover/filtered_<YYYY-MM-DD>.csv (today's when `day` is not given)."""
    return settings.path("discover") / f"filtered_{day or datetime.now(timezone.utc).date().isoformat()}.csv"


def filtered_seen(settings) -> set[tuple]:
    """(company, title, filter) of every row of every audit file so far, the older single filtered.csv included."""
    out: set[tuple] = set()
    folder = settings.path("discover")
    for p in sorted(folder.glob("filtered*.csv")) if folder.exists() else []:
        out |= {(r.get("company"), r.get("title"), r.get("filter")) for r in read_rows(p)}
    return out


def queue_key(row: dict) -> str:
    return row.get("link") or f"{row.get('company')}|{row.get('title')}"


def append_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Append rows to a CSV (header written once); never changes a row that is there. A file written before a column existed gets the
    column added to its header (its old rows keep an empty cell) so that the new rows line up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = columns
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            head = next(csv.reader(f), [])
        if head and head != columns:
            fields = head if set(columns) <= set(head) else [*head, *[c for c in columns if c not in head]]
            if fields != head:
                old = read_rows(path)                       # all of it is in memory before the file is opened for writing
                with open(path, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def queue_manual(settings, source: str, company: str, title: str, location: str, link: str, reason: str,
                 *, now: datetime | None = None) -> bool:
    """Append a row to paths.jds/manual_queue.csv unless that link (or, without one, that company + title) is already in it.
    True when a row was added. (A discover run does not call this per row: it ranks the run's rows and writes the best ones.)"""
    path = manual_queue_path(settings)
    row = {"ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "source": source, "company": company,
           "title": title, "location": location, "link": link, "reason": reason,
           "apply_kind": applykind.kind_of(link) or "aggregator_only"}
    if queue_key(row) in {queue_key(r) for r in read_manual_queue(path)}:
        return False
    append_rows(path, MANUAL_COLUMNS, [row])
    return True


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_manual_queue(path: Path) -> list[dict]:
    return read_rows(path)


# ------------------------------------------------------------------ boards: cache, lookup
class BoardCache:
    """Posting lists per board kept in paths.discover/boards/<ats>-<slug>.json for discover.cache_hours. (Which tokens are boards at all
    is boards.BoardIndex: paths.discover/boards.json, hits and misses, discover.ats.board_cache_days.)"""

    def __init__(self, settings, fetcher: Fetcher, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.dir = settings.path("discover")
        self.settings = settings
        self.hours = float(settings["discover"].get("cache_hours", 6))
        self.fetcher, self.clock = fetcher, clock
        self.memory: dict[tuple[str, str], list[ats_mod.Posting]] = {}

    def postings(self, board: ats_mod.Board) -> list[ats_mod.Posting]:
        k = (board.ats, board.slug)
        if k in self.memory:
            return self.memory[k]
        path = self.dir / "boards" / f"{board.ats}-{board.slug}.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if self.clock() - datetime.fromisoformat(d["fetched"]) < timedelta(hours=self.hours) and d.get("name") == board.name:
                self.memory[k] = [ats_mod.Posting(**p) for p in d["postings"]]
                return self.memory[k]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if board.ats == "workday":                         # only the role family and place that match are read in full (one request each)
            def keep(p: ats_mod.Posting) -> bool:
                c = posting_candidate(board, p)
                return cand.family_of(c, self.settings) is not None and cand.uk_ok(c, self.settings)
            posts = workday_mod.board_postings(board, self.fetcher, keep=keep, today=self.clock().date(),
                                               **workday_mod.options_for(self.settings, board))
        elif board.ats in more_ats.SINGLE:                 # SmartRecruiters / Eploy lists have no text: the postings in a role family (and a place, when known) are read in full
            def keep_more(p: ats_mod.Posting) -> bool:
                c = posting_candidate(board, p)
                return cand.family_of(c, self.settings) is not None and (not p.location or cand.uk_ok(c, self.settings))
            posts = ats_mod.board_postings(board, self.fetcher, keep=keep_more)
        else:
            posts = ats_mod.board_postings(board, self.fetcher)
        self.put(board, posts)
        return posts

    def put(self, board: ats_mod.Board, posts: list[ats_mod.Posting]) -> None:
        """Keep a posting list that was just fetched (in memory, and on disk for discover.cache_hours)."""
        self.memory[(board.ats, board.slug)] = posts
        path = self.dir / "boards" / f"{board.ats}-{board.slug}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched": self.clock().isoformat(), "name": board.name,
                                        "postings": [p.__dict__ for p in posts]}), encoding="utf-8")
        except OSError:
            pass


class BoardLookup:
    """Which board a company's jobs are on, and the posting on it that matches a title: the boards you listed, the register
    boards `ats.resolve` verified, and, for any other company an alert or Adzuna names, boards found from its name the way
    `discover --ats` finds them (boards.find: up to four tokens, each asked of Greenhouse, Lever and Ashby, every answer cached for
    discover.ats.board_cache_days). A board found only by a guessed token is held to a stricter match (see plausible())."""

    def __init__(self, settings, db: DB, fetcher: Fetcher, cache: BoardCache, probes: int | None = None,
                 extra_boards: list[ats_mod.Board] | None = None, index: boards_mod.BoardIndex | None = None):
        self.settings, self.fetcher, self.cache = settings, fetcher, cache
        self.index = index or boards_mod.BoardIndex(settings)
        self.boards = merge_boards(ats_mod.all_boards(settings, db), extra_boards or [])
        self.by_name: dict[str, list[ats_mod.Board]] = defaultdict(list)
        for b in self.boards:
            self.by_name[sponsors.normalise(b.name)].append(b)
            self.by_name["".join(sponsors.normalise(b.slug.replace("-", " ")).split())].append(b)
        d = settings["discover"]
        self.probes = int(d.get("alert_probes_per_run", 100)) if probes is None else probes
        self.probes_allowed = self.probes
        self.title_match = float(d.get("title_match", TITLE_MATCH))
        self.guessed: dict[str, list[ats_mod.Board]] = {}            # company (normalised) -> boards found from its name, this run
        self.findings: list[boards_mod.Finding] = []
        self.not_looked: set[str] = set()                            # companies left alone because the per-run limit was reached
        self.names: dict[tuple[str, str], str | None] = {}
        self.link_note = ""                                          # why the last find_by_link found nothing (the trace of an unresolved alert)

    def boards_for(self, company: str) -> list[ats_mod.Board]:
        norm = sponsors.normalise(company)
        found = list(self.by_name.get(norm, [])) or list(self.by_name.get("".join(norm.split()), []))
        if found or not norm:
            return found
        if norm in self.guessed:
            return self.guessed[norm]
        if self.probes <= 0:
            if self.probes_allowed:
                self.not_looked.add(norm)
            return []
        self.probes -= 1
        finding = boards_mod.find(Seed(company, "alert"), self.fetcher, self.index, on_postings=lambda b, posts: self.cache.put(b, posts))
        self.findings.append(finding)
        if not finding.tokens:                                       # every token was rejected as not a company name: nothing was asked, so it costs no probe
            self.probes += 1
        self.guessed[norm] = finding.boards
        return finding.boards

    def find_by_link(self, company: str, title: str, location: str, link: str) -> tuple[ats_mod.Board, ats_mod.Posting] | None:
        """The employer's own posting an alert's LINK points to, when the link is on a board this tool reads (more_ats.link_board: Workable, SmartRecruiters,
        Teamtailor, Recruitee, Personio, Pinpoint, Workday, and the Greenhouse / Lever / Ashby addresses): the board is named by the link (no slug is guessed, so
        Pinpoint works too), the posting is the one whose id is in the link, else (a link to the board only) the one titled like the alert. Nothing is requested
        from a linkedin / indeed / cv-library address (they name no board). None when the link names no such board or the posting is not on it any more."""
        self.link_note = ""
        lk = more_ats.link_board(link)
        if lk is None or applykind.host_of(link).endswith(applykind.NEVER_FETCH[:4]):
            host = applykind.host_of(link) or "no host"
            self.link_note = (f"{host} is never fetched by this tool, and the link names no employer board" if applykind.never_fetched(link)
                              else f"{host} is not the address of a board this tool reads (Greenhouse, Lever, Ashby, Workable, Teamtailor ...)")
            return None
        board = ats_mod.Board(lk.ats, lk.slug, company or "", "link")
        try:
            posts = self.cache.postings(board)
        except (FetchError, Blocked) as e:
            self.link_note = f"reading the {lk.ats}/{lk.slug} board the link names failed: {e}"
            return None
        want = cand.norm_title(title)
        post = next((p for p in posts if p.listed and more_ats.matches(p, lk)), None)
        if post is None and not lk.ref:
            scored = [(SequenceMatcher(None, want, cand.norm_title(p.title)).ratio(), p) for p in posts if p.listed and cand.places_agree(location, p.location)]
            scored = [t for t in scored if t[0] >= self.title_match and cand.level_words(title) == cand.level_words(t[1].title)]
            post = max(scored, key=lambda t: t[0])[1] if scored else None
        if post is None:
            self.link_note = f"the {lk.ats}/{lk.slug} board the link names was read; no open posting has the link's id or the alert's title"
            return None
        if not post.description and lk.ats in ("smartrecruiters", "eploy"):
            more_ats.fill(lk.ats, lk.slug, post, self.fetcher)
        return board, post

    def explain(self, company: str) -> tuple[str, str]:
        """(category, sentence) for a company whose alert found no posting: what board_known and board_guessed did, read from the state this lookup already
        holds (no request). Categories: no_company | no_posting | not_looked | board_unverified | not_a_company | not_listed | guess_limit | provider_error | no_board."""
        norm = sponsors.normalise(company)
        if not norm:
            return "no_company", "board_known / board_guessed: the alert names no company"
        known = list(self.by_name.get(norm, [])) or list(self.by_name.get("".join(norm.split()), []))
        if known:
            b = known[0]
            return "no_posting", (f"board_known: the {b.ats}/{b.slug} board (listed in your settings or sources.yaml, or verified from the register) was read; "
                                  "no open posting is titled like the alert in the same place")
        guessed = self.guessed.get(norm)
        if guessed:
            b = guessed[0]
            return "no_posting", f"board_guessed: the {b.ats}/{b.slug} board was found from the name; no open posting is titled like the alert in the same place"
        if norm in self.not_looked:
            return "not_looked", (f"board_guessed: not tried, this run's limit of {self.probes_allowed} compan(ies) (discover.alert_probes_per_run) was reached")
        known_text = f"board_known: no board is listed for '{company}' (settings, sources.yaml, the register)"
        f = next((x for x in self.findings if sponsors.normalise(x.seed.name) == norm), None)
        if f is None:
            return "no_board", known_text + "; board_guessed: nothing was asked"
        cat = ("board_unverified" if f.unverified else "not_a_company" if f.rejected and not f.tokens else "not_listed" if getattr(f, "not_listed", False)
               else "guess_limit" if f.capped else "provider_error" if f.errors else "no_board")
        return cat, known_text + "; board_guessed: " + f.tried_text()

    def board_name(self, board: ats_mod.Board) -> str | None:
        """The company name a Greenhouse board gives itself (one request, kept for the run); None when it cannot be read."""
        k = (board.ats, board.slug)
        if k not in self.names:
            try:
                data = self.fetcher.json(f"{ats_mod.GH}/{board.slug}", what=f"Greenhouse board {board.slug}")
                self.names[k] = str(data["name"]) if isinstance(data, dict) and data.get("name") else None
            except (FetchError, Blocked):
                self.names[k] = None
        return self.names[k]

    def plausible(self, board: ats_mod.Board, company: str, location: str, post: ats_mod.Posting) -> bool:
        """A board named by you or verified against the register is the company's. A board found only because its token looked like the
        company's name might be another company's (token `arc`), so a Greenhouse board's own name must also be the company's (or start
        with it). (A shortened token has already been through boards.verify_shortened.)"""
        if board.origin != "alert":
            return True
        if board.ats == "greenhouse":
            name = self.board_name(board)
            if name:
                bn, cn = sponsors.normalise(name).split(), sponsors.normalise(company).split()
                if bn and cn and bn[: len(cn)] != cn and cn[: len(bn)] != bn:
                    return False
        return True

    def find(self, company: str, title: str, location: str = "") -> tuple[ats_mod.Board, ats_mod.Posting] | None:
        """The open posting on the company's board whose title is the title (same normalised title, or >= discover.title_match similar
        with the same level words: "Data Scientist" is not "Senior Data Scientist") and whose place agrees with the alert's (cand.places_agree:
        a posting in another town is another job), preferring the one in the same place. None when the company has no known board or the
        board has no such posting."""
        want = cand.norm_title(title)
        for board in self.boards_for(company):
            try:
                posts = self.cache.postings(board)
            except (FetchError, Blocked):
                continue
            scored = []
            for p in posts:
                if not p.listed:
                    continue
                s = SequenceMatcher(None, want, cand.norm_title(p.title)).ratio()
                if (s >= self.title_match and cand.level_words(title) == cand.level_words(p.title)
                        and cand.places_agree(location, p.location) and self.plausible(board, company, location, p)):
                    here = bool(location) and cand.norm_place(location) != "" and cand.norm_place(location) == cand.norm_place(p.location)
                    scored.append((s, here, p))
            if scored:
                best = max(scored, key=lambda t: (t[1], t[0]))
                return board, best[2]
        return None


def merge_boards(boards: list[ats_mod.Board], extra: list[ats_mod.Board]) -> list[ats_mod.Board]:
    """`boards`, then the `extra` ones that are not among them (same provider and token); the earlier name wins."""
    seen, out = set(), []
    for b in [*boards, *extra]:
        if (b.ats, b.slug) not in seen:
            seen.add((b.ats, b.slug))
            out.append(b)
    return out


POSTED_FIELD = {"greenhouse": "first_published", "lever": "createdAt", "lever_eu": "createdAt", "ashby": "publishedAt", "workday": "postedOn",
                "workable": "published_on", "smartrecruiters": "releasedDate", "teamtailor": "pubDate", "recruitee": "published_at", "personio": "createdAt",
                "eploy": "datePosted"}


def posting_candidate(board: ats_mod.Board, p: ats_mod.Posting, origin: str = "") -> Candidate:
    """A board posting as a candidate, with the board's own posting time and where it comes from (freshness.py)."""
    posted = p.posted_at or ""
    return Candidate(source=board.ats.replace("lever_eu", "lever"), company=board.name or p.company, title=p.title,
                     location=p.location, url=p.url, description=p.description, posted=posted, ext_id=p.ext_id,
                     posted_source=freshness.label(board.ats.replace("lever_eu", "lever"), POSTED_FIELD[board.ats])
                     if posted and board.ats in POSTED_FIELD and freshness.is_iso(posted) else "",
                     country=p.country, origin=origin, board=f"{board.ats}:{board.slug}", apply_url=p.url,
                     apply_kind=applykind.board_kind(board.ats))


# ------------------------------------------------------------------ the pipeline for one candidate
class Discover:
    def __init__(self, settings, db: DB, fetcher: Fetcher, *, fb: dict | None = None, dry_run: bool = False, limit: int | None = None,
                 today: datetime | None = None, say: Callable[[str], None] = print, regions: Sequence[str] | None = None):
        self.s, self.db, self.f, self.say, self.dry_run = settings, db, fetcher, say, dry_run
        self.regions = tuple(regions) if regions is not None else None             # the selected region keys; None = the old UK gate
        self.region_tally: dict[tuple, str] = {}                                   # one job -> its latest verdict (a Reed job is asked twice)
        self.today = today or datetime.now(timezone.utc)
        self.limit = limit if limit is not None else int(settings["discover"].get("max_new_per_run", 40))
        self.known = load_known(settings, db)
        self.decided = decided_mod.load(settings, db)                  # skipped / applied jobs: their ids and duplicate keys (decided.py)
        self.run = Run(started=self.today.isoformat(timespec="seconds"), dry_run=dry_run)
        self.run.notes += self.known.notes
        self.fb = fb
        self.registry = registry_mod.load(settings)
        if self.regions is not None:                                  # a source that is for other regions only does not run (its alerts are not read, its boards not asked)
            left = [s for s in self.registry if not region_mod.source_runs(s.region_tags, self.regions)]
            if left:
                self.registry = registry_mod.Registry([s for s in self.registry if s not in left])
                if any(s.enabled for s in left):
                    self.run.notes.append(f"not run for region {','.join(self.regions)}: " + ", ".join(f"{s.id} ({'/'.join(s.region_tags)})" for s in left if s.enabled))
        self.cache = BoardCache(settings, fetcher, clock=lambda: self.today)          # "Posted Today" is relative to the run's day
        self.index = boards_mod.BoardIndex(settings)                 # one for the run: boards.json is written whole
        self.index.dataset = dataset_mod.Dataset(settings, fetcher)  # the public board list (dataset.py); read on first use, off with discover.dataset.enabled: false
        self.lookup = BoardLookup(settings, db, fetcher, self.cache, extra_boards=self.registry.boards(), index=self.index)
        self.first_seen = firstseen.FirstSeen(settings, persist=not dry_run)
        self.today_day = firstseen.day(self.today)
        self.pending_queue: list[dict] = []                          # unresolved links of the run, ranked and written by flush_queue()
        self.pending_filtered: list[dict] = []                       # titles the filters turned away, written by flush_filtered()
        self._sponsor_index: object = False                          # loaded on first use (False = not yet)
        self.inbox = settings.path("jds") / "inbox"
        self.new_files = 0
        self.listed_files = 0
        lo = settings["discover"].get("listed_only") or {}
        self.listed_enabled = bool(lo.get("enabled", True))
        self.listed_cap = int(lo.get("max_per_run", 20))
        self.parked_keys = parked_keys(settings)                     # the ads already parked (an ad outside the region is parked once, not every run)
        self.findings: list[boards_mod.Finding] = []

    # ---- filters, in the order that spends least first
    def wanted(self, c: Candidate, label: str) -> bool:
        """Role family and place; no network, no counters for the dedupe (a job costs a details request only after this)."""
        if cand.family_of(c, self.s) is None:
            self.run.count(label, "role_filtered")
            self.filtered_out(c.source, c.company, c.title, c.location, c.url, "role", role_reason(c.title))
            return False
        if self.regions is not None:
            m = region_mod.match(c.location, c.description, self.regions, country=c.country)
            self.region_tally[(c.company, c.title, c.location, c.url)] = m.verdict
            if m.verdict != region_mod.OUT:
                return True                                           # in, or unknown (kept, flagged `region?` in the shortlist: missing data is no penalty)
        elif cand.uk_ok(c, self.s):
            return True
        elsewhere = self.elsewhere(c)
        if elsewhere and self.listed_enabled:
            c.country, c.listed_only = elsewhere, True            # outside the UK: written for the record, never applied to
            return True
        self.run.count(label, "place_filtered")
        where = f"in {elsewhere}, and discover.listed_only is off" if elsewhere else f"{c.location or c.country or 'no place given'}"
        self.filtered_out(c.source, c.company, c.title, c.location, c.url, "place",
                          (f"not in region {','.join(self.regions)} ({where})" if self.regions is not None else f"not in the UK or remote UK ({where})"))
        return False

    def filtered_out(self, source: str, company: str, title: str, location: str, link: str, which: str, reason: str) -> None:
        """Note a title the role or place filter turned away (written to paths.discover/filtered_<date>.csv by flush_filtered)."""
        self.pending_filtered.append({"ts": self.today.isoformat(timespec="seconds"), "source": source, "company": company, "title": title,
                                      "location": location, "filter": which, "reason": reason, "link": link})

    def region_counts(self) -> Counter:
        """in / out / unknown over every job this run asked about (a job once, by its latest verdict)."""
        c = Counter({region_mod.IN: 0, region_mod.OUT: 0, region_mod.UNKNOWN: 0})
        c.update(self.region_tally.values())
        return c

    @staticmethod
    def elsewhere(c: Candidate) -> str:
        """The country a candidate that is not in the UK is in (the ad's own country, else read from its place), '' when unknown."""
        country = countries.canon(c.country) or countries.detect(c.location)
        return "" if country in ("", countries.UK) else country

    def already_decided(self, company: str, title: str, url: str | None, label: str, location: str | None = None) -> bool:
        """Is this job (by its id, ATS key or URL key: apply/decided.py, v2.5) one you skipped or applied to? Then it is not written again:
        `already_decided`. Company + title alone never decide."""
        if self.decided.hit(company, title, url, location):
            self.run.count(label, "already_decided")
            return True
        return False

    def duplicate(self, c: Candidate, label: str) -> bool:
        """Seen before? Not when it is a NEWER copy of an inbox file (the same job on another board of the employer): that one replaces it.
        A job you already decided on (skipped or applied, any copy of it) is counted as `already_decided`, not as a duplicate."""
        if self.already_decided(c.company, c.title, c.url, label, c.location):
            return True
        if self.known.key_where(c.company, c.title, c.url, c.apply_url, c.location):       # the same id / ATS key / URL in inbox, approved/ or jds_done/
            self.run.count(label, "duplicate")
            return True
        where = self.known.where(c.company, c.title, c.location)
        if where and self.known.older_copy(c.company, c.title, c.posted) is None:
            self.run.count(label, "duplicate")
            return True
        return False

    def supersede(self, older: Waiting, new_path: Path, label: str) -> None:
        """Keep the newer copy: the older inbox file gets a `Duplicate of: <newer file>` header (`careerops live` does not work on it). Its ad is
        not changed. A dry run only counts."""
        self.run.count(label, "superseded")
        self.known.forget(older)
        if self.dry_run:
            return
        try:
            mark_duplicate(older.path, new_path.name)
        except OSError as e:
            self.run.notes.append(f"{older.path.name}: could not be marked as a duplicate of {new_path.name} ({type(e).__name__})")

    def accept(self, c: Candidate, label: str, counted: bool = False) -> str:
        """Run one candidate through the filters and write it. Returns 'written', 'exists', 'dropped' (filtered / closed /
        duplicate) or 'full' (the per-run limit of new files is reached). `counted`: the caller already counted it as seen."""
        if not counted:
            self.run.count(label, "seen")
        if not self.wanted(c, label):
            return "dropped"
        if c.closed:
            self.run.count(label, "closed")
            return "dropped"
        older = self.known.older_copy(c.company, c.title, c.posted)
        if self.duplicate(c, label):
            return "dropped"
        c.first_seen = self.first_seen.record(c.company, c.title, c.location, firstseen.day(c.seen) or self.today_day)
        c.open_listing = bool(c.board)                         # read off an employer's own board just now: still open
        if c.listed_only:
            if self.listed_files >= self.listed_cap:           # its own limit: a flood of non-UK ads never crowds out UK ones
                self.run.count(label, "listed_capped")
                return "dropped"
        elif self.new_files >= self.limit:
            return "full"
        if len(c.description.encode("utf-8")) < manual.MIN_JD_BYTES:
            self.run.count(label, "needs_full_text")
            return "dropped"
        if c.listed_only:
            return self.park_listed(c, label)
        if self.dry_run:
            would = self.inbox / intake.file_name(c.to_ad())
            self.known.add(c.company, c.title, c.location, "found earlier in this run", path=would, posted=c.posted)
            if older is not None:
                self.supersede(older, would, label)
            self.wrote(c, label)
            self.run.written.append((c, would))
            return "written"
        try:
            path, created = intake.write_inbox(self.inbox, c.to_ad())
        except intake.IntakeError as e:
            self.run.count(label, "bad_header")
            self.run.notes.append(f"{c.company} | {c.title}: {e}")
            return "dropped"
        self.known.add(c.company, c.title, c.location, "found earlier in this run", path=path, posted=c.posted)
        if not created:
            self.run.count(label, "already_in_inbox")
            return "exists"
        if older is not None:
            self.supersede(older, path, label)
        repost = self.decided.repost(c.company, c.title, c.url, c.location)
        if repost:
            self.run.count(label, "possible_repost")
            try:
                mark_repost(path, repost)
            except OSError as e:
                self.run.notes.append(f"{path.name}: could not be labelled a possible repost ({type(e).__name__})")
        self.wrote(c, label)
        self.run.written.append((c, path))
        return "written"

    def wrote(self, c: Candidate, label: str) -> None:
        """Count one new inbox ad against the run's limit and credit its registry source."""
        self.new_files += 1
        self.run.count(label, "written")
        src = self.registry.attribute(c)
        self.run.credit(src.id if src else "other", "new")

    def park_listed(self, c: Candidate, label: str) -> str:
        """An ad outside the selected region (the UK when no region is chosen) is parked, not written: one row in paths.discover/parked.csv, counted
        against its own per-run limit (the inbox, the shortlist and `live` never see it). An ad already parked is a duplicate. A dry run counts only."""
        key = parked_key(c.company, c.title, c.location, c.url)
        if key in self.parked_keys:
            self.run.count(label, "duplicate")
            return "dropped"
        self.parked_keys.add(key)
        if not self.dry_run:
            where = f"region {','.join(self.regions)}" if self.regions is not None else "the UK"
            append_rows(parked_path(self.s), PARKED_COLUMNS,
                        [{"ts": self.today.isoformat(timespec="seconds"), "source": c.source, "country": c.country, "company": c.company, "title": c.title,
                          "location": c.location, "posted": c.posted, "link": c.url, "reason": f"outside {where}: parked, not written to the inbox", "file": ""}])
        self.listed_files += 1
        self.run.count(label, "listed_only")
        self.run.count(label, "listed_parked")
        src = self.registry.attribute(c)
        self.run.credit(src.id if src else "other", "listed_only")
        return "parked"

    def to_queue(self, label: str, source: str, company: str, title: str, location: str, link: str, reason: str,
                 relevance: float | None = None, seen_at: str = "", apply_kind: str = "") -> None:
        """Hold a link that could not be resolved. flush_queue() ranks the run's links like the inbox: the freshness tier of the alert's
        arrival first (freshness.py; no time = tier 3), then title relevance x S (the stand-in for P = F x S: there is no ad to score), and
        writes the best discover.manual_queue_max of them to manual_queue.csv, the rest to manual_queue_overflow.csv."""
        rel = cand.title_relevance(title, self.s) if relevance is None else relevance
        tier = freshness.tier({"posted_at": seen_at or None, "posted_at_source": freshness.GMAIL if seen_at else None}, self.today)
        self.pending_queue.append({"label": label, "ts": self.today.isoformat(timespec="seconds"), "source": source, "company": company,
                                   "title": title, "location": location, "link": link, "reason": reason,
                                   "apply_kind": apply_kind or applykind.kind_of(link) or "aggregator_only",
                                   "score": round(rel * self.sponsor_weight(company), 3), "tier": tier})

    def sponsor_weight(self, company: str) -> float:
        """S of priority.py for this employer: 1.0 on the register by name, 0.9 near, 0.75 otherwise (or when no register is loaded)."""
        if self._sponsor_index is False:
            self._sponsor_index = sponsors.load_index(self.db)
        idx = self._sponsor_index
        match = idx.lookup(company, self.s["sponsors"].get("fuzzy_threshold", 0.92)).label if idx is not None and company else None
        return priority.sponsor_factor({"sponsor_match": match}, self.s)[0]

    def flush_queue(self) -> None:
        rows, self.pending_queue = self.pending_queue, []
        if not rows:
            return
        queue, overflow = manual_queue_path(self.s), overflow_path(self.s)
        have = {queue_key(r) for r in [*read_rows(queue), *read_rows(overflow)]}
        fresh = []
        for r in rows:                                          # arrival order; a link already queued (or seen twice now) is skipped
            if queue_key(r) not in have:
                have.add(queue_key(r))
                fresh.append(r)
        ranked = sorted(fresh, key=lambda r: (r.get("tier", 3), -r["score"]))      # stable: equal keys keep their order of arrival
        cap = max(0, int(self.s["discover"].get("manual_queue_max", 10)))
        keep, rest = ranked[:cap], ranked[cap:]
        for r in keep:
            self.run.count(r["label"], "to_manual_queue")
        for r in rest:
            self.run.count(r["label"], "to_manual_queue_overflow")
        if not self.dry_run:
            if keep:
                append_rows(queue, MANUAL_COLUMNS, keep)
            if rest:
                append_rows(overflow, OVERFLOW_COLUMNS, rest)

    def flush_filtered(self) -> None:
        """Append the titles the filters turned away to today's paths.discover/filtered_<date>.csv, with the reason of each; a (company, title,
        filter) already in ANY audit file is not written again, so a board read every day adds its new titles only."""
        rows, self.pending_filtered = self.pending_filtered, []
        if not rows or self.dry_run:
            return
        path = filtered_path(self.s, self.today_day)
        have = filtered_seen(self.s)
        fresh = []
        for r in rows:
            k = (r["company"], r["title"], r["filter"])
            if k not in have:
                have.add(k)
                fresh.append(r)
        if fresh:
            append_rows(path, FILTERED_COLUMNS, fresh)

    def finish(self) -> None:
        """Write what the run collected. Safe to call again: a second call finds nothing left to write."""
        self.flush_queue()
        self.flush_filtered()
        self.first_seen.save()
        self.run.refused = self.index.refused_counts()
        self.run.guesses, self.run.guess_cap, self.run.slug_rejected = self.index.guess_counts(), self.index.max_guesses, self.index.reject_summary()
        self.run.tiers = freshness.tier_counts(
            ({"posted_at": c.posted or None, "posted_at_source": c.posted_source or None, "seen_on": c.first_seen or None}
             for c, _ in self.run.written), self.today)

    # ---- sources
    @_finishing
    def from_boards(self) -> None:
        """Boards of the seed companies (seeds.py: archive folder names, discover.sources, register companies among them), found by
        guessing board tokens from each name (boards.py), plus the boards you named token by token in sources:. One company at a
        time: find its board, read its postings, move on. self.findings is the per-company report (ats_report)."""
        label = "ats"
        self.findings = []
        self.run.notes += workday_mod.config_problems(self.s)         # a sources.workday entry that is not a Workday address
        self.run.notes += careers_mod.problems(self.s)                # an entry of careers_urls.yaml that no reader understands, and why
        index = self.index
        listed = merge_boards(ats_mod.configured_boards(self.s), self.registry.boards())     # boards named by token: sources: and sources.yaml
        for board in listed:
            f = boards_mod.Finding(seeds_mod.Seed(board.name, "sources"), boards=[board])
            self.findings.append(f)
            if self.read_board(board, f, label) == "full":
                return
        seeds = seeds_mod.seeds(self.s, self.db)
        have = {(b.ats, b.slug) for b in listed}
        for i, seed in enumerate(seeds):
            if i and i % 20 == 0:
                self.say(f"ats: {i}/{len(seeds)} companies looked at ...")
            if folder_key(seed.name) in index.careers:                  # its address is in careers_urls.yaml: read above, not guessed
                continue
            f = boards_mod.find(seed, self.f, index, on_postings=lambda b, posts: self.cache.put(b, posts))
            self.findings.append(f)
            self.run.count(label, "board_unverified", len(f.unverified))
            for board in (b for b in f.boards if (b.ats, b.slug) not in have):      # a board named in sources: is read once
                if self.read_board(board, f, label) == "full":
                    left = len(seeds) - i - 1
                    if left:
                        self.run.notes.append(f"ats: the limit of {self.limit} new file(s) is reached; {left} compan(ies) not looked at "
                                              "this run (the next run carries on)")
                    return
        if self.f.backed_off:
            urls = getattr(self.f, "refused_url", {}) or {}
            self.run.notes.append("ats: stopped asking " + ", ".join(f"{h} (HTTP {c}" + (f" at {urls[h]}" if urls.get(h) else "") + ")"
                                                                for h, c in sorted(self.f.backed_off.items())))
        soft = getattr(self.f, "soft_401s", None)
        if isinstance(soft, list) and soft:
            self.run.notes.append(f"ats: {len(soft)} guessed board name(s) answered HTTP 401 (Ashby's answer for a board that does not exist): read as no board, "
                                  f"the host was not stopped; the first: {soft[0]}")
        self.run.notes += self.index.notes()
        ds = self.index.dataset
        if ds is not None and ds.enabled and ds.ready:
            self.run.notes.append(ds.summary())
            self.run.notes += ds.notes
        self.from_vc(label)

    def from_vc(self, label: str) -> None:
        """The VC portfolio boards in sources.yaml (method getro / consider): their jobs name the portfolio company and carry a link but no ad text, so each job
        in a role family and the selected place is looked for in full like an alert (resolve_link: the link's own board, else the company's)."""
        for src in self.registry.vc():
            try:
                posts = more_ats.vc_read(src.method, src.url, src.token, self.f, now=self.today)
            except (Blocked, FetchError) as e:
                self.run.notes.append(f"{src.id}: {e}")
                continue
            for p in posts:
                self.run.count(label, "seen")
                c = Candidate(source=src.method, company=p.company, title=p.title, location=p.location, url=p.url, description="",
                              posted=p.posted_at or "", ext_id=p.ext_id, board=f"{src.method}:{src.id}", apply_url=p.url,
                              apply_kind=applykind.kind_of(p.url) or "employer_other")
                if not self.wanted(c, label) or self.duplicate(c, label):
                    continue
                if self.resolve_link(label, src.method, p.company, p.title, p.location, p.url, f"vc:{src.id}", counted=True, source_id=src.id) == "full":
                    return

    def read_board(self, board: ats_mod.Board, finding: boards_mod.Finding, label: str) -> str:
        """Run every posting of one board through the filters; counts the ones that match the role and place into the finding."""
        if board.ats in self.index.stopped:
            finding.errors.append(f"{board.ats} was stopped for this run: {self.index.stopped[board.ats]}")
            return "error"
        try:
            posts = self.cache.postings(board)
        except (Blocked, FetchError) as e:
            if isinstance(e, Blocked) and board.ats in more_ats.SINGLE:
                self.index.stop(board.ats, e)
            finding.errors.append(str(e))
            self.run.notes.append(str(e) if isinstance(e, (Blocked, workday_mod.WorkdayError)) else f"{board.ats}:{board.slug}: {e}")
            return "error"
        for p in posts:
            if not p.listed:
                self.run.count(label, "closed")
                continue
            c = posting_candidate(board, p)
            finding.matched += cand.family_of(c, self.s) is not None and cand.uk_ok(c, self.s)
            if self.accept(c, label) == "full":
                return "full"
        return "ok"

    def ats_tally(self) -> list[str]:
        """The 'board: none' counts before and after the v2.6 providers, kept in the run record (`discover --report` prints them later) and returned as the line to print.
        Before = companies with no board among Greenhouse / Lever / Ashby / Workday; after = companies with no board at all."""
        fs = self.findings
        before = [f for f in fs if any(b.ats in OLD_PROVIDERS for b in f.boards)]
        self.run.sources["ats"]["companies"] = len(fs)
        self.run.sources["ats"]["board_none_before"] = len(fs) - len(before)
        self.run.sources["ats"]["board_none_after"] = len([f for f in fs if not f.boards])
        return ats_none_lines(self.run.sources["ats"])

    def ats_report(self) -> list[str]:
        """Per company: the tokens tried, the board found (provider/token) or none, the postings that matched. Boards found first."""
        fs = self.findings
        got = [f for f in fs if f.boards]
        self.ats_tally()
        lines = [f"ats: {len(fs)} company(ies) looked at · {len(got)} with a board · {len(fs) - len(got)} without · "
                 f"{sum(f.matched for f in fs)} job(s) matched the role and place"]
        entries, _ = careers_mod.load(self.s)
        if entries:
            ok = sum(1 for e in entries if careers_mod.board_of(e.url, e.company, e.provider)[0] is not None)
            lines.append(f"careers_urls.yaml: {len(entries)} address(es), {ok} with a reader (those companies are read from their address, not guessed)")
        lines += self.board_check_lines(fs)
        lines += [f.line() for f in got] + [f.line() for f in fs if not f.boards]
        return lines

    @staticmethod
    def board_check_lines(findings: list[boards_mod.Finding]) -> list[str]:
        """The boards a shortened slug found whose own name was not the company's (`board_unverified`): none of their ads were read. []
        when there are none. A board that IS the company's goes under `sources:` in settings.yaml (named token by token, never name-checked)."""
        bad = [(f, b, why) for f in findings for b, why in f.unverified]
        if not bad:
            return []
        out = [f"ats: {len(bad)} board(s) rejected by the board-name check (board_unverified); no ad was read from them. If one is the "
               "right company, name it under sources: in config/settings.yaml:"]
        out += [f"  board_unverified: {b.ats}/{b.slug} for {f.seed.name}: {why}" for f, b, why in bad]
        return out
    @_finishing
    def from_reed(self) -> None:
        label = "reed"
        try:
            stream = reed.fetch(self.f, self.s, want=lambda c: self.wanted_cheap(c, label), today=self.today)
            for kind, item in stream:
                if kind == "note":
                    self.run.notes.append(str(item))
                    continue
                if kind == "count":
                    self.run.count(label, item[0], item[1])
                    continue
                if self.accept(item, label) == "full":
                    return
        except reed.ReedKeyMissing as e:
            self.run.notes.append(str(e))
            self.say(f"reed: {e}")

    def wanted_cheap(self, c: Candidate, label: str) -> bool:
        """Before a Reed details request: role, place and not a duplicate (counted once, here; accept() then counts `seen`)."""
        self.run.count(label, "seen")
        if not self.wanted(c, label):
            return False
        if self.duplicate(c, label):
            return False
        self.run.sources[label]["seen"] -= 1                    # accept() will count this job again
        return True

    @_finishing
    def from_adzuna(self) -> None:
        label = "adzuna"
        cfg = self.s["sources"].get("adzuna") or {}
        app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
        if not (app_id and app_key):
            self.run.notes.append("adzuna: ADZUNA_APP_ID / ADZUNA_APP_KEY are not set in .env")
            self.say(self.run.notes[-1])
            return
        for query in cfg.get("queries") or []:
            for page in range(1, int(cfg.get("pages_per_query", 2)) + 1):
                try:
                    data = self.f.json(f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}", robots=False, what="Adzuna search",
                                       params={"app_id": app_id, "app_key": app_key, "what": query, "results_per_page": 50,
                                               "max_days_old": cfg.get("max_days_old", 7), "content-type": "application/json"})
                except FetchError as e:
                    self.run.notes.append(f"adzuna ({query}): {e}")
                    break
                results = (data or {}).get("results", []) if isinstance(data, dict) else []
                for j in results:
                    c = Candidate(source="adzuna", company=str((j.get("company") or {}).get("display_name") or ""),
                                  title=html_to_text(j.get("title")), location=str((j.get("location") or {}).get("display_name") or ""),
                                  url=str(j.get("redirect_url") or ""), description=html_to_text(j.get("description")),
                                  posted=str(j.get("created") or ""), ext_id=str(j.get("id") or ""), country="GB",   # the /gb/ endpoint
                                  posted_source=freshness.label("adzuna", "created") if freshness.is_iso(j.get("created")) else "")
                    if self.adzuna_one(c, label) == "full":
                        return
                if len(results) < 50:
                    break

    def adzuna_one(self, c: Candidate, label: str) -> str:
        """Adzuna's description is a snippet unless it is long and not cut: a full one is used as it is; otherwise the result
        is a link and is looked for on the employer's own board (D1)."""
        text = c.description.strip()
        if len(text) >= SNIPPET_MAX_CHARS and not text.endswith(("…", "...")):
            return self.accept(c, label)
        self.run.count(label, "seen")
        if not self.wanted(c, label) or self.duplicate(c, label):
            return "dropped"
        return self.resolve_link(label, "adzuna", c.company, c.title, c.location, c.url, "", counted=True, source_id="adzuna")

    # ---- a link or a (company, title): look for the whole ad
    def resolve_link(self, label: str, kind: str, company: str, title: str, location: str, link: str, origin: str,
                     counted: bool = False, default_country: str = "", source_id: str = "", seen: str = "", seen_at: str = "") -> str:
        """Find the full ad for something that is only a pointer. Order: a Reed job link (with a key: the Reed API), then the employer's own
        Greenhouse / Lever / Ashby board (found from the company's name). Nothing is requested from the link itself: linkedin.com, indeed,
        cv-library and Workday hosts are never fetched (applykind.NEVER_FETCH), and a job board's page is not a source for the ad. Anything
        not found goes to the manual queue, with the reason and its apply_kind. `source_id` is who gets the credit in the resolved-% report
        (default: `kind`); `seen` is the day the alert about it arrived (YYYY-MM-DD) and `seen_at` the same moment as an ISO datetime: a
        job whose own source gave no trusted posting time is dated by it (freshness.GMAIL, an upper bound)."""
        host = applykind.host_of(link)
        c: Candidate | None = None
        why = ""
        step = ""
        if host.endswith("reed.co.uk") and reed.job_id_from_url(link) and os.environ.get("REED_API_KEY"):
            try:
                d = reed.details(self.f, reed.api_key(), reed.job_id_from_url(link))
                c = reed.to_candidate(d, False, self.today) if d else None
                why = "" if d else "Reed returned no ad for that link (closed?)"
            except (FetchError, reed.ReedKeyMissing) as e:
                why = str(e)
            if c is not None:
                step, c.apply_url, c.apply_kind = "reed", c.apply_url or c.url, c.apply_kind or "aggregator_only"
        tried: list[str] = []                                  # what each step did, for the trace of an alert that stays unresolved
        category = ""
        if c is None:
            tried.append("reed: " + ((why or "REED_API_KEY is not set") if host.endswith("reed.co.uk") and reed.job_id_from_url(link) else "not a Reed job link"))
        if c is None and link:
            found = self.lookup.find_by_link(company, title, location, link)
            if found:
                c, step = posting_candidate(found[0], found[1], origin), "board_link"
            else:
                tried.append("board_link: " + self.lookup.link_note)
        elif c is None:
            tried.append("board_link: the alert has no link")
        if c is None and company:
            found = self.lookup.find(company, title, location)
            if found:
                c = posting_candidate(found[0], found[1], origin)
                step = "board_guessed" if found[0].origin == "alert" else "board_known"
            else:
                category, text = self.lookup.explain(company)
                tried.append(text)
                if not why:
                    boards = self.lookup.boards_for(company)
                    if boards:
                        why = f"no posting titled like this on the {boards[0].ats} board of {company}: closed, or titled differently, or in another place"
                    elif sponsors.normalise(company) in self.lookup.not_looked:
                        why = (f"board of {company} not looked for: the limit of {self.lookup.probes_allowed} compan(ies) a run "
                               "(discover.alert_probes_per_run) was reached")
                    else:
                        why = f"no public job board known for {company}"
                        unverified = [x for f in self.lookup.findings if f.seed.name == company for x in f.unverified]
                        if unverified:
                            why += f" (board_unverified: {unverified[0][1]})"
        elif c is None:
            tried.append("board_known / board_guessed: the alert names no company")
            category = "no_company"
        link_kind = applykind.kind_of(link) or "aggregator_only"
        if c is None:
            if link_kind == "workday":
                why = "Workday careers site (its pages are never fetched; only the public list API was asked): " + (why or "no company given")
                category = "workday"
            if len(self.run.unresolved) < 500:
                self.run.unresolved.append(self.unresolved_row(label, kind, source_id or kind, company, title, location, link, tried, why or "no company given", category or "no_board"))
            self.run.credit(source_id or kind, "unresolved")
            self.to_queue(label, kind, company, title, location, link, why or "no company given", seen_at=seen_at, apply_kind=link_kind)
            return "queued"
        self.run.count(label, "resolved")
        self.run.count(label, f"resolved_via_{step}")
        self.run.credit(source_id or kind, "resolved")
        self.run.credit(source_id or kind, f"via_{step}")
        if step == "board_link" or (c.board.partition(":")[0] in more_ats.SINGLE):          # found only because of the v2.6 providers: the "after" of the report lines
            self.run.count(label, "resolved_new_provider")
            self.run.credit(source_id or kind, "via_new_provider")
        c.origin = origin
        if seen:
            c.seen = seen
        if seen_at:
            c.seen_at = seen_at
            if not c.posted_source:                            # no trusted time from the ad itself: the email's arrival
                c.posted, c.posted_source = seen_at, freshness.GMAIL
        if default_country and not c.country:               # an alert from a one-country source: that country unless the place says another
            c.country = countries.detect(c.location) or default_country
        result = self.accept(c, label, counted=counted)
        if result == "full":                                # found, but this run has no room: never lost, handed to you with the page to open
            self.run.count(label, "resolved_but_full")
            self.to_queue(label, kind, company, title, location, c.url,
                          f"resolved to {c.url} but this run's limit of {self.limit} new file(s) (discover.max_new_per_run) was reached: "
                          "add it with `careerops live --url`, or run discover again", seen_at=seen_at, apply_kind=c.apply_kind)
            return "queued"
        return result

    @staticmethod
    def unresolved_row(label: str, kind: str, source: str, company: str, title: str, location: str, link: str, tried: list[str], why: str, category: str) -> dict:
        """One alert that stayed unresolved, as it goes in the run record: who sent it, what each step tried, why it failed, and for CV-Library whether the link is
        a job page (the ad is on CV-Library or an agency only) or a click-tracker link."""
        cv = ""
        if applykind.host_of(link).endswith("cv-library.co.uk"):
            cv = "tracker link" if gmail.is_cv_tracking(link) else "agency or board-only"
        return {"label": label, "kind": kind, "source": source, "company": company, "title": title, "location": location, "link": link,
                "tried": list(tried), "why": why, "category": category, "cv": cv}

    def alert_source(self, from_header: str, subject: str) -> registry_mod.Source | None:
        """Which source an alert email belongs to. With discover.gmail.senders set (kinds) or no registry: the four built-in senders;
        otherwise the sources.yaml entry whose sender domain (and subject words) match."""
        g = self.s["discover"].get("gmail") or {}
        if self.registry.alert_sources() and not g.get("senders"):
            return self.registry.match_sender(from_header, subject)
        kind = gmail.sender_kind(from_header)
        if kind is None or kind not in (g.get("senders") or gmail.SENDERS):
            return None
        return self.registry.by_kind(kind) or registry_mod.Source(
            id=kind, name=kind, url="https://" + gmail.SENDERS[kind][0], country="multi", method="gmail_alerts",
            domains=gmail.SENDERS[kind], parser=kind)

    @_finishing
    def from_gmail(self) -> None:
        label = "gmail"
        g = self.s["discover"].get("gmail") or {}
        try:
            token = gmail.access_token(self.s)
        except gmail.GmailAuthError as e:
            self.run.notes.append(str(e))
            self.run.failures.append(f"gmail: {e}")
            self.say(f"gmail: {e}")
            return
        seen_path = self.s.path("discover") / "gmail_seen.json"
        try:
            seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()
        try:
            ids = gmail.list_message_ids(self.f, token, gmail.build_query(self.s, self.registry), int(g.get("max_messages", 60)))
        except (FetchError, Blocked) as e:
            self.run.notes.append(f"gmail: {e}")
            self.run.failures.append(f"gmail: {e}")
            return
        done_now: list[str] = []
        for mid in ids:
            if mid in seen:
                continue
            try:
                msg = gmail.get_message(self.f, token, mid)
                frm, subject, html, text = gmail.message_text(msg)
            except (FetchError, Blocked) as e:
                self.run.notes.append(f"gmail message {mid}: {e}")
                break
            alert_time = gmail.message_time(msg)                  # an upper bound on when its jobs were posted
            alert_day = alert_time[:10]
            src = self.alert_source(frm, subject)
            done_now.append(mid)
            if src is None:
                continue
            kind = src.kind
            self.run.credit(src.id, "mails")
            dedicated = kind in gmail.JOB_LINK
            parsed = (gmail.parse_alert(kind, html, text, mid) if dedicated else
                      gmail.parse_alert(kind, html, text, mid, link_fn=lambda href, s=src: gmail.generic_link(s, href)))
            self.run.credit(src.id, "parsed", len(parsed))
            if not parsed:
                self.run.no_job_subjects.setdefault(src.id, " ".join((subject or "").split())[:120] or "(no subject)")
            if not parsed and not dedicated and ALERT_SUBJECT.search(subject or ""):
                # a source without a dedicated parser whose alert showed no job link we recognise: never guessed at, handed to you
                self.run.count(label, "alerts_unparsed")
                self.to_queue(label, src.id, "", (subject or "")[:120], "", gmail.web_link(mid),
                              f"alert email from {src.name}: no job links were recognised in it; open the email and add the ads you want",
                              relevance=ALERT_EMAIL_RELEVANCE, seen_at=alert_time)
            default_country = src.home_country if src.home_country not in ("", countries.UK) else ""
            for item in parsed:
                self.run.count(label, "alerts")
                self.run.count(label, f"alerts_{kind}")
                self.run.credit(src.id, "alerts")
                if self.already_decided(item.company, item.title, None, label):
                    self.run.credit(src.id, "already_decided")
                    continue
                if self.known.where(item.company, item.title, item.location):
                    self.run.count(label, "duplicate")
                    self.run.credit(src.id, "duplicate")
                    continue
                probe = Candidate(source=kind, company=item.company, title=item.title, location=item.location, url=item.link,
                                  description="")
                if cand.family_of(probe, self.s) is None:
                    self.run.count(label, "role_filtered")
                    self.run.credit(src.id, "role_filtered")
                    self.filtered_out(kind, item.company, item.title, item.location, item.link, "role", role_reason(item.title))
                    continue
                if self.resolve_link(label, kind, item.company, item.title, item.location, item.link, f"gmail:{kind}",
                                     default_country=default_country, source_id=src.id, seen=alert_day, seen_at=alert_time) == "full":
                    break
        if not self.dry_run:
            try:
                seen_path.parent.mkdir(parents=True, exist_ok=True)
                seen_path.write_text(json.dumps(sorted(seen | set(done_now))[-2000:]), encoding="utf-8")
            except OSError:
                pass

    @_finishing
    def from_jobbank(self) -> None:
        """Canada's Job Bank open-data CSV: rows in a target role family, first posted recently, LISTED (no employer, no ad text)."""
        label = "jobbank"
        sources = self.registry.with_method("jobbank_csv")
        if not sources:
            self.run.notes.append("jobbank: no enabled jobbank_csv source in config/sources.yaml")
            return
        cfg = self.s["discover"].get("jobbank") or {}
        cap = int(cfg.get("max_rows_per_run", 50))
        for src in sources:
            have = {f"{r['source']}|{r['ref']}" for r in read_listed(self.s) if r.get("ref")}
            added = 0
            try:
                stream = jobbank.listed(self.f, self.s, src.dataset, want=lambda t: gates.classify_role(t, self.s) is not None,
                                        today=self.today, say=self.say)
                for kind, item in stream:
                    if kind == "note":
                        self.run.notes.append(str(item))
                        continue
                    self.run.count(label, "seen")
                    key = f"{src.id}|{item['ref']}"
                    if item["ref"] and key in have:
                        self.run.count(label, "duplicate")
                        continue
                    if added >= cap:
                        self.run.count(label, "listed_capped")
                        continue
                    have.add(key)
                    added += 1
                    if not self.dry_run:
                        append_listed(self.s, [{"ts": self.today.isoformat(timespec="seconds"), "source": src.id,
                                                "country": src.home_country, "company": "", "title": item["title"],
                                                "location": item["location"], "posted": item["posted"], "salary": item["salary"],
                                                "ref": item["ref"], "link": "",
                                                "note": "Job Bank open data has no employer or link: search the title on jobbank.gc.ca"}])
                    self.run.count(label, "listed_only")
                    self.run.credit(src.id, "listed_only")
            except (jobbank.JobBankError, FetchError, Blocked) as e:
                self.run.notes.append(f"jobbank ({src.id}): {e}")
                self.say(f"jobbank: {e}")

    # ---- output
    def ranked(self) -> list[tuple[priority.Priority, Candidate, Path]]:
        idx = sponsors.load_index(self.db)
        out = []
        for c, path in self.run.written:
            match = idx.lookup(c.company, self.s["sponsors"].get("fuzzy_threshold", 0.92)).label if idx is not None else None
            try:
                pre = gates.pre_ceiling(c.description, self.fb, self.s) if self.fb is not None else None
            except Exception:  # noqa: BLE001
                pre = None
            work = workcountry.resolve(location=c.location, description=c.description, title=c.title,
                                       override=c.country if c.listed_only else "",
                                       rules=workcountry.rules_from(workcountry.applicant_visa(self.s)))
            job = {"id": c.ad_id(), "company": c.company, "title": c.title, "description": c.description, "sponsor_match": match, "agency": c.agency,
                   "posted_at": c.posted or None, "posted_at_source": c.posted_source or None, "seen_on": c.first_seen or None,
                   "open_listing": c.open_listing, "ceiling_pre": pre, "country": c.country if c.listed_only else None,
                   "work_country": work.country}
            out.append((priority.priority(job, self.s, tailored=False, today=self.today), c, path))
        return sorted(out, key=lambda t: (t[0].tier, -t[0].p, -t[0].s, t[1].company, t[1].title))

    def listing(self, n: int = 10) -> list[str]:
        lines = []
        for pr, c, path in self.ranked()[:n]:
            lines.append(f"  T{pr.tier} {pr.age():>7}  estimate P {pr.p:.2f} = {pr.f:.2f} x {pr.s:.2f} x {pr.l:.2f}{' ' + pr.role_text() if pr.r is not None else ''}  {c.company} | {c.title} | "
                         f"{c.location or '?'} | {c.url}  "
                         f"[{c.source}{', agency' if c.agency else ''}{', listed only' if c.listed_only else ''}; {pr.s_reason}"
                         f"{'; ' + pr.flag() if pr.flag() else ''}]  {path.name}")
        return lines


# ------------------------------------------------------------------ rows that are listed, not written as ads (append-only)
def format_region_size(region: dict, size: dict) -> str:
    """`in region 41 · out 112 (parked) · unknown 9 | SME 14 · Large 22 · size? 31` (`discover --report`)."""
    return (f"in region {region.get(region_mod.IN, 0)} · out {region.get(region_mod.OUT, 0)} (parked) · unknown {region.get(region_mod.UNKNOWN, 0)} | "
            f"SME {size.get('sme', 0)} · Large {size.get('large', 0)} · size? {size.get('unknown', 0)}")


def listed_path(settings) -> Path:
    return settings.path("discover") / "listed_only.csv"


def read_listed(settings) -> list[dict]:
    path = listed_path(settings)
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_listed(settings, rows: list[dict]) -> None:
    """Append rows to paths.discover/listed_only.csv (postings outside the UK that have no full ad to write: Job Bank Canada)."""
    path = listed_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LISTED_COLUMNS)
        if new:
            w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ ads outside the region: parked, not written (append-only)
def parked_path(settings) -> Path:
    return settings.path("discover") / "parked.csv"


def parked_dir(settings) -> Path:
    return settings.path("discover") / "parked"


def parked_key(company: str, title: str, location: str, link: str) -> str:
    return (link or f"{company}|{title}|{location}").strip().lower()


def parked_keys(settings) -> set[str]:
    return {parked_key(r.get("company") or "", r.get("title") or "", r.get("location") or "", r.get("link") or "") for r in read_rows(parked_path(settings))}


def park_inbox_listed(settings, regions: Sequence[str] | None = None, *, dry_run: bool = False, now: datetime | None = None) -> list[Path]:
    """Move the inbox files that `discover` wrote before listed ads were parked (`Status: listed_only`) and that are outside `regions` (every one when
    `regions` is None: the UK-only reading) to paths.discover/parked/, one row each in paths.discover/parked.csv (its `file` column names where the ad
    went). An ad of a region you have chosen since stays in the inbox. Nothing is deleted or overwritten; a dry run moves and writes nothing. Returns the
    files it moved (or would move)."""
    now = now or datetime.now(timezone.utc)
    have = parked_keys(settings)
    moved: list[Path] = []
    for p in decided_mod.txt_files(decided_mod.inbox_dir(settings)):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hdr = manual.header_fields(text)
        if (hdr.get("status") or "").strip().lower() != "listed_only":
            continue
        if regions is not None and region_mod.match(hdr.get("location"), text, regions, country=hdr.get("country")).verdict != region_mod.OUT:
            continue
        moved.append(p)
        if dry_run:
            continue
        try:
            dst = decided_mod.move_file(settings, p, parked_dir(settings))
        except (OSError, decided_mod.DecidedError):
            moved.pop()
            continue
        moved[-1] = dst
        key = parked_key(hdr.get("company") or "", hdr.get("title") or "", hdr.get("location") or "", hdr.get("url") or "")
        if key not in have:
            have.add(key)
            where = f"outside region {','.join(regions)}" if regions is not None else "outside the UK"
            append_rows(parked_path(settings), PARKED_COLUMNS,
                        [{"ts": now.isoformat(timespec="seconds"), "source": hdr.get("source") or "", "country": hdr.get("country") or "",
                          "company": hdr.get("company") or "", "title": hdr.get("title") or "", "location": hdr.get("location") or "",
                          "posted": hdr.get("posted") or "", "link": hdr.get("url") or "", "reason": f"{where}: moved out of the inbox", "file": f"parked/{dst.name}"}])
    return moved


def last_alert_read(runs: list[dict]) -> tuple[bool, str | None]:
    """(did the LAST recorded run read the alert emails, the start time of the latest run that did). A run counts as having read them when it was asked for
    `gmail_alerts` and Gmail did not fail (a lapsed sign-in is a failure); a run recorded before runs said what they were asked for counts when it has gmail counts."""
    def read(r: dict) -> bool:
        asked = r.get("asked")
        asked_gmail = ("gmail_alerts" in asked) if isinstance(asked, list) else bool((r.get("sources") or {}).get("gmail"))
        return asked_gmail and not any(str(f).startswith("gmail") for f in r.get("failures") or [])
    last = next((r.get("ts") for r in reversed(runs) if read(r)), None)
    return (bool(runs) and read(runs[-1])), last


REFUSED_NOTE = re.compile(r"^ats: (\w+) refused (\d+) request\(s\)")
STOPPED_NOTE = re.compile(r"^ats: (\w+) stopped for this run")


def refused_of(run: dict) -> dict[str, int]:
    """provider -> requests it refused in one recorded run: the `refused` field, else (a run recorded before it existed) read back from its notes."""
    got = run.get("refused")
    if isinstance(got, dict):
        return {str(k): int(v) for k, v in got.items()}
    out: dict[str, int] = {}
    for n in run.get("notes") or []:
        if (m := REFUSED_NOTE.match(str(n))):
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
        elif (m := STOPPED_NOTE.match(str(n))):
            out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


def refused_lines(runs: list[dict], current: dict[str, int] | None = None) -> list[str]:
    """`Refused requests per provider, before -> after: workable 31 -> 0 · personio 52 -> 0`. `before` is the run before, `after` the run now (`current`, the
    run in hand, else the last recorded one). Providers with none either time are left out; [] when there is nothing to compare."""
    if current is None:
        after, before = (refused_of(runs[-1]) if runs else None), (refused_of(runs[-2]) if len(runs) > 1 else None)
    else:
        after, before = current, (refused_of(runs[-1]) if runs else None)
    if after is None:
        return []
    names = sorted({*after, *(before or {})})
    shown = [n for n in names if after.get(n, 0) or (before or {}).get(n, 0)]
    if before is None:
        return ["Refused requests per provider (first run recorded): " + (" · ".join(f"{n} {after[n]}" for n in shown) or "none")]
    if not shown:
        return ["Refused requests per provider, before -> after: none in either run"]
    return ["Refused requests per provider, before -> after: " + " · ".join(f"{n} {before.get(n, 0)} -> {after.get(n, 0)}" for n in shown)]


def save_run(settings, run: Run) -> None:
    p = settings.path("discover") / "runs.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(run.as_json()) + "\n")
    except OSError:
        pass


def read_runs(settings) -> list[dict]:
    p = settings.path("discover") / "runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def inbox_tiers(settings, now: datetime | None = None) -> list[int]:
    """How many of the JD files in the inbox are in each freshness tier (0 <= 24 h ... 3 older or undated), read from their headers
    (Posted / Posted via / First seen, else the file's own time). A file that cannot be read counts as tier 3."""
    jobs = []
    for p in sorted((settings.path("jds") / "inbox").glob("*.txt")):
        try:
            hdr = manual.header_fields(p.read_text(encoding="utf-8"))
            written = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except (OSError, ValueError):
            jobs.append({})
            continue
        jobs.append({"posted_at": hdr.get("posted"), "posted_at_source": hdr.get("posted_source"), "seen_on": hdr.get("first_seen"),
                     "first_seen": written})
    return freshness.tier_counts(jobs, now)


def family_lines(settings, written: list[dict]) -> list[str]:
    """'  data_scientist (#1)  Company | Title  file' for each ad a run wrote: the role family its title matches (roles.families, list
    order) and the user's priority of it, or 'no family' (a title that no longer matches, e.g. after the patterns were edited)."""
    out = []
    for w in written:
        fam = gates.match_family(str(w.get("title") or ""), settings)
        label = f"{fam.name} (#{fam.priority})" if fam and fam.priority is not None else fam.name if fam else "no family"
        out.append(f"  {label}  {w.get('company', '')} | {w.get('title', '')}  {w.get('file', '')}".rstrip())
    return out


def format_report(settings, run: Run | None = None) -> str:
    """Counts per source over all runs and for the last one, the role family of each ad that run wrote (`run`, else the last saved
    run), and what is in the manual queue (resolved vs sent to it)."""
    runs = read_runs(settings)
    queue = read_manual_queue(settings.path("jds") / "manual_queue.csv")
    lines = []
    if not runs:
        lines.append("No discover run recorded yet (nothing in data/discover/runs.jsonl).")
    total: dict[str, Counter] = defaultdict(Counter)
    for r in runs:
        for s, c in r["sources"].items():
            total[s].update(c)

    def table(title: str, data: dict[str, Counter]) -> list[str]:
        keys = [k for k in (*COUNTERS, "needs_full_text") if any(k in c for c in data.values())]
        extra = sorted({k for c in data.values() for k in c} - set(keys))
        out = [title]
        for s in sorted(data):
            out.append(f"  {s:8s} " + ", ".join(f"{k} {data[s][k]}" for k in (*keys, *extra) if data[s].get(k)))
        return out
    if runs:
        lines += table(f"Last run ({runs[-1]['ts']}{', dry run' if runs[-1].get('dry_run') else ''}):",
                       {s: Counter(c) for s, c in runs[-1]["sources"].items()})
        lines += table(f"All {len(runs)} run(s):", total)
        for n in runs[-1].get("notes", [])[:8]:
            lines.append(f"  note: {n}")
    if runs and runs[-1].get("tiers") is not None:
        lines.append("Freshness of the ads the last run wrote: " + freshness.counts_line(runs[-1]["tiers"]))
    wrote = run.as_json()["written"] if run is not None else runs[-1].get("written") if runs else None
    if wrote:
        lines += ["Role family of each ad the last run wrote:", *family_lines(settings, wrote)]
    waiting = inbox_tiers(settings)
    lines.append("Waiting in the inbox for `careerops live` (`live --fresh` takes tier 0): " + freshness.counts_line(waiting))
    resolved = sum(c.get("resolved", 0) for c in total.values())
    lines.append(f"Resolved to a full ad: {resolved} · in the manual queue: {len(queue)} "
                 f"({', '.join(f'{k} {v}' for k, v in sorted(Counter(r['source'] for r in queue).items())) or 'empty'})")
    kinds = Counter(r.get("apply_kind") or "unknown" for r in queue)
    if queue:
        lines.append("  manual queue by where to apply: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    if queue:
        lines.append(f"  manual queue: {settings.path('jds') / 'manual_queue.csv'} — for each row, copy the ad and run "
                     "`careerops live --clipboard`")
    overflow = read_rows(overflow_path(settings))
    if overflow:
        lines.append(f"  {len(overflow)} more in {overflow_path(settings)} (below the best {settings['discover'].get('manual_queue_max', 10)} of their run)")
    if runs:
        by_source: dict[str, Counter] = defaultdict(Counter)
        for r in runs:
            for s, c in (r.get("by_source") or {}).items():
                by_source[s].update(c)
        lines += resolution_lines(runs[-1].get("by_source") or {}, "Alerts resolved to the employer's board or a full ad, last run:")
        lines += resolution_lines(by_source, f"Alerts resolved, all {len(runs)} run(s):")
        lines += step_lines(runs[-1].get("sources") or {}, "Alerts resolved by step, last run:")
        last_ats = next((r["sources"]["ats"] for r in reversed(runs) if (r.get("sources") or {}).get("ats", {}).get("companies")), None)
        if last_ats:
            lines += ["Companies without a job board (the last `discover --ats` run):", *("  " + x for x in ats_none_lines(last_ats))]
        lines += step_lines({s: dict(c) for s, c in total.items()}, f"Alerts resolved by step, all {len(runs)} run(s):")
        if run is None:                                 # a run in hand printed its own lines (cli._discover_inbox)
            lines += refused_lines(runs)
            lines += trace_lines(runs[-1])
    return "\n".join(lines)


def ats_none_lines(ats: dict) -> list[str]:
    """'board: none  219 of 267 before the v2.6 providers -> 190 of 267 after' from the ats counters of a run; [] when the run did not look at companies."""
    n = int(ats.get("companies", 0))
    if not n:
        return []
    return [f"board: none  {int(ats.get('board_none_before', 0))} of {n} companies before the v2.6 providers (Workable, SmartRecruiters, Teamtailor, Recruitee, Personio) "
            f"-> {int(ats.get('board_none_after', 0))} of {n} after"]


def step_lines(sources: dict, title: str) -> list[str]:
    """'Alerts resolved by step: board_known 3 · board_guessed 5 · reed 1' from the resolved_via_<step> counters; [] when none was resolved."""
    got = Counter()
    for c in sources.values():
        for step in RESOLVED_BY:
            got[step] += int(c.get(f"resolved_via_{step}", 0))
    if not sum(got.values()):
        return []
    return [f"{title} " + " · ".join(f"{step} {got[step]}" for step in RESOLVED_BY)]


def diverse(rows: list[dict], n: int) -> list[dict]:
    """Up to n rows: the first of each category first (in the order met), then the rest in order. A short list that shows every way an alert failed."""
    first, rest, seen = [], [], set()
    for r in rows:
        (rest if r.get("category") in seen else first).append(r)
        seen.add(r.get("category"))
    return (first + rest)[:n]


def example_rows(rows: list[dict]) -> list[dict]:
    """The unresolved alerts kept in the run record: 30 that show every way it failed, and up to 10 CV-Library ones the same way."""
    keep = diverse(rows, 30)
    for r in diverse([r for r in rows if r.get("kind") == "cvlibrary"], 10):
        if r not in keep:
            keep.append(r)
    return keep


CV_NOTES = {"agency or board-only": "a CV-Library job page: agency or board-only (the employer's own board was not found; CV-Library pages are never fetched)",
            "tracker link": "a CV-Library click-tracker link: it names no job page (open the alert email to see the job); never fetched"}


def _example(n: int, r: dict) -> list[str]:
    mark = {"agency or board-only": " [agency or board-only]", "tracker link": " [click-tracker link]"}.get(r.get("cv") or "", "")
    out = [f"  {n}. {r.get('company') or '(no company)'} | {r.get('title') or '?'} | {r.get('link') or 'no link'}{mark}"]
    out += [f"     - {t}" for t in r.get("tried") or []]
    out.append(f"     failed: {r.get('why') or 'no reason recorded'}")
    return out


def alert_why_lines(rec: dict, n: int = 5) -> list[str]:
    """When board_link or board_known resolved none of the run's alerts: `Why board_link and board_known resolved 0 of 37 alert(s)`, the reasons by count, and n example
    alerts with the company, what each step tried and why it failed. [] when both resolved some, or nothing was left unresolved. `rec` is a run record (Run.as_json)."""
    srcs = rec.get("sources") or {}
    zero = [s for s in ("board_link", "board_known") if not sum(int(c.get(f"resolved_via_{s}", 0) or 0) for c in srcs.values())]
    rows = [r for r in rec.get("unresolved") or [] if r.get("label") != "ats"]
    if not zero or not rows:
        return []
    alerts = sum(int(c.get("alerts", 0) or 0) for c in srcs.values())
    total = int(rec.get("unresolved_total") or len(rows))
    shown = diverse(rows, n)
    kinds = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted((rec.get("unresolved_kinds") or {}).items(), key=lambda kv: -kv[1]))
    out = [f"Why {' and '.join(zero)} resolved 0 of {alerts or total} alert(s) this run ({total} unresolved; {len(shown)} example(s) below):"]
    if kinds:
        out.append(f"  unresolved by reason: {kinds}")
    for i, r in enumerate(shown, 1):
        out += _example(i, r)
    return out


def cvlibrary_lines(rec: dict, n: int = 5) -> list[str]:
    """CV-Library in a run: mails read, alerts parsed, what became of them, and n parsed alerts that stayed unresolved with the reason of each. A link on a CV-Library
    job page is marked `agency or board-only`; a click-tracker link is said to name no job page. [] when the run saw no CV-Library mail and no CV-Library alert."""
    by = rec.get("by_source") or {}
    rows = [r for r in rec.get("unresolved") or [] if r.get("kind") == "cvlibrary"]
    sids = sorted({r.get("source") for r in rows if r.get("source")} | {s for s in by if re.sub(r"[^a-z]", "", s.lower()) == "cvlibrary"})

    def total(key: str) -> int:
        return sum(int((by.get(s) or {}).get(key, 0) or 0) for s in sids)
    mails, parsed, ok, no = total("mails"), total("parsed"), total("resolved"), total("unresolved")
    if not (mails or parsed or rows):
        return []
    gone = ", ".join(f"{k.replace('_', ' ')} {total(k)}" for k in ("role_filtered", "duplicate", "already_decided") if total(k))
    head = f"CV-Library: {mails} alert mail(s) read, {parsed} alert(s) parsed, {ok} resolved, {no or len(rows)} unresolved" + (f", {gone}" if gone else "")
    if mails and not parsed:
        subj = next((rec.get("no_job_subjects", {}).get(s) for s in sids if (rec.get("no_job_subjects") or {}).get(s)), "")
        head += "; no job could be read from them" + (f" (first subject: {subj})" if subj else "")
    out = [head]
    shown = rows[:n]
    if shown:
        out.append(f"  {len(shown)} parsed alert(s) that stayed unresolved, with the reason:")
    for i, r in enumerate(shown, 1):
        out += _example(i, r)
        if r.get("cv") in CV_NOTES:
            out.append(f"     note: {CV_NOTES[r['cv']]}")
    return out


def guess_lines(rec: dict) -> list[str]:
    """`Slug guesses per provider this run (limit 60 each): greenhouse 60 guess(es), 61 request(s), 14 cached, 22 over the limit ...` and the tokens never asked because
    they are not company names. [] when the run guessed nothing."""
    g, rej = rec.get("guesses") or {}, rec.get("slug_rejected") or {}
    out = []
    if g:
        parts = " · ".join(f"{a} {v.get('guesses', 0)} guess(es), {v.get('requests', 0)} request(s), {v.get('cached', 0)} cached"
                           + (f", {v['over_cap']} over the limit" if v.get("over_cap") else "")
                           + (f", {v['checked_from_list']} checked from the public list" if v.get("checked_from_list") else "") for a, v in sorted(g.items()))
        out.append(f"Slug guesses per provider this run (limit {rec.get('guess_cap') or '-'} each): {parts}")
        listed = [(a, v) for a, v in sorted(g.items()) if v.get("list_matched")]
        if listed:
            out.append("Matched from the public board list (v3.1, no blind guesses): " + " · ".join(
                f"{a} {v['list_matched']} matched, {v.get('list_verified', 0)} verified, {v.get('list_failed', 0)} failed" for a, v in listed))
    if rej.get("count"):
        out.append(f"Slugs not asked because they are not company names: {rej['count']}" + (" (" + "; ".join(rej.get("examples") or [])[:300] + ")" if rej.get("examples") else ""))
    return out


def trace_lines(rec: dict) -> list[str]:
    """The v2.8 lines of a run record: slug guesses per provider, why board_link / board_known resolved nothing, CV-Library."""
    return [*guess_lines(rec), *alert_why_lines(rec), *cvlibrary_lines(rec)]


def resolution_lines(by_source: dict, title: str) -> list[str]:
    """Per source: how many of the alerts that needed a full ad found one ('linkedin  12 of 20 (60%)'). [] when none needed one."""
    rows = []
    for sid in sorted(by_source):
        ok, no = int(by_source[sid].get("resolved", 0)), int(by_source[sid].get("unresolved", 0))
        new = int(by_source[sid].get("via_new_provider", 0))
        if ok + no:
            rows.append(f"  {sid:16s} {ok} of {ok + no} ({100 * ok / (ok + no):.0f}%)"
                        + (f" · before the v2.6 providers {ok - new} of {ok + no} ({100 * (ok - new) / (ok + no):.0f}%)" if new else ""))
    return [title, *rows] if rows else []
