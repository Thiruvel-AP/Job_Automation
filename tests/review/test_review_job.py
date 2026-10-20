"""review_job: the single-job review prompt `careerops review` loops over and `careerops live` calls for one job id."""
import pytest

from careerops.review import workflow as review


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


def scripted(*answers):
    it = iter(answers)
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return next(it)
    ask.prompts = prompts
    return ask


def test_approve_returns_approved_and_sets_the_status(tailored, settings, db, job):
    _, fb, _ = tailored
    ask = scripted("a")
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=ask) == "approved"
    assert db.job(job["id"])["status"] == "approved"
    assert ask.prompts == ["[a]pprove  [e]dit  [r]eject  [l]etter regenerate  [n]ote  [o]pen PDFs  [w] open apply page  [s]kip  [q]uit > "]


def test_skip_and_quit_change_nothing_in_the_database(tailored, settings, db, job):
    _, fb, _ = tailored
    for choice, want in (("s", "skipped"), ("q", "quit")):
        assert review.review_job(db, db.job(job["id"]), settings, fb, ask=scripted(choice)) == want
        assert db.job(job["id"])["status"] == "needs_review"


def test_a_choice_that_is_not_offered_is_asked_again(tailored, settings, db, job):
    _, fb, _ = tailored
    ask = scripted("r", "x", "", "s")                    # reject is not offered below; nor is x
    out = review.review_job(db, db.job(job["id"]), settings, fb, ask=ask, allow="aelosq")
    assert out == "skipped" and db.job(job["id"])["status"] == "needs_review" and len(ask.prompts) == 4
    assert "[r]eject" not in ask.prompts[0] and "[a]pprove  [e]dit  [l]etter regenerate  [o]pen PDFs  [s]kip  [q]uit" in ask.prompts[0]


def test_reject_still_works_for_the_review_command(tailored, settings, db, job):
    _, fb, _ = tailored
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=scripted("r", "not a fit")) == "rejected"
    assert db.job(job["id"])["status"] == "rejected" and db.job(job["id"])["reason"] == "not a fit"


def test_review_run_still_loops_and_stops_on_quit(tailored, settings, db, job, monkeypatch, capsys):
    _, fb, _ = tailored
    answers = iter(["s", "q"])
    monkeypatch.setattr("builtins.input", lambda p="": next(answers))
    other = dict(job, id="manual:other")
    db.upsert_job(other)
    db.update_job("manual:other", status="needs_review", out_dir=db.job(job["id"])["out_dir"], tier=1, family="ai_engineer")
    review.run(db, settings, fb)
    assert "2 job(s) waiting for review." in capsys.readouterr().out
    assert {db.job(job["id"])["status"], db.job("manual:other")["status"]} == {"needs_review"}
