"""Workday coverage: the open jobs of the Workday careers sites you list under `sources.workday: [{company, url}]` in settings.yaml (seeded with nothing).

Every Workday careers site serves a public JSON list for its own page (the page's own scripts call it):

    https://<tenant>.<wdN>.myworkdayjobs.com[/<locale>]/<site>          the careers page you put in settings.yaml
    POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs     {appliedFacets: {}, limit: 20, offset: n, searchText: "<query>"}
    https://<wdN>.myworkdaysite.com[/<locale>]/recruiting/<tenant>/<site>   the same, with the API at https://<wdN>.myworkdaysite.com/wday/cxs/<tenant>/<site>/jobs

Manners (discovery/http.py does them): robots.txt of the tenant host is read before the first request and a tenant that disallows /wday/cxs/ is skipped
(`Blocked`); one request a second per host; at most `max_pages` (10) list pages per tenant per run, shared by the searches; a 401 / 403 / 429 backs the host off.

The list has no description, so a posting that passes the caller's `keep` filter (role family and place) is read once more, from the posting's own
/wday/cxs/.../job/... address (at most MAX_DETAILS a run); one that says "2 Locations" is read first, so its real places decide.

"Posted Today / Yesterday / N Days Ago" become a date (`posted_on`); "Posted 30+ Days Ago" becomes the day 30 days back, which is freshness tier 3 whatever the
real day was. A 422, or a redirect to community.workday.com/maintenance-page or /wday/drs/outage, or a reply that is not the list, is `Transient`: nothing is
cached, the next run asks again, and it is never reported as "zero jobs". Only a real empty list is zero jobs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from .ats import Board, Posting
from .http import Blocked, FetchError, Fetcher
from .sources import html_to_text

LIMIT = 20                                   # postings per page: the size the endpoint serves
MAX_PAGES = 10                               # list requests per tenant per run
MAX_DETAILS = 40                             # posting pages read per tenant per run
DEFAULT_QUERIES = ("data scientist", "machine learning", "AI engineer", "generative AI")      # searchText when an entry names none
OUTAGE = re.compile(r"community\.workday\.com/maintenance-page|/wday/drs/outage", re.I)
REDIRECTS = (301, 302, 303, 307, 308)
HOST_JOBS = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
HOST_SITE = re.compile(r"^wd\d+\.myworkdaysite\.com$")
LOCALE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")
MULTI_PLACE = re.compile(r"^\d+\s+locations?$", re.I)
POSTED = re.compile(r"posted (today|yesterday|(\d+)(\+)? days? ago)")


class WorkdayError(FetchError):
    """The site cannot be read (one line, fit to print)."""


class Transient(WorkdayError):
    """Workday is down or answered something that is not the list: not an empty board. Nothing is kept; the next run asks again."""


# ------------------------------------------------------------------ the address
@dataclass(frozen=True)
class Site:
    host: str
    tenant: str
    site: str
    locale: str = ""
    myworkdaysite: bool = False

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def detail(self, external_path: str) -> str:
        """The JSON of one posting, from the posting's `externalPath` ("/job/London/Data-Scientist_R123")."""
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}{external_path}"

    def public(self, external_path: str) -> str:
        """The page a person opens for the posting."""
        base = f"https://{self.host}/{self.locale or 'en-US'}" + (f"/recruiting/{self.tenant}" if self.myworkdaysite else "")
        return f"{base}/{self.site}{external_path}"

    @property
    def slug(self) -> str:
        """The site as the `slug` of a Board: safe in a cache file name, and enough to rebuild the Site."""
        return "~".join((self.host, self.tenant, quote(self.site, safe=""), self.locale or "-", "s" if self.myworkdaysite else "j"))


def site_from_slug(slug: str) -> Site:
    host, tenant, site, locale, kind = slug.split("~")
    return Site(host, tenant, unquote(site), "" if locale == "-" else locale, kind == "s")


def parse_url(url: str) -> Site | None:
    """The Workday site a careers address names (either host form), or None when it is not one."""
    p = urlparse(str(url or "").strip())
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        return None
    segs = [unquote(s) for s in p.path.split("/") if s]
    locale = ""
    if segs and LOCALE.match(segs[0]):
        locale, segs = segs[0], segs[1:]
    m = HOST_JOBS.match(host)
    if m and segs:
        return Site(host, m.group(1), segs[0], locale)
    if HOST_SITE.match(host) and len(segs) >= 3 and segs[0] == "recruiting":
        return Site(host, segs[1], segs[2], locale, True)
    return None


# ------------------------------------------------------------------ "Posted N Days Ago"
def posted_on(text: object, today: date) -> str | None:
    """YYYY-MM-DD of "Posted Today" / "Posted Yesterday" / "Posted N Days Ago"; "Posted 30+ Days Ago" is dated 30 days back (at least that old: tier 3);
    None for anything else."""
    m = POSTED.fullmatch(" ".join(str(text or "").lower().split()))
    if not m:
        return None
    days = 0 if m.group(1) == "today" else 1 if m.group(1) == "yesterday" else int(m.group(2))
    return (today - timedelta(days=days)).isoformat()


# ------------------------------------------------------------------ the configuration
@dataclass(frozen=True)
class Entry:
    company: str
    url: str
    site: Site
    queries: tuple[str, ...] = DEFAULT_QUERIES
    max_pages: int = MAX_PAGES

    def board(self) -> Board:
        return Board("workday", self.site.slug, self.company, "config")


def _rows(settings) -> list:
    rows = (settings["sources"] or {}).get("workday") or []
    return rows if isinstance(rows, list) else []


def entries(settings) -> list[Entry]:
    """The valid entries of `sources.workday`; an entry that is not understood is left out (config_problems says why)."""
    out = []
    for e in _rows(settings):
        if not isinstance(e, dict):
            continue
        company, site = str(e.get("company") or "").strip(), parse_url(str(e.get("url") or ""))
        if not company or site is None:
            continue
        q = e.get("queries")
        queries = tuple(str(x) for x in q if str(x).strip()) if isinstance(q, list) and q else DEFAULT_QUERIES
        pages = e.get("max_pages")
        out.append(Entry(company, str(e["url"]), site, queries, int(pages) if isinstance(pages, int) and not isinstance(pages, bool) and pages > 0 else MAX_PAGES))
    return out


def config_problems(settings) -> list[str]:
    """One line for each `sources.workday` entry that is not understood."""
    out = []
    rows = (settings["sources"] or {}).get("workday")
    if rows is not None and not isinstance(rows, list):
        return ["sources.workday must be a list of {company, url} entries"]
    for n, e in enumerate(_rows(settings), 1):
        if not isinstance(e, dict):
            out.append(f"sources.workday entry {n} is not a {{company, url}} mapping")
        elif not str(e.get("company") or "").strip():
            out.append(f"sources.workday entry {n} has no company")
        elif parse_url(str(e.get("url") or "")) is None:
            out.append(f"sources.workday entry {n} ({e.get('company')}): {e.get('url') or 'no url'} is not a Workday careers address "
                       "(https://<tenant>.<wdN>.myworkdayjobs.com/<site> or https://<wdN>.myworkdaysite.com/recruiting/<tenant>/<site>)")
    return out


def options_for(settings, board: Board) -> dict:
    """The queries and page cap of the entry a board came from."""
    for e in entries(settings):
        if e.site.slug == board.slug:
            return {"queries": e.queries, "max_pages": e.max_pages}
    return {}


# ------------------------------------------------------------------ asking
def _call(fetcher: Fetcher, url: str, what: str, body: dict | None = None) -> Any:
    """The parsed JSON of a POST (body given) or GET. Transient for an outage or a reply that is not JSON; WorkdayError for a wrong address."""
    r = fetcher.request(url, what=what, method="POST" if body is not None else "GET", json=body, follow=False,
                        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})})
    status = r.status_code
    if status in REDIRECTS:
        where = str((getattr(r, "headers", None) or {}).get("Location") or "")
        if OUTAGE.search(where):
            raise Transient(f"{what}: Workday answered with its maintenance / outage page; nothing was read, retried next run")
        raise WorkdayError(f"{what}: redirected to {where[:80] or 'somewhere else'}: check the address under sources.workday")
    if status == 422:
        raise Transient(f"{what}: HTTP 422 (Workday cannot answer now); nothing was read, retried next run")
    if status == 404:
        raise WorkdayError(f"{what}: HTTP 404: no such Workday site, check the address under sources.workday")
    if status >= 400:
        raise WorkdayError(f"{what}: HTTP {status}")
    try:
        return json.loads(r.text)
    except (ValueError, TypeError):
        raise Transient(f"{what}: the reply was not JSON (a maintenance page?); nothing was read, retried next run") from None


def _list_page(fetcher: Fetcher, site: Site, query: str, offset: int, what: str) -> list[dict]:
    data = _call(fetcher, site.api, what, {"appliedFacets": {}, "limit": LIMIT, "offset": offset, "searchText": query})
    rows = data.get("jobPostings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise Transient(f"{what}: the reply had no list of jobs (an outage page?); nothing was read, retried next run")
    return [r for r in rows if isinstance(r, dict) and r.get("externalPath")]


def _detail(fetcher: Fetcher, site: Site, path: str, what: str) -> dict | None:
    """jobPostingInfo of one posting; None when it cannot be read (robots.txt, a status, an outage): the posting then has no description."""
    try:
        data = _call(fetcher, site.detail(path), what)
    except (Blocked, FetchError):
        return None
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    return info if isinstance(info, dict) else None


def board_postings(board: Board, fetcher: Fetcher, *, queries: tuple[str, ...] | list[str] | None = None, max_pages: int = MAX_PAGES,
                   max_details: int = MAX_DETAILS, keep: Callable[[Posting], bool] | None = None, today: date | None = None) -> list[Posting]:
    """The open postings of one Workday site, as Postings. `keep(posting)` (role family, place) decides which ones are read in full; the others are left out.
    Raises Blocked (robots.txt disallows /wday/cxs/, or the host refused), Transient (an outage) or FetchError; never returns [] for a failure."""
    site = site_from_slug(board.slug)
    today = today or datetime.now(timezone.utc).date()
    what = f"Workday {board.name} ({site.host})"
    queries = tuple(queries) if queries else DEFAULT_QUERIES
    base, extra = divmod(max_pages, len(queries))
    found: dict[str, dict] = {}
    try:
        for i, query in enumerate(queries):
            pages = base + (1 if i < extra else 0)
            for page in range(pages):
                rows = _list_page(fetcher, site, query, page * LIMIT, what)
                for row in rows:
                    found.setdefault(str(row["externalPath"]), row)
                if len(rows) < LIMIT:
                    break
    except Blocked as e:
        raise Blocked(f"{what}: skipped, {e}", getattr(e, "status", None)) from None
    details = {"left": max_details}
    out: list[Posting] = []
    for path, row in found.items():
        title, place = str(row.get("title") or ""), str(row.get("locationsText") or "")
        bullets = [str(b) for b in row.get("bulletFields") or []]
        post = Posting(bullets[0] if bullets else path.rsplit("_", 1)[-1], board.name, title, place, site.public(path), "",
                       posted_on(row.get("postedOn"), today))
        info = None
        if MULTI_PLACE.match(place) and details["left"] > 0:           # "2 Locations": the real places decide
            details["left"] -= 1
            info = _detail(fetcher, site, path, what)
            if info:
                places = [str(info.get("location") or ""), *[str(x) for x in info.get("additionalLocations") or []]]
                post.location = "; ".join(x for x in places if x) or place
        if keep is not None and not keep(post):
            continue
        if info is None and details["left"] > 0:
            details["left"] -= 1
            info = _detail(fetcher, site, path, what)
        if info:
            post.description = html_to_text(info.get("jobDescription"))
            post.country = str((info.get("country") or {}).get("descriptor") or "") if isinstance(info.get("country"), dict) else ""
            post.ext_id = str(info.get("jobReqId") or post.ext_id)
            post.url = str(info.get("externalUrl") or post.url)
            if post.posted_at is None:
                post.posted_at = posted_on(info.get("postedOn"), today)
        out.append(post)
    return out
