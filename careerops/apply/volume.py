"""Volume caps: how many applications, and how many at one company (`targeting:` in settings.yaml). Read-only: it counts, it never writes.

  * Daily cap       `daily_max_applications` counts jobs submitted today, by you or by auto-apply (a job marked applied with today's date). `live` checks it
                    before it tailors a job and again right before a form is opened, and stops with the count.
  * Per company     checked again right before a form is opened (and when `careerops shortlist` builds its list):
        open          an application with no reply yet (no acknowledged / screen / interview / offer / rejected / withdrawn event), made in the last
                      outcomes.NO_RESPONSE_DAYS days (after that the funnel counts it as no response). `per_company_max_open` of them at one company, over all its
                      roles, and no further application goes there.
        cooldown      after a rejection or a withdrawal at a company, no new application there for `per_company_cooldown_days` days from that day.
    Earlier applications count from the database (what the tracker Sheet mirrors) AND from the archive folders: a role folder in `paths.applications` or
    `paths.applications_auto` that no database job accounts for counts as open for the days after its folder was made. Companies are matched as the archive
    matches them (naming.folder_key), so "Acme Ltd" and "Acme" are one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import targeting as targeting_mod
from ..db import DB
from ..reporting.outcomes import NO_RESPONSE_DAYS, REPLIES
from .naming import folder_key

CLOSED_EVENTS = ("rejected", "withdrawn")
ANSWERED = tuple(sorted(REPLIES | {"withdrawn"}))


def today() -> date:
    """The calendar day applications are counted by: the date stored in applied_at (UTC, as `mark-applied` writes it)."""
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class Application:
    company: str
    on: date
    source: str                       # database | archive
    job_id: str = ""
    answered: bool = False            # a reply, or a withdrawal
    closed_on: date | None = None     # the day of a rejection or withdrawal


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def applications(db: DB, settings) -> list[Application]:
    """Every application on record: the database's applied jobs (with their outcomes) and the archive's role folders no database job accounts for."""
    out: list[Application] = []
    seen_rel: set[str] = set()
    events: dict[str, list[tuple[str, date]]] = {}
    for r in db.conn.execute("SELECT job_id, event, on_date FROM outcomes WHERE event != 'applied'"):
        if (d := _day(r["on_date"])) is not None:
            events.setdefault(r["job_id"], []).append((r["event"], d))
    for j in db.jobs():
        if not j["applied_at"] and j["status"] != "applied":
            continue
        on = _day(j["applied_at"]) or _day(j["updated_at"] if "updated_at" in j.keys() else None)
        if on is None:
            continue
        ev = events.get(j["id"], [])
        closed = max((d for e, d in ev if e in CLOSED_EVENTS), default=None)
        out.append(Application(j["company"], on, "database", j["id"], any(e in ANSWERED for e, _ in ev), closed))
        if j["application_dir"]:
            seen_rel.add(str(Path(j["application_dir"]).resolve()))
    for root in settings.archive_roots():
        try:
            companies = [c for c in os.scandir(root) if c.is_dir()]
        except OSError:
            continue
        for c in companies:
            try:
                roles = [r for r in os.scandir(c.path) if r.is_dir()]
            except OSError:
                continue
            for r in roles:
                here = str(Path(r.path).resolve())
                if any(here == s or here.startswith(s + os.sep) or s.startswith(here + os.sep) for s in seen_rel):
                    continue                              # a role folder (or its v2 ...) that a database job accounts for
                when = _day(datetime.fromtimestamp(r.stat().st_mtime, timezone.utc).isoformat())
                if when is not None:
                    out.append(Application(c.name, when, "archive"))
    return out


def applied_today(db: DB, day: date | None = None) -> int:
    """Jobs submitted on `day` (default today), by anyone: the applied date of every applied job."""
    iso = (day or today()).isoformat()
    return db.conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL AND substr(applied_at, 1, 10)=?", (iso,)).fetchone()[0]


def daily_block(db: DB, settings, day: date | None = None) -> str | None:
    """Why no further application may be made today, or None. Names the count."""
    cap = targeting_mod.config(settings).daily_max_applications
    n = applied_today(db, day)
    if n >= cap:
        return f"daily cap reached: {n} of {cap} applications submitted today (targeting.daily_max_applications); live stops here"
    return None


def open_count(db: DB, settings, company: str, day: date | None = None, *, apps: list[Application] | None = None) -> int:
    """How many open applications (no reply yet, made in the last NO_RESPONSE_DAYS days) there are at `company` (the shortlist adds its own rows to it, v3.1)."""
    key, now = folder_key(company or ""), day or today()
    if not key:
        return 0
    window = timedelta(days=NO_RESPONSE_DAYS)
    return sum(1 for a in (apps if apps is not None else applications(db, settings))
               if folder_key(a.company) == key and not a.answered and a.closed_on is None and now - a.on <= window)


def company_block(db: DB, settings, company: str, day: date | None = None, *, apps: list[Application] | None = None) -> str | None:
    """Why no application to `company` may be started now, or None."""
    cfg = targeting_mod.config(settings)
    key, now = folder_key(company or ""), day or today()
    if not key:
        return None
    mine = [a for a in (apps if apps is not None else applications(db, settings)) if folder_key(a.company) == key]
    window = timedelta(days=NO_RESPONSE_DAYS)
    open_ = [a for a in mine if not a.answered and a.closed_on is None and now - a.on <= window]
    if len(open_) >= cfg.per_company_max_open:
        return (f"{company}: {len(open_)} open application(s) with no reply yet "
                f"(targeting.per_company_max_open is {cfg.per_company_max_open}; an application counts as open for {NO_RESPONSE_DAYS} days)")
    closed = [a.closed_on for a in mine if a.closed_on is not None]
    if closed and cfg.per_company_cooldown_days:
        latest = max(closed)
        until = latest + timedelta(days=cfg.per_company_cooldown_days)
        if now < until:
            return f"{company}: cooling down after the application closed on {latest.isoformat()} (targeting.per_company_cooldown_days is {cfg.per_company_cooldown_days}; until {until.isoformat()})"
    return None
