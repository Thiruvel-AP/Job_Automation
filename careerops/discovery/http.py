"""The polite HTTP client the discovery sources share (`careerops discover`, ATS probing).

  * at most one request per second per host, the shared honest User-Agent (`sources.UA`), a timeout on every call;
  * a page or public-API URL is checked against the host's robots.txt first (robots.py), redirects are followed by hand and
    every hop is checked again; `robots=False` is only for an official, credentialed API (Reed, Adzuna, Gmail) whose terms are
    its own and not a crawler's;
  * linkedin.com is never contacted;
  * an HTTP 401 / 403 / 429 stops that host for the rest of the run: `Blocked` is raised, nothing is retried or worked around, and
    later requests to the host raise at once without sending anything. With a `blocklog` (blocklog.py) the refusal is written down (time,
    requests to that host before it), and the next runs keep at least blocklog.SLOW_GAP_S between requests to it;
  * a 429 from a host in LONG_BACKOFF_HOSTS (Workable) is remembered in the blocklog with an `until` time: no run asks that host for 24 h, or for as
    long as its `Retry-After` header says when that is longer (never more than blocklog.MAX_BACKOFF_S). The `Blocked` it raises says until when;
  * a connection error is retried once; other statuses are returned for the caller to read;
  * v3.1, `soft_401` (the Ashby posting API answers 401, not 404, for a board name that does not exist): "guess" = a 401 for a GUESSED board is "no board
    there" (returned like a 404, the host is not stopped); "known" = a 401 for a board known to exist counts towards KNOWN_401_STREAK in a row, and only the
    KNOWN_401_STREAK-th stops the host (the ones before raise a FetchError: no answer, nothing cached); any other answer from the host resets the count.
    Without it (the default) a 401 stops the host at once, as before. Every refusal records the exact URL that got it (`refused_url`, the blocklog, the notes);
  * `method="POST", json=...` sends a JSON body (the public Workday CXS list endpoint); `follow=False` returns a redirect instead of following it, so the
    caller can read where it points (a Workday outage page);
  * `allow_paths`: path prefixes that are fetched even on a host in `never` (the Workday /wday/cxs/ API; the careers PAGES on that host stay refused);
  * a `Crawl-delay` in the host's robots.txt (up to MAX_CRAWL_DELAY_S) is the gap to that host instead of one second.

Tests replace `get`, `sleep` and `clock`, so no test touches the network.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .blocklog import SLOW_GAP_S, BlockLog, backoff_seconds
from .robots import DOCUMENTED_API_HOSTS, RobotsCache, RobotsError, RobotsRedirect, host_of, never_fetch
from .sources import UA

MIN_HOST_GAP_S = 1.0
TIMEOUT_S = 30
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
BLOCKED_STATUSES = (401, 403, 429)
MAX_BYTES = 20_000_000
MAX_CRAWL_DELAY_S = 30
LONG_BACKOFF_HOSTS = ("workable.com",)            # a 429 from these (or a subdomain) keeps every later run away for 24 h (or its Retry-After)
KNOWN_401_STREAK = 3                              # v3.1: 401s in a row on boards known to exist before the host is stopped (soft_401="known")
SOFT_401 = ("", "guess", "known")


class FetchError(RuntimeError):
    """The request could not be made or answered (one line, fit to print)."""


class Blocked(FetchError):
    """The host answered 401 / 403 / 429 (or robots.txt says no): no retry, no workaround, no more requests to it this run."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Soft401(FetchError):
    """v3.1: a 401 on a board known to exist, below KNOWN_401_STREAK in a row: not an answer (nothing is read or cached), and the host is not stopped."""


class RobotsRedirected(Blocked):
    """The host's robots.txt answered with a redirect, not rules (robots.RobotsRedirect). Still a Blocked for every caller that does not care; a provider with
    one host per company reads it as "no such company" (more_ats.fetch)."""


def _real_get(url: str, **kw) -> requests.Response:
    return requests.get(url, timeout=kw.pop("timeout", TIMEOUT_S), allow_redirects=False, **kw)


def _real_post(url: str, **kw) -> requests.Response:
    return requests.post(url, timeout=kw.pop("timeout", TIMEOUT_S), allow_redirects=False, **kw)


class Fetcher:
    def __init__(self, robots: RobotsCache | None = None, get: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 gap_s: float = MIN_HOST_GAP_S, never: tuple[str, ...] = (), blocklog: BlockLog | None = None,
                 post: Callable[..., Any] | None = None, allow_paths: tuple[str, ...] = ()):
        self.robots = robots if robots is not None else RobotsCache(None)
        # looked up when called, not when defined, so a test can replace requests.get / time.sleep after import
        self._get = get or (lambda url, **kw: _real_get(url, **kw))
        self._post = post or (lambda url, **kw: _real_post(url, **kw))
        self.allow_paths = tuple(allow_paths)             # path prefixes fetched even on a `never` host
        self._sleep = sleep or (lambda s: time.sleep(s))
        self._clock = clock or (lambda: time.monotonic())
        self.gap_s = gap_s
        self.never = never                            # hosts (and their subdomains) that are refused before anything is sent
        self.last: dict[str, float] = {}
        self.host_gap: dict[str, float] = {}          # host -> the Crawl-delay its robots.txt asks for, when longer than gap_s
        self.backed_off: dict[str, int] = {}          # host -> the 401 / 403 / 429 it answered
        self.backoff_until: dict[str, datetime] = {}  # host -> when it may be asked again (a long backoff: 429 from LONG_BACKOFF_HOSTS, this run or an earlier one)
        self.requests = 0
        self.by_host: Counter = Counter()             # requests actually sent, per host (robots.txt included)
        self.blocklog = blocklog                      # the memory of refusals between runs (None: this run only)
        self.refused_url: dict[str, str] = {}         # host -> the exact URL that got the 401 / 403 / 429 (v3.1)
        self.soft_401s: list[str] = []                # URLs of guessed boards that answered 401: "no board", the host was not stopped (v3.1)
        self.known_401: Counter = Counter()           # host -> 401s in a row on boards known to exist (soft_401="known")
        for host in (blocklog.slow_hosts() if blocklog is not None else []):
            self.host_gap[host] = SLOW_GAP_S          # it refused us recently: a slower pace, however the robots.txt reads
        for host, (status, until) in (blocklog.active_backoffs() if blocklog is not None else {}).items():
            self.backed_off[host] = status            # asked for a long pause by an earlier run (429 from Workable): not one request until it ends
            self.backoff_until[host] = until

    def _now(self) -> datetime:
        return self.blocklog.now() if self.blocklog is not None else datetime.now(timezone.utc)

    def _refused(self, host: str, status: int, retry_after: object = None, url: str = "") -> None:
        """A host said no: remember it for the rest of this run, and write down when, after how many requests, and the URL that got it. A 429 from a
        LONG_BACKOFF_HOSTS host also sets the time before which no later run asks it (24 h, or its Retry-After when longer)."""
        self.backed_off[host] = status
        if url:
            self.refused_url[host] = url
        until = None
        if status == 429 and any(host == h or host.endswith("." + h) for h in LONG_BACKOFF_HOSTS):
            now = self._now()
            until = now + timedelta(seconds=backoff_seconds(retry_after, now))
            self.backoff_until[host] = until
        if self.blocklog is not None:
            try:
                self.blocklog.record(host, status, max(self.by_host[host] - 1, 0), sum(self.by_host.values()), until=until, url=url)
            except OSError:
                pass                                  # the memory is a courtesy; the refusal is honoured either way

    def _again(self, host: str) -> str:
        """`earlier in this run` or `until 2026-11-05 14:07 UTC` for the message of a host that is not asked."""
        until = self.backoff_until.get(host)
        return f"until {until.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC" if until is not None else "earlier in this run"

    # ---- pacing
    def _throttle(self, host: str) -> None:
        gap = max(self.gap_s, self.host_gap.get(host, 0.0))
        wait = self.last.get(host, -gap) + gap - self._clock()
        if wait > 0:
            self._sleep(wait)
        self.last[host] = self._clock()

    def _robots_get(self, url: str) -> tuple[int | None, str]:
        host = host_of(url)
        if host in self.backed_off:
            return self.backed_off[host], ""
        self._throttle(host)
        self.by_host[host] += 1
        try:
            r = self._get(url, headers=UA)
        except requests.RequestException:
            return None, ""
        if r.status_code in BLOCKED_STATUSES and (r.status_code == 429 or host not in DOCUMENTED_API_HOSTS):
            self._refused(host, r.status_code, (getattr(r, "headers", None) or {}).get("Retry-After"), url)      # the host said no to a script: not asked again this run
        return r.status_code, (r.text[:500_000] if getattr(r, "ok", False) else "")

    def robots_state(self, url: str) -> str:
        """'allowed' | 'disallowed' | 'unchecked' for a page or API URL: what robots.txt says about it for this tool ('unchecked' when
        robots.txt cannot be read, or the host is one this tool never contacts). Costs one paced request per host per 24 hours."""
        try:
            self.robots.check(url, self._robots_get)
        except RobotsError as e:
            return "disallowed" if "disallows" in str(e) else "unchecked"
        return "allowed"

    # ---- the request
    def request(self, url: str, *, params: dict | None = None, headers: dict | None = None, auth: tuple | None = None,
                robots: bool = True, what: str = "", stream: bool = False, method: str = "GET", json: Any = None,
                follow: bool = True, soft_401: str = "") -> requests.Response:
        """GET `url`. Raises Blocked (401 / 403 / 429, robots.txt, a host already backed off, linkedin.com) or FetchError
        (no answer, 5xx, too many redirects, a response over MAX_BYTES). 404 and other statuses are returned. `soft_401`: see the module text
        ("guess": a 401 is returned like a 404; "known": a 401 is a FetchError until the KNOWN_401_STREAK-th in a row, which stops the host).
        `stream=True` leaves the body unread (and the MAX_BYTES check to the caller, who must close the response): for an open-data
        file that is larger than a job-board answer."""
        what = what or url
        for _hop in range(MAX_REDIRECTS + 1):
            host = host_of(url)
            gone = never_fetch(url)
            if gone:
                raise Blocked(gone)
            if any(host == h or host.endswith("." + h) for h in self.never) and not any(urlparse(url).path.startswith(p) for p in self.allow_paths):
                raise Blocked(f"{what}: {host} is never requested by this tool (it is only recorded as the place to apply)")
            if host in self.backed_off:
                raise Blocked(f"{what}: {host} answered HTTP {self.backed_off[host]} {self._again(host)}; not asked again", self.backed_off[host])
            if robots:
                try:
                    self.robots.check(url, self._robots_get)
                except RobotsError as e:
                    if host in self.backed_off:
                        raise Blocked(f"{what}: {host} answered HTTP {self.backed_off[host]} {self._again(host)}; not asked again",
                                      self.backed_off[host]) from None
                    if isinstance(e, RobotsRedirect):
                        raise RobotsRedirected(str(e)) from None
                    raise Blocked(str(e)) from None
                delay = self.robots.crawl_delay(url)
                if delay and delay > self.gap_s:
                    self.host_gap[host] = max(self.host_gap.get(host, 0.0), min(delay, MAX_CRAWL_DELAY_S))
            r = None
            for attempt in (0, 1):
                self._throttle(host)
                self.requests += 1
                self.by_host[host] += 1
                try:
                    if method == "POST":
                        r = self._post(url, headers={**UA, **(headers or {})}, json=json)
                    else:
                        r = self._get(url, headers={**UA, **(headers or {})}, params=params if _hop == 0 else None, auth=auth,
                                      **({"stream": True} if stream else {}))
                    break
                except requests.RequestException as e:
                    if attempt == 1 or not isinstance(e, requests.ConnectionError):
                        raise FetchError(f"{what}: could not be fetched ({type(e).__name__})") from None
            if r.status_code == 401 and soft_401 == "guess":
                self.soft_401s.append(url)                         # a guessed board that does not exist: "no board", not a refusal
                return r
            if r.status_code == 401 and soft_401 == "known":
                self.known_401[host] += 1
                if self.known_401[host] < KNOWN_401_STREAK:
                    raise Soft401(f"{what}: HTTP 401 at {url} ({self.known_401[host]} of {KNOWN_401_STREAK} in a row on known boards before {host} "
                                     "is stopped); nothing was read")
            elif soft_401:
                self.known_401[host] = 0                           # any other answer: the run of 401s is over
            if r.status_code in BLOCKED_STATUSES:
                self._refused(host, r.status_code, (getattr(r, "headers", None) or {}).get("Retry-After"), url)
                raise Blocked(f"{what}: HTTP {r.status_code}: {host} blocks automated access; it is not asked again " +
                              (f"{self._again(host)}" if host in self.backoff_until else "this run"), r.status_code)
            where = (getattr(r, "headers", None) or {}).get("Location") if r.status_code in REDIRECT_STATUSES else None
            if where and follow:
                method, json = "GET", None                         # a redirect is followed with a GET
                url = urljoin(url, where)
                if urlparse(url).scheme not in ("http", "https"):
                    raise FetchError(f"{what}: redirected to something that is not a web link")
                auth = None if host_of(url) != host else auth           # a credential never follows a redirect to another host
                continue
            if r.status_code >= 500:
                raise FetchError(f"{what}: HTTP {r.status_code}")
            if not stream and len(getattr(r, "content", b"") or b"") > MAX_BYTES:
                raise FetchError(f"{what}: the response is over {MAX_BYTES // 1_000_000} MB")
            return r
        raise FetchError(f"{what}: too many redirects")

    def json(self, url: str, **kw) -> Any:
        """The JSON body of a 200 answer; None for a 404 (not there), or a 401 with soft_401="guess"; FetchError for anything else or a body that is not JSON."""
        r = self.request(url, **kw)
        if r.status_code == 404 or (r.status_code == 401 and kw.get("soft_401") == "guess"):
            return None
        if not r.ok:
            raise FetchError(f"{kw.get('what') or url}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise FetchError(f"{kw.get('what') or url}: the reply was not JSON") from None
