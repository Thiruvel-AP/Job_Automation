"""Outputs older than the code (9 Nov 2026). The RightShip dry run filled the form with a CV from 31 Oct and a letter from 3 Nov: neither had the
v3.3 / v3.4 changes or the Education-first order, so the form showed what the code no longer makes.

Before a form is filled or a file uploaded, the CV and letter PDFs of the job are compared with the time of the last commit that changed the
tailoring, letter or template code (CODE_PATHS, `git log -1 --format=%ct`):

  * `careerops live` (dry runs included) sends a job whose outputs are older back to 'triaged', so it is tailored again and reviewed again,
    when `apply.stale_outputs` is `retailor` (the default); with `stop` the job stops with STOP_LINE.
  * `careerops apply` and the auto-apply refuse it (StaleOutputs, a WrongFileError) with STOP_LINE: no browser is opened.

A CV the report names that is missing on disk counts as stale. When git cannot say when the code last changed (no git, not a repository) the check
cannot run: it says so in one line and lets the job go on, as before this check existed.
The environment variable CAREEROPS_CODE_TIME overrides git: "off" (tests: nothing is checked) or a Unix time.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ..config import ROOT
from .errors import WrongFileError

CODE_PATHS = ("careerops/tailoring", "careerops/hook.py", "templates")
ENV = "CAREEROPS_CODE_TIME"
STOP_LINE = "outputs are older than the code: run with --retailor"
POLICIES = ("retailor", "stop")


class StaleOutputs(WrongFileError):
    """The CV or letter was made before the tailoring code last changed."""


@dataclass
class CodeChange:
    time: float                    # Unix time of the last commit that touched CODE_PATHS
    commit: str = ""               # "<short hash> <subject>"


@dataclass
class Stale:
    code: CodeChange
    files: list[tuple[str, str, float | None]] = field(default_factory=list)      # (CV | letter, path, mtime; None = missing)

    def line(self) -> str:
        bits = [f"{label} {'missing' if t is None else _day(t)}" for label, _, t in self.files]
        what = f" ({self.code.commit[:60]})" if self.code.commit else ""
        return f"{STOP_LINE} ({', '.join(bits)}; tailoring code changed {_day(self.code.time)}{what})"


def _day(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%d %b %H:%M")


def policy(settings) -> str:
    """apply.stale_outputs: retailor (the default) | stop."""
    raw = getattr(settings, "raw", settings) or {}
    v = str(((raw.get("apply") or {}) if isinstance(raw, Mapping) else {}).get("stale_outputs") or "retailor").strip().lower()
    return v if v in POLICIES else "retailor"


@lru_cache(maxsize=4)
def _git(root: str) -> CodeChange | None:
    try:
        out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%ct %h %s", "--", *CODE_PATHS], capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse(out.stdout) if out.returncode == 0 else None


def parse(line: str) -> CodeChange | None:
    """`<unix time> <hash> <subject>` (git log's format) -> CodeChange, None for anything else."""
    head, _, rest = (line or "").strip().partition(" ")
    try:
        return CodeChange(float(head), rest.strip())
    except ValueError:
        return None


def last_code_change(root: Path | str = ROOT) -> CodeChange | None:
    """When the tailoring, letter or template code last changed (a commit), None when that cannot be known or the check is off."""
    env = os.environ.get(ENV, "").strip()
    if env:
        if env.lower() == "off":
            return None
        try:
            return CodeChange(float(env), "set by " + ENV)
        except ValueError:
            return None
    return _git(str(root))


def outputs(job: Mapping[str, Any], rep: Mapping[str, Any] | None = None) -> list[tuple[str, str]]:
    """[(CV | letter, path)] the job's report names (its report.json when `rep` is None). No report: none."""
    if rep is None:
        out_dir = str(job["out_dir"] or "") if "out_dir" in job.keys() else ""
        try:
            rep = json.loads((Path(out_dir) / "report.json").read_text(encoding="utf-8")) if out_dir else {}
        except (OSError, ValueError):
            rep = {}
    found = []
    if rep.get("cv_pdf"):
        found.append(("CV", str(rep["cv_pdf"])))
    letter = rep.get("letter") or {}
    if rep.get("letter_pdf") and (Path(str(rep["letter_pdf"])).is_file() or letter.get("status") == "ok"):
        found.append(("letter", str(rep["letter_pdf"])))
    return found


def check(job: Mapping[str, Any], rep: Mapping[str, Any] | None = None, code: CodeChange | None = None,
          files: list[tuple[str, str]] | None = None) -> Stale | None:
    """Stale when a named PDF is missing or was written before the last code change; None when fresh, when there is nothing to check, or when
    the time of the code is unknown (see `unknown`)."""
    code = code if code is not None else last_code_change()
    if code is None:
        return None
    named = files if files is not None else outputs(job, rep)
    rows: list[tuple[str, str, float | None]] = []
    for label, path in named:
        p = Path(path)
        rows.append((label, path, p.stat().st_mtime if p.is_file() else None))
    old = [r for r in rows if r[2] is None or r[2] < code.time]
    return Stale(code, rows) if old else None


def unknown() -> str:
    """One line when the time of the code cannot be known (git missing or failing), '' when it can or the check is off."""
    if os.environ.get(ENV, "").strip():
        return ""
    return "" if last_code_change() is not None else "the date of the tailoring code is unknown (git log failed): outputs not checked against it"


def require_fresh(job: Mapping[str, Any], cv: str | Path | None, letter: str | Path | None) -> None:
    """Raise StaleOutputs when the CV or the letter about to be uploaded is older than the code (or missing). Called next to
    guard.check_job_files, before any browser opens."""
    files = [(label, str(p)) for label, p in (("CV", cv), ("letter", letter)) if p]
    found = check(job, files=files)
    if found is not None:
        raise StaleOutputs(f"{job['id']}: {found.line()}")
