"""A1 — keep a permanent record of exactly what was sent: <archive root>/<Company>/<Role>/ (+ vN for resubmissions).

Rule 0: the archive root is APPEND-ONLY. This module only ever creates new folders and new files there, and only
through safe_io. It never deletes, renames, moves, overwrites or edits anything that already exists. Bookkeeping
(which folders are ours, job -> folder) lives in the `archive_index` table, not in marker files in the user's tree.

Order of work: preflight (read-only: root, letter, wrong-file guard, names, path length) -> stage the files in
paths.tmp -> create the new folders and copy the files in -> one DB transaction. A failure while copying leaves the
half-made folder where it is, flags the job, and never cleans up inside the archive.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from difflib import SequenceMatcher
from itertools import count
from pathlib import Path

from ..db import DB, now
from ..reporting.outcomes import record_applied
from .errors import ArchiveError, ArchiveIncomplete, ArchiveRootMissing, LetterMissing
from .guard import check_job_files
from .naming import folder_key, id6, safe_name
from .safe_io import safe_copy, safe_mkdir, sha256_of

MAX_PATH = 240          # Windows MAX_PATH is 260; keep clear of it
MIN_ROLE_CHARS = 12     # below this a role folder is no longer recognisable: refuse instead
NEAR_MISS = 0.85        # SequenceMatcher ratio on folder keys that triggers a "possible existing folder" flag


@dataclass
class Archived:
    folder: Path                      # the folder the files went into (role folder, or its vN)
    rel: str                          # same, relative to the archive root, '/'-separated
    version: int                      # 1 = first submission of this job, 2+ = resubmissions
    files: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class Plan:
    root: Path
    company: str                      # company folder name
    company_new: bool                 # create it (else it exists: reused or adopted)
    adopt: bool                       # existing hand-made folder to be recorded in archive_index
    key: str
    role_dir: Path
    role_rel: str
    version: int
    flags: list[str]
    sources: list[Path]               # CV pdf/tex, letter pdf/tex, report.json — files that already exist
    letter_missing: bool
    applied_at: str
    day: str


# ------------------------------------------------------------------ names and lookup (read-only)
def _applied(applied_on: date | None) -> tuple[str, str]:
    if applied_on:
        at = datetime.combine(applied_on, time(0, 0), tzinfo=timezone.utc).isoformat(timespec="seconds")
        return at, applied_on.isoformat()
    at = now()
    return at, at[:10]


def _own_role(db: DB, root: Path, job_id: str) -> str | None:
    """Folder (relative) this job was archived into before, if it is still there."""
    for (folder,) in db.conn.execute(
            "SELECT folder FROM archive_index WHERE job_id=? AND kind='role' ORDER BY length(folder)", (job_id,)):
        if folder.count("/") == 1 and (root / folder).is_dir():
            return folder
    return None


def _company(db: DB, root: Path, company: str) -> tuple[str, bool, bool, str, list[str]]:
    """-> (folder name, create?, adopt?, key, flags). Create if missing, else reuse; never guess on similarity."""
    key = folder_key(company) or folder_key(safe_name(company)) or "company"
    entries = sorted(root.iterdir(), key=lambda p: (p.name.casefold(), p.name))
    dirs = [p.name for p in entries if p.is_dir()]
    indexed = {r[0] for r in db.conn.execute(
        "SELECT folder FROM archive_index WHERE kind='company' AND company_key=?", (key,))}
    same = [d for d in dirs if folder_key(d) == key or d in indexed]
    if same:
        pick = next((d for d in same if d in indexed), same[0])
        flags = [f"Ambiguous company folders: {', '.join(same)}"] if len(same) > 1 else []
        return pick, False, pick not in indexed, key, flags
    flags = []
    near = max(((SequenceMatcher(None, key, folder_key(d)).ratio(), d) for d in dirs if folder_key(d)), default=None)
    if near and near[0] >= NEAR_MISS:
        flags.append(f"Possible existing folder: {near[1]} — merge by hand if same company")
    name = safe_name(company)
    if name.casefold() in {p.name.casefold() for p in entries}:
        raise ArchiveError(f"cannot create company folder '{name}': something with that name already exists in {root}")
    return name, True, False, key, flags


def _fit(title: str, suffix: str, cap: int) -> str:
    return safe_name(title, cap - len(suffix)) + suffix


def _role_name(title: str, taken: set[str], day: str, job_id: str, cap: int) -> str:
    """Plain title; on collision ' – <date>', then ' – <id6>', then ' – <id6>-2', '-3'... (case-insensitive)."""
    def candidates():
        yield _fit(title, "", cap)
        yield _fit(title, f" – {day}", cap)
        yield _fit(title, f" – {id6(job_id)}", cap)
        for i in count(2):
            yield _fit(title, f" – {id6(job_id)}-{i}", cap)
    return next(c for c in candidates() if c.casefold() not in taken)


def _sources(job: dict, rep: dict, no_letter: bool) -> tuple[list[Path], bool]:
    out_dir = Path(job["out_dir"] or "")
    cv = Path(rep.get("cv_pdf") or "")
    letter = Path(rep["letter_pdf"]) if rep.get("letter_pdf") else None
    has_letter = letter is not None and letter.is_file()
    # status none: approved with 'no letter' in review; status off: letter.mode is off, no letter was ever asked for
    no_letter = no_letter or (rep.get("letter") or {}).get("status") in ("none", "off")
    if not has_letter and not no_letter:
        raise LetterMissing(f"no cover letter PDF for {job['id']} ({letter or 'none recorded'}); "
                            f"regenerate it in review, or pass --no-letter to go without")
    check_job_files(job, cv, letter if has_letter else None)         # before anything else is trusted
    srcs = [cv, cv.with_suffix(".tex")] + ([letter, letter.with_suffix(".tex")] if has_letter else [])
    srcs.append(out_dir / "report.json")
    for p in srcs:
        if not p.is_file():
            raise ArchiveError(f"expected file is missing: {p}")
    return srcs, not has_letter


def preflight(db: DB, settings, job, rep: dict, *, applied_on: date | None = None, no_letter: bool = False) -> Plan:
    """Everything that can be checked without writing: archive root, letter, wrong-file guard, folder choice, path
    length. Raises ArchiveError (nothing has changed). Run before opening a form and again before archiving."""
    job = dict(job)
    root = settings.path("applications")
    if not root.is_dir():
        raise ArchiveRootMissing(f"archive root {root} does not exist — run `careerops init` to create it "
                                 f"(nothing was created or changed)")
    tmp = settings.path("tmp")
    if _inside(tmp, root) or _inside(root, tmp):
        raise ArchiveError(f"paths.tmp ({tmp}) and paths.applications ({root}) must not contain each other")
    sources, letter_missing = _sources(job, rep, no_letter)
    applied_at, day = _applied(applied_on)
    names = [p.name for p in sources] + ["JD.txt", "job.json"]
    longest = max(len(n) for n in names)

    own = _own_role(db, root, job["id"])
    if own:
        company, role_parent = own.split("/")[0], root / own
        company_new = adopt = False
        row = db.conn.execute("SELECT company_key FROM archive_index WHERE folder=?", (own.split("/")[0],)).fetchone()
        key, flags = (row[0] if row else None) or folder_key(company), []
        taken = {p.name.casefold() for p in role_parent.iterdir()}
        version = next(n for n in count(2) if f"v{n}".casefold() not in taken)
        role_dir, role_rel = role_parent / f"v{version}", f"{own}/v{version}"
    else:
        company, company_new, adopt, key, flags = _company(db, root, job["company"])
        company_dir = root / company
        taken = {p.name.casefold() for p in company_dir.iterdir()} if not company_new else set()
        fixed = len(str(root)) + 1 + len(company) + 1
        cap = min(80, MAX_PATH - fixed - len("/v99") - 1 - longest)
        if cap < MIN_ROLE_CHARS:
            raise ArchiveError(f"archive path would exceed {MAX_PATH} characters under {root}; "
                               f"shorten the company name or move the archive root")
        role = _role_name(job["title"], taken, day, job["id"], cap)
        version, role_dir, role_rel = 1, company_dir / role, f"{company}/{role}"
    if len(str(role_dir)) + 1 + longest > MAX_PATH:
        raise ArchiveError(f"archive path would exceed {MAX_PATH} characters: {role_dir}")
    return Plan(root=root, company=company, company_new=company_new, adopt=adopt, key=key, role_dir=role_dir,
                role_rel=role_rel, version=version, flags=flags, sources=sources, letter_missing=letter_missing,
                applied_at=applied_at, day=day)


def _inside(path: Path, folder: Path) -> bool:
    p, f = path.resolve(), folder.resolve()
    return p == f or f in p.parents


# ------------------------------------------------------------------ writing
def _job_meta(job: dict, rep: dict, plan: Plan, confirmation: str | None) -> dict:
    keys = ("id", "company", "title", "location", "url", "apply_url", "source", "family", "tier", "sponsor_name",
            "sponsor_score", "score", "score_stemmed", "ceiling", "ceiling_pre", "letter_score", "posted_at")
    meta = {("job_id" if k == "id" else k): job.get(k) for k in keys}
    meta.update(letter_status=(rep.get("letter") or {}).get("status"), applied_at=plan.applied_at,
                confirmation=confirmation, no_letter=plan.letter_missing, version=plan.version, archived_at=now())
    return meta


def _remove_staging(stage: Path, tmp_root: Path, archive_root: Path) -> None:
    """Delete one staging folder under paths.tmp. The only deletion in this module: it refuses anything that is
    not strictly inside paths.tmp, and anything at or inside the archive root."""
    s, t, a = stage.resolve(), tmp_root.resolve(), archive_root.resolve()
    if t not in s.parents or s == a or a in s.parents:
        raise ArchiveError(f"refusing to delete {stage}: not a staging folder under {tmp_root}")
    shutil.rmtree(s)


def _merge_flags(current: str | None, new: list[str]) -> list[str]:
    have = json.loads(current or "[]")
    return have + [f for f in new if f not in have]


def _stage(plan: Plan, job: dict, rep: dict, confirmation: str | None, tmp_root: Path) -> tuple[Path, list[str]]:
    """Copy everything to be archived into a fresh folder under paths.tmp and verify the copies."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="archive-", dir=tmp_root))
    try:
        for src in plan.sources:
            dst = stage / src.name
            shutil.copyfile(src, dst)
            if sha256_of(dst) != sha256_of(src):
                raise OSError(f"staged copy of {src} differs from the original")
        (stage / "JD.txt").write_bytes((job.get("description") or "").encode("utf-8"))
        (stage / "job.json").write_bytes(json.dumps(_job_meta(job, rep, plan, confirmation), indent=2,
                                                     default=str).encode("utf-8"))
    except Exception as e:
        _remove_staging(stage, tmp_root, plan.root)
        raise ArchiveError(f"could not stage files for archiving (nothing was written to the archive): {e}") from e
    order = [p.name for p in plan.sources if p.name != "report.json"] + ["JD.txt", "job.json", "report.json"]
    return stage, order


def record_application(db: DB, settings, job, rep: dict, *, confirmation: str | None = None,
                       applied_on: date | None = None, no_letter: bool = False) -> Archived:
    """Archive a submitted application and mark the job applied.

    Raises ArchiveError when it refused before changing anything. Raises ArchiveIncomplete when copying failed
    part-way: the job IS marked applied and flagged, and the partial folder is left for the user.
    """
    job = dict(job)
    plan = preflight(db, settings, job, rep, applied_on=applied_on, no_letter=no_letter)
    tmp_root = settings.path("tmp")
    stage, order = _stage(plan, job, rep, confirmation, tmp_root)
    new_rows: list[tuple[str, str, str | None]] = []     # (folder rel, kind, job_id) to index
    made_role = False
    failure: Exception | None = None
    try:
        if plan.company_new:
            safe_mkdir(plan.root / plan.company)
            new_rows.append((plan.company, "company", None))
        elif plan.adopt:
            new_rows.append((plan.company, "company", None))          # hand-made folder: recorded, not touched
        safe_mkdir(plan.role_dir)
        made_role = True
        new_rows.append((plan.role_rel, "role", job["id"]))
        for name in order:
            safe_copy(stage / name, plan.role_dir / name)
    except Exception as e:  # noqa: BLE001 — whatever went wrong, the partial folder stays and the job is flagged
        failure = e
    finally:
        _remove_staging(stage, tmp_root, plan.root)

    new_flags = list(plan.flags)
    if failure is not None:
        new_flags.append(f"Archive incomplete: {plan.role_rel} — finish by hand")
    fields: dict = {"status": "applied", "applied_at": plan.applied_at, "confirmation": confirmation,
                    "flags": _merge_flags(job.get("flags"), new_flags)}
    if made_role:
        fields["application_dir"] = str(plan.role_dir)
    detail = {"folder": plan.role_rel, "version": plan.version, "files": order if failure is None else [],
              "no_letter": plan.letter_missing, "flags": new_flags}
    if failure is not None:
        detail["error"] = str(failure)
    with db.conn:
        for rel, kind, jid in new_rows:
            db.conn.execute("INSERT OR REPLACE INTO archive_index VALUES (?,?,?,?,?)",
                            (rel, kind, plan.key, jid, now()))
        db.update_job(job["id"], commit=False, **fields)
        record_applied(db, job["id"], plan.day)
        db.conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                        (job["id"], now(), "archive", json.dumps(detail, default=str)))
    if failure is not None:
        raise ArchiveIncomplete(f"archive of {job['id']} is incomplete at {plan.role_rel}: {failure}",
                                plan.role_dir if made_role else None) from failure
    return Archived(folder=plan.role_dir, rel=plan.role_rel, version=plan.version, files=order, flags=new_flags)


def mark_applied_unarchived(db: DB, job, confirmation: str | None, reason: str,
                            applied_on: date | None = None) -> None:
    """The application WAS submitted but the archive step refused (nothing written there). Record the truth and
    flag the job so the user archives it by hand."""
    job = dict(job)
    applied_at, _ = _applied(applied_on)
    with db.conn:
        db.update_job(job["id"], commit=False, status="applied", applied_at=applied_at, confirmation=confirmation,
                      flags=_merge_flags(job.get("flags"), [f"Archive refused: {reason} — archive by hand"]))
        record_applied(db, job["id"], applied_at[:10])
        db.conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                        (job["id"], now(), "archive", json.dumps({"refused": reason})))
