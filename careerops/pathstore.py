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
