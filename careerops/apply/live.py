"""`careerops live`: work through the JD files in data/jds/inbox/ one job at a time, with a human at every gate.

Per JD file: validate -> archive snapshot -> add-jd (internal function, the job id is a return value) -> triage ->
tailor that id -> review that id (approve / edit / skip) -> [not --dry-run] the apply assist, which never submits ->
"Did YOU submit the form?" -> mark-applied only on a `y` -> archive snapshot again and compare -> move the JD to done/.

Nothing here submits anything, answers a gate for the user, or writes to the archive. The archive is only read (a snapshot
is os.walk + lstat); the one permitted write to it, for a job the user says they submitted, is mark-applied's own
(`record_application`, injected as a hook). If the archive differs afterwards in any way other than that job's one new
<Company>/<Role> folder, the whole loop stops with exit code 3.

The heavy steps are hooks (tailor, review, preflight, assist, mark_applied) that each take a JOB ID, so the loop never has
to read another command's output, and tests can put fakes in their place.

State lives in paths.live/state.json, keyed by the sha256 of the JD file: a started job keeps its id (so a rerun resumes it
at whatever status it reached) and a finished one is skipped. A JD is moved to done/ only when its job is finished; a job
stopped by Ctrl-C, a dry run, a pending letter or an error stays in inbox/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..db import DB
from ..discovery import manual
from . import snapshot
from .errors import ArchiveError

EXIT_BUDGET = 1               # the day's LLM budget is spent: nothing half-done, run again later
EXIT_ARCHIVE = 3              # the archive changed in a way the pipeline never does
EXIT_INTERRUPTED = 130        # Ctrl-C (or an unanswered prompt)

FINISHED = ("applied", "applied (archive incomplete)", "ready_not_submitted", "skipped", "skipped (rules)", "rejected")
NOT_FINISHED = ("approved (dry run)", "letter_pending", "apply_refused", "error")
# statuses --retailor sends back to 'triaged': the earlier tailoring is replaced. Never an applied job.
RETAILOR_FROM = ("needs_review", "approved", "skipped", "ready_not_submitted", "letter_pending", "error")


class StopSession(Exception):
    def __init__(self, reason: str, code: int):
        super().__init__(reason)
        self.reason, self.code = reason, code


@dataclass
class Hooks:
    """The steps that cost money, open a browser, or ask the person. Each takes a job id."""
    ask: Callable[[str], str]
    tailor: Callable[[str], None]                       # tailor exactly this job (or finish its pending letter)
    review: Callable[[str], str]                        # 'approved' | 'skipped' | 'quit'
    preflight: Callable[[str], None]                    # raises ArchiveError before a form is opened
    assist: Callable[[str], dict]                       # {"confirmation": str, "visa": [{"question", "value"}]}; never submits
    mark_applied: Callable[[str, str | None], bool]     # archive + mark applied; False = archive incomplete


@dataclass
class JobResult:
    file: str
    job_id: str = "-"
    company: str = "-"
    role: str = "-"
    status: str = ""
    sponsor: str = "n/a"
    visa: list[str] = field(default_factory=list)
    archive: str = "n/a"
    note: str = ""


@dataclass
class Folders:
    inbox: Path
    done: Path
    rejected: Path
    live: Path


def folders(settings) -> Folders:
    base = settings.path("jds")
    f = Folders(base / "inbox", base / "done", base / "rejected", settings.path("live"))
    for p in (f.inbox, f.done, f.rejected, f.live):
        p.mkdir(parents=True, exist_ok=True)
    return f


def safe_part(text: str) -> str:
    """A job id as a file-name prefix: ':' and the like are not valid on the NTFS drive the repo lives on."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "job"


def _unique(directory: Path, name: str, extra: tuple[str, ...] = ()) -> str:
    """`name`, or `name` with -2, -3 ... before its suffix, so that it (and name+extra) is not yet in `directory`."""
    stem, suffix = os.path.splitext(name)
    n, candidate = 1, name
    while (directory / candidate).exists() or any((directory / (os.path.splitext(candidate)[0] + e)).exists() for e in extra):
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    return candidate


class State:
    """paths.live/state.json: {"jobs": {sha256: {file, job_id, company, title, url, location, done, status}}}."""

    def __init__(self, path: Path):
        self.path = path
        self.jobs: dict[str, dict] = {}
        if path.exists():
            try:
                self.jobs = json.loads(path.read_text(encoding="utf-8"))["jobs"]
            except (OSError, ValueError, KeyError) as e:
                raise StopSession(f"{path} is unreadable ({e}); fix or move it by hand, nothing was changed", 2) from None

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "jobs": self.jobs}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)                      # whole file or nothing: an interrupt never leaves half a state


class Live:
    def __init__(self, db: DB, settings, hooks: Hooks, *, max_jobs: int = 5, dry_run: bool = False,
                 resume_cmd: str = "careerops live", out: Callable[[str], None] = print, retailor: bool = False,
                 only: Path | None = None):
        self.db, self.s, self.h, self.retailor = db, settings, hooks, retailor
        self.only = only                                # `live --url`: work on this one inbox file, leave the rest
        self.max_jobs, self.dry_run, self.resume_cmd, self.out = max_jobs, dry_run, resume_cmd, out
        self.root = settings.path("applications")
        self.dirs = folders(settings)
        self.state: State | None = None
        self.results: list[JobResult] = []
        self.current: dict = {}
        self.before: snapshot.Snapshot | None = None
        self.dir_before: str | None = None

    # ------------------------------------------------------------------ files
    def _inside_archive(self, p: Path) -> bool:
        r, a = p.resolve(), self.root.resolve()
        return r == a or a in r.parents

    def _move(self, src: Path, dest_dir: Path, name: str, extra: tuple[str, ...] = ()) -> Path:
        """Move a JD file between the folders under paths.jds. Never into or out of the archive, never over a file."""
        dst = dest_dir / _unique(dest_dir, name, extra)
        if self._inside_archive(src) or self._inside_archive(dst):
            raise RuntimeError(f"refusing to move {src} -> {dst}: the application archive is never touched")
        if dst.exists():
            raise RuntimeError(f"refusing to overwrite {dst}")
        shutil.move(str(src), str(dst))
        return dst

    def _reject(self, f: Path, reason: str) -> str:
        """Move a bad JD to rejected/ with <name>.reason.txt beside it."""
        try:
            dst = self._move(f, self.dirs.rejected, f.name, extra=(".reason.txt",))
            dst.with_name(dst.stem + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
            return f"moved to rejected/{dst.name}"
        except (OSError, RuntimeError) as e:
            return f"could not be moved ({e})"

    def _finish_file(self, f: Path, sha: str, entry: dict, status: str) -> None:
        entry.update(done=True, status=status)
        self.state.jobs[sha] = entry
        self.state.save()
        if f.exists():
            self._move(f, self.dirs.done, f"{safe_part(entry.get('job_id') or 'nojob')}-{f.name}")

    # ------------------------------------------------------------------ the session
    def run(self) -> int:
        """Run the session; returns the exit code (0 done, 1 budget, 3 archive changed, 130 interrupted)."""
        code, stop = 0, None
        self.out(f"careerops live: inbox {self.dirs.inbox} (max {self.max_jobs}{', dry run' if self.dry_run else ''})")
        try:
            self.state = State(self.dirs.live / "state.json")
            self._loop()
        except StopSession as e:
            code, stop = e.code, e.reason
            if e.code == EXIT_ARCHIVE:
                self.out("Look at the archive by hand before running live again.")
            elif self.current:
                self._where_stopped()
        except (KeyboardInterrupt, EOFError):
            code, stop = EXIT_INTERRUPTED, "interrupted (Ctrl-C, or no answer at a prompt)"
            if not self._where_stopped():
                code = EXIT_ARCHIVE
        text = self.summary(stop)
        self.out(text)
        report = self.dirs.live / f"{datetime.now():%Y%m%d-%H%M%S}.md"
        report.write_text(text + "\n", encoding="utf-8")
        self.out(f"Summary written to {report}")
        return code

    def _loop(self) -> None:
        files = ([self.only] if self.only is not None else
                 sorted((p for p in self.dirs.inbox.iterdir() if p.is_file() and p.suffix.lower() == ".txt"),
                        key=lambda p: p.name))
        if not files:
            self.out(f"Nothing in {self.dirs.inbox}: put one JD per .txt file there (optional first lines "
                     "`Company: ...`, `Title: ...`, `URL: ...`).")
        counted = 0
        for f in files:
            if counted >= self.max_jobs:
                self.out(f"--max {self.max_jobs} reached; {len(files) - files.index(f)} file(s) left in the inbox.")
                break
            r = self._one(f)
            if r is not None:
                counted += 1

    def _one(self, f: Path) -> JobResult | None:
        """Process one file. Returns None when it was rejected or already done (it does not count towards --max)."""
        self.current = {"file": f.name, "job_id": None, "stage": "validate"}
        self.before = None
        try:
            text, sha = manual.read_jd(f)
        except manual.JDError as e:
            where = self._reject(f, str(e))
            self.out(f"REJECTED {e}  ({where})")
            self.results.append(JobResult(f.name, status="rejected (invalid file)", note=str(e)))
            return None
        entry = self.state.jobs.get(sha) or {"file": f.name, "job_id": None, "done": False}
        if entry.get("done"):
            if not self.dry_run:
                self._finish_file(f, sha, entry, entry.get("status", "done"))
            self.out(f"SKIP {f.name}: already done as job {entry.get('job_id')} ({entry.get('status')})")
            self.results.append(JobResult(f.name, entry.get("job_id") or "-", entry.get("company", "-"),
                                          entry.get("title", "-"), f"already done ({entry.get('status')})"))
            return None

        r = JobResult(f.name)
        self.results.append(r)
        self.before = snapshot.take(self.root)                                     # 1. archive snapshot
        self.dir_before = None
        try:
            status = self._advance(f, text, sha, entry, r)
        except StopSession:
            self._fill(r)
            raise
        r.status = status
        self._fill(r)
        check = self._archive_check(r)                                             # 6. second snapshot, compare
        if not check.ok:
            for line in check.lines():
                self.out("  " + line)
            raise StopSession(f"the application archive changed outside this job's one new folder "
                              f"(job {r.job_id}); nothing else was processed", EXIT_ARCHIVE)
        if status == "rejected (JD)":                                              # its file was moved already
            return None
        if status in FINISHED and not self.dry_run:                                # 7. the JD moves to done/ (never in a dry run)
            self._finish_file(f, sha, entry, status)
        return r

    def _fill(self, r: JobResult) -> None:
        job = self.db.job(self.current.get("job_id")) if self.current.get("job_id") else None
        if job is not None:
            r.job_id, r.company, r.role = job["id"], job["company"], job["title"]
            r.sponsor = ((job["sponsor_match"] or "unchecked") +
                         (f" ({job['sponsor_name']})" if job["sponsor_name"] and job["sponsor_match"] != "none" else ""))
            if not r.status:
                r.status = job["status"]

    def _archive_check(self, r: JobResult) -> snapshot.Check:
        after = snapshot.take(self.root)
        job = self.db.job(r.job_id) if r.job_id != "-" else None
        now_dir = job["application_dir"] if job is not None else None
        allowed = snapshot.role_folder(self.root, now_dir) if now_dir and now_dir != self.dir_before else None
        check = snapshot.compare(self.before or {}, after, allowed)
        r.archive = check.summary()
        return check

    def _where_stopped(self) -> bool:
        """Say where the session stopped and how to resume; check the archive once more. False = the archive changed."""
        cur, jid = self.current, self.current.get("job_id")
        job = self.db.job(jid) if jid else None
        ok = True
        r = self.results[-1] if self.results else None
        if r is not None and r.file == cur.get("file"):
            if not r.status:
                r.status = f"stopped at {cur.get('stage')}"
            self._fill(r)
            if self.before is not None and r.archive == "n/a":
                check = self._archive_check(r)
                ok = check.ok
                for line in check.lines():
                    self.out("  " + line)
        self.out(f"\nSTOPPED at stage '{cur.get('stage')}' of {cur.get('file')}"
                 + (f" (job {jid}, status '{job['status']}')" if job is not None else " (no job yet)") + ".")
        self.out("The JD is still in inbox/ and the job keeps the status it reached; nothing was submitted by this command.")
        if cur.get("stage") in ("assist", "confirm-submitted", "mark-applied") and jid:
            self.out(f"If you already pressed Submit in the browser, run: careerops mark-applied --id {jid}")
        self.out(f"Resume with:  {self.resume_cmd}")
        return ok

    # ------------------------------------------------------------------ one job
    def _stage(self, name: str) -> None:
        self.current["stage"] = name

    def _ask_valid(self, prompt: str, field: str) -> str:
        """Ask until the answer passes `manual.meta_problem` (a blank answer ends it: ''). A stray letter or a template
        placeholder is not taken for a company, title or URL; three bad answers in a row are treated as blank."""
        for _ in range(3):
            answer = self.h.ask(prompt).strip()
            problem = manual.meta_problem(field, answer) if answer else None
            if not problem:
                return answer
            self.out(f"  {problem}; try again, or leave it blank.")
        return ""

    def _meta(self, f: Path, text: str) -> tuple[dict | None, str]:
        """(company / title / url / location from the file's header lines, asking for what is missing; why not). A header
        that holds `<` or `>` (an unfilled placeholder) or a company / title under 2 characters makes the file invalid
        (meta=None); so does a blank answer."""
        hdr = manual.header_fields(text)
        for key, value in hdr.items():
            problem = manual.meta_problem(key, value)
            if problem:
                return None, f"header {problem}"
        company = hdr.get("company") or self._ask_valid(f"{f.name}: company? (blank = reject this file) ", "company")
        title = hdr.get("title") or (self._ask_valid(f"{f.name}: job title? (blank = reject this file) ", "title")
                                     if company else "")
        if not (company and title):
            return None, "no company or title given"
        url = hdr.get("url")
        if url is None:
            url = self._ask_valid(f"{f.name}: application URL? (blank = none; you would apply by hand) ", "url") or None
        return {"company": company, "title": title, "url": url, "location": hdr.get("location") or "United Kingdom"}, ""

    def _advance(self, f: Path, text: str, sha: str, entry: dict, r: JobResult) -> str:
        db = self.db
        self._stage("add-jd")
        if not (entry.get("job_id") and db.job(entry["job_id"])):
            meta, reason = self._meta(f, text)
            if meta is None:
                self.out(f"REJECTED {f.name}: {reason}  ({self._reject(f, reason)})")
                r.note = reason
                return "rejected (JD)"
            try:
                jid = manual.add_jd(db, text, **meta)                               # 2. add-jd: the id is the return value
            except (manual.JobExists, manual.JDError) as e:
                self.out(f"REJECTED {f.name}: {e}  ({self._reject(f, str(e))})")
                r.note = str(e)
                return "rejected (JD)"
            entry.update(job_id=jid, company=meta["company"], title=meta["title"], **{k: meta[k] for k in ("url", "location")})
            self.state.jobs[sha] = entry
            self.state.save()
            db.log(jid, "live", {"added_from": f.name})
        jid = entry["job_id"]
        self.current["job_id"] = jid
        self.dir_before = db.job(jid)["application_dir"]
        self.out(f"\n== {entry.get('company')} | {entry.get('title')}  [{jid}]  ({f.name})")
        if self.retailor and db.job(jid)["status"] in RETAILOR_FROM:
            self.out(f"  --retailor: '{db.job(jid)['status']}' -> 'triaged'; the earlier tailoring of this job is replaced.")
            db.update_job(jid, status="triaged")
            db.log(jid, "live", "retailor: sent back to triaged")

        for _ in range(12):                                                         # a status machine, never an endless loop
            job = db.job(jid)
            st = job["status"]
            if st == "new":
                self._stage("triage")
                manual.triage_job(db, self.s, jid)
            elif st == "needs_jd":
                reason = "JD looks truncated (under 800 characters): paste the full text and drop it in again"
                self.out(f"REJECTED {f.name}: {reason}  ({self._reject(f, reason)})")
                r.note = reason
                return "rejected (JD)"
            elif st == "skipped" and not job["out_dir"]:
                self.out(f"  The rules skipped this job: {job['reason']}")
                if self.h.ask("  Tailor it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                    return "skipped (rules)"
                db.update_job(jid, status="triaged")
                db.log(jid, "live", "triage skip overridden by the user")
            elif st == "error":
                self.out(f"  Earlier tailoring failed ({job['reason']}); trying again.")
                db.update_job(jid, status="triaged")
            elif st in ("triaged", "letter_pending"):
                self._stage("tailor")
                self.h.tailor(jid)                                                  # 3. tailor exactly this id
                after = db.job(jid)["status"]
                if after == "letter_pending":
                    self.out("  The CV is done but the letter is pending (its provider has no budget now); "
                             "run live again later.")
                    return "letter_pending"
                if after == st:
                    raise StopSession("tailoring did not run (daily LLM token budget spent?); the job stays 'triaged', "
                                      "run live again after the budget resets", EXIT_BUDGET)
                if after == "error":
                    return "error"
            elif st == "needs_review":
                self._stage("review")
                choice = self.h.review(jid)                                         # 4. review exactly this id
                if choice == "quit":
                    raise StopSession("you chose quit at the review prompt; the job stays 'needs_review'", 0)
                if choice == "skipped":
                    db.update_job(jid, status="skipped", reason="skipped in live review")
                    db.log(jid, "live", "skipped in review")
                    return "skipped"
            elif st == "approved":
                if self.dry_run:
                    self.out("  --dry-run: approved, not applied. Run live without --dry-run to apply; this JD stays in inbox/.")
                    return "approved (dry run)"
                return self._apply(jid, job, r)
            else:                                                                   # applied, rejected, ready_not_submitted, ...
                return st
        return db.job(jid)["status"]

    def _apply(self, jid: str, job, r: JobResult) -> str:
        db = self.db
        self._stage("preflight")
        try:
            self.h.preflight(jid)
        except ArchiveError as e:
            self.out(f"  REFUSED {jid}: {e}")
            r.note = str(e)
            return "apply_refused"
        if job["apply_url"] or job["url"]:
            if self.h.ask("  Open the application form? [y/N] ").strip().lower() not in ("y", "yes"):
                return self._not_submitted(jid, "form not opened")
            self._stage("assist")
            res = self.h.assist(jid)                                                # 5. the existing assist; it never submits
            r.visa = [f"CHECK VISA ANSWER: {v['question'][:150]} -> {v.get('value') or 'EMPTY'}" for v in res.get("visa") or []]
        else:
            self.out("  This job has no application URL: apply by hand.")
            res = {"confirmation": "", "visa": []}
        for line in r.visa or ["(no visa question was found on the form)"]:
            self.out("  " + line)
        confirmation = (res.get("confirmation") or "").strip()
        self._stage("confirm-submitted")
        submitted = confirmation.lower() != "x" and self.h.ask("  Did YOU submit the form? [y/N] ").strip().lower() in ("y", "yes")
        if not submitted:
            return self._not_submitted(jid, "you did not submit")
        self._stage("mark-applied")
        ok = self.h.mark_applied(jid, confirmation or None)
        return "applied" if ok else "applied (archive incomplete)"

    def _not_submitted(self, jid: str, why: str) -> str:
        self.db.update_job(jid, status="ready_not_submitted")
        self.db.log(jid, "live", {"ready_not_submitted": why})
        self.out(f"  Not applied ({why}): status 'ready_not_submitted'. Nothing was recorded as sent.")
        return "ready_not_submitted"

    # ------------------------------------------------------------------ summary
    def summary(self, stop: str | None) -> str:
        lines = [f"# careerops live — {datetime.now():%Y-%m-%d %H:%M}" + (" (dry run)" if self.dry_run else ""), ""]
        if not self.results:
            lines.append("No files were processed.")
        for r in self.results:
            lines += [f"## {r.file}", f"- id: {r.job_id}", f"- company: {r.company}", f"- role: {r.role}",
                      f"- status: {r.status}", f"- sponsor register: {r.sponsor}", f"- archive check: {r.archive}"]
            if r.note:
                lines.append(f"- note: {r.note}")
            lines += ["- visa lines:"] + [f"    {v}" for v in r.visa] if r.visa else ["- visa lines: none"]
            lines.append("")
        if stop:
            lines.append(f"Stopped: {stop}")
        return "\n".join(lines)
