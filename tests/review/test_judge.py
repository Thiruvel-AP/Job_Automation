"""The LLM judge (review/judge.py): configuration, the question it is asked, the verdict parser, the append-only log, the
statistics (agreement, Cohen's kappa, false approves, rule of three) and the gate. No network: MockLLM / SpyLLM, and one test
through the real Router with a fake `requests.post`."""
import csv
import json

import pytest
import requests

from careerops import cli
from careerops.llm import MockLLM, Router, router
from careerops.llm.router import LLMError
from careerops.review import judge
from careerops.review.judge import Verdict
from careerops.tailoring.tailor import bullet_index


# ------------------------------------------------------------------ helpers
def make_rep(fb, letter_status="ok", used_models=("openai/gpt-oss-20b", "openai/gpt-oss-120b")):
    exp, proj = fb["experience"][0], fb["projects"][0]
    ctx = {"id": {"name": "Ada Lovelace", "email": "ada.lovelace@example.org", "phone": "+44 7700 900123"},
           "profile": "Data scientist with LLM agent experience.", "skills": {"Core": ["Python", "SQL"]},
           "experience": [{**exp, "bullet_ids": [b["id"] for b in exp["bullets"][:2]],
                           "bullets": [b["text"] for b in exp["bullets"][:2]]}],
           "projects": [{**proj, "bullet_ids": [proj["bullets"][0]["id"]], "bullets": [proj["bullets"][0]["text"]]}],
           "education": [{"degree": "MSc Data Science", "org": "University of Northfield", "dates": "2025"}]}
    ev = [{"id": proj["bullets"][0]["id"], "text": proj["bullets"][0]["text"]}]
    letter = ({"status": "ok", "paragraphs": ["Hi Northwind team.", "I built an agent.", "Could we talk?"], "evidence": ev}
              if letter_status == "ok" else {"status": letter_status, "paragraphs": [], "evidence": ev})
    return {"context": ctx, "letter": letter, "llm_usage": {"calls": [{"model": m, "task": "x"} for m in used_models]}}


def a_job(job):
    return {**job, "flags": "[]"}


def row(judge_says, human):
    """A log row as log_decision writes it (judge_says: 'true' | 'false' | '')."""
    return {"human": human, "judge_would_send": judge_says}


def rows(tp=0, fa=0, fr=0, tn=0, unjudged=0, auto=0):
    return ([row("true", "approve")] * tp + [row("true", "reject")] * fa + [row("false", "approve")] * fr
            + [row("false", "reject")] * tn + [row("", "approve")] * unjudged + [row("true", "auto")] * auto)


# ------------------------------------------------------------------ configuration
@pytest.mark.parametrize("model, family", [
    ("openai/gpt-oss-20b", "openai"), ("openai/gpt-oss-120b", "openai"), ("gpt-4o", "openai"),
    ("llama-3.3-70b-versatile", "llama"), ("meta-llama/llama-4-scout-17b", "llama"), ("qwen2.5-7b-8k", "qwen"),
    ("qwen/qwen3-32b", "qwen"), ("mistral-small", "mistral"), ("gemma2-9b-it", "google"), ("deepseek-r1", "deepseek"),
    ("kimi-k2", "kimi")])
def test_model_family(model, family):
    assert judge.model_family(model) == family


def test_the_shipped_judge_is_the_local_qwen_pinned_to_ollama(settings):
    assert judge.judge_model(settings) == {"provider": "ollama", "model": "qwen2.5-7b-8k"}
    assert judge.judge_mode(settings) == "shadow" and judge.check_config(settings) == []
    assert settings["llm"]["task_providers"]["judge"] == ["ollama"]
    assert judge.model_family("qwen2.5-7b-8k") != judge.model_family(settings["llm"]["providers"][0]["model"])
    ollama = next(p for p in settings["llm"]["providers"] if p["name"] == "ollama")
    assert ollama["model"] == "qwen2.5-7b-8k" and ollama["context_tokens"] == 8192      # the judge input is fitted to this


def test_a_judge_of_the_generators_family_is_refused(settings):
    settings.raw["llm"]["models"]["judge"] = {"provider": "groq", "model": "openai/gpt-oss-safeguard-20b"}
    settings.raw["llm"]["task_providers"]["judge"] = ["groq"]
    problems = judge.check_config(settings)
    assert len(problems) == 1 and "same family as the generator" in problems[0] and "gpt-oss-20b" in problems[0]


def test_a_missing_judge_model_or_provider_pin_is_a_problem(settings):
    del settings.raw["llm"]["task_providers"]["judge"]
    assert "llm.task_providers.judge: [ollama]" in judge.check_config(settings)[0]
    del settings.raw["llm"]["models"]["judge"]
    assert "llm.models.judge is not set" in judge.check_config(settings)[0]


def test_a_groq_llama_is_also_a_different_family_from_gpt_oss(settings):
    settings.raw["llm"]["models"]["judge"] = {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    settings.raw["llm"]["task_providers"]["judge"] = ["groq"]
    assert judge.check_config(settings) == []


def test_an_unquoted_off_in_yaml_is_the_boolean_false_and_still_means_off(settings):
    import yaml
    assert yaml.safe_load("judge: off") == {"judge": False}                          # the trap
    settings.raw["review"]["judge"] = False
    assert judge.judge_mode(settings) == "off" and judge.check_config(settings) == []


def test_off_needs_no_judge_model_and_a_bad_mode_raises(settings):
    settings.raw["review"]["judge"] = "off"
    del settings.raw["llm"]["models"]["judge"]
    assert judge.check_config(settings) == []
    settings.raw["review"]["judge"] = "always"
    with pytest.raises(ValueError, match="review.judge must be one of off, shadow, gate"):
        judge.judge_mode(settings)


def test_the_judge_is_not_refused_when_no_model_of_its_family_wrote_the_job(fb_fixture):
    rep = make_rep(fb_fixture, used_models=("openai/gpt-oss-20b",))
    assert judge.check_family("qwen2.5-7b-8k", rep) is None
    rep = make_rep(fb_fixture, used_models=("openai/gpt-oss-20b", "qwen2.5-7b-8k"))              # Ollama stood in for Groq
    assert "same family as a model that wrote this job (qwen2.5-7b-8k)" in judge.check_family("qwen2.5-7b-8k", rep)


# ------------------------------------------------------------------ the question
def test_the_judge_is_shown_the_jd_cv_letter_and_the_source_facts_but_not_the_contact_block(settings, fb_fixture, job):
    rep = make_rep(fb_fixture)
    inp = judge.build_input(job, rep, fb_fixture)
    assert inp["title"] == "AI Engineer" and inp["company"] == "Northwind Health AI" and inp["jd"] == job["description"]
    assert "Data scientist with LLM agent experience." in inp["cv"] and "Python, SQL" in inp["cv"] and "MSc Data Science" in inp["cv"]
    assert "I built an agent." in inp["letter"]
    bidx = bullet_index(fb_fixture)
    for b in rep["context"]["experience"][0]["bullet_ids"]:
        assert f"[{b}] {bidx[b]['text']}" in inp["facts"]
    blob = judge.USER.format(**inp) + judge.SYSTEM
    for private in ("Ada Lovelace", "ada.lovelace@example.org", "+44 7700 900123"):
        assert private not in blob


def test_a_letter_that_is_off_none_or_pending_is_shown_as_no_letter(fb_fixture, job):
    for status in ("off", "none", "pending"):
        assert judge.build_input(job, make_rep(fb_fixture, status), fb_fixture)["letter"] == "(no cover letter)"


def test_a_very_long_jd_is_cut_before_it_is_sent(fb_fixture, job):
    inp = judge.build_input({**job, "description": "word " * 5000}, make_rep(fb_fixture), fb_fixture)
    assert len(inp["jd"]) < judge.JD_CHARS + 30 and inp["jd"].endswith("[JD cut here]")


# ------------------------------------------------------------------ the verdict
@pytest.mark.parametrize("out, yes, score", [
    ({"would_send": True, "score": 5, "issues": []}, True, 5),
    ({"would_send": False, "score": 2, "issues": ["invented a number"]}, False, 2),
    ({"would_send": "yes", "score": "4", "issues": "one nit"}, True, 4),
    ({"would_send": "No", "score": 3.0, "issues": None}, False, 3),
    ({"would_send": True, "score": 2, "issues": []}, False, 2)])                 # contradiction: counted as a no
def test_parse_verdict(out, yes, score):
    v = judge.parse_verdict(out, model="m")
    assert v.would_send is yes and v.score == score and v.error == "" and v.yes is yes


def test_a_contradictory_answer_says_so_in_the_issues():
    v = judge.parse_verdict({"would_send": True, "score": 2, "issues": ["x"]})
    assert v.issues[-1] == "(judge said would_send with a score of 2: counted as a no)"


@pytest.mark.parametrize("out", [None, [], "yes", {}, {"would_send": True}, {"score": 4}, {"would_send": "maybe", "score": 4},
                                 {"would_send": True, "score": 6}, {"would_send": True, "score": 0},
                                 {"would_send": True, "score": True}, {"would_send": 1, "score": 4}])
def test_an_answer_that_is_not_the_asked_shape_gives_no_verdict(out):
    v = judge.parse_verdict(out, model="m")
    assert v.would_send is None and v.score is None and v.error and not v.yes
    assert "no verdict" in v.line()


# ------------------------------------------------------------------ run_judge
def spy(spy_llm, answer=None):
    return spy_llm({"judge": lambda ctx: answer if answer is not None else {"would_send": True, "score": 4, "issues": ["nit"]}})


def test_run_judge_asks_once_with_the_judge_task_and_the_input_and_caches_the_verdict(settings, fb_fixture, job, spy_llm):
    llm, rep = spy(spy_llm), make_rep(fb_fixture)
    v = judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert v.would_send is True and v.score == 4 and v.issues == ["nit"] and v.model == "mock" and v.key
    assert [s["task"] for s in llm.sent] == ["judge"]
    assert "The role" in llm.sent[0]["user"] and "What you'll do" in llm.sent[0]["user"]          # the role part of the JD
    assert "About Northwind Health AI" not in llm.sent[0]["user"]                                  # not the employer's pitch
    assert judge.SYSTEM == llm.sent[0]["system"]
    again = judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert len(llm.sent) == 1 and again == v                                        # the cache
    rep["letter"]["paragraphs"].append("A new sentence.")                           # a regenerated letter is judged afresh
    judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert len(llm.sent) == 2


def test_run_judge_never_writes_to_the_job_or_its_report(settings, fb_fixture, job, spy_llm):
    rep = make_rep(fb_fixture)
    before = json.dumps(rep, sort_keys=True), json.dumps(job, sort_keys=True, default=str), json.dumps(fb_fixture, default=str)
    judge.run_judge(spy(spy_llm), settings, job, rep, fb_fixture)
    assert (json.dumps(rep, sort_keys=True), json.dumps(job, sort_keys=True, default=str), json.dumps(fb_fixture, default=str)) == before


def test_a_failed_call_is_a_verdict_with_an_error_not_an_exception_and_is_not_cached(settings, fb_fixture, job, spy_llm):
    def down(ctx):
        raise LLMError("all providers failed")
    llm, rep = spy_llm({"judge": down}), make_rep(fb_fixture)
    v = judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert v.would_send is None and "the judge call failed" in v.error and "all providers failed" in v.error
    judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert len(llm.sent) == 2


def test_task_unavailable_and_garbage_answers_are_verdicts_with_errors(settings, fb_fixture, job, spy_llm):
    from careerops.llm import TaskUnavailable

    def none_can(ctx):
        raise TaskUnavailable("judge can only run on groq")
    v = judge.run_judge(spy_llm({"judge": none_can}), settings, job, make_rep(fb_fixture), fb_fixture)
    assert "cannot take the call now" in v.error and v.would_send is None
    v = judge.run_judge(spy(spy_llm, {"verdict": "fine"}), settings, job, make_rep(fb_fixture), fb_fixture)
    assert v.would_send is None and "lacks a boolean would_send" in v.error


def test_run_judge_refuses_a_misconfigured_judge_without_asking(settings, fb_fixture, job, spy_llm):
    settings.raw["llm"]["models"]["judge"] = {"provider": "groq", "model": "openai/gpt-oss-20b"}
    llm = spy(spy_llm)
    v = judge.run_judge(llm, settings, job, make_rep(fb_fixture), fb_fixture)
    assert "same family as the generator" in v.error and llm.sent == []


def test_no_llm_is_a_verdict_with_an_error(settings, fb_fixture, job):
    assert judge.run_judge(None, settings, job, make_rep(fb_fixture), fb_fixture).error == "no LLM for this run"


class _Resp:
    status_code, ok, headers = 200, True, {}

    def __init__(self, body, content='{"would_send": false, "score": 2, "issues": ["wrong role"]}'):
        self.body = body
        self.text = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "model": body["model"],
                                "usage": {"prompt_tokens": 900, "completion_tokens": 30, "total_tokens": 930}})

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def ollama_judge(settings, db, monkeypatch):
    """The shipped judge (Ollama qwen2.5-7b-8k) through the real Router, `requests.post` replaced by a script of replies."""
    sent, replies = [], []
    monkeypatch.setattr(router, "_pace_sleep", lambda s: None)
    monkeypatch.setattr(router, "_sleep", lambda s: None)

    def post(url, headers=None, json=None, timeout=None):
        sent.append({"url": url, **json})
        step = replies.pop(0) if replies else _Resp(json)
        if isinstance(step, Exception):
            raise step
        return step(json) if callable(step) else step
    monkeypatch.setattr(router.requests, "post", post)
    return Router(settings, db), sent, replies


def test_through_the_router_the_judge_runs_on_the_local_model_and_the_verdict_names_it(ollama_judge, settings, fb_fixture, job):
    r, sent, _ = ollama_judge
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    assert [(b["url"], b["model"]) for b in sent] == [("http://localhost:11434/v1/chat/completions", "qwen2.5-7b-8k")]
    assert "reasoning_effort" not in sent[0] and sent[0]["max_tokens"] == 800 and sent[0]["response_format"] == {"type": "json_object"}
    assert v.would_send is False and v.score == 2 and v.issues == ["wrong role"] and v.model == "ollama/qwen2.5-7b-8k"
    assert r.db.llm_tokens_today("ollama") == 930 and r.db.llm_tokens_today("groq") == 0 and r.db.llm_calls_today("groq") == 0
    assert v.tokens == 900 and "Judge input: 900 prompt tokens" in v.input_line()


def test_the_judge_never_goes_to_groq_even_when_ollama_is_down(ollama_judge, settings, fb_fixture, job, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r, sent, replies = ollama_judge
    replies += [requests.ConnectionError("refused")] * 2
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    assert v.would_send is None and all("11434" in b["url"] for b in sent)


def test_ollama_unreachable_is_retried_once_then_no_verdict_and_the_review_goes_on(ollama_judge, settings, fb_fixture, job):
    r, sent, replies = ollama_judge
    replies += [requests.ConnectionError("refused"), requests.ConnectionError("refused")]
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    assert len(sent) == 2                                               # one try and one retry, no more
    assert v.would_send is None and not v.yes and "the judge call failed" in v.error and "refused" in v.error
    assert "no verdict" in v.line()
    # logged beside the human decision like any other verdict
    judge.log_decision(settings, job, make_rep(fb_fixture), v, "approve")
    (row,) = judge.read_log(judge.log_path(settings))
    assert row["judge_would_send"] == "" and "refused" in row["judge_error"] and row["human"] == "approve"
    assert any(e["stage"] == "judge" and "failed" in e["detail"] for e in r.db.conn.execute("SELECT * FROM events"))


def test_an_answer_that_is_not_json_is_retried_once_and_a_good_second_answer_is_the_verdict(ollama_judge, settings, fb_fixture, job):
    r, sent, replies = ollama_judge
    replies += [lambda b: _Resp(b, "I think this looks fine, send it"),
                lambda b: _Resp(b, '{"would_send": true, "score": 5, "issues": []}')]
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    assert len(sent) == 2 and v.would_send is True and v.score == 5


def test_two_answers_that_are_not_json_give_no_verdict(ollama_judge, settings, fb_fixture, job):
    r, sent, replies = ollama_judge
    replies += [lambda b: _Resp(b, "no json here")] * 2
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    assert len(sent) == 2 and v.would_send is None and "valid JSON" in v.error


def test_a_failed_judge_does_not_stop_the_review(settings, db, fb_fixture, job, monkeypatch, capsys):
    """review_job asks the judge, gets 'no verdict', and the prompt still appears and the decision is still taken."""
    from careerops.review import workflow
    rep = make_rep(fb_fixture)
    monkeypatch.setattr(workflow, "load_report", lambda j: rep)
    monkeypatch.setattr(workflow, "summary", lambda j, r: "SUMMARY")
    monkeypatch.setattr(workflow, "approve", lambda *a, **k: None)
    monkeypatch.setattr(judge, "run_judge", lambda *a, **k: Verdict(error="the judge call failed: refused", model="ollama/qwen2.5-7b-8k"))
    answers = iter(["a"])
    out = workflow.review_job(db, a_job(job), settings, fb_fixture, make_llm=lambda: object(), ask=lambda p: next(answers))
    assert out == "approved" and "Judge: no verdict (the judge call failed: refused)" in capsys.readouterr().out
    (row,) = judge.read_log(judge.log_path(settings))
    assert row["human"] == "approve" and row["judge_would_send"] == ""


def test_through_the_router_a_generator_of_the_judges_family_blocks_the_call(ollama_judge, settings, fb_fixture, job):
    r, sent, _ = ollama_judge
    v = judge.run_judge(r, settings, job, make_rep(fb_fixture, used_models=("qwen2.5-7b-8k",)), fb_fixture)
    assert sent == [] and "same family as a model that wrote this job" in v.error


# ------------------------------------------------------------------ the input fits the local model's context
LONG_JD = ("About Acme\n" + "We are a company that does many things for many people and is proud of it. " * 40
           + "\n\nThe role\nYou will build and evaluate machine learning models in Python and SQL for our pricing team.\n"
           + "\nRequirements\n" + "\n".join(f"- Experience with tool number {i} in production" for i in range(60))
           + "\n\nBenefits\n" + "\n".join(f"- Perk number {i}" for i in range(80))
           + "\n\nEqual opportunities\nWe welcome everyone. " * 30)


def test_the_budget_is_the_context_minus_the_answer_and_a_margin(settings):
    assert judge.judge_limits(settings) == (8192, 800)
    assert judge.input_budget(settings) == 8192 - 800 - judge.CTX_MARGIN
    ollama = next(p for p in settings["llm"]["providers"] if p["name"] == "ollama")
    del ollama["context_tokens"]
    assert judge.input_budget(settings) is None                          # no limit known: nothing is cut for the context


def test_the_jd_is_cut_to_its_role_and_requirements_sections(fb_fixture, job):
    inp, info = judge.prepare_input({**job, "description": LONG_JD}, make_rep(fb_fixture), fb_fixture, None)
    assert "The role" in inp["jd"] and "Requirements" in inp["jd"] and "tool number 59" in inp["jd"]
    assert "Perk number" not in inp["jd"] and "Equal opportunities" not in inp["jd"] and "About Acme" not in inp["jd"]
    assert any("dropped" in n and "Benefits" in n for n in info["notes"]) and info["jd_chars"] == len(LONG_JD)
    assert info["jd_sent"] == len(inp["jd"]) < len(LONG_JD)


def test_a_jd_without_headings_or_that_would_shrink_to_nothing_is_kept_whole(fb_fixture, job):
    plain = "Build models in Python for our pricing team and own their evaluation end to end. " * 20
    assert judge.prepare_input({**job, "description": plain}, make_rep(fb_fixture), fb_fixture, None)[0]["jd"] == plain
    tiny = "About Acme\nWe like cake.\n\nBenefits\nCake.\n\nThe role\nBuild."
    assert judge.prepare_input({**job, "description": tiny}, make_rep(fb_fixture), fb_fixture, None)[0]["jd"] == tiny


def test_the_prompt_is_fitted_to_the_budget_by_cutting_the_jd(fb_fixture, job):
    big = "The role\n" + "You will build models and evaluate them carefully in production every day. " * 400
    budget = 3000
    inp, info = judge.prepare_input({**job, "description": big}, make_rep(fb_fixture), fb_fixture, budget)
    assert judge.prompt_tokens(inp) <= budget and info["tokens"] == judge.prompt_tokens(inp) and not info["error"]
    assert inp["jd"].endswith("[JD cut here]") and len(inp["jd"]) >= judge.MIN_JD_CHARS
    assert any(n.startswith("JD cut to") for n in info["notes"])


def test_facts_are_dropped_before_the_jd_goes_below_the_minimum(fb_fixture, job):
    rep = make_rep(fb_fixture)
    inp0, _ = judge.prepare_input(job, rep, fb_fixture, None)
    assert len(inp0["jd"]) < judge.MIN_JD_CHARS and inp0["facts"].count("\n") >= 1      # a short JD: nothing to cut there
    budget = judge.prompt_tokens(inp0) - 60
    inp, info = judge.prepare_input(job, rep, fb_fixture, budget)
    assert inp["facts"].count("\n") < inp0["facts"].count("\n") and any("source fact(s) left out" in n for n in info["notes"])
    assert judge.prompt_tokens(inp) <= budget and inp["jd"] == inp0["jd"] and not info["error"]


def test_a_cv_and_letter_too_big_for_the_context_give_no_verdict_and_send_nothing(settings, fb_fixture, job, spy_llm):
    rep = make_rep(fb_fixture)
    rep["context"]["profile"] = "profile word " * 3000
    llm = spy(spy_llm)
    v = judge.run_judge(llm, settings, job, rep, fb_fixture)
    assert v.would_send is None and "too long to judge on this model" in v.error and llm.sent == []


def test_a_real_sized_ad_is_sent_within_the_ollama_context(ollama_judge, settings, fb_fixture):
    from conftest import fixture
    r, sent, _ = ollama_judge
    monzo = fixture("tailoring", "jd_monzo.txt").read_text(encoding="utf-8")
    job = {"id": "x", "title": "Data Scientist", "company": "Monzo", "description": monzo * 6}          # ~37 KB
    judge.run_judge(r, settings, job, make_rep(fb_fixture), fb_fixture)
    (body,) = sent
    est = judge.estimate_tokens(*(m["content"] for m in body["messages"]))
    assert est + body["max_tokens"] <= 8192 - judge.CTX_MARGIN
    assert r.db.conn.execute("SELECT detail FROM events WHERE stage='judge'").fetchone()["detail"]      # the tokens are logged


def test_the_tokens_sent_are_in_the_log_and_an_old_log_keeps_its_header(settings, job):
    rep = {"letter": {"status": "ok"}}
    old = [c for c in judge.LOG_COLUMNS if c != "judge_input_tokens"]
    path = judge.log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(old) + "\n", encoding="utf-8")
    judge.log_decision(settings, job, rep, Verdict(True, 4, [], "m", tokens=1234), "approve")        # a log from before the column
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(old) and len(judge.read_log(path)) == 1
    path.unlink()
    judge.log_decision(settings, job, rep, Verdict(True, 4, [], "m", tokens=1234), "approve")
    assert judge.read_log(path)[0]["judge_input_tokens"] == "1234"


def test_the_review_screen_shows_the_judge_input_line(settings, db, fb_fixture, job, monkeypatch, capsys):
    from careerops.review import workflow
    rep = make_rep(fb_fixture)
    monkeypatch.setattr(workflow, "load_report", lambda j: rep)
    monkeypatch.setattr(workflow, "summary", lambda j, r: "SUMMARY")
    monkeypatch.setattr(judge, "run_judge", lambda *a, **k: Verdict(True, 4, [], "ollama/qwen2.5-7b-8k", tokens=3210, note="JD cut to 4,000 characters"))
    workflow.review_job(db, a_job(job), settings, fb_fixture, make_llm=lambda: object(), ask=lambda p: "s")
    assert "Judge input: 3,210 prompt tokens (JD cut to 4,000 characters)" in capsys.readouterr().out


# ------------------------------------------------------------------ the log is append-only
def test_log_decision_appends_rows_and_writes_the_header_once(settings, job):
    rep = {"letter": {"status": "ok"}}
    v = Verdict(True, 4, ["nit", "other"], "groq/llama", key="abc")
    judge.log_decision(settings, job, rep, v, "approve")
    judge.log_decision(settings, job, rep, Verdict(error="boom", model="groq/llama"), "reject", "not a fit, really")
    path = judge.log_path(settings)
    assert path == settings.path("judge") / "log.csv"
    text = path.read_text(encoding="utf-8")
    assert text.count("ts,job_id") == 1
    first, second = judge.read_log(path)
    assert list(first) == judge.LOG_COLUMNS
    assert (first["job_id"], first["human"], first["judge_would_send"], first["judge_score"], first["judge_issues"]) == (
        job["id"], "approve", "true", "4", "nit | other")
    assert (second["human"], second["reason"], second["judge_would_send"], second["judge_error"]) == ("reject", "not a fit, really", "", "boom")
    assert first["mode"] == "shadow" and first["letter_status"] == "ok" and first["verdict_key"] == "abc"


def test_no_row_is_written_when_the_judge_was_never_asked(settings, job):
    judge.log_decision(settings, job, {"letter": {}}, None, "approve")
    assert not judge.log_path(settings).exists()


def test_a_missing_log_reads_as_empty(settings):
    assert judge.read_log(judge.log_path(settings)) == []


# ------------------------------------------------------------------ statistics
def test_stats_agreement_kappa_and_the_confusion_counts():
    s = judge.stats(rows(tp=40, fa=5, fr=5, tn=50))
    assert (s["n"], s["tp"], s["fa"], s["fr"], s["tn"], s["judge_yes"]) == (100, 40, 5, 5, 50, 45)
    assert s["agreement"] == pytest.approx(0.90)
    assert s["kappa"] == pytest.approx((0.90 - 0.505) / (1 - 0.505))                 # 0.798


def test_kappa_is_zero_for_chance_agreement_and_one_for_perfect():
    assert judge.stats(rows(tp=25, fa=25, fr=25, tn=25))["kappa"] == pytest.approx(0.0)
    assert judge.stats(rows(tp=30, tn=20))["kappa"] == pytest.approx(1.0)


def test_kappa_is_undefined_when_one_side_never_varies():
    s = judge.stats(rows(tp=30))                      # the judge said yes and so did you, every time
    assert s["kappa"] is None and s["agreement"] == 1.0 and s["n"] == 30
    assert judge.stats([])["n"] == 0 and judge.stats([])["agreement"] is None


def test_unjudged_and_auto_rows_are_not_in_n():
    s = judge.stats(rows(tp=3, tn=2, unjudged=4, auto=7))
    assert s["n"] == 5 and s["unjudged"] == 4 and s["auto"] == 7


def test_the_report_gives_the_rule_of_three_bound_when_there_are_no_false_approves(tmp_path):
    out = judge.format_report(rows(tp=40, fr=3, tn=27), tmp_path / "log.csv")
    assert "labelled and judged reviews (n): 70" in out
    assert "false approves: 0 of 70" in out and "3/n = 4.3%" in out and "below 7.5% of the judge's 40 yes verdicts" in out
    assert "FALSE APPROVE): 0" in out and "Cohen's kappa:" in out and "agreement: 96%" in out


def test_the_report_gives_a_wilson_interval_when_there_are_false_approves(tmp_path):
    out = judge.format_report(rows(tp=40, fa=5, fr=5, tn=50), tmp_path / "log.csv")
    assert "false approves: 5 of 100 = 5.0% (95% Wilson interval" in out and "Rule of three" not in out
    assert "FALSE APPROVE): 5" in out


def test_the_report_with_nothing_to_compare_says_so(tmp_path):
    assert "nothing to compare yet" in judge.format_report([], tmp_path / "log.csv")


# ------------------------------------------------------------------ the gate
def test_gate_needs_60_labelled_reviews_and_no_false_approve():
    assert judge.GATE_MIN_LABELS == 60
    ok, why = judge.gate_status(rows(tp=30, tn=29))
    assert not ok and "59 human-labelled reviews, gate needs 60" in why
    assert judge.gate_status(rows(tp=30, tn=30)) == (True, "")
    ok, why = judge.gate_status(rows(tp=30, tn=29, fa=1))
    assert not ok and "1 false approve" in why
    ok, why = judge.gate_status(rows(tp=30, tn=30, fa=1))
    assert not ok and "1 false approve" in why                                       # 61 labels do not outweigh one false approve


def test_unjudged_and_auto_rows_do_not_count_towards_the_gate():
    assert not judge.gate_status(rows(tp=30, tn=29, unjudged=50, auto=50))[0]


def _write_log(settings, rs):
    path = judge.log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=judge.LOG_COLUMNS)
        w.writeheader()
        for r in rs:
            w.writerow({**{c: "" for c in judge.LOG_COLUMNS}, **r})


def test_require_gate_raises_in_code_until_the_log_earns_it(settings):
    with pytest.raises(judge.GateRefused, match="0 human-labelled reviews, gate needs 60"):
        judge.require_gate(settings)
    _write_log(settings, rows(tp=30, tn=29))
    with pytest.raises(judge.GateRefused, match="59"):
        judge.require_gate(settings)
    _write_log(settings, rows(tp=30, tn=30))
    judge.require_gate(settings)                                                     # earned
    _write_log(settings, rows(tp=30, tn=30, fa=1))
    with pytest.raises(judge.GateRefused, match="false approve"):
        judge.require_gate(settings)


def test_the_gate_threshold_cannot_be_lowered_from_the_settings(settings):
    settings.raw["review"]["gate_min_labels"] = 1
    with pytest.raises(judge.GateRefused):
        judge.require_gate(settings)


# ------------------------------------------------------------------ the commands
def _cli(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)


def test_judge_report_command_prints_the_numbers(settings, monkeypatch, capsys):
    _cli(monkeypatch, settings)
    _write_log(settings, rows(tp=40, fr=3, tn=27))
    cli.main(["judge-report"])
    out = capsys.readouterr().out
    assert "n): 70" in out and "3/n = 4.3%" in out and "gate: allowed" in out


def test_judge_report_with_no_log_says_so(settings, monkeypatch, capsys):
    _cli(monkeypatch, settings)
    cli.main(["judge-report"])
    assert "nothing to compare yet" in capsys.readouterr().out


def test_gate_mode_without_the_labels_is_an_error_exit_not_a_traceback(settings, monkeypatch, capsys):
    _cli(monkeypatch, settings)
    settings.raw["review"]["judge"] = "gate"
    monkeypatch.setattr(cli.review, "run", lambda *a, **k: judge.require_gate(settings))
    with pytest.raises(SystemExit) as e:
        cli.main(["review"])
    assert e.value.code == 1 and "review.judge is 'gate'" in capsys.readouterr().err


def test_mock_llm_answers_the_judge_task_with_a_fixed_yes():
    assert MockLLM().json("judge", "s", "u") == {"would_send": True, "score": 4, "issues": []}


def test_doctor_reports_the_judge(settings):
    from careerops.setup import judge_checks
    ((name, ok, hint),) = judge_checks(settings)
    assert ok and "review.judge: shadow" in name and "qwen2.5-7b-8k" in name and "family qwen" in name
    settings.raw["llm"]["models"]["judge"]["model"] = "openai/gpt-oss-120b"
    ((name, ok, hint),) = judge_checks(settings)
    assert not ok and "same family" in hint
    settings.raw["review"]["judge"] = "off"
    assert judge_checks(settings) == [("review.judge: off (no judge)", True, "")]


def test_doctor_does_not_ping_the_models_of_features_that_are_off(settings, monkeypatch):
    from careerops.setup import model_checks
    settings.raw["review"]["judge"] = "off"
    settings.raw["letter"]["mode"] = "off"
    names = [n for n, _, _ in model_checks(settings, ping=False)]
    assert "judge: no model asked (review.judge is off)" in names and not any("for judge" in n for n in names)
