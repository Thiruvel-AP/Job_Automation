"""The judge inside `review_job`: the verdict on the screen, the log row beside your decision, and the gate. Real tailored job
(xelatex), MockLLM / SpyLLM for the judge, every path under tmp_path."""
import csv
import json
from pathlib import Path

import pytest

from careerops.llm.router import LLMError
from careerops.review import judge
from careerops.review import workflow as review

pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)

YES = {"would_send": True, "score": 5, "issues": []}
NO = {"would_send": False, "score": 2, "issues": ["the letter pastes a bullet", "wrong team"]}


def scripted(*answers):
    it = iter(answers)
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return next(it)
    ask.prompts = prompts
    return ask


def never_asked(prompt=""):
    raise AssertionError(f"the human was asked: {prompt}")


def judge_llm(spy_llm, answer):
    if isinstance(answer, Exception):
        def boom(ctx):
            raise answer
        return spy_llm({"judge": boom})
    return spy_llm({"judge": lambda ctx: answer})


def log_rows(settings):
    return judge.read_log(judge.log_path(settings))


def earn_the_gate(settings, tp=30, tn=30, fa=0):
    path = judge.log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    rs = ([("true", "approve")] * tp + [("false", "reject")] * tn + [("true", "reject")] * fa)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=judge.LOG_COLUMNS)
        w.writeheader()
        for says, human in rs:
            w.writerow({**{c: "" for c in judge.LOG_COLUMNS}, "judge_would_send": says, "human": human})


def run(tailored, settings, db, job, llm, *answers, **kw):
    _, fb, _ = tailored
    ask = kw.pop("ask", None) or scripted(*answers)
    return review.review_job(db, db.job(job["id"]), settings, fb, make_llm=kw.pop("make_llm", lambda: llm), ask=ask, **kw), ask


# ------------------------------------------------------------------ shadow
def test_shadow_shows_the_verdict_and_logs_it_beside_your_approval(tailored, settings, db, job, spy_llm, capsys):
    llm = judge_llm(spy_llm, NO)
    out, ask = run(tailored, settings, db, job, llm, "a")
    shown = capsys.readouterr().out
    assert out == "approved" and db.job(job["id"])["status"] == "approved"           # shadow never decides
    assert "Judge (mock): WOULD NOT SEND 2/5 [shadow]" in shown and "- the letter pastes a bullet" in shown
    (r,) = log_rows(settings)
    assert (r["job_id"], r["human"], r["judge_would_send"], r["judge_score"], r["mode"]) == (job["id"], "approve", "false", "2", "shadow")
    assert "pastes a bullet | wrong team" in r["judge_issues"] and r["letter_status"] == "ok"
    assert [s["task"] for s in llm.sent] == ["judge"] and ask.prompts[0].endswith("[q]uit > ")


def test_shadow_logs_a_rejection_with_its_reason(tailored, settings, db, job, spy_llm):
    run(tailored, settings, db, job, judge_llm(spy_llm, YES), "r", "not a fit")
    (r,) = log_rows(settings)
    assert (r["human"], r["reason"], r["judge_would_send"]) == ("reject", "not a fit", "true")
    assert db.job(job["id"])["status"] == "rejected"


def test_a_plain_skip_and_quit_log_nothing_but_live_s_skip_is_a_no(tailored, settings, db, job, spy_llm):
    for choice in ("s", "q"):
        run(tailored, settings, db, job, judge_llm(spy_llm, YES), choice)
    assert log_rows(settings) == []
    out, _ = run(tailored, settings, db, job, judge_llm(spy_llm, YES), "s", skip_means_no=True, allow="aelosq")
    (r,) = log_rows(settings)
    assert out == "skipped" and (r["human"], r["reason"]) == ("reject", "skipped in live review")


def test_the_judge_never_changes_the_job_or_its_files(tailored, settings, db, job, spy_llm):
    rep, _, _ = tailored
    files = lambda: {str(p.relative_to(rep["out_dir"])): p.stat().st_mtime_ns for p in Path(rep["out_dir"]).rglob("*") if p.is_file()}  # noqa: E731
    report_before, files_before = (Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"), files()
    row_before = dict(db.job(job["id"]))
    run(tailored, settings, db, job, judge_llm(spy_llm, NO), "s")
    assert (Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8") == report_before and files() == files_before
    assert dict(db.job(job["id"])) == row_before


def test_a_judge_that_cannot_answer_is_shown_as_no_verdict_and_the_review_goes_on(tailored, settings, db, job, spy_llm, capsys):
    out, _ = run(tailored, settings, db, job, judge_llm(spy_llm, LLMError("all providers failed")), "a")
    assert out == "approved" and "Judge: no verdict (the judge call failed" in capsys.readouterr().out
    (r,) = log_rows(settings)
    assert r["judge_would_send"] == "" and "all providers failed" in r["judge_error"] and r["human"] == "approve"
    assert judge.stats(log_rows(settings))["n"] == 0 and judge.stats(log_rows(settings))["unjudged"] == 1


def test_without_an_llm_the_judge_is_not_run_and_nothing_is_logged(tailored, settings, db, job, capsys):
    out, _ = run(tailored, settings, db, job, None, "a", make_llm=None)
    assert out == "approved" and "Judge: not run (no LLM for this run)" in capsys.readouterr().out and log_rows(settings) == []


def test_off_makes_no_judge_call_and_logs_nothing(tailored, settings, db, job, spy_llm, capsys):
    settings.raw["review"]["judge"] = "off"
    llm = judge_llm(spy_llm, YES)
    out, _ = run(tailored, settings, db, job, llm, "a")
    assert out == "approved" and llm.sent == [] and "Judge" not in capsys.readouterr().out and log_rows(settings) == []


def test_a_misconfigured_judge_is_shown_as_no_verdict(tailored, settings, db, job, spy_llm, capsys):
    settings.raw["llm"]["models"]["judge"] = {"provider": "groq", "model": "openai/gpt-oss-120b"}
    llm = judge_llm(spy_llm, YES)
    run(tailored, settings, db, job, llm, "s")
    assert llm.sent == [] and "same family as the generator" in capsys.readouterr().out


def test_regenerating_the_letter_shows_the_verdict_again_for_the_new_version(tailored, settings, db, job, spy_llm, capsys):
    llm = judge_llm(spy_llm, YES)
    out, ask = run(tailored, settings, db, job, llm, "l", "a")
    shown = capsys.readouterr().out
    assert out == "approved" and shown.count("Judge (mock): WOULD SEND 5/5") == 2          # before and after the new letter
    assert [s["task"] for s in llm.sent].count("judge") == 1                              # same text again: the cached verdict
    assert len(log_rows(settings)) == 1                                                   # one decision, one row


# ------------------------------------------------------------------ the letter is off
def test_with_the_letter_off_approving_needs_no_confirmation_and_l_says_why(tailored, settings, db, job, spy_llm, capsys):
    _, fb, _ = tailored
    rep_path = Path(db.job(job["id"])["out_dir"]) / "report.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    rep["letter"] = {"paragraphs": [], "problems": [], "score": None, "pages": 0, "status": "off", "reason": "letter.mode: off",
                     "notes": [], "evidence": rep["letter"]["evidence"]}
    rep["letter_pdf"] = None
    rep_path.write_text(json.dumps(rep), encoding="utf-8")
    llm = judge_llm(spy_llm, YES)
    out, ask = run(tailored, settings, db, job, llm, "l", "a")
    shown = capsys.readouterr().out
    assert out == "approved" and "Letter: off (letter.mode: off" in shown and "set letter.mode: draft" in shown
    assert not any("Type '" in p for p in ask.prompts)
    assert "(no cover letter)" in llm.sent[0]["user"]


# ------------------------------------------------------------------ gate
def test_gate_is_refused_in_code_until_the_log_earns_it(tailored, settings, db, job, spy_llm):
    settings.raw["review"]["judge"] = "gate"
    llm = judge_llm(spy_llm, YES)
    with pytest.raises(judge.GateRefused, match="0 human-labelled reviews, gate needs 60"):
        run(tailored, settings, db, job, llm, ask=never_asked)
    earn_the_gate(settings, tp=29, tn=30)
    with pytest.raises(judge.GateRefused, match="59"):
        run(tailored, settings, db, job, llm, ask=never_asked)
    earn_the_gate(settings, tp=30, tn=30, fa=1)
    with pytest.raises(judge.GateRefused, match="1 false approve"):
        run(tailored, settings, db, job, llm, ask=never_asked)
    assert llm.sent == [] and db.job(job["id"])["status"] == "needs_review"          # refused before anything was judged


def test_gate_approves_a_judge_yes_without_the_prompt_and_logs_it_as_auto(tailored, settings, db, job, spy_llm, capsys):
    settings.raw["review"]["judge"] = "gate"
    earn_the_gate(settings)
    out, _ = run(tailored, settings, db, job, judge_llm(spy_llm, YES), ask=never_asked)
    assert out == "approved" and db.job(job["id"])["status"] == "approved"
    assert "Judge gate: approved without the prompt" in capsys.readouterr().out
    last = log_rows(settings)[-1]
    assert (last["human"], last["judge_would_send"], last["mode"], last["job_id"]) == ("auto", "true", "gate", job["id"])
    assert judge.stats(log_rows(settings))["n"] == 60 and judge.stats(log_rows(settings))["auto"] == 1      # not a label
    events = [r["detail"] for r in db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='review'", (job["id"],))]
    assert any("auto_approved_by_judge" in e for e in events)


def test_gate_sends_a_judge_no_to_you(tailored, settings, db, job, spy_llm):
    settings.raw["review"]["judge"] = "gate"
    earn_the_gate(settings)
    out, ask = run(tailored, settings, db, job, judge_llm(spy_llm, NO), "s")
    assert out == "skipped" and len(ask.prompts) == 1 and db.job(job["id"])["status"] == "needs_review"


def test_gate_sends_a_job_with_no_verdict_to_you(tailored, settings, db, job, spy_llm):
    settings.raw["review"]["judge"] = "gate"
    earn_the_gate(settings)
    out, ask = run(tailored, settings, db, job, judge_llm(spy_llm, LLMError("down")), "s")
    assert out == "skipped" and len(ask.prompts) == 1
    out, ask = run(tailored, settings, db, job, None, "s", make_llm=None)
    assert out == "skipped" and len(ask.prompts) == 1


def test_gate_sends_a_flagged_job_to_you_even_on_a_judge_yes(tailored, settings, db, job, spy_llm):
    settings.raw["review"]["judge"] = "gate"
    earn_the_gate(settings)
    db.update_job(job["id"], flags=["Clearance mentioned — check residency/eligibility"])
    out, ask = run(tailored, settings, db, job, judge_llm(spy_llm, YES), "s")
    assert out == "skipped" and len(ask.prompts) == 1


@pytest.mark.parametrize("status", ["fallback", "none"])
def test_gate_never_skips_the_deliberate_confirmation_of_a_fallback_or_missing_letter(tailored, settings, db, job, spy_llm, status):
    settings.raw["review"]["judge"] = "gate"
    earn_the_gate(settings)
    rep_path = Path(db.job(job["id"])["out_dir"]) / "report.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    rep["letter"]["status"] = status
    rep_path.write_text(json.dumps(rep), encoding="utf-8")
    out, ask = run(tailored, settings, db, job, judge_llm(spy_llm, YES), "s")
    assert out == "skipped" and len(ask.prompts) == 1 and db.job(job["id"])["status"] == "needs_review"


def test_the_judge_module_does_not_reach_the_apply_step():
    """Gate only moves a job to 'approved'. Opening a form, the visa answers and submitting live in apply / live."""
    import inspect
    import re
    import careerops.review.judge as j
    imports = re.findall(r"^\s*(?:from|import) .*$", inspect.getsource(j), re.M)
    assert not [i for i in imports if "apply" in i or "playwright" in i]
