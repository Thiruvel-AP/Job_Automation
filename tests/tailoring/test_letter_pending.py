"""A letter that no allowed provider can write now: the CV is still built, the job waits as `letter_pending`, nothing
else writes the letter, and a later `careerops tailor` finishes it. No network, no model."""
from pathlib import Path

import pytest

from careerops import cli
from careerops.config import load_yaml
from careerops.llm import MockLLM, TaskUnavailable
from careerops.tailoring.tailor import letter_summary, regenerate_letter, tailor_job

WHY = "cover_letter can only run on groq, and none can take it now: groq: unavailable (no key, daily request or token quota used)"


def _no_letter(ctx):
    raise TaskUnavailable(WHY)


def test_the_cv_is_built_and_the_letter_is_pending_not_a_fallback(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"cover_letter": _no_letter})
    rep = tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert Path(rep["cv_pdf"]).is_file() and rep["final"]["pages"] <= settings["scoring"]["max_pages"]
    assert rep["letter"]["status"] == "pending" and rep["letter"]["reason"] == WHY
    assert rep["letter_pdf"] is None and rep["letter"]["pages"] == 0 and rep["letter"]["score"] is None
    assert not list(Path(rep["out_dir"]).glob("*Cover Letter*"))                  # no letter file of any kind
    assert len([s for s in llm.sent if s["task"] == "cover_letter"]) == 1         # and no retry on another provider
    assert letter_summary(rep["letter"]) == f"pending ({WHY})"


def test_a_pending_letter_is_finished_later_under_its_usual_name(settings, fb_fixture, job, spy_llm):
    rep = tailor_job(job, fb_fixture, spy_llm({"cover_letter": _no_letter}), settings, settings.path("outputs"))
    done = regenerate_letter(job, fb_fixture, MockLLM(), settings, rep)
    assert done["letter"]["status"] == "ok" and done["letter"]["pages"] == 1
    assert Path(done["letter_pdf"]).is_file() and done["letter_pdf"].endswith("Cover Letter.pdf")


# ---------------------------------------------------------------- the command
@pytest.fixture
def queue(settings, db, job, monkeypatch):
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    return job["id"]


def test_tailor_parks_the_job_as_letter_pending_and_a_later_run_finishes_it(queue, db, settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM({"cover_letter": _no_letter}))
    cli.main(["tailor", "--limit", "1"])
    out = capsys.readouterr().out
    assert db.job(queue)["status"] == "letter_pending" and "letter_pending" in out and "pending (" in out
    assert db.jobs("needs_review") == []                                           # not in the review queue

    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM())
    cli.main(["tailor"])
    out = capsys.readouterr().out
    assert "1 job(s) waiting for a letter" in out and "-> needs_review" in out
    assert db.job(queue)["status"] == "needs_review"
    rep = load_yaml(Path(db.job(queue)["out_dir"]) / "report.json")                # json is yaml
    assert rep["letter"]["status"] == "ok" and Path(rep["letter_pdf"]).is_file()


def test_a_still_pending_letter_stays_pending_and_untouched(queue, db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM({"cover_letter": _no_letter}))
    cli.main(["tailor", "--limit", "1"])
    cli.main(["tailor"])
    out = capsys.readouterr().out
    assert "still pending" in out and db.job(queue)["status"] == "letter_pending"
