"""Where a job is applied for: `apply_kind` from the apply page's host, and the hosts discover never fetches to resolve an alert.

  greenhouse | lever | ashby   the employer's page on that applicant-tracking system
  workday                      a host ending myworkdayjobs.com or myworkday.com: recorded, never fetched
  employer_other               any other host (the employer's own site, another ATS)
  aggregator_only              a job board or an alert link only: no employer page was found

The kind is read from the host of the URL, except for a posting read off a Greenhouse / Lever / Ashby board, whose kind is the board's
(a Greenhouse job can be shown on the employer's own domain). Nothing here makes a request.
"""
from __future__ import annotations

from urllib.parse import urlparse

KINDS = ("greenhouse", "lever", "ashby", "workday", "employer_other", "aggregator_only")
HOSTS = {"greenhouse": ("greenhouse.io",), "lever": ("lever.co",), "ashby": ("ashbyhq.com",),
         "workday": ("myworkdayjobs.com", "myworkday.com")}
AGGREGATORS = ("linkedin.com", "indeed.com", "indeed.co.uk", "cv-library.co.uk", "reed.co.uk", "glassdoor.com", "glassdoor.co.uk",
               "totaljobs.com", "adzuna.co.uk", "adzuna.com", "welcometothejungle.com", "wellfound.com", "workatastartup.com",
               "jooble.org", "ziprecruiter.com", "monster.co.uk", "jobsite.co.uk", "careerjet.co.uk", "talent.com", "google.com",
               "findajob.dwp.gov.uk")
# Never requested to resolve an alert: LinkedIn and Indeed disallow automation, CV-Library is a job board whose pages are not
# fetched here, and Workday careers sites are recorded, not read.
NEVER_FETCH = ("linkedin.com", "indeed.com", "indeed.co.uk", "cv-library.co.uk", "myworkdayjobs.com", "myworkday.com")


def host_of(url: str | None) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def _under(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def never_fetched(url: str | None) -> bool:
    """True for a LinkedIn, Indeed, CV-Library or Workday address: the resolver asks nothing of these hosts."""
    return _under(host_of(url), NEVER_FETCH)


def kind_of(url: str | None) -> str:
    """The apply_kind of an apply page, from its host; '' when there is no URL."""
    host = host_of(url)
    if not host:
        return ""
    for kind, domains in HOSTS.items():
        if _under(host, domains):
            return kind
    return "aggregator_only" if _under(host, AGGREGATORS) else "employer_other"


def board_kind(ats: str) -> str:
    """The kind of a posting read off a board of this provider (lever_eu is Lever). The v2.6 providers have no form agent: their kind is the employer's page."""
    ats = "lever" if ats.startswith("lever") else ats
    return ats if ats in KINDS else "employer_other"


def line(url: str | None, kind: str | None = None) -> str:
    """'Apply: greenhouse https://...' for the review screen and the final table; says so when there is no page."""
    url = (url or "").strip()
    if not url:
        return "Apply: none (no application URL: apply by hand)"
    return f"Apply: {kind or kind_of(url)} {url}"
