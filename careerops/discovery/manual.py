"""Jobs added by hand (`careerops add-jd`, `careerops live`): read a JD file, add the job, triage that one job.

The functions return values (a job id, a verdict status); nothing here prints, so a caller never has to parse output.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import load_yaml
from ..db import DB
from . import gates
from . import register as sponsors

MIN_NAME_CHARS = 2                     # a company or title shorter than this is a stray keystroke, not a name
MIN_JD_BYTES = 500                     # a shorter file is a stub or a snippet, not a job description
# a job that has been tailored or acted on must not be pushed back to 'new' by dropping another JD with the same key
REPLACEABLE = ("new", "triaged", "skipped", "needs_jd", "error")
HEADER_KEYS = {"company": "company", "employer": "company", "title": "title", "role": "title", "job title": "title",
               "url": "url", "link": "url", "location": "location"}
HEADER_LINE = re.compile(r"^\s*([A-Za-z ]{3,12}?)\s*:\s*(\S.*?)\s*$")


class JDError(ValueError):
    """The JD file cannot be used. The message is one line, fit to print as it is."""


class JobExists(ValueError):
    """A job with this id is already past triage; adding the JD again would reset it."""


def read_jd(path: str | Path) -> tuple[str, str]:
    """(text, sha256 of the file's bytes) of a JD file. Raises JDError (one line) for a missing or unreadable file, a file
    under MIN_JD_BYTES, or one that is not UTF-8."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise JDError(f"{p}: no such file") from None
    except IsADirectoryError:
        raise JDError(f"{p}: is a folder, not a JD file") from None
    except OSError as e:
        raise JDError(f"{p}: cannot be read ({e.strerror or e})") from None
    if len(raw) < MIN_JD_BYTES:
        raise JDError(f"{p.name}: only {len(raw)} bytes (a job description needs at least {MIN_JD_BYTES})")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise JDError(f"{p.name}: not UTF-8 text (byte {e.start} is invalid); re-save it as UTF-8") from None
    return text.lstrip("﻿"), hashlib.sha256(raw).hexdigest()


def header_fields(text: str) -> dict[str, str]:
    """`Company:`, `Title:`, `URL:`, `Location:` lines among the first ten lines of a JD file, if it has them."""
    out: dict[str, str] = {}
    for line in text.splitlines()[:10]:
        m = HEADER_LINE.match(line)
        if m and m.group(1).strip().lower() in HEADER_KEYS:
            out.setdefault(HEADER_KEYS[m.group(1).strip().lower()], m.group(2))
    return out


def meta_problem(field: str, value: str | None) -> str | None:
    """Why this header / answer cannot be a company, title, URL or location, or None when it can. A value holding `<` or
    `>` is an unfilled template placeholder (`Company: <company name>`); a company or title under 2 characters is a
    stray keystroke (a single `o`, typed at a prompt meant for something else), not a name."""
    v = (value or "").strip()
    if "<" in v or ">" in v:
        return f"{field} {v[:60]!r} contains < or >: it looks like an unfilled placeholder"
    if field in ("company", "title") and len(v) < MIN_NAME_CHARS:
        return f"{field} {v!r} is shorter than {MIN_NAME_CHARS} characters"
    return None


def job_id_for(company: str, title: str, url: str | None) -> str:
    return "manual:" + hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest()[:12]


def add_jd(db: DB, text: str, *, company: str, title: str, url: str | None = None,
           location: str = "United Kingdom") -> str:
    """Add a hand-made job (status 'new') and return its id. Raises JobExists when that job is already tailored or
    acted on, so adding a JD never resets work. Raises JDError for a company, title or URL that is a placeholder or too
    short (meta_problem)."""
    for field, value in (("company", company), ("title", title), ("url", url)):
        problem = meta_problem(field, value) if value is not None else None
        if problem:
            raise JDError(problem)
    jid = job_id_for(company, title, url)
    have = db.job(jid)
    if have is not None and have["status"] not in REPLACEABLE:
        raise JobExists(f"{jid} ({company} / {title}) already exists with status '{have['status']}'")
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": title, "location": location,
                   "url": url, "apply_url": url, "description": text})
    db.update_job(jid, description=text, status="new")
    return jid


def triage_job(db: DB, settings, job_id: str) -> str:
    """Triage this one job (not every 'new' job) and return its status: triaged | skipped | needs_jd."""
    fb = load_yaml(settings.path("fact_bank"))
    gates.load_roles(settings, fb)
    v = gates.triage(dict(db.job(job_id)), settings, sponsors.load_index(db), fb)
    fields = gates.verdict_fields(v)
    db.update_job(job_id, **fields)
    return fields["status"]
