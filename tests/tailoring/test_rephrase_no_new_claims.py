"""Rephrasing may not add claims (round of 17 Oct, item 3).

A reworded bullet may not contain a content word that is in neither the original bullet text nor the ad's keyword synonyms for a
term the original already has. Evidence: az3 ("Deployed the agent as a Docker container on AWS Lambda ...") gained "and MLOps
practices", taken from its `terms:` list, which the bullet never says. No network, no model.
"""
import pytest

from careerops.tailoring import tailor
from careerops.tailoring.rewrite_rules import added_word_problems
from careerops.tailoring.scorer import Keyword

A3 = ("Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines, IAM execution roles and least-privilege "
      "policies, replacing a manual copy-and-paste release process.")
KWS = [Keyword("Docker", 2, True), Keyword("MLOps", 2, True), Keyword("AWS", 2, True)]


# ---------------------------------------------------------------- the rule
def test_a_word_from_the_terms_list_that_the_bullet_never_says_is_a_new_claim():
    new = A3.rstrip(".") + " and MLOps practices."
    out = added_word_problems(new, A3, KWS)
    assert any("'mlops'" in p for p in out) and any("'practices'" in p for p in out), out


def test_a_reordering_with_the_same_content_words_adds_nothing():
    new = ("Replaced a manual copy-and-paste release process by deploying the agent as a Docker container on AWS Lambda with ECR "
           "image pipelines, IAM execution roles and least-privilege policies.")
    assert added_word_problems(new, A3, KWS) == []


def test_another_form_of_a_word_the_bullet_has_is_not_new():
    assert added_word_problems("Prototyping a chat tool.", "Prototyped a chat tool.", []) == []


def test_the_ads_synonym_for_a_term_the_bullet_already_has_is_allowed():
    kws = [Keyword("LLM", 3, True, ["large language models"])]
    assert added_word_problems("Built a tool-calling large language models agent.", "Built a tool-calling LLM agent.", kws) == []


def test_the_ads_synonym_for_a_term_the_bullet_does_not_have_is_not_allowed():
    kws = [Keyword("LLM", 3, True, ["large language models"])]
    out = added_word_problems("Built a tool-calling agent on large language models.", "Built a tool-calling agent.", kws)
    assert any("'language'" in p for p in out), out


def test_glue_words_numbers_and_the_bullets_result_are_not_new_claims():
    assert added_word_problems("With 12 tests, the team checked it.", "The team checked it with 12 tests.", []) == []
    assert added_word_problems("Cut cost, about £0.003 per query.", "Cut cost.", [], result="about £0.003 per query") == []


def test_dropping_words_is_always_allowed():
    assert added_word_problems("Deployed the agent on AWS Lambda.", A3, KWS) == []


# ---------------------------------------------------------------- the guard
def test_the_guard_refuses_a_rewording_that_appends_mlops_practices_and_keeps_the_original(fb_fixture):
    fb = fb_fixture
    original = tailor.bullet_index(fb)["a3"]["text"]
    assert "mlops" in tailor.bullet_index(fb)["a3"]["terms"] and "mlops" not in original.lower()
    kws = [Keyword("Docker", 2, True), Keyword("MLOps", 2, True)]
    accepted, violations, _ = tailor.guard_rephrases(fb, {"a3": original.rstrip(".") + " and MLOps practices."}, kws)
    assert accepted == {} and any(v.startswith("a3:") and "'mlops'" in v for v in violations), violations


def test_the_guard_still_accepts_an_honest_reordering(fb_fixture):
    original = tailor.bullet_index(fb_fixture)["a3"]["text"]
    new = ("Replaced a manual copy-and-paste release process by deploying the agent as a Docker container on AWS Lambda with ECR image "
           "pipelines, IAM execution roles and least-privilege policies.")
    assert original != new
    accepted, violations, _ = tailor.guard_rephrases(fb_fixture, {"a3": new}, [Keyword("Docker", 2, True)])
    assert accepted == {"a3": new} and violations == []


def test_the_model_is_not_asked_to_introduce_a_keyword_from_a_terms_list(fb_fixture, spy_llm):
    """The offers are the other half of the fault: 'MAY INTRODUCE: mlops' is how az3 came to be asked for the claim."""
    kws = [Keyword("Docker", 2, True), Keyword("MLOps", 2, True), Keyword("pytest", 2, True)]
    llm = spy_llm()
    sel = {"experience": {"ex1": ["a3", "a4"]}, "projects": []}
    proposed, stats = tailor.propose_rephrases({"title": "AI Engineer", "company": "Northwind"}, fb_fixture, sel, kws, llm, {}, [])
    assert stats["offered"] == {} and llm.sent == [] and proposed == {}
    assert stats["unoffered"] == ["MLOps", "pytest"], stats                  # reported as missed, never put in a bullet


@pytest.mark.slow
def test_a_tailored_cv_keeps_the_original_text_when_the_model_appends_mlops_practices(settings, fb_fixture, spy_llm):
    jd = "Requirements\nPython\nDocker\nAWS Lambda\nMLOps\n"
    job = {"id": "manual:mlops1", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer", "location": "Bristol, UK",
           "url": "https://example.com/j", "apply_url": "https://example.com/j", "description": jd}

    def rephrase(ctx):
        return {b["id"]: b["text"].rstrip(".") + " and MLOps practices." for b in ctx["bullets"]}

    llm = spy_llm({"rephrase_bullets": rephrase, "extract_keywords": lambda ctx: {"required": ["Python", "Docker", "AWS Lambda", "MLOps"],
                                                                                 "preferred": [], "company_hook": "", "team": ""}})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    texts = {bid: t for sec in ("experience", "projects") for e in rep["context"][sec] for bid, t in zip(e["bullet_ids"], e["bullets"])}
    assert "a3" in texts and "MLOps practices" not in " ".join(texts.values())
    assert texts["a3"] == tailor.bullet_index(fb_fixture)["a3"]["text"]
