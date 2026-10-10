"""`llm.models`: a task may use another model than its provider's own (cover_letter -> groq openai/gpt-oss-120b). The model is
sent in the request, counted, paced and budgeted under its own key, shown per model in the usage, and `careerops doctor` /
`llm-check` send it a tiny request. No network: a local fake server echoes the model it was asked for."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from careerops.llm import Router, TaskUnavailable, check, router
from careerops.setup import model_checks

LETTER_MODEL = "openai/gpt-oss-120b"
KEY = f"groq/{LETTER_MODEL}"


class _S(BaseHTTPRequestHandler):
    bodies: list = []           # the JSON body of every request, in order
    fail_models: set = set()    # models answered with HTTP 404

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _S.bodies.append(body)
        if body["model"] in _S.fail_models:
            out, code = json.dumps({"error": {"message": f"The model `{body['model']}` does not exist"}}).encode(), 404
        else:
            out, code = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                                    "model": body["model"],
                                    "usage": {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100}}).encode(), 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    _S.bodies, _S.fail_models = [], set()
    monkeypatch.setattr(router, "_sleep", lambda s: None)
    srv = HTTPServer(("127.0.0.1", 0), _S)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def groq_settings(settings, server, monkeypatch):
    """The shipped settings with groq pointing at the fake server (and a fake key). These tests are about the letter model, so
    the letter is on (letter.mode: draft) and the judge's model (tests/review/test_judge.py) is out of the way."""
    monkeypatch.setenv("FAKE_GROQ", "k")
    groq = {**next(p for p in settings["llm"]["providers"] if p["name"] == "groq"), "base_url": server,
            "api_key_env": "FAKE_GROQ", "rpm": 60000, "tpm": 10**9, "tpd": 1000}
    settings.raw["llm"]["providers"] = [groq]
    settings.raw["letter"]["mode"] = "draft"
    settings.raw["llm"]["models"] = {"cover_letter": settings.raw["llm"]["models"]["cover_letter"]}
    return settings


def test_the_shipped_settings_send_only_the_letter_to_the_120b_model(settings):
    assert settings["llm"]["models"] == {"cover_letter": {"provider": "groq", "model": LETTER_MODEL},
                                         "judge": {"provider": "groq", "model": "llama-3.3-70b-versatile"}}
    groq = next(p for p in settings["llm"]["providers"] if p["name"] == "groq")
    assert groq["model"] == "openai/gpt-oss-20b"                         # every other task is unchanged
    assert settings["llm"]["task_providers"] == {"cover_letter": ["groq"], "judge": ["groq"]}


def test_the_letter_is_sent_with_the_letter_model_and_every_other_task_with_the_providers_own(groq_settings, db):
    r = Router(groq_settings, db)
    for task in ("extract_keywords", "cover_letter", "rephrase_bullets", "tailor_profile"):
        r.json(task, "s", "u")
    assert [(b["model"]) for b in _S.bodies] == ["openai/gpt-oss-20b", LETTER_MODEL, "openai/gpt-oss-20b",
                                                 "openai/gpt-oss-20b"]
    assert {b["reasoning_effort"] for b in _S.bodies} == {"low"}         # gpt-oss-120b is a reasoning model too
    assert {b["include_reasoning"] for b in _S.bodies} == {False}


def test_the_usage_records_which_model_answered_each_call(groq_settings, db):
    r = Router(groq_settings, db)
    r.json("extract_keywords", "s", "u")
    r.json("cover_letter", "s", "u")
    assert [(u["task"], u["provider"], u["model"], u["total_tokens"]) for u in r.usage] == [
        ("extract_keywords", "groq", "openai/gpt-oss-20b", 100), ("cover_letter", "groq", LETTER_MODEL, 100)]


def test_each_model_is_counted_under_its_own_key(groq_settings, db):
    r = Router(groq_settings, db)
    r.json("extract_keywords", "s", "u")
    r.json("cover_letter", "s", "u")
    r.json("cover_letter", "s", "u")
    assert (db.llm_calls_today("groq"), db.llm_tokens_today("groq")) == (1, 100)
    assert (db.llm_calls_today(KEY), db.llm_tokens_today(KEY)) == (2, 200)


def test_the_default_models_spent_budget_does_not_stop_the_letter_and_the_letter_models_does_not_stop_the_rest(
        groq_settings, db):
    r = Router(groq_settings, db)
    db.llm_count("groq")
    db.llm_add_tokens("groq", 1000)                                       # gpt-oss-20b: tpd 1000 reached
    assert r.json("cover_letter", "s", "u") == {"ok": True}                # gpt-oss-120b has its own day
    db.llm_count(KEY)
    db.llm_add_tokens(KEY, 1000)
    n = len(_S.bodies)
    with pytest.raises(TaskUnavailable):
        r.json("cover_letter", "s", "u")
    assert len(_S.bodies) == n                                            # nothing was sent for the letter


def test_the_job_budget_check_for_a_letter_uses_the_letter_models_budget(groq_settings, db):
    r = Router(groq_settings, db)
    db.llm_count("groq")
    db.llm_add_tokens("groq", 900)                                        # the default model has 100 left
    assert r.begin_job(500, task="cover_letter") == ["groq"]              # the letter model has 1000
    db.llm_count(KEY)
    db.llm_add_tokens(KEY, 800)
    with pytest.raises(router.BudgetExhausted, match="needs ~500"):
        r.begin_job(500, task="cover_letter")
    with pytest.raises(router.BudgetExhausted):
        r.begin_job(500)                                                  # and a whole job still checks the default model


def test_each_model_has_its_own_pacer(groq_settings, db):
    p = Router(groq_settings, db).providers[0]
    assert p.pacer_for(None) is p.pacer and p.pacer_for("openai/gpt-oss-20b") is p.pacer
    assert p.pacer_for(LETTER_MODEL) is not p.pacer and p.pacer_for(LETTER_MODEL) is p.pacer_for(LETTER_MODEL)


def test_a_provider_that_is_not_configured_is_ignored_by_the_router_and_named_by_the_router_for_doctor(settings, db):
    settings.raw["llm"]["providers"] = [p for p in settings["llm"]["providers"] if p["name"] == "ollama"]
    r = Router(settings, db)
    assert r.task_models == {} and "no LLM provider named 'groq'" in r.unknown_models[0]


@pytest.mark.parametrize("bad", ["openai/gpt-oss-120b", {"provider": "groq"}, {"model": "m"}])
def test_a_malformed_entry_is_refused(settings, db, bad):
    settings.raw["llm"]["models"] = {"cover_letter": bad}
    with pytest.raises(ValueError, match="llm.models.cover_letter"):
        Router(settings, db)


def test_a_pinned_ollama_router_uses_no_task_model(settings, db):
    r = Router(settings, db, only="ollama")
    assert r.model_for(r.providers[0], "cover_letter") is None


# ---------------------------------------------------------------- llm-check and doctor
def test_llm_check_checks_the_letter_model_as_well_as_each_provider(groq_settings, db):
    rows = check.check_all(Router(groq_settings, db))
    assert [(r["provider"], r["task"], r["configured_model"], r["ok"]) for r in rows] == [
        ("groq", None, "openai/gpt-oss-20b", True), ("groq", "cover_letter", LETTER_MODEL, True)]
    assert "groq (cover_letter)" in check.format_row(rows[1])[0] and LETTER_MODEL in check.format_row(rows[1])[0]
    assert [b["model"] for b in _S.bodies] == ["openai/gpt-oss-20b", LETTER_MODEL]


def test_doctor_checks_that_the_letter_model_answers(groq_settings):
    lines = [(n, ok, h) for n, ok, h in model_checks(groq_settings) if "answers" in n]
    assert len(lines) == 1 and lines[0][1] is True
    assert lines[0][0].startswith(f"groq/{LETTER_MODEL} answers for cover_letter (") and "served by" in lines[0][0]
    assert [b["model"] for b in _S.bodies] == [LETTER_MODEL]               # one tiny request, for that model only


def test_doctor_fails_with_the_providers_message_when_the_model_does_not_answer(groq_settings):
    _S.fail_models = {LETTER_MODEL}
    ((name, ok, hint),) = model_checks(groq_settings)
    assert not ok and name == f"groq/{LETTER_MODEL} answers for cover_letter"
    assert "HTTP 404" in hint and "does not exist" in hint


def test_doctor_fails_without_the_key_and_sends_nothing(groq_settings, monkeypatch):
    monkeypatch.delenv("FAKE_GROQ")
    ((name, ok, hint),) = model_checks(groq_settings)
    assert not ok and "FAKE_GROQ is not set" in hint and _S.bodies == []


def test_doctor_no_ping_sends_nothing_and_says_so(groq_settings):
    ((name, ok, hint),) = model_checks(groq_settings, ping=False)
    assert ok and "not asked (--no-ping)" in name and _S.bodies == []


@pytest.mark.parametrize("argv, ping", [(["doctor"], True), (["doctor", "--no-ping"], False)])
def test_the_doctor_command_pings_unless_told_not_to(argv, ping, monkeypatch):
    from careerops import cli
    seen = []
    monkeypatch.setattr(cli, "doctor", lambda s, ping=True: seen.append(ping) or [])
    monkeypatch.setattr(cli, "load_dotenv", lambda path: None)          # never read the real .env (it holds the real key)
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 0 and seen == [ping]


def test_doctor_names_a_models_entry_whose_provider_does_not_exist(groq_settings):
    groq_settings.raw["llm"]["models"] = {"cover_letter": {"provider": "nowhere", "model": "m"}}
    out = model_checks(groq_settings)
    assert len(out) == 1 and not out[0][1] and "no LLM provider named 'nowhere'" in out[0][0]


def test_doctor_has_no_model_line_when_no_task_has_its_own_model(groq_settings):
    del groq_settings.raw["llm"]["models"]
    assert model_checks(groq_settings) == [] and _S.bodies == []


def test_a_connection_failure_is_a_failed_check_not_a_crash(groq_settings, monkeypatch):
    def refuse(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(router.requests, "post", refuse)
    ((name, ok, hint),) = model_checks(groq_settings)
    assert not ok and "refused" in hint


# ---------------------------------------------------------------- reasoning parameters belong to the model that reasons
def test_a_task_model_that_does_not_reason_is_not_sent_the_providers_reasoning_settings(groq_settings, db):
    """The shipped groq entry says reasoning_effort: low for gpt-oss-20b. The judge's llama-3.3-70b-versatile must not get it
    (Groq answers 400 for a model that does not reason), while a gpt-oss task model still gets the default."""
    groq_settings.raw["llm"]["models"]["judge"] = {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    r = Router(groq_settings, db)
    r.json("judge", "s", "u")
    r.json("cover_letter", "s", "u")
    r.json("extract_keywords", "s", "u")
    by_model = {b["model"]: b for b in _S.bodies}
    assert "reasoning_effort" not in by_model["llama-3.3-70b-versatile"] and "include_reasoning" not in by_model["llama-3.3-70b-versatile"]
    assert by_model[LETTER_MODEL]["reasoning_effort"] == "low" and by_model[LETTER_MODEL]["include_reasoning"] is False
    assert by_model["openai/gpt-oss-20b"]["reasoning_effort"] == "low"
