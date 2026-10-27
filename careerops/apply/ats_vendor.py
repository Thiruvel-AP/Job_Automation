"""Which applicant-tracking system a host belongs to, and the address of its real application form.

`vendor_of(url)` maps a host to a vendor name (greenhouse, lever, ashby, workday, icims, successfactors, smartrecruiters, workable, bamboohr,
taleo, oracle); any other host returns the host itself. `direct_url(url)` is where the form is:

  Greenhouse  a job on a company's own domain (`gh_jid=<id>` in the address) opens `https://job-boards.greenhouse.io/<token>/jobs/<id>`, when the
              board token is known: the job's own board (its `board`, or its id `greenhouse:<token>:<posting>`), or a known board that the company domain or name spells.
              A Greenhouse board address stays as it is (an embedded `embed/job_app?for=..&token=..` one is resolved to its board). No token = the page as it is.
  Lever       `<job url>/apply`
  Ashby       `<job url>/application`
  anything else: the address as it is.

Nothing here makes a request.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse, urlunparse

from ..discovery import applykind

# vendor -> the domains it serves from (a host equal to one, or under one)
VENDOR_DOMAINS = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com"),
    "icims": ("icims.com",),
    "successfactors": ("successfactors.com", "successfactors.eu", "jobs2web.com"),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "bamboohr": ("bamboohr.com",),
    "taleo": ("taleo.net",),
}
ORACLE_HOST = re.compile(r"(?:^|\.)oraclecloud[\w-]*\.com$")
JOB_BOARDS = "job-boards.greenhouse.io"
GREENHOUSE_BOARD_HOSTS = ("boards.greenhouse.io", JOB_BOARDS)


def _host(url_or_host: str | None) -> str:
    text = str(url_or_host or "").strip()
    if not text:
        return ""
    return (urlparse(text if "//" in text else "//" + text).hostname or "").lower()


def _under(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def vendor_of(url_or_host: str | None) -> str:
    """The vendor of a URL or host; any other host returns the host itself ('' for nothing)."""
    host = _host(url_or_host)
    for vendor, domains in VENDOR_DOMAINS.items():
        if _under(host, domains):
            return vendor
    return "oracle" if ORACLE_HOST.search(host) else host


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _get(job: Mapping[str, Any] | None, key: str) -> str:
    """job[key] as text; '' when there is no job or no such column (a dict or a sqlite3.Row)."""
    try:
        return str(job[key] or "") if job is not None else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _board_token(job: Mapping[str, Any] | None, kind: str) -> str:
    """The token of the job's own board when it is a `<kind>:<token>` one, else ''."""
    provider, _, token = _get(job, "board").partition(":")
    if provider.strip().lower() == kind and token.strip():
        return token.strip()
    parts = _get(job, "id").split(":")                                    # a board posting's id is `<provider>:<board token>:<posting id>`
    return parts[1].strip() if len(parts) >= 3 and parts[0].strip().lower() == kind and parts[1].strip() else ""


def greenhouse_token(url: str, job: Mapping[str, Any] | None = None, tokens: Iterable[str] = ()) -> str:
    """The Greenhouse board token of a job on a company domain: the job's own board, else a known token that the domain or the company name
    spells (epicgames.com / "Epic Games" -> epicgames); '' when none is known."""
    own = _board_token(job, "greenhouse")
    if own:
        return own
    known = {_squash(t): t for t in tokens if t}
    labels = [_squash(p) for p in _host(url).split(".")[:-1] if p not in ("www", "careers", "jobs", "co")]
    company = _squash(_get(job, "company"))
    for key in (*labels, company):
        if key and key in known:
            return known[key]
    return ""


def _greenhouse_url(url: str, job: Mapping[str, Any] | None, tokens: Iterable[str]) -> str:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if host in GREENHOUSE_BOARD_HOSTS:
        query = parse_qs(parts.query)
        if parts.path.rstrip("/") == "/embed/job_app" and query.get("for") and query.get("token"):      # the embedded form of a board
            return f"https://{JOB_BOARDS}/{query['for'][0]}/jobs/{query['token'][0]}"
        return url
    ids = parse_qs(parts.query).get("gh_jid") or []
    jid = ids[0] if ids and ids[0].isdigit() else ""
    token = greenhouse_token(url, job, tokens) if jid else ""
    return f"https://{JOB_BOARDS}/{token}/jobs/{jid}" if token else url


def direct_url(url: str | None, *, kind: str | None = None, job: Mapping[str, Any] | None = None, tokens: Iterable[str] = ()) -> str:
    """The address of the application form for a job page `url` (see the module docstring). `kind` is the recorded apply_kind (it wins over
    the host for Lever and Ashby: a posting can sit on the employer's own domain)."""
    url = str(url or "")
    if not url:
        return ""
    kind = kind if kind in applykind.KINDS else applykind.kind_of(url)
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    if kind == "ashby" and not path.endswith("/application"):
        return urlunparse(parts._replace(path=path + "/application"))
    if kind == "lever" and not path.endswith("/apply"):
        return urlunparse(parts._replace(path=path + "/apply"))
    if kind in ("ashby", "lever"):
        return url
    if vendor_of(url) == "greenhouse" or "gh_jid=" in parts.query:
        return _greenhouse_url(url, job, tokens)
    return url
