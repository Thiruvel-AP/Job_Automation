"""URL intake (`careerops add-url`, `careerops live --url`): fetch ONE job ad and write it to data/jds/inbox/ as a JD file.
A site that answers 401 / 403 / 429 is not retried or worked around: `live --clipboard` takes the text you copied instead.

The file starts with `Company:` / `Title:` / `URL:` / `Location:` header lines (the ones `manual.header_fields` reads), then
the ad's text. Nothing here adds a job to the database or applies to anything: `careerops live` does that with you at every
gate. Public, unauthenticated APIs only, at most one request per second per host, identified by the shared `UA` header.

  Greenhouse  GET boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}        (job-boards.greenhouse.io, boards.greenhouse.io, and the EU region's
              job-boards.eu.greenhouse.io / boards.eu.greenhouse.io: there is NO boards-api.eu host (it does not resolve); the one API serves both
              regions and the job's `absolute_url` names the EU page. Checked against the live API on 3 Nov 2026 with RightShip's EU board.)
              GET boards-api.greenhouse.io/v1/boards/{board}                  (the company's `name`, when the job reply has no `company_name`)
  Lever       GET api.lever.co/v0/postings/{site}/{id}  (api.eu.lever.co for jobs.eu.lever.co)  - documented single posting
  Ashby       the board list GET api.ashbyhq.com/posting-api/job-board/{board}, then the posting in it by its id. Ashby
              documents no single-posting endpoint, so this is the one documented call, filtered here.
  CV-Library  https://www.cv-library.co.uk/job/apply/{id}... is rewritten to https://www.cv-library.co.uk/job/{id}; title,
              company / agency, location, salary and the description block only (JSON-LD JobPosting, else the page's own
              description element; never the "similar jobs" sidebar). A CV-Library search or query URL is never fetched.
  otherwise   the page itself: a JobPosting JSON-LD block if it has one, else the page's main text. Under MIN_PAGE_CHARS
              characters is refused rather than guessed at (a page that builds its text with JavaScript, a login wall).

Before ANY fetch the host's robots.txt is read and cached for 24 hours (robots.py); a disallowed path is refused with a message
that says so, and redirects are followed by hand so that every hop is checked too. linkedin.com is never fetched.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from . import freshness, manual
from .robots import RobotsCache, RobotsError, never_fetch
from .sources import UA, html_to_text

MIN_PAGE_CHARS = 2000                  # a page with less text than this is not taken for a job description
MIN_HOST_GAP_S = 1.0                   # at most one request per second per host
MAX_BYTES = 5_000_000                  # a bigger response is not a job ad
GREENHOUSE_API = "boards-api.greenhouse.io"
GREENHOUSE_HOSTS = {"job-boards.greenhouse.io": GREENHOUSE_API, "boards.greenhouse.io": GREENHOUSE_API,
                    "job-boards.eu.greenhouse.io": GREENHOUSE_API, "boards.eu.greenhouse.io": GREENHOUSE_API}   # board host -> its API host (one API for both regions)
LEVER_HOSTS = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}
ASHBY_HOSTS = ("jobs.ashbyhq.com",)
CVLIBRARY_HOSTS = ("www.cv-library.co.uk", "cv-library.co.uk")
CVLIBRARY_JOB = re.compile(r"/job/(?:apply/)?(\d+)(?:/[^/?#]*)?/?")        # /job/123, /job/123/slug, /job/apply/123[/slug]
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
AGENCY_WORDS = re.compile(r"\b(?:recruit\w*|staffing|resourcing|headhunt\w*|executive search|talent (?:partners?|solutions?|acquisition)|"
                          r"personnel|appointments|search (?:&|and) selection)\b", re.I)
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


BLOCKED_STATUSES = (401, 403, 429)     # the site says no to a script: no retry, no workaround, the human pastes the text
BLOCKED_MESSAGE = "This site blocks automated access — copy the job text and use --clipboard"
CLIPBOARD_CMD = ["powershell.exe", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard"]


class IntakeError(ValueError):
    """The ad could not be fetched or is not usable. The message is one line, fit to print as it is."""


class BlockedError(IntakeError):
    """The site answered 401 / 403 / 429. It is never retried or worked around."""


class RobotsRefused(IntakeError):
    """robots.txt disallows the path (or cannot be checked), or the host is one this tool never contacts."""


@dataclass
class Target:
    kind: str                          # greenhouse | lever | ashby | cvlibrary | page
    url: str
    board: str = ""
    ad_id: str = ""
    api_host: str = ""


@dataclass
class Ad:
    company: str
    title: str
    url: str
    location: str
    text: str
    source: str
    ad_id: str
    salary: str = ""
    agency: bool = False               # the poster looks like a recruitment agency, not the employer
    posted: str = ""                   # ISO date or datetime the ad was posted, when the source says (freshness.py)
    posted_source: str = ""            # where `posted` comes from, one of freshness.TRUSTED ("greenhouse.first_published"); "" = not trusted
    country: str = ""                  # set only for a job outside the UK: written as `Country:` (live refuses it without a visa block)
    listed_only: bool = False          # written as `Status: listed_only`
    first_seen: str = ""               # YYYY-MM-DD discover first saw the job (an alert's date or the first run): what R counts from
    open_listing: bool = False         # written as `Listed: yes`: the job is on the employer's own board (R never falls under the floor)
    apply_url: str = ""                # written as `Apply URL:` when it is not the `URL:` itself
    apply_kind: str = ""               # written as `Apply kind:` (discovery/applykind.py)
    linkedin_job_id: str = ""          # v3.8: written as `LinkedIn job id:` (with `Found via: linkedin_email`) for a job found through a Gmail label card


# ------------------------------------------------------------------ network (tests replace _get / _sleep / _clock)
_last: dict[str, float] = {}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _throttle(host: str) -> None:
    wait = _last.get(host, -MIN_HOST_GAP_S) + MIN_HOST_GAP_S - _clock()
    if wait > 0:
        _sleep(wait)
    _last[host] = _clock()


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=60, allow_redirects=False, **kw)      # redirects are followed by _fetch, hop by hop
    return r


_default_robots = RobotsCache(None)           # when no cache is given (tests, library use): in memory, for this process


def _robots_get(url: str) -> tuple[int | None, str]:
    """The one request for a robots.txt: paced and identified like every other; (None, "") when there is no answer."""
    _throttle(urlparse(url).netloc.lower())
    try:
        r = _get(url)
    except requests.RequestException:
        return None, ""
    return r.status_code, (r.text[:500_000] if r.ok else "")


def _fetch(url: str, what: str, robots: RobotsCache | None = None) -> requests.Response:
    """One GET, paced per host and checked against the host's robots.txt first (and so is every redirect hop). Any failure
    becomes an IntakeError saying what could not be fetched."""
    robots = robots or _default_robots
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            robots.check(url, _robots_get)
        except RobotsError as e:
            raise RobotsRefused(str(e)) from None
        _throttle(urlparse(url).netloc.lower())
        try:
            r = _get(url)
        except requests.RequestException as e:
            raise IntakeError(f"{what}: could not be fetched ({type(e).__name__}: {e})") from None
        where = (getattr(r, "headers", None) or {}).get("Location") if r.status_code in REDIRECT_STATUSES else None
        if not where:
            break
        url = urljoin(url, where)
        if urlparse(url).scheme not in ("http", "https"):
            raise IntakeError(f"{what}: redirected to something that is not a web link")
    else:
        raise IntakeError(f"{what}: too many redirects")
    if r.status_code in BLOCKED_STATUSES:
        raise BlockedError(BLOCKED_MESSAGE)
    if r.status_code == 404:
        raise IntakeError(f"{what}: not found (HTTP 404): the posting is closed, or the link is wrong")
    if not r.ok:
        raise IntakeError(f"{what}: HTTP {r.status_code}")
    if len(r.content or b"") > MAX_BYTES:
        raise IntakeError(f"{what}: the response is over {MAX_BYTES // 1_000_000} MB; that is not a job ad")
    return r


def _json(url: str, what: str, robots: RobotsCache | None = None):
    r = _fetch(url, what, robots)
    try:
        return r.json()
    except ValueError:
        raise IntakeError(f"{what}: the reply was not JSON") from None


# ------------------------------------------------------------------ url -> target
def parse_url(url: str) -> Target:
    """Which kind of job page this is. Anything that is not a http(s) link raises IntakeError."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise IntakeError(f"not a web link: {url!r} (it must start with http:// or https://)")
    host, parts = u.netloc.lower().split(":")[0], [p for p in u.path.split("/") if p]
    full = u.geturl()
    if never_fetch(full):
        raise RobotsRefused(never_fetch(full))
    if host in CVLIBRARY_HOSTS:
        m = CVLIBRARY_JOB.fullmatch(u.path)
        if not m:
            raise IntakeError(f"{full}: only a single CV-Library job link (cv-library.co.uk/job/<number>) is fetched; its "
                              "search and query pages are never fetched (robots.txt disallows them). Open the job and use "
                              "its own link, or copy the text and use --clipboard")
        return Target("cvlibrary", f"https://www.cv-library.co.uk/job/{m.group(1)}", ad_id=m.group(1))      # no apply/, no query
    if host in GREENHOUSE_HOSTS:
        q = parse_qs(u.query)
        if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
            return Target("greenhouse", full, board=parts[0], ad_id=parts[2], api_host=GREENHOUSE_HOSTS[host])
        if parts[:2] == ["embed", "job_app"] and q.get("for") and (q.get("token") or [""])[0].isdigit():
            return Target("greenhouse", full, board=q["for"][0], ad_id=q["token"][0], api_host=GREENHOUSE_HOSTS[host])
        raise IntakeError(f"{full}: a Greenhouse link needs the form {host}/<board>/jobs/<number>")
    if host in LEVER_HOSTS:
        if len(parts) >= 2 and UUID.fullmatch(parts[1]):
            return Target("lever", full, board=parts[0], ad_id=parts[1].lower(), api_host=LEVER_HOSTS[host])
        raise IntakeError(f"{full}: a Lever link needs the form {host}/<site>/<posting id>")
    if host in ASHBY_HOSTS:
        if len(parts) >= 2 and UUID.fullmatch(parts[1]):
            return Target("ashby", full, board=parts[0], ad_id=parts[1].lower())
        raise IntakeError(f"{full}: an Ashby link needs the form {host}/<board>/<posting id>")
    return Target("page", full, ad_id=hashlib.sha1(full.encode()).hexdigest()[:10])


def _name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def _http_url(candidate, fallback: str) -> str:
    c = str(candidate or "").strip()
    return c if urlparse(c).scheme in ("http", "https") else fallback


# ------------------------------------------------------------------ the APIs
def _first_published(j: dict) -> str:
    """Greenhouse's first_published (never updated_at: it resets when a job is reposted), or "" when it is missing."""
    v = str(j.get("first_published") or "").strip()
    return v if freshness.is_iso(v) else ""


def _board_name(t: Target, robots: RobotsCache | None = None) -> str:
    """The company's name from the board API (GET /v1/boards/{board} -> {"name": ...}), for a job reply that has no `company_name`. A board that
    does not answer is not an error here ("": the caller uses the board's slug); a refusal is never worked around."""
    try:
        j = _json(f"https://{t.api_host or GREENHOUSE_API}/v1/boards/{t.board}", f"Greenhouse board {t.board}", robots)
    except BlockedError:
        raise
    except RobotsRefused:
        raise
    except IntakeError:
        return ""
    return str(j.get("name") or "").strip() if isinstance(j, dict) else ""


def greenhouse_ad(t: Target, robots: RobotsCache | None = None) -> Ad:
    j = _json(f"https://{t.api_host or GREENHOUSE_API}/v1/boards/{t.board}/jobs/{t.ad_id}", f"Greenhouse job {t.board}/{t.ad_id}", robots)
    company = str(j.get("company_name") or "").strip() or _board_name(t, robots) or _name(t.board)
    return Ad(company=company, title=str(j.get("title") or "").strip(),
              url=_http_url(j.get("absolute_url"), t.url), location=str((j.get("location") or {}).get("name") or "").strip(),
              text=html_to_text(j.get("content")), source="greenhouse", ad_id=t.ad_id,
              posted=_first_published(j), posted_source=freshness.label("greenhouse", "first_published") if _first_published(j) else "")


def lever_ad(t: Target, robots: RobotsCache | None = None) -> Ad:
    j = _json(f"https://{t.api_host}/v0/postings/{t.board}/{t.ad_id}", f"Lever posting {t.board}/{t.ad_id}", robots)
    parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
    for lst in j.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
    parts.append(j.get("additionalPlain") or "")
    return Ad(company=_name(t.board), title=str(j.get("text") or "").strip(), url=_http_url(j.get("hostedUrl"), t.url),
              location=str((j.get("categories") or {}).get("location") or "").strip(),
              text="\n\n".join(p for p in parts if p).strip(), source="lever", ad_id=t.ad_id,
              posted=freshness.from_millis(j.get("createdAt")) or "",
              posted_source=freshness.label("lever", "createdAt") if freshness.from_millis(j.get("createdAt")) else "")


def ashby_ad(t: Target, robots: RobotsCache | None = None) -> Ad:
    data = _json(f"https://api.ashbyhq.com/posting-api/job-board/{t.board}", f"Ashby board {t.board}", robots)
    for j in data.get("jobs") or []:
        if (str(j.get("id") or "").lower() == t.ad_id or t.ad_id in str(j.get("jobUrl") or "").lower()
                or t.ad_id in str(j.get("applyUrl") or "").lower()):
            loc = j.get("location") or ""
            return Ad(company=_name(t.board), title=str(j.get("title") or "").strip(),
                      url=_http_url(j.get("jobUrl"), t.url), location=str(loc.get("name") if isinstance(loc, dict) else loc).strip(),
                      text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), source="ashby", ad_id=t.ad_id,
                      posted=str(j.get("publishedAt") or "") if freshness.is_iso(j.get("publishedAt")) else "",
                      posted_source=freshness.label("ashby", "publishedAt") if freshness.is_iso(j.get("publishedAt")) else "")
    raise IntakeError(f"Ashby board {t.board}: no posting {t.ad_id} in its public list (closed, or not listed)")


# ------------------------------------------------------------------ any other page
class _Page(HTMLParser):
    """Visible text of a page, minus scripts, styles and page furniture; also the text inside <main>/<article>, the
    <title>, and the JSON-LD blocks."""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.keep = 0                       # depth inside <main> / <article>
        self.all: list[str] = []
        self.main: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self.ld: list[str] = []
        self._in_title = False
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("property") or a.get("name")) and a.get("content"):
            self.meta.setdefault((a.get("property") or a.get("name")).lower(), a["content"])
        elif tag == "script" and "ld+json" in (a.get("type") or "").lower():
            self._in_ld = True
            self.ld.append("")
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in ("main", "article"):
            self.keep += 1
        if self.skip == 0:
            mark = "\n- " if tag == "li" else "\n" if tag in self.BLOCK else ""
            if mark:
                self.all.append(mark)
                if self.keep:
                    self.main.append(mark)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_ld = False
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in ("main", "article") and self.keep:
            self.keep -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_ld:
            self.ld[-1] += data
        elif self.skip == 0:
            self.all.append(data)
            if self.keep:
                self.main.append(data)


def _tidy(chunks: list[str]) -> str:
    t = re.sub(r"[ \t\xa0]+", " ", "".join(chunks))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", t)).strip()


def _job_posting(blocks: list[str]) -> dict:
    """The first schema.org JobPosting in the page's JSON-LD blocks ({} when there is none)."""
    def walk(node):
        if isinstance(node, list):
            for n in node:
                yield from walk(n)
        elif isinstance(node, dict):
            kinds = node.get("@type")
            if "JobPosting" in (kinds if isinstance(kinds, list) else [kinds]):
                yield node
            for v in node.values():
                if isinstance(v, (list, dict)):
                    yield from walk(v)
    for raw in blocks:
        try:
            for node in walk(json.loads(raw)):
                return node
        except ValueError:
            continue
    return {}


def _ld_location(jp: dict) -> str:
    loc = jp.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    addr = (loc or {}).get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        return ", ".join(str(addr[k]) for k in ("addressLocality", "addressRegion", "addressCountry")
                         if isinstance(addr.get(k), (str, int)) and str(addr[k]).strip())
    return ""


def page_ad(t: Target, robots: RobotsCache | None = None) -> Ad:
    r = _fetch(t.url, t.url, robots)
    p = _Page()
    p.feed(r.text)
    jp = _job_posting(p.ld)
    from_ld = html_to_text(jp.get("description")) if isinstance(jp.get("description"), str) else ""
    main, everything = _tidy(p.main), _tidy(p.all)
    text = next((c for c in (from_ld, main, everything) if len(c) >= MIN_PAGE_CHARS), max((from_ld, main, everything), key=len))
    if len(text) < MIN_PAGE_CHARS:
        raise IntakeError(f"{t.url}: only {len(text)} characters of text found (a job description needs at least "
                          f"{MIN_PAGE_CHARS}); the page probably builds its text with JavaScript or needs a login. "
                          "Save the ad as a .txt file by hand instead.")
    org = jp.get("hiringOrganization")
    company = (org.get("name") if isinstance(org, dict) else org) or p.meta.get("og:site_name") or ""
    title = jp.get("title") or p.meta.get("og:title") or " ".join(p.title.split())
    return Ad(company=str(company).strip(), title=str(title).strip(), url=t.url, location=_ld_location(jp), text=text,
              source="page", ad_id=t.ad_id)


# ------------------------------------------------------------------ CV-Library
class _Fields(HTMLParser):
    """The first element of a page for each field: title (<h1>), company, location, salary and the description block, found by
    the words in their class / id / itemprop. Inside the description, sub-elements that are not the ad ("similar jobs",
    "related", "recommended", sidebars, share and apply buttons) are skipped. One element is read at a time."""
    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "col", "embed", "param", "track"}
    BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}
    MATCH = {"company": re.compile(r"hiring[-_ ]?organi[sz]ation|company|employer|recruiter|posted[-_ ]?by|agency", re.I),
             "location": re.compile(r"location", re.I), "salary": re.compile(r"salary", re.I),
             "description": re.compile(r"job[-_ ]*description|itemprop=description", re.I)}
    NOT_THE_AD = re.compile(r"similar|related|recommended|sidebar|more[-_ ]jobs|you[-_ ]may|share|apply[-_ ]?(?:btn|button)|alert",
                            re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: dict[str, list[str]] = {}
        self.done: set[str] = set()
        self.key: str | None = None
        self.depth = 0
        self.skip = 0                     # depth inside a not-the-ad sub-element of the description

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        label = " ".join([a.get("class") or "", a.get("id") or "", f"itemprop={a.get('itemprop')}" if a.get("itemprop") else ""])
        if self.key is None:
            key = "title" if tag == "h1" and "title" not in self.done else next(
                (k for k, rx in self.MATCH.items() if k not in self.done and rx.search(label)), None)
            if key and tag not in self.VOID:
                self.key, self.depth = key, 1
                self.out[key] = []
            return
        if tag in self.VOID:
            if tag == "br" and not self.skip:
                self.out[self.key].append("\n")
            return
        self.depth += 1
        if self.skip:
            self.skip += 1
        elif self.key == "description" and self.NOT_THE_AD.search(label):
            self.skip = 1
        elif tag in self.BLOCK:
            self.out[self.key].append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if self.key is None or tag in self.VOID:
            return
        self.depth -= 1
        if self.skip:
            self.skip -= 1
        if self.depth <= 0:
            self.done.add(self.key)
            self.key, self.depth, self.skip = None, 0, 0

    def handle_data(self, data):
        if self.key and not self.skip:
            self.out[self.key].append(data)

    def get(self, key: str) -> str:
        return _tidy(self.out.get(key, []))


def looks_like_agency(name: str) -> bool:
    return bool(AGENCY_WORDS.search(name or ""))


def _num(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _ld_salary(jp: dict) -> str:
    """"GBP 50,000-60,000 per year" from a JobPosting's baseSalary, or "" when it names none."""
    base = jp.get("baseSalary")
    if not isinstance(base, dict):
        return ""
    value = base.get("value") if isinstance(base.get("value"), dict) else base
    lo, hi, one = value.get("minValue"), value.get("maxValue"), value.get("value")
    if lo is not None and hi is not None:
        amount = f"{_num(lo)}-{_num(hi)}"
    else:
        pick = next((v for v in (one, lo, hi) if v is not None and not isinstance(v, dict)), None)
        amount = _num(pick) if pick is not None else ""
    if not amount:
        return ""
    unit = str(value.get("unitText") or "").lower()
    return " ".join(x for x in (str(base.get("currency") or value.get("currency") or "").strip(), amount,
                                f"per {unit}" if unit else "") if x)


def cvlibrary_ad(t: Target, robots: RobotsCache | None = None) -> Ad:
    """One CV-Library job page (always cv-library.co.uk/job/<id>): title, company or agency, location, salary and the description
    block only. JSON-LD JobPosting first (what the page gives search engines), the page's own elements for what it lacks."""
    r = _fetch(t.url, t.url, robots)
    p = _Page()
    p.feed(r.text)
    f = _Fields()
    f.feed(r.text)
    jp = _job_posting(p.ld)
    org = jp.get("hiringOrganization")
    company = str((org.get("name") if isinstance(org, dict) else org) or f.get("company") or "").strip()
    title = str(jp.get("title") or f.get("title") or p.meta.get("og:title") or " ".join(p.title.split())).strip()
    desc = html_to_text(jp.get("description")) if isinstance(jp.get("description"), str) else ""
    if len(desc) < manual.MIN_JD_BYTES:
        desc = f.get("description")
    if len(desc) < manual.MIN_JD_BYTES:
        raise IntakeError(f"{t.url}: the description block was not found or is under {manual.MIN_JD_BYTES} characters; copy the "
                          "job text and use --clipboard")
    posted = str(jp.get("datePosted") or "")[:10]
    return Ad(company=company, title=title, url=t.url, location=_ld_location(jp) or f.get("location"), text=desc,
              source="cvlibrary", ad_id=t.ad_id, salary=_ld_salary(jp) or f.get("salary"), agency=looks_like_agency(company),
              posted=posted if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted) else "")


# ------------------------------------------------------------------ the entry points
def fetch_ad(url: str, company: str | None = None, title: str | None = None, robots: RobotsCache | None = None) -> Ad:
    """Fetch the ad behind `url`. `company` / `title` replace what the page or API says. The host's robots.txt is read first
    (`robots`: the cache to use; default one in memory)."""
    t = parse_url(url)
    # An EU board (job-boards.eu.greenhouse.io) is read from the same API as a US one. The earlier guess of a boards-api.eu host fell back to the
    # board's page, a script-built page with no company or title; a refusal or a 404 is an error, never worked around.
    ad = {"greenhouse": greenhouse_ad, "lever": lever_ad, "ashby": ashby_ad, "cvlibrary": cvlibrary_ad, "page": page_ad}[t.kind](t, robots)
    if company:
        ad.company = company.strip()
    if title:
        ad.title = title.strip()
    if len(ad.text.encode("utf-8")) < manual.MIN_JD_BYTES:
        raise IntakeError(f"{url}: the ad has only {len(ad.text)} characters of text (a job description needs at least "
                          f"{manual.MIN_JD_BYTES} bytes)")
    return ad


def slug(text: str, limit: int = 40) -> str:
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:limit].strip("-")


def file_name(ad: Ad) -> str:
    return "-".join(p for p in (slug(ad.company), slug(ad.title), slug(ad.ad_id, 36)) if p) + ".txt"


def render(ad: Ad) -> str:
    one_line = lambda s: " ".join(str(s).split())          # noqa: E731 — a header value is one line
    head = [f"Company: {one_line(ad.company)}", f"Title: {one_line(ad.title)}", f"URL: {ad.url}", f"Source: {one_line(ad.source)}"]
    if ad.apply_kind:
        head.append(f"Apply kind: {one_line(ad.apply_kind)}")
    if ad.apply_url and ad.apply_url != ad.url:
        head.append(f"Apply URL: {one_line(ad.apply_url)}")
    if one_line(ad.location):
        head.append(f"Location: {one_line(ad.location)}")
    if one_line(ad.country):
        head.append(f"Country: {one_line(ad.country)}")
    if ad.listed_only:
        head.append("Status: listed_only")
    if one_line(ad.salary):
        head.append(f"Salary: {one_line(ad.salary)}")
    if ad.posted:
        head.append(f"Posted: {ad.posted}")
        if ad.posted_source:
            head.append(f"Posted via: {ad.posted_source}")
    if ad.first_seen:
        head.append(f"First seen: {ad.first_seen}")
    if ad.open_listing:
        head.append("Listed: yes")
    if ad.agency:
        head.append("Agency: yes")
    if ad.linkedin_job_id:
        head += ["Found via: linkedin_email", f"LinkedIn job id: {one_line(ad.linkedin_job_id)}"]
    return "\n".join(head) + "\n\n" + ad.text.strip() + "\n"


def write_inbox(inbox: Path, ad: Ad) -> tuple[Path, bool]:
    """Write the ad to <inbox>/<company>-<title>-<id>.txt and return (path, created). An existing file with that name is
    left exactly as it is (created=False): a fetch never overwrites. The headers must pass the same checks `careerops live`
    makes, so a file written here is never one that live would reject for its headers."""
    for field, value in (("company", ad.company), ("title", ad.title), ("url", ad.url)):
        if not value.strip():
            raise IntakeError(f"the {field} could not be read from {ad.url}: pass --company and --title")
        problem = manual.meta_problem(field, value)
        if problem:
            raise IntakeError(f"{problem} (from {ad.url}); pass --company and --title to set it yourself")
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / file_name(ad)
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(render(ad))
    except FileExistsError:
        return path, False
    return path, True


# ------------------------------------------------------------------ the Windows clipboard (from WSL)
def _run_clipboard() -> bytes:
    """Raw stdout of powershell.exe's Get-Clipboard (tests replace this)."""
    try:
        p = subprocess.run(CLIPBOARD_CMD, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise IntakeError("powershell.exe was not found: --clipboard reads the Windows clipboard from WSL") from None
    except subprocess.SubprocessError as e:
        raise IntakeError(f"the clipboard could not be read ({type(e).__name__})") from None
    if p.returncode != 0:
        raise IntakeError(f"the clipboard could not be read (powershell.exe exited {p.returncode})")
    return p.stdout


# What a command copied by mistake starts with (the clipboard has held `careerops live --clipboard` itself). The names no job
# ad starts with are matched in any case; `cd`, `git` and `source` are ordinary words, so only the lowercase spelling a shell
# takes counts ("Source Digital is hiring ..." is an ad).
SHELL_ANY_CASE = ("careerops", "powershell", "powershell.exe", "explorer", "explorer.exe")
SHELL_LOWER_ONLY = ("cd", "git", "source")
_PROMPT_MARK = re.compile(r"^(?:PS [^>\n]*>|[$>#%]+)\s*")


def shell_command_start(text: str) -> str | None:
    """The command word the text starts with (careerops, powershell, explorer, cd, git, source), or None when it does not
    look like a shell command. A prompt mark ($, >, PS C:\\...>) before it is ignored."""
    words = _PROMPT_MARK.sub("", text.lstrip()).split(None, 1)
    word = words[0] if words else ""
    return word if word.lower() in SHELL_ANY_CASE or word in SHELL_LOWER_ONLY else None


def read_clipboard() -> str:
    """The clipboard's text: UTF-8, \\r removed, trimmed. Under MIN_PAGE_CHARS characters is refused (a snippet, or
    something else on the clipboard), and so is text that starts like a shell command (see shell_command_start)."""
    text = _run_clipboard().decode("utf-8", errors="replace").removeprefix(chr(0xFEFF)).replace("\r", "").strip()
    if len(text) < MIN_PAGE_CHARS:
        raise IntakeError(f"the clipboard holds {len(text)} characters of text (a job description needs at least "
                          f"{MIN_PAGE_CHARS}): copy the whole job ad, then run this again")
    word = shell_command_start(text)
    if word:
        raise IntakeError(f"the clipboard starts with '{word}': that looks like a shell command, not a job ad. Copy the job "
                          "ad itself, then run this again")
    return text


def preview(text: str, lines: int = 3, width: int = 110) -> str:
    """The character count and the first `lines` non-empty lines of the clipboard text, each cut to `width` characters."""
    head = [ln.strip() for ln in text.splitlines() if ln.strip()][:lines]
    return f"{len(text):,} characters. First lines:\n" + "\n".join("  " + (h if len(h) <= width else h[: width - 1] + "…")
                                                                  for h in head)


def _ask_field(ask: Callable[[str], str], field: str, prompt: str) -> str:
    """Ask until the answer passes `manual.meta_problem` (and, for a URL, starts with http:// or https://). Three bad or
    blank answers in a row raise IntakeError, so nothing is written for a job with no usable company, title or link."""
    for _ in range(3):
        answer = ask(prompt).strip()
        problem = manual.meta_problem(field, answer) if answer else f"{field} is blank"
        if not problem and field == "url" and urlparse(answer).scheme not in ("http", "https"):
            problem = f"url {answer[:60]!r} must start with http:// or https://"
        if not problem:
            return answer
        print(f"  {problem}; try again.")
    raise IntakeError(f"no usable {field} given; nothing was written")


def add_clipboard(settings, ask: Callable[[str], str] = input) -> tuple[Path, bool, Ad]:
    """Wait for you to copy the ad ("Copy the job ad now, then press Enter"), read the clipboard, show its size and first three
    lines and ask "Use this? [y/N]", then ask for Company / Title / URL and put the ad in paths.jds/inbox as
    <company>-<title>.txt. Anything but y at the question writes nothing. Returns (file, created, ad); an existing file of that
    name is left exactly as it is (created=False)."""
    print("Copy the job ad now, then press Enter")
    ask("")
    text = read_clipboard()
    print(preview(text))
    if ask("Use this? [y/N] ").strip().lower() not in ("y", "yes"):
        raise IntakeError("clipboard text not used; nothing was written")
    company = _ask_field(ask, "company", "Company: ")
    title = _ask_field(ask, "title", "Job title: ")
    url = _ask_field(ask, "url", "Application URL: ")
    ad = Ad(company=company, title=title, url=url, location="", text=text, source="clipboard", ad_id="")
    refuse_decided(settings, ad)
    path, created = write_inbox(settings.path("jds") / "inbox", ad)
    return path, created, ad


def filed_in_approved(jds: Path, name: str) -> Path | None:
    """The file called `name` in an approved/<date>/ folder of paths.jds (the newest day first), or None: an ad you approved is already filed there."""
    approved = jds / "approved"
    if approved.is_dir():
        for day in sorted((d for d in approved.iterdir() if d.is_dir()), reverse=True):
            if (day / name).is_file():
                return day / name
    return None


def refuse_decided(settings, ad: Ad) -> None:
    """v2.5: an ad of a job you skipped or applied to (the decision log, by its id, ATS key or URL key) is never written to the inbox again; the
    message says when and how to see why. Company + title alone never refuse an ad."""
    from ..apply import decisions, listing_keys
    from . import manual

    jid = manual.job_id_for(ad.company, ad.title, ad.url) if ad.company and ad.title and ad.url else None
    m = decisions.Book.load(settings).decided(listing_keys.keys_for(ad.company, ad.title, ad.url, None, getattr(ad, "location", None), jid), jid)
    if m is not None:
        what = "applied to" if m.decision == decisions.APPLIED else "skipped"
        raise IntakeError(f"you {what} this job on {m.date} ({m.record.get('job_id') or '?'}); it is not added again. "
                          f"See: careerops why {m.key}; undo a skip: careerops unskip --id {m.record.get('job_id') or 'JOB'}")


def file_for_job(settings, db, job_id: str) -> tuple[Path, bool]:
    """`live --id JOB`: the JD file of a job the database already knows, as (file, created). The file is looked for in the inbox and in
    approved/<date>/ (the same job id as `live` gives it); when it was filed away it is written again into the inbox from the stored ad (the same
    company, title and URL, so the same job id). A job you skipped or applied to is refused (`careerops unskip --id` undoes a skip). Nothing is fetched."""
    from ..apply import decided

    job = db.job(job_id)
    if job is None:
        raise IntakeError(f"no job {job_id} in the database; use --url with its link to bring the ad in")
    which = decided.load(settings, db).hit(job["company"], job["title"], job["url"], job["location"], job["apply_url"], job["id"])
    if which:
        raise IntakeError(f"you {'applied to' if which == decided.APPLIED else 'skipped'} {job['company']} | {job['title']} ({job_id}); it is not worked on "
                          f"again. Undo a skip: careerops unskip --id {job_id}")
    jds = settings.path("jds")
    days = sorted((d for d in (jds / "approved").glob("*") if d.is_dir()), reverse=True) if (jds / "approved").is_dir() else []
    for folder in [jds / "inbox", *days]:
        for p in decided.txt_files(folder):
            if decided.file_job_id(p) == job["id"]:
                return p, False
    text = str(job["description"] or "")
    if len(text.encode("utf-8")) < manual.MIN_JD_BYTES:
        raise IntakeError(f"{job_id} has no stored ad text to work from ({len(text)} characters); use --url with its link")
    ad = Ad(company=str(job["company"]), title=str(job["title"]), url=str(job["url"] or ""), location=str(job["location"] or ""), text=text,
            source=str(job["source"] or "manual"), ad_id=job_id.split(":", 1)[-1], apply_url=str(job["apply_url"] or ""))
    return write_inbox(jds / "inbox", ad)


def add_url(settings, url: str, company: str | None = None, title: str | None = None) -> tuple[Path, bool, Ad]:
    """Fetch `url` and put the ad in paths.jds/inbox. Returns (file, created, ad). An ad you already approved (the same file name in
    approved/<date>/) is not written again: that file is returned with created=False, so `live --url ... --retailor` resumes it instead of
    meeting a second copy of the same job."""
    ad = fetch_ad(url, company, title, RobotsCache(settings.path("robots")))
    refuse_decided(settings, ad)
    held = filed_in_approved(settings.path("jds"), file_name(ad))
    if held is not None:
        return held, False, ad
    path, created = write_inbox(settings.path("jds") / "inbox", ad)
    return path, created, ad
