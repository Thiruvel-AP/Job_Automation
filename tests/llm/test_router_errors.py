"""Router error handling against a scripted fake server: error bodies, 429 + Retry-After / back-off,
400 on response_format → retry without it, and `careerops llm-check`."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from careerops import cli
from careerops.llm import LLMError, Router, check, router

KEY = "sk-secret-123"
GOOD = {"choices": [{"message": {"content": '{"ok": true}'}}], "model": "served-model"}


class _S(BaseHTTPRequestHandler):
    """script[path] = list of (status, headers, body); the last entry repeats. seen = [(path, auth, json body)]."""
    script: dict = {}
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _S.seen.append((self.path, self.headers.get("Authorization"), body))
        n = sum(1 for p, _, _ in _S.seen if p == self.path)
        steps = _S.script[self.path.split("/chat")[0]]
        status, headers, payload = steps[min(n, len(steps)) - 1]
        out = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    _S.script, _S.seen = {}, []
    srv = HTTPServer(("127.0.0.1", 0), _S)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FAKE_KEY", KEY)
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(router, "_sleep", waited.append)
    return waited


def _router(settings, db, server, *names, json_mode=True):
    settings.raw["llm"]["providers"] = [
        {"name": n, "base_url": f"{server}/{n}", "api_key_env": "FAKE_KEY", "model": "m", "rpm": 60000, "rpd": 100,
         "json_mode": json_mode} for n in names]
    return Router(settings, db)


def _events(db):
    return [json.loads(r["detail"]) for r in db.conn.execute("SELECT detail FROM events WHERE stage='llm_error'")]


def _hits(path):
    return [b for p, _, b in _S.seen if p == f"/{path}/chat/completions"]


# ---------------------------------------------------------------- 1. error bodies
def test_http_error_keeps_status_and_truncated_body_in_message_and_events(settings, db, server, sleeps):
    _S.script["/p"] = [(401, {}, ("invalid api key " + "x" * 2000).encode())]
    with pytest.raises(LLMError) as e:
        _router(settings, db, server, "p").json("t", "s", "u")
    assert "HTTP 401" in str(e.value) and "invalid api key" in str(e.value)
    assert "x" * 501 not in str(e.value)                                  # body capped at 500 chars
    rows = [r for r in _events(db) if r["status"] == 401]
    assert rows and rows[0]["provider"] == "p" and rows[0]["body"].startswith("invalid api key")
    assert len(rows[0]["body"]) == 500


def test_a_key_echoed_in_the_body_and_the_auth_header_never_reach_the_message_or_events(settings, db, server, sleeps):
    _S.script["/p"] = [(401, {}, f"Incorrect API key provided: {KEY}".encode())]
    with pytest.raises(LLMError) as e:
        _router(settings, db, server, "p").json("t", "s", "u")
    dump = str(e.value) + json.dumps(_events(db)) + json.dumps([dict(r) for r in db.conn.execute("SELECT * FROM events")])
    assert KEY not in dump and "Bearer" not in dump and "***" in dump


def test_5xx_is_recorded_too(settings, db, server, sleeps):
    _S.script["/p"] = [(503, {}, b"overloaded")]
    with pytest.raises(LLMError, match="HTTP 503: overloaded"):
        _router(settings, db, server, "p").json("t", "s", "u")
    assert {"provider": "p", "status": 503, "body": "overloaded"} in _events(db)


# ---------------------------------------------------------------- 2. 429
def test_429_honours_retry_after_then_succeeds_on_the_same_provider(settings, db, server, sleeps):
    _S.script["/p"] = [(429, {"Retry-After": "3"}, b"slow down"), (200, {}, GOOD)]
    assert _router(settings, db, server, "p").json("t", "s", "u") == {"ok": True}
    assert sleeps == [3.0] and len(_hits("p")) == 2


def test_429_without_retry_after_backs_off_2_4_8_then_falls_back(settings, db, server, sleeps):
    _S.script["/a"] = [(429, {}, b"rate limited")]
    _S.script["/b"] = [(200, {}, GOOD)]
    assert _router(settings, db, server, "a", "b").json("t", "s", "u") == {"ok": True}
    assert sleeps == [2, 4, 8] and len(_hits("a")) == 4 and len(_hits("b")) == 1
    assert any(r["status"] == 429 and r["body"] == "rate limited" for r in _events(db))


def test_a_retry_after_beyond_the_cap_is_not_waited_for(settings, db, server, sleeps):
    _S.script["/a"] = [(429, {"Retry-After": "86400"}, b"daily quota")]
    _S.script["/b"] = [(200, {}, GOOD)]
    assert _router(settings, db, server, "a", "b").json("t", "s", "u") == {"ok": True}
    assert sleeps == [] and len(_hits("a")) == 1


# ---------------------------------------------------------------- 3. 400 on response_format
def test_400_with_response_format_retries_once_without_it(settings, db, server, sleeps):
    _S.script["/p"] = [(400, {}, b"response_format is not supported by this model"), (200, {}, GOOD)]
    r = _router(settings, db, server, "p")
    assert r.json("t", "s", "u") == {"ok": True}
    first, second = _hits("p")
    assert "response_format" in first and "response_format" not in second
    p = r.providers[0]
    assert p.json_mode_rejected and not p.json_mode
    assert any(e["status"] == 400 and "response_format" in e["body"] for e in _events(db))
    r.json("t", "s", "u")                                                  # remembered: no more wasted 400s
    assert "response_format" not in _hits("p")[2] and len(_hits("p")) == 3


def test_400_that_persists_without_response_format_is_a_plain_error(settings, db, server, sleeps):
    _S.script["/p"] = [(400, {}, b"unknown model m")]
    r = _router(settings, db, server, "p")
    with pytest.raises(LLMError, match="HTTP 400: unknown model m"):
        r.json("t", "s", "u")
    assert not r.providers[0].json_mode_rejected and r.providers[0].json_mode


def test_400_without_response_format_is_not_retried_as_json_mode(settings, db, server, sleeps):
    _S.script["/p"] = [(400, {}, b"bad request")]
    r = _router(settings, db, server, "p", json_mode=False)
    with pytest.raises(LLMError):
        r.json("t", "s", "u")
    assert all("response_format" not in b for b in _hits("p"))


# ---------------------------------------------------------------- 5. llm-check
def test_llm_check_reports_everything_and_flags_json_mode_rejection(settings, db, server, sleeps):
    _S.script["/ok"] = [(200, {}, GOOD)]
    _S.script["/picky"] = [(400, {}, b"response_format unsupported"), (200, {}, GOOD)]
    rows = {r["provider"]: r for r in check.check_all(_router(settings, db, server, "ok", "picky"))}
    ok, picky = rows["ok"], rows["picky"]
    assert ok["reachable"] and ok["ok"] and ok["json_ok"] and ok["model"] == "served-model"
    assert ok["json_mode"] == "accepted" and isinstance(ok["latency_ms"], int)
    assert picky["ok"] and picky["json_mode"] == "rejected"
    assert "json_mode: false" in "\n".join(check.format_row(picky))


def test_llm_check_unreachable_missing_key_and_bad_json(settings, db, server, monkeypatch, sleeps):
    _S.script["/words"] = [(200, {}, {"choices": [{"message": {"content": "sure thing"}}]})]
    settings.raw["llm"]["providers"] = [
        {"name": "down", "base_url": "http://127.0.0.1:1", "api_key_env": None, "model": "m", "rpm": 60000},
        {"name": "nokey", "base_url": server, "api_key_env": "ABSENT_KEY", "model": "m", "rpm": 60000},
        {"name": "words", "base_url": f"{server}/words", "api_key_env": None, "model": "m", "rpm": 60000},
    ]
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    rows = {r["provider"]: r for r in check.check_all(Router(settings, db))}
    assert not rows["down"]["reachable"] and not rows["down"]["ok"] and rows["down"]["error"]
    assert not rows["nokey"]["reachable"] and "ABSENT_KEY is not set" in rows["nokey"]["error"]
    assert rows["words"]["reachable"] and not rows["words"]["json_ok"] and not rows["words"]["ok"]


def test_cli_llm_check_prints_no_key_and_exits_by_result(settings, db, server, sleeps, monkeypatch, capsys):
    _S.script["/ok"] = [(200, {}, GOOD)]
    _S.script["/bad"] = [(401, {}, f"bad key {KEY}".encode())]
    _router(settings, db, server, "ok", "bad")
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    with pytest.raises(SystemExit) as e:
        cli.main(["llm-check"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "[ok] ok" in out and "[!!] bad" in out and "HTTP 401" in out
    assert KEY not in out and "Bearer" not in out

    settings.raw["llm"]["providers"] = settings.raw["llm"]["providers"][1:]
    with pytest.raises(SystemExit) as e:
        cli.main(["llm-check"])
    assert e.value.code == 1                                               # no provider passed
