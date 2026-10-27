"""`careerops followups` (Phase D2): the applications whose follow-up day has come and that nobody has answered, oldest first.

  * Due = the day you applied plus outreach.follow_up_days (7), the same day the tracker Sheet's "Follow-up due" shows once a draft exists; a job
    without a draft is listed on the same day. An application is "unanswered" when the outcome log holds no acknowledged / screen / interview / offer /
    rejected / withdrawn event for it (`careerops outcome`, or a `y` in the reply watch). A job backfilled from the archive (approximate date) is skipped.
  * The contact is the best stored one (contacts.py: yours, Hunter, the ad, the company site).
  * This module only reads. A follow-up message is drafted by outreach.py under its own rules (`careerops outreach draft JOB`); nothing is sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .. import contacts as contacts_mod
from ..db import DB
from ..outreach import DEFAULT_FOLLOW_UP_DAYS
from .outcomes import EVENTS


@dataclass(frozen=True)
class Due:
    job_id: str
    company: str
    title: str
    applied_on: date
    due: date
    days_over: int
    contact: str               # "" when none is stored


def follow_up_days(settings) -> int:
    cfg = (getattr(settings, "raw", None) or {}).get("outreach") or {}
    try:
        return int(cfg.get("follow_up_days") or DEFAULT_FOLLOW_UP_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_FOLLOW_UP_DAYS


def contact_text(db: DB, job_id: str) -> str:
    """`Name, Role <address>` of the best stored contact, `Name, Role (no address yet)` when it has none, "" for a job without a contact."""
    people = contacts_mod.stored(db, job_id)
    if not people:
        return ""
    p = people[0]
    who = ", ".join(x for x in (str(p.get("name") or "").strip(), str(p.get("role") or "").strip()) if x) or "(no name)"
    return f"{who} <{p['email']}>" if p.get("email") else f"{who} (no address yet)"


def due(db: DB, settings, today: date) -> list[Due]:
    """Unanswered applications on or past their follow-up day, the one due longest ago first."""
    answered = {r[0] for r in db.conn.execute(f"SELECT job_id FROM outcomes WHERE event IN ({','.join('?' * (len(EVENTS) - 1))})",
                                                [e for e in EVENTS if e != "applied"])}
    approximate = {r[0] for r in db.conn.execute("SELECT job_id FROM outcomes WHERE event='applied' AND source='archive'")}
    days = follow_up_days(settings)
    out: list[Due] = []
    for j in db.jobs("applied"):
        if not j["applied_at"] or j["id"] in answered or j["id"] in approximate:
            continue
        try:
            sent = date.fromisoformat(str(j["applied_at"])[:10])
        except ValueError:
            continue
        when = sent + timedelta(days=days)
        if when <= today:
            out.append(Due(j["id"], j["company"], j["title"], sent, when, (today - when).days, contact_text(db, j["id"])))
    return sorted(out, key=lambda r: (r.due, r.applied_on, r.job_id))


def lines(rows: list[Due], today: date) -> list[str]:
    if not rows:
        return ["No application is waiting for a follow-up."]
    out = [f"{len(rows)} application(s) waiting for a follow-up on {today.isoformat()}, the longest overdue first:"]
    for r in rows:
        late = "due today" if r.days_over == 0 else f"{r.days_over} day{'s' if r.days_over != 1 else ''} over"
        out.append(f"  {r.company} | {r.title} | applied {r.applied_on.isoformat()} | due {r.due.isoformat()} ({late}) | {r.job_id}")
        out.append(f"      contact: {r.contact or 'no contact known (careerops contact add ...)'}")
    out.append("To draft one (a Gmail draft you send yourself; nothing is sent): careerops outreach draft " + rows[0].job_id)
    return out
