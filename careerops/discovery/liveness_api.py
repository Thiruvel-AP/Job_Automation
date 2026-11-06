"""Is this posting still open? Ask the ATS's own public API first (v3.0), the page only when the API cannot say.

A Greenhouse, Lever, Ashby or Workday posting has a public JSON address of its own. One small request to it is more reliable than reading the web page (an Ashby page is
a script shell with no text; Greenhouse and Lever pages differ per employer) and it is polite: the same host, the same robots.txt and pace as any board read.

    Greenhouse   boards-api.greenhouse.io/v1/boards/<slug>/jobs/<id>        200 = open, 404 = closed
    Lever        api.lever.co (api.eu.lever.co)/v0/postings/<slug>/<id>     200 = open, 404 = closed
    Ashby        api.ashbyhq.com/posting-api/job-board/<slug>               the posting is open when the board still lists its id; closed when it does not
    Workday      <site>/wday/cxs/<tenant>/<site>/job/...                    200 with the posting = open, 404 = closed (the page host stays unread)

Conservative on purpose: "closed" only for a definite answer (a 404, or a board that is read in full and lacks the id). A redirect, a 5xx, a body that is not what the API
returns or a link of another shape (an employer-hosted `?gh_jid=`): None, and `liveness.check` goes on to the page check as before. A 401 / 403 / 429 or a robots.txt refusal
from the API is NOT worked around by asking the vendor's page instead: the answer is "uncertain" and nothing more is requested. The caller caches the answer for the usual hours.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from . import ats as ats_mod
from . import workday
from .http import Blocked, FetchError, Fetcher, Soft401

_GH_HOST = re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$")
_GH_PATH = re.compile(r"^/([A-Za-z0-9_-]+)/jobs/(\d+)")
_LEVER_PATH = re.compile(r"^/([A-Za-z0-9_-]+)/([0-9a-f]{8}-[0-9a-f-]{20,})", re.I)
_ASHBY_PATH = re.compile(r"^/([A-Za-z0-9_-]+)/([0-9a-f]{8}-[0-9a-f-]{20,})", re.I)


class Refused(Exception):
    """The vendor's API (or its robots.txt) said no: not a reason to ask somewhere else."""


def _get(fetcher: Fetcher, url: str, what: str, headers: dict | None = None, soft_401: str = "") -> Any | None:
    """The response, or None when the API cannot say (blocked, no answer, a redirect)."""
    try:
        r = fetcher.request(url, what=what, follow=False, **({"headers": headers} if headers else {}), **({"soft_401": soft_401} if soft_401 else {}))
    except (Blocked, Soft401) as e:              # v3.1: a 401 on a known Ashby board ends the check too (the host is only stopped at the third in a row)
        raise Refused(str(e)[:160]) from None
    except FetchError:
        return None
    return None if 300 <= r.status_code < 400 else r


def _json(r: Any) -> Any:
    try:
        return r.json()
    except (ValueError, AttributeError):
        return None


def check(url: str, fetcher: Fetcher) -> tuple[str, str] | None:
    """('open'|'closed', reason) from the ATS's API; ('uncertain', reason) when the API refused; None when the link is not one of these or the API cannot say."""
    try:
        return _check(url, fetcher)
    except Refused as e:
        return "uncertain", f"not checked, the ATS API said no: {e}"


def _check(url: str, fetcher: Fetcher) -> tuple[str, str] | None:
    u = urlparse(str(url or "").strip())
    host, path = (u.hostname or "").lower(), u.path or ""
    if u.scheme not in ("http", "https") or not host:
        return None
    if _GH_HOST.match(host):
        m = _GH_PATH.match(path)
        if not m:
            return None
        slug, jid = m.groups()
        r = _get(fetcher, f"{ats_mod.GH}/{slug}/jobs/{jid}", f"Greenhouse job {jid}")
        if r is None:
            return None
        if r.status_code == 404:
            return "closed", f"not on the Greenhouse board of {slug} any more (HTTP 404)"
        return ("open", f"listed on the Greenhouse board of {slug}") if r.status_code == 200 and isinstance(_json(r), dict) else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        m = _LEVER_PATH.match(path)
        if not m:
            return None
        slug, jid = m.groups()
        base = ats_mod.LEVER["lever_eu" if host == "jobs.eu.lever.co" else "lever"]
        r = _get(fetcher, f"{base}/{slug}/{jid}", f"Lever job {jid}")
        if r is None:
            return None
        if r.status_code == 404:
            return "closed", f"not on the Lever board of {slug} any more (HTTP 404)"
        return ("open", f"listed on the Lever board of {slug}") if r.status_code == 200 and isinstance(_json(r), dict) else None
    if host == "jobs.ashbyhq.com":
        m = _ASHBY_PATH.match(path)
        if not m:
            return None
        slug, jid = m.groups()
        r = _get(fetcher, f"{ats_mod.ASHBY}/{slug}", f"Ashby board {slug}", soft_401="known")    # v3.1: a board a real link names; 3 401s in a row stop the host
        data = _json(r) if r is not None and r.status_code == 200 else None
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list) or not jobs:
            return None                                   # a board that cannot be read (or is empty) says nothing about one posting
        listed = {str(j.get("id", "")).lower() for j in jobs if isinstance(j, dict) and j.get("isListed", True)}
        return ("open", f"listed on the Ashby board of {slug}") if jid.lower() in listed else ("closed", f"not on the Ashby board of {slug} any more")
    site = workday.parse_url(str(url))
    if site is not None and "/job/" in path:
        external = path[path.index("/job/"):]
        r = _get(fetcher, site.detail(external), f"Workday job at {site.tenant}", headers={"Accept": "application/json"})
        if r is None:
            return None
        if r.status_code == 404:
            return "closed", f"not on the Workday site of {site.tenant} any more (HTTP 404)"
        body = _json(r) if r.status_code == 200 else None
        return ("open", f"listed on the Workday site of {site.tenant}") if isinstance(body, dict) and body.get("jobPostingInfo") else None
    return None
