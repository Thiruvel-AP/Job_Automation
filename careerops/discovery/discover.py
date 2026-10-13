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
  adzuna   the Adzuna API: its descriptions are snippets, so each result is a LINK, resolved to the employer's own board (or, for a
           CV-Library address, to the CV-Library page); what cannot be resolved goes to the manual queue
  gmail    the job-alert emails of every source in config/sources.yaml that lists sender domains (LinkedIn, CV-Library, Indeed, Reed
           and the rest) through the read-only Gmail API; the same resolution; LinkedIn and Indeed pages are never fetched
  jobbank  Canada's Job Bank open-data CSV (jobbank.py): rows are only LISTED (no employer, no ad text), in listed_only.csv

The source registry (config/sources.yaml, registry.py) says which sources exist and how each is read; health.py checks each of them
after every run. Outside the UK: a job whose place is another country (the ad's own country, else read from its place) is not dropped
but written with `Country:` and `Status: listed_only` headers (at most discover.listed_only.max_per_run a run, apart from the
UK limit); `careerops live` will not apply to it without a visa block for that country in applicant.yaml, and its priority S is
priority.sponsor_weights.non_uk. Jobs already applied to through AIApply (paths.aiapply_applied, company + title) are skipped as duplicates.

The manual queue (paths.jds/manual_queue.csv, append-only, one row per link) lists what could not be resolved, for
`careerops live --clipboard`. Sponsorship is never a filter here: the output is ordered by priority P = F x S x R (priority.py).
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ..apply.naming import folder_key
from ..db import DB
from . import ats as ats_mod
from . import candidate as cand
from . import boards as boards_mod
from . import countries, gates, gmail, intake, jobbank, manual, priority, reed, registry as registry_mod, seeds as seeds_mod
from . import register as sponsors
from .candidate import Candidate
from .http import Blocked, FetchError, Fetcher
from .sources import html_to_text

MANUAL_COLUMNS = ["ts", "source", "company", "title", "location", "link", "reason"]
LISTED_COLUMNS = ["ts", "source", "country", "company", "title", "location", "posted", "salary", "ref", "link", "note"]
ALERT_SUBJECT = re.compile(r"job|vacanc|stellen|vacature|alert|match|recommend|new roles?|positions?|offres?|opportunit", re.I)
COMPANY_COLUMNS = ("company", "company_name", "employer", "employer_name", "organisation", "organization")
TITLE_COLUMNS = ("title", "job_title", "job title", "position", "role", "job")
SNIPPET_MAX_CHARS = 1500           # an Adzuna description shorter than this (or cut with an ellipsis) is a snippet, not the ad
TITLE_MATCH = 0.88                 # similarity at which an alert's title is taken for a posting's title on the employer's board
COUNTERS = ("alerts", "seen", "role_filtered", "place_filtered", "closed", "duplicate", "written", "listed_only", "listed_capped",
            "already_in_inbox", "bad_header", "resolved", "to_manual_queue")


# ------------------------------------------------------------------ the run record
@dataclass
class Run:
    started: str = ""
    sources: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))      # by the step that found it (ats, gmail ...)
    by_source: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))    # by registry source id: new, listed_only, alerts
    notes: list[str] = field(default_factory=list)
    written: list[tuple[Candidate, Path]] = field(default_factory=list)
    dry_run: bool = False

    def count(self, source: str, what: str, n: int = 1) -> None:
        self.sources[source][what] += n

    def credit(self, source_id: str, what: str, n: int = 1) -> None:
        self.by_source[source_id][what] += n

    def new_jobs(self, source_id: str) -> int:
        """Ads written to the inbox plus rows listed for this source in this run."""
        c = self.by_source.get(source_id) or {}
        return int(c.get("new", 0)) + int(c.get("listed_only", 0))

    def as_json(self) -> dict:
        return {"ts": self.started, "dry_run": self.dry_run, "sources": {s: dict(c) for s, c in self.sources.items()},
                "by_source": {s: dict(c) for s, c in self.by_source.items()}, "notes": self.notes[:50]}


# ------------------------------------------------------------------ what is already known
class Known:
    """Jobs already seen: in the database, in the inbox / done / rejected folders, in the application archive and in this run.
    A job is the same when its company, normalised title and place agree; a place that is missing on either side matches any."""

    def __init__(self):
        self.full: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)       # (company, title) -> {place: where}
        self.notes: list[str] = []                                                 # what could not be read while loading

    def add(self, company: str, title: str, location: str, where: str) -> None:
        c, t, p = cand.dedupe_key(company, title, location)
        if c and t:
            self.full[(c, t)].setdefault(p, where)

    def where(self, company: str, title: str, location: str) -> str | None:
        c, t, p = cand.dedupe_key(company, title, location)
        places = self.full.get((c, t))
        if not places:
            return None
        if p in places:
            return places[p]
        if p == "" or "" in places:
            return next(iter(places.values()))
        return None


def _header_of(path: Path) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(10))
    except OSError:
        return {}
    return manual.header_fields(head)


def archive_pairs(root: Path) -> list[tuple[str, str]]:
    """(company folder, role folder) names in the application archive. READ ONLY: directory listings, nothing is opened,
    written, renamed or moved. A role folder 'AI Engineer' or a version folder below it ('v2') both name the role."""
    out: list[tuple[str, str]] = []
    try:
        companies = [e for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return out
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
                    k.add(h["company"], h["title"], h.get("location", ""), f"already in jds/{sub}")
    for company, role in archive_pairs(settings.path("applications")):
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
def queue_manual(settings, source: str, company: str, title: str, location: str, link: str, reason: str,
                 *, now: datetime | None = None) -> bool:
    """Append a row to paths.jds/manual_queue.csv unless that link (or, without one, that company + title) is already in it.
    True when a row was added."""
    path = settings.path("jds") / "manual_queue.csv"
    have = read_manual_queue(path)
    key = link or f"{company}|{title}"
    if any((r.get("link") or f"{r.get('company')}|{r.get('title')}") == key for r in have):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANUAL_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({"ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "source": source, "company": company,
                    "title": title, "location": location, "link": link, "reason": reason})
    return True


def read_manual_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ boards: cache, lookup
class BoardCache:
    """Posting lists per board kept in paths.discover/boards/<ats>-<slug>.json for discover.cache_hours, and which boards a company
    name was found to have (paths.discover/company_boards.json) for ats.reprobe_after_days, misses included."""

    def __init__(self, settings, fetcher: Fetcher, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.dir = settings.path("discover")
        self.hours = float(settings["discover"].get("cache_hours", 6))
        self.reprobe_days = float(settings["companies"].get("reprobe_after_days", 30))
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

    # company -> board lookups found by probing
    def _lookups(self) -> dict:
        try:
            return json.loads((self.dir / "company_boards.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def remembered(self, company_norm: str) -> dict | None:
        d = self._lookups().get(company_norm)
        if d and self.clock() - datetime.fromisoformat(d["ts"]) < timedelta(days=self.reprobe_days):
            return d
        return None

    def remember(self, company_norm: str, board: ats_mod.Board | None) -> None:
        d = self._lookups()
        d[company_norm] = {"ts": self.clock().isoformat(), **({"ats": board.ats, "slug": board.slug, "name": board.name} if board else {})}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "company_boards.json").write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass


class BoardLookup:
    """Which board a company's jobs are on, and the posting on it that matches a title: the boards you listed, the register
    boards `ats.resolve` verified, and (a few per run) boards found by probing slugs derived from the company name."""

    def __init__(self, settings, db: DB, fetcher: Fetcher, cache: BoardCache, probes: int | None = None,
                 extra_boards: list[ats_mod.Board] | None = None):
        self.settings, self.fetcher, self.cache = settings, fetcher, cache
        self.boards = merge_boards(ats_mod.all_boards(settings, db), extra_boards or [])
        self.by_name: dict[str, list[ats_mod.Board]] = defaultdict(list)
        for b in self.boards:
            self.by_name[sponsors.normalise(b.name)].append(b)
            self.by_name["".join(sponsors.normalise(b.slug.replace("-", " ")).split())].append(b)
        self.probes = int(settings["discover"].get("alert_probes_per_run", 12)) if probes is None else probes

    def boards_for(self, company: str) -> list[ats_mod.Board]:
        norm = sponsors.normalise(company)
        found = list(self.by_name.get(norm, [])) or list(self.by_name.get("".join(norm.split()), []))
        if found or not norm:
            return found
        mem = self.cache.remembered(norm)
        if mem is not None:
            return [ats_mod.Board(mem["ats"], mem["slug"], mem["name"], "resolved")] if mem.get("ats") else []
        if self.probes <= 0:
            return []
        self.probes -= 1
        board = self._probe(company, norm)
        self.cache.remember(norm, board)
        return [board] if board else []

    def _probe(self, company: str, norm: str) -> ats_mod.Board | None:
        for slug in ats_mod.slug_candidates(company):
            full = slug in ("".join(norm.split()), "-".join(norm.split()))
            try:
                hits = ats_mod.probe(slug, fetcher=self.fetcher, settings=self.settings)
            except Exception:  # noqa: BLE001
                return None
            for h in hits:
                ok, _ = ats_mod.verify_rule(h, norm, full)
                if ok:
                    return ats_mod.Board(h.ats, h.slug, company, "resolved")
        return None

    def find(self, company: str, title: str, location: str = "") -> tuple[ats_mod.Board, ats_mod.Posting] | None:
        """The posting on the company's board whose title is the title (same normalised title, or >= TITLE_MATCH similar), preferring
        the one in the same place. None when the company has no known board or the board has no such posting."""
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
                if s >= TITLE_MATCH:
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


def posting_candidate(board: ats_mod.Board, p: ats_mod.Posting, origin: str = "") -> Candidate:
    return Candidate(source=board.ats.replace("lever_eu", "lever"), company=board.name or p.company, title=p.title,
                     location=p.location, url=p.url, description=p.description, posted=p.posted_at or "", ext_id=p.ext_id,
                     country=p.country, origin=origin, board=f"{board.ats}:{board.slug}")


# ------------------------------------------------------------------ the pipeline for one candidate
class Discover:
    def __init__(self, settings, db: DB, fetcher: Fetcher, *, fb: dict | None = None, dry_run: bool = False, limit: int | None = None,
                 today: datetime | None = None, say: Callable[[str], None] = print):
        self.s, self.db, self.f, self.say, self.dry_run = settings, db, fetcher, say, dry_run
        self.today = today or datetime.now(timezone.utc)
        self.limit = limit if limit is not None else int(settings["discover"].get("max_new_per_run", 40))
        self.known = load_known(settings, db)
        self.run = Run(started=self.today.isoformat(timespec="seconds"), dry_run=dry_run)
        self.run.notes += self.known.notes
        self.fb = fb
        self.registry = registry_mod.load(settings)
        self.cache = BoardCache(settings, fetcher)
        self.lookup = BoardLookup(settings, db, fetcher, self.cache, extra_boards=self.registry.boards())
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
            return False
        if cand.uk_ok(c, self.s):
            return True
        elsewhere = self.elsewhere(c)
        if elsewhere and self.listed_enabled:
            c.country, c.listed_only = elsewhere, True            # outside the UK: written for the record, never applied to
            return True
        self.run.count(label, "place_filtered")
        return False

    @staticmethod
    def elsewhere(c: Candidate) -> str:
        """The country a candidate that is not in the UK is in (the ad's own country, else read from its place), '' when unknown."""
        country = countries.canon(c.country) or countries.detect(c.location)
        return "" if country in ("", countries.UK) else country

    def duplicate(self, c: Candidate, label: str) -> bool:
        where = self.known.where(c.company, c.title, c.location)
        if where:
            self.run.count(label, "duplicate")
            return True
        return False

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
        if self.duplicate(c, label):
            return "dropped"
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
            self.known.add(c.company, c.title, c.location, "found earlier in this run")
            self.wrote(c, label)
            self.run.written.append((c, self.inbox / intake.file_name(c.to_ad())))
            return "written"
        try:
            path, created = intake.write_inbox(self.inbox, c.to_ad())
        except intake.IntakeError as e:
            self.run.count(label, "bad_header")
            self.run.notes.append(f"{c.company} | {c.title}: {e}")
            return "dropped"
        self.known.add(c.company, c.title, c.location, "found earlier in this run")
        if not created:
            self.run.count(label, "already_in_inbox")
            return "exists"
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

    def to_queue(self, label: str, source: str, company: str, title: str, location: str, link: str, reason: str) -> None:
        if self.dry_run:
            self.run.count(label, "to_manual_queue")
            return
        if queue_manual(self.s, source, company, title, location, link, reason, now=self.today):
            self.run.count(label, "to_manual_queue")

    # ---- sources
    def from_boards(self) -> None:
        """Boards of the seed companies (seeds.py: archive folder names, discover.sources, register companies among them), found by
        guessing board tokens from each name (boards.py), plus the boards you named token by token in sources:. One company at a
        time: find its board, read its postings, move on. self.findings is the per-company report (ats_report)."""
        label = "ats"
        self.findings = []
        index = boards_mod.BoardIndex(self.s)
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
            self.run.notes.append(str(e) if isinstance(e, Blocked) else f"{board.ats}:{board.slug}: {e}")
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
        lines += [f.line() for f in got] + [f.line() for f in fs if not f.boards]
        return lines
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
                                  posted=str(j.get("created") or ""), ext_id=str(j.get("id") or ""), country="GB")   # the /gb/ endpoint
                    if self.adzuna_one(c, label) == "full":
                        return
                if len(results) < 50:
                    break

    def adzuna_one(self, c: Candidate, label: str) -> str:
        """Adzuna's description is a snippet unless it is long and not cut: a full one is used as it is; otherwise the result
        is a link and is looked for on the employer's own board (D1) or, when the link is a CV-Library job, fetched from there."""
        text = c.description.strip()
        if len(text) >= SNIPPET_MAX_CHARS and not text.endswith(("…", "...")):
            return self.accept(c, label)
        self.run.count(label, "seen")
        if not self.wanted(c, label) or self.duplicate(c, label):
            return "dropped"
        return self.resolve_link(label, "adzuna", c.company, c.title, c.location, c.url, "", counted=True)

    # ---- a link or a (company, title): look for the whole ad
    def resolve_link(self, label: str, kind: str, company: str, title: str, location: str, link: str, origin: str,
                     counted: bool = False, default_country: str = "") -> str:
        """Find the full ad for something that is only a pointer. Order: a CV-Library job link (C2), a Reed job link (with a key),
        the employer's own board (D1). Anything else goes to the manual queue, with the reason."""
        host = (urlparse(link).hostname or "").lower()
        c: Candidate | None = None
        why = ""
        if host.endswith("cv-library.co.uk") and intake.CVLIBRARY_JOB.fullmatch(urlparse(link).path):
            try:
                ad = intake.fetch_ad(link, robots=self.f.robots)
                c = Candidate(source=ad.source, company=ad.company, title=ad.title, location=ad.location, url=ad.url,
                              description=ad.text, posted=ad.posted, agency=ad.agency, salary=ad.salary, ext_id=ad.ad_id, origin=origin,
                              country="GB" if ad.source == "cvlibrary" else "")
            except intake.IntakeError as e:
                why = str(e)
        elif host.endswith("reed.co.uk") and reed.job_id_from_url(link) and os.environ.get("REED_API_KEY"):
            try:
                d = reed.details(self.f, reed.api_key(), reed.job_id_from_url(link))
                c = reed.to_candidate(d, False, self.today) if d else None
                why = "" if d else "Reed returned no ad for that link (closed?)"
            except (FetchError, reed.ReedKeyMissing) as e:
                why = str(e)
        if c is None and company:
            found = self.lookup.find(company, title, location)
            if found:
                c = posting_candidate(found[0], found[1], origin)
            elif not why:
                boards = self.lookup.boards_for(company)
                why = (f"no posting titled like this on the {boards[0].ats} board of {company}: closed, or titled differently"
                       if boards else f"no public job board known for {company}")
        if c is None:
            self.to_queue(label, kind, company, title, location, link, why or "no company given")
            return "queued"
        self.run.count(label, "resolved")
        c.origin = origin
        if default_country and not c.country:               # an alert from a one-country source: that country unless the place says another
            c.country = countries.detect(c.location) or default_country
        return self.accept(c, label, counted=counted)

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

    def from_gmail(self) -> None:
        label = "gmail"
        g = self.s["discover"].get("gmail") or {}
        try:
            token = gmail.access_token(self.s)
        except gmail.GmailAuthError as e:
            self.run.notes.append(str(e))
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
            return
        done_now: list[str] = []
        for mid in ids:
            if mid in seen:
                continue
            try:
                frm, subject, html, text = gmail.message_text(gmail.get_message(self.f, token, mid))
            except (FetchError, Blocked) as e:
                self.run.notes.append(f"gmail message {mid}: {e}")
                break
            src = self.alert_source(frm, subject)
            done_now.append(mid)
            if src is None:
                continue
            kind = src.kind
            dedicated = kind in gmail.JOB_LINK
            parsed = (gmail.parse_alert(kind, html, text, mid) if dedicated else
                      gmail.parse_alert(kind, html, text, mid, link_fn=lambda href, s=src: gmail.generic_link(s, href)))
            if not parsed and not dedicated and ALERT_SUBJECT.search(subject or ""):
                # a source without a dedicated parser whose alert showed no job link we recognise: never guessed at, handed to you
                self.run.count(label, "alerts_unparsed")
                self.to_queue(label, src.id, "", (subject or "")[:120], "", gmail.web_link(mid),
                              f"alert email from {src.name}: no job links were recognised in it; open the email and add the ads you want")
            default_country = src.home_country if src.home_country not in ("", countries.UK) else ""
            for item in parsed:
                self.run.count(label, "alerts")
                self.run.count(label, f"alerts_{kind}")
                self.run.credit(src.id, "alerts")
                if self.known.where(item.company, item.title, item.location):
                    self.run.count(label, "duplicate")
                    continue
                probe = Candidate(source=kind, company=item.company, title=item.title, location=item.location, url=item.link,
                                  description="")
                if cand.family_of(probe, self.s) is None:
                    self.run.count(label, "role_filtered")
                    continue
                if self.resolve_link(label, kind, item.company, item.title, item.location, item.link, f"gmail:{kind}",
                                     default_country=default_country) == "full":
                    break
        if not self.dry_run:
            try:
                seen_path.parent.mkdir(parents=True, exist_ok=True)
                seen_path.write_text(json.dumps(sorted(seen | set(done_now))[-2000:]), encoding="utf-8")
            except OSError:
                pass

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
            job = {"id": c.ad_id(), "company": c.company, "description": c.description, "sponsor_match": match, "agency": c.agency,
                   "posted_at": c.posted or None, "ceiling_pre": pre, "country": c.country if c.listed_only else None}
            out.append((priority.priority(job, self.s, tailored=False, today=self.today), c, path))
        return sorted(out, key=lambda t: (-t[0].p, -(t[0].s * t[0].r), t[1].company, t[1].title))

    def listing(self, n: int = 10) -> list[str]:
        lines = []
        for pr, c, path in self.ranked()[:n]:
            lines.append(f"  {pr.p:.2f} = {pr.f:.2f} x {pr.s:.2f} x {pr.r:.2f}  {c.company} | {c.title} | {c.location or '?'} | {c.url}  "
                         f"[{c.source}{', agency' if c.agency else ''}{', listed only' if c.listed_only else ''}; {pr.s_reason}]  {path.name}")
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


def format_report(settings) -> str:
    """Counts per source over all runs and for the last one, and what is in the manual queue (resolved vs sent to it)."""
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
    resolved = sum(c.get("resolved", 0) for c in total.values())
    lines.append(f"Resolved to a full ad: {resolved} · in the manual queue: {len(queue)} "
                 f"({', '.join(f'{k} {v}' for k, v in sorted(Counter(r['source'] for r in queue).items())) or 'empty'})")
    if queue:
        lines.append(f"  manual queue: {settings.path('jds') / 'manual_queue.csv'} — for each row, copy the ad and run "
                     "`careerops live --clipboard`")
    return "\n".join(lines)
