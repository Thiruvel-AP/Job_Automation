"""Letter failure policy `letter.on_failure`: none (default) | template. After 3 rejected drafts "none" produces no letter PDF,
review shows NO LETTER and approving needs the words 'no letter'; "template" writes the deterministic template letter, flagged as
a fallback, as before. Every check on the letter itself is the same under both. No network, no model."""
from pathlib import Path

import pytest
from conftest import restate

from careerops import cli
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.review import workflow as review
from careerops.tailoring import tailor
from careerops.tailoring.tailor import FALLBACK_MARK, letter_policy, letter_summary, tailor_job

LYING = ["Reviewly turns feedback into themes.", "I cut latency by 9876 percent across the platform."]   # a number nothing supports

pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex


def _drafts(*bodies):
    queue = list(bodies)
    return lambda ctx: {"paragraphs": queue.pop(0) if len(queue) > 1 else queue[0]}


def _boom(ctx):
    raise RuntimeError("provider quota exhausted")


def _letters_in(rep) -> list[Path]:
    return sorted(Path(rep["out_dir"]).glob("*Cover Letter*"))


# ---------------------------------------------------------------- none: the default
def test_the_default_is_none_and_the_shipped_settings_say_so(settings):
    assert settings["letter"]["on_failure"] == "none" and letter_policy(settings) == "none"
    del settings.raw["letter"]["on_failure"]
    assert letter_policy(settings) == "none"                            # a settings file without the key behaves the same


def test_three_rejected_drafts_produce_no_letter_at_all_and_the_cv_is_still_built(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"cover_letter": _drafts(LYING)})
    rep = tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert len([s for s in llm.sent if s["task"] == "cover_letter"]) == 3            # all 3 drafts were tried
    letter = rep["letter"]
    assert letter["status"] == "none" and letter["paragraphs"] == [] and letter["pages"] == 0 and letter["score"] is None
    assert rep["letter_pdf"] is None and _letters_in(rep) == []                       # no PDF, no .tex
    assert Path(rep["cv_pdf"]).is_file() and rep["final"]["pages"] <= settings["scoring"]["max_pages"]
    assert [f["attempt"] for f in letter["retry"]["failures"]] == [1, 2, 3] and letter["retry"]["outcome"] == "no letter"
    assert any("new numbers" in p for p in letter["problems"]) and FALLBACK_MARK not in letter["problems"]
    assert letter["reason"] == "3 draft(s) rejected"
    assert letter_summary(letter).startswith("NO LETTER (3 draft(s) rejected; first: new numbers")


def test_a_letter_model_that_errors_also_gives_no_letter(settings, fb_fixture, job, spy_llm):
    rep = tailor_job(job, fb_fixture, spy_llm({"cover_letter": _boom}), settings, settings.path("outputs"))
    assert rep["letter"]["status"] == "none" and rep["letter_pdf"] is None and _letters_in(rep) == []
    assert rep["letter"]["reason"] == "the letter model failed" and "letter LLM failed" in rep["letter"]["problems"][0]


def test_a_draft_that_passes_is_unaffected_by_the_policy(settings, fb_fixture, job):
    for policy in ("none", "template"):
        settings.raw["letter"]["on_failure"] = policy
        rep = tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
        assert rep["letter"]["status"] == "ok" and Path(rep["letter_pdf"]).is_file() and rep["letter"]["pages"] == 1


def test_a_draft_accepted_on_the_third_try_is_a_letter_under_none(settings, fb_fixture, job, spy_llm):
    def clean(ctx):
        return {"paragraphs": ["Northwind Health AI builds tools I have worked on.", *map(restate, ctx["evidence"])]}
    calls = []

    def third_time_lucky(ctx):
        calls.append(1)
        return clean(ctx) if len(calls) == 3 else {"paragraphs": LYING}
    rep = tailor_job(job, fb_fixture, spy_llm({"cover_letter": third_time_lucky}), settings, settings.path("outputs"))
    assert len(calls) == 3 and rep["letter"]["status"] == "ok" and Path(rep["letter_pdf"]).is_file()
    assert [f["attempt"] for f in rep["letter"]["retry"]["failures"]] == [1, 2]


# ---------------------------------------------------------------- template: the old behaviour
def test_template_writes_the_fallback_letter_flagged_as_before(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["on_failure"] = "template"
    rep = tailor_job(job, fb_fixture, spy_llm({"cover_letter": _drafts(LYING)}), settings, settings.path("outputs"))
    letter = rep["letter"]
    assert letter["status"] == "fallback" and FALLBACK_MARK in letter["problems"] and letter["pages"] == 1
    assert Path(rep["letter_pdf"]).is_file() and letter["paragraphs"] and letter["retry"]["outcome"] == "fallback"
    assert letter_summary(letter).startswith("fallback (")


def test_template_also_covers_a_letter_model_that_errors(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["on_failure"] = "template"
    rep = tailor_job(job, fb_fixture, spy_llm({"cover_letter": _boom}), settings, settings.path("outputs"))
    assert rep["letter"]["status"] == "fallback" and Path(rep["letter_pdf"]).is_file()


@pytest.mark.parametrize("bad", ["fallback", "off", "Template2", 3])
def test_any_other_value_is_refused_with_the_allowed_ones_named(settings, bad):
    settings.raw["letter"]["on_failure"] = bad
    with pytest.raises(ValueError, match="letter.on_failure must be one of none, template"):
        letter_policy(settings)


def test_the_value_is_case_and_space_insensitive(settings):
    settings.raw["letter"]["on_failure"] = "  Template "
    assert letter_policy(settings) == "template"


# ---------------------------------------------------------------- review
def _job_without_letter(settings, db, job, policy="none"):
    settings.raw["letter"]["on_failure"] = policy
    fb = load_yaml(settings.path("fact_bank"))
    rep = tailor_job(job, fb, MockLLM({"cover_letter": _boom}), settings, settings.path("outputs"))
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer",
                  letter_score=rep["letter"]["score"])
    return db.job(job["id"]), rep, fb


def test_the_review_screen_says_no_letter_and_does_not_crash_on_the_missing_score(settings, db, job):
    row, rep, _ = _job_without_letter(settings, db, job)
    text = review.summary(row, rep)
    assert review.letter_status(rep) == "none"
    assert "NO LETTER — write your own or apply without" in text and "Letter: none" in text and "Letter: (none)" in text
    assert "LETTER IS A FALLBACK" not in text and "Letter score" not in text


def test_approving_without_a_letter_needs_the_words_no_letter(settings, db, job):
    row, rep, _ = _job_without_letter(settings, db, job)
    prefs = settings.path("preferences")
    for confirm in (None, "", "yes", "send fallback", "NO LETTER please"):
        with pytest.raises(review.NoLetterError, match="type 'no letter'"):
            review.approve(db, row, rep, prefs, confirm=confirm)
    assert db.job(job["id"])["status"] == "needs_review" and not prefs.exists()
    review.approve(db, row, rep, prefs, confirm=" no letter ")
    assert db.job(job["id"])["status"] == "approved"
    ev = [r["detail"] for r in db.conn.execute("SELECT detail FROM events WHERE job_id=? AND stage='review'", (job["id"],))]
    assert any("approved_without_letter" in d for d in ev)


def test_the_interactive_review_asks_for_no_letter_and_l_regenerates(settings, db, job, monkeypatch):
    row, rep, fb = _job_without_letter(settings, db, job)
    answers = iter(["a", "nope", "l", "a"])                              # approve? refused -> regenerate -> approve
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or next(answers))
    review.run(db, settings, fb, make_llm=lambda: MockLLM())
    assert db.job(job["id"])["status"] == "approved" and db.job(job["id"])["letter_score"] is not None
    assert sum("Type 'no letter' to approve without one" in p for p in prompts) == 1       # asked once; the new letter is plain


def test_the_interactive_review_approves_without_a_letter_when_the_words_are_typed(settings, db, job, monkeypatch):
    row, rep, fb = _job_without_letter(settings, db, job)
    answers = iter(["a", "no letter"])
    monkeypatch.setattr("builtins.input", lambda p="": next(answers))
    assert review.review_job(db, row, settings, fb) == "approved"
    assert db.job(job["id"])["status"] == "approved"


def test_open_pdfs_opens_the_cv_only_when_there_is_no_letter(settings, db, job, monkeypatch):
    row, rep, fb = _job_without_letter(settings, db, job)
    opened = []
    monkeypatch.setattr(review, "open_file", opened.append)
    answers = iter(["o", "q"])
    review.review_job(db, row, settings, fb, ask=lambda p="": next(answers))
    assert opened == [rep["cv_pdf"]]


def test_a_fallback_letter_under_template_still_needs_send_fallback_and_not_no_letter(settings, db, job):
    row, rep, _ = _job_without_letter(settings, db, job, policy="template")
    assert review.letter_status(rep) == "fallback" and "NO LETTER" not in review.summary(row, rep)
    with pytest.raises(review.FallbackLetterError):
        review.approve(db, row, rep, settings.path("preferences"), confirm="no letter")
    review.approve(db, row, rep, settings.path("preferences"), confirm="send fallback")
    assert db.job(job["id"])["status"] == "approved"


# ---------------------------------------------------------------- the command
@pytest.fixture
def queue(settings, db, job, monkeypatch):
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    return job["id"]


def test_tailor_reports_no_letter_and_leaves_the_job_in_review_not_pending(queue, db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM({"cover_letter": _boom}))
    cli.main(["tailor", "--limit", "1"])
    out = capsys.readouterr().out
    row = db.job(queue)
    assert row["status"] == "needs_review" and row["letter_score"] is None        # not letter_pending: there is nothing to wait for
    assert "NO LETTER (the letter model failed" in out and "review asks for 'no letter' to approve" in out
    assert "!! expected" not in out and "letter_pending" not in out
    rep = review.load_report(row)
    assert rep["letter_pdf"] is None and rep["letter"]["status"] == "none"


def test_a_job_with_no_letter_can_be_finished_later_by_regenerating_it(queue, db, job, settings, monkeypatch):
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM({"cover_letter": _boom}))
    cli.main(["tailor", "--limit", "1"])
    fb = load_yaml(settings.path("fact_bank"))
    rep = review.regenerate_letter(db, db.job(queue), review.load_report(db.job(queue)), fb, MockLLM(), settings)
    assert rep["letter"]["status"] == "ok" and Path(rep["letter_pdf"]).is_file()
    assert rep["letter_pdf"].endswith("Cover Letter.pdf")
