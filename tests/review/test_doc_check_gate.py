"""The document check gates (Document rules, A7): a failure blocks `[a]pprove` unless the person types `override doc check` in full, blocks the
judge's auto-approval, shows on the review screen, and always blocks auto-submit. The check itself is faked here (tests/tailoring/test_doccheck.py
has the rules); the job is a plain row and a bare report."""
import json

import pytest

from careerops.review import workflow as review
from careerops.tailoring import doccheck
from careerops.tailoring.doccheck import DocCheck

FAIL = DocCheck(["PROFILE 84/100 words: add profile sentences to fact_bank.yaml → profile.sentences", 'SKILLS repeat "Lambda" in A and B'])


@pytest.fixture
def row(db, job):
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review")
    return db.job(job["id"])


REP = {"keywords": [], "attempts": [{"selection": {}}], "final": {"chosen_attempt": 1, "score": 0.8}, "letter": {"status": "off", "paragraphs": []}}


def events(db, job_id):
    return [json.loads(r["detail"]) if str(r["detail"]).startswith("{") else r["detail"]
            for r in db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='review'", (job_id,))]


def test_a_failed_check_blocks_a_plain_approve(db, row, settings):
    prefs = settings.path("preferences")
    for override in (None, "", "yes", "override", "OVERRIDE DOC CHECK please", "override doc"):
        with pytest.raises(doccheck.DocCheckError, match="override doc check"):
            review.approve(db, row, REP, prefs, doc=FAIL, override=override)
    assert db.job(row["id"])["status"] == "needs_review" and not prefs.exists()


def test_typing_override_doc_check_approves_and_logs_the_failures(db, row, settings):
    review.approve(db, row, REP, settings.path("preferences"), doc=FAIL, override="override doc check")
    assert db.job(row["id"])["status"] == "approved"
    logged = [e for e in events(db, row["id"]) if isinstance(e, dict) and e.get("approved_over_doc_check")]
    assert logged and logged[0]["failures"] == FAIL.failures


def test_a_passing_check_needs_no_override(db, row, settings):
    review.approve(db, row, REP, settings.path("preferences"), doc=DocCheck())
    assert db.job(row["id"])["status"] == "approved"
    assert not [e for e in events(db, row["id"]) if isinstance(e, dict) and e.get("approved_over_doc_check")]


def test_the_override_word_is_the_one_in_the_rules():
    assert review.CONFIRM_DOC_CHECK == doccheck.OVERRIDE == "override doc check"


def test_a_check_that_cannot_run_is_a_failure(settings, monkeypatch):
    def boom(*a, **k):
        raise OSError("the PDF is locked")
    monkeypatch.setattr(doccheck, "for_report", boom)
    out = review.doc_check({}, settings, {}, {})
    assert not out.ok and "the check could not run (OSError: the PDF is locked)" in out.failures[0]


def test_the_review_screen_shows_the_check(settings, monkeypatch):
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: FAIL)
    lines = review.doc_check_lines({}, settings, {}, {})
    assert lines[0] == "DOC CHECK: FAILED (2)" and lines[1].startswith("  - PROFILE 84/100")
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: DocCheck())
    assert review.doc_check_lines({}, settings, {}, {}) == ["DOC CHECK: pass"]
    assert review.doc_check_lines({}, None, None, {}) == []                             # a bare summary has nothing to check against


# ------------------------------------------------------------------ in the review prompt (a real tailored job)
def scripted(*answers):
    it = iter(answers)
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return next(it)
    ask.prompts = prompts
    return ask


@pytest.mark.slow
def test_the_prompt_asks_for_the_override_after_a_and_a_wrong_word_leaves_the_job_unapproved(tailored, settings, db, job, monkeypatch, capsys):
    _, fb, _ = tailored
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: FAIL)
    ask = scripted("a", "yes please", "s")
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=ask) == "skipped"
    out = capsys.readouterr().out
    assert "DOC CHECK: FAILED (2)" in out and "NOT approved: the document check failed (2)" in out
    assert any("override doc check" in p for p in ask.prompts) and db.job(job["id"])["status"] == "needs_review"


@pytest.mark.slow
def test_the_prompt_approves_after_the_override_is_typed_in_full(tailored, settings, db, job, monkeypatch):
    _, fb, _ = tailored
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: FAIL)
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=scripted("a", "override doc check")) == "approved"
    assert db.job(job["id"])["status"] == "approved"


@pytest.mark.slow
def test_a_passing_check_approves_with_one_keystroke(tailored, settings, db, job, monkeypatch):
    _, fb, _ = tailored
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: DocCheck())
    ask = scripted("a")
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=ask) == "approved" and len(ask.prompts) == 1


@pytest.mark.slow
def test_the_judge_gate_does_not_auto_approve_over_a_failed_check(tailored, settings, db, job, monkeypatch, spy_llm):
    from test_judge_review import YES, earn_the_gate, judge_llm, never_asked  # noqa: F401

    _, fb, _ = tailored
    earn_the_gate(settings)
    settings.raw["review"]["judge"] = "gate"
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: FAIL)
    ask = scripted("s")
    out = review.review_job(db, db.job(job["id"]), settings, fb, make_llm=lambda: judge_llm(spy_llm, YES), ask=ask)
    assert out == "skipped" and ask.prompts and db.job(job["id"])["status"] == "needs_review"
