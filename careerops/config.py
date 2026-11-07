"""Settings + fact bank loading. Paths in settings.yaml are relative to the repo root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("CAREEROPS_ROOT", Path(__file__).resolve().parent.parent))
SETTINGS_ENV = "CAREEROPS_SETTINGS"      # a settings file to use when none is given (the test suite points it at tests/fixtures/config/)
# A paths key the settings file may leave out: the built-in folder is used. Never one of the user's own folders.
# jds_done: the JD files of skipped / applied jobs (discovery/decided.py). facts_proposed: facts the user typed in review for a gap keyword
# (tailoring/provisional.py); they wait there, provisional for their own job, until the user saves them to the fact bank.
# state: the program's own memory (targeting.json: the last region / size you chose, choices.py). company_size: the 90-day cache of company sizes
# (discovery/size.py); company_size_overrides.yaml is YOURS (`Graphcore: large`): read only, a missing file means no overrides.
DEFAULT_PATHS = {"applications_auto": "data/applications_auto", "jds_done": "data/jds_done", "letter_feedback": "data/letters/feedback.jsonl",
                 "facts_proposed": "data/facts/proposed.yaml",     # where the pipeline writes its archive; paths.applications is read-only
                 "state": "data/state", "company_size": "data/company_size.jsonl", "company_size_overrides": "data/company_size_overrides.yaml",
                 "search": "data/search",         # v3.1 agent search: plan-<date>.json, <date>.csv (query_id,url), yield.jsonl, ingest.jsonl
                 "evals": "data/evals"}           # v3.2 the agent's evaluations (<job id>.json, evals.jsonl), decisions.jsonl, digest-<date>.json


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path

    def path(self, key: str) -> Path:
        if key not in self.raw["paths"] and key in DEFAULT_PATHS:
            return self.root / DEFAULT_PATHS[key]
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    def archive_roots(self) -> list[Path]:
        """Every folder of submitted applications that is READ (duplicate check, company list, import): paths.applications (yours,
        never written) and paths.applications_auto (where this pipeline writes)."""
        out: list[Path] = []
        for key in ("applications", "applications_auto"):
            p = self.path(key)
            if p not in out:
                out.append(p)
        return out

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else Path(os.environ.get(SETTINGS_ENV) or ROOT / "config" / "settings.yaml")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(raw=raw, root=ROOT)


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
