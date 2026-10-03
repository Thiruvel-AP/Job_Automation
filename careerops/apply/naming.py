"""Names for the archive: folder keys, NTFS-safe components, recruiter-visible file names. Pure functions."""
from __future__ import annotations

import hashlib
import re

# The archive lives on D: (NTFS), reached from WSL, so every component must be legal there.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Trailing legal suffixes only. NOT "co": folder_key keeps it so `Zone & Co` == `Zone and Co`, `Co-op` == `COOP`.
_SUFFIX = re.compile(r"[\s,]+(ltd|limited|plc|llp|lp|inc|incorporated|llc|corp|corporation|gmbh|sa|bv|ag)\.?$")
FILE_NAME_MAX = 80
STYLES = ("full", "short")


def safe_name(s: str, limit: int = 80) -> str:
    """One legal NTFS path component: illegal chars and control chars -> '-', whitespace collapsed, no trailing
    dots/spaces, reserved device names get a '_', capped at `limit` characters."""
    s = re.sub(r"\s+", " ", _ILLEGAL.sub("-", str(s))).strip()
    s = s[:limit].rstrip(" .")
    stem, dot, rest = s.partition(".")
    if stem.upper() in _RESERVED:
        s = f"{stem}_{dot}{rest}"[:limit].rstrip(" .")
    return s or "untitled"


def folder_key(name: str) -> str:
    """Key for matching company folders: lower-case, `&` -> ` and `, drop `(...)`, strip trailing legal suffixes,
    keep [a-z0-9]. Deliberately not discovery.register.normalise() (that one strips `co` anywhere)."""
    s = re.sub(r"\([^)]*\)", " ", name.lower().replace("&", " and ")).strip()
    while True:
        t = _SUFFIX.sub("", s).strip()
        if t == s or not t:
            break
        s = t
    return re.sub(r"[^a-z0-9]", "", s)


def id6(job_id: str) -> str:
    """First 6 hex chars of sha1(job_id). Job ids contain colons (greenhouse:acme:7), which NTFS forbids."""
    return hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:6]


def file_stem(identity_name: str, kind: str, style: str = "full") -> str:
    """'<name> CV' / '<name> Cover Letter'. No company, role or date: the folder carries those.
    The whole file name with a 3-letter extension stays within 80 characters."""
    if style not in STYLES:
        raise ValueError(f"files.name_style must be one of {STYLES}, got {style!r}")
    parts = identity_name.split()
    name = parts[0] if style == "short" and parts else identity_name
    room = FILE_NAME_MAX - 4 - len(kind) - 1
    return safe_name(f"{safe_name(name, room)} {kind}", FILE_NAME_MAX - 4)
