"""v3.9.3: the two answers saved in `.env` so they are asked once, not every run: CAREEROPS_REGION and CAREEROPS_COMPANY_SIZE.

  * read:   `saved(path, key)` reads the key from the file itself (never from os.environ, so a value loaded earlier in the same process cannot hide a
            missing key); an inline `# comment` and surrounding quotes are dropped.
  * append: `append(path, key, value)` copies `.env` to `.env.bak-<YYYYMMDD-HHMMSS>` first, then adds ONE line at the end. Nothing else is reordered,
            rewritten or printed (the other lines hold secrets).
  * set:    `set_value(path, key, value)` takes the same backup and replaces only the key's own line(s); every other byte stays as it was. The file is
            rewritten in place (same inode), so its permissions are kept.
`env_path(settings)` is `<repo>/.env`, or the file named by CAREEROPS_ENV_FILE (tests point it at tmp_path; the real `.env` is never touched by a test).
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

REGION_KEY = "CAREEROPS_REGION"
SIZE_KEY = "CAREEROPS_COMPANY_SIZE"
ENV_FILE_VAR = "CAREEROPS_ENV_FILE"
KEYS = {"region": REGION_KEY, "size": SIZE_KEY}
FALLBACK = {"region": "uk", "size": "both"}          # an unattended run with nothing saved uses these, with a warning
# the size as written in .env (the README / .env.example names) <-> the mode the code uses (choices.SIZE_MODES)
SIZE_TO_ENV = {"sme": "sme", "large": "large", "both": "both", "sme-first": "both_sme_first", "large-first": "both_large_first"}


def env_path(settings) -> Path:
    override = os.environ.get(ENV_FILE_VAR)
    return Path(override) if override else Path(settings.root) / ".env"


def _key_line(key: str) -> re.Pattern[bytes]:
    return re.compile(rb"^[ \t]*(?:export[ \t]+)?" + re.escape(key.encode()) + rb"[ \t]*=")


def _value(raw: str) -> str:
    v = raw.strip()
    if v and v[0] in "\"'" and v.find(v[0], 1) > 0:
        return v[1:v.find(v[0], 1)]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def saved(path: Path, key: str) -> str | None:
    """The key's value in the file (the last line that sets it, as a shell would), or None when the file or the key is missing or the value is blank."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    pat, found = _key_line(key), None
    for line in data.decode("utf-8", errors="replace").splitlines():
        if pat.match(line.encode("utf-8", errors="replace")):
            found = _value(line.split("=", 1)[1]) or None
    return found


def backup(path: Path, now: datetime | None = None) -> Path | None:
    """`.env` -> `.env.bak-<YYYYMMDD-HHMMSS>` beside it (copy2: bytes and permissions); None when there is no `.env` yet. Never overwrites a backup."""
    path = Path(path)
    if not path.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest, n = path.with_name(f"{path.name}.bak-{stamp}"), 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.bak-{stamp}-{n}")
    shutil.copy2(path, dest)
    return dest


def append(path: Path, key: str, value: str, now: datetime | None = None) -> Path | None:
    """Add `KEY=value` as the file's last line, after a backup. Returns the backup (None when `.env` did not exist and was created, owner-only)."""
    path = Path(path)
    bak = backup(path, now)
    if bak is None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(f"{key}={value}\n".encode("utf-8"))
        return None
    data = path.read_bytes()
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    with open(path, "ab") as fh:                          # append mode: the bytes before are never rewritten
        fh.write((nl if data and not data.endswith(b"\n") else b"") + f"{key}={value}".encode("utf-8") + nl)
    return bak


def set_value(path: Path, key: str, value: str, now: datetime | None = None) -> tuple[str | None, Path | None]:
    """(old value, backup) after replacing the key's line(s) with `KEY=value`; a key the file does not have is appended. Only that line changes."""
    path = Path(path)
    old = saved(path, key)
    if not path.exists() or old is None and not any(_key_line(key).match(l) for l in path.read_bytes().splitlines()):
        return old, append(path, key, value, now)
    bak = backup(path, now)
    pat, out, done = _key_line(key), [], False
    for line in path.read_bytes().splitlines(keepends=True):
        if pat.match(line):
            if done:
                continue                                  # a second line setting the same key would win in a shell: drop it, keep one
            end = line[len(line.rstrip(b"\r\n")):]
            out.append(f"{key}={value}".encode("utf-8") + end)
            done = True
        else:
            out.append(line)
    with open(path, "r+b") as fh:                         # in place: same file, same permissions
        fh.write(b"".join(out))
        fh.truncate()
    return old, bak
