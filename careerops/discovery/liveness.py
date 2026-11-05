"""Is a posting still open? Asked once per job right before tailoring (a closed ad is never tailored), and for the inbox in `discover --report`.

  closed     HTTP 404 / 410; a redirect to a generic careers or search page; a page with no job title and no apply path; an expired phrase
  open       the page has a job title (an <h1>, or a JSON-LD JobPosting) and a way to apply
  uncertain  anything else: a page that is only a script shell, a host that blocks us, a server error, a host this tool never fetches.
             The job goes on, with a flag.

Expired phrases are compared after normalising the text (curly quotes made straight, accents stripped, whitespace collapsed): "no longer available",
"no longer accepting applications", "this job has expired", "job posting has expired", "this role|position|job is no longer / closed" (a compound
word such as "closed-loop" is not a closed job), and "<job noun> ... has been filled" ("the application has been filled out" is not).

LinkedIn, Indeed and CV-Library (and the Workday hosts) are never fetched. Every other request goes through the discovery Fetcher: robots.txt is
checked, at most one request a second per host, a 401 / 403 / 429 backs the host off for the run and is never worked around. An answer is kept for
6 hours (paths.discover/liveness.json).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import applykind
from .http import Blocked, FetchError, Fetcher
from .robots import never_fetch

OPEN, CLOSED, UNCERTAIN = "open", "closed", "uncertain"
CACHE_HOURS = 6
MIN_TEXT = 200                       # a page with less visible text than this may be a script shell: it is never called closed for lacking a title
CACHE_FILE = "liveness.json"

# ---- the phrases (compared on normalise()d text)
_NOUN = r"(?:job|role|position|vacancy|post|opening|requisition)"
EXPIRED = (
    re.compile(r"no longer available"),
    re.compile(r"no longer accepting applications?"),
    re.compile(r"this (?:job|position|role|vacancy|listing|posting|advert) has expired"),
    re.compile(r"job posting has expired"),
    re.compile(r"this (?:role|position|job) is (?:no longer|closed(?![\w-]))"),
    re.compile(rf"\b{_NOUN}\b[^.\n]{{0,60}}?\bhas (?:now |already |since )?been filled\b(?! (?:out|in|up)\b)"),
)


def normalise(text: str) -> str:
    """Curly quotes made straight, accents stripped, lower case, every run of whitespace (no-break spaces too) one space."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.translate({0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x02BC: "'", 0x201C: '"', 0x201D: '"', 0x201E: '"'})
    return " ".join(t.lower().split())


def expired_phrase(text: str) -> str | None:
    """The first expired phrase in `text`, as it reads after normalising, or None."""
    t = normalise(text)
    for rx in EXPIRED:
        m = rx.search(t)
        if m:
            return m.group(0)
    return None


# ---- the page
APPLY_TEXT = re.compile(r"^\s*(?:apply|apply (?:now|today|here|online|for (?:this|the) (?:job|role|position)|with .*)|i['’]?m interested|"
                        r"submit (?:your |an )?application|start (?:your |an )?application|bewerben)\s*[.!→>»]*\s*$", re.I)


class _Page(HTMLParser):
    """Visible text (not script or style), the <h1> text, the apply controls, a file input, and the JSON-LD blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.h1: list[str] = []
        self.controls: list[str] = []
        self.hrefs: list[str] = []
        self.file_input = False
        self.ld: list[str] = []
        self._skip = 0
        self._in_h1 = False
        self._ctl: list[str] | None = None
        self._ld = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            self._ld = tag == "script" and "ld+json" in str(a.get("type") or "").lower()
        elif tag == "h1":
            self._in_h1 = True
        elif tag in ("a", "button"):
            self._ctl = []
            if tag == "a" and a.get("href"):
                self.hrefs.append(str(a["href"]))
        elif tag == "input":
            kind = str(a.get("type") or "").lower()
            self.file_input = self.file_input or kind == "file"
            if kind in ("submit", "button"):
                self.controls.append(str(a.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
            self._ld = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag in ("a", "button") and self._ctl is not None:
            self.controls.append(" ".join(self._ctl))
            self._ctl = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            if self._ld:
                self.ld.append(data)
            return
        self.text.append(data)
        if self._in_h1:
            self.h1.append(data)
        if self._ctl is not None:
            self._ctl.append(data)

    # ---- what it found
    def visible(self) -> str:
        return " ".join(" ".join(self.text).split())

    def has_title(self) -> bool:
        return bool(" ".join(" ".join(self.h1).split())) or self.job_posting()

    def job_posting(self) -> bool:
        for block in self.ld:
            try:
                data: Any = json.loads(block)
            except ValueError:
                continue
            items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in items:
                kinds = item.get("@type") if isinstance(item, dict) else None
                if kinds == "JobPosting" or (isinstance(kinds, list) and "JobPosting" in kinds):
                    return True
        return False

    def has_apply(self) -> bool:
        return (self.job_posting() or self.file_input or any(APPLY_TEXT.match(" ".join(c.split())) for c in self.controls)
                or any(re.search(r"/apply(?:[/?#]|$)", h, re.I) for h in self.hrefs))


def read_page(html: str) -> _Page:
    page = _Page()
    try:
        page.feed(html or "")
        page.close()
    except Exception:  # noqa: BLE001  (broken markup: whatever was read is used)
        pass
    return page


# ---- where it ended up
GENERIC_WORDS = frozenset({"careers", "career", "jobs", "job", "vacancies", "vacancy", "search", "job-search", "jobsearch", "opportunities", "openings",
                           "positions", "work-with-us", "join-us", "join", "current-vacancies", "find-a-job", "all-jobs", "joblist", "job-list", "listings"})
LOCALE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$")
SEARCH_KEYS = ("q", "query", "keyword", "keywords", "search", "text", "error")


def _id_like(segment: str) -> bool:
    """A path segment that names one posting: 4+ digits, a UUID, or a long hyphenated slug."""
    return bool(re.search(r"\d{4,}", segment) or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", segment, re.I)
                or segment.count("-") >= 3)


def _segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def generic_redirect(requested: str, final: str) -> str | None:
    """Why `final` is a generic careers or search page rather than the posting asked for (None when it is the same posting, or another page of it)."""
    if not final or final == requested:
        return None
    want, got = _segments(requested), _segments(final)
    if [s.lower() for s in want] == [s.lower() for s in got]:
        return None                                         # http -> https, www, a trailing slash, a query string, job-boards.greenhouse.io
    if any(_id_like(s) for s in got):
        return None                                         # it still names a posting (a locale prefix, a renamed slug)
    words = [s.lower() for s in got if not LOCALE.match(s)]
    query = parse_qs(urlparse(final).query)
    if not words or all(w in GENERIC_WORDS for w in words):
        return f"redirected to a generic page ({urlparse(final).path or '/'})"
    if any(k in query for k in SEARCH_KEYS) and len(got) <= len(want):
        return f"redirected to a search page ({urlparse(final).path or '/'}?{urlparse(final).query[:40]})"
    if any(_id_like(s) for s in want) and len(got) < len(want):
        return f"redirected up to {urlparse(final).path or '/'}, which names no posting"
    return None


# ---- the judgement
@dataclass(frozen=True)
class Result:
    status: str                        # open | closed | uncertain
    reason: str = ""
    url: str = ""
    cached: bool = False

    def line(self) -> str:
        return f"{self.status}" + (f" ({self.reason})" if self.reason else "")


def judge(requested: str, final: str, status: int, html: str) -> Result:
    """The pure part: open / closed / uncertain from the HTTP status, the address the fetch ended at and the page."""
    if status in (404, 410):
        return Result(CLOSED, f"HTTP {status}", requested)
    if status >= 400:
        return Result(UNCERTAIN, f"HTTP {status}", requested)
    why = generic_redirect(requested, final)
    if why:
        return Result(CLOSED, why, requested)
    page = read_page(html)
    phrase = expired_phrase(page.visible())
    if phrase:
        return Result(CLOSED, f'expired: "{phrase}"', requested)
    title, apply = page.has_title(), page.has_apply()
    if title and apply:
        return Result(OPEN, "", requested)
    if not title and not apply:
        if len(page.visible()) >= MIN_TEXT:
            return Result(CLOSED, "no job title and no apply path on the page", requested)
        return Result(UNCERTAIN, "the page has almost no text (a script shell?)", requested)
    return Result(UNCERTAIN, "no apply path found" if title else "no job title found", requested)


# ---- the cache
def cache_key(url: str) -> str:
    p = urlparse(str(url or "").strip())
    host = (p.hostname or "").lower()
    return f"{host}{p.path.rstrip('/')}"


class Cache:
    """{key: {status, reason, at}} in one JSON file; an answer older than CACHE_HOURS is not used. A damaged file is an empty cache."""

    def __init__(self, path: Path | None, hours: float = CACHE_HOURS):
        self.path, self.hours = path, hours
        self.data: dict[str, dict] = {}
        if path is not None:
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                self.data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    def get(self, url: str, now: datetime) -> Result | None:
        e = self.data.get(cache_key(url))
        try:
            if e and now - datetime.fromisoformat(e["at"]) < timedelta(hours=self.hours):
                return Result(e["status"], e.get("reason", ""), url, cached=True)
        except (KeyError, ValueError, TypeError):
            pass
        return None

    def put(self, url: str, result: Result, now: datetime) -> None:
        self.data[cache_key(url)] = {"status": result.status, "reason": result.reason, "at": now.isoformat(timespec="seconds")}
        if self.path is not None:
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                tmp = Path(self.path).with_name(Path(self.path).name + ".tmp")
                tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass                                     # the cache is a courtesy


def cache_for(settings) -> Cache:
    return Cache(settings.path("discover") / CACHE_FILE)


# ---- asking
def check(url: str, fetcher: Fetcher, cache: Cache | None = None, now: datetime | None = None) -> Result:
    """Is the posting at `url` open? One paced, robots-checked request unless the answer is cached; never raises for a page that cannot be read."""
    url = str(url or "").strip()
    now = now or datetime.now(timezone.utc)
    if not url:
        return Result(UNCERTAIN, "the job has no address to check")
    if never_fetch(url):
        return Result(UNCERTAIN, "this host is never fetched by this tool (the ad is only recorded)", url)
    if cache is not None:
        hit = cache.get(url, now)
        if hit is not None:
            return hit
    from . import liveness_api
    api = liveness_api.check(url, fetcher)                   # v3.0: the ATS's own API first (Greenhouse, Lever, Ashby, Workday); the page only when it cannot say
    if api is not None:
        result = Result(api[0], api[1], url)
        if cache is not None:
            cache.put(url, result, now)
        return result
    if applykind.never_fetched(url):                         # a Workday careers PAGE is never read (its API above is the only way in)
        return Result(UNCERTAIN, "this host is never fetched by this tool (the ad is only recorded)", url)
    from . import more_ats
    board = more_ats.liveness(url, fetcher)                  # a v2.6 provider's link: the employer's board says whether the posting is still listed
    if board is not None:
        result = Result(board[0], board[1], url)
        if cache is not None:
            cache.put(url, result, now)
        return result
    try:
        r = fetcher.request(url, what="liveness check")
    except Blocked as e:
        return Result(UNCERTAIN, f"not checked: {str(e)[:120]}", url)
    except FetchError as e:
        return Result(UNCERTAIN, f"not checked: {str(e)[:120]}", url)
    result = judge(url, str(getattr(r, "url", "") or url), r.status_code, getattr(r, "text", "") or "")
    result = Result(result.status, result.reason, url)
    if cache is not None:
        cache.put(url, result, now)
    return result
