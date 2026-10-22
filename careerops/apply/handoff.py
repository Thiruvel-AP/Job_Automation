"""Hand-off: a bot check or a sign-in wall means the person finishes the form in their own browser (the page `formfind.blocker` recognised).

What happens, and only this:
  1. the apply URL is opened in the person's normal Windows browser (`explorer.exe <url>`; nothing is clicked, filled or sent from there);
  2. the CV (and letter) PDF paths are printed;
  3. every answer that can be prepared from the person's own files is written to `answers_to_paste.txt` in the job's output folder, ready to
     copy: applicant.yaml (name, e-mail, phone, links, location, right to work / sponsorship, equal opportunities), the answers of answers.yaml
     that have an answer for this job, the approved free-text answers of this job (answer library), the cover letter text, and whatever the form
     had been filled with before the stop. Nothing is worked out or invented here; an answer that is blank in those files is listed as blank;
  4. the host is recorded in paths.live/handoff_hosts.json: the next job on that host goes straight to steps 1-3 and no automated browser is
     opened there. Delete the host's entry from that file to let the program try it again.

No challenge is solved, bypassed or outsourced, and no credential is ever typed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from . import answers as answers_mod
from . import eo, filler
from .formfind import host_of

HOSTS_FILE = "handoff_hosts.json"
PASTE_FILE = "answers_to_paste.txt"
EXPLORER_FALLBACK = "/mnt/c/Windows/explorer.exe"
STANDARD = (("Full name", None), ("First name", "first_name"), ("Last name", "last_name"), ("E-mail", "email"), ("Phone", "phone"),
            ("Location", "location"), ("LinkedIn", "linkedin"), ("GitHub", "github"), ("Website / portfolio", "website"))


def open_in_browser(url: str) -> bool:
    """Open `url` in the person's normal Windows browser with explorer.exe (inside WSL or on Windows). False when there is no such program."""
    if urlparse(url or "").scheme not in ("http", "https") or not host_of(url):
        return False
    explorer = shutil.which("explorer.exe") or (EXPLORER_FALLBACK if Path(EXPLORER_FALLBACK).exists() else None)
    if not explorer:
        return False
    subprocess.Popen([explorer, url])
    return True


class Handoff:
    """The hosts that handed a job over, and the hand-off itself. `opener(url)` opens a page for the person (tests replace it)."""

    def __init__(self, settings, opener: Callable[[str], bool] = open_in_browser, out: Callable[[str], None] = print):
        self.settings, self.opener, self.out = settings, opener, out
        self.path = settings.path("live") / HOSTS_FILE

    # ---- the hosts
    def hosts(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def known(self, url: str | None) -> str | None:
        """Why this URL's host was handed over before, or None."""
        entry = self.hosts().get(host_of(url))
        return str(entry.get("reason") or "handed over before") if isinstance(entry, dict) else None

    def record(self, url: str | None, reason: str, job_id: str = "") -> None:
        host = host_of(url)
        if not host:
            return
        hosts = self.hosts()
        hosts.setdefault(host, {"reason": reason, "job": job_id, "at": datetime.now().isoformat(timespec="seconds")})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(hosts, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- the hand-off
    def run(self, job: Mapping[str, Any], rep: Mapping[str, Any], applicant: Mapping[str, Any], book: answers_mod.AnswerBook | None, plan: Any,
            url: str, reason: str, filled: list[str] | None = None, remember: bool = True) -> Path | None:
        """Open `url` for the person, print the file paths and write answers_to_paste.txt in the job's output folder (its path is returned)."""
        job = dict(job)                               # a sqlite3.Row has no .get
        if remember:
            self.record(url, reason, str(job.get("id") or ""))
        opened = self.opener(url)
        self.out(f"\n  HAND-OFF: {reason}\n  {'Opened in your browser' if opened else 'Open this in your browser'}: {url}")
        for label, key in (("CV", "cv_pdf"), ("Letter", "letter_pdf")):
            if rep.get(key):
                self.out(f"  {label}: {rep[key]}")
        folder = job.get("out_dir") or (Path(rep["cv_pdf"]).parent if rep.get("cv_pdf") else None)
        if not folder:
            return None
        path = Path(folder) / PASTE_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(paste_text(job, rep, applicant, book, plan, url, reason, filled or [], self.settings), encoding="utf-8", newline="\n")
        except OSError as e:
            self.out(f"  could not write {path}: {e}")
            return None
        self.out(f"  Answers to paste: {path}")
        return path


def paste_text(job: Mapping[str, Any], rep: Mapping[str, Any], applicant: Mapping[str, Any], book: answers_mod.AnswerBook | None, plan: Any,
               url: str, reason: str, filled: list[str], settings=None) -> str:
    """The text of answers_to_paste.txt: every answer prepared from the person's own files, one block each. Nothing is made up."""
    book = book or answers_mod.AnswerBook()
    out = [f"{job.get('company', '')} | {job.get('title', '')}", f"Apply: {url}", f"Why this file exists: {reason}", "",
           "FILES", f"  CV: {rep.get('cv_pdf') or '(none)'}", f"  Letter: {rep.get('letter_pdf') or '(none)'}", "", "ABOUT YOU (applicant.yaml)"]
    for label, key in STANDARD:
        value = filler.full_name(applicant) if key is None else str(applicant.get(key) or "").strip()
        if value:
            out.append(f"  {label}: {value}")
    if plan is not None:
        out += ["", "RIGHT TO WORK AND SPONSORSHIP (applicant.yaml visa block for this job's work country)", f"  {plan.describe()}"]
        if getattr(plan, "summary", ""):
            out.append(f"  If a box wants words: {plan.summary}")
    listed = eo.entries(applicant, book)
    if listed:
        out += ["", "EQUAL OPPORTUNITIES (applicant.yaml and the eo_* entries of answers.yaml; the first option on the form that equals one of these)"]
        out += [f"  {e.key}: {e.answer or e.match_any[0]}  [options: {' | '.join(e.match_any)}]" for e in listed]
    rows = []
    for e in book.entries:
        if e.id.startswith("eo_"):
            continue
        text = answers_mod._fill(e.overrides.get(answers_mod.norm(str(job.get("company") or "")), e.answer), job)
        rows.append(f"  {' / '.join(e.patterns[:2])}: {text if text else '(blank in answers.yaml: yours to answer)'}")
    if rows:
        out += ["", "YOUR ANSWERS (answers.yaml, for this job)", *rows]
    approved = _approved_free_text(job, settings)
    if approved:
        out += ["", "FREE-TEXT ANSWERS YOU APPROVED FOR THIS JOB"]
        for q, a in approved:
            out += [f"  Q: {q}", f"  A: {a}", ""]
    letter = "\n\n".join((rep.get("letter") or {}).get("paragraphs") or [])
    if letter:
        out += ["", "COVER LETTER TEXT", letter]
    if filled:
        out += ["", "ALREADY FILLED ON THE FORM BEFORE THE STOP", *[f"  {x}" for x in filled]]
    return "\n".join(out).rstrip() + "\n"


def _approved_free_text(job: Mapping[str, Any], settings) -> list[tuple[str, str]]:
    """(question, answer) of the answers in the library that the person approved for this very job."""
    if settings is None:
        return []
    from . import freetext

    library = settings.path("answers_library")
    if not library.exists():                      # Library.records() would seed it: reading must not write
        return []
    try:
        records = freetext.Library(library).records()
    except (OSError, ValueError, KeyError):
        return []
    return [(r.question, r.answer) for r in records if r.job_id and r.job_id == job.get("id")]
