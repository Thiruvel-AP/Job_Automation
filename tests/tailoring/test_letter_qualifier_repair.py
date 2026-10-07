"""C1-C3 at letter level: a draft wrong only by unsupported qualifier words is repaired (words deleted, FULL check re-run);
anything else is rejected exactly as before, and the retry feedback reaches prompts 2 and 3 when the model repeats the word."""
import pytest
from test_letter_attempts import BAD_1, BAD_2, GOOD, ONLY_1, ONLY_2, SECOND, _run

from careerops.review import workflow as review
from careerops.tailoring import qualifiers, tailor


def test_a_draft_whose_only_violation_is_a_qualifier_is_repaired_with_no_second_call(settings, spy_llm):
    """Monzo, draft 1: rejected for 'integrated' alone. Now the word is deleted, the whole check re-run, the edit accepted."""
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[ONLY_1, SECOND]])
    assert len(llm.sent) == 1 and problems == [] and paras[0] == GOOD
    assert "integrated" not in " ".join(paras) and paras[1] == SECOND
    assert retry["removed"] == ["integrated"] and retry["outcome"] == "qualifiers removed" and retry["failures"] == []
    assert any("removed unsupported qualifier(s) integrated" in n for n in notes)


def test_the_monzo_sequence_integrated_then_specialised_now_costs_one_call(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[ONLY_1, SECOND], [ONLY_2, SECOND]])
    assert len(llm.sent) == 1 and problems == [] and paras[0] == GOOD
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[ONLY_2, SECOND]])
    assert len(llm.sent) == 1 and problems == [] and paras[0] == GOOD and retry["removed"] == ["specialised"]


def test_the_repair_is_accepted_by_the_full_check_not_taken_on_trust(settings, spy_llm, monkeypatch):
    """If the edited draft would still fail anything, the repair is refused and the draft rejected as before."""
    monkeypatch.setattr(tailor, "repair", lambda body, problems, recheck: qualifiers.repair(
        body, problems, lambda edited: ["something else is wrong"]))
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[ONLY_1, SECOND]] * 3)
    assert len(llm.sent) == 3 and tailor.FALLBACK_MARK in problems and retry["removed"] == []
    assert retry["outcome"] == "fallback"


def test_a_repair_after_a_real_rejection_keeps_the_earlier_failure_for_the_review_screen(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[BAD_1, SECOND], [ONLY_2, SECOND]])
    assert len(llm.sent) == 2 and problems == [] and paras[0] == GOOD
    assert retry["outcome"] == "qualifiers removed after retry" and retry["removed"] == ["specialised"]
    assert [f["attempt"] for f in retry["failures"]] == [1]


def test_a_qualifier_with_any_other_violation_is_still_rejected_and_the_word_is_never_deleted(settings, spy_llm):
    """The scripted model repeats the rejected words every time. 'specialised' + an unsupported tool is not repairable."""
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[BAD_2, SECOND]] * 3)
    assert len(llm.sent) == 3 and tailor.FALLBACK_MARK in problems and retry["outcome"] == "fallback"
    assert retry["removed"] == [] and "specialised" not in " ".join(paras)             # the fallback letter is the template
    assert all(any("'specialised'" in p for p in f["problems"]) for f in retry["failures"])


def test_a_qualifier_that_is_part_of_a_skill_name_is_rejected_not_cut(settings, spy_llm):
    """'deep' is a qualifier, but "deep learning" is a skill: the claim check owns that one."""
    draft = GOOD.replace("my work building", "my deep learning work building")
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[draft, SECOND]] * 3)
    assert tailor.FALLBACK_MARK in problems and retry["removed"] == [] and len(llm.sent) == 3


def test_retry_feedback_reaches_prompts_two_and_three_when_the_model_repeats_the_rejected_word(settings, spy_llm):
    paras, problems, notes, retry, llm = _run(settings, spy_llm, [[BAD_2, SECOND]] * 3)
    first, second, third = (c["user"] for c in llm.sent)
    assert "The check rejected these words" not in first and "YOUR LAST DRAFT" not in first
    for later in (second, third):
        assert "The check rejected these words as unsupported: 'Spark', 'specialised'." in later
        assert "Do not use 'specialised' unless it appears in the evidence bullets." in later
        assert "Do not use 'Spark' unless it appears in the evidence bullets." in later
        assert "YOUR LAST DRAFT BROKE THESE RULES; fix every one:" in later and "is not in the two evidence bullets" in later
    assert retry["rejected_words"] == ["Spark", "specialised"] and retry["count"] == 2


def test_the_letter_prompt_bans_qualifiers_up_front_except_the_ones_the_evidence_uses(settings, spy_llm):
    _, _, _, _, llm = _run(settings, spy_llm, [[GOOD, SECOND]])
    system = llm.sent[0]["system"]
    assert "{qualifiers}" not in system and "Banned unless EVIDENCE has the exact word:" in system
    banned = system.split("Banned unless EVIDENCE has the exact word:")[1].split(".\n")[0]
    for w in ("specialised", "integrated", "extensive", "deep", "proven", "seamless", "robust", "strong", "solid"):
        assert w in banned, w
    text = "Built a robust parser and a strong baseline."
    assert "robust" not in qualifiers.prompt_list(text) and "strong" not in qualifiers.prompt_list(text)
    assert "specialised" in qualifiers.prompt_list(text)


@pytest.mark.slow
def test_the_review_screen_says_which_words_were_removed(settings, spy_llm, db, job):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [ONLY_1, SECOND]}})
    rep = tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    rep["letter"]["retry"]["removed"] = ["integrated", "specialised"]          # whatever this model wrote, the screen reports it
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer")
    text = review.summary(db.job(job["id"]), rep)
    assert "Letter removed: integrated, specialised" in text
    rep["letter"]["retry"]["removed"] = []
    assert "Letter removed" not in review.summary(db.job(job["id"]), rep)
