"""The only code allowed to write under the archive root (Rule 0: append-only).

It can create a new folder and create a new file. It cannot delete, rename, replace or overwrite — there is no such
function here, and a test scans the package to keep it that way.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

CHUNK = 1 << 20


class IntegrityError(OSError):
    """A copied file does not hash like its source. The bad copy is left in place for the user to see."""


def safe_mkdir(path: Path | str) -> Path:
    """Create one new folder. Raises FileExistsError if it exists; never creates missing parents."""
    p = Path(path)
    p.mkdir(parents=False, exist_ok=False)
    return p


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_copy(src: Path | str, dst: Path | str) -> str:
    """Copy src to a NEW file dst and return the sha256 of the bytes written.

    Mode "xb" makes the operating system refuse an existing destination (file, folder or dangling link), with no
    gap between a check and the write. The copy is read back and compared with the source; metadata is not copied.
    """
    h = hashlib.sha256()
    with open(src, "rb") as s, open(dst, "xb") as d:
        for chunk in iter(lambda: s.read(CHUNK), b""):
            h.update(chunk)
            d.write(chunk)
        d.flush()
        os.fsync(d.fileno())
    digest = h.hexdigest()
    if sha256_of(dst) != digest:
        raise IntegrityError(f"{dst}: written bytes do not match {src}")
    return digest
