"""Skip and applied decisions are final, and cover every copy of the SAME listing.

v2.5: "the same listing" is decided by its canonical keys (apply/listing_keys.py), never by company + title alone: the job id, the ATS key
`ats:<vendor>:<board>:<job_id>` or the normalised URL. Two "Forward Deployed Engineer" postings of one company (London, Munich, Paris: three
Greenhouse ids) are three jobs, and skipping one hides neither of the others. A job that only shares the fingerprint (company family | title |
country) with a decided one is labelled `possible repost of a job you skipped on <date>` (`Decided.repost`) and is still shown to you.
When a job is skipped or applied to:

  * the decision is appended to the decision log (apply/decisions.py, data/state/decisions.jsonl) with all its keys;
  * every other job in the database with the same id / ATS key / URL key is skipped too (an applied job is never touched; a copy of an applied
    job is marked `skipped` with the reason "same job as <id>", it was not applied to);
  * every JD file of it in the inbox or an approved shortlist folder (jds/approved/<date>/) moves to paths.jds_done/skipped/ or
    paths.jds_done/applied/ (never deleted, never overwritten: a name already there gets -2, -3 ...): a skip overrides an earlier approval;
  * `discover`, the inbox intake, `shortlist` and `live` write or show no ad whose id / ATS key / URL key is decided (`Decided`: the decision
    log, the database rows with status skipped / applied, and the files in jds_done/), and discover counts it as `already_decided`.

`unskip` undoes a skip (the job, its copies, their files, and an `unskipped` line in the log); an applied job is final. `sweep` applies the
same rules to what is in the inbox and the approved folders now (`careerops retriage --decided`, a dry run unless --apply; with --apply it
first writes the log lines the database's decided jobs do not have yet). Nothing in this module submits anything.
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
from . import decisions as decisions_mod
from . import listing_keys as LK

SKIPPED, APPLIED = "skipped", "applied"
DECISIONS = (SKIPPED, APPLIED)
# live's result statuses and the database statuses that are a decision
STATUS_DECISION = {"skipped": SKIPPED, "skipped (rules)": SKIPPED, "skipped (user)": SKIPPED, "applied": APPLIED,
                   "applied (archive incomplete)": APPLIED}
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


def file_keys(path: Path) -> LK.Keys:
    """The canonical keys of a JD file, from its Company / Title / URL / Location header."""
    hdr = header_of(path)
    return LK.keys_for(hdr.get("company"), hdr.get("title"), hdr.get("url"), hdr.get("apply_url") or hdr.get("apply url"), hdr.get("location"))


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
    keys: dict[Key, str] = field(default_factory=dict)         # (company family, title) -> decision: kept for reports, never decides on its own
    strong: dict[str, str] = field(default_factory=dict)       # ATS key / URL key -> decision (applied wins over skipped)
    prints: dict[str, tuple[str, str, str]] = field(default_factory=dict)      # fingerprint -> (decision, date, job id)
    book: decisions_mod.Book = field(default_factory=decisions_mod.Book)

    def add(self, key: Key, decision: str, job_id: str | None = None, lk: LK.Keys | None = None, date: str = "") -> None:
        if job_id:
            self.ids[job_id] = decision
        if usable(key) and self.keys.get(key) != APPLIED:
            self.keys[key] = decision
        for k in (lk.strong() if lk is not None else []):
            if self.strong.get(k) != APPLIED:
                self.strong[k] = decision
        if lk is not None and lk.fingerprint and lk.fingerprint not in self.prints:
            self.prints[lk.fingerprint] = (decision, date, job_id or "")

    def hit(self, company: str, title: str, url: str | None = None, location: str | None = None, apply_url: str | None = None,
            job_id: str | None = None) -> str | None:
        """The decision that covers this ad: by its job id, its ATS key or its URL key (the decision log first, then the database rows and the
        files in jds_done/). Company + title alone never decide (v2.5). None = not decided."""
        jid = job_id or (manual.job_id_for(company, title, url) if url else None)
        if jid and self.ids.get(jid):
            return self.ids[jid]
        lk = LK.keys_for(company, title, url, apply_url, location, jid)
        m = self.book.decided(lk, jid)
        if m is not None:
            return APPLIED if m.decision == decisions_mod.APPLIED else SKIPPED
        return next((self.strong[k] for k in lk.strong() if k in self.strong), None)

    def repost(self, company: str, title: str, url: str | None = None, location: str | None = None, apply_url: str | None = None,
               job_id: str | None = None) -> str:
        """`possible repost of a job you skipped on <date> (<id>)` when only the fingerprint matches a decided job; "" otherwise."""
        if self.hit(company, title, url, location, apply_url, job_id):
            return ""
        jid = job_id or (manual.job_id_for(company, title, url) if url else None)
        lk = LK.keys_for(company, title, url, apply_url, location, jid)
        m = self.book.repost(lk, jid)
        if m is not None and m.record.get("job_id") != jid:
            return m.label()
        got = (self.prints.get(lk.fingerprint) or self.prints.get(_no_country(lk.fingerprint))) if lk.fingerprint else None
        if got and got[2] != jid:
            what = "applied to" if got[0] == APPLIED else "skipped"
            return f"possible repost of a job you {what} on {got[1][:10] or '?'} ({got[2] or '?'})"
        return ""


def load(settings, db: DB) -> Decided:
    """Every decision made so far: the decision log, database rows with status skipped / applied, and the JD files in jds_done/skipped and
    jds_done/applied (those stay decided even if a later retriage gives the row a new status)."""
    d = Decided(book=decisions_mod.Book.load(settings))
    for r in db.conn.execute("SELECT * FROM jobs WHERE status IN (?, ?)", (SKIPPED, APPLIED)):
        d.add(key_of(r["company"], r["title"]), STATUS_DECISION[r["status"]], r["id"], LK.keys_of_row(r), str(r["updated_at"] or ""))
    for decision in DECISIONS:
        for p in txt_files(decision_dir(settings, decision)):
            key = file_key(p)
            if key:
                d.add(key, decision, file_job_id(p), file_keys(p))
    return d


def _no_country(fp: str) -> str:
    """The fingerprint with its country left blank: a decided job whose place was not recorded still labels a repost."""
    return fp.rsplit("|", 1)[0] + "|" if fp else ""


def same_listing(a: LK.Keys, b: LK.Keys) -> bool:
    """Do two jobs share an ATS key or a URL key (the same posting)?"""
    return bool((a.ats and a.ats == b.ats) or (a.url and a.url == b.url))


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


def group_rows(db: DB, job) -> list:
    """The job and every database row of the same listing (same id, ATS key or URL key)."""
    mine = LK.keys_of_row(job)
    return [r for r in db.jobs() if r["id"] == job["id"] or same_listing(mine, LK.keys_of_row(r))]


def group_files(folder: Path, job) -> list[Path]:
    """The JD files in `folder` of the same listing as `job` (its id, ATS key or URL key). Given a (company family, title) key instead of a job,
    the files with that key (what a report or a test asks: "is a file of this job there?")."""
    if isinstance(job, tuple):
        return [p for p in txt_files(folder) if file_key(p) == job] if usable(job) else []
    mine = LK.keys_of_row(job)
    return [p for p in txt_files(folder) if file_job_id(p) == job["id"] or same_listing(mine, file_keys(p))]


def approved_dirs(settings) -> list[Path]:
    """The shortlist's approved folders (jds/approved/<date>/): a skip takes a job out of them too."""
    root = settings.path("jds") / "approved"
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def record(settings, job, decision: str, reason: str = "") -> dict:
    """Append the decision for `job` (a database row) to the decision log, with all its keys."""
    kind = {APPLIED: decisions_mod.APPLIED}.get(decision) or (decisions_mod.SKIPPED_USER if "user" in reason or "review" in reason or "you" in reason
                                                              else decisions_mod.SKIPPED)
    return decisions_mod.append(settings, LK.keys_of_row(job), kind, reason, job["id"])


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


def settle(db: DB, settings, job_id: str, decision: str, *, also: Path | None = None, reason: str = "") -> Settled:
    """Apply `decision` (skipped | applied) to the job and to every copy of the same listing: the decision log gets a line with its keys, copies
    in the database are skipped (an applied row is left alone), every inbox or approved-folder file of it (and `also`, the file the job came
    from) moves to jds_done/<decision>/. An applied copy in the group makes the group's decision `applied`. Idempotent (a second call adds a
    line to the log, which is append-only, and moves nothing)."""
    if decision not in DECISIONS:
        raise DecidedError(f"unknown decision {decision!r}")
    job = db.job(job_id)
    if job is None:
        raise DecidedError(f"no such job: {job_id}")
    rows = group_rows(db, job)
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
    record(settings, job, decision, reason or str(job["reason"] or ""))
    files = [f for folder in (inbox_dir(settings), *approved_dirs(settings)) for f in group_files(folder, job)]
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
    backfilled: list[dict] = field(default_factory=list)                   # decision-log lines written first (apply)

    def lines(self, applied: bool) -> list[str]:
        out = ([f"decision log: {len(self.backfilled)} line(s) written for decided jobs that had none"] if self.backfilled else [])
        out += [f"{len(self.moves)} inbox file(s) {'moved' if applied else 'would move'} out of the inbox; "
               f"{len(self.copies)} job(s) {'set to skipped' if applied else 'would be set to skipped'}."]
        out += [f"  {p.name} -> jds_done/{d}/  ({why})" for p, d, why in self.moves]
        out += [f"  job {jid} -> skipped  ({why})" for jid, d, why in self.copies]
        if not applied and (self.moves or self.copies):
            out.append("Dry run: nothing was changed. Run again with --apply to do it.")
        return out


def sweep(db: DB, settings, *, apply: bool = False) -> Sweep:
    """The decided jobs' copies that are still around: inbox and approved-folder files whose id / ATS key / URL key is decided, and database rows
    (not skipped, not applied) of a decided listing. A dry run lists them; `apply` first writes the decision-log lines the database's decided jobs
    lack (decisions.backfill), then skips the rows and moves the files (to jds_done/<decision>/, never deleted)."""
    plan = Sweep()
    if apply:
        plan.backfilled = decisions_mod.backfill(db, settings)
    decided = load(settings, db)
    for folder in (inbox_dir(settings), *approved_dirs(settings)):
        for p in txt_files(folder):
            hdr = header_of(p)
            if not (hdr.get("company") and hdr.get("title")):
                continue
            which = decided.hit(hdr["company"], hdr["title"], hdr.get("url"), hdr.get("location"))
            if which:
                plan.moves.append((p, which, f"same job as one you {'applied to' if which == APPLIED else 'skipped'}"))
    for r in db.jobs():
        if r["status"] in (SKIPPED, APPLIED) or r["status"].startswith(("skipped", "applied")):
            continue
        which = decided.hit(r["company"], r["title"], r["url"], r["location"], r["apply_url"], r["id"])
        if which and decided.ids.get(r["id"]) is None:
            plan.copies.append((r["id"], which, f"same job as one you {'applied to' if which == APPLIED else 'skipped'}"))
        elif which:
            plan.copies.append((r["id"], which, f"you {'applied to' if which == APPLIED else 'skipped'} this job"))
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
    rows = group_rows(db, job)
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
    for r in rows:
        if r["id"] in res.job_ids:
            decisions_mod.append(settings, LK.keys_of_row(r), decisions_mod.UNSKIPPED, f"careerops unskip --id {job_id}", r["id"])
    for f in group_files(decision_dir(settings, SKIPPED), job):
        res.moved.append((f, move_file(settings, f, inbox_dir(settings))))
    _reopen(settings, set(res.job_ids))
    return res


def bring_back(settings, job_id: str, company: str, title: str) -> list[Path]:
    """`retriage --ats`: a job skipped on the ATS band that now reaches the review band. Its JD file(s) move from jds_done/skipped/ back to
    the inbox (the `.reason.txt` beside them stays) and live's record of it is reopened. Returns the files moved."""
    key = key_of(company, title)
    moved = [move_file(settings, f, inbox_dir(settings)) for f in txt_files(decision_dir(settings, SKIPPED))
             if file_job_id(f) == job_id or (usable(key) and file_key(f) == key)]
    _reopen(settings, {job_id})
    return moved


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
