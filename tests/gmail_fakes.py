"""A stand-in for the Gmail API for tests outside tests/discovery (which has its own `netfake`): `Net.get` has the signature `requests.get` is called with in
discovery/http.py, answers from routes the test registers, and keeps every request."""
import json


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


class Net:
    """routes: [(url_part, reply)]; the first part found in the URL wins, so register the more specific part first. An unrouted URL raises AssertionError;
    robots.txt is a 404 (everything allowed)."""

    def __init__(self):
        self.routes: list[tuple[str, object]] = []
        self.requests: list[dict] = []

    def add(self, part: str, reply, status: int = 200) -> "Net":
        self.routes.append((part, reply if isinstance(reply, Resp) else Resp(reply, status)))
        return self

    def get(self, url, headers=None, params=None, auth=None, timeout=None, allow_redirects=None, **kw):
        self.requests.append({"url": url, "headers": headers or {}, "params": params})
        for part, reply in self.routes:
            if part in url:
                return reply
        if url.endswith("/robots.txt"):
            return Resp("", 404)
        raise AssertionError(f"unexpected request to {url}")

    def urls(self, include_robots: bool = False) -> list[str]:
        return [r["url"] for r in self.requests if include_robots or not r["url"].endswith("/robots.txt")]
