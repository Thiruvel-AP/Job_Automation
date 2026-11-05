"""The careers addresses you give: paths.discover/careers_urls.yaml (v3.0). The tool never writes this file; you edit it.

A company's careers address is looked at once, by you, and kept. From it the tool knows the employer's own job board and reads that, instead of guessing a
board name from the company's name (a guess is a request to a host that may not exist, and it is what got Ashby and Recruitee to refuse). The file:

    companies:
      - {company: Mistral AI, url: https://jobs.lever.co/mistral}
      - {company: NVIDIA, url: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite}
      - {company: British Heart Foundation, url: https://jobs.bhf.org.uk/, provider: eploy}

(a plain list of the same entries, or a `Company: url` mapping, is read too). A company may have several entries (several Workday sites, a graduate board). The address is matched to a reader by its host, nothing is fetched here:

    Greenhouse (boards.greenhouse.io/<slug>, job-boards.greenhouse.io/<slug>), Lever (jobs.lever.co/<slug>, jobs.eu.lever.co/<slug>), Ashby (jobs.ashbyhq.com/<slug>),
    Workable (apply.workable.com/<slug>), SmartRecruiters, Teamtailor / Recruitee / Pinpoint (<slug>.<their host>), Personio (<slug>.jobs.personio.de|com),
    Workday (<tenant>.<wdN>.myworkdayjobs.com/<site>), Eploy (a tenant's own domain: add `provider: eploy`).

An address no reader understands is reported with the reason, never guessed at. LinkedIn, Indeed and CV-Library addresses are refused. `careerops search-plan` lists
the companies that still have no address; `search-plan --check FILE` says which reader each proposed address would get, and prints the lines to paste.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from ..apply.naming import folder_key
from .ats import Board
from .robots import never_fetch

FILE = "careers_urls.yaml"
ORIGIN = "careers_url"
READERS = "Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Teamtailor, Recruitee, Pinpoint, Personio, Workday, Eploy (with provider: eploy)"

_SLUG = r"([A-Za-z0-9][A-Za-z0-9_-]*)"
_ROOTS = (                                               # (provider, host pattern, the board's slug is the first path segment)
    ("greenhouse", re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$")),
    ("lever", re.compile(r"^jobs\.lever\.co$")),
    ("lever_eu", re.compile(r"^jobs\.eu\.lever\.co$")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$")),
    ("workable", re.compile(r"^apply\.workable\.com$")),
    ("smartrecruiters", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com$")),
)
_HOSTED = (                                              # providers with one host per company: the slug is in the host name
    ("teamtailor", re.compile(r"^([a-z0-9][a-z0-9-]*)\.teamtailor\.com$")),
    ("recruitee", re.compile(r"^([a-z0-9][a-z0-9-]*)\.recruitee\.com$")),
    ("pinpoint", re.compile(r"^([a-z0-9][a-z0-9-]*)\.pinpointhq\.com$")),
    ("personio", re.compile(r"^([a-z0-9][a-z0-9-]*\.jobs\.personio\.(?:de|com))$")),
)
_NOT_A_BOARD = {"embed", "jobs", "careers", "j", "o", "api", "v1"}      # a first path segment that is not a company


@dataclass(frozen=True)
class Entry:
    company: str
    url: str
    provider: str = ""            # only `eploy` is ever needed: its tenants have no host pattern

    @property
    def key(self) -> str:
        return folder_key(self.company)


def path(settings) -> Path:
    return settings.path("discover") / FILE


def _entries(doc: object) -> tuple[list[dict], str]:
    """The raw entry dicts of a parsed file and a problem line when its shape is not understood."""
    if doc is None:
        return [], ""
    if isinstance(doc, dict) and isinstance(doc.get("companies"), list):
        doc = doc["companies"]
    if isinstance(doc, list):
        return [e if isinstance(e, dict) else {"_bad": e} for e in doc], ""
    if isinstance(doc, dict) and all(isinstance(v, str) for v in doc.values()) and "companies" not in doc:
        return [{"company": k, "url": v} for k, v in doc.items()], ""
    return [], "expected `companies:` followed by a list of {company, url} entries"


def load(settings) -> tuple[list[Entry], list[str]]:
    """(entries, problems). A missing file is no entries and no problem; an entry that is not understood is left out and named."""
    p = path(settings)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as e:
        return [], [f"{p}: cannot be read ({type(e).__name__})"]
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return [], [f"{p}: is not valid YAML"]
    rows, shape = _entries(doc)
    if shape:
        return [], [f"{p}: {shape}"]
    out: list[Entry] = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, e in enumerate(rows, 1):
        company, url = str(e.get("company") or "").strip(), str(e.get("url") or "").strip()
        if not company or not url:
            problems.append(f"careers_urls.yaml entry {n} needs both company and url")
            continue
        entry = Entry(company, url, str(e.get("provider") or "").strip().lower())
        same = (entry.key, url.rstrip("/").lower())                    # a company may have several addresses (Workday sites, a graduate board); only a repeat is a duplicate
        if same in seen:
            problems.append(f"careers_urls.yaml: {company} lists {url} twice (the first entry is used)")
            continue
        seen.add(same)
        out.append(entry)
    return out, problems


def board_of(url: str, company: str = "", provider: str = "") -> tuple[Board | None, str]:
    """(board, "") for an address a reader understands, (None, why not) for one that none does. Pure: nothing is fetched."""
    from . import more_ats, workday
    u = urlparse(str(url or "").strip())
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return None, "not an http(s) address"
    gone = never_fetch(url)
    if gone:
        return None, gone.split(" (")[0]
    from . import applykind
    if applykind.never_fetched(url) and not re.search(r"(?:myworkdayjobs|myworkday|myworkdaysite)\.com$", host):       # a Workday address is a board, read by its API
        return None, f"{host} is never fetched by this tool (LinkedIn, Indeed and CV-Library pages are not read)"
    segs = [s for s in u.path.split("/") if s]
    if provider == "eploy" or (more_ats.EPLOY_JOB.match(u.path or "") is not None) or (u.path or "").endswith("/live-jobs.xml"):
        return Board("eploy", host, company, ORIGIN), ""
    for ats, rx in _ROOTS:
        if rx.match(host):
            if not segs or segs[0].lower() in _NOT_A_BOARD or not re.fullmatch(_SLUG, segs[0]):
                return None, f"{host} address with no company name in it (expected {host}/<company>)"
            slug = segs[0] if ats in ("greenhouse", "lever", "lever_eu", "ashby") else segs[0].lower()
            return Board(ats, slug, company, ORIGIN), ""
    for ats, rx in _HOSTED:
        m = rx.match(host)
        if m:
            return Board(ats, m.group(1), company, ORIGIN), ""
    site = workday.parse_url(url)
    if site is not None:
        return Board("workday", site.slug, company, ORIGIN), ""
    if re.search(r"\.myworkdayjobs\.com$", host):
        return None, "a Workday address needs the site name: https://<tenant>.<wdN>.myworkdayjobs.com/<site>"
    return None, f"no reader for {host} yet (readers: {READERS})"


def boards(settings) -> list[Board]:
    """The boards of the valid entries, under the company name written in the file."""
    out: list[Board] = []
    for e in load(settings)[0]:
        b, _ = board_of(e.url, e.company, e.provider)
        if b is not None:
            out.append(b)
    return out


def keys(settings) -> set[str]:
    """folder_key of every company that has an entry (whatever its address): those companies' board names are not guessed."""
    return {e.key for e in load(settings)[0]}


def problems(settings) -> list[str]:
    """What is wrong in the file: unreadable, an entry that lacks a company or url, a duplicate, an address no reader understands (and why)."""
    entries, bad = load(settings)
    out = list(bad)
    for e in entries:
        b, why = board_of(e.url, e.company, e.provider)
        if b is None:
            out.append(f"careers_urls.yaml: {e.company}: {e.url}: {why}")
    return out


def yaml_line(company: str, url: str, provider: str = "") -> str:
    """One line to paste into careers_urls.yaml."""
    c = company.replace("'", "''")
    return f"  - {{company: '{c}', url: '{url}'" + (f", provider: {provider}" if provider else "") + "}"
