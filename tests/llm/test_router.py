"""Router against a fake OpenAI-compatible server: request shape, JSON mode, 429 → fallback, quota counting."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from careerops.llm import LLMError, Router, router


class _H(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _H.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.path.startswith("/limited"):
            self.send_response(429); self.end_headers(); return
        msg = {"choices": [{"message": {"content": '```json\n{"ok": true, "model": "%s"}\n```' % body["model"]}}]}
        out = json.dumps(msg).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _router(settings, db, providers):
    settings.raw["llm"]["providers"] = providers
    return Router(settings, db)


def test_fallback_on_429_and_quota(settings, db, server, monkeypatch):
    monkeypatch.setattr(router, "_sleep", lambda s: None)       # the 429 is retried with back-off first
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    _H.seen.clear()
    r = _router(settings, db, [
        {"name": "groq", "base_url": f"{server}/limited", "api_key_env": "FAKE_KEY", "model": "big", "rpm": 6000, "rpd": 10, "json_mode": True},
        {"name": "ollama", "base_url": f"{server}/local", "api_key_env": None, "model": "small", "rpm": 6000, "rpd": 10, "json_mode": True},
    ])
    out = r.json("t", "sys", "user")
    assert out == {"ok": True, "model": "small"}
    first, second = _H.seen[0], _H.seen[-1]          # groq's first try, then the ollama fallback (3 retries between)
    assert len(_H.seen) == 5
    assert first[0] == "/limited/chat/completions" and first[1] == "Bearer sk-test"
    assert first[2]["response_format"] == {"type": "json_object"}
    assert second[1] is None
    assert db.llm_calls_today("groq") == 4 and db.llm_calls_today("ollama") == 1   # 1 try + 3 back-off retries


def test_missing_key_and_exhausted_quota(settings, db, server, monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    r = _router(settings, db, [
        {"name": "nokey", "base_url": server, "api_key_env": "NOPE_KEY", "model": "m", "rpm": 6000, "rpd": 10},
        {"name": "spent", "base_url": server, "api_key_env": None, "model": "m", "rpm": 6000, "rpd": 0},
    ])
    with pytest.raises(LLMError, match="unavailable"):
        r.json("t", "s", "u")
