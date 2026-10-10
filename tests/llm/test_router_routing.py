"""Task routing: `llm.task_providers` (cover_letter: [groq]) keeps the letter off every other provider. When the allowed
provider cannot take it the router raises TaskUnavailable and nothing is sent elsewhere; other tasks still fall back.
No network: a local fake server stands in for both providers."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from careerops.llm import LLMError, Router, TaskUnavailable, router


class _S(BaseHTTPRequestHandler):
    seen: list = []             # path prefix of every request
    limited: set = set()        # prefixes answering 429
    garbage: set = set()        # prefixes answering 200 with text that is not JSON

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        prefix = self.path.split("/chat")[0]
        _S.seen.append(prefix)
        if prefix in _S.limited:
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        content = "not json at all" if prefix in _S.garbage else '{"ok": true}'
        out = json.dumps({"choices": [{"message": {"content": content}}], "model": "m"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    _S.seen, _S.limited, _S.garbage = [], set(), set()
    monkeypatch.setattr(router, "_sleep", lambda s: None)
    srv = HTTPServer(("127.0.0.1", 0), _S)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _router(settings, db, server, monkeypatch, groq_key=True, only=None, tpd=None):
    monkeypatch.setenv("FAKE_GROQ", "k")
    if not groq_key:
        monkeypatch.delenv("FAKE_GROQ")
    groq = {"name": "groq", "base_url": f"{server}/groq", "api_key_env": "FAKE_GROQ", "model": "g", "rpm": 60000,
            "rpd": 1000, "tpm": 10**9, "json_mode": True}
    if tpd:
        groq["tpd"] = tpd
    ollama = {"name": "ollama", "base_url": f"{server}/ollama", "api_key_env": None, "model": "o", "rpm": 60000,
              "rpd": 1000, "tpm": 10**9, "json_mode": True}
    settings.raw["llm"]["providers"] = [groq, ollama]
    settings.raw["llm"]["task_providers"] = {"cover_letter": ["groq"]}
    return Router(settings, db, only=only)


def test_the_settings_restrict_the_letter_to_groq(settings):
    assert settings["llm"]["task_providers"]["cover_letter"] == ["groq"]


def test_the_letter_is_written_by_groq_when_groq_can(settings, db, server, monkeypatch):
    r = _router(settings, db, server, monkeypatch)
    assert r.json("cover_letter", "s", "u") == {"ok": True}
    assert _S.seen == ["/groq"]


def test_the_letter_is_never_written_by_ollama_when_groq_has_no_key(settings, db, server, monkeypatch):
    r = _router(settings, db, server, monkeypatch, groq_key=False)
    with pytest.raises(TaskUnavailable, match="cover_letter can only run on groq"):
        r.json("cover_letter", "s", "u")
    assert _S.seen == []                                   # not a single request went to ollama (or groq)


def test_the_letter_waits_when_groq_is_rate_limited_and_ollama_is_not_asked(settings, db, server, monkeypatch):
    _S.limited = {"/groq"}
    r = _router(settings, db, server, monkeypatch)
    with pytest.raises(TaskUnavailable):
        r.json("cover_letter", "s", "u")
    assert set(_S.seen) == {"/groq"}


def test_the_letter_waits_when_groqs_daily_budget_is_spent(settings, db, server, monkeypatch):
    r = _router(settings, db, server, monkeypatch, tpd=1000)
    db.llm_count("groq/openai/gpt-oss-120b")             # the letter model is counted under its own key (llm.models)
    db.llm_add_tokens("groq/openai/gpt-oss-120b", 1000)
    with pytest.raises(TaskUnavailable):
        r.json("cover_letter", "s", "u")
    assert _S.seen == []


def test_a_job_budget_check_that_leaves_only_ollama_also_holds_the_letter_back(settings, db, server, monkeypatch):
    r = _router(settings, db, server, monkeypatch, tpd=10_000)
    db.llm_count("groq")
    db.llm_add_tokens("groq", 9_000)                       # not enough for a whole job: begin_job keeps only ollama
    assert r.begin_job(5_000) == ["ollama"]
    assert r.json("rephrase_bullets", "s", "u") == {"ok": True} and _S.seen == ["/ollama"]
    with pytest.raises(TaskUnavailable):
        r.json("cover_letter", "s", "u")
    assert _S.seen == ["/ollama"]                          # the letter sent nothing


def test_rephrase_bullets_may_fall_back_to_ollama(settings, db, server, monkeypatch):
    _S.limited = {"/groq"}
    r = _router(settings, db, server, monkeypatch)
    assert r.json("rephrase_bullets", "s", "u") == {"ok": True}
    assert _S.seen[-1] == "/ollama"


def test_a_pinned_ollama_router_cannot_write_the_letter(settings, db, server, monkeypatch):
    r = _router(settings, db, server, monkeypatch, only="ollama")
    with pytest.raises(TaskUnavailable):
        r.json("cover_letter", "s", "u")
    assert _S.seen == []
    assert r.json("rephrase_bullets", "s", "u") == {"ok": True}


def test_a_groq_answer_that_is_not_json_is_an_error_not_a_wait_and_still_never_reaches_ollama(settings, db, server,
                                                                                              monkeypatch):
    _S.garbage = {"/groq"}
    r = _router(settings, db, server, monkeypatch)
    with pytest.raises(LLMError) as e:
        r.json("cover_letter", "s", "u")
    assert not isinstance(e.value, TaskUnavailable) and "/ollama" not in _S.seen
