"""Fix round 2, item 3: a letter never carries a pasted `terms:` list, never writes a known technology in lower case, and in hook mode
a letter that fails its checks is "none", never a template.

The evidence of the failing Anaplan dry run: "...mirrors my work on docker, containerised, containerized, aws lambda, serverless, mlops,
cloud, iam, security, production." That is bullet az3's `terms` list. Root cause: evidence_fields.fields() sent the raw `terms` as the
`method` field whenever the bank had no `method:` key, and the model copied the field into prose; no check looked for it.
No code interpolates `terms` into letter text (letter_rules.fallback_letter uses bullet text), so there was no such template to
delete; what is removed is the template gap sentence, and the raw-terms method field.
"""
import pytest

from careerops.llm import MockLLM
from careerops.tailoring import evidence_fields as ef
from careerops.tailoring import letters as L
from careerops.tailoring import tailor
from careerops.tailoring.letter_rules import term_dump_problems
from careerops.tailoring.scorer import Keyword

AZ3 = ["docker", "containerised", "containerized", "aws lambda", "serverless", "deployment", "mlops", "cloud", "iam", "security", "production"]
FL1 = ["machine learning", "deep learning", "scikit-learn", "tensorflow", "keras", "fastapi", "docker", "deployment", "model deployment", "python"]
AZ3_TEXT = "Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines and IAM execution roles."
FL1_TEXT = "Built scikit-learn pipelines and LSTM models in TensorFlow/Keras served as FastAPI services in Docker."


def bank():
    return {"experience": [{"id": "az", "org": "Acme", "bullets": [
        {"id": "az3", "text": AZ3_TEXT, "terms": AZ3}, {"id": "fl1", "text": FL1_TEXT, "terms": FL1}]}], "projects": []}


# ------------------------------------------------------------------ the guard
@pytest.mark.parametrize("text", [
    "The role mirrors my work on docker, containerised, containerized, aws lambda, serverless, mlops, cloud, iam, security, production.",
    "It mirrors my work on machine learning, deep learning, scikit-learn, tensorflow, keras, fastapi, docker, deployment, model deployment, python.",
    "I worked with Docker, AWS Lambda, and IAM.",                    # three bare terms in a row: a list, not a sentence
    "The same pipeline uses MLOps, cloud, serverless, in my last role.",
])
def test_a_run_of_three_or_more_terms_is_a_pasted_list(text):
    out = term_dump_problems([text], bank())
    assert out and any("list of terms" in p for p in out), out


@pytest.mark.parametrize("text", [
    "I deployed the agent as a Docker container on AWS Lambda, behind IAM execution roles.",
    "Docker and AWS Lambda are where most of my deployment work happened.",
    "I use Python, SQL and pandas daily.",                            # two comma items: a sentence
    "Production matters, so I wrote tests first.",
    "",
])
def test_ordinary_sentences_pass(text):
    assert term_dump_problems([text], bank()) == []


@pytest.mark.parametrize("word", ["docker", "aws", "fastapi", "python", "tensorflow", "keras"])
def test_a_known_technology_in_lower_case_is_refused(word):
    out = term_dump_problems([f"I shipped the model with {word} behind an API."], bank())
    assert any(f"lower case '{word}'" in p for p in out), out


def test_correct_casing_of_every_listed_technology_passes():
    text = "I shipped Keras and TensorFlow models behind FastAPI on AWS in Docker, written in Python."
    assert term_dump_problems([text], bank()) == []


def test_a_token_repeated_inside_one_list_is_refused():
    out = term_dump_problems(["My focus is tests, tests, and review."], bank())
    assert any("repeated" in p for p in out), out
    assert term_dump_problems(["I used Docker images, Docker images, and queues."], bank())


def test_the_check_is_applied_to_a_model_drafted_letter_body(settings, spy_llm):
    dump = ["The role mirrors my work on docker, containerised, aws lambda, serverless, mlops, cloud, iam.", "I also built scikit-learn pipelines."]
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": dump}})
    ev = [{"id": "az3", "text": AZ3_TEXT, "terms": AZ3}, {"id": "fl1", "text": FL1_TEXT, "terms": FL1}]
    job = {"company": "Acme", "title": "Data Scientist", "description": "docker aws"}
    paras, problems = tailor._letter(job, bank(), [], "", ev, llm, settings, [], [], "", {}, False)
    assert paras == [] and any("list of terms" in p for p in problems), problems


# ------------------------------------------------------------------ the evidence fields
def test_the_method_field_names_only_the_tools_the_bullet_itself_says():
    ev = {"id": "az3", "text": AZ3_TEXT, "terms": AZ3}
    assert ef.fields(ev)["method"] == "Docker; AWS Lambda; IAM"
    block = ef.block([ev])
    for synonym in ("containerised", "containerized", "serverless", "mlops", "security"):
        assert synonym not in block


def test_a_bullet_whose_text_names_no_term_says_not_stated():
    assert ef.fields({"id": "x", "text": "Built a thing.", "terms": ["python", "sql"]})["method"] == "(not stated)"


def test_the_banks_own_method_key_still_wins():
    assert ef.fields({"id": "x", "text": "t", "terms": ["a"], "method": "pandas and SQL joins"})["method"] == "pandas and SQL joins"


# ------------------------------------------------------------------ hook mode: a failed letter is "none", not a template
@pytest.mark.slow
def test_an_assembled_body_that_dumps_terms_leaves_letter_none_and_no_template(settings, fb_fixture, job, monkeypatch):
    settings.raw["letter"]["mode"] = "hook"
    dump = ["It mirrors my work on docker, aws lambda, serverless, mlops, cloud, iam.",
            "I also worked on machine learning, deep learning, scikit-learn, fastapi, python."]
    monkeypatch.setattr(L, "assemble_body", lambda *a, **k: list(dump))
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
    a, b = rep["letters"]["A"], rep["letters"]["B"]
    assert a["status"] == b["status"] == "none" and a["pdf"] is None and b["pdf"] is None and rep["letters"]["choice"] == "none"
    assert any("list of terms" in p for p in a["problems"]), a["problems"]
    assert rep["letter"]["status"] == "none" and rep["letter_pdf"] is None


def test_there_is_no_template_gap_sentence_and_no_gap_model_call():
    assert not hasattr(L, "template_gap") and not hasattr(L, "draft_gap") and not hasattr(L, "draft_body")


# ------------------------------------------------------------------ the gap and its bridge
def test_the_gap_may_be_a_deny_listed_tool_it_comes_from_the_learn_list():
    kws = [Keyword("dbt", 2, True), Keyword("Python", 3, True)]
    learn = L.learn_list(kws, ["Python"], ["dbt"])
    assert [r["term"] for r in learn] == ["dbt"] and learn[0]["confirmed_absent"]
    assert L.pick_gap(kws, learn, "We use dbt for modelling.") == "dbt"


def test_the_bridge_for_dbt_and_for_lightgbm_is_a_neighbour_the_bank_really_has(fb_fixture):
    from careerops.tailoring.tailor import possible_evidence

    fb = {**fb_fixture}
    fb["skills"] = {**fb["skills"], "ML & Stats": [*fb["skills"]["ML & Stats"], "XGBoost"]}
    possible = possible_evidence(fb, ["dbt", "LightGBM"], {})
    assert possible["dbt"][0]["via"].lower() == "sql"
    assert possible["LightGBM"][0]["via"].lower() == "xgboost"
    kws = [Keyword("dbt", 2, True), Keyword("LightGBM", 3, True), Keyword("XGBoost", 3, True), Keyword("Python", 3, True)]
    assert L.pick_bridge("dbt", fb, kws, ["XGBoost", "Python"], possible).lower() == "sql"
    assert L.pick_bridge("LightGBM", fb, kws, ["XGBoost", "Python"], possible).lower() == "xgboost"


def test_a_neighbour_the_bank_does_not_have_is_never_the_bridge():
    kws = [Keyword("LightGBM", 3, True), Keyword("XGBoost", 3, True)]
    assert L.pick_bridge("LightGBM", {}, kws, ["XGBoost"], {"LightGBM": [{"via": "CatBoost", "where": []}]}) == "XGBoost"
    assert L.pick_bridge("LightGBM", {}, kws, [], {"LightGBM": [{"via": "CatBoost", "where": []}]}) is None
    assert L.pick_bridge("LightGBM", {"deny_terms": ["catboost"]}, kws, ["XGBoost"],
                         {"LightGBM": [{"via": "CatBoost", "where": ["skills: ML"]}]}) == "XGBoost"
