"""Reed Jobseeker API (`careerops discover --reed`). Docs: https://www.reed.co.uk/developers/jobseeker

  GET https://www.reed.co.uk/api/1.0/search?keywords=...&resultsToTake=100&postedByDirectEmployer=true     one page of results
  GET https://www.reed.co.uk/api/1.0/jobs/{jobId}                                                          the whole ad

Auth is HTTP Basic with the API key (REED_API_KEY in .env) as the username and an empty password. The search result carries only a
snippet; the details call has the full description, so each job we keep costs one more request (one per second). The result has no
"agency" field, so every query is run twice, once with postedByDirectEmployer and once with postedByRecruitmentAgency, and the
second run's jobs are flagged as agency posts (kept, and weighted in priority, never dropped). A job whose expirationDate has
passed is closed and skipped. The key is never printed, logged or put in a URL.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterator

from . import freshness
from .candidate import Candidate, posted_date
from .http import FetchError, Fetcher
from .sources import html_to_text

BASE = "https://www.reed.co.uk/api/1.0"
PAGE = 100                         # the documented maximum for resultsToTake


class ReedKeyMissing(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("REED_API_KEY", "").strip()
    if not key:
        raise ReedKeyMissing("REED_API_KEY is not set: add it to .env (see README, 'Getting a Reed API key')")
    return key


def _salary(d: dict) -> str:
    lo, hi, cur = d.get("minimumSalary"), d.get("maximumSalary"), d.get("currency") or "GBP"
    if lo and hi:
        return f"{cur} {lo:,.0f}-{hi:,.0f}"
    one = lo or hi
    return f"{cur} {one:,.0f}" if one else ""


def _iso(value: str | None) -> str:
    dt = posted_date(value)
    return dt.date().isoformat() if dt else ""


def _expired(d: dict, today: datetime) -> bool:
    exp = posted_date(d.get("expirationDate"))
    return bool(exp and exp.date() < today.date())


def search(fetcher: Fetcher, key: str, keywords: str, *, agency: bool, pages: int = 1, location: str | None = None,
           today: datetime | None = None) -> Iterator[dict]:
    """The results of one query: direct employers (agency=False) or recruitment agencies (agency=True), `pages` pages of 100."""
    for page in range(pages):
        params = {"keywords": keywords, "resultsToTake": PAGE, "resultsToSkip": page * PAGE,
                  "postedByRecruitmentAgency" if agency else "postedByDirectEmployer": "true"}
        if location:
            params["locationName"] = location
        data = fetcher.json(f"{BASE}/search", params=params, auth=(key, ""), robots=False, what="Reed search")
        results = (data or {}).get("results", []) if isinstance(data, dict) else []
        yield from results
        if len(results) < PAGE:
            break


def details(fetcher: Fetcher, key: str, job_id) -> dict | None:
    d = fetcher.json(f"{BASE}/jobs/{job_id}", auth=(key, ""), robots=False, what=f"Reed job {job_id}")
    return d if isinstance(d, dict) and (d.get("jobId") or d.get("jobTitle")) else None      # a blank job comes back for an unknown id


def to_candidate(d: dict, agency: bool, today: datetime | None = None) -> Candidate:
    today = today or datetime.now(timezone.utc)
    return Candidate(
        source="reed", company=str(d.get("employerName") or "").strip(), title=str(d.get("jobTitle") or "").strip(),
        location=str(d.get("locationName") or "").strip(), url=str(d.get("jobUrl") or f"https://www.reed.co.uk/jobs/{d.get('jobId')}"),
        description=html_to_text(d.get("jobDescription") or d.get("description")), posted=_iso(d.get("date")),
        posted_source=freshness.label("reed", "date") if _iso(d.get("date")) else "",
        agency=agency, salary=_salary(d), ext_id=str(d.get("jobId") or ""), closed=_expired(d, today),
        country="GB")                                                    # reed.co.uk is a UK site: any town in it is in the UK


def fetch(fetcher: Fetcher, settings, *, want, today: datetime | None = None) -> Iterator[tuple[str, Candidate | str]]:
    """Yield ('candidate', Candidate) for each job `want(candidate_from_search)` accepts, with its full description, and
    ('note', text) for what could not be fetched. `want` is called with the snippet-level Candidate so that jobs the filters
    would drop never cost a details request."""
    cfg = (settings["discover"].get("reed") or {})
    key = api_key()
    seen: set[str] = set()
    budget = int(cfg.get("details_per_run", 60))
    for query in cfg.get("queries") or []:
        for agency in (False, True):
            try:
                for d in search(fetcher, key, query, agency=agency, pages=int(cfg.get("pages_per_query", 1)),
                                location=cfg.get("location")):
                    jid = str(d.get("jobId"))
                    if jid in seen:
                        continue
                    seen.add(jid)
                    snippet = to_candidate(d, agency, today)
                    if not want(snippet):
                        continue
                    if snippet.closed:
                        yield "candidate", snippet                            # the caller counts it as closed and skips it
                        continue
                    if budget <= 0:
                        yield "note", f"Reed: the per-run limit of {cfg.get('details_per_run', 60)} full descriptions is used up"
                        return
                    budget -= 1
                    full = details(fetcher, key, jid)
                    if full is None:
                        yield "note", f"Reed job {jid}: no details returned"
                        continue
                    yield "candidate", to_candidate({**d, **full}, agency, today)
            except FetchError as e:
                yield "note", f"Reed ({query}, {'agency' if agency else 'direct'}): {e}"
                if getattr(e, "status", None):
                    return                                                    # blocked: the host is not asked again


JOB_ID_IN_URL = re.compile(r"reed\.co\.uk/jobs/[^?#]*?/(\d+)(?:[/?#]|$)|reed\.co\.uk/jobs/(\d+)", re.I)


def job_id_from_url(url: str) -> str | None:
    m = JOB_ID_IN_URL.search(url or "")
    return (m.group(1) or m.group(2)) if m else None
