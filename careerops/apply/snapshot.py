"""Read-only snapshots of the application archive, and the check `careerops live` runs around every job.

The archive (paths.applications) is append-only. A snapshot is `os.walk` + `lstat` of every path: kind, size, mtime_ns.
Nothing is opened, read or written. Comparing two snapshots gives:

  removed   a path that was there before and is gone (a rename shows up as a removal plus an addition)
  modified  a FILE whose size or mtime_ns changed (a folder's size and mtime change whenever something is added to it, so
            for folders only a removal counts)
  stray     an addition that is not under the one <Company>/<Role> folder this job was archived into (the new <Company>
            folder itself, which has to be made on the way there, is allowed)

Any of the three means something outside the pipeline's one permitted write touched the archive.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

Snapshot = dict[str, tuple[str, int, int]]            # relative '/'-path -> (kind 'd'|'f'|'l', size, mtime_ns)


def take(root: Path) -> Snapshot:
    """Every path under `root` (not `root` itself). A missing root is an empty snapshot."""
    out: Snapshot = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue                                # vanished between listing and stat: the next walk will say so
            kind = "d" if os.path.isdir(full) and not os.path.islink(full) else "l" if os.path.islink(full) else "f"
            out[os.path.relpath(full, root).replace(os.sep, "/")] = (kind, st.st_size, st.st_mtime_ns)
    return out


@dataclass
class Check:
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    stray: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)     # additions that were allowed

    @property
    def ok(self) -> bool:
        return not (self.removed or self.modified or self.stray)

    def lines(self) -> list[str]:
        out = [f"REMOVED  {p}" for p in self.removed] + [f"MODIFIED {p}" for p in self.modified]
        return out + [f"STRAY    {p} (not in this job's archive folder)" for p in self.stray]

    def summary(self) -> str:
        if self.ok:
            return f"ok ({len(self.added)} path(s) added)" if self.added else "ok (unchanged)"
        return f"VIOLATION: {len(self.removed)} removed, {len(self.modified)} modified, {len(self.stray)} stray"


def role_folder(root: Path, application_dir: str | Path | None) -> tuple[str, str] | None:
    """(Company, Role) of an archive folder such as <root>/Lendable/AI Engineer[/v2]; None when it is not under `root`."""
    if not application_dir:
        return None
    try:
        parts = Path(application_dir).relative_to(root).parts
    except ValueError:
        return None
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def compare(before: Snapshot, after: Snapshot, allowed: tuple[str, str] | None = None) -> Check:
    """Check `after` against `before`. `allowed` = the (Company, Role) folder the job was archived into, or None when it
    was not archived (then no addition at all is allowed)."""
    res = Check()
    for path, (kind, size, mtime) in sorted(before.items()):
        now = after.get(path)
        if now is None:
            res.removed.append(path)
        elif kind != "d" and (now[0] != kind or now[1] != size or now[2] != mtime):
            res.modified.append(path)
    for path in sorted(set(after) - set(before)):
        parts = path.split("/")
        under = allowed is not None and tuple(parts[:2]) == allowed
        company_dir = allowed is not None and len(parts) == 1 and parts[0] == allowed[0] and after[path][0] == "d"
        (res.added if under or company_dir else res.stray).append(path)
    return res
