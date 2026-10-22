"""Skip and applied decisions are final, and cover every copy of a job.

A job is "the same" when its duplicate key agrees: the company family (candidate.family_key: "Graphcore Early Careers" is
"Graphcore") and the normalised title ("Graduate Machine Learning Engineer 2027" on two boards of one employer). Place is not
part of the key. When one copy is skipped or applied to:

  * every other job in the database with that key is skipped too (an applied job is never touched; a copy of an applied job is
    marked `skipped` with the reason "same job as <id>", it was not applied to);
  * every JD file in the inbox with that key moves to paths.jds_done/skipped/ or paths.jds_done/applied/ (never deleted, never
    overwritten: a name already there gets -2, -3 ...);
  * `discover` writes no ad whose job id or key is decided (`Decided`: the database rows with status skipped / applied, and the
    files in jds_done/), and counts it as `already_decided`.

`unskip` undoes a skip (the job, its copies, their files); an applied job is final. `sweep` applies the same rules to what is in
the inbox now (`careerops retriage --decided`, a dry run unless --apply). Nothing in this module submits anything.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..db import DB, now
from ..discovery import candidate as cand
from ..discovery import manual

SKIPPED, APPLIED = "skipped", "applied"
DECISIONS = (SKIPPED, APPLIED)
# live's result statuses and the database statuses that are a decision
STATUS_DECISION = {"skipped": SKIPPED, "skipped (rules)": SKIPPED, "applied": APPLIED, "applied (archive incomplete)": APPLIED}
Key = tuple[str, str]


class DecidedError(Exception):
    """A decision could not be applied or undone; the message says why. Nothing was changed."""


def key_of(company: str | None, title: str | None) -> Key:
    """The duplicate key: (company family, normalised title). Blank on either side never matches anything (see `usable`)."""
    return (cand.family_key(company or ""), cand.norm_title(title or ""))


def usable(key: Key) -> bool:
    return bool(key[0] and key[1])


def decision_dir(settings, decision: str) -> Path:
    return settings.path("jds_done") / decision


def inbox_dir(settings) -> Path:
    return settings.path("jds") / "inbox"


def header_of(path: Path) -> dict[str, str]:
    """The Company / Title / URL ... header lines of a JD file ({} when it cannot be read)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(20))
    except OSError:
        return {}
    return manual.header_fields(head)


def file_key(path: Path) -> Key | None:
    hdr = header_of(path)
    key = key_of(hdr.get("company"), hdr.get("title"))
    return key if usable(key) else None


def file_job_id(path: Path) -> str | None:
    """The id `careerops live` would give this file's job (None without Company and Title headers)."""
    hdr = header_of(path)
    if not (hdr.get("company") and hdr.get("title")):
        return None
    return manual.job_id_for(hdr["company"], hdr["title"], hdr.get("url"))


def txt_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".txt")
    except OSError:
        return []


# ------------------------------------------------------------------ what is decided
@dataclass
class Decided:
    ids: dict[str, str] = field(default_factory=dict)          # job id -> decision
    keys: dict[Key, str] = field(default_factory=dict)         # duplicate key -> decision (applied wins over skipped)

    def add(self, key: Key, decision: str, job_id: str | None = None) -> None:
        if job_id:
            self.ids[job_id] = decision
        if usable(key) and self.keys.get(key) != APPLIED:
            self.keys[key] = decision

    def hit(self, company: str, title: str, url: str | None = None) -> str | None:
        """The decision that covers this ad (by job id when the URL is known, else by key), or None."""
        if url:
            found = self.ids.get(manual.job_id_for(company, title, url))
            if found:
                return found
        key = key_of(company, title)
        return self.keys.get(key) if usable(key) else None


def load(settings, db: DB) -> Decided:
    """Every decision made so far: database rows with status skipped / applied, and the JD files in jds_done/skipped and jds_done/applied
    (those stay decided even if a later retriage gives the row a new status)."""
    d = Decided()
    for r in db.conn.execute("SELECT id, company, title, status FROM jobs WHERE status IN (?, ?)", (SKIPPED, APPLIED)):
        d.add(key_of(r["company"], r["title"]), STATUS_DECISION[r["status"]], r["id"])
    for decision in DECISIONS:
        for p in txt_files(decision_dir(settings, decision)):
            key = file_key(p)
            if key:
                d.add(key, decision, file_job_id(p))
    return d


# ------------------------------------------------------------------ moving files
def _guard(settings, *paths: Path) -> None:
    roots = [r.resolve() for r in settings.archive_roots()]
    for p in paths:
        q = p.resolve()
        if any(q == r or r in q.parents for r in roots):
            raise DecidedError(f"refusing to move {p}: the application archive is never touched")


def _unique(folder: Path, name: str) -> Path:
    stem, suffix = os.path.splitext(name)
    n, target = 1, folder / name
    while target.exists():
        n += 1
        target = folder / f"{stem}-{n}{suffix}"
    return target


def move_file(settings, src: Path, folder: Path) -> Path:
    """Move a JD file into `folder` (made if needed). Never over a file, never into or out of the application archive."""
    folder.mkdir(parents=True, exist_ok=True)
    dst = _unique(folder, src.name)
    _guard(settings, src, dst)
    shutil.move(str(src), str(dst))
    return dst


def group_rows(db: DB, key: Key) -> list:
    return [r for r in db.jobs() if key_of(r["company"], r["title"]) == key] if usable(key) else []


def group_files(folder: Path, key: Key) -> list[Path]:
    return [p for p in txt_files(folder) if file_key(p) == key] if usable(key) else []


# ------------------------------------------------------------------ a decision
@dataclass
class Settled:
    decision: str
    job_id: str
    copies: list[str] = field(default_factory=list)                 # other jobs set to skipped
    moved: list[tuple[Path, Path]] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"{self.decision}: {self.job_id}" + (f" and {len(self.copies)} cop{'y' if len(self.copies) == 1 else 'ies'} "
                                                     f"({', '.join(self.copies)})" if self.copies else "")]
        out += [f"  moved {src.name} -> {dst.parent.parent.name}/{dst.parent.name}/{dst.name}" for src, dst in self.moved]
        return out


def settle(db: DB, settings, job_id: str, decision: str, *, also: Path | None = None) -> Settled:
    """Apply `decision` (skipped | applied) to the job and to every copy of it: copies in the database are skipped (an applied row is left
    alone), every inbox file with the key (and `also`, the file the job came from) moves to jds_done/<decision>/. An applied copy in the
    group makes the group's decision `applied`. Idempotent."""
    if decision not in DECISIONS:
        raise DecidedError(f"unknown decision {decision!r}")
    job = db.job(job_id)
    if job is None:
        raise DecidedError(f"no such job: {job_id}")
    key = key_of(job["company"], job["title"])
    rows = group_rows(db, key)
    if any(r["status"] == APPLIED for r in rows):
        decision = APPLIED
    res = Settled(decision, job_id)
    with db.conn:
        for r in rows:
            if r["id"] == job_id or r["status"] in (APPLIED, SKIPPED):
                continue
            db.update_job(r["id"], commit=False, status=SKIPPED, reason=f"same job as {job_id}: {decision}")
            db.conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                            (r["id"], now(), "decided", json.dumps({"copy_of": job_id, "decision": decision})))
            res.copies.append(r["id"])
    files = group_files(inbox_dir(settings), key)
    if also is not None and also.exists() and also not in files:
        files.append(also)
    for f in files:
        res.moved.append((f, move_file(settings, f, decision_dir(settings, decision))))
    return res


# ------------------------------------------------------------------ the inbox now (retriage --decided)
@dataclass
class Sweep:
    moves: list[tuple[Path, str, str]] = field(default_factory=list)       # (inbox file, decision, why)
    copies: list[tuple[str, str, str]] = field(default_factory=list)       # (job id, decision, why): database rows to skip
    moved: list[tuple[Path, Path]] = field(default_factory=list)           # filled by apply

    def lines(self, applied: bool) -> list[str]:
        out = [f"{len(self.moves)} inbox file(s) {'moved' if applied else 'would move'} out of the inbox; "
               f"{len(self.copies)} job(s) {'set to skipped' if applied else 'would be set to skipped'}."]
        out += [f"  {p.name} -> jds_done/{d}/  ({why})" for p, d, why in self.moves]
        out += [f"  job {jid} -> skipped  ({why})" for jid, d, why in self.copies]
        if not applied and (self.moves or self.copies):
            out.append("Dry run: nothing was changed. Run again with --apply to do it.")
        return out


def sweep(db: DB, settings, *, apply: bool = False) -> Sweep:
    """The decided jobs' copies that are still around: inbox files whose job id or key is decided, and database rows (not skipped, not
    applied) whose key is. A dry run lists them; `apply` skips the rows and moves the files (to jds_done/<decision>/, never deleted)."""
    decided = load(settings, db)
    plan = Sweep()
    for p in txt_files(inbox_dir(settings)):
        hdr = header_of(p)
        if not (hdr.get("company") and hdr.get("title")):
            continue
        which = decided.hit(hdr["company"], hdr["title"], hdr.get("url"))
        if which:
            plan.moves.append((p, which, f"same job as one you {'applied to' if which == APPLIED else 'skipped'}"))
    for r in db.jobs():
        key = key_of(r["company"], r["title"])
        if usable(key) and r["status"] not in (SKIPPED, APPLIED) and key in decided.keys:
            plan.copies.append((r["id"], decided.keys[key], f"same job as one you {'applied to' if decided.keys[key] == APPLIED else 'skipped'}"))
    if apply:
        with db.conn:
            for jid, which, why in plan.copies:
                db.update_job(jid, commit=False, status=SKIPPED, reason=f"{why}: {which}")
                db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (jid, now(), "decided", json.dumps({"retriage_decided": which})))
        for p, which, _ in plan.moves:
            plan.moved.append((p, move_file(settings, p, decision_dir(settings, which))))
    return plan


# ------------------------------------------------------------------ undo
@dataclass
class Restored:
    job_ids: list[str] = field(default_factory=list)
    moved: list[tuple[Path, Path]] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [f"unskipped {', '.join(self.job_ids)}"] + [f"  moved {src.name} -> inbox/{dst.name}" for src, dst in self.moved]


def restore(db: DB, settings, job_id: str) -> Restored:
    """Undo a skip: the job and every skipped copy go back to a status `careerops live` works on ('needs_review' if tailored, else 'new'),
    their files move from jds_done/skipped/ to the inbox, and live's record of them as finished is reopened. An applied job is final."""
    job = db.job(job_id)
    if job is None:
        raise DecidedError(f"no such job: {job_id}")
    key = key_of(job["company"], job["title"])
    rows = group_rows(db, key)
    if any(r["status"] == APPLIED for r in rows):
        raise DecidedError(f"{job_id} is the same job as one you applied to: an applied decision is final")
    if job["status"] != SKIPPED:
        raise DecidedError(f"{job_id} is '{job['status']}', not skipped")
    res = Restored()
    with db.conn:
        for r in rows:
            if r["status"] != SKIPPED:
                continue
            db.update_job(r["id"], commit=False, status="needs_review" if r["out_dir"] else "new", reason=None)
            db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (r["id"], now(), "decided", json.dumps({"unskipped": job_id})))
            res.job_ids.append(r["id"])
    for f in group_files(decision_dir(settings, SKIPPED), key):
        res.moved.append((f, move_file(settings, f, inbox_dir(settings))))
    _reopen(settings, set(res.job_ids))
    return res


def _reopen(settings, job_ids: set[str]) -> None:
    """In live's state file, mark the entries of these jobs not done, so that live works on their files again."""
    path = settings.path("live") / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data["jobs"]
    except (OSError, ValueError, KeyError):
        return
    changed = False
    for entry in jobs.values():
        if entry.get("job_id") in job_ids and entry.get("done"):
            entry["done"] = False
            entry.pop("status", None)
            changed = True
    if changed:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
