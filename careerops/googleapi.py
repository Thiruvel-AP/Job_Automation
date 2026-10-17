"""The one place career-ops talks to Google's APIs with your token (Sheets for the tracker, Gmail for outreach drafts).

  * at most one request per second per host, the shared honest User-Agent, a timeout on every call;
  * the token comes from discovery/gmail.py (`access_token`, refreshed when it is about to expire) and goes nowhere but the
    Authorization header of a request to a Google host; it is never printed or put in an error message;
  * a call that would SEND mail is refused before anything leaves the machine: a path ending in /send. The only write career-ops makes
    to Gmail is creating a draft; tests/test_never_sends.py fails on any send.

Tests pass `request` (a function replacing requests.request), `sleep` and `clock`: no test touches the network.
"""
from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .discovery import gmail
from .discovery.sources import UA

MIN_HOST_GAP_S = 1.0
TIMEOUT_S = 30
GOOGLE_HOSTS = ("gmail.googleapis.com", "sheets.googleapis.com")


class GoogleError(RuntimeError):
    """A Google API call that was refused or failed. One line, fit to print (no token, no body)."""


class GoogleAPI:
    def __init__(self, settings, *, need: tuple[str, ...] = (), request: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 token: str | None = None):
        self.settings, self.need, self._token = settings, tuple(need), token
        self._request = request or (lambda method, url, **kw: requests.request(method, url, timeout=TIMEOUT_S, **kw))
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.last: dict[str, float] = {}
        self.calls = 0

    def token(self) -> str:
        if self._token is None:
            self._token = gmail.access_token(self.settings, need=self.need or (gmail.SCOPE,))
        return self._token

    def _throttle(self, host: str) -> None:
        wait = self.last.get(host, -MIN_HOST_GAP_S) + MIN_HOST_GAP_S - self._clock()
        if wait > 0:
            self._sleep(wait)
        self.last[host] = self._clock()

    def call(self, method: str, url: str, *, params: dict | None = None, body: dict | None = None, what: str = "") -> dict:
        """One JSON call -> the decoded answer ({} for an empty one). Raises GoogleError."""
        u = urlparse(url)
        what = what or f"{method} {u.hostname}{u.path}"
        if u.scheme != "https" or (u.hostname or "") not in GOOGLE_HOSTS:
            raise GoogleError(f"{what}: refused, not a Google API address")
        if u.path.rstrip("/").endswith("/send") or ":send" in u.path:
            raise GoogleError(f"{what}: refused, career-ops never sends mail (it creates drafts; you send them)")
        headers = {**UA, "Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        self._throttle(u.hostname)
        self.calls += 1
        try:
            r = self._request(method, url, headers=headers, params=params, json=body)
        except requests.RequestException as e:
            raise GoogleError(f"{what}: could not be reached ({type(e).__name__})") from None
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        if not 200 <= r.status_code < 300:
            msg = ((data.get("error") or {}) if isinstance(data, dict) else {}).get("message") or ""
            raise GoogleError(f"{what}: HTTP {r.status_code}" + (f": {' '.join(str(msg).split())[:240]}" if msg else ""))
        return data if isinstance(data, dict) else {}
