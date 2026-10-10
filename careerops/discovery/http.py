"""The polite HTTP client the discovery sources share (`careerops discover`, ATS probing).

  * at most one request per second per host, the shared honest User-Agent (`sources.UA`), a timeout on every call;
  * a page or public-API URL is checked against the host's robots.txt first (robots.py), redirects are followed by hand and
    every hop is checked again; `robots=False` is only for an official, credentialed API (Reed, Adzuna, Gmail) whose terms are
    its own and not a crawler's;
  * linkedin.com is never contacted;
  * an HTTP 401 / 403 / 429 stops that host for the rest of the run: `Blocked` is raised, nothing is retried or worked around, and
    later requests to the host raise at once without sending anything;
  * a connection error is retried once; other statuses are returned for the caller to read.

Tests replace `get`, `sleep` and `clock`, so no test touches the network.
"""
from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .robots import DOCUMENTED_API_HOSTS, RobotsCache, RobotsError, host_of, never_fetch
from .sources import UA

MIN_HOST_GAP_S = 1.0
TIMEOUT_S = 30
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
BLOCKED_STATUSES = (401, 403, 429)
MAX_BYTES = 20_000_000


class FetchError(RuntimeError):
    """The request could not be made or answered (one line, fit to print)."""


class Blocked(FetchError):
    """The host answered 401 / 403 / 429 (or robots.txt says no): no retry, no workaround, no more requests to it this run."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _real_get(url: str, **kw) -> requests.Response:
    return requests.get(url, timeout=kw.pop("timeout", TIMEOUT_S), allow_redirects=False, **kw)


class Fetcher:
    def __init__(self, robots: RobotsCache | None = None, get: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 gap_s: float = MIN_HOST_GAP_S):
        self.robots = robots if robots is not None else RobotsCache(None)
        # looked up when called, not when defined, so a test can replace requests.get / time.sleep after import
        self._get = get or (lambda url, **kw: _real_get(url, **kw))
        self._sleep = sleep or (lambda s: time.sleep(s))
        self._clock = clock or (lambda: time.monotonic())
        self.gap_s = gap_s
        self.last: dict[str, float] = {}
        self.backed_off: dict[str, int] = {}          # host -> the 401 / 403 / 429 it answered
        self.requests = 0

    # ---- pacing
    def _throttle(self, host: str) -> None:
        wait = self.last.get(host, -self.gap_s) + self.gap_s - self._clock()
        if wait > 0:
            self._sleep(wait)
        self.last[host] = self._clock()

    def _robots_get(self, url: str) -> tuple[int | None, str]:
        host = host_of(url)
        if host in self.backed_off:
            return self.backed_off[host], ""
        self._throttle(host)
        try:
            r = self._get(url, headers=UA)
        except requests.RequestException:
            return None, ""
        if r.status_code in BLOCKED_STATUSES and (r.status_code == 429 or host not in DOCUMENTED_API_HOSTS):
            self.backed_off[host] = r.status_code          # the host said no to a script: not asked again this run
        return r.status_code, (r.text[:500_000] if getattr(r, "ok", False) else "")

    # ---- the request
    def request(self, url: str, *, params: dict | None = None, headers: dict | None = None, auth: tuple | None = None,
                robots: bool = True, what: str = "") -> requests.Response:
        """GET `url`. Raises Blocked (401 / 403 / 429, robots.txt, a host already backed off, linkedin.com) or FetchError
        (no answer, 5xx, too many redirects, a response over MAX_BYTES). 404 and other statuses are returned."""
        what = what or url
        for _hop in range(MAX_REDIRECTS + 1):
            host = host_of(url)
            gone = never_fetch(url)
            if gone:
                raise Blocked(gone)
            if host in self.backed_off:
                raise Blocked(f"{what}: {host} answered HTTP {self.backed_off[host]} earlier in this run; not asked again")
            if robots:
                try:
                    self.robots.check(url, self._robots_get)
                except RobotsError as e:
                    if host in self.backed_off:
                        raise Blocked(f"{what}: {host} answered HTTP {self.backed_off[host]}; not asked again",
                                      self.backed_off[host]) from None
                    raise Blocked(str(e)) from None
            r = None
            for attempt in (0, 1):
                self._throttle(host)
                self.requests += 1
                try:
                    r = self._get(url, headers={**UA, **(headers or {})}, params=params if _hop == 0 else None, auth=auth)
                    break
                except requests.RequestException as e:
                    if attempt == 1 or not isinstance(e, requests.ConnectionError):
                        raise FetchError(f"{what}: could not be fetched ({type(e).__name__})") from None
            if r.status_code in BLOCKED_STATUSES:
                self.backed_off[host] = r.status_code
                raise Blocked(f"{what}: HTTP {r.status_code}: {host} blocks automated access; it is not asked again this run",
                              r.status_code)
            where = (getattr(r, "headers", None) or {}).get("Location") if r.status_code in REDIRECT_STATUSES else None
            if where:
                url = urljoin(url, where)
                if urlparse(url).scheme not in ("http", "https"):
                    raise FetchError(f"{what}: redirected to something that is not a web link")
                auth = None if host_of(url) != host else auth           # a credential never follows a redirect to another host
                continue
            if r.status_code >= 500:
                raise FetchError(f"{what}: HTTP {r.status_code}")
            if len(getattr(r, "content", b"") or b"") > MAX_BYTES:
                raise FetchError(f"{what}: the response is over {MAX_BYTES // 1_000_000} MB")
            return r
        raise FetchError(f"{what}: too many redirects")

    def json(self, url: str, **kw) -> Any:
        """The JSON body of a 200 answer; None for a 404 (not there); FetchError for anything else or a body that is not JSON."""
        r = self.request(url, **kw)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise FetchError(f"{kw.get('what') or url}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise FetchError(f"{kw.get('what') or url}: the reply was not JSON") from None
