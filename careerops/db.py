"""SQLite state store. One row per job posting; `status` is the pipeline state machine.

new -> skipped | triaged -> needs_review -> approved | rejected -> applied
triaged -> letter_pending -> needs_review   (CV built; the letter waits for the one provider allowed to write it)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sponsors (
    norm TEXT NOT NULL,
    name TEXT NOT NULL,
    town TEXT, county TEXT, rating TEXT, route TEXT
);
CREATE INDEX IF NOT EXISTS ix_sponsors_norm ON sponsors(norm);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,              -- source:external_id
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    apply_url TEXT,
    description TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT,
    flags TEXT DEFAULT '[]',          -- JSON list of review warnings
    sponsor_name TEXT,
    sponsor_score REAL,
    keywords TEXT,                    -- JSON [{term, weight, required}]
    ceiling REAL,
    score REAL,
    score_stemmed REAL,
    letter_score REAL,
    attempts INTEGER DEFAULT 0,
    out_dir TEXT,
    applied_at TEXT,
    confirmation TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS events (
    job_id TEXT, ts TEXT, stage TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    provider TEXT, day TEXT, calls INTEGER, PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS llm_task_usage (       -- tokens the router was charged today, per task and model (llm/usage.py)
    day TEXT, task TEXT, model TEXT, calls INTEGER, tokens INTEGER, PRIMARY KEY (day, task, model)
);
CREATE TABLE IF NOT EXISTS register_snapshots (
    day TEXT PRIMARY KEY, csv_url TEXT, path TEXT, rows INTEGER
);

CREATE TABLE IF NOT EXISTS companies (
    norm TEXT NOT NULL, town_norm TEXT NOT NULL,
    register_name TEXT, town TEXT, county TEXT, rating TEXT, route TEXT,
    first_seen TEXT, last_seen TEXT, removed_on TEXT,
    ch_number TEXT, ch_name TEXT, ch_status TEXT, sic_codes TEXT, sector TEXT,
    ch_match TEXT,                    -- exact | town | ambiguous | none
    public_sector INTEGER DEFAULT 0,
    ats TEXT, ats_slug TEXT, ats_verified INTEGER, careers_url TEXT,
    probe_status TEXT,                -- verified | unverified | not_found | error | manual
    last_probed TEXT,
    PRIMARY KEY (norm, town_norm)
);
CREATE INDEX IF NOT EXISTS ix_companies_sector ON companies(sector);

CREATE TABLE IF NOT EXISTS company_events (
    norm TEXT, town_norm TEXT, day TEXT, kind TEXT, detail TEXT   -- added | removed | rating
);

-- Outcome log: what happened after each application. 'no_response' is never stored: it is computed when reporting.
CREATE TABLE IF NOT EXISTS outcomes (
    job_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('applied','acknowledged','screen','interview','offer','rejected','withdrawn')),
    on_date TEXT,                     -- YYYY-MM-DD the event happened
    note TEXT,
    source TEXT,                      -- manual | mark-applied | archive
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outcomes_job ON outcomes(job_id);

-- A1: folders of the append-only archive that the pipeline created or adopted. Lives here, never as marker files in
-- the user's tree. `folder` is relative to paths.applications, '/'-separated ("Lendable", "Lendable/AI Engineer/v2").
CREATE TABLE IF NOT EXISTS archive_index (
    folder TEXT PRIMARY KEY,
    kind TEXT CHECK(kind IN ('company','role')),
    company_key TEXT, job_id TEXT, created_at TEXT
);

-- People to write to about an application (contacts.py). At most two are kept by discovery; `contact add` may add more, and the best two
-- (user, hunter, jd, company_site) are the ones the tracker shows and outreach drafts to. A row is never deleted.
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    name TEXT, role TEXT,
    email TEXT,                       -- lower case; NULL for a person whose address is not known yet
    source TEXT NOT NULL,             -- jd | company_site | hunter | user
    mx_status TEXT,                   -- ok | a_record | no_mx | invalid | unknown (domain check only, no SMTP probe)
    hunter_status TEXT,               -- Hunter's verification status for an address it found (valid, accept_all, ...)
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_contacts_job ON contacts(job_id);

-- Gmail drafts made by outreach.py: one per contact, two per job at most. Nothing here was ever sent by career-ops.
CREATE TABLE IF NOT EXISTS outreach_drafts (
    job_id TEXT NOT NULL, contact_email TEXT NOT NULL, draft_id TEXT, subject TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outreach_job ON outreach_drafts(job_id);

-- The zero-token triage score T of an inbox ad (discovery/triage.py), written by `careerops shortlist` whether or not the ad is offered. Keyed by the
-- duplicate key of company + title (apply/decided.py), so a job that is later added to `jobs` finds its score; `careerops patterns` reads it.
CREATE TABLE IF NOT EXISTS triage_scores (
    key TEXT PRIMARY KEY,             -- "<company family>|<normalised title>"
    company TEXT, title TEXT,
    t REAL,                           -- 1-5
    factors TEXT,                     -- JSON {"A":..,"E":..,"V":..,"L":..,"Fr":..,"P":..}
    scored_on TEXT
);
"""

# Additive columns for databases created before they existed: (table, column, type). Never drop or rename.
COLUMNS = [
    ("jobs", "family", "TEXT"),        # M4b role family (data_scientist, ai_engineer, ...)
    ("jobs", "tier", "INTEGER"),       # M4b priority tier, 1 = highest
    ("jobs", "ceiling_pre", "REAL"),   # M4b honest ceiling at triage, taxonomy keywords only (no LLM)
    ("jobs", "application_dir", "TEXT"),  # A1 archive folder of the latest submission
    ("llm_usage", "tokens", "INTEGER"),   # M5 provider-reported total tokens per day, for the tpd cap
    ("jobs", "sponsor_match", "TEXT"),    # M1 how the employer matched the register: exact | fuzzy(0.95) | none
    ("jobs", "sponsor_route", "TEXT"),     # the matched sponsor's route (Skilled Worker, ...)
    ("jobs", "sponsor_rating", "TEXT"),    # the matched sponsor's rating letter (A / B)
    ("jobs", "cv_pages", "INTEGER"),      # pages of the tailored CV (2 by default, 1 with --one-page)
    ("jobs", "page2_fill", "REAL"),       # how full page 2 is, 0-1, from the rendered PDF (0 for a one-page CV)
    ("companies", "ats_evidence", "TEXT"),   # M3: JSON {rule, board_name, uk_jobs, total_jobs, tried} behind ats / ats_slug
    ("jobs", "agency", "INTEGER"),        # 1 = posted by a recruitment agency, not the employer (a weight on priority, never a filter)
    ("jobs", "analysis", "TEXT"),        # JSON: the job's first keyword extraction, frozen and reused (tailoring/freeze.py)
    ("jobs", "work_country", "TEXT"),    # W: ISO code of the country the work is done in (discovery/workcountry.py); NULL = unknown
    ("jobs", "work_evidence", "TEXT"),   # the header / sentence W rests on (home hidden for a remote role), or "set by you: ..."
    ("jobs", "seen_on", "TEXT"),         # YYYY-MM-DD discover first saw the job (an alert's date or the first run): R counts from here, not from posted_at
    ("jobs", "open_listing", "INTEGER"), # 1 = it was on the employer's own board when discover read it: kept for the record: the posting is still open when discover read it (no longer a factor in the order)
    ("jobs", "posted_at_source", "TEXT"),  # where posted_at comes from: greenhouse.first_published | lever.createdAt | ashby.publishedAt | reed.date | adzuna.created | gmail.received | first_seen (discovery/freshness.py)
    ("jobs", "apply_kind", "TEXT"),      # greenhouse | lever | ashby | workday | employer_other | aggregator_only: where the application is made (discovery/applykind.py)
    ("jobs", "work_location", "TEXT"),   # the location as it may be shown: the remote label for a remote role at home, else NULL (use `location`)
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")   # lets discover + tailor crons overlap safely
        self.conn.executescript(SCHEMA)
        with self.conn:
            for table, col, typ in COLUMNS:
                if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ---- jobs -------------------------------------------------------------
    def upsert_job(self, job: dict[str, Any]) -> bool:
        """Insert a new job or refresh last_seen/description. Returns True if new."""
        ts = now()
        cur = self.conn.execute("SELECT id FROM jobs WHERE id = ?", (job["id"],))
        if cur.fetchone():
            self.conn.execute(
                "UPDATE jobs SET last_seen=?, description=COALESCE(?, description), updated_at=? WHERE id=?",
                (ts, job.get("description"), ts, job["id"]),
            )
            self.conn.commit()
            return False
        cols = ["id", "source", "company", "title", "location", "url", "apply_url",
                "description", "posted_at", "posted_at_source"]
        vals = [job.get(c) for c in cols]
        self.conn.execute(
            f"INSERT INTO jobs ({','.join(cols)}, first_seen, last_seen, updated_at) "
            f"VALUES ({','.join('?' * len(cols))}, ?, ?, ?)",
            (*vals, ts, ts, ts),
        )
        self.conn.commit()
        return True

    def update_job(self, job_id: str, commit: bool = True, **fields: Any) -> None:
        """commit=False lets a caller batch many updates inside one `with db.conn:` transaction."""
        for k in ("flags", "keywords", "analysis"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        if commit:
            self.conn.commit()

    def jobs(self, status: str | Iterable[str] | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM jobs", []
        if status:
            st = [status] if isinstance(status, str) else list(status)
            q += f" WHERE status IN ({','.join('?' * len(st))})"
            args += st
        q += " ORDER BY first_seen, rowid"      # rowid: jobs added within the same second keep their order
        if limit:
            q += f" LIMIT {int(limit)}"
        return list(self.conn.execute(q, args))

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def log(self, job_id: str, stage: str, detail: Any = "") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (job_id, now(), stage, detail))
        self.conn.commit()

    # ---- sponsors ---------------------------------------------------------
    def replace_sponsors(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM sponsors")
            self.conn.executemany("INSERT INTO sponsors VALUES (?,?,?,?,?,?)", rows)

    def sponsor_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sponsors").fetchone()[0]

    # ---- llm quota --------------------------------------------------------
    def llm_calls_today(self, provider: str) -> int:
        day = now()[:10]
        r = self.conn.execute("SELECT calls FROM llm_usage WHERE provider=? AND day=?", (provider, day)).fetchone()
        return r[0] if r else 0

    def llm_count(self, provider: str) -> None:
        day = now()[:10]
        self.conn.execute(
            "INSERT INTO llm_usage (provider, day, calls) VALUES (?,?,1) ON CONFLICT(provider, day) DO UPDATE SET calls = calls + 1",
            (provider, day),
        )
        self.conn.commit()

    def llm_tokens_today(self, provider: str) -> int:
        day = now()[:10]
        r = self.conn.execute("SELECT tokens FROM llm_usage WHERE provider=? AND day=?", (provider, day)).fetchone()
        return (r[0] or 0) if r else 0

    def llm_task_add(self, task: str, model: str, tokens: int) -> None:
        """One answered call of `task` on `model` ("<provider>/<model id>") that cost `tokens`, filed under today."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO llm_task_usage (day, task, model, calls, tokens) VALUES (?,?,?,1,?) "
                "ON CONFLICT(day, task, model) DO UPDATE SET calls = calls + 1, tokens = tokens + excluded.tokens",
                (now()[:10], task, model, int(tokens or 0)))

    def llm_task_today(self, task: str) -> list[tuple[str, int, int]]:
        """[(model, calls, tokens)] for today's calls of `task`, the model that used most tokens first."""
        return [(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT model, calls, tokens FROM llm_task_usage WHERE day=? AND task=? ORDER BY tokens DESC, model", (now()[:10], task))]

    def llm_add_tokens(self, provider: str, tokens: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE llm_usage SET tokens = COALESCE(tokens, 0) + ? WHERE provider=? AND day=?",
                              (int(tokens), provider, now()[:10]))
