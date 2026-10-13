"""Where `discover --ats` starts: the companies to look for a job board of, in this order and no other.

  1. the top-level folder names of the application archive (paths.applications): NAMES ONLY. The folders are listed, never
     opened, written, renamed or moved; what is inside them is not read.
  2. `discover.sources` in settings.yaml: employers you add by hand.
  3. sponsor-register companies, only those that are also in 1 or 2 (a register company nobody asked about is never a seed). The
     register adds a flag, and the slug `ats.resolve` verified for the company, if any, which is tried first.

A company is listed once (its name compared as apply.naming.folder_key does: case, spaces, punctuation and Ltd / plc ignored);
the first spelling wins, so the archive's own folder name is the one reported.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..apply.naming import folder_key
from ..db import DB
from . import register as sponsors
from .ats import FETCHABLE


@dataclass
class Seed:
    name: str
    origin: str                      # archive | config
    on_register: bool = False
    hint: str | None = None          # a board slug already verified for this company (ats.resolve), tried first


def archive_names(root: Path) -> list[str]:
    """The names of the folders directly under the archive root, sorted. READ ONLY: one directory listing, nothing opened."""
    try:
        names = [e.name for e in os.scandir(root) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def config_names(settings) -> list[str]:
    """`discover.sources`: company names, or {name: ...} mappings."""
    out = []
    for e in (settings["discover"].get("sources") or []):
        name = e.get("name") if isinstance(e, dict) else e
        if name and str(name).strip():
            out.append(str(name).strip())
    return out


def _verified_slugs(db: DB) -> dict[str, str]:
    """register name (normalised) -> slug of a board that `ats.resolve` verified for it (Greenhouse / Lever / Ashby only)."""
    marks = ",".join("?" * len(FETCHABLE))
    rows = db.conn.execute(f"SELECT register_name, ats_slug FROM companies WHERE ats_verified = 1 AND ats_slug IS NOT NULL "
                           f"AND ats IN ({marks}) ORDER BY register_name", sorted(FETCHABLE)).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(sponsors.normalise(r["register_name"]), r["ats_slug"])
    return out


def seeds(settings, db: DB) -> list[Seed]:
    """The companies to look for a board of: archive folders, then discover.sources, each once; register data only for those."""
    out: list[Seed] = []
    seen: set[str] = set()
    for origin, names in (("archive", archive_names(settings.path("applications"))), ("config", config_names(settings))):
        for name in names:
            key = folder_key(name)
            if key and key not in seen:
                seen.add(key)
                out.append(Seed(name, origin))
    idx = sponsors.load_index(db)
    if idx is not None:
        verified = _verified_slugs(db)
        threshold = settings["sponsors"].get("fuzzy_threshold", 0.92)
        for seed in out:
            m = idx.lookup(seed.name, threshold)
            if m.name and m.method != "none":
                seed.on_register = True
                seed.hint = verified.get(sponsors.normalise(m.name))
    return out
