"""Review's `l` and `e` on a company letter (the prompts and the plumbing; the guards and the PDF are tested in tests/tailoring/test_letter_feedback.py and
test_letter_edit.py). The report, the summary and the letter work are faked: no PDF, no model, no editor."""
import json

import pytest

from careerops.review import letter_edit, workflow

FEEDBACK = "Too formal; lead P3 with the churn model."


class Rig:
    pass


@pytest.fixture
def rig(settings, db, job, tmp_path, monkeypatch):
    settings.raw["review"]["judge"] = "off"
    out = tmp_path / "out"
    out.mkdir()
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="needs_review", out_dir=str(out))
    r = Rig()
    r.rep = {"out_dir": str(out), "letter": {"mode": "company", "status": "ok", "paragraphs": ["p1", "p2", "p3", "p4", "p5"]}}
    r.db, r.settings, r.job_id, r.regenerated, r.edited = db, settings, job["id"], [], []
    monkeypatch.setattr(workflow, "load_report", lambda j: r.rep)
    monkeypatch.setattr(workflow, "summary", lambda *a, **k: "SUMMARY")

    def fake_regenerate(db_, job_, rep, fb, llm, settings_, feedback=""):
        r.regenerated.append(feedback)
        return rep
    monkeypatch.setattr(workflow, "regenerate_letter", fake_regenerate)

    def fake_edit(db_, job_, rep, fb, settings_, ask, edit=None, out=print):
        r.edited.append(job_["id"])
        return rep
    monkeypatch.setattr(letter_edit, "edit_letter", fake_edit)

    def review(*answers, allow="elq"):
        it = iter(answers)
        prompts: list[str] = []

        def ask(prompt=""):
            prompts.append(prompt)
            return next(it)
        result = workflow.review_job(db, db.job(job["id"]), settings, {}, make_llm=lambda: object(), ask=ask, allow=allow)
        return result, prompts
    r.review = review
    return r


def feedback_rows(settings) -> list[dict]:
    p = settings.path("letter_feedback")
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ------------------------------------------------------------------ l
def test_l_asks_what_should_change_and_hands_the_answer_to_the_next_draft_word_for_word(rig):
    result, prompts = rig.review("l", FEEDBACK, "q")
    assert result == "quit" and rig.regenerated == [FEEDBACK]
    assert "What should change? (enter = just retry) " in prompts


def test_the_feedback_is_saved_with_the_job_id_the_text_and_the_date(rig):
    rig.review("l", FEEDBACK, "q")
    [row] = feedback_rows(rig.settings)
    assert row["job_id"] == rig.job_id and row["text"] == FEEDBACK and len(row["date"]) == 10


def test_enter_just_retries_and_saves_nothing(rig):
    rig.review("l", "", "q")
    assert rig.regenerated == [""] and feedback_rows(rig.settings) == []


def test_l_on_another_letter_mode_asks_nothing(rig):
    rig.rep["letter"]["mode"] = "draft"
    result, prompts = rig.review("l", "q")
    assert rig.regenerated == [""] and not any("What should change" in p for p in prompts)


def test_each_l_asks_again(rig):
    rig.review("l", "shorter", "l", "", "l", "warmer", "q")
    assert rig.regenerated == ["shorter", "", "warmer"] and [r["text"] for r in feedback_rows(rig.settings)] == ["shorter", "warmer"]


def test_after_l_the_days_cover_letter_tokens_are_printed(rig, capsys):
    rig.db.llm_task_add("cover_letter", "groq/openai/gpt-oss-120b", 4_321)
    rig.review("l", "", "q")
    out = capsys.readouterr().out
    assert "Tokens used today for cover_letter:" in out and "groq/openai/gpt-oss-120b: 4,321 tokens in 1 call(s)" in out


# ------------------------------------------------------------------ e
def test_e_on_a_company_letter_asks_cv_or_letter_and_the_letter_opens_the_letter_editor(rig):
    result, prompts = rig.review("e", "l", "q")
    assert rig.edited == [rig.job_id] and "Edit the [c]v or the [l]etter? (enter = cv) " in prompts


def test_the_judge_is_asked_again_after_a_letter_edit(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(workflow, "_ask_judge", lambda *a, **k: seen.append(1))
    rig.review("e", "letter", "q")
    assert len(seen) >= 1


def test_e_on_a_company_letter_with_enter_goes_on_to_the_cv_edit(rig, monkeypatch):
    called = []
    monkeypatch.setattr(workflow.subprocess, "call", lambda cmd: called.append(cmd))
    monkeypatch.setattr(workflow, "edit_file_for", lambda rep: {"profile": "", "bullets": {}})
    monkeypatch.setattr(workflow, "apply_edits", lambda *a, **k: [])
    rig.rep.update(final={"score": 0.5, "pages": 2})
    rig.review("e", "", "q")
    assert called and rig.edited == []


def test_e_on_another_letter_mode_goes_straight_to_the_cv_edit(rig, monkeypatch):
    rig.rep["letter"]["mode"] = "draft"
    called = []
    monkeypatch.setattr(workflow.subprocess, "call", lambda cmd: called.append(cmd))
    monkeypatch.setattr(workflow, "edit_file_for", lambda rep: {"profile": "", "bullets": {}})
    monkeypatch.setattr(workflow, "apply_edits", lambda *a, **k: [])
    rig.rep.update(final={"score": 0.5, "pages": 2})
    result, prompts = rig.review("e", "q")
    assert called and rig.edited == [] and not any("Edit the [c]v" in p for p in prompts)
