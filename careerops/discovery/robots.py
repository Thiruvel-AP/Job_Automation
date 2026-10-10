"""robots.txt for the fetches this tool makes (`add-url`, `live --url`, and the page fetches of `discover`).

Before a page is fetched, the host's robots.txt is read (one request, paced like any other) and cached for 24 hours; a path the
file disallows for this tool is refused with a message that says so. The rules are matched as RFC 9309 describes (groups by
user-agent product token, `*` and `$` in paths, the longest matching rule wins, Allow wins a tie), because Python's
`urllib.robotparser` does not understand the `/*?` rule CV-Library publishes.

  robots.txt 2xx            parsed
  robots.txt 404 / 410      no robots.txt: everything is allowed (RFC 9309 "unavailable")
  robots.txt 401 / 403      refused: the site says no to a script, never worked around.
                            Exception: a host in DOCUMENTED_API_HOSTS, whose public JSON API is meant for programs; its own
                            401 / 403 / 429 still stops the fetch, and a robots.txt that can be read is still obeyed.
  robots.txt 429 / 5xx / no answer   refused: the rules cannot be checked.

`never_fetch(url)` names the hosts this tool never contacts at all (linkedin.com: its robots.txt disallows automation).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

PRODUCT_TOKEN = "career-ops"            # the product token of sources.UA ("career-ops/2.0 (personal job search)")
TTL_S = 24 * 3600
NEVER_FETCH = ("linkedin.com", "lnkd.in")      # a host or any subdomain of it
# Public, documented, unauthenticated job-board APIs (their own docs say programs may call them): a robots.txt that answers
# 401 / 403 on these hosts is not read as a refusal of the API.
DOCUMENTED_API_HOSTS = ("boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com")
LINKEDIN_MESSAGE = ("linkedin.com is never fetched (its robots.txt disallows automated access, and logging in on your behalf "
                    "is out of bounds): LinkedIn ads come in through job-alert emails (`careerops discover --gmail-alerts`) "
                    "or from the clipboard (`careerops live --clipboard`)")


class RobotsError(ValueError):
    """The fetch is not allowed (or cannot be checked). One line, fit to print as it is."""


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def never_fetch(url: str) -> str | None:
    """The message to print when `url` is on a host this tool never contacts, else None."""
    host = host_of(url)
    if any(host == h or host.endswith("." + h) for h in NEVER_FETCH):
        return LINKEDIN_MESSAGE
    return None


# ------------------------------------------------------------------ the rules
@dataclass
class Rules:
    """The rules that apply to this tool: [(allow, pattern)] from the best-matching user-agent group (or `*`)."""
    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None

    def allowed(self, path_and_query: str) -> bool:
        best_len, best_allow = -1, True
        for allow, pattern in self.rules:
            if pattern and _matches(pattern, path_and_query):
                n = len(pattern.rstrip("$"))
                if n > best_len or (n == best_len and allow):
                    best_len, best_allow = n, allow
        return best_allow

    def to_json(self) -> dict:
        return {"rules": [[a, p] for a, p in self.rules], "crawl_delay": self.crawl_delay}

    @staticmethod
    def from_json(d: dict) -> "Rules":
        return Rules([(bool(a), str(p)) for a, p in d.get("rules", [])], d.get("crawl_delay"))


def _matches(pattern: str, target: str) -> bool:
    regex = "".join(".*" if c == "*" else "$" if c == "$" else re.escape(c) for c in pattern)
    if pattern.endswith("$"):
        regex = regex[:-1] + r"\Z"
    return re.match(regex, target) is not None


def parse(text: str, token: str = PRODUCT_TOKEN) -> Rules:
    """The rules of the group whose user-agent line names `token` (a substring match, case-insensitive, the longest name
    wins), else those of the `*` group, else none (everything allowed)."""
    groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    delay: float | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not last_was_agent and (agents or rules):
                groups.append((agents, rules, delay))
                agents, rules, delay = [], [], None
            agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key in ("allow", "disallow"):
            if value or key == "allow":
                rules.append((key == "allow", value))
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                pass
    if agents or rules:
        groups.append((agents, rules, delay))
    tok = token.lower()
    named = [(max(len(a) for a in ag if a != "*" and a in tok), r, d) for ag, r, d in groups
             if any(a != "*" and a in tok for a in ag)]
    if named:
        _, r, d = max(named, key=lambda g: g[0])
        return Rules(r, d)
    for ag, r, d in groups:
        if "*" in ag:
            return Rules(r, d)
    return Rules()


# ------------------------------------------------------------------ the cache
class RobotsCache:
    """robots.txt rules per host, kept in memory and (when `directory` is given) as <directory>/<host>.json for TTL_S seconds.
    `fetch(url)` -> (status, text) does the one request for a robots.txt (the caller paces and identifies it); a status of
    None means there was no answer."""

    def __init__(self, directory: Path | None = None, clock: Callable[[], float] = time.time):
        self.dir, self.clock = directory, clock
        self.memory: dict[str, tuple[float, Rules | str]] = {}      # host -> (when, Rules | the refusal message)

    def _load(self, host: str) -> tuple[float, Rules | str] | None:
        if host in self.memory:
            return self.memory[host]
        if self.dir is None:
            return None
        try:
            d = json.loads((self.dir / f"{host}.json").read_text(encoding="utf-8"))
            entry = (float(d["fetched"]), Rules.from_json(d))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self.memory[host] = entry
        return entry

    def _store(self, host: str, rules: Rules) -> None:
        self.memory[host] = (self.clock(), rules)
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / f"{host}.json").write_text(json.dumps({"fetched": self.clock(), **rules.to_json()}), encoding="utf-8")
            except OSError:
                pass

    def rules_for(self, url: str, fetch: Callable[[str], tuple[int | None, str]]) -> Rules:
        host = host_of(url)
        have = self._load(host)
        if have is not None and self.clock() - have[0] < TTL_S:
            if isinstance(have[1], str):
                raise RobotsError(have[1])
            return have[1]
        scheme = urlparse(url).scheme or "https"
        status, text = fetch(f"{scheme}://{urlparse(url).netloc}/robots.txt")
        if status is not None and 200 <= status < 300:
            rules = parse(text)
        elif status in (404, 410):
            rules = Rules()
        elif status in (401, 403) and host in DOCUMENTED_API_HOSTS:
            rules = Rules()                                  # see the module docstring: the API's own status decides
        else:
            why = (f"{host}/robots.txt answered HTTP {status}" if status else f"{host}/robots.txt could not be fetched")
            msg = (f"{why}, so whether {host} allows automated access cannot be checked: this site blocks automated access, "
                   "copy the job text and use --clipboard")
            self.memory[host] = (self.clock(), msg)           # remembered for this run only: not written to disk
            raise RobotsError(msg)
        self._store(host, rules)
        return rules

    def check(self, url: str, fetch: Callable[[str], tuple[int | None, str]]) -> None:
        """Raises RobotsError when `url` may not be fetched: a host in NEVER_FETCH, a robots.txt that cannot be checked, or a
        path that robots.txt disallows for this tool."""
        gone = never_fetch(url)
        if gone:
            raise RobotsError(gone)
        u = urlparse(url)
        target = (u.path or "/") + (("?" + u.query) if u.query else "")
        if not self.rules_for(url, fetch).allowed(target):
            raise RobotsError(f"{u.hostname}/robots.txt disallows {target[:80]} for automated access: this tool does not fetch it. "
                              "Copy the job text and use --clipboard")
