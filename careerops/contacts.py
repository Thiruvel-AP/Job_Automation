"""Who to write to about an application: the people and addresses behind a job, found without scraping anyone.

Sources, in this order, until a job has MAX_CONTACTS (2) stored:
  1. jd            e-mail addresses that appear in the job ad's own text
  2. company_site  `mailto:` links on <domain>/careers, /jobs and /contact (each fetched once, robots.txt respected, at most one request
                   a second to the host) whose address looks like recruitment or HR (careers@, jobs@, recruitment@, talent@, hr@ ...) and
                   is on the company's own domain
  3. user / hunter `careerops contact add JOB "First Last" --role "Hiring Manager" [--email x@y]`: YOU find the person on LinkedIn (this code
                   never fetches linkedin.com). With no --email and HUNTER_API_KEY set, Hunter's Email Finder is asked once (domain + name);
                   its verification status is kept and `accept_all` is NOT treated as verified. No address is ever guessed from a pattern.

Every address gets a domain check (MX record, else an A record: `mx_status`). There is no SMTP `RCPT TO` probe: port 25 is usually blocked
at home, a catch-all server says 250 to every address (so the answer means nothing) and probing can get an IP flagged.

`data/outreach/do_not_contact.csv` (one address or domain per row, header `value,note`) is checked before any draft (outreach.py).
Nothing here sends anything. Tests pass `fetcher`, `resolver` and `hunter` fakes: no test touches the network or DNS.
"""
from __future__ import annotations

import csv
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

import dns.exception
import dns.resolver

from .db import DB, now
from .discovery.http import Blocked, FetchError, Fetcher
from .discovery.register import normalise
from .discovery.robots import RobotsCache
from .setup import DNC_FILE

MAX_CONTACTS = 2                                   # discovery stops at two stored contacts per job
SITE_PATHS = ("/careers", "/jobs", "/contact")
RECRUIT_LOCALS = frozenset({"careers", "career", "jobs", "recruitment", "recruiting", "talent", "hr", "hiring"})
GENERIC_LOCALS = RECRUIT_LOCALS | {"info", "hello", "contact", "enquiries", "people", "team", "admin", "office", "resourcing"}
SOURCE_RANK = {"user": 0, "hunter": 1, "jd": 2, "company_site": 3}    # who is shown and written to first
MX_OK = ("ok", "a_record")                         # a domain that can receive mail (an A record is the implicit MX of RFC 5321)
HUNTER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_VERIFIED = "valid"                          # every other Hunter status (accept_all, unknown, ...) is unverified
DNS_SERVERS = ["1.1.1.1", "9.9.9.9", "8.8.8.8", "193.110.81.0"]     # public resolvers, as in the old VaidateMailAddress.py
DNS_LIFETIME_S = 10
# ATS and job-board hosts: a job link on one of these says nothing about the employer's own domain
BOARD_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com", "workday.com",
               "linkedin.com", "indeed.com", "indeed.co.uk", "reed.co.uk", "cv-library.co.uk", "adzuna.co.uk", "totaljobs.com",
               "glassdoor.com", "glassdoor.co.uk", "icims.com", "bamboohr.com", "teamtailor.com", "recruitee.com", "personio.com",
               "personio.de", "jobvite.com", "pinpointhq.com", "breezy.hr", "taleo.net", "successfactors.com", "oraclecloud.com",
               "wellfound.com", "hackajob.com", "otta.com", "welcometothejungle.com", "jobserve.com", "gov.uk", "findajob.dwp.gov.uk")
TWO_LEVEL = frozenset({"co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "com.au", "co.nz", "co.in", "com.br", "co.jp"})
_EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_MAILTO = re.compile(r"""href\s*=\s*["']mailto:([^"'?>\s]+)""", re.I)
_NOT_PEOPLE = re.compile(r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|postmaster|abuse|privacy|dpo|unsubscribe)@", re.I)
_FILE_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


class ContactError(ValueError):
    """Something wrong with what was asked (one line, fit to print)."""


@dataclass
class Contact:
    email: str | None = None
    name: str = ""
    role: str = ""
    source: str = "user"
    mx_status: str | None = None
    hunter_status: str | None = None
    notes: list[str] = field(default_factory=list)       # why something was not done (not stored)

    @property
    def first_name(self) -> str:
        """The first word of the name, "" when the contact is a role address or has no name."""
        return self.name.split()[0] if self.name.split() else ""

    @property
    def verified(self) -> bool:
        """False for an address Hunter could not verify (accept_all, unknown ...); True otherwise."""
        return self.source != "hunter" or self.hunter_status == HUNTER_VERIFIED


# ------------------------------------------------------------------ addresses and domains
def valid_address(email: str) -> bool:
    e = (email or "").strip()
    return bool(_EMAIL.fullmatch(e)) and ".." not in e and not e.lower().endswith(_FILE_SUFFIX)


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def registrable(host: str) -> str:
    """example.co.uk for careers.example.co.uk; example.com for www.example.com."""
    labels = [x for x in (host or "").lower().strip(".").split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-3:] if ".".join(labels[-2:]) in TWO_LEVEL else labels[-2:])


def _is_board(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in BOARD_HOSTS)


def company_domain(db: DB, job: Mapping[str, Any]) -> str:
    """The employer's own domain, "" when it cannot be told: the careers URL the companies table holds for the name, else the host of the
    job's own link when that is not an ATS or a job board."""
    cands: list[str] = []
    try:
        row = db.conn.execute("SELECT careers_url FROM companies WHERE norm=? AND careers_url IS NOT NULL AND careers_url != '' LIMIT 1",
                              (normalise(str(job["company"])),)).fetchone()
    except Exception:  # noqa: BLE001  (a database from before the companies table: no such table)
        row = None
    if row:
        cands.append(row[0])
    cands += [str(job[k] or "") for k in ("url", "apply_url") if k in job.keys()] if hasattr(job, "keys") else []
    for url in cands:
        host = (urlparse(url).hostname or "").lower()
        if host and not _is_board(host):
            return registrable(host)
    return ""


def emails_in_text(text: str, own: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Addresses in a job ad, in order, lower case, once each: not yours, not a no-reply or privacy mailbox, not a file name."""
    out: list[str] = []
    for m in _EMAIL.finditer(text or ""):
        e = m.group(0).lower().rstrip(".")
        if (e in own or e in out or _NOT_PEOPLE.search(e) or e.endswith(_FILE_SUFFIX) or domain_of(e) in ("example.com", "example.org")
                or not valid_address(e)):
            continue
        out.append(e)
    return out


def recruiting_mailtos(html: str, domain: str) -> list[str]:
    """`mailto:` addresses of a page that look like recruitment or HR and are on `domain` (the company's own, subdomains included)."""
    out: list[str] = []
    for m in _MAILTO.finditer(html or ""):
        e = unquote(m.group(1)).strip().lower()
        if not valid_address(e) or e in out:
            continue
        if e.split("@")[0] in RECRUIT_LOCALS and registrable(domain_of(e)) == registrable(domain):
            out.append(e)
    return out


# ------------------------------------------------------------------ the domain check (no SMTP probe)
def default_resolver():
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = list(DNS_SERVERS)
    r.lifetime = DNS_LIFETIME_S
    return r


def _lookup(resolver, domain: str, rdtype: str, retries: int = 2, sleep: Callable[[float], None] | None = None) -> tuple[list | None, bool]:
    """(records or None, definitive?). No such record / no such domain is a definitive None; a timeout or a resolver that did not answer
    is retried, then an indefinite None (an unknown, not a no)."""
    for attempt in range(retries):
        try:
            return list(resolver.resolve(domain, rdtype, tcp=True)), True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return None, True
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            if attempt + 1 < retries:
                (sleep or time.sleep)(1)
        except dns.exception.DNSException:
            return None, False
    return None, False


def mx_status(email: str, resolver=None, sleep: Callable[[float], None] | None = None) -> str:
    """ok (an MX record) | a_record (no MX, an A record: the domain's own host takes the mail) | no_mx (the domain takes no mail: nothing,
    or a null MX) | invalid (not an address) | unknown (DNS did not answer: try again later). The domain only: no mailbox is probed."""
    if not valid_address(email):
        return "invalid"
    domain = domain_of(email)
    resolver = resolver or default_resolver()
    mx, sure_mx = _lookup(resolver, domain, "MX", sleep=sleep)
    if mx:
        hosts = [str(getattr(r, "exchange", "")).rstrip(".") for r in mx]
        return "ok" if any(hosts) else "no_mx"             # RFC 7505: a lone "." exchange says the domain accepts no mail
    a, sure_a = _lookup(resolver, domain, "A", sleep=sleep)
    if a:
        return "a_record"
    return "no_mx" if sure_mx and sure_a else "unknown"


# ------------------------------------------------------------------ do-not-contact
def dnc_path(settings) -> Path:
    return settings.path("outreach") / DNC_FILE


def ensure_dnc_file(settings) -> Path:
    """data/outreach/do_not_contact.csv with its header, created once; never rewritten."""
    p = dnc_path(settings)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("value,note\n", encoding="utf-8")
    return p


def load_dnc(settings) -> tuple[set[str], set[str]]:
    """(addresses, domains) of the do-not-contact file: the first column of each row; `a@b.com` is an address, `b.com` or `@b.com` a
    domain. A missing file is an empty list."""
    addresses: set[str] = set()
    domains: set[str] = set()
    try:
        text = dnc_path(settings).read_text(encoding="utf-8-sig")
    except OSError:
        return addresses, domains
    for row in csv.reader(text.splitlines()):
        v = (row[0] if row else "").strip().lower()
        if not v or v.startswith("#") or v in ("value", "address", "email", "domain"):
            continue
        if "@" in v and not v.startswith("@"):
            addresses.add(v)
        else:
            domains.add(v.lstrip("@"))
    return addresses, domains


def blocked_by(settings, email: str) -> str | None:
    """The do-not-contact entry that covers `email`, or None."""
    e = (email or "").strip().lower()
    addresses, domains = load_dnc(settings)
    if e in addresses:
        return e
    d = domain_of(e)
    return next((x for x in domains if d == x or d.endswith("." + x)), None)


# ------------------------------------------------------------------ the contacts table
def stored(db: DB, job_id: str) -> list[dict]:
    """A job's contacts, best first (user, hunter, jd, company_site; then oldest)."""
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM contacts WHERE job_id=? ORDER BY id", (job_id,))]
    return sorted(rows, key=lambda r: (SOURCE_RANK.get(r["source"], 9), r["id"]))


def _save(db: DB, job_id: str, c: Contact) -> bool:
    """Store a contact unless that address (or, with no address, that name) is already stored for the job; a named contact whose
    address is now known gets it. True when something was written."""
    email = (c.email or "").strip().lower() or None
    with db.conn:
        if email:
            have = db.conn.execute("SELECT id FROM contacts WHERE job_id=? AND email=?", (job_id, email)).fetchone()
            if have:
                return False
            named = (db.conn.execute("SELECT id FROM contacts WHERE job_id=? AND email IS NULL AND lower(name)=?",
                                     (job_id, c.name.lower())).fetchone() if c.name else None)
            if named:
                db.conn.execute("UPDATE contacts SET email=?, role=COALESCE(NULLIF(?, ''), role), source=?, mx_status=?, hunter_status=? "
                                "WHERE id=?", (email, c.role, c.source, c.mx_status, c.hunter_status, named[0]))
                return True
        elif db.conn.execute("SELECT id FROM contacts WHERE job_id=? AND email IS NULL AND lower(name)=?",
                             (job_id, c.name.lower())).fetchone():
            return False
        db.conn.execute("INSERT INTO contacts (job_id, name, role, email, source, mx_status, hunter_status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (job_id, c.name or None, c.role or None, email, c.source, c.mx_status,
                                                       c.hunter_status, now()))
    return True


def describe(row: Mapping[str, Any], settings=None) -> str:
    who = (row.get("name") or "").strip() or "(no name)"
    bits = [f"source {row['source']}", f"MX {row.get('mx_status') or 'not checked'}"]
    if row["source"] == "hunter":
        bits.append(f"Hunter {row.get('hunter_status') or 'unknown'}" + ("" if row.get("hunter_status") == HUNTER_VERIFIED else " (unverified)"))
    if row.get("role"):
        bits.insert(0, row["role"])
    if settings is not None and row.get("email") and blocked_by(settings, row["email"]):
        bits.append("DO NOT CONTACT")
    return f"{who} <{row.get('email') or 'no address yet'}> | " + " | ".join(bits)


# ------------------------------------------------------------------ discovery
def _own_addresses(settings) -> set[str]:
    try:
        from .config import load_yaml
        return {str(load_yaml(settings.path("applicant")).get("email") or "").lower()} - {""}
    except Exception:  # noqa: BLE001
        return set()


def discover(db: DB, settings, job: Mapping[str, Any], *, fetcher: Fetcher | None = None, resolver=None) -> list[dict]:
    """Find contacts for one job (the ad, then the company site) until MAX_CONTACTS are stored; every new address gets its domain check, an
    address whose check was `unknown` is checked again. Returns the job's contacts, best first. Never raises for a page that cannot be
    fetched: that source is skipped."""
    jid = job["id"]
    resolver = resolver or default_resolver()
    for r in stored(db, jid):
        if r["email"] and r["mx_status"] in (None, "unknown"):
            with db.conn:
                db.conn.execute("UPDATE contacts SET mx_status=? WHERE id=?", (mx_status(r["email"], resolver), r["id"]))
    if len(stored(db, jid)) < MAX_CONTACTS:
        for e in emails_in_text(str(job["description"] or ""), _own_addresses(settings)):
            if len(stored(db, jid)) >= MAX_CONTACTS:
                break
            _save(db, jid, Contact(email=e, source="jd", mx_status=mx_status(e, resolver)))
    domain = company_domain(db, job) if len(stored(db, jid)) < MAX_CONTACTS else ""
    if domain:
        fetcher = fetcher or Fetcher(RobotsCache(settings.path("robots")))
        for path in SITE_PATHS:
            if len(stored(db, jid)) >= MAX_CONTACTS:
                break
            try:
                r = fetcher.request(f"https://{domain}{path}", what=f"{domain}{path}")
            except (Blocked, FetchError):
                continue
            if r.status_code != 200:
                continue
            for e in recruiting_mailtos(r.text, domain):
                if len(stored(db, jid)) >= MAX_CONTACTS:
                    break
                _save(db, jid, Contact(email=e, source="company_site", mx_status=mx_status(e, resolver)))
    return stored(db, jid)


def hunter_find(domain: str, name: str, key: str, fetcher: Fetcher) -> tuple[str | None, str | None]:
    """Hunter's Email Finder, asked once: (address, verification status), (None, None) when it knows none. The key goes in a header, not
    in the URL. Raises FetchError / Blocked."""
    r = fetcher.request(HUNTER_URL, params={"domain": domain, "full_name": name}, headers={"X-API-KEY": key}, robots=False,
                        what="Hunter email finder")
    if r.status_code == 404:
        return None, None
    if not r.ok:
        raise FetchError(f"Hunter email finder: HTTP {r.status_code}")
    try:
        data = (r.json() or {}).get("data") or {}
    except ValueError:
        raise FetchError("Hunter email finder: the reply was not JSON") from None
    email = str(data.get("email") or "").strip().lower()
    if not valid_address(email):
        return None, None
    return email, str((data.get("verification") or {}).get("status") or "unknown")


def add(db: DB, settings, job: Mapping[str, Any], name: str, role: str = "", email: str = "", domain: str = "", *,
        resolver=None, fetcher: Fetcher | None = None, environ: Mapping[str, str] | None = None) -> Contact:
    """`careerops contact add`: a person YOU found. With an address it is stored as given (source user). Without one and with
    HUNTER_API_KEY set, Hunter is asked once for domain + name (source hunter, its status kept; accept_all is unverified); with neither
    the name is stored with no address and `notes` says why. The domain check runs on every address."""
    name = " ".join((name or "").split())
    if not name:
        raise ContactError("a contact needs a name: contact add JOB \"First Last\" --role ...")
    env = os.environ if environ is None else environ
    resolver = resolver or default_resolver()
    c = Contact(name=name, role=" ".join((role or "").split()), source="user")
    email = (email or "").strip().lower()
    if email:
        if not valid_address(email):
            raise ContactError(f"{email!r} is not an e-mail address")
        c.email = email
    else:
        key = (env.get("HUNTER_API_KEY") or "").strip()
        domain = (domain or company_domain(db, job)).strip().lower()
        if not key:
            c.notes.append("no address: pass --email, or set HUNTER_API_KEY to have Hunter look it up (nothing is guessed from a pattern)")
        elif not domain:
            c.notes.append("no address: the company's domain is not known (pass --domain example.com), so Hunter was not asked")
        else:
            try:
                found, status = hunter_find(domain, name, key, fetcher or Fetcher(RobotsCache(settings.path("robots"))))
            except (Blocked, FetchError) as e:
                c.notes.append(f"no address: {e}")
            else:
                if found:
                    c.email, c.source, c.hunter_status = found, "hunter", status
                    if status != HUNTER_VERIFIED:
                        c.notes.append(f"Hunter status is {status}: the address is unverified, so no draft goes to it by itself "
                                       "(add it with --email if you have checked it)")
                else:
                    c.notes.append("no address: Hunter knows none for that name and domain")
    if c.email:
        c.mx_status = mx_status(c.email, resolver)
    _save(db, job["id"], c)
    db.log(job["id"], "contact", {"name": c.name, "source": c.source, "email": c.email, "mx": c.mx_status})
    return c


# ------------------------------------------------------------------ the review screen (after [a]pprove)
def review_step(db: DB, settings, job: Mapping[str, Any], ask: Callable[[str], str] = input, say: Callable[[str], None] = print, *,
                fetcher: Fetcher | None = None, resolver=None) -> None:
    """Show the contacts found for an approved job, with their source and domain check, and offer `[c]ontact add` before mark-applied.
    A failure here is printed and never blocks the review."""
    try:
        rows = discover(db, settings, job, fetcher=fetcher, resolver=resolver)
    except Exception as e:  # noqa: BLE001  (DNS, network, a missing dnspython: report, keep reviewing)
        say(f"  contacts: could not look ({e})")
        rows = stored(db, job["id"])

    def show(rows):
        say("  Contacts (at most two are written to; nothing is sent, drafts only):" if rows else
            "  Contacts: none found in the ad or on the company site. Find the hiring manager on LinkedIn, then [c]ontact add.")
        for r in rows:
            say("   - " + describe(r, settings))
    show(rows)
    while ask("  [c]ontact add  [enter] continue > ").strip().lower() == "c":
        try:
            c = add(db, settings, job, ask("  name (First Last): "), ask("  role (Hiring Manager): ").strip() or "Hiring Manager",
                    ask("  email (blank: ask Hunter if a key is set): "), fetcher=fetcher, resolver=resolver)
        except (ContactError, OSError, Blocked, FetchError) as e:
            say(f"  not added: {e}")
            continue
        for n in c.notes:
            say(f"  note: {n}")
        show(stored(db, job["id"]))
