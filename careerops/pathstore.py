"""Paths kept in report.json: stored relative to the repo root, resolved against the current root on read.

An old report that holds an absolute path from another root (the folder was renamed or moved) is re-rooted in memory: the part after
"/career-ops/" is joined to the current root. The old file is never rewritten by a read. A path that cannot be found stays as written,
so the caller's "no CV PDF" message still names it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)
PATH_KEYS = ("out_dir", "cv_pdf", "letter_pdf", "pdf", "tex")     # the report keys whose string value is a file or folder path
MARK = "/career-ops/"
_logged: set[str] = set()


def to_stored(p: Any, root: Path | None = None) -> Any:
    """A path under the repo root as a root-relative posix string; anything else unchanged."""
    root = Path(root or config.ROOT)
    if not isinstance(p, str) or not p:
        return p
    try:
        return Path(p).relative_to(root).as_posix() if Path(p).is_absolute() else p
    except ValueError:
        return p


def resolve(p: Any, root: Path | None = None) -> Any:
    """A stored path as an absolute path under the current root. Absolute and existing: as is. Absolute and missing: re-rooted at
    "/career-ops/" when that file exists (logged once). Otherwise unchanged."""
    root = Path(root or config.ROOT)
    if not isinstance(p, str) or not p:
        return p
    path = Path(p)
    if not path.is_absolute():
        return str(root / path)
    if path.exists() or MARK not in p:
        return p
    moved = root / p.rsplit(MARK, 1)[1]
    if moved.exists():
        if p not in _logged:
            _logged.add(p)
            log.warning("re-rooted stored path %s -> %s", p, moved)
        return str(moved)
    return p


def _walk(o: Any, fn) -> Any:
    if isinstance(o, dict):
        return {k: (fn(v) if k in PATH_KEYS and isinstance(v, str) else _walk(v, fn)) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(v, fn) for v in o]
    return o


def read_report(path: Path | str, root: Path | None = None) -> dict:
    """report.json with its path fields resolved against the current root. The file on disk is not touched."""
    return _walk(json.loads(Path(path).read_text(encoding="utf-8")), lambda v: resolve(v, root))


def write_report(path: Path | str, rep: dict, root: Path | None = None) -> None:
    """Write report.json with its path fields relative to the repo root (paths outside the root stay absolute)."""
    Path(path).write_text(json.dumps(_walk(rep, lambda v: to_stored(v, root)), indent=2, default=str), encoding="utf-8")


# ------------------------------------------------------------------ v3.9: save the corrected paths once
def _moved(p: Any, root: Path) -> str | None:
    """The root-relative path a stored absolute path from another root now has, or None when it needs no change or cannot be found."""
    if not isinstance(p, str) or not p or not Path(p).is_absolute() or Path(p).exists() or MARK not in p:
        return None
    tail = p.rsplit(MARK, 1)[1]
    return tail if (root / tail).exists() else None


def backup_db(db, tag: str) -> Path | None:
    """A copy of the SQLite file next to it (`<name>.<tag>-<UTC stamp>`), made with SQLite's own backup so it is consistent. Never overwrites."""
    import sqlite3
    from datetime import datetime, timezone

    row = next(iter(db.conn.execute("PRAGMA database_list")), None)
    src = Path(row[2]) if row and row[2] else None
    if src is None or not src.exists():
        return None
    dest = src.with_name(f"{src.name}.{tag}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}")
    if dest.exists():
        return dest
    out = sqlite3.connect(str(dest))
    try:
        db.conn.backup(out)
    finally:
        out.close()
    return dest


def save_rerooted(db, outputs: Path | str, root: Path | None = None) -> int:
    """Write the re-rooted paths back, once: `jobs.out_dir` in the database (absolute, under the current root) and the path fields of every report.json
    under `outputs` (root-relative, what `write_report` stores). Each file that changes is copied first (the database to `<db>.pre-reroot-<stamp>`, a report to
    `report.json.pre-reroot`, never overwriting an existing backup); nothing is deleted. Returns how many stored paths were saved; 0 means nothing
    needed it (the second run), so the caller prints its one summary line only when this is not 0."""
    root = Path(root or config.ROOT)
    outputs = Path(outputs)
    saved = 0
    reports: list[tuple[Path, dict, int]] = []
    for f in sorted(outputs.rglob("report.json")) if outputs.is_dir() else []:
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n = [0]

        def fix(v: str, n=n) -> str:
            new = _moved(v, root)
            if new is None:
                return v
            n[0] += 1
            return new
        fixed = _walk(rep, fix)
        if n[0]:
            reports.append((f, fixed, n[0]))
    rows = [(r["id"], _moved(r["out_dir"], root)) for r in db.conn.execute("SELECT id, out_dir FROM jobs WHERE out_dir IS NOT NULL AND out_dir <> ''")]
    rows = [(i, str(root / new)) for i, new in rows if new]       # the database keeps the ABSOLUTE path (guard.py and assist.py read it as is)
    if rows:
        backup_db(db, "pre-reroot")
        with db.conn:
            db.conn.executemany("UPDATE jobs SET out_dir = ? WHERE id = ?", [(new, i) for i, new in rows])
        saved += len(rows)
    for f, fixed, n in reports:
        bak = f.with_name(f.name + ".pre-reroot")
        if not bak.exists():
            bak.write_bytes(f.read_bytes())
        f.write_text(json.dumps(fixed, indent=2, default=str), encoding="utf-8")
        saved += n
    return saved
