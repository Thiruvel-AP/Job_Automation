"""Fix round 2, item 4: a rephrase must keep what the fact-bank bullet says: every number, the words of its `result:` and its qualifiers
(deterministic, overfitting, held-out, validated, approximately / ~, under, per). A rephrase that drops any is rejected and the original
text is used.

Evidence from the failing dry run:
  CoverFit  "Deterministic comparable-company matching ..." became "Determined comparable-company matching ..." (the meaning changed)
  GTAI      "validated them on held-out data, surfacing overfitting (train Pearson 0.807 vs test 0.545)" became "validating with train
            Pearson 0.807 vs test 0.545": the overfitting finding is gone, so the two numbers read as a success.
Root cause: guard_rephrases ran faithfulness.check (no NEW numbers, no deny term, no new keyword) plus rewrite_rules.qualifier_problems,
which only knew team / contributed / coursework / MSc / prototype / PoC / hackathon / assisted / supported / helped. Nothing asked that
what the bullet says must SURVIVE.
"""
import pytest

from careerops.tailoring.rewrite_rules import must_keep_problems
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.tailor import guard_rephrases

CF = ("Deterministic comparable-company matching over 221 profiles and 1,116 cover lines with a single LLM call for rationale, "
      "at ~£0.003 per query and under 3 s.")
CF_RESULT = "~£0.003 per query, under 3 s"
GG = ("Trained per-gene regression models for 11,459 genes (OLS and GPU random forest) with resumable chunked training, cutting RAM "
      "from 8.2 to 5.5 GB, and validated them on held-out data, surfacing overfitting (train Pearson 0.807 vs test 0.545).")
GG_RESULT = "RAM 8.2 → 5.5 GB; train Pearson 0.807 vs test 0.545"


def problems(new, text, result=""):
    return must_keep_problems(new, text, result)


def test_the_coverfit_rewrite_that_changed_deterministic_to_determined_is_refused():
    out = problems("Determined comparable-company matching over 221 profiles and 1,116 cover lines with a single LLM call for "
                   "rationale, at ~£0.003 per query and under 3 s.", CF, CF_RESULT)
    assert out == ["dropped 'deterministic'"]


def test_the_gtai_rewrite_that_lost_the_overfitting_finding_is_refused():
    out = problems("Trained per-gene regression models for 11,459 genes (OLS and GPU random forest), cutting RAM from 8.2 to 5.5 GB, "
                   "and validating with train Pearson 0.807 vs test 0.545.", GG, GG_RESULT)
    assert "dropped 'overfitting'" in out and "dropped 'held-out'" in out


@pytest.mark.parametrize("new", [
    "Matched comparable companies deterministically over 221 profiles and 1,116 cover lines, with a single LLM call for the rationale, "
    "at ~£0.003 per query and under 3 s.",
    CF.replace("a single LLM call", "one LLM call"),
])
def test_a_faithful_rewording_passes(new):
    assert problems(new, CF, CF_RESULT) == []


def test_a_faithful_gtai_rewording_passes():
    new = ("Trained regression models per gene for 11,459 genes (OLS and GPU random forest) with resumable chunked training, cutting RAM "
           "from 8.2 to 5.5 GB, and validated them on held out data: it surfaced overfitting (train Pearson 0.807 vs test 0.545).")
    assert problems(new, GG, GG_RESULT) == []


@pytest.mark.parametrize("new, want", [
    (CF.replace("221 profiles and ", ""), "dropped the number 221"),
    (CF.replace("1,116", "1116"), None),                                    # the same number written without the comma
    (CF.replace("under 3 s", "quickly"), "dropped the number 3"),
    (CF.replace("~£0.003", "£0.003"), "dropped the approximation '~'"),
    (CF.replace("per query", "a query"), "dropped 'per'"),
    (CF.replace("and under 3 s", "and within 3 s"), "dropped 'under'"),
])
def test_numbers_and_the_small_qualifiers_survive(new, want):
    out = problems(new, CF, CF_RESULT)
    assert (want in out) if want else out == [], out


def test_the_words_of_the_result_survive():
    new = ("Trained per-gene regression models for 11,459 genes, cutting memory from 8.2 to 5.5 GB, validated on held-out data, "
           "surfacing overfitting (Pearson 0.807 vs 0.545).")
    out = problems(new, GG, GG_RESULT)
    assert any("result" in p and "ram" in p.lower() for p in out), out
    assert any("result" in p and "test" in p.lower() for p in out), out


def test_a_source_without_the_qualifier_asks_nothing_of_the_rewording():
    assert problems("Built a churn classifier in R with XGBoost.", "Built a churn classifier in R with XGBoost and tidymodels.") == []


def test_approximately_may_be_written_as_a_tilde_or_the_word():
    assert problems("Wrote approximately 65 tests across 8 files.", "Wrote ~65 tests across 8 files.") == []
    assert problems("Wrote 65 tests across 8 files.", "Wrote ~65 tests across 8 files.") == ["dropped the approximation '~'"]


# ------------------------------------------------------------------ the guard uses it: the original text stays
FB = {"deny_terms": [], "identity": {"name": "A"}, "profile_facts": [], "skills": {}, "education": [], "certifications": [],
      "experience": [], "projects": [{"id": "pj", "name": "Pj", "bullets": [
          {"id": "p_cf1", "text": CF, "result": CF_RESULT, "terms": ["api", "llm", "pandas"]},
          {"id": "p_gg2", "text": GG, "result": GG_RESULT, "terms": ["regression", "random forest", "overfitting"]}]}]}
KWS = [Keyword("LLM", 2, True), Keyword("regression", 2, True)]


def test_guard_rephrases_rejects_both_dry_run_rewrites_and_keeps_the_original():
    proposed = {"p_cf1": "Determined comparable-company matching over 221 profiles and 1,116 cover lines with a single LLM call for "
                         "rationale, at ~£0.003 per query and under 3 s.",
                "p_gg2": "Trained per-gene regression models for 11,459 genes, cutting RAM from 8.2 to 5.5 GB, and validating with train "
                         "Pearson 0.807 vs test 0.545."}
    accepted, violations, _ = guard_rephrases(FB, proposed, KWS)
    assert accepted == {}
    assert any("p_cf1" in v and "deterministic" in v for v in violations)
    assert any("p_gg2" in v and "overfitting" in v for v in violations)


def test_guard_rephrases_still_accepts_a_faithful_rewording():
    good = ("Matched comparable companies deterministically over 221 profiles and 1,116 cover lines with a single LLM call for "
            "rationale, at ~£0.003 per query and under 3 s.")
    accepted, violations, _ = guard_rephrases(FB, {"p_cf1": good}, KWS)
    assert list(accepted) == ["p_cf1"] and violations == []
