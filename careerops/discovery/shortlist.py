"""`careerops shortlist`: the human promotes. The inbox is scored (discovery/triage.py), the hard stops are applied, and the best few are OFFERED; only
the ones you approve go to data/jds/approved/<date>/, and `careerops live` reads nothing else.

    1. every inbox ad gets the triage score T (zero tokens) and its row in the database (triage_scores);
    2. the hard disqualifiers (blacklist, the same role under another channel, the level exclude list, your dealbreakers) file the ad in jds_done/skipped/
       with the reason beside it; it is never offered. A closed posting goes to jds_done/closed/ (liveness is asked only for the ads that would otherwise
       be offered, best T first, because it costs a request each);
    3. offered = T >= targeting.min_triage_score, best T first then freshest, at most targeting.shortlist_size; an ad older than targeting.max_posting_age_days
       needs T >= 4.5; a company at targeting.per_company_max_open open applications, or inside its cooldown, is left out (apply/volume.py);
    4. [a] approves all · `1 3 5` approves those · `s 2` skips job 2 for good (filed in jds_done/skipped/, so discover does not bring it back) · [q] quits.
Nothing is tailored here, and nothing is sent anywhere.
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

    @property
    def name(self) -> str:
        return f"{self.job['company']} | {self.job['title']}"

    def line(self, n: int) -> str:
        age = "date unknown" if self.t.age_days is None else f"{self.t.age_days:.0f} d"
        return (f"{n:>2}. {self.name} | {age} | {self.t.text()} | have: {', '.join(self.t.have[:HAVE_SHOWN]) or '-'} | "
                f"lack: {', '.join(self.t.lack[:LACK_SHOWN]) or '-'} | {self.kind}")


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    dropped: list[tuple[Path, str, str]] = field(default_factory=list)       # (file, folder in jds_done, reason line)
    held: list[tuple[str, str]] = field(default_factory=list)                # (company | role, why not offered)
    scored: int = 0
    below_min: int = 0
    too_old: int = 0
    unreadable: int = 0


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
        plan.rows.append(r)
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


def lines(plan: Plan, cfg: targeting_mod.Targeting) -> list[str]:
    out = [f"Shortlist: {plan.scored} ad(s) scored; {len(plan.rows)} offered (at most {cfg.shortlist_size}, T >= {cfg.min_triage_score:g}). "
           f"Left out: {len(plan.dropped)} disqualified, {plan.below_min} under T {cfg.min_triage_score:g}, "
           f"{plan.too_old} older than {cfg.max_posting_age_days} d without T >= {targeting_mod.OLD_POSTING_MIN_T:g}, {len(plan.held)} at a company limit"
           + (f", {plan.unreadable} unreadable" if plan.unreadable else "") + "."]
    out += [r.line(n) for n, r in enumerate(plan.rows, 1)]
    out += [f"  held back: {name}: {why}" for name, why in plan.held[:10]]
    out += [f"  disqualified: {p.name}: {why}" for p, _, why in plan.dropped[:10]]
    return out


def parse(answer: str, n: int) -> tuple[str, list[int]]:
    """('all', []) | ('approve', [numbers]) | ('skip', [numbers]) | ('quit', []) | ('bad', []). Numbers are 1..n."""
    toks = answer.replace(",", " ").lower().split()
    if not toks:
        return "bad", []
    if toks[0] in ("q", "quit"):
        return "quit", []
    if toks[0] in ("a", "all"):
        return "all", []
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
    if ask is None or dry_run or not plan.rows:
        if plan.rows and not dry_run:
            out("(no terminal: nothing was approved; run `careerops shortlist` in one)")
        return plan
    rows = list(plan.rows)
    numbered = {n: r for n, r in enumerate(rows, 1)}
    while numbered:
        try:
            answer = ask("[a] approve all · [1 3 5] approve these · [s n] skip job n for good · [q] quit > ")
        except (EOFError, KeyboardInterrupt):
            out("(cut short: nothing more was approved)")
            break
        verb, nums = parse(answer, len(rows))
        if verb == "quit":
            break
        if verb == "bad":
            out("  not understood: type a, numbers such as 1 3 5, s and a number such as s 2, or q")
            continue
        picked = [numbered[n] for n in (list(numbered) if verb == "all" else nums) if n in numbered]
        if not picked:
            out("  those are already decided")
            continue
        if verb == "skip":
            for r in picked:
                file_away(settings, r.path, "skipped", "skipped at the shortlist (you chose not to apply)")
            out(f"  skipped {len(picked)}: filed in jds_done/skipped/, never offered again")
        else:
            moved = approve(settings, picked, now.date())
            out(f"  approved {len(moved)}: data/jds/approved/{now.date().isoformat()}/ (careerops live reads these)")
        for n in [n for n, r in numbered.items() if r in picked]:
            numbered.pop(n)
    return plan
