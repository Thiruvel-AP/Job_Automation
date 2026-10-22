"""`careerops live`: work through the JD files in data/jds/inbox/ one job at a time, with a human at every gate.

Per JD file: validate -> archive snapshot -> add-jd (internal function, the job id is a return value) -> triage ->
tailor that id -> review that id (approve / edit / skip) -> [not --dry-run] the apply assist, which never submits ->
"Did YOU submit the form?" -> mark-applied only on a `y` -> archive snapshot again and compare -> move the JD to done/.

With `--auto` (hooks `auto_gate` and `auto_apply`) a tailored job meets the gate first (apply/autogate.py). Every gate passed: the form is
filled and, unless `--no-submit`, submitted by autoapply.py, and mark-applied runs on the confirmation. Any gate failed: `AUTO BLOCKED: ...`
is printed and the job goes to the review prompt and the assist exactly as without `--auto`. A CAPTCHA or a question nobody answered makes the
job `needs_user`; a click without a confirmation page makes it `submit_unconfirmed` (never retried, never marked applied).

A file marked as outside the UK (a `Country:` header that is not the UK, or `Status: listed_only`, which `careerops discover` writes for
a job in another country) is not worked on: it stays in inbox/, is not counted towards --max and is listed in the summary as
`listed_only`, unless applicant.yaml has a `visa.by_country.<country>` block for that country (you add those yourself).

Nothing in this module submits anything (only autoapply.py clicks a submit control, through the `auto_apply` hook), answers a review gate
for the user, or writes to the archive. The archive is only read (a snapshot is os.walk + lstat); the one permitted write to it, for a job
submitted by the user or by the gated auto-apply, is mark-applied's own (`record_application`, injected as a hook). If the archive differs
afterwards in any way other than that job's one new <Company>/<Role> folder, the whole loop stops with exit code 3.

The heavy steps are hooks (tailor, review, preflight, assist, mark_applied) that each take a JOB ID, so the loop never has
to read another command's output, and tests can put fakes in their place.

State lives in paths.live/state.json, keyed by the sha256 of the JD file: a started job keeps its id (so a rerun resumes it
at whatever status it reached) and a finished one is skipped. The moment the apply page is opened the entry gets `apply_opened`; it is
cleared when "Did YOU submit?" has been answered. If the session died in between (a closed browser, a crash), the next session starts by
asking "Did you submit <company> <role>? [y/N]" for that job, and a `y` marks it applied (archive and tracker included). A JD is moved only when its job is finished; a job
stopped by Ctrl-C, a dry run, a pending letter or an error stays in inbox/. A skipped or applied job is final (apply/decided.py): its JD, and
the JD of every copy of the job in the inbox, goes to paths.jds_done/skipped/ or paths.jds_done/applied/; any other finished job goes to done/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .. import bands
from ..db import DB
from ..discovery import applykind, countries, freshness, manual
from . import autolog, decided, snapshot
from .errors import ArchiveError

if TYPE_CHECKING:
    from ..autoapply import AutoResult
    from .autogate import Gates

EXIT_BUDGET = 1               # the day's LLM budget is spent: nothing half-done, run again later
EXIT_ARCHIVE = 3              # the archive changed in a way the pipeline never does
EXIT_INTERRUPTED = 130        # Ctrl-C (or an unanswered prompt)

HANDED_OFF = "handed_off"       # the form went to the person's own browser (apply/handoff.py); mark-applied records it once they submit
FINISHED = ("applied", "applied (archive incomplete)", "ready_not_submitted", "skipped", "skipped (rules)", bands.ATS_SKIP, "rejected",
            "submit_unconfirmed", HANDED_OFF)
NOT_FINISHED = ("approved (dry run)", "letter_pending", "apply_refused", "error", "needs_user", "auto_filled (not submitted)",
                "auto_ready (dry run)")
# statuses --retailor sends back to 'triaged': the earlier tailoring is replaced. Never an applied job.
AWAITING = ("needs_review", "approved")             # tailored: what pass 2 of the session (review, then apply) works on
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
    auto_gate: "Callable[[str], Gates] | None" = None   # `--auto`: the gate for this job (apply/autogate.py); None = no auto-apply in this session
    auto_apply: "Callable[[str], AutoResult] | None" = None    # `--auto`: fill the form and, unless --no-submit, submit it (autoapply.py)


@dataclass
class JobResult:
    file: str
    job_id: str = "-"
    company: str = "-"
    role: str = "-"
    status: str = ""
    sponsor: str = "n/a"
    pass1_done: bool = False            # tailored in pass 1 of the session (a --retailor must not send it back to triaged again)
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
                 only: Path | None = None, fresh: bool = False):
        self.db, self.s, self.h, self.retailor = db, settings, hooks, retailor
        self.fresh = fresh                              # `live --fresh`: tier 0 only (posted at most 24 h ago); the rest stays in the inbox
        self.only = only                                # `live --url`: work on this one inbox file, leave the rest
        self.max_jobs, self.dry_run, self.resume_cmd, self.out = max_jobs, dry_run, resume_cmd, out
        self.root = settings.path("applications_auto")      # the one folder this pipeline writes its archive to
        self.own_root = settings.path("applications")       # the user's own folder: read-only, snapshotted and checked unchanged
        self.dirs = folders(settings)
        self.state: State | None = None
        self.entry: dict = {}                             # the state entry of the job in hand (its `apply_opened` flag lives there)
        self.results: list[JobResult] = []
        self.current: dict = {}
        self.before: snapshot.Snapshot | None = None
        self.before_own: snapshot.Snapshot | None = None
        self.dir_before: str | None = None
        self._auto_tried: set[str] = set()                # a job meets the auto gate once per session; a decline falls through to the review prompt
        self.ats_skipped: list[str] = []                  # one `SKIPPED (ATS 32 < 50): Company | Role` line per job under the ATS band

    # ------------------------------------------------------------------ files
    def _inside_archive(self, p: Path) -> bool:
        r = p.resolve()
        return any(r == a or a in r.parents for a in (self.root.resolve(), self.own_root.resolve()))

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
        if status == bands.ATS_SKIP:
            self._file_ats_skip(f, entry)
            return
        which = decided.STATUS_DECISION.get(status)
        if which and entry.get("job_id") and self.db.job(entry["job_id"]) is not None:
            # a skip or an application is final and covers every copy of the job: their JD files go to paths.jds_done/<decision>/
            try:
                for line in decided.settle(self.db, self.s, entry["job_id"], which, also=f).lines():
                    self.out("  " + line)
            except decided.DecidedError as e:
                self.out(f"  could not move the JD of {entry['job_id']} out of the inbox: {e}")
            return
        if f.exists():
            self._move(f, self.dirs.done, f"{safe_part(entry.get('job_id') or 'nojob')}-{f.name}")

    def _file_ats_skip(self, f: Path, entry: dict) -> None:
        """A job under the ATS band: its JD goes to jds_done/skipped/ with `<name>.reason.txt` ("ATS S < 50") beside it. Only this file moves:
        `careerops retriage --ats` brings it back if the fact bank grows."""
        if not f.exists():
            return
        job = self.db.job(entry["job_id"]) if entry.get("job_id") else None
        try:
            dst = decided.move_file(self.s, f, decided.decision_dir(self.s, decided.SKIPPED))
            dst.with_name(dst.stem + ".reason.txt").write_text(((job["reason"] if job is not None else "") or "ATS S below the review band") + "\n",
                                                               encoding="utf-8")
            self.out(f"  JD filed in jds_done/skipped/{dst.name} (reason: {dst.stem}.reason.txt)")
        except (OSError, decided.DecidedError) as e:
            self.out(f"  could not file the JD of {entry.get('job_id')}: {e}")

    def _ats_skip(self, jid: str) -> str:
        """The line for a job whose strict CV score and ceiling are under the ATS bands. No letter, no form, no prompt."""
        job = self.db.job(jid)
        line = bands.skip_line(100 * (job["score"] or 0), bands.config(self.s).review, job["company"], job["title"])
        if line not in self.ats_skipped:
            self.ats_skipped.append(line)
        self.out(line)
        return bands.ATS_SKIP

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

    def _by_priority(self, files: list[Path]) -> list[Path]:
        """The inbox files best first: freshness tier, then priority P = F x S (discovery/priority.py), ties by name. Sponsorship only
        weights the order; no file is left out (`--fresh` leaves the files older than 24 h in the inbox, untouched). If the ranking
        itself fails the files go by name, as before (and `--fresh` then cannot tell the fresh ones: it says so and takes none)."""
        from ..config import load_yaml
        from ..discovery import priority as prio

        files = sorted(files, key=lambda p: p.name)
        if len(files) < 2 and not self.fresh:
            return files
        try:
            ranked = prio.order_files(files, self.s, self.db, load_yaml(self.s.path("fact_bank")))
        except Exception as e:  # noqa: BLE001
            self.out(f"(inbox order by priority not available: {e}; going by file name)")
            return [] if self.fresh else files
        self.out("Inbox order by freshness tier, then the ESTIMATE of P = F x S x L x R (before tailoring; the review order uses the final P):")
        for f, pr in ranked:
            self.out(f"  T{pr.tier} {pr.age():>7}  estimate P {pr.p:.2f} = F {pr.f:.2f} x S {pr.s:.2f} x L {pr.l:.2f}"
                     f"{' ' + pr.role_text() if pr.r is not None else ''}"
                     f"{'  ' + pr.flag() if pr.flag() else ''}  {f.name}" if pr else f"     -     {f.name} (unreadable)")
        if self.fresh:
            keep = [f for f, pr in ranked if pr is not None and pr.tier == 0]
            self.out(f"--fresh: {len(keep)} file(s) posted at most 24 h ago (tier 0); {len(ranked) - len(keep)} older file(s) stay in the inbox.")
            return keep
        return [f for f, _ in ranked]

    def _review_order(self, pending: list[tuple[Path, JobResult]]) -> list[tuple[Path, JobResult]]:
        """Pass 2's order: the tailored jobs by freshness tier, then the FINAL P = F x S x L (F is the ceiling tailoring found), ties to the
        surer employer. Prints the table before the first prompt. Nothing is dropped: the same jobs come back, in another order."""
        from ..discovery import priority as prio

        if not pending:
            return []
        rows = []
        for f, r in pending:
            job = self.db.job(r.job_id)
            rows.append((prio.sort_key(job, self.s, tailored=True), f, r, job, prio.priority(job, self.s, tailored=True)))
        rows.sort(key=lambda t: t[0][:3])                       # tier, -P, -S; a tie keeps the order of pass 1 (tier, estimate, name)
        self.out("Review queue by freshness tier, then final P (P = F x S x L x R; F is the ATS ceiling after tailoring, not the estimate):")
        self.out("     company | role | age | tier | level | ATS ceiling | P | role factor")
        for n, (_, f, r, job, pr) in enumerate(rows, 1):
            self.out(f"  {n}. {job['company']} | {job['title']} | {pr.age()} | T{pr.tier} | {pr.flag() or '-'} | "
                     f"ATS ceiling {pr.f:.2f} | P {pr.p:.2f} | {pr.role_text() or 'no role factor'}")
            self.out("     " + applykind.line(job["apply_url"] or job["url"], job["apply_kind"]))
        return [(f, r) for _, f, r, _, _ in rows]

    def _opened(self, on: bool) -> None:
        """Remember in the state file that the apply page of the job in hand is (not any more) open."""
        if on:
            self.entry["apply_opened"] = datetime.now().isoformat(timespec="seconds")
        else:
            self.entry.pop("apply_opened", None)
        self.state.save()

    def _ask_unconfirmed(self) -> None:
        """First thing in a session: a job whose apply page an earlier session opened and never got an answer for. Asks "Did you submit ...?";
        `y` marks it applied. Nothing is asked in a dry run (it cannot have been the one that opened a form)."""
        if self.dry_run or self.state is None:
            return
        for sha, entry in list(self.state.jobs.items()):
            if not entry.get("apply_opened") or entry.get("done"):
                continue
            job = self.db.job(entry.get("job_id")) if entry.get("job_id") else None
            entry.pop("apply_opened")
            if job is None or job["status"] in FINISHED:
                self.state.save()
                continue
            self.current = {"file": entry.get("file"), "job_id": job["id"], "stage": "confirm-submitted"}
            self.out(f"\n{job['company']} | {job['title']} [{job['id']}]: the last session opened its application form and ended before "
                     "you answered whether you submitted it.")
            r = JobResult(entry.get("file") or "-", job["id"], job["company"], job["title"])
            self.results.append(r)
            self.state.save()                                                      # asked once: a Ctrl-C here does not ask again next time
            if self.h.ask(f"  Did you submit {job['company']} {job['title']}? [y/N] ").strip().lower() not in ("y", "yes"):
                self.out("  Noted: not submitted. The job keeps its status.")
                r.status = job["status"]
                continue
            self._stage("mark-applied")
            ok = self.h.mark_applied(job["id"], None)
            r.status = "applied" if ok else "applied (archive incomplete)"
            self._finish_file(self.dirs.inbox / (entry.get("file") or ""), sha, entry, r.status)

    def _loop(self) -> None:
        self._ask_unconfirmed()
        files = ([self.only] if self.only is not None else self._by_priority(
                 [p for p in self.dirs.inbox.iterdir() if p.is_file() and p.suffix.lower() == ".txt"]))
        if self.fresh and not files and self.only is None:
            self.out("Nothing posted in the last 24 hours is waiting in the inbox: run `careerops discover` first, or drop --fresh.")
        if not files:
            self.out(f"Nothing in {self.dirs.inbox}: put one JD per .txt file there (optional first lines "
                     "`Company: ...`, `Title: ...`, `URL: ...`).")
        # Pass 1: the selected files (tier, then the estimate, at most --max) are added, triaged and tailored; nothing is asked yet but the
        # rule-skip question. Pass 2: the tailored jobs are reviewed (and applied to) best FINAL P first. The estimate of P cannot tell which
        # tailoring will be good (an estimate of 0.86 became 0.25), the final one can.
        counted, pending, deferred = 0, [], None
        for f in files:
            if self.only is None and not f.exists():                 # a copy of a job decided earlier in this session was filed away with it
                continue
            if counted >= self.max_jobs:
                self.out(f"--max {self.max_jobs} reached; {len(files) - files.index(f)} file(s) left in the inbox.")
                break
            try:
                r = self._one(f, stop_before_review=True)
            except StopSession as e:
                if e.code != EXIT_BUDGET or not pending:
                    raise
                deferred = (e, dict(self.current))             # budget spent: review what is tailored, then stop
                self.out(f"  {e.reason}")
                break
            if r is not None:
                counted += 1
                if r.status in AWAITING:
                    pending.append((f, r))
        for f, r in self._review_order(pending):
            job = self.db.job(r.job_id)
            if not f.exists() and job is not None and job["status"] in (decided.SKIPPED, decided.APPLIED):
                r.status, r.note = job["status"], job["reason"] or "the same job was decided earlier in this session"
                self.out(f"\n== {r.company} | {r.role}  [{r.job_id}]: {r.note}; its JD was filed with the other copy")
                continue
            self._one(f, r=r)
        if deferred:
            self.current = deferred[1]
            raise deferred[0]

    def _one(self, f: Path, *, stop_before_review: bool = False, r: JobResult | None = None) -> JobResult | None:
        """Process one file. Returns None when it was rejected or already done (it does not count towards --max).
        `stop_before_review` is pass 1 of the session: add, triage and tailor, then stop with the job awaiting its review (status
        'needs_review', or 'approved' from an earlier dry run); `r` is the result that pass returned, which pass 2 carries on with."""
        self.current = {"file": f.name, "job_id": None, "stage": "validate"}
        self.before = self.before_own = None
        try:
            text, sha = manual.read_jd(f)
        except manual.JDError as e:
            where = self._reject(f, str(e))
            self.out(f"REJECTED {e}  ({where})")
            self.results.append(JobResult(f.name, status="rejected (invalid file)", note=str(e)))
            return None
        entry = self.state.jobs.get(sha) or {"file": f.name, "job_id": None, "done": False}
        self.entry = entry
        if entry.get("done"):
            if not self.dry_run:
                self._finish_file(f, sha, entry, entry.get("status", "done"))
            self.out(f"SKIP {f.name}: already done as job {entry.get('job_id')} ({entry.get('status')})")
            self.results.append(JobResult(f.name, entry.get("job_id") or "-", entry.get("company", "-"),
                                          entry.get("title", "-"), f"already done ({entry.get('status')})"))
            return None

        twin = manual.header_fields(text).get("duplicate_of")
        if twin:                                                                   # discovery found a newer copy of this job on another board
            reason = f"duplicate of {twin}: the same job on another board of the employer, posted later; work on that file"
            self.out(f"DUPLICATE {f.name}: {reason}  ({self._reject(f, reason)})")
            self.results.append(JobResult(f.name, status="rejected (duplicate)", note=reason))
            return None

        refusal = self._outside_uk(text)
        if refusal:                                                                # listed only: no tailoring, no apply, no archive
            hdr = manual.header_fields(text)
            self.out(f"LISTED ONLY {f.name}: {refusal}")
            self.results.append(JobResult(f.name, company=hdr.get("company", "-"), role=hdr.get("title", "-"), status="listed_only",
                                          note=refusal))
            return None

        if r is None:
            r = JobResult(f.name)
            self.results.append(r)
        else:
            r.archive = "n/a"                                                      # pass 2 is checked again from its own snapshot
        self.before = snapshot.take(self.root)                                     # 1. archive snapshots (ours, and yours)
        self.before_own = snapshot.take(self.own_root)
        self.dir_before = None
        try:
            status = self._advance(f, text, sha, entry, r, stop_before_review)
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

    def _outside_uk(self, text: str) -> str | None:
        """Why this JD must not be worked on: it is for a job outside the UK (`Country:` header) or marked `Status: listed_only`, and
        applicant.yaml has no `visa.by_country.<country>` block for that country. None when it may go on (a UK job, or a country you gave a block)."""
        from ..config import load_yaml

        hdr = manual.header_fields(text)
        said = (hdr.get("country") or "").strip()
        country = countries.canon(said)
        if country == countries.UK or not (said or (hdr.get("status") or "").strip().lower() == "listed_only"):
            return None
        try:
            applicant = load_yaml(self.s.path("applicant")) or {}
        except (OSError, ValueError):
            applicant = {}
        if country and countries.visa_block(applicant, country):
            return None
        where = country or said or "no country given"
        return (f"outside the UK ({where}): listed only. It is not tailored or applied to unless applicant.yaml has a "
                f"`visa.by_country` block for {country or 'its country'}; the file stays in inbox/")

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
        mine = snapshot.compare(self.before_own or {}, snapshot.take(self.own_root))           # your folder: no addition at all is allowed
        check.removed += [f"{self.own_root.name}/{p}" for p in mine.removed]
        check.modified += [f"{self.own_root.name}/{p}" for p in mine.modified]
        check.stray += [f"{self.own_root.name}/{p}" for p in mine.stray]
        r.archive = check.summary()
        return check

    def _where_stopped(self) -> bool:
        """Say where the session stopped and how to resume; check the archive once more. False = the archive changed."""
        cur, jid = self.current, self.current.get("job_id")
        job = self.db.job(jid) if jid else None
        ok = True
        r = next((x for x in reversed(self.results) if x.file == cur.get("file")), None)      # pass 2 revisits earlier results
        if r is not None:
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
        if cur.get("stage") == "auto-fill":
            self.out("The JD is still in inbox/. The form may already have been submitted automatically: look at the last rows of "
                     f"{autolog.path(self.s)} and at the confirmation e-mail before anything else.")
        else:
            self.out("The JD is still in inbox/ and the job keeps the status it reached; nothing was submitted by this command.")
        if cur.get("stage") in ("assist", "confirm-submitted", "mark-applied") and jid:
            self.out(f"If you already pressed Submit in the browser, run: careerops mark-applied --id {jid}")
        elif cur.get("stage") == "auto-fill" and jid:
            self.out(f"If the form went through, run: careerops mark-applied --id {jid}")
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
        meta = {"company": company, "title": title, "url": url, "location": hdr.get("location") or "United Kingdom"}
        if re.fullmatch(r"[A-Za-z][\w.-]{0,30}", hdr.get("source") or ""):
            meta["source"] = hdr["source"].lower()
        if freshness.is_iso(hdr.get("posted")):
            meta["posted"] = hdr["posted"]
            if (hdr.get("posted_source") or "") in freshness.TRUSTED:
                meta["posted_source"] = hdr["posted_source"]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", hdr.get("first_seen") or ""):
            meta["seen_on"] = hdr["first_seen"]
        if hdr.get("apply_url"):
            meta["apply_url"] = hdr["apply_url"]
        if (hdr.get("apply_kind") or "").lower() in applykind.KINDS:
            meta["apply_kind"] = hdr["apply_kind"].lower()
        if (hdr.get("listed") or "").lower() in ("yes", "true", "1"):
            meta["open_listing"] = True
        if (hdr.get("agency") or "").lower() in ("yes", "true", "1"):
            meta["agency"] = True
        if countries.place_code(hdr.get("country")):
            meta["work_country"] = countries.place_code(hdr["country"])     # a `Country:` header is W, as you gave it
        return meta, ""

    def _advance(self, f: Path, text: str, sha: str, entry: dict, r: JobResult, stop_before_review: bool = False) -> str:
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
        if self.retailor and not r.pass1_done and db.job(jid)["status"] in RETAILOR_FROM:     # once: not again at the review pass
            self.out(f"  --retailor: '{db.job(jid)['status']}' -> 'triaged'; the earlier tailoring of this job is replaced.")
            db.update_job(jid, status="triaged")
            db.log(jid, "live", "retailor: sent back to triaged")

        for _ in range(12):                                                         # a status machine, never an endless loop
            job = db.job(jid)
            st = job["status"]
            if st == "new":
                self._stage("triage")
                manual.triage_job(db, self.s, jid)
            elif st == "needs_country":
                self.out(f"  Work country unknown ({job['reason']}). Not tailored, not applied to. Add a line `Country: XX` (a code "
                         f"or name, e.g. Country: DE) to {f.name} and run live again, or: careerops set-country --id {jid} --country XX")
                r.note = job["reason"] or "work country unknown"
                return "needs_country"
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
            elif st in AWAITING and stop_before_review:
                r.pass1_done = True
                return st                                                           # pass 2 reviews it, in the order of the final P
            elif st == bands.ATS_SKIP:
                return self._ats_skip(jid)
            elif st == "needs_user":
                self.out(f"  This job needs you: {job['reason']}. Finish the form by hand (careerops apply --id {jid}); once submitted: "
                         f"careerops mark-applied --id {jid}")
                return "needs_user"
            elif st == "needs_review":
                if self.h.auto_gate is not None and jid not in self._auto_tried:
                    self._auto_tried.add(jid)
                    done = self._try_auto(jid, r)
                    if done:
                        return done
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

    def _try_auto(self, jid: str, r: JobResult) -> str | None:
        """The gate, then the automatic fill (and submit). Returns the job's final status, or None when the job is for the review prompt
        (a gate failed, the form step was refused, the user declined the submit, or the page could not be driven)."""
        db = self.db
        self._stage("auto-gate")
        gates = self.h.auto_gate(jid)
        for g in gates.items:
            if g.info:
                self.out("  " + g.info)
        if not gates.ok:
            r.note = gates.line()
            self.out("  " + r.note)
            return None
        if self.dry_run:
            self.out("  AUTO READY: every gate passed. --dry-run opens no browser and submits nothing; this JD stays in inbox/.")
            return "auto_ready (dry run)"
        self._stage("preflight")
        try:
            self.h.preflight(jid)
        except ArchiveError as e:
            r.note = f"AUTO BLOCKED: archive: {e}"
            self.out("  " + r.note)
            return None
        self._stage("auto-fill")
        try:
            res = self.h.auto_apply(jid)
        except ArchiveError as e:                                                   # the wrong-file guard, an unknown work country
            r.note = f"AUTO BLOCKED: {e}"
            self.out("  " + r.note)
            return None
        db.log(jid, "autoapply", {"status": res.status, "reason": res.reason, "screenshots": res.screenshots, "filled": res.filled,
                                  "left_blank": res.blank})
        for shot in res.screenshots:
            self.out(f"  screenshot: {shot}")
        if res.blank:
            self.out("  left empty (optional, no answer): " + "; ".join(res.blank[:8]))
        if res.status == autolog.SUBMITTED:
            self.out(f"  Submitted and confirmed: {res.reason}.")
            self._stage("mark-applied")
            ok = self.h.mark_applied(jid, res.confirmation or None)
            return "applied" if ok else "applied (archive incomplete)"
        if res.status == autolog.NOT_SUBMITTED:
            self.out(f"  {res.reason}. Nothing was submitted; the job keeps its status and this JD stays in inbox/.")
            return "auto_filled (not submitted)"
        if res.status == autolog.UNCONFIRMED:
            db.update_job(jid, status="submit_unconfirmed", reason=res.reason)
            r.note = res.reason
            self.out(f"  SUBMIT CLICKED, NOT CONFIRMED: {res.reason}\n  Look at the screenshots and your e-mail; if it went through, run: "
                     f"careerops mark-applied --id {jid}")
            return "submit_unconfirmed"
        if res.status == autolog.NEEDS_USER and res.handed_off:
            return self._handed_off(jid, res.reason, r)
        if res.status == autolog.NEEDS_USER:
            db.update_job(jid, status="needs_user", reason=res.reason)
            r.note = res.reason
            self.out(f"  NEEDS YOU: {res.reason}\n  Finish the form by hand (careerops apply --id {jid}); once submitted: careerops mark-applied --id {jid}")
            return "needs_user"
        if res.status == autolog.DECLINED:
            self.out("  You declined the submit: the review prompt follows.")
            return None
        r.note = f"AUTO FAILED: {res.reason}"
        self.out("  " + r.note + " (nothing was submitted); the review prompt follows.")
        return None

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
            self._opened(True)                                                      # until "Did YOU submit?" is answered, a crash is asked about next time
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
        self._opened(False)
        if not submitted:
            if res.get("handoff"):
                return self._handed_off(jid, "HAND-OFF: " + str(res["handoff"]), r)
            return self._not_submitted(jid, "you did not submit")
        self._stage("mark-applied")
        ok = self.h.mark_applied(jid, confirmation or None)
        return "applied" if ok else "applied (archive incomplete)"

    def _handed_off(self, jid: str, why: str, r: JobResult) -> str:
        """The form went to the person's own browser (answers_to_paste.txt, CV and letter paths printed): status `handed_off`, nothing recorded as sent."""
        self.db.update_job(jid, status=HANDED_OFF, reason=why)
        self.db.log(jid, "live", {"handed_off": why})
        r.note = why
        self.out(f"  HANDED OFF: {why}\n  Submit it in your own browser; once you have: careerops mark-applied --id {jid}")
        return HANDED_OFF

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
        if self.ats_skipped:
            lines += [f"Skipped by the ATS band: {len(self.ats_skipped)}", *self.ats_skipped, ""]
        if stop:
            lines.append(f"Stopped: {stop}")
        return "\n".join(lines)
