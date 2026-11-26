"""Jobs added by hand (`careerops add-jd`, `careerops live`): read a JD file, add the job, triage that one job.

The functions return values (a job id, a verdict status); nothing here prints, so a caller never has to parse output.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import load_yaml
from ..db import DB
from . import applykind, gates, workcountry
from . import register as sponsors

MIN_NAME_CHARS = 2                     # a company or title shorter than this is a stray keystroke, not a name
MIN_JD_BYTES = 500                     # a shorter file is a stub or a snippet, not a job description
# a job that has been tailored or acted on must not be pushed back to 'new' by dropping another JD with the same key
REPLACEABLE = ("new", "triaged", "skipped", "needs_jd", "needs_country", "error")
HEADER_KEYS = {"company": "company", "employer": "company", "title": "title", "role": "title", "job title": "title",
               "url": "url", "link": "url", "location": "location", "source": "source", "salary": "salary", "posted": "posted",
               "agency": "agency", "country": "country", "status": "status",
               "first seen": "first_seen", "listed": "listed", "posted via": "posted_source",
               "apply url": "apply_url", "apply kind": "apply_kind", "duplicate of": "duplicate_of"}
HEADER_WINDOW = 10                     # a header is looked for in the first lines; a block that starts there is read to its end
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
    """`Company:`, `Title:`, `URL:`, `Location:` ... lines at the top of a JD file, if it has them: among its first HEADER_WINDOW lines,
    and a run of header lines that is still going at the end of that window is read to its end (an inbox ad has up to 16)."""
    out: dict[str, str] = {}
    for i, line in enumerate(text.splitlines()):
        m = HEADER_LINE.match(line)
        key = HEADER_KEYS.get(m.group(1).strip().lower()) if m else None
        if i >= HEADER_WINDOW and not (key and out):
            break
        if key:
            out.setdefault(key, m.group(2))
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


def ad_body(text: str) -> str:
    """The ad without the file's own leading header block (`Company:`, `Title:` ... written by discovery, often from a board slug and so
    wrongly cased): the company's spelling is read from the ad's words, never from that header."""
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or (HEADER_LINE.match(lines[i]) and
                                                       HEADER_KEYS.get(HEADER_LINE.match(lines[i]).group(1).strip().lower()))):
        i += 1
    return "\n".join(lines[i:])


def display_name(company: str, title: str, text: str) -> str:
    """v3.9.7: the company as the ad writes it (LSEG, not Lseg; Kong, not Kong Inc). Never `.title()`."""
    from ..tailoring.letters import display_company

    return display_company(company, f"{title or ''}\n{ad_body(text)}")


def refresh_company(db: DB, job_id: str) -> bool:
    """Re-derive the stored company name of a job added before v3.9.7 (a board slug had been title-cased). True when the row changed."""
    job = db.job(job_id)
    if job is None or not job["company"]:
        return False
    name = display_name(job["company"], job["title"] or "", job["description"] or "")
    if name == job["company"]:
        return False
    db.update_job(job_id, company=name)
    return True


def add_jd(db: DB, text: str, *, company: str, title: str, url: str | None = None,
           location: str = "United Kingdom", source: str = "manual", posted: str | None = None, agency: bool = False,
           work_country: str | None = None, seen_on: str | None = None, open_listing: bool = False,
           posted_source: str | None = None, apply_url: str | None = None, apply_kind: str | None = None) -> str:
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
    db.upsert_job({"id": jid, "source": source, "company": display_name(company, title, text), "title": title, "location": location,
                   "url": url, "apply_url": apply_url or url, "description": text, "posted_at": posted,
                   "posted_at_source": posted_source if posted else None})
    kind = apply_kind if apply_kind in applykind.KINDS else applykind.kind_of(apply_url or url)
    db.update_job(jid, description=text, status="new", source=source, agency=int(bool(agency)), apply_kind=kind or None,
                  **({"posted_at": posted, "posted_at_source": posted_source} if posted and posted_source else
                     {"posted_at": posted} if posted else {}),
                  **({"seen_on": seen_on} if seen_on else {}), **({"open_listing": 1} if open_listing else {}),
                  **({"work_country": work_country, "work_evidence": f"{workcountry.SET_BY_YOU}: Country header"} if work_country else {}))
    return jid


def triage_job(db: DB, settings, job_id: str) -> str:
    """Triage this one job (not every 'new' job) and return its status: triaged | skipped | needs_jd."""
    fb = load_yaml(settings.path("fact_bank"))
    gates.load_roles(settings, fb)
    v = gates.triage(dict(db.job(job_id)), settings, sponsors.load_index(db), fb)
    fields = gates.verdict_fields(v)
    db.update_job(job_id, **fields)
    return fields["status"]


def refresh_sponsor(db: DB, settings, job_id: str) -> bool:
    """v3.9.6: re-run the register lookup (alias map included) for a stored job and save it when it differs, so the final P reads the same S as the
    inbox estimate (which always looks the employer up fresh). A row triaged before the alias map existed kept 'none'. True when the row changed."""
    job = db.job(job_id)
    idx = sponsors.load_index(db)
    if job is None or idx is None or not job["company"]:
        return False
    m = idx.lookup(job["company"], settings["sponsors"].get("fuzzy_threshold", 0.92))
    if (job["sponsor_match"], job["sponsor_name"]) == (m.label, m.name):
        return False
    db.update_job(job_id, sponsor_name=m.name, sponsor_score=m.score, sponsor_match=m.label, sponsor_route=m.route, sponsor_rating=m.rating)
    db.log(job_id, "live", f"sponsor re-checked: {job['sponsor_match']} -> {m.label}")
    return True
