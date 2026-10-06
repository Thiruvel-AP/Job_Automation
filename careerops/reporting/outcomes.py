"""Outcome log: what happened after each application (applied, acknowledged, screen, interview, offer, rejected,
withdrawn), the funnel computed from it, and a read-only backfill from the archive of submitted CVs.

The funnel is applied -> any reply -> screen -> interview -> offer. A later stage counts an earlier one (an offer implies an
interview happened even if nobody logged it). "No response" is computed when reporting (applied more than 28 days ago and
nothing logged since), never stored. The archive (paths.applications) is only ever LISTED here; `--apply` writes to the
database alone.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..db import DB, now

EVENTS = ("applied", "acknowledged", "screen", "interview", "offer", "rejected", "withdrawn")
REPLIES = frozenset({"acknowledged", "screen", "interview", "offer", "rejected"})   # withdrawn is the candidate's move
# stage -> the events that mean the job got at least that far
STAGES = (
    ("applied", frozenset(EVENTS)),
    ("any reply", REPLIES),
    ("screen", frozenset({"screen", "interview", "offer"})),
    ("interview", frozenset({"interview", "offer"})),
    ("offer", frozenset({"offer"})),
)
NO_RESPONSE_DAYS = 28
SPLITS = (("role family", "family"), ("tier", "tier"), ("cv pages", "cv_pages"), ("sponsor match", "sponsor_match"))


class OutcomeError(ValueError):
    pass


# ---------------------------------------------------------------- writing
def record(db: DB, job_id: str, event: str, on_date: date | str | None = None, note: str = "", source: str = "manual",
           commit: bool = True) -> bool:
    """Log one event for a job. False if the same (job, event, date) is already logged. `commit=False` joins the caller's
    transaction (mark-applied writes 'applied' together with the rest of its changes)."""
    if event not in EVENTS:
        raise OutcomeError(f"unknown event '{event}' (one of: {', '.join(EVENTS)})")
    day = (on_date.isoformat() if isinstance(on_date, date) else on_date) or now()[:10]
    date.fromisoformat(day)                                          # a malformed date is an error, not a stored string
    if db.conn.execute("SELECT 1 FROM outcomes WHERE job_id=? AND event=? AND on_date=?", (job_id, event, day)).fetchone():
        return False
    db.conn.execute("INSERT INTO outcomes (job_id, event, on_date, note, source, created_at) VALUES (?,?,?,?,?,?)",
                    (job_id, event, day, note or None, source, now()))
    if commit:
        db.conn.commit()
    return True


def record_applied(db: DB, job_id: str, on_date: date | str | None, source: str = "mark-applied") -> bool:
    """The 'applied' event, once per job (a resubmission of the same job does not log a second one). No commit."""
    if db.conn.execute("SELECT 1 FROM outcomes WHERE job_id=? AND event='applied'", (job_id,)).fetchone():
        return False
    return record(db, job_id, "applied", on_date, source=source, commit=False)


# ---------------------------------------------------------------- statistics
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials; None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Applied:
    job_id: str
    company: str
    title: str
    applied_on: date
    events: dict[str, date]          # event -> its latest date
    family: str
    tier: str
    cv_pages: str
    sponsor_match: str
    approximate: bool

    def reached(self, stage: str) -> bool:
        wanted = dict(STAGES)[stage]
        return any(e in wanted for e in self.events)

    def status(self, today: date) -> str:
        """offer / interview / screen / replied / rejected / withdrawn, else no_response (> 28 days) or pending."""
        for e in ("offer", "interview", "screen", "rejected", "withdrawn", "acknowledged"):
            if e in self.events:
                return "replied" if e == "acknowledged" else e
        return "no_response" if (today - self.applied_on).days > NO_RESPONSE_DAYS else "pending"


def _label(v, prefix: str = "") -> str:
    return "unknown" if v is None or v == "" else f"{prefix}{v}"


def applied_jobs(db: DB) -> list[Applied]:
    """Every job with an 'applied' event, with the facts the report splits on. Jobs the pipeline never tailored have no
    family / tier / cv_pages / sponsor_match: they are 'unknown' in those splits."""
    events: dict[str, dict[str, date]] = defaultdict(dict)
    notes: dict[str, bool] = defaultdict(bool)
    for r in db.conn.execute("SELECT job_id, event, on_date, source FROM outcomes ORDER BY on_date, rowid"):
        d = date.fromisoformat(r["on_date"])
        ev = events[r["job_id"]]
        ev[r["event"]] = min(ev[r["event"]], d) if r["event"] == "applied" and r["event"] in ev else d   # first application
        if r["event"] == "applied" and r["source"] == "archive":
            notes[r["job_id"]] = True
    out = []
    for jid, ev in events.items():
        if "applied" not in ev:
            continue
        j = db.job(jid)
        if j is None:
            continue
        match = (j["sponsor_match"] or "").split("(")[0]
        out.append(Applied(jid, j["company"], j["title"], ev["applied"], ev,
                           _label(j["family"]), _label(j["tier"], "T"), _label(j["cv_pages"]), _label(match), notes[jid]))
    return sorted(out, key=lambda a: (a.applied_on, a.job_id))


def funnel(rows: list[Applied]) -> list[dict]:
    """[{"stage", "n", "of", "rate", "lo", "hi"}] for applied, any reply, screen, interview, offer."""
    total = len(rows)
    out = []
    for stage, _ in STAGES:
        k = sum(r.reached(stage) for r in rows)
        ci = wilson(k, total)
        out.append({"stage": stage, "n": k, "of": total, "rate": k / total if total else None,
                    "lo": ci[0] if ci else None, "hi": ci[1] if ci else None})
    return out


def build_report(db: DB, today: date | None = None) -> dict:
    """{"applied", "pending", "no_response", "approximate", "overall": funnel, "splits": {name: {value: funnel}},
    "orphans": jobs with events but no 'applied' event}."""
    today = today or datetime.now(timezone.utc).date()
    rows = applied_jobs(db)
    status = [r.status(today) for r in rows]
    logged = {r[0] for r in db.conn.execute("SELECT DISTINCT job_id FROM outcomes")}
    splits = {}
    for name, attr in SPLITS:
        groups: dict[str, list[Applied]] = defaultdict(list)
        for r in rows:
            groups[getattr(r, attr)].append(r)
        splits[name] = {k: funnel(v) for k, v in sorted(groups.items(), key=lambda kv: (kv[0] == "unknown", kv[0]))}
    return {"applied": len(rows), "pending": status.count("pending"), "no_response": status.count("no_response"),
            "approximate": sum(r.approximate for r in rows), "overall": funnel(rows), "splits": splits,
            "orphans": sorted(logged - {r.job_id for r in rows}), "today": today.isoformat()}


def _pct(x: float | None) -> str:
    return "  - " if x is None else f"{x:.0%}"


def _cell(f: dict) -> str:
    if not f["of"]:
        return "-"
    return f"{f['n']}/{f['of']} {_pct(f['rate'])} [{_pct(f['lo'])}-{_pct(f['hi'])}]"


def format_report(rep: dict) -> str:
    lines = [f"Funnel as of {rep['today']}: {rep['applied']} applied job(s); {rep['pending']} applied <= {NO_RESPONSE_DAYS} "
             f"days ago and waiting, {rep['no_response']} with no response after {NO_RESPONSE_DAYS} days."]
    if rep["approximate"]:
        lines.append(f"{rep['approximate']} applied date(s) are approximate (archive folder dates).")
    if rep["orphans"]:
        lines.append(f"{len(rep['orphans'])} job(s) have outcomes but no 'applied' event and are left out: "
                     + ", ".join(rep["orphans"][:5]))
    if not rep["applied"]:
        return "\n".join(lines + ["Nothing to report yet: log applications with `careerops mark-applied` or "
                                  "`careerops outcome --event applied`."])
    lines += ["", f"{'stage':<11}{'n':>4}  {'of applied':<11}95% Wilson interval"]
    for f in rep["overall"]:
        lines.append(f"{f['stage']:<11}{f['n']:>4}  {_pct(f['rate']):<11}[{_pct(f['lo'])} - {_pct(f['hi'])}]")
    for name, groups in rep["splits"].items():
        lines += ["", f"By {name}  (k/applied rate [95% Wilson])"]
        head = ["any reply", "screen", "interview", "offer"]
        lines.append(f"  {'':<14}{'applied':>7}  " + "".join(f"{h:<26}" for h in head))
        for value, fun in groups.items():
            lines.append(f"  {value:<14}{fun[0]['n']:>7}  " + "".join(f"{_cell(f):<26}" for f in fun[1:]))
    return "\n".join(lines)


# ---------------------------------------------------------------- backfill from the archive (read-only)
@dataclass
class Proposal:
    company: str
    role: str
    folder: str                      # Company/Role, relative to the archive root
    applied_on: date                 # the role folder's modification date: approximate
    job_id: str


def archive_job_id(company: str, role: str) -> str:
    return "archive:" + hashlib.sha1(f"{company}|{role}".encode("utf-8")).hexdigest()[:12]


def propose_from_archive(db: DB, root: Path) -> tuple[list[Proposal], list[str]]:
    """(proposals, skipped): <root>/<Company>/<Role> folders (LISTED and stat'ed, nothing else; their contents are never
    opened) that no job is recorded for. A folder the pipeline archived itself (archive_index) or that an earlier import
    already turned into a job is skipped, so the import can be run again."""
    if not root.is_dir():
        raise OutcomeError(f"archive folder not found: {root}")
    known = {r[0] for r in db.conn.execute("SELECT folder FROM archive_index WHERE kind='role'")}
    known |= {rel for (d,) in db.conn.execute("SELECT application_dir FROM jobs WHERE application_dir IS NOT NULL")
              if (rel := _company_role(Path(d), root))}
    props, skipped = [], []
    for company in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        for role in sorted((p for p in company.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
            rel = f"{company.name}/{role.name}"
            jid = archive_job_id(company.name, role.name)
            if rel in known or db.job(jid) is not None:
                skipped.append(rel)
                continue
            day = datetime.fromtimestamp(role.stat().st_mtime, tz=timezone.utc).date()
            props.append(Proposal(company.name, role.name, rel, day, jid))
    return props, skipped


def _company_role(p: Path, root: Path) -> str | None:
    """'Company/Role' for a path inside the archive root (a vN folder counts as its role), else None."""
    try:
        parts = p.relative_to(root).parts
    except ValueError:
        return None
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def apply_proposals(db: DB, props: list[Proposal]) -> int:
    """Write the proposals to the database (never to the archive): a manual job per folder, status 'applied', and an
    'applied' event dated by the folder's mtime, source 'archive', noted as approximate. One transaction."""
    ts = now()
    with db.conn:
        for p in props:
            db.conn.execute(
                "INSERT OR IGNORE INTO jobs (id, source, company, title, status, flags, first_seen, last_seen, applied_at, "
                "updated_at, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p.job_id, "archive", p.company, p.role, "applied", json.dumps(["Imported from the archive: applied date is "
                                                                                  "approximate (folder date)"]),
                 ts, ts, f"{p.applied_on.isoformat()}T00:00:00+00:00", ts, "imported from the archive"))
            record(db, p.job_id, "applied", p.applied_on, "approximate: archive folder modification date", "archive",
                   commit=False)
    return len(props)
