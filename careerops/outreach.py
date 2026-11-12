"""Outreach drafts: a short Gmail DRAFT to the people behind a job, made after you apply. You read it in Gmail and press send yourself.

This module never sends: the only Gmail call it makes is `drafts.create` (POST .../drafts). No SMTP client, no send endpoint; the guard in
googleapi.py refuses a path ending in /send and tests/test_never_sends.py fails the build on any send in the package.

  careerops outreach draft JOB [--to ADDRESS]    drafts for the job's best contacts (contacts.py); with --to, ONE test draft to that address
                                                 (nothing recorded, no limits used) to see how a draft looks
  runs by itself after `mark-applied` / `apply` / `live` when settings.yaml has `outreach.mode: drafts` (default `off`)

Limits: one draft per contact, two per job. The subject is `Application: <role> at <company> – <your name>`. The body is plain text,
at most MAX_WORDS words, and built from fact-bank text only: a greeting, "I have just applied for <role> (<req id>).", the job's hook letter
line word for word, one ask, your signature (name, phone, LinkedIn, GitHub from applicant.yaml) and an opt-out line. Before a draft is made
the body passes the letter claim guard (no "we" / "our", no bullet linked to another bullet's tool), carries no number the fact bank lacks
and no company name other than the target. Attachments: the CV PDF made for the job, and the letter PDF when one was chosen.
A contact on data/outreach/do_not_contact.csv, an address whose domain takes no mail, and a Hunter address that is not verified get no
draft.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import contacts as contacts_mod
from . import pathstore
from .config import load_yaml
from .db import DB
from .googleapi import GoogleAPI
from .discovery import gmail

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_PER_JOB = 2
MAX_WORDS = 120
DEFAULT_FOLLOW_UP_DAYS = 7
SUBJECT = "Application: {role} at {company} – {name}"
ASK = "I would value 15 minutes to hear what the team needs most in the first 90 days."
OPT_OUT = "If you'd rather not hear from me, just reply and I won't contact you again."
_REQ = re.compile(r"(?:\breq(?:uisition)?(?:\s*(?:id|no\.?|number|#))?|\bjob\s*(?:id|ref(?:erence)?|number)|\bref(?:erence)?(?:\s*(?:no\.?|number|#|id))?)"
                  r"\s*[:#]?\s*([A-Za-z]{0,4}[-_]?\d[\w-]{2,15})", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


class OutreachError(RuntimeError):
    """A draft cannot be made (one line, fit to print)."""


@dataclass
class Result:
    drafted: list[tuple[str, str]] = field(default_factory=list)      # (address, Gmail draft id) made by this call
    skipped: list[tuple[str, str]] = field(default_factory=list)      # (address or name, why no draft)
    status: str = ""                                                   # what the Sheet's Outreach status column says
    test: bool = False


# ------------------------------------------------------------------ settings and what the Sheet shows
def _cfg(settings) -> dict:
    return (getattr(settings, "raw", None) or {}).get("outreach") or {}


def mode(settings) -> str:
    """outreach.mode: "drafts", else "off" (YAML reads an unquoted `off` as False, which is also off)."""
    return str(_cfg(settings).get("mode") or "off").strip().lower()


def drafts_on(settings) -> bool:
    return mode(settings) == "drafts"


def drafts_made(db: DB, job_id: str) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM outreach_drafts WHERE job_id=?", (job_id,)).fetchone()[0]


def status(db: DB, job_id: str) -> str:
    """The Outreach status cell: "drafted <n>" once a draft exists, else why the last attempt made none ("" before any attempt)."""
    n = drafts_made(db, job_id)
    if n:
        return f"drafted {n}"
    row = db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='outreach' ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
    try:
        return str(json.loads(row[0]).get("status") or "") if row else ""
    except (ValueError, AttributeError):
        return ""


def follow_up_due(db: DB, settings, job: Mapping[str, Any]) -> str:
    """YYYY-MM-DD, the day you applied plus outreach.follow_up_days (7), once a draft exists; else ""."""
    if not drafts_made(db, job["id"]) or not job.get("applied_at"):
        return ""
    days = int(_cfg(settings).get("follow_up_days") or DEFAULT_FOLLOW_UP_DAYS)
    try:
        return (date.fromisoformat(str(job["applied_at"])[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return ""


# ------------------------------------------------------------------ the body
def req_id(job: Mapping[str, Any]) -> str:
    """The requisition / reference number the ad names ("Req ID: R-12345"), "" when it names none."""
    m = _REQ.search(str(job.get("description") or ""))
    return m.group(1) if m else ""


def applicant_name(applicant: Mapping[str, Any]) -> str:
    return " ".join(str(applicant.get(k) or "").strip() for k in ("first_name", "last_name")).strip()


def hook_line(rep: Mapping[str, Any]) -> str:
    """The job's hook letter line (the library entry's `letter_line`, stored as `letter_sentence`), "" when the job has no hook."""
    return str((rep.get("hook") or {}).get("letter_sentence") or "").strip()


def subject(job: Mapping[str, Any], applicant: Mapping[str, Any]) -> str:
    return SUBJECT.format(role=job["title"], company=job["company"], name=applicant_name(applicant))


def signature(applicant: Mapping[str, Any]) -> list[str]:
    return [x for x in (applicant_name(applicant), str(applicant.get("phone") or "").strip(), str(applicant.get("linkedin") or "").strip(),
                        str(applicant.get("github") or "").strip()) if x]


def build_body(job: Mapping[str, Any], rep: Mapping[str, Any], applicant: Mapping[str, Any], contact: contacts_mod.Contact | None) \
        -> tuple[str, list[str]]:
    """(the body, the strings the template itself supplies: greeting, opening line, ask, signature, opt-out). Raises OutreachError when
    the job has no hook line: a draft is built from the hook, never without it."""
    line = hook_line(rep)
    if not line:
        raise OutreachError("the job has no hook letter line (hook.enabled is off, or no hook passed its checks): no draft is made without it")
    first = contact.first_name if contact else ""
    greeting = f"Dear {first}," if first else f"Hello {job['company']} team,"
    rid = req_id(job)
    opening = f"I have just applied for {job['title']}" + (f" ({rid})" if rid else "") + "."
    sig = signature(applicant)
    body = "\n\n".join([greeting, opening, line, ASK, "\n".join(sig), OPT_OUT]) + "\n"
    return body, [greeting, opening, ASK, OPT_OUT, *sig]


def _bank_numbers(fb: Mapping[str, Any]) -> set[str]:
    text = yaml.safe_dump(dict(fb), allow_unicode=True, width=10_000)
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _bank_names(fb: Mapping[str, Any]) -> str:
    """Every employer, institution and project name of the fact bank, lower case, as one string (names the body may mention)."""
    return yaml.safe_dump(dict(fb), allow_unicode=True, width=10_000).lower()


def other_companies(db: DB, company: str) -> list[str]:
    """Company names of the other jobs in the database: a body that names one of them is a copy-paste from another application."""
    me = company.strip().lower()
    names = {str(r[0]).strip() for r in db.conn.execute("SELECT DISTINCT company FROM jobs")}
    return sorted(n for n in names if len(n) >= 4 and n.lower() != me and me not in n.lower() and n.lower() not in me)


def body_problems(body: str, supplied: list[str], job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], db: DB) -> list[str]:
    """Why a body may not become a draft (empty list = fine): too long; "we" / "our" or a bullet's object tied to another bullet's tool (the
    letter claim guard); a number that is in neither the fact bank nor the template; another company's name."""
    from .hook import Hook
    from .tailoring import letters

    out: list[str] = []
    words = len(body.split())
    if words > MAX_WORDS:
        out.append(f"{words} words, over the {MAX_WORDS} allowed (the hook line is used word for word, so it cannot be cut)")
    hk = Hook.from_dict(rep["hook"]) if rep.get("hook") else None
    exempt = letters.exempt_words(hk, dict(job), []) if hk else set()
    out += letters.claim_problems([p for p in body.split("\n\n") if p.strip()], str(job["company"]), dict(fb), exempt)
    rest = body
    for s in sorted(supplied, key=len, reverse=True):
        rest = rest.replace(s, " ")
    rest = rest.replace(str(job["company"]), " ").replace(str(job["title"]), " ").replace(req_id(job) or "\0", " ")
    known = _bank_numbers(fb)
    bad = sorted({n for n in (x.replace(",", "") for x in _NUMBER.findall(rest)) if n not in known})
    if bad:
        out.append("a number the fact bank does not have: " + ", ".join(bad))
    bank = _bank_names(fb)
    for name in other_companies(db, str(job["company"])):
        if re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", body, re.I) and name.lower() not in bank:
            out.append(f"names '{name}', a company other than {job['company']}")
    return out


# ------------------------------------------------------------------ the message and the Gmail draft
def attachments_for(job: Mapping[str, Any], rep: Mapping[str, Any]) -> list[Path]:
    """The CV PDF made for the job, then the letter PDF when one was chosen. A file that is gone from the output folder is taken from
    the job's archive folder when the same name is there. Raises OutreachError when the CV cannot be found."""
    out: list[Path] = []
    for key, required in (("cv_pdf", True), ("letter_pdf", False)):
        if not rep.get(key):
            if required:
                raise OutreachError("the job's report names no CV PDF")
            continue
        p = Path(rep[key])
        if not p.is_file() and job.get("application_dir") and (Path(job["application_dir"]) / p.name).is_file():
            p = Path(job["application_dir"]) / p.name
        if p.is_file():
            out.append(p)
        elif required:
            raise OutreachError(f"the CV PDF is not there: {p}")
    return out


def build_message(to: str, subject_line: str, body: str, files: list[Path]) -> EmailMessage:
    m = EmailMessage()
    m["To"], m["Subject"] = to, subject_line               # no From: Gmail fills in your own address on a draft
    m.set_content(body)
    for p in files:
        m.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return m


def create_draft(api: GoogleAPI, message: EmailMessage) -> str:
    """drafts.create: the message is stored in your Drafts folder and goes nowhere. Returns the draft's id."""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return str(api.call("POST", f"{GMAIL}/drafts", body={"message": {"raw": raw}}, what="Gmail draft").get("id") or "")


def drafts_api(settings) -> GoogleAPI:
    return GoogleAPI(settings, need=(gmail.SCOPE_COMPOSE,))


# ------------------------------------------------------------------ one job
def _why_not(settings, row: Mapping[str, Any]) -> str:
    if not row.get("email"):
        return "no address yet"
    hit = contacts_mod.blocked_by(settings, row["email"])
    if hit:
        return f"do-not-contact ({hit})"
    if row.get("mx_status") not in contacts_mod.MX_OK:
        return f"domain check: {row.get('mx_status') or 'not done'}"
    if row["source"] == "hunter" and row.get("hunter_status") != contacts_mod.HUNTER_VERIFIED:
        return f"unverified address (Hunter: {row.get('hunter_status') or 'unknown'})"
    return ""


def draft_for_job(db: DB, settings, job_row: Any, *, api: GoogleAPI | None = None, to: str | None = None, fb: Mapping[str, Any] | None = None,
                  resolver=None) -> Result:
    """Make the Gmail drafts for one applied job (see the module docstring); `to` makes one test draft to that address instead. Raises
    OutreachError / GoogleError / GmailAuthError when nothing could be made at all; a contact that gets no draft is in `skipped`."""
    job = {k: job_row[k] for k in job_row.keys()} if not isinstance(job_row, dict) else job_row
    jid = job["id"]
    if not job.get("out_dir") or not (Path(pathstore.resolve(job["out_dir"])) / "report.json").is_file():
        raise OutreachError(f"{jid} has no tailored CV (no report.json)")
    rep = pathstore.read_report(Path(pathstore.resolve(job["out_dir"])) / "report.json")
    fb = fb if fb is not None else load_yaml(settings.path("fact_bank"))
    applicant = load_yaml(settings.path("applicant"))
    if not hook_line(rep):
        raise OutreachError("the job has no hook letter line (hook.enabled is off, or no hook passed its checks): no draft is made without it")
    files = attachments_for(job, rep)
    subj = subject(job, applicant)
    res = Result(test=bool(to))
    api = api or drafts_api(settings)

    def make(address: str, contact: contacts_mod.Contact | None) -> str | None:
        body, supplied = build_body(job, rep, applicant, contact)
        problems = body_problems(body, supplied, job, rep, fb, db)
        if problems:
            res.skipped.append((address, "body refused: " + "; ".join(problems)))
            return None
        return create_draft(api, build_message(address, subj, body, files))

    if to:
        if not contacts_mod.valid_address(to):
            raise OutreachError(f"{to!r} is not an e-mail address")
        did = make(to.strip().lower(), None)
        if did is not None:
            res.drafted.append((to.strip().lower(), did))
        res.status = "test draft" if did is not None else "test draft refused"
        return res

    made = drafts_made(db, jid)
    done = {r[0] for r in db.conn.execute("SELECT contact_email FROM outreach_drafts WHERE job_id=?", (jid,))}
    for row in contacts_mod.stored(db, jid):
        label = row.get("email") or row.get("name") or "?"
        if made >= MAX_PER_JOB:
            break
        if row.get("email") and row["email"] in done:
            continue
        if row.get("email") and row.get("mx_status") in (None, "unknown"):
            row["mx_status"] = contacts_mod.mx_status(row["email"], resolver or contacts_mod.default_resolver())
            with db.conn:
                db.conn.execute("UPDATE contacts SET mx_status=? WHERE id=?", (row["mx_status"], row["id"]))
        why = _why_not(settings, row)
        if why:
            res.skipped.append((label, why))
            continue
        did = make(row["email"], contacts_mod.Contact(name=row.get("name") or "", role=row.get("role") or "", email=row["email"],
                                                       source=row["source"]))
        if did is None:
            continue
        with db.conn:
            db.conn.execute("INSERT INTO outreach_drafts VALUES (?,?,?,?,?)", (jid, row["email"], did, subj, _now()))
        res.drafted.append((row["email"], did))
        made += 1
    res.status = f"drafted {made}" if made else "no draft: " + ("; ".join(f"{a}: {w}" for a, w in res.skipped)[:160] or "no contact with an address")
    db.log(jid, "outreach", {"status": res.status, "drafted": [a for a, _ in res.drafted], "skipped": res.skipped})
    return res


def _now() -> str:
    from .db import now
    return now()
