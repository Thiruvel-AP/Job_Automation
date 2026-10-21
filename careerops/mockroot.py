"""`--mock` data root: a mock run never opens the real database, outputs, exports or application archive.

Every path in settings.yaml → paths that points at generated data is redirected under `paths.mock_root`
(default data/mock), so `careerops run --mock` works on its own careerops.db and outputs/ and cannot tailor,
re-status or overwrite a real job. Only the user's config files (fact bank, applicant details) are still read — never
written. The mock DB is seeded with one fixture job (the Northwind AI Engineer JD from tests/fixtures) so a mock run
always has something to tailor.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .config import Settings
from .setup import DATA_DIRS, FILE_PARENTS

DEFAULT_MOCK_ROOT = "data/mock"
KEEP_AS_IS = ("fact_bank", "applicant", "answers", "sources", "mock_root")      # config the user owns; read-only for the pipeline
SEED_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "tailoring"
SEED_JOB_ID = "mock:northwind-ai-engineer"


def mock_root(s: Settings) -> Path:
    p = Path(s.raw["paths"].get("mock_root", DEFAULT_MOCK_ROOT))
    return p if p.is_absolute() else s.root / p


def mock_settings(s: Settings) -> Settings:
    """A copy of `s` whose generated-data paths all live under the mock root. The original is not modified."""
    raw = copy.deepcopy(s.raw)
    root, data = mock_root(s), s.root / "data"
    for key in list(raw["paths"]):
        if key in KEEP_AS_IS:
            continue
        p = s.path(key)
        try:
            rel = p.relative_to(data)            # data/exports/x.xlsx -> <mock>/exports/x.xlsx
        except ValueError:
            # outside data/: never reuse it (the frozen keyword lists are config, so they get their own mock folder)
            rel = {"applications": Path("applications"), "frozen_keywords": Path("keywords")}.get(key, Path(p.name))
        raw["paths"][key] = str((root / rel))
    return Settings(raw=raw, root=s.root)


def is_mock(s: Settings) -> bool:
    """Is `s` the mock root's settings (its database lives under paths.mock_root)? The network steps after an application (Gmail drafts,
    the Google Sheet) never run for it."""
    try:
        return s.path("db").is_relative_to(mock_root(s))
    except (KeyError, ValueError):
        return False


def ensure_dirs(s: Settings) -> None:
    for key in DATA_DIRS:
        s.path(key).mkdir(parents=True, exist_ok=True)
    for key in FILE_PARENTS:
        s.path(key).parent.mkdir(parents=True, exist_ok=True)


def seed_job() -> dict:
    return {"id": SEED_JOB_ID, "source": "mock", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "https://example.com/mock-job", "apply_url": "https://example.com/mock-job",
            "description": (SEED_DIR / "jd_northwind.txt").read_text(encoding="utf-8")}


def seed(db, triage) -> str:
    """Put the fixture job in the mock DB and (re)triage it, so every mock run tailors it afresh.
    `triage(db)` is cli.triage_new. Returns the job's status; anything but 'triaged' means a rule change now rejects
    the fixture job, which a mock run should say loudly."""
    job = seed_job()
    db.upsert_job(job)
    db.update_job(job["id"], description=job["description"], status="new")
    triage(db)
    return db.job(job["id"])["status"]
