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


def _router(settings, db, server, *names, json_mode=True, **cfg):
    settings.raw["llm"]["task_providers"] = {}          # routing is tested in test_router_routing.py
    settings.raw["llm"]["providers"] = [
        {"name": n, "base_url": f"{server}/{n}", "api_key_env": "FAKE_KEY", "model": "m", "rpm": 60000, "rpd": 100,
         "tpm": 10**9, "json_mode": json_mode, **cfg} for n in names]
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


# ---------------------------------------------------------------- 6. pacing to the provider's limits
@pytest.fixture
def fake_time(monkeypatch):
    """A clock that only moves when the router sleeps to pace itself."""
    t = {"now": 1000.0, "slept": []}

    def nap(s):
        t["slept"].append(round(s, 3))
        t["now"] += s

    monkeypatch.setattr(router, "_clock", lambda: t["now"])
    monkeypatch.setattr(router, "_pace_sleep", nap)
    return t


def _ok(headers=None, usage=None, content='{"ok": true}', finish="stop"):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish}], "model": "served-model"}
    if usage:
        body["usage"] = usage
    return (200, headers or {}, body)


def test_waits_for_the_token_window_named_in_the_response_headers(settings, db, server, fake_time):
    hdr = {"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "5.2s",
           "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-requests": "900",
           "x-ratelimit-reset-requests": "1m26.4s"}
    _S.script["/p"] = [_ok(hdr)]
    r = _router(settings, db, server, "p", tpm=8000)
    r.json("t", "s", "u")
    assert fake_time["slept"] == []                       # nothing known yet, and the first call fits
    r.json("t", "s", "u")                                 # ~1000 tokens asked, 100 left, window resets in 5.2 s
    assert fake_time["slept"] == [5.25] and len(_hits("p")) == 2
    r.json("t", "s", "u")                                 # the response to the 2nd call re-armed the window
    assert fake_time["slept"] == [5.25, 5.25]


def test_no_wait_when_the_budget_covers_the_call(settings, db, server, fake_time):
    _S.script["/p"] = [_ok({"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "9s"})]
    r = _router(settings, db, server, "p", tpm=8000)
    for _ in range(3):
        r.json("t", "s", "u")
    assert fake_time["slept"] == []


def test_without_headers_it_paces_to_the_configured_tpm(settings, db, server, fake_time):
    _S.script["/p"] = [_ok()]
    r = _router(settings, db, server, "p", tpm=2500)       # each call is estimated at ~1000 (max_tokens) + a few input
    r.json("t", "s", "u"); r.json("t", "s", "u")
    assert fake_time["slept"] == []                        # 2 x ~1000 fits in 2500
    r.json("t", "s", "u")                                  # a third does not: wait for the first to leave the minute
    assert fake_time["slept"] == [60.05]
    fake_time["now"] += 61
    r.json("t", "s", "u")
    assert fake_time["slept"] == [60.05]                   # a quiet minute resets everything


def test_without_headers_it_paces_to_the_configured_rpm(settings, db, server, fake_time):
    _S.script["/p"] = [_ok()]
    r = _router(settings, db, server, "p", rpm=2)
    for _ in range(3):
        r.json("t", "s", "u")
    assert fake_time["slept"] == [60.05]


def test_a_spent_daily_request_budget_is_not_waited_for_the_router_moves_on(settings, db, server, fake_time):
    _S.script["/a"] = [_ok({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2h3m"})]
    _S.script["/b"] = [_ok()]
    r = _router(settings, db, server, "a", "b")
    r.json("t", "s", "u")                                  # served by a; its headers say the day's requests are gone
    r.json("t", "s", "u")                                  # a is skipped without sleeping 2 hours, b answers
    assert fake_time["slept"] == [] and len(_hits("a")) == 1 and len(_hits("b")) == 1
    assert any(e["provider"] == "a" and "pacing would wait 7380s" in e["body"] for e in _events(db))


def test_defaults_are_8000_tpm_and_30_rpm_and_null_means_unlimited(settings, db):
    from careerops.llm.router import Provider
    base = {"name": "p", "base_url": "http://x", "model": "m"}
    p = Provider(dict(base), db, 0.3, 10)
    assert (p.pacer.tpm, p.pacer.rpm) == (8000, 30)
    q = Provider({**base, "tpm": None, "rpm": 0}, db, 0.3, 10)
    assert (q.pacer.tpm, q.pacer.rpm) == (None, None) and q.pacer.wait_needed(10**6) == 0.0


def test_settings_carry_the_pacing_limits_and_per_task_caps(settings):
    llm = settings["llm"]
    assert llm["max_tokens"] == {"extract_keywords": 2000, "rephrase_bullets": 1200, "judge": 800, "free_text_answer": 1500,
                              "cover_letter": 3000, "default": 1000}
    groq = next(p for p in llm["providers"] if p["name"] == "groq")
    assert (groq["tpm"], groq["rpm"], groq["tpd"]) == (8000, 30, 200000)
    assert (groq["reasoning_effort"], groq["include_reasoning"]) == ("low", False)
    assert llm["job_token_estimate"] == 10000


# ---------------------------------------------------------------- 7. max_tokens per task and usage per call
def test_each_task_sends_its_own_max_tokens(settings, db, server, fake_time):
    _S.script["/p"] = [_ok()]
    r = _router(settings, db, server, "p")
    for task in ("extract_keywords", "rephrase_bullets", "cover_letter", "something_else"):
        r.json(task, "s", "u")
    assert [b["max_tokens"] for b in _hits("p")] == [2000, 1200, 3000, 1000]


def test_max_tokens_falls_back_to_a_built_in_default_when_settings_have_none(settings, db, server, fake_time):
    _S.script["/p"] = [_ok()]
    del settings.raw["llm"]["max_tokens"]
    r = _router(settings, db, server, "p")
    r.json("rephrase_bullets", "s", "u")
    assert _hits("p")[0]["max_tokens"] == 1000


def test_actual_usage_is_recorded_per_call_and_counted_against_tpd(settings, db, server, fake_time):
    use = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    _S.script["/p"] = [_ok(usage=use)]
    r = _router(settings, db, server, "p", tpd=250)
    r.json("rephrase_bullets", "s", "u")
    assert r.usage == [{"task": "rephrase_bullets", "provider": "p", "model": "served-model", "max_tokens": 1200,
                        "est_input_tokens": 1, "prompt_tokens": 120, "completion_tokens": 30, "reasoning_tokens": None, "total_tokens": 150,
                        "finish_reason": "stop"}]
    assert db.llm_tokens_today("p") == 150 and r.providers[0].available()
    r.json("cover_letter", "s", "u")
    assert db.llm_tokens_today("p") == 300 and not r.providers[0].available()      # tpd 250 reached
    with pytest.raises(LLMError, match="unavailable"):
        r.json("cover_letter", "s", "u")


def test_an_answer_cut_off_by_max_tokens_names_the_setting_and_is_not_retried(settings, db, server, fake_time):
    _S.script["/p"] = [_ok(content='{"required": ["Pyth', finish="length")]
    r = _router(settings, db, server, "p")
    with pytest.raises(LLMError) as e:
        r.json("extract_keywords", "s", "u")
    assert "cut off at max_tokens=2000" in str(e.value) and "llm.max_tokens.extract_keywords" in str(e.value)
    assert len(_hits("p")) == 1


def test_an_old_database_gets_the_tokens_column_and_keeps_its_rows(tmp_path):
    import sqlite3
    from careerops.db import DB
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE llm_usage (provider TEXT, day TEXT, calls INTEGER, PRIMARY KEY (provider, day))")
    old.execute("INSERT INTO llm_usage VALUES ('groq', date('now'), 7)")
    old.commit(); old.close()
    db = DB(path)
    assert db.llm_calls_today("groq") == 7 and db.llm_tokens_today("groq") == 0
    db.llm_count("groq"); db.llm_add_tokens("groq", 40)
    assert db.llm_calls_today("groq") == 8 and db.llm_tokens_today("groq") == 40


# ---------------------------------------------------------------- reasoning parameters (gpt-oss)
def _body_for(settings, db, server, fake_time, model, **cfg):
    _S.script["/p"] = [_ok()]
    r = _router(settings, db, server, "p", model=model, **cfg)
    r.json("t", "s", "u")
    return _hits("p")[-1]


def test_gpt_oss_gets_low_reasoning_effort_and_no_reasoning_text_by_default(settings, db, server, fake_time):
    body = _body_for(settings, db, server, fake_time, "openai/gpt-oss-20b")
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False


def test_explicit_reasoning_settings_win(settings, db, server, fake_time):
    body = _body_for(settings, db, server, fake_time, "openai/gpt-oss-20b", reasoning_effort="medium",
                     include_reasoning=True)
    assert body["reasoning_effort"] == "medium" and body["include_reasoning"] is True


def test_other_models_get_neither_parameter_unless_configured(settings, db, server, fake_time):
    body = _body_for(settings, db, server, fake_time, "qwen2.5:7b-instruct")
    assert "reasoning_effort" not in body and "include_reasoning" not in body
    body = _body_for(settings, db, server, fake_time, "qwen2.5:7b-instruct", reasoning_effort="high")
    assert body["reasoning_effort"] == "high" and "include_reasoning" not in body


def test_the_shipped_groq_provider_sends_both(settings, db, server, fake_time):
    groq = dict(next(p for p in settings["llm"]["providers"] if p["name"] == "groq"))
    _S.script["/g"] = [_ok()]
    settings.raw["llm"]["providers"] = [{**groq, "base_url": f"{server}/g", "api_key_env": "FAKE_KEY", "rpm": 60000,
                                         "tpm": 10**9}]
    Router(settings, db).json("t", "s", "u")
    body = _hits("g")[0]
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False and body["model"] == groq["model"]


def test_reasoning_tokens_are_recorded_when_the_provider_reports_them(settings, db, server, fake_time):
    use = {"prompt_tokens": 100, "completion_tokens": 400, "total_tokens": 500,
           "completion_tokens_details": {"reasoning_tokens": 350}}
    _S.script["/p"] = [_ok(usage=use)]
    r = _router(settings, db, server, "p")
    r.json("rephrase_bullets", "s", "u")
    assert r.usage[0]["reasoning_tokens"] == 350 and r.usage[0]["completion_tokens"] == 400


# ---------------------------------------------------------------- daily token budget before a job
def _spend(db, name, tokens):
    db.llm_count(name)
    db.llm_add_tokens(name, tokens)


def test_a_job_is_not_started_when_it_would_not_fit_in_the_daily_budget(settings, db, server, fake_time):
    from careerops.llm import BudgetExhausted
    _S.script["/a"] = [_ok()]
    r = _router(settings, db, server, "a", tpd=100_000)
    _spend(db, "a", 85_000)
    with pytest.raises(BudgetExhausted) as e:
        r.begin_job(20_000)
    assert "a: 85,000 of 100,000 tokens used today, 15,000 left, a job needs ~20,000" in str(e.value)
    assert r.begin_job(15_000) == ["a"]                      # exactly what is left is fine
    assert _hits("a") == []                                  # checking the budget costs no request


def test_the_next_provider_takes_the_job_when_the_first_budget_is_spent(settings, db, server, fake_time):
    _S.script["/a"] = [_ok()]
    _S.script["/b"] = [_ok()]
    r = _router(settings, db, server, "a", "b", tpd=100_000)
    r.providers[1].cfg["tpd"] = None                         # b (a local model) has no daily cap
    _spend(db, "a", 95_000)
    assert r.begin_job(20_000) == ["b"]
    for _ in range(3):
        r.json("t", "s", "u")
    assert _hits("a") == [] and len(_hits("b")) == 3          # the whole job went to b, none to a
    r.end_job()
    _spend(db, "a", 0)                                        # the next job starts afresh
    assert r.begin_job(1_000) == ["a", "b"]


def test_a_budget_stop_leaves_every_job_untouched_and_says_why(settings, db, server, fake_time, monkeypatch, capsys):
    from careerops.tailoring import tailor as tailor_mod
    _S.script["/a"] = [_ok()]
    r = _router(settings, db, server, "a", tpd=50_000)
    _spend(db, "a", 45_000)
    for i in (1, 2):
        db.upsert_job({"id": f"gh:x:{i}", "source": "greenhouse", "company": f"Co{i}", "title": "AI Engineer",
                       "location": "London, UK", "url": "u", "apply_url": "u", "description": "d" * 900})
        db.update_job(f"gh:x:{i}", status="triaged", tier=1, ceiling_pre=0.9)
    called = []
    monkeypatch.setattr(tailor_mod, "tailor_job", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError("a job must not start with no budget")))
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: r)
    cli.main(["tailor", "--limit", "2"])
    out = capsys.readouterr().out
    assert called == [] and "STOP" in out and "Nothing was half-tailored" in out
    assert "a: 45,000 of 50,000 tokens used today" in out and "2 job(s) left untouched" in out
    assert [j["status"] for j in db.jobs()] == ["triaged", "triaged"]
    assert not list(settings.path("outputs").glob("*"))                       # no folder, not even a partial one
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE stage='error'").fetchone()[0] == 0


def test_a_job_that_fits_is_started_and_the_budget_gate_is_released_after_it(settings, db, server, fake_time,
                                                                             monkeypatch, capsys):
    from careerops.tailoring import tailor as tailor_mod
    r = _router(settings, db, server, "a", tpd=500_000)
    db.upsert_job({"id": "gh:x:1", "source": "greenhouse", "company": "Co", "title": "AI Engineer",
                   "location": "London, UK", "url": "u", "apply_url": "u", "description": "d" * 900})
    db.update_job("gh:x:1", status="triaged", tier=1, ceiling_pre=0.9)
    seen = []
    monkeypatch.setattr(tailor_mod, "tailor_job", lambda *a, **k: seen.append(r._allowed) or (_ for _ in ()).throw(
        RuntimeError("stub")))
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: r)
    cli.main(["tailor", "--limit", "1"])
    assert seen == [{"a"}] and r._allowed is None                             # gated during the job, open after
    assert db.job("gh:x:1")["status"] == "error"                              # the stub's failure is an ordinary error


# ---------------------------------------------------------------- careerops status: tokens today vs tpd
def test_status_shows_tokens_and_requests_used_today_against_the_limits(settings, db, monkeypatch, capsys):
    settings.raw["llm"]["providers"] = [
        {"name": "groq", "base_url": "x", "model": "m", "tpd": 200_000, "rpd": 1000},
        {"name": "ollama", "base_url": "x", "model": "m", "rpd": 1_000_000}]
    _spend(db, "groq", 12_345)
    db.llm_count("groq")
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "LLM usage today" in out
    assert "groq" in out and "12,345 / 200,000 tokens (6%)" in out and "2 / 1,000 requests" in out
    assert "ollama" in out and "0 tokens (no tpd set)" in out


# ---------------------------------------------------------------- the raw reply is kept, even when it is not JSON
def test_the_router_keeps_the_raw_reply_of_an_answer_that_is_not_valid_json(settings, db, server, fake_time):
    _S.script["/p"] = [_ok(content="Sure! Here is the CV: {not really json")]
    r = _router(settings, db, server, "p")
    with pytest.raises(LLMError):
        r.json("rephrase_bullets", "s", "u")
    assert r.last_raw == "Sure! Here is the CV: {not really json"
    _S.script["/p"] = [_ok(content='{"a": 1}')]
    r.json("rephrase_bullets", "s", "u")
    assert r.last_raw == '{"a": 1}'


# ---------------------------------------------------------------- 8. temperature 0 and a seed for every task
def test_settings_run_every_task_at_temperature_zero_with_a_seed(settings):
    assert settings["llm"]["temperature"] == 0 and settings["llm"]["seed"] == 7


def test_every_request_carries_temperature_zero_and_the_seed(settings, db, server, fake_time):
    _S.script["/p"] = [_ok()]
    r = _router(settings, db, server, "p")
    for task in ("extract_keywords", "rephrase_bullets", "tailor_profile", "cover_letter"):
        r.json(task, "s", "u")
    assert [(b["temperature"], b["seed"]) for b in _hits("p")] == [(0, 7)] * 4


def test_a_provider_may_set_its_own_seed_or_none_at_all(settings, db, server, fake_time):
    _S.script["/a"] = _S.script["/b"] = [_ok()]
    settings.raw["llm"]["providers"] = [
        {"name": "a", "base_url": f"{server}/a", "api_key_env": "FAKE_KEY", "model": "m", "tpm": 10**9, "seed": 99},
        {"name": "b", "base_url": f"{server}/b", "api_key_env": "FAKE_KEY", "model": "m", "tpm": 10**9, "seed": None}]
    r = Router(settings, db)
    r.json("t", "s", "u")                                         # provider a answers
    assert _hits("a")[0]["seed"] == 99
    r.providers = r.providers[1:]
    r.json("t", "s", "u")
    assert "seed" not in _hits("b")[0] and _hits("b")[0]["temperature"] == 0


def test_without_a_seed_in_settings_none_is_sent_and_the_temperature_default_is_unchanged(settings, db, server,
                                                                                         fake_time):
    _S.script["/p"] = [_ok()]
    del settings.raw["llm"]["seed"]
    del settings.raw["llm"]["temperature"]
    _router(settings, db, server, "p").json("t", "s", "u")
    assert "seed" not in _hits("p")[0] and _hits("p")[0]["temperature"] == 0.3
