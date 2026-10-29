"""`careerops shortlist`: the human promotes. The inbox is scored (discovery/triage.py), the hard stops are applied, and the best few are OFFERED; only
the ones you approve go to data/jds/approved/<date>/, and `careerops live` reads nothing else.

    1. every inbox ad gets the triage score T (zero tokens) and its row in the database (triage_scores);
    2. the hard disqualifiers (blacklist, the same role under another channel, the level exclude list, your dealbreakers) file the ad in jds_done/skipped/
       with the reason beside it; it is never offered. A closed posting goes to jds_done/closed/ (liveness is asked only for the ads that would otherwise
       be offered, best T first, because it costs a request each);
    3. offered = T >= targeting.min_triage_score, best T first then freshest, at most targeting.shortlist_size; an ad older than targeting.max_posting_age_days
       needs T >= 4.5; a company at targeting.per_company_max_open open applications, or inside its cooldown, is left out (apply/volume.py);
    4. [a] approves all · `1 3 5` approves those · `s 2` skips job 2 for good (filed in jds_done/skipped/, so discover does not bring it back) ·
   [x] skips every job in the list that you did not approve · [q] quits.
Nothing is tailored here, and nothing is sent anywhere.

Repeats: an ad already approved (waiting in data/jds/approved/), skipped, applied to, closed, or parked is never offered again. Every list you were asked
about is logged in data/jds/shortlist_log.jsonl (times_shown per job); an ad shown in PARK_AFTER lists with no decision is parked: it stays in the inbox but is
only counted ("parked: N"). `careerops unskip --shortlist` brings skipped and parked ads back. With nothing new since the last list, no list is printed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .. import targeting as targeting_mod
from ..apply import decided, volume
from ..db import DB
from . import applykind, liveness, triage
from . import register as sponsors
from .http import Fetcher

APPROVED = "approved"
HAVE_SHOWN, LACK_SHOWN = 3, 2
LOG = "shortlist_log.jsonl"
PARK_AFTER = 3                                  # lists an ad may be shown in without a decision before it is parked
SKIP_MARK = "(user, shortlist)"
SKIP_REASON = f"skipped {SKIP_MARK}: skipped at the shortlist, not offered again"


def approved_root(settings) -> Path:
    return settings.path("jds") / APPROVED


def approved_files(settings) -> list[Path]:
    """The JD files you approved: every .txt in data/jds/approved/<date>/. The only files `careerops live` works on."""
    root = approved_root(settings)
    try:
        return sorted(p for d in root.iterdir() if d.is_dir() for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".txt")
    except OSError:
        return []


@dataclass
class Row:
    path: Path
    job: dict
    t: triage.Triage
    kind: str
    shown: int = 0                              # lists this job was in before this one (shortlist_log.jsonl)

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
                f"lack: {', '.join(self.t.lack[:LACK_SHOWN]) or '-'} | {self.kind}{seen}")


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


def save_score(db: DB, job: dict, t: triage.Triage, on: date) -> None:
    with db.conn:
        db.conn.execute("INSERT OR REPLACE INTO triage_scores (key, company, title, t, factors, scored_on) VALUES (?,?,?,?,?,?)",
                        (_key(job["company"], job["title"]), job["company"], job["title"], round(t.t, 3), json.dumps(t.factors()), on.isoformat()))


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
          archive: list[tuple[str, str]] | None = None, files: list[Path] | None = None) -> Plan:
    """Score the inbox and decide what is offered. Writes the scores to the database; moves nothing (see `file_away`)."""
    now = now or datetime.now(timezone.utc)
    cfg = targeting_mod.config(settings)
    plan = Plan()
    hist = read_history(settings)
    plan.last_shown = hist.last
    known = decided.load(settings, db)                                       # skipped and applied: database rows and the files in jds_done/
    for p in decided.txt_files(decided.decision_dir(settings, "closed")):
        if (k := decided.file_key(p)):
            known.add(k, "closed")
    waiting = {k for p in approved_files(settings) if (k := decided.file_key(p))}
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
        jkey = decided.key_of(job["company"], job["title"])
        if decided.usable(jkey) and jkey in waiting:
            plan.approved += 1
            continue
        if known.hit(job["company"], job["title"], job.get("url") or job.get("apply_url")):
            plan.decided += 1
            continue
        if _key(job["company"], job["title"]) in hist.parked:
            plan.parked.append(f"{job['company']} | {job['title']}")
            continue
        plan.scored += 1
        t = triage.score(job, settings, fb, floor=floor, now=now)
        save_score(db, job, t, day)
        if (d := triage.disqualify(job, settings, db, archive=archive, others=others)) is not None:
            plan.dropped.append((p, "skipped", f"disqualified, {d.line()}"))
            continue
        if t.t < cfg.min_triage_score:
            plan.below_min += 1
            continue
        if t.age_days is not None and t.age_days > cfg.max_posting_age_days and t.t < targeting_mod.OLD_POSTING_MIN_T:
            plan.too_old += 1
            continue
        pool.append(Row(p, job, t, kind_of(job)))
    pool.sort(key=lambda r: (-r.t.t, r.t.age_days if r.t.age_days is not None else float("inf"), r.path.name))
    alive = make_alive(fetcher, cache if cache is not None else (liveness.cache_for(settings) if fetcher is not None else None), now)
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
    return plan


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


def lines(plan: Plan, cfg: targeting_mod.Targeting) -> list[str]:
    if not plan.new:                                                         # nothing you have not been shown: no list
        since = f"since {plan.last_shown.astimezone():%Y-%m-%d %H:%M}" if plan.last_shown else "yet"
        return [f"No new jobs {since}. Run careerops discover --ats --gmail-alerts."] + parked_line(plan)
    out = [f"Shortlist: {plan.scored} ad(s) scored; {len(plan.rows)} offered (at most {cfg.shortlist_size}, T >= {cfg.min_triage_score:g}). "
           f"Left out: {len(plan.dropped)} disqualified, {plan.below_min} under T {cfg.min_triage_score:g}, "
           f"{plan.too_old} older than {cfg.max_posting_age_days} d without T >= {targeting_mod.OLD_POSTING_MIN_T:g}, {len(plan.held)} at a company limit"
           + (f", {plan.approved} already approved" if plan.approved else "") + (f", {plan.decided} already decided" if plan.decided else "")
           + (f", {plan.unreadable} unreadable" if plan.unreadable else "") + ".",
           f"new since last shortlist: {plan.new}"]
    out += [r.line(n) for n, r in enumerate(plan.rows, 1)]
    out += [f"  held back: {name}: {why}" for name, why in plan.held[:10]]
    out += [f"  disqualified: {p.name}: {why}" for p, _, why in plan.dropped[:10]]
    return out + parked_line(plan)


def parse(answer: str, n: int) -> tuple[str, list[int]]:
    """('all', []) | ('approve', [numbers]) | ('skip', [numbers]) | ('rest', []) | ('quit', []) | ('bad', []). Numbers are 1..n."""
    toks = answer.replace(",", " ").lower().split()
    if not toks:
        return "bad", []
    if toks[0] in ("q", "quit"):
        return "quit", []
    if toks[0] in ("a", "all"):
        return "all", []
    if toks == ["x"]:
        return "rest", []
    verb, nums = ("skip", toks[1:]) if toks[0] == "s" else ("approve", toks)
    if not nums or not all(t.isdigit() and 1 <= int(t) <= n for t in nums):
        return "bad", []
    return verb, sorted({int(t) for t in nums})


def run(settings, db: DB, fb: dict, *, ask: Callable[[str], str] | None, out: Callable[[str], None] = print, fetcher: Fetcher | None = None,
        now: datetime | None = None, dry_run: bool = False, archive: list[tuple[str, str]] | None = None) -> Plan:
    """Build the list, file the disqualified ads, print it, and (with `ask`) take your choices. `ask` None or `dry_run`: print only, nothing is moved."""
    now = now or datetime.now(timezone.utc)
    cfg = targeting_mod.config(settings)
    plan = build(settings, db, fb, fetcher=fetcher, now=now, archive=archive)
    if not dry_run:
        apply_drops(settings, plan)
    out("\n".join(lines(plan, cfg)))
    if not plan.new:
        return plan
    if ask is None or dry_run:
        if not dry_run:
            out("(no terminal: nothing was approved; run `careerops shortlist` in one)")
        return plan
    rows = list(plan.rows)
    numbered = {n: r for n, r in enumerate(rows, 1)}
    log_events(settings, [_event(r, "shown", times_shown=r.shown + 1) for r in rows], now)
    while numbered:
        try:
            answer = ask("[a] approve all · [1 3 5] approve these · [s n] skip job n for good · [x] skip every job left in this list · [q] quit > ")
        except (EOFError, KeyboardInterrupt):
            out("(cut short: nothing more was approved)")
            break
        verb, nums = parse(answer, len(rows))
        if verb == "quit":
            break
        if verb == "bad":
            out("  not understood: type a, numbers such as 1 3 5, s and a number such as s 2, or q")
            continue
        picked = [numbered[n] for n in (list(numbered) if verb in ("all", "rest") else nums) if n in numbered]
        if not picked:
            out("  those are already decided")
            continue
        if verb in ("skip", "rest"):
            gone = [r for r in picked if file_away(settings, r.path, "skipped", SKIP_REASON) is not None]
            log_events(settings, [_event(r, "skipped") for r in gone], now)
            out(f"  skipped {len(gone)}: filed in jds_done/skipped/ as skipped {SKIP_MARK}, never offered again (`careerops unskip --shortlist` brings them back)")
        else:
            moved = approve(settings, picked, now.date())
            log_events(settings, [_event(r, "approved") for r in picked], now)
            out(f"  approved {len(moved)}: data/jds/approved/{now.date().isoformat()}/ (careerops live reads these)")
        for n in [n for n, r in numbered.items() if r in picked]:
            numbered.pop(n)
    tired = [r for r in numbered.values() if r.shown + 1 >= PARK_AFTER]          # shown for the PARK_AFTER-th time and still undecided
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
            events.append({"event": "reset", "key": _key(hdr["company"], hdr["title"]), "company": hdr["company"], "title": hdr["title"]})
            out.append(f"back in the inbox: {name} ({dst.name})")
    for key, name in parked_here(settings).items():
        if want(name):
            company, _, title = name.partition(" | ")
            events.append({"event": "reset", "key": key, "company": company, "title": title})
            out.append(f"offered again: {name} (was parked)")
    log_events(settings, events, now)
    return out
