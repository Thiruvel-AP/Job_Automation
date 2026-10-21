"""READ-ONLY census for the README's "Measured" table: how the jobs in data/careerops.db fare against the per-job auto-apply gates.

    python scripts/auto_census.py

The database is opened with SQLite's mode=ro, report.json files and CV PDFs are only read, and nothing is written, fetched or submitted. It
prints counts only (no company names). The duplicate gate and the daily cap depend on what happened elsewhere and are not part of it.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from careerops.apply import autogate  # noqa: E402
from careerops.config import load_settings  # noqa: E402
from careerops.discovery import applykind  # noqa: E402


def main() -> int:
    s = load_settings()
    db = s.path("db")
    if not db.is_file():
        print(f"no database at {db}")
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM jobs")]
    fb = yaml.safe_load(s.path("fact_bank").read_text(encoding="utf-8"))
    cfg = autogate.config(s)
    print(f"census of {db.name} on {date.today()}")
    print(f"jobs in the database: {len(rows)}")
    print("by status:", dict(Counter(r["status"] for r in rows)))
    print("by apply_kind:", dict(Counter(r.get("apply_kind") or applykind.kind_of(r.get("apply_url") or r.get("url")) or "none" for r in rows)))
    tailored = []
    for r in rows:
        report = Path(r["out_dir"] or "") / "report.json"
        try:
            tailored.append((r, json.loads(report.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    print(f"jobs with a tailored report.json: {len(tailored)}")
    strict = [(rep.get("ats") or {}).get("strict") for _, rep in tailored]
    known = [v for v in strict if v is not None]
    print(f"strict ATS recorded for {len(known)}; at least {cfg.min_ats_strict:g}: {sum(v >= cfg.min_ats_strict for v in known)}; "
          f"values: {sorted(round(v, 1) for v in known)}")
    fails: Counter = Counter()
    passing = 0
    for r, rep in tailored:
        gates = [autogate._ats(rep, cfg), autogate._format(rep, fb), autogate._faithfulness(rep), autogate._level(r, s),
                 autogate._sponsorship(r), autogate._kind(r, cfg)]
        fails.update(g.name for g in gates if not g.ok)
        passing += all(g.ok for g in gates)
    print("tailored jobs failing each per-job gate:", dict(fails))
    print(f"tailored jobs passing all six per-job gates: {passing} of {len(tailored)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
