"""The letter gets up to three drafts. Each retry is told every word the claim check rejected so far ("do not use 'X' unless
it appears in the evidence bullets"), so a model cannot swap one unsupported word for another (the Monzo run: 'integrated',
then 'specialised'); every failed draft's reasons are kept for the review screen. No check is loosened.

A draft whose ONLY violations are unsupported qualifier words (adjectives and adverbs: specialised, integrated, ...) is not
retried: the words are deleted, the FULL check re-run, and the edit accepted only if it passes (tailoring/qualifiers.py).
Drafts that also break another rule (a name, a capability, a number ...) are rejected exactly as before; those tests use
BAD_1 / BAD_2, a qualifier together with an unsupported tool name."""
import pytest
from test_guard_round import EVIDENCE, HOOKS, JOBS, REST

from careerops.review import workflow as review
from careerops.tailoring import tailor

GOOD = ("Chattermill's mission to understand customer feedback mirrors my work building NLP and agentic RAG workflows "
        "with LangChain and LangGraph.")
ONLY_1 = GOOD.replace("my work building", "my integrated work building")       # the qualifier is the only violation
ONLY_2 = GOOD.replace("my work building", "my specialised work building")
BAD_1 = GOOD.replace("my work building", "my integrated Spark work building")    # a qualifier AND a tool the evidence lacks
BAD_2 = GOOD.replace("my work building", "my specialised Spark work building")
SECOND = REST["Chattermill"][1]                       # a paragraph written from the other evidence bullet


def _run(settings, spy_llm, drafts):
    it = iter(drafts)
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": next(it)}})
    job = {"company": "Chattermill", "title": JOBS["Chattermill"],
           "description": HOOKS["Chattermill"] + " " + EVIDENCE["Chattermill"][0]["text"]}
    notes, retry = [], {}
    paras, problems = tailor._letter(job, {"deny_terms": []}, [], HOOKS["Chattermill"], EVIDENCE["Chattermill"], llm, settings,
                                     [], notes, "", retry)
    return paras, problems, notes, retry, llm


def test_each_retry_is_told_every_rejected_word_so_far_and_the_third_draft_can_pass(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[BAD_1, SECOND], [BAD_2, SECOND], [GOOD, SECOND]])
    assert problems == [] and paras[0] == GOOD and len(llm.sent) == 3
    second, third = llm.sent[1]["user"], llm.sent[2]["user"]
    assert "Do not use 'integrated' unless it appears in the evidence bullets." in second and "'specialised'" not in second
    assert "Do not use 'integrated' unless it appears in the evidence bullets." in third
    assert "Do not use 'specialised' unless it appears in the evidence bullets." in third
    assert "YOUR LAST DRAFT BROKE THESE RULES" in third and "is not in the two evidence bullets" in third
    assert retry["count"] == 2 and retry["outcome"] == "ok after retry"
    assert retry["rejected_words"] == ["Spark", "integrated", "specialised"] and retry["removed"] == []
    assert [f["attempt"] for f in retry["failures"]] == [1, 2]
    assert any("'integrated'" in p for p in retry["failures"][0]["problems"])
    assert any("'specialised'" in p for p in retry["failures"][1]["problems"])
    assert any("retry 2 of 2, naming: Spark, integrated, specialised" in n for n in notes)


def test_three_failed_drafts_fall_back_and_all_three_reasons_are_kept(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[BAD_1, SECOND], [BAD_2, SECOND], [BAD_1, SECOND]])
    assert len(llm.sent) == 3 and tailor.FALLBACK_MARK in problems and retry["outcome"] == "fallback"
    assert retry["count"] == 2 and [f["attempt"] for f in retry["failures"]] == [1, 2, 3]
    assert not any(f["problems"] == [] for f in retry["failures"])
    assert "I'd like to bring my experience to that work" in paras[0]             # the deterministic fallback letter


def test_a_draft_that_passes_first_time_costs_one_call_and_logs_nothing(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[GOOD, SECOND]])
    assert len(llm.sent) == 1 and problems == [] and retry["failures"] == [] and retry["count"] == 0


def test_a_draft_with_a_problem_that_names_no_word_is_retried_with_the_problem_text(settings, spy_llm):
    too_long = [GOOD + " " + "More words. " * 200, SECOND]
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [too_long, [GOOD, SECOND]])
    assert len(llm.sent) == 2 and problems == [] and retry["rejected_words"] == []
    assert "words >" in llm.sent[1]["user"] and "unless it appears" not in llm.sent[1]["user"]


@pytest.mark.slow
def test_the_review_screen_shows_each_failed_draft_and_the_final_outcome(settings, spy_llm, db, job):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    it = iter([[BAD_1, SECOND], [BAD_2, SECOND], [GOOD, SECOND]])
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": next(it)}})
    rep = tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer")
    text = review.summary(db.job(job["id"]), rep)
    failed = [l for l in rep["letter"]["retry"]["failures"]]
    if rep["letter"]["status"] == "ok":
        assert "Letter accepted on draft" in text
    assert text.count("Letter draft ") == len(failed) and all(f"Letter draft {f['attempt']} rejected: " in text for f in failed)
    assert failed and "LETTER IS A FALLBACK" not in text or rep["letter"]["status"] == "fallback"
