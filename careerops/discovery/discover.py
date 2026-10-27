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
after every run. Outside the UK: a job whose place is another country (the ad's own country, else read from its place) is not dropped
but written with `Country:` and `Status: listed_only` headers (at most discover.listed_only.max_per_run a run, apart from the
UK limit); `careerops live` will not apply to it without a visa block for that country in applicant.yaml, and its priority S is
priority.sponsor_weights.non_home. Jobs already applied to through AIApply (paths.aiapply_applied, company + title) are skipped as duplicates.

Resolving an alert (or an Adzuna link): the company's name gives up to four board tokens (boards.py), each asked of Greenhouse, Lever
and Ashby (answers, hits and misses, cached 30 days; a board found only through a shortened token must prove it is this company); the
alert's title is then matched to an open posting on the board (discover.title_match, 0.9, the same level words, and a place that agrees).
Nothing is ever requested from linkedin.com, indeed, cv-library or a Workday host (applykind.py): a Reed link goes to the Reed API, the
rest to the boards. What is found carries apply_url and apply_kind (greenhouse | lever | ashby | workday | employer_other |
aggregator_only); what is not goes to the manual queue with its apply_kind. Per source and per step, the report says how many
alerts were resolved.

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
from typing import Callable, Iterable
from urllib.parse import urlparse

from ..apply.naming import folder_key
from ..db import DB
from . import applykind
from . import ats as ats_mod
from . import candidate as cand
from ..apply import decided as decided_mod
from . import boards as boards_mod
from . import countries, firstseen, freshness, gates, gmail, intake, jobbank, manual, priority, reed, registry as registry_mod, seeds as seeds_mod, workcountry
from . import workday as workday_mod
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
ALERT_SUBJECT = re.compile(r"job|vacanc|stellen|vacature|alert|match|recommend|new roles?|positions?|offres?|opportunit", re.I)
COMPANY_COLUMNS = ("company", "company_name", "employer", "employer_name", "organisation", "organization")
TITLE_COLUMNS = ("title", "job_title", "job title", "position", "role", "job")
SNIPPET_MAX_CHARS = 1500           # an Adzuna description shorter than this (or cut with an ellipsis) is a snippet, not the ad
TITLE_MATCH = 0.9                  # default of discover.title_match: similarity at which an alert's title is taken for a posting's title
COUNTERS = ("alerts", "seen", "role_filtered", "place_filtered", "closed", "already_decided", "duplicate", "written", "listed_only", "listed_capped",
            "already_in_inbox", "bad_header", "resolved", "to_manual_queue", "to_manual_queue_overflow", "board_unverified")
RESOLVED_BY = ("reed", "board_known", "board_guessed")       # the steps that can resolve an alert (counted as resolved_via_<step>)


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
                "no_job_subjects": dict(self.no_job_subjects),
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
    for company, role in archive_pairs(settings.archive_roots()):
        k.add(company, role, "", "already applied (application archive)")
    add_aiapply(settings, k)
    return k


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
            posts = workday_mod.board_postings(board, self.fetcher, keep=keep, **workday_mod.options_for(self.settings, board))
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
        self.guessed[norm] = finding.boards
        return finding.boards

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


POSTED_FIELD = {"greenhouse": "first_published", "lever": "createdAt", "lever_eu": "createdAt", "ashby": "publishedAt", "workday": "postedOn"}


def posting_candidate(board: ats_mod.Board, p: ats_mod.Posting, origin: str = "") -> Candidate:
    """A board posting as a candidate, with the board's own posting time and where it comes from (freshness.py)."""
    posted = p.posted_at or ""
    return Candidate(source=board.ats.replace("lever_eu", "lever"), company=board.name or p.company, title=p.title,
                     location=p.location, url=p.url, description=p.description, posted=posted, ext_id=p.ext_id,
                     posted_source=freshness.label(board.ats.replace("lever_eu", "lever"), POSTED_FIELD[board.ats])
                     if posted and freshness.is_iso(posted) else "",
                     country=p.country, origin=origin, board=f"{board.ats}:{board.slug}", apply_url=p.url,
                     apply_kind=applykind.board_kind(board.ats))


# ------------------------------------------------------------------ the pipeline for one candidate
class Discover:
    def __init__(self, settings, db: DB, fetcher: Fetcher, *, fb: dict | None = None, dry_run: bool = False, limit: int | None = None,
                 today: datetime | None = None, say: Callable[[str], None] = print):
        self.s, self.db, self.f, self.say, self.dry_run = settings, db, fetcher, say, dry_run
        self.today = today or datetime.now(timezone.utc)
        self.limit = limit if limit is not None else int(settings["discover"].get("max_new_per_run", 40))
        self.known = load_known(settings, db)
        self.decided = decided_mod.load(settings, db)                  # skipped / applied jobs: their ids and duplicate keys (decided.py)
        self.run = Run(started=self.today.isoformat(timespec="seconds"), dry_run=dry_run)
        self.run.notes += self.known.notes
        self.fb = fb
        self.registry = registry_mod.load(settings)
        self.cache = BoardCache(settings, fetcher)
        self.index = boards_mod.BoardIndex(settings)                 # one for the run: boards.json is written whole
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
        self.findings: list[boards_mod.Finding] = []

    # ---- filters, in the order that spends least first
    def wanted(self, c: Candidate, label: str) -> bool:
        """Role family and place; no network, no counters for the dedupe (a job costs a details request only after this)."""
        if cand.family_of(c, self.s) is None:
            self.run.count(label, "role_filtered")
            self.filtered_out(c.source, c.company, c.title, c.location, c.url, "role", role_reason(c.title))
            return False
        if cand.uk_ok(c, self.s):
            return True
        elsewhere = self.elsewhere(c)
        if elsewhere and self.listed_enabled:
            c.country, c.listed_only = elsewhere, True            # outside the UK: written for the record, never applied to
            return True
        self.run.count(label, "place_filtered")
        where = f"in {elsewhere}, and discover.listed_only is off" if elsewhere else f"{c.location or c.country or 'no place given'}"
        self.filtered_out(c.source, c.company, c.title, c.location, c.url, "place", f"not in the UK or remote UK ({where})")
        return False

    def filtered_out(self, source: str, company: str, title: str, location: str, link: str, which: str, reason: str) -> None:
        """Note a title the role or place filter turned away (written to paths.discover/filtered_<date>.csv by flush_filtered)."""
        self.pending_filtered.append({"ts": self.today.isoformat(timespec="seconds"), "source": source, "company": company, "title": title,
                                      "location": location, "filter": which, "reason": reason, "link": link})

    @staticmethod
    def elsewhere(c: Candidate) -> str:
        """The country a candidate that is not in the UK is in (the ad's own country, else read from its place), '' when unknown."""
        country = countries.canon(c.country) or countries.detect(c.location)
        return "" if country in ("", countries.UK) else country

    def already_decided(self, company: str, title: str, url: str | None, label: str) -> bool:
        """Is this job (by id, else by company family + title) one you skipped or applied to? Then it is not written again: `already_decided`."""
        if self.decided.hit(company, title, url):
            self.run.count(label, "already_decided")
            return True
        return False

    def duplicate(self, c: Candidate, label: str) -> bool:
        """Seen before? Not when it is a NEWER copy of an inbox file (the same job on another board of the employer): that one replaces it.
        A job you already decided on (skipped or applied, any copy of it) is counted as `already_decided`, not as a duplicate."""
        if self.already_decided(c.company, c.title, c.url, label):
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
        self.wrote(c, label)
        self.run.written.append((c, path))
        return "written"

    def wrote(self, c: Candidate, label: str) -> None:
        """Count one new inbox ad: against the UK limit, or (outside the UK) against the listed-only one; credit its registry source."""
        if c.listed_only:
            self.listed_files += 1
            self.run.count(label, "listed_only")
        else:
            self.new_files += 1
        self.run.count(label, "written")
        src = self.registry.attribute(c)
        self.run.credit(src.id if src else "other", "listed_only" if c.listed_only else "new")

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
            self.run.notes.append("ats: stopped asking " + ", ".join(f"{h} (HTTP {c})" for h, c in sorted(self.f.backed_off.items())))

    def read_board(self, board: ats_mod.Board, finding: boards_mod.Finding, label: str) -> str:
        """Run every posting of one board through the filters; counts the ones that match the role and place into the finding."""
        try:
            posts = self.cache.postings(board)
        except (Blocked, FetchError) as e:
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

    def ats_report(self) -> list[str]:
        """Per company: the tokens tried, the board found (provider/token) or none, the postings that matched. Boards found first."""
        fs = self.findings
        got = [f for f in fs if f.boards]
        lines = [f"ats: {len(fs)} company(ies) looked at · {len(got)} with a board · {len(fs) - len(got)} without · "
                 f"{sum(f.matched for f in fs)} job(s) matched the role and place"]
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
        if c is None and company:
            found = self.lookup.find(company, title, location)
            if found:
                c = posting_candidate(found[0], found[1], origin)
                step = "board_guessed" if found[0].origin == "alert" else "board_known"
            elif not why:
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
        link_kind = applykind.kind_of(link) or "aggregator_only"
        if c is None:
            if link_kind == "workday":
                why = "Workday careers site (nothing is fetched from it): " + (why or "no company given")
            self.run.credit(source_id or kind, "unresolved")
            self.to_queue(label, kind, company, title, location, link, why or "no company given", seen_at=seen_at, apply_kind=link_kind)
            return "queued"
        self.run.count(label, "resolved")
        self.run.count(label, f"resolved_via_{step}")
        self.run.credit(source_id or kind, "resolved")
        self.run.credit(source_id or kind, f"via_{step}")
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
                    continue
                if self.known.where(item.company, item.title, item.location):
                    self.run.count(label, "duplicate")
                    continue
                probe = Candidate(source=kind, company=item.company, title=item.title, location=item.location, url=item.link,
                                  description="")
                if cand.family_of(probe, self.s) is None:
                    self.run.count(label, "role_filtered")
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
        lines += step_lines({s: dict(c) for s, c in total.items()}, f"Alerts resolved by step, all {len(runs)} run(s):")
    return "\n".join(lines)


def step_lines(sources: dict, title: str) -> list[str]:
    """'Alerts resolved by step: board_known 3 · board_guessed 5 · reed 1' from the resolved_via_<step> counters; [] when none was resolved."""
    got = Counter()
    for c in sources.values():
        for step in RESOLVED_BY:
            got[step] += int(c.get(f"resolved_via_{step}", 0))
    if not sum(got.values()):
        return []
    return [f"{title} " + " · ".join(f"{step} {got[step]}" for step in RESOLVED_BY)]


def resolution_lines(by_source: dict, title: str) -> list[str]:
    """Per source: how many of the alerts that needed a full ad found one ('linkedin  12 of 20 (60%)'). [] when none needed one."""
    rows = []
    for sid in sorted(by_source):
        ok, no = int(by_source[sid].get("resolved", 0)), int(by_source[sid].get("unresolved", 0))
        if ok + no:
            rows.append(f"  {sid:16s} {ok} of {ok + no} ({100 * ok / (ok + no):.0f}%)")
    return [title, *rows] if rows else []
