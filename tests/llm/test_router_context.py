"""Context limits and provider pinning: a request that would overflow a provider's context is never sent to it, and
`Router(only=name)` can never fall back to another provider. No network: a local fake server stands in for both
providers. (The `careerops calibrate --provider` command and report are tested in tests/reporting/test_calibrate.py.)"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from careerops.config import ROOT
from careerops.llm import LLMError, Router, router


class _S(BaseHTTPRequestHandler):
    seen: list = []          # (path prefix, json body)
    limited: set = set()     # path prefixes that answer 429

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prefix = self.path.split("/chat")[0]
        _S.seen.append((prefix, body))
        if prefix in _S.limited:
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}], "model": "served" + prefix}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    _S.seen, _S.limited = [], set()
    monkeypatch.setattr(router, "_sleep", lambda s: None)
    srv = HTTPServer(("127.0.0.1", 0), _S)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _router(settings, db, server, groq_ctx=None, ollama_ctx=None, only=None):
    def prov(n, ctx):
        return {"name": n, "base_url": f"{server}/{n}", "api_key_env": None, "model": f"{n}-model", "rpm": 60000,
                "rpd": 1000, "tpm": 10**9, "json_mode": True, "context_tokens": ctx}
    settings.raw["llm"]["providers"] = [prov("groq", groq_ctx), prov("ollama", ollama_ctx)]
    return Router(settings, db, only=only)


def _hit_providers():
    return [p.strip("/") for p, _ in _S.seen]


BIG = "x" * 5000          # ~1430 estimated tokens; the default completion cap adds 1000


# ------------------------------------------------------------------ context_tokens
def test_a_request_that_overflows_a_providers_context_goes_to_the_next_one(settings, db, server):
    r = _router(settings, db, server, groq_ctx=2000, ollama_ctx=8192)
    assert r.json("t", "sys", BIG) == {"ok": True}
    assert _hit_providers() == ["ollama"]                         # groq was never sent the prompt
    assert [u["provider"] for u in r.usage] == ["ollama"]
    ev = db.conn.execute("SELECT detail FROM events WHERE stage='llm_skip'").fetchall()
    assert len(ev) == 1 and "groq" in ev[0][0] and "2,000" in ev[0][0]      # the reason is logged


def test_a_request_that_fits_is_sent_to_the_first_provider(settings, db, server):
    r = _router(settings, db, server, groq_ctx=2000, ollama_ctx=8192)
    r.json("t", "sys", "short")
    assert _hit_providers() == ["groq"]


def test_input_plus_max_tokens_is_what_is_compared_with_the_context(settings, db, server):
    # the prompt alone (~1430) fits 2000; the 1000-token completion cap on top does not
    p = _router(settings, db, server, groq_ctx=2000).providers[0]
    assert "needs ~2,4" in p.overflow("sys", BIG, "t")
    assert p.overflow("sys", BIG[:1000], "t") is None
    p.max_tokens["t"] = 100                                       # a smaller cap: the same prompt now fits
    assert p.overflow("sys", BIG, "t") is None


def test_when_no_provider_has_room_nothing_is_sent_and_every_reason_is_reported(settings, db, server):
    r = _router(settings, db, server, groq_ctx=1500, ollama_ctx=1600)
    with pytest.raises(LLMError) as e:
        r.json("t", "sys", BIG)
    assert _S.seen == []
    msg = str(e.value)
    assert "groq: skipped" in msg and "ollama: skipped" in msg and "context is 1,500" in msg


def test_a_provider_without_context_tokens_is_not_checked(settings, db, server):
    r = _router(settings, db, server, groq_ctx=None)
    r.json("t", "sys", "x" * 500_000)
    assert _hit_providers() == ["groq"]


def test_settings_name_each_providers_context_and_the_modelfile_agrees(settings):
    prov = {p["name"]: p for p in settings["llm"]["providers"]}
    assert prov["groq"]["context_tokens"] == 131072 and prov["ollama"]["context_tokens"] == 8192
    assert prov["ollama"]["model"] == "qwen2.5-7b-8k"
    modelfile = (ROOT / "config" / "ollama" / "Modelfile").read_text(encoding="utf-8")
    assert f"PARAMETER num_ctx {prov['ollama']['context_tokens']}" in modelfile
    assert "FROM qwen2.5:7b-instruct" in modelfile


# ------------------------------------------------------------------ pinning
def test_a_pinned_router_has_only_that_provider(settings, db, server):
    r = _router(settings, db, server, only="ollama")
    assert [p.name for p in r.providers] == ["ollama"] and r.pinned == "ollama"
    for task in ("a", "b", "c"):
        r.json(task, "s", "u")
    assert set(_hit_providers()) == {"ollama"} and {u["provider"] for u in r.usage} == {"ollama"}


def test_a_pinned_provider_that_fails_does_not_fall_back(settings, db, server):
    _S.limited = {"/groq"}
    r = _router(settings, db, server, only="groq")
    with pytest.raises(LLMError, match="all providers failed"):
        r.json("t", "s", "u")
    assert set(_hit_providers()) == {"groq"}                      # ollama was never tried
    assert _router(settings, db, server).json("t", "s", "u") == {"ok": True}     # unpinned: the same failure falls back
    assert _hit_providers()[-1] == "ollama"


def test_a_pinned_provider_that_is_too_small_fails_rather_than_truncating(settings, db, server):
    r = _router(settings, db, server, groq_ctx=1500, only="groq")
    with pytest.raises(LLMError, match="skipped"):
        r.json("t", "sys", BIG)
    assert _S.seen == []


def test_an_unknown_provider_name_is_an_error_that_lists_the_configured_ones(settings, db, server):
    with pytest.raises(ValueError, match=r"no LLM provider named 'gemini'; configured: groq, ollama"):
        _router(settings, db, server, only="gemini")
