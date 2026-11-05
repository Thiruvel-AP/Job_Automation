"""`careerops shortlist`: the human promotes. The inbox is scored (discovery/triage.py), the hard stops are applied, and the best few are OFFERED; only
the ones you approve go to data/jds/approved/<date>/, and `careerops live` reads nothing else.

    1. every inbox ad gets the triage score T (zero tokens) and its row in the database (triage_scores);
    2. the hard disqualifiers (blacklist, the same role under another channel, the level exclude list, your dealbreakers) file the ad in jds_done/skipped/
       with the reason beside it; it is never offered. A closed posting goes to jds_done/closed/ (liveness is asked only for the ads that would otherwise
       be offered, best T first, because it costs a request each);
    3. offered = T >= targeting.min_triage_score, best T first then freshest, at most targeting.shortlist_size; an ad older than targeting.max_posting_age_days
       needs T >= 4.5; a company at targeting.per_company_max_open open applications, or inside its cooldown, is left out (apply/volume.py);
    4. [a] approves all, except a possible repost of a job you skipped (that needs its number typed) · `1 3 5` approves those ·
   `s 2` skips job 2 for good (filed in jds_done/skipped/, so discover does not bring it back) ·
   [x] skips every job in the list that you did not approve · [q] quits.
Nothing is tailored here, and nothing is sent anywhere.

Region and size (v2.4): an ad outside the chosen region(s) (discovery/region.py) is PARKED, never skipped: it is not scored or offered, and a later
`--region` brings it back. After the other filters the company size of each ad (discovery/size.py) is worked out and the sizes in the queue are
shown before you choose: `sme` / `large` keep that bucket and list the size-unknown ads under [u] (they count towards the daily limit only when you press
u); `sme-first` / `large-first` order by T + 0.3 for the preferred bucket and drop nobody; `both` changes nothing. [i] prints the evidence of each line.

What it did not read (v2.7): when the last `discover` run did not read the alert emails (no --gmail-alerts, or Gmail failed), the list starts with
`alerts not read since <time>: run discover --gmail-alerts`. `careerops daily` runs discover --ats --gmail-alerts --report and then this.

Alerts that could not be traced to the employer (the manual queue, paths.jds/manual_queue.csv and its overflow) are their own block below the list:
`From alerts, apply on the board (N): company | title | board | link`, the top ALERT_BLOCK_SIZE (no ad text, so E is unknown and is written `E ?`; the freshness is the
day it was queued). They are ranked by A, Fr and V at T's weights (Triage.alert_rank), then by how well the title matches roles.families, then freshest. Rows with the
same company and title (the same job on LinkedIn and Indeed) are one row with all their links. A link on a CV-Library job page is marked `[agency or board-only]`: the
employer's own board was not found, so the ad is on CV-Library (or an agency) only. `c n` prints what to do: open the link, copy the whole ad, run `careerops live
--clipboard`; `s c n` dismisses one (parked, `unskip --shortlist` brings it back). The board page is never fetched here.

`--all` lists every undecided in-region ad again, not only the new ones: an ad parked after being shown PARK_AFTER times is included, a list with
nothing new is still printed, and the list is not counted as a showing (so it never parks anything). The caps and the age / T rules still apply.

Repeats: an ad already approved (waiting in data/jds/approved/), skipped, applied to, closed, or parked is never offered again. Every list you were asked
about is logged in data/jds/shortlist_log.jsonl (times_shown per job); an ad shown in PARK_AFTER lists with no decision is parked: it stays in the inbox but is
only counted ("parked: N"). `careerops unskip --shortlist` brings skipped and parked ads back. With nothing new since the last list, no list is printed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .. import targeting as targeting_mod
from ..apply import decided, targets, volume
from ..apply import decisions as decisions_mod
from ..db import DB
from . import applykind, freshness, liveness, triage, workcountry
from . import candidate as cand
from .gmail import CV_TRACKING_HOST
from . import region as region_mod
from . import register as sponsors
from . import size as size_mod
from .http import Fetcher

APPROVED = "approved"
HAVE_SHOWN, LACK_SHOWN = 3, 2
LOG = "shortlist_log.jsonl"
PARK_AFTER = 3                                  # lists an ad may be shown in without a decision before it is parked
SKIP_MARK = "(user, shortlist)"
SKIP_REASON = f"skipped {SKIP_MARK}: skipped at the shortlist, not offered again"
SIZE_FIRST_BONUS = 0.3                          # T added for the preferred bucket by `sme-first` / `large-first`
ALERT_BLOCK_SIZE = 10                           # alerts that could not be traced to the employer: at most this many are listed, best first (A, Fr, V, title match)
BOARD_ONLY = "agency or board-only"             # a CV-Library job page: the ad is on CV-Library (or an agency) only, the employer's own board was not found


def approved_root(settings) -> Path:
    return settings.path("jds") / APPROVED


def approved_files(settings, db=None) -> list[Path]:
    """The JD files you approved: every .txt in data/jds/approved/<date>/. The only files `careerops live` works on. With `db`, a file whose job you
    skipped or applied to since (the decision log, by id / ATS key / URL key: v2.5) is left out: a skip overrides an earlier approval."""
    root = approved_root(settings)
    try:
        files = sorted(p for d in root.iterdir() if d.is_dir() for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".txt")
    except OSError:
        return []
    if db is None:
        return files
    known = decided.load(settings, db)
    keep = []
    for p in files:
        hdr = decided.header_of(p)
        if hdr.get("company") and hdr.get("title") and known.hit(hdr["company"], hdr["title"], hdr.get("url"), hdr.get("location")):
            continue
        keep.append(p)
    return keep


@dataclass
class Row:
    path: Path
    job: dict
    t: triage.Triage
    kind: str
    shown: int = 0                              # lists this job was in before this one (shortlist_log.jsonl)
    region: region_mod.Match | None = None      # in / unknown (an out ad is parked before it becomes a Row)
    size: size_mod.Size | None = None           # None when the size was not asked for (no resolver)
    repost: str = ""                            # v2.5: "possible repost of a job you skipped on <date> (<id>)": only company + title + country match

    @property
    def name(self) -> str:
        return f"{self.job['company']} | {self.job['title']}"

    @property
    def key(self) -> str:
        return _key(self.job["company"], self.job["title"])

    def line(self, n: int) -> str:
        age = "date unknown" if self.t.age_days is None else f"{self.t.age_days:.0f} d"
        seen = f" (shown {self.shown}×)" if self.shown else ""
        return (f"{n:>2}. {self.name} | {age} | {self.t.text()} | have: {', '.join(self.t.have[:HAVE_SHOWN]) or '-'} | "
                f"lack: {', '.join(self.t.lack[:LACK_SHOWN]) or '-'} | {self.kind}{seen}{self.tags()}")

    def tags(self) -> str:
        """` [SME·small] [region?]`: the size and the unknown region of the line (nothing for what was not asked)."""
        got = ([f"[{self.size.tag}]"] if self.size is not None else []) + ([f"[{region_mod.FLAG}]"] if self.region and self.region.flag else [])
        got += [f"[{self.repost}]"] if self.repost else []
        return (" " + " ".join(got)) if got else ""

    def evidence(self, n: int) -> str:
        """What [i] prints: the evidence behind the size and the region of the line."""
        bits = ([f"size {self.size.tag}: {self.size.evidence or 'no evidence'}"] if self.size is not None else []) + \
               ([f"region {self.region.verdict}: {self.region.evidence}"] if self.region is not None else [])
        return f"  {n:>2}. {self.name}: " + (" | ".join(bits) or "nothing recorded")


@dataclass
class AlertRow:
    """An alert that could not be traced to the employer's own board (a manual-queue row): there is no ad text, so it is listed with the link to apply on the board."""
    company: str
    title: str
    board: str                                  # where the alert came from (linkedin, indeed ...), with the kind of page when it is the employer's own (workday ...)
    link: str
    t: triage.Triage
    location: str = ""
    repost: str = ""                            # "possible repost of a job you skipped on <date> (<id>)": only company + title + country match (v2.5: shown, never skipped on that alone)
    links: list[str] = field(default_factory=list)    # every link of the same company + title (the same job in two alerts): `link` is the first
    boards: list[str] = field(default_factory=list)   # ... and where each came from
    rank: float = 0.0                           # Triage.alert_rank: A, Fr, V at T's weights
    relevance: float = 0.0                      # candidate.title_relevance: how close the title is to roles.families
    board_only: bool = False                    # a link is a CV-Library job page: agency or board-only

    @property
    def name(self) -> str:
        return f"{self.company} | {self.title}"

    @property
    def key(self) -> str:
        return _key(self.company, self.title)

    @property
    def all_links(self) -> list[str]:
        return self.links or [self.link]

    def line(self, n: int) -> str:
        return (f"  c{n}. {self.company} | {self.title} | {', '.join(self.boards) or self.board} | {' · '.join(self.all_links)}  ({self.t.text(e_unknown=True)})"
                + (f" [{self.repost}]" if self.repost else ""))

    def steps(self, n: int) -> str:
        links = self.all_links
        where = links[0] + (f" (or {', '.join(links[1:])})" if links[1:] else "")
        return (f"  c{n}. {self.name} ({', '.join(self.boards) or self.board}): open {where} in your browser, copy the whole ad, then run  careerops live --clipboard\n"
                f"      (it asks for Company: {self.company} · Job title: {self.title} · Application URL; this program never opens or fetches that page)"
                + (f"\n      CV-Library job page: {BOARD_ONLY}, the employer's own board was not found" if self.board_only else ""))


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    dropped: list[tuple[Path, str, str]] = field(default_factory=list)       # (file, folder in jds_done, reason line)
    held: list[tuple[str, str]] = field(default_factory=list)                # (company | role, why not offered)
    scored: int = 0
    below_min: int = 0
    too_old: int = 0
    unreadable: int = 0
    approved: int = 0                                                        # inbox ads whose job is already approved (waiting in approved/)
    decided: int = 0                                                         # inbox ads whose job is skipped, applied to or closed
    parked: list[str] = field(default_factory=list)                          # (company | role) of the inbox ads parked earlier
    new: int = 0                                                             # offered rows never shown before
    last_shown: datetime | None = None                                       # when the last list was shown to you
    selection: tuple[str, ...] | None = None                                 # the region(s) the list was built for (None: no region filter)
    region_parked: list[str] = field(default_factory=list)                   # (company | role) of the ads outside the region: parked (region), not skipped
    region_unknown: int = 0                                                  # ads kept although the place did not say where the work is (flag region?)
    size_mode: str | None = None                                             # sme | large | both | sme-first | large-first (None: size not asked)
    size_counts: Counter | None = None                                       # SME / large / unknown of the ads that passed every other filter
    size_hidden: int = 0                                                     # ads of the other bucket under `sme` / `large`
    unknown_rows: list[Row] = field(default_factory=list)                    # size-unknown ads under `sme` / `large`: listed under [u], offered only when you press u
    alive: Callable[[dict], str | None] | None = None                        # liveness check, used when [u] adds an ad
    size_notes: list[str] = field(default_factory=list)                      # what the size lookups said (no key, lookup limit, refusals)
    show_all: bool = False                                                   # --all: every undecided in-region ad, not only the new ones
    alert_rows: list[AlertRow] = field(default_factory=list)                 # the manual queue's best ALERT_BLOCK_SIZE (A, Fr, V, title match), one row per company + title
    alert_total: int = 0                                                     # ... of this many that could be listed


@dataclass
class History:
    shown: dict[str, int] = field(default_factory=dict)                      # job key -> lists it was shown in since its last reset
    parked: dict[str, str] = field(default_factory=dict)                     # job key -> "company | role"
    names: dict[str, str] = field(default_factory=dict)
    last: datetime | None = None


def log_path(settings) -> Path:
    return settings.path("jds") / LOG


def read_history(settings) -> History:
    """Replay shortlist_log.jsonl: `shown` adds one to a job's count, `parked` parks it, `reset` (unskip) clears both. A line that is not JSON is ignored."""
    h = History()
    try:
        text = log_path(settings).read_text(encoding="utf-8")
    except OSError:
        return h
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        key, ev = (e.get("key"), e.get("event")) if isinstance(e, dict) else (None, None)
        if not key:
            continue
        h.names[key] = f"{e.get('company', '')} | {e.get('title', '')}"
        if ev == "shown":
            h.shown[key] = h.shown.get(key, 0) + 1
            try:
                h.last = datetime.fromisoformat(str(e["ts"]))
            except (KeyError, ValueError):
                pass
        elif ev == "parked":
            h.parked[key] = h.names[key]
        elif ev == "reset":
            h.shown.pop(key, None)
            h.parked.pop(key, None)
    return h


def log_events(settings, events: list[dict], now: datetime) -> None:
    """Append one line per event to shortlist_log.jsonl (append-only; never rewritten)."""
    if not events:
        return
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), **e}, ensure_ascii=False) + "\n")


def _event(row: Row, event: str, **extra) -> dict:
    return {"event": event, "key": row.key, "company": row.job["company"], "title": row.job["title"], **extra}


def kind_of(job: dict) -> str:
    return (job.get("apply_kind") or applykind.kind_of(job.get("apply_url") or job.get("url"))).strip() or "unknown"


def _key(company: str, title: str) -> str:
    return "|".join(decided.key_of(company, title))


def save_score(db: DB, job: dict, t: triage.Triage, on: date, region: region_mod.Match | None = None) -> None:
    with db.conn:
        db.conn.execute("INSERT OR REPLACE INTO triage_scores (key, company, title, t, factors, scored_on, region_match, region_evidence) VALUES (?,?,?,?,?,?,?,?)",
                        (_key(job["company"], job["title"]), job["company"], job["title"], round(t.t, 3), json.dumps(t.factors()), on.isoformat(),
                         region.verdict if region else None, region.evidence if region else None))


def save_size(db: DB, row: Row) -> None:
    """The size of an offered-or-pooled ad beside its score (`careerops patterns` reads it)."""
    if row.size is None:
        return
    with db.conn:
        db.conn.execute("UPDATE triage_scores SET size=?, size_label=?, size_evidence=? WHERE key=?",
                        (row.size.size, row.size.label, row.size.evidence, row.key))


def make_alive(fetcher: Fetcher | None, cache, now: datetime) -> Callable[[dict], str | None]:
    """liveness for `disqualify`: the reason a posting is closed, else None (open, uncertain, no address, or no network)."""
    def alive(job: dict) -> str | None:
        url = job.get("url") or job.get("apply_url")
        if fetcher is None or not url:
            return None
        r = liveness.check(url, fetcher, cache, now)
        return f"the posting is closed ({r.reason})" if r.status == liveness.CLOSED else None
    return alive


def build(settings, db: DB, fb: dict, *, fetcher: Fetcher | None = None, cache=None, now: datetime | None = None,
          archive: list[tuple[str, str]] | None = None, files: list[Path] | None = None, regions: Sequence[str] | None = None,
          size_mode: str | None = None, size_resolver: size_mod.Resolver | None = None,
          choose_size: Callable[[Counter], str] | None = None, show_all: bool = False) -> Plan:
    """Score the inbox and decide what is offered. Writes the scores to the database; moves nothing (see `file_away`).

    `show_all`: also the ads that were parked after PARK_AFTER showings without a decision (every undecided in-region ad, not only the new ones).
    `regions`: the selected region keys; an ad outside them is parked (region), not scored. None = no region filter.
    `size_resolver`: works out each pooled ad's company size; `size_mode` (or `choose_size(counts)`, asked once the counts are known) then applies it."""
    now = now or datetime.now(timezone.utc)
    cfg = targeting_mod.config(settings)
    plan = Plan(selection=tuple(regions) if regions is not None else None, show_all=show_all)
    non_uk_ok = bool(regions) and (region_mod.ANY in regions or bool(region_mod.iso_of(regions) - {"GB"}))      # S = unknown, not "not home", for a job abroad
    hist = read_history(settings)
    plan.last_shown = hist.last
    known = decided.load(settings, db)                                       # the decision log, skipped / applied rows and the files in jds_done/
    for p in decided.txt_files(decided.decision_dir(settings, "closed")):
        if (k := decided.file_key(p)):
            known.add(k, "closed", decided.file_job_id(p), decided.file_keys(p))
    # an ad whose job is already approved (the same id / ATS key / URL key: v2.5, never company + title alone, so London's approval hides no Munich)
    waiting = {k for p in approved_files(settings) for k in [*decided.file_keys(p).strong(), decided.file_job_id(p) or ""] if k}
    idx = sponsors.load_index(db)
    floor = triage.salary_floor(settings)
    if archive is None:
        from . import discover
        archive = discover.archive_pairs(settings.archive_roots())
    apps = volume.applications(db, settings)
    others = [dict(r) for r in db.jobs()]
    day = now.date()
    pool = []
    for p in (files if files is not None else decided.txt_files(decided.inbox_dir(settings))):
        job = triage.inbox_job(p, settings, fb, idx)
        if job is None:
            plan.unreadable += 1
            continue
        jkeys = {*decided.file_keys(p).strong(), decided.file_job_id(p) or ""} - {""}
        if jkeys & waiting:
            plan.approved += 1
            continue
        if known.hit(job["company"], job["title"], job.get("url") or job.get("apply_url"), job.get("location")):
            plan.decided += 1
            continue
        repost = known.repost(job["company"], job["title"], job.get("url") or job.get("apply_url"), job.get("location"))
        if _key(job["company"], job["title"]) in hist.parked and not show_all:
            plan.parked.append(f"{job['company']} | {job['title']}")
            continue
        rm = None
        if regions is not None:
            rm = region_mod.job_match(job, regions)
            if rm.verdict == region_mod.OUT:
                plan.region_parked.append(f"{job['company']} | {job['title']}")
                continue
            plan.region_unknown += rm.verdict == region_mod.UNKNOWN
            job["region_target"] = non_uk_ok
        plan.scored += 1
        t = triage.score(job, settings, fb, floor=floor, now=now)
        save_score(db, job, t, day, rm)
        if (d := triage.disqualify(job, settings, db, archive=archive, others=others)) is not None:
            plan.dropped.append((p, "skipped", f"disqualified, {d.line()}"))
            continue
        if t.t < cfg.min_triage_score:
            plan.below_min += 1
            continue
        if t.age_days is not None and t.age_days > cfg.max_posting_age_days and t.t < targeting_mod.OLD_POSTING_MIN_T:
            plan.too_old += 1
            continue
        pool.append(Row(p, job, t, kind_of(job), region=rm, repost=repost))
    by_age = lambda r: r.t.age_days if r.t.age_days is not None else float("inf")        # noqa: E731
    pool.sort(key=lambda r: (-r.t.t, by_age(r), r.path.name))
    alive = make_alive(fetcher, cache if cache is not None else (liveness.cache_for(settings) if fetcher is not None else None), now)
    plan.alive = alive
    if size_resolver is not None and pool:
        for r in pool:
            r.size = size_resolver.resolve(r.job["company"], r.job.get("description") or "")
            save_size(db, r)
        plan.size_counts = size_mod.counts(r.size for r in pool)
        plan.size_notes = list(size_resolver.notes)
        if size_resolver.skipped_for_limit:
            plan.size_notes.append(f"{size_resolver.skipped_for_limit} compan{'y' if size_resolver.skipped_for_limit == 1 else 'ies'} not looked up yet: at most "
                                   f"{size_resolver.max_lookups} Companies House lookups a run (the next run continues)")
        if not size_resolver.has_key:
            plan.size_notes.append(f"{size_mod.KEY_ENV} is not set: Companies House was not asked (sizes come from your overrides and the ad text)")
        if size_mode is None and choose_size is not None:
            size_mode = choose_size(plan.size_counts)
        plan.size_mode = size_mode or "both"
        if plan.size_mode in ("sme", "large"):
            keep = [r for r in pool if r.size.size == plan.size_mode]
            plan.unknown_rows = [r for r in pool if r.size.size == size_mod.UNKNOWN]
            plan.size_hidden = len(pool) - len(keep) - len(plan.unknown_rows)
            pool = keep
        elif plan.size_mode in ("sme-first", "large-first"):
            pref = size_mod.SME if plan.size_mode == "sme-first" else size_mod.LARGE
            pool.sort(key=lambda r: (-(r.t.t + (SIZE_FIRST_BONUS if r.size.size == pref else 0.0)), by_age(r), r.path.name))
    for r in pool:
        if len(plan.rows) >= cfg.shortlist_size:
            break
        why = volume.company_block(db, settings, r.job["company"], day, apps=apps)
        if why:
            plan.held.append((r.name, why))
            continue
        if (closed := alive(r.job)):
            plan.dropped.append((r.path, "closed", f"disqualified, liveness: {closed}"))
            continue
        r.shown = hist.shown.get(r.key, 0)
        plan.rows.append(r)
    plan.new = sum(1 for r in plan.rows if not r.shown)
    plan.unknown_rows = [r for r in plan.unknown_rows if not volume.company_block(db, settings, r.job["company"], day, apps=apps)][:cfg.shortlist_size]
    for r in plan.unknown_rows:
        r.shown = hist.shown.get(r.key, 0)
    plan.alert_rows, plan.alert_total = alert_block(settings, db, fb, idx=idx, floor=floor, now=now, regions=regions, non_uk_ok=non_uk_ok, known=known,
                                                    hist=hist, archive=archive, others=others)
    return plan


def _taken_keys(settings, db: DB, others: list[dict]) -> set[str]:
    """(company, title) keys of the ads already in hand: the inbox, approved/<date>/, and the database (a job you skipped or applied to is not "in hand": the same
    company + title alone is only a possible repost, v2.5)."""
    out = {_key(r["company"], r["title"]) for r in others if not str(r.get("status") or "").startswith(("skipped", "applied"))}
    for p in [*decided.txt_files(decided.inbox_dir(settings)), *approved_files(settings)]:
        h = decided.header_of(p)
        if h.get("company") and h.get("title"):
            out.add(_key(h["company"], h["title"]))
    return out


def alert_block(settings, db: DB, fb: dict, *, idx, floor: int | None, now: datetime, regions: Sequence[str] | None, non_uk_ok: bool, known, hist: History,
                archive: list[tuple[str, str]], others: list[dict]) -> tuple[list[AlertRow], int]:
    """(the best ALERT_BLOCK_SIZE manual-queue rows by T, how many could be listed). A row is an alert `discover` could not trace to the employer's own board
    (paths.jds/manual_queue.csv and its overflow). It needs a company, a title and a link; it is left out when the job is decided, already in hand (inbox,
    approved, the database), dismissed with `s c n`, outside the chosen region, disqualified (blacklist, level, the same role under another channel), or older than
    targeting.max_posting_age_days without T >= 4.5. There is no ad text, so E is neutral; the age is the day the row was queued. Nothing is fetched."""
    from . import discover as disc

    cfg = targeting_mod.config(settings)
    raw = [*disc.read_rows(disc.manual_queue_path(settings)), *disc.read_rows(disc.overflow_path(settings))]
    if not raw:
        return [], 0
    taken = _taken_keys(settings, db, others)
    rules = workcountry.rules_from(workcountry.applicant_visa(settings))
    threshold = settings["sponsors"].get("fuzzy_threshold", 0.92)
    seen: set[str] = set()
    groups: dict[str, list[tuple[dict, str, str, str, str]]] = {}              # company + title -> its rows (the same job in two alerts is one row)
    for r in raw:
        company, title, link = (r.get("company") or "").strip(), (r.get("title") or "").strip(), (r.get("link") or "").strip()
        if not (company and title and link) or link in seen:                  # an alert e-mail nothing was read from has no company: it is not a job
            continue
        seen.add(link)
        loc = (r.get("location") or "").strip()
        key = _key(company, title)
        if key in hist.parked or key in taken or known.hit(company, title, link, loc):
            continue
        if regions is not None and region_mod.match(loc, "", regions).verdict == region_mod.OUT:
            continue
        groups.setdefault(key, []).append((r, company, title, link, loc))
    out: list[AlertRow] = []
    for rows in groups.values():
        r, company, title, link, loc = rows[0]
        ts = max((x[0].get("ts") or "" for x in rows), default="") or None            # the freshest alert of the group dates it
        work = workcountry.resolve(location=loc, description="", title=title, override="", rules=rules)
        job = {"id": link, "company": company, "title": title, "description": "", "location": loc, "url": link, "apply_url": link, "source": r.get("source") or "",
               "sponsor_match": idx.lookup(company, threshold).label if idx is not None else None, "agency": False, "work_country": work.country,
               "posted_at": ts, "posted_at_source": freshness.GMAIL if ts else None, "region_target": non_uk_ok}
        if triage.disqualify(job, settings, db, archive=archive, others=others) is not None:
            continue
        t = triage.score(job, settings, fb, floor=floor, now=now)
        if t.age_days is not None and t.age_days > cfg.max_posting_age_days and t.t < targeting_mod.OLD_POSTING_MIN_T:
            continue
        boards = list(dict.fromkeys(_board_label(x[0], x[3]) for x in rows))
        links = list(dict.fromkeys(x[3] for x in rows))
        out.append(AlertRow(company, title, boards[0], link, t, loc, known.repost(company, title, link, loc), links=links, boards=boards, rank=t.alert_rank(),
                            relevance=cand.title_relevance(title, settings), board_only=any(_cv_job_page(x[3]) for x in rows)))
    out.sort(key=lambda a: (-round(a.rank, 6), -a.relevance, a.t.age_days if a.t.age_days is not None else float("inf"), a.company, a.title))
    return out[:ALERT_BLOCK_SIZE], len(out)


def _cv_job_page(link: str) -> bool:
    """A CV-Library job page (not its click-tracker link): such an ad is on CV-Library, or with an agency, and nowhere the program could find on the employer's board."""
    host = applykind.host_of(link)
    return host.endswith("cv-library.co.uk") and host != CV_TRACKING_HOST


def _board_label(r: dict, link: str) -> str:
    """`linkedin`, `indeed/workday`, `cvlibrary [agency or board-only]`: where the alert came from, the kind of page when it is the employer's own."""
    kind = (r.get("apply_kind") or "").strip()
    return (r.get("source") or "alert") + (f"/{kind}" if kind and kind != "aggregator_only" else "") + (f" [{BOARD_ONLY}]" if _cv_job_page(link) else "")


def file_away(settings, path: Path, folder: str, reason: str) -> Path | None:
    """Move an ad to jds_done/<folder>/ with `<name>.reason.txt` beside it (never deleted, never over a file). None when it could not be moved."""
    try:
        dst = decided.move_file(settings, path, decided.decision_dir(settings, folder))
        dst.with_name(dst.stem + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
        return dst
    except (OSError, decided.DecidedError):
        return None


def apply_drops(settings, plan: Plan) -> int:
    n = 0
    for path, folder, reason in plan.dropped:
        n += file_away(settings, path, folder, reason) is not None
    return n


def approve(settings, rows: list[Row], day: date) -> list[Path]:
    """Move the approved ads to data/jds/approved/<day>/."""
    out = []
    for r in rows:
        try:
            out.append(decided.move_file(settings, r.path, approved_root(settings) / day.isoformat()))
        except (OSError, decided.DecidedError):
            pass
    return out


def parked_line(plan: Plan) -> list[str]:
    return [f"parked: {len(plan.parked)} (shown {PARK_AFTER} times with no decision; `careerops unskip --shortlist` brings them back)"] if plan.parked else []


def region_lines(plan: Plan) -> list[str]:
    """The parked (region) count: outside the chosen region, not scored, not skipped (a later --region brings them back)."""
    if not plan.region_parked:
        return []
    where = region_mod.label_of(plan.selection) if plan.selection else "the region"
    return [f"parked (region): {len(plan.region_parked)} outside {where}: not scored, not skipped; `--region` brings them back"]


def unknown_lines(plan: Plan) -> list[str]:
    """Under `sme` / `large`: the size-unknown ads, offered only when you press u."""
    if not plan.unknown_rows:
        return []
    return [f"Size unknown ({len(plan.unknown_rows)}): [u] include them"] + [f"     {r.name} ({r.size.evidence or 'no evidence'})" for r in plan.unknown_rows[:10]]


def alert_lines(plan: Plan) -> list[str]:
    """The block of alerts that could not be traced to the employer: apply on the board (nothing here is fetched)."""
    if not plan.alert_rows:
        return []
    count = f"{plan.alert_total}, the top {len(plan.alert_rows)} by A, Fr, V" if plan.alert_total > len(plan.alert_rows) else f"{len(plan.alert_rows)}"
    note = ([f"  [{BOARD_ONLY}] = a CV-Library job page: the employer's own board was not found, so this ad is on CV-Library (or with an agency) only; apply there"]
            if any(a.board_only for a in plan.alert_rows) else [])
    return [f"From alerts, apply on the board ({count}): company | title | board | link   [c n] = open the link, copy the whole ad, run `careerops live --clipboard` · [s c n] = dismiss"
            ] + note + [a.line(n) for n, a in enumerate(plan.alert_rows, 1)]


def lines(plan: Plan, cfg: targeting_mod.Targeting) -> list[str]:
    if not plan.new and not plan.show_all:                                   # nothing you have not been shown: no list
        since = f"since {plan.last_shown.astimezone():%Y-%m-%d %H:%M}" if plan.last_shown else "yet"
        return ([f"No new jobs {since}. Run careerops discover --ats --gmail-alerts."]
                + region_lines(plan) + unknown_lines(plan) + parked_line(plan) + alert_lines(plan))
    out = [f"Shortlist: {plan.scored} ad(s) scored; {len(plan.rows)} offered (at most {cfg.shortlist_size}, T >= {cfg.min_triage_score:g}). "
           f"Left out: {len(plan.dropped)} disqualified, {plan.below_min} under T {cfg.min_triage_score:g}, "
           f"{plan.too_old} older than {cfg.max_posting_age_days} d without T >= {targeting_mod.OLD_POSTING_MIN_T:g}, {len(plan.held)} at a company limit"
           + (f", {plan.approved} already approved" if plan.approved else "") + (f", {plan.decided} already decided" if plan.decided else "")
           + (f", {plan.unreadable} unreadable" if plan.unreadable else "") + (f", {plan.size_hidden} in the other size bucket" if plan.size_hidden else "") + ".",
           f"new since last shortlist: {plan.new}" + (" (--all: every undecided in-region ad is listed, not only the new ones)" if plan.show_all else "")]
    if plan.region_unknown:
        out.append(f"region unknown for {plan.region_unknown} ad(s): kept, flagged [{region_mod.FLAG}] (the place does not say where the work is)")
    out += [r.line(n) for n, r in enumerate(plan.rows, 1)]
    out += [f"  held back: {name}: {why}" for name, why in plan.held[:10]]
    out += [f"  disqualified: {p.name}: {why}" for p, _, why in plan.dropped[:10]]
    out += [f"  size: {note}" for note in plan.size_notes]
    return out + unknown_lines(plan) + region_lines(plan) + parked_line(plan) + alert_lines(plan)


def unread_lines(settings) -> list[str]:
    """What the shortlist did not read: `alerts not read since <time>: run discover --gmail-alerts` when the last recorded `discover` run did not read the alert
    emails (it was not asked for --gmail-alerts, or Gmail failed). [] when it did."""
    from . import discover

    runs = discover.read_runs(settings)
    read, last = discover.last_alert_read(runs)
    if read:
        return []
    if last is None:
        return ["alerts not read: no recorded discover run has read them yet: run discover --gmail-alerts"]
    try:
        when = f"{datetime.fromisoformat(last).astimezone():%Y-%m-%d %H:%M}"
    except ValueError:
        when = last
    return [f"alerts not read since {when}: run discover --gmail-alerts"]


def _alert_verb(toks: list[str], alerts: int) -> tuple[str, list[int]] | None:
    """('copy' | 'dismiss', [n]) for `c 2`, `c2`, `s c 2`, `s c2` (numbers 1..alerts); ('bad', []) when the number is out of range; None when the answer is not about the alerts."""
    skip = toks[0] == "s" and len(toks) > 1
    words = toks[1:] if skip else toks
    if not words or not (words[0] == "c" or (words[0][0] == "c" and words[0][1:].isdigit())):
        return None
    nums = ([words[0][1:]] if words[0] != "c" else []) + words[1:]
    if not nums or not all(t.isdigit() and 1 <= int(t) <= alerts for t in nums):
        return "bad", []
    return ("dismiss" if skip else "copy"), sorted({int(t) for t in nums})


def parse(answer: str, n: int, alerts: int = 0) -> tuple[str, list[int]]:
    """('all', []) | ('approve', [numbers]) | ('skip', [numbers]) | ('rest', []) | ('quit', []) | ('bad', []). Numbers are 1..n.
    With `alerts` > 0 also ('copy', [n]) for `c 2` / `c2` and ('dismiss', [n]) for `s c 2` / `s c2`, n in 1..alerts (the alert block's own numbers)."""
    toks = answer.replace(",", " ").lower().split()
    if not toks:
        return "bad", []
    if alerts and (got := _alert_verb(toks, alerts)) is not None:
        return got
    if toks[0] in ("q", "quit"):
        return "quit", []
    if toks[0] in ("a", "all"):
        return "all", []
    if toks == ["x"]:
        return "rest", []
    if toks == ["u"]:
        return "unknown", []
    if toks == ["i"]:
        return "info", []
    verb, nums = ("skip", toks[1:]) if toks[0] == "s" else ("approve", toks)
    if not nums or not all(t.isdigit() and 1 <= int(t) <= n for t in nums):
        return "bad", []
    return verb, sorted({int(t) for t in nums})


def run(settings, db: DB, fb: dict, *, ask: Callable[[str], str] | None, out: Callable[[str], None] = print, fetcher: Fetcher | None = None,
        now: datetime | None = None, dry_run: bool = False, archive: list[tuple[str, str]] | None = None, regions: Sequence[str] | None = None,
        size_mode: str | None = None, size_resolver: size_mod.Resolver | None = None,
        choose_size: Callable[[Counter], str] | None = None, show_all: bool = False, preface: Sequence[str] = ()) -> Plan:
    """Build the list, file the disqualified ads, print it, and (with `ask`) take your choices. `ask` None or `dry_run`: print only, nothing is moved.
    `preface`: lines printed first (what the shortlist did not read: `unread_lines`). `show_all`: see `build`; the list is then not counted as a showing."""
    now = now or datetime.now(timezone.utc)
    cfg = targeting_mod.config(settings)
    plan = build(settings, db, fb, fetcher=fetcher, now=now, archive=archive, regions=regions, size_mode=size_mode, size_resolver=size_resolver,
                 choose_size=choose_size, show_all=show_all)
    if not dry_run:
        apply_drops(settings, plan)
    out("\n".join([*preface, *lines(plan, cfg)]))
    interactive = ask is not None and not dry_run
    if not plan.new and not plan.show_all and not (plan.alert_rows and interactive):
        return plan
    if not interactive:
        if not dry_run:
            out("(no terminal: nothing was approved; run `careerops shortlist` in one)")
        return plan
    listed = bool(plan.new or plan.show_all)                                    # with nothing new the list was not printed: only the alert block is asked about
    rows = list(plan.rows) if listed else []
    numbered = {n: r for n, r in enumerate(rows, 1)}
    alerts = {n: a for n, a in enumerate(plan.alert_rows, 1)}                   # the block's own numbers (c1 ...): stable, a dismissed one is removed
    if listed and not plan.show_all:                                            # --all is a look, not a showing: it never counts towards parking
        log_events(settings, [_event(r, "shown", times_shown=r.shown + 1) for r in rows], now)
    extra = ((" · [u] include the size-unknown ads" if listed and plan.unknown_rows else "") + (" · [i] evidence" if any(r.size or r.region for r in rows) else "")
             + (" · [c n] how to apply on the board · [s c n] dismiss an alert" if alerts else ""))
    while numbered or alerts:
        try:
            n_repost = sum(1 for r in numbered.values() if r.repost)
            head = ("[a] approve all" + (f" (not the {n_repost} possible repost(s): type their numbers)" if n_repost else "")
                    + " · [1 3 5] approve these · [s n] skip job n for good · [x] skip every job left in this list") if numbered else "Nothing left to approve"
            answer = ask(head + extra + " · [q] quit > ")
        except (EOFError, KeyboardInterrupt):
            out("(cut short: nothing more was approved)")
            break
        verb, nums = parse(answer, len(rows), len(plan.alert_rows))
        if verb == "quit":
            break
        if verb == "bad":
            out("  not understood: type a, numbers such as 1 3 5, s and a number such as s 2, " + ("c and an alert number such as c1, " if alerts else "") + "or q")
            continue
        if verb == "copy":
            out("\n".join(plan.alert_rows[n - 1].steps(n) if n in alerts else f"  c{n}: already dismissed" for n in nums))
            continue
        if verb == "dismiss":
            gone = [alerts.pop(n) for n in nums if n in alerts]
            log_events(settings, [{"event": "parked", "key": a.key, "company": a.company, "title": a.title} for a in gone], now)
            out(f"  dismissed {len(gone)}: not listed again (`careerops unskip --shortlist` brings them back)" if gone else "  those are already dismissed")
            continue
        if verb == "info":
            out("\n".join(r.evidence(n) for n, r in numbered.items()))
            continue
        if verb == "unknown":
            room = cfg.shortlist_size - len(numbered)
            if not plan.unknown_rows:
                out("  there are no size-unknown ads to include")
                continue
            if room <= 0:
                out(f"  the list is full ({cfg.shortlist_size}): decide some of these first, then press u")
                continue
            added = []
            taken, plan.unknown_rows = plan.unknown_rows[:room], plan.unknown_rows[room:]
            for r in taken:
                if plan.alive is not None and (closed := plan.alive(r.job)):
                    if not dry_run:
                        file_away(settings, r.path, "closed", f"disqualified, liveness: {closed}")
                    continue
                n = len(rows) + 1
                rows.append(r)
                numbered[n] = r
                added.append((n, r))
            if not plan.show_all:
                log_events(settings, [_event(r, "shown", times_shown=r.shown + 1) for _, r in added], now)
            out("\n".join(r.line(n) for n, r in added) if added else "  none of them is still open")
            if plan.unknown_rows:
                out(f"  {len(plan.unknown_rows)} more size-unknown ad(s) did not fit in the list")
            continue
        picked = [numbered[n] for n in (list(numbered) if verb in ("all", "rest") else nums) if n in numbered]
        if verb == "all":
            # a possible repost of a job you skipped is never approved by [a]: it needs its own number (v2.5 fix)
            held = [(n, r) for n, r in numbered.items() if r.repost]
            picked = [r for r in picked if not r.repost]
            if held:
                out("  [a] did not approve these (you skipped the job before; type a number to approve one, s n to skip it again):")
                out("\n".join(f"    {n}. {r.name}: {r.repost}" for n, r in held))
        if not picked:
            out("  nothing to approve: only possible reposts are left" if verb == "all" and any(r.repost for r in numbered.values())
                else "  those are already decided")
            continue
        if verb in ("skip", "rest"):
            gone = []
            for r in picked:
                dst = file_away(settings, r.path, "skipped", SKIP_REASON)
                if dst is not None:
                    gone.append(r)
                    targets.record_file(settings, dst, decided.SKIPPED, SKIP_REASON)          # the decision log too (v2.5 fix: it was only a moved file)
            log_events(settings, [_event(r, "skipped") for r in gone], now)
            out(f"  skipped {len(gone)}: filed in jds_done/skipped/ as skipped {SKIP_MARK}, never offered again (`careerops unskip --shortlist` brings them back)")
        else:
            moved = approve(settings, picked, now.date())
            log_events(settings, [_event(r, "approved") for r in picked], now)
            out(f"  approved {len(moved)}: data/jds/approved/{now.date().isoformat()}/ (careerops live reads these)")
        for n in [n for n, r in numbered.items() if r in picked]:
            numbered.pop(n)
    tired = [] if plan.show_all else [r for r in numbered.values() if r.shown + 1 >= PARK_AFTER]          # shown for the PARK_AFTER-th time and still undecided
    if tired:
        log_events(settings, [_event(r, "parked") for r in tired], now)
        out(f"  parked {len(tired)}: shown {PARK_AFTER} times with no decision, not offered again ({'; '.join(r.name for r in tired)}); "
            "`careerops unskip --shortlist` brings them back")
    return plan


# ------------------------------------------------------------------ bring back
def skipped_here(settings) -> list[tuple[str, Path]]:
    """(company | role, file) of the ads you skipped at the shortlist: the files in jds_done/skipped/ whose `.reason.txt` carries SKIP_MARK."""
    out = []
    for p in decided.txt_files(decided.decision_dir(settings, decided.SKIPPED)):
        if p.name.endswith(".reason.txt"):
            continue
        try:
            why = p.with_name(p.stem + ".reason.txt").read_text(encoding="utf-8")
        except OSError:
            continue
        hdr = decided.header_of(p)
        if SKIP_MARK in why and hdr.get("company") and hdr.get("title"):
            out.append((f"{hdr['company']} | {hdr['title']}", p))
    return out


def parked_here(settings) -> dict[str, str]:
    """job key -> company | role of the ads parked now."""
    return dict(read_history(settings).parked)


def restore(settings, *, match: str | None = None, now: datetime | None = None) -> list[str]:
    """Bring back the ads you skipped at the shortlist (their files go back to the inbox) and the parked ones (they are offered again, their count
    starts from zero). `match` limits it to ads whose `company | role` contains that text (case-insensitive); None brings back all. Returns one line each."""
    now = now or datetime.now(timezone.utc)
    want = (lambda name: True) if match is None else (lambda name: match.lower() in name.lower())
    out, events = [], []
    for name, p in skipped_here(settings):
        if want(name):
            hdr = decided.header_of(p)
            dst = decided.move_file(settings, p, decided.inbox_dir(settings))
            decisions_mod.append(settings, decided.file_keys(dst), decisions_mod.UNSKIPPED, "careerops unskip --shortlist", decided.file_job_id(dst))
            events.append({"event": "reset", "key": _key(hdr["company"], hdr["title"]), "company": hdr["company"], "title": hdr["title"]})
            out.append(f"back in the inbox: {name} ({dst.name})")
    for key, name in parked_here(settings).items():
        if want(name):
            company, _, title = name.partition(" | ")
            events.append({"event": "reset", "key": key, "company": company, "title": title})
            out.append(f"offered again: {name} (was parked)")
    log_events(settings, events, now)
    return out
