"""SQLite state store. One row per job posting; `status` is the pipeline state machine.

new -> skipped | triaged -> needs_review -> approved | rejected -> applied
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

-- A1: folders of the append-only archive that the pipeline created or adopted. Lives here, never as marker files in
-- the user's tree. `folder` is relative to paths.applications, '/'-separated ("Lendable", "Lendable/AI Engineer/v2").
CREATE TABLE IF NOT EXISTS archive_index (
    folder TEXT PRIMARY KEY,
    kind TEXT CHECK(kind IN ('company','role')),
    company_key TEXT, job_id TEXT, created_at TEXT
);
"""

# Additive columns for databases created before they existed: (table, column, type). Never drop or rename.
COLUMNS = [
    ("jobs", "family", "TEXT"),        # M4b role family (data_scientist, ai_engineer, ...)
    ("jobs", "tier", "INTEGER"),       # M4b priority tier, 1 = highest
    ("jobs", "ceiling_pre", "REAL"),   # M4b honest ceiling at triage, taxonomy keywords only (no LLM)
    ("jobs", "application_dir", "TEXT"),  # A1 archive folder of the latest submission
    ("llm_usage", "tokens", "INTEGER"),   # M5 provider-reported total tokens per day, for the tpd cap
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
                "description", "posted_at"]
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
        for k in ("flags", "keywords"):
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
        q += " ORDER BY first_seen"
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

    def llm_add_tokens(self, provider: str, tokens: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE llm_usage SET tokens = COALESCE(tokens, 0) + ? WHERE provider=? AND day=?",
                              (int(tokens), provider, now()[:10]))
