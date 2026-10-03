"""Wrong-file guard. Every CV is now named "<name> CV.pdf", so a wrong file is invisible in its name.

Called before fill_page uploads and again before archiving.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..tailoring.render import pdf_text
from .errors import WrongFileError


def _inside(path: Path, folder: Path) -> bool:
    p, f = path.resolve(), folder.resolve()
    return p == f or f in p.parents


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def check_job_files(job, cv_pdf: str | Path | None, letter_pdf: str | Path | None) -> None:
    """Raise WrongFileError unless the CV and (when given) the letter belong to `job`.

    - each path must lie inside the job's out_dir;
    - the letter's text layer (pdftotext) must contain the job's company name. Comparison ignores case, whitespace and
      punctuation (line wraps, curly quotes), and fails closed: unreadable text counts as a mismatch.
    """
    out_dir = job["out_dir"]
    if not out_dir:
        raise WrongFileError(f"{job['id']}: job has no out_dir, cannot verify its files")
    for label, path in (("CV", cv_pdf), ("letter", letter_pdf)):
        if path is None:
            continue
        if not _inside(Path(path), Path(out_dir)):
            raise WrongFileError(f"{label} {path} is not inside this job's out_dir {out_dir} ({job['id']})")
    if letter_pdf is None:
        return
    text = pdf_text(Path(letter_pdf)) or ""
    company = job["company"]
    want = _alnum(company)
    ok = want in _alnum(text) if want else " ".join(company.casefold().split()) in " ".join(text.casefold().split())
    if not ok:
        raise WrongFileError(f"letter {letter_pdf} does not mention the company '{company}' — "
                             f"it may belong to another job ({job['id']})")
