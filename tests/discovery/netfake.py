"""A stand-in for the network, shared by the discovery tests: `Net.get` has the signature `requests.get` is called with in
discovery/http.py, answers from routes the test registers, and keeps every request so a test can say what was (not) asked."""
import json
from pathlib import Path

from conftest import FIX

DIR = FIX / "discover"


class Resp:
    def __init__(self, body="", status=200, headers=None):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        self.content = raw.encode() if isinstance(raw, str) else raw
        self.text = self.content.decode("utf-8")
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.url = ""

    def json(self):
        return json.loads(self.text)


def fixture_json(name: str):
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8")


class Net:
    """routes: [(url_part, reply)]; the first part found in the URL wins, so register the more specific part first. A reply is a
    Resp, a dict/list (JSON 200), a str (text 200) or a function (url, kw) -> reply. An unrouted URL raises AssertionError, and a
    robots.txt with no route is a 404 (everything allowed)."""

    def __init__(self, strict: bool = True):
        self.strict = strict           # False: a URL with no route is a 404 (a real API's answer for an unknown board)
        self.routes: list[tuple[str, object]] = []
        self.requests: list[dict] = []

    def add(self, part: str, reply, status: int = 200, headers=None) -> "Net":
        if not callable(reply) and not isinstance(reply, Resp):
            reply = Resp(reply, status, headers)
        self.routes.append((part, reply))
        return self

    def get(self, url, headers=None, params=None, auth=None, timeout=None, allow_redirects=None, **kw):
        self.requests.append({"url": url, "headers": headers or {}, "params": params, "auth": auth, "timeout": timeout,
                              "allow_redirects": allow_redirects})
        for part, reply in self.routes:
            if part in url:
                r = reply(url, params=params, auth=auth, headers=headers) if callable(reply) else reply
                if isinstance(r, Exception):
                    raise r
                return r if isinstance(r, Resp) else Resp(r)
        if url.endswith("/robots.txt") or not self.strict:
            return Resp("", 404)
        raise AssertionError(f"unexpected request to {url}: add a route in the test")

    # ---- what was asked
    def urls(self, include_robots: bool = False) -> list[str]:
        return [r["url"] for r in self.requests if include_robots or not r["url"].endswith("/robots.txt")]

    def hosts(self) -> set[str]:
        from urllib.parse import urlparse
        return {urlparse(r["url"]).hostname for r in self.requests}
