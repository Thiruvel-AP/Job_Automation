"""Cover letter: every sentence is checked against the evidence bullet it is written from and the JD. A proper noun,
client, sector or capability word that is in neither is flagged; the letter is retried once, then flagged (fallback)."""
import pytest
from conftest import restate

from careerops.tailoring.grounding import grounding_problems, sentences
from careerops.tailoring.tailor import _style_problems

EV1 = {"text": "Built a tool-calling LLM agent (LangChain, AWS Bedrock) for a multi-omics cancer research platform, with "
               "three tools for gene alias resolution and dataset lookup, used by the whole research team.",
       "terms": ["llm", "agent", "agentic", "langchain", "bedrock", "aws", "tool calling", "python"]}
EV2 = {"text": "Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines and IAM execution roles.",
       "terms": ["docker", "aws lambda", "serverless", "deployment", "mlops"]}
JD = "We help brands turn customer feedback into insight. You will build LLM features in Python and ship them."
HOOK = "Acme turns customer feedback into insight for brands."
EXTRA = "Acme Data Scientist"


def _check(*paras, evidence=(EV1, EV2), jd=JD, hook=HOOK, gaps=None):
    return grounding_problems(list(paras), list(evidence), jd, hook, EXTRA, gaps)


def test_a_sentence_that_stays_inside_its_evidence_and_the_jd_passes():
    assert _check("Acme turns customer feedback into insight, and my work on a tool-calling LLM agent built with "
                  "LangChain is the same kind of problem.",
                  "I deployed that agent as a Docker container on AWS Lambda with IAM execution roles.",
                  "Could we talk for twenty minutes about it?") == []


def test_an_invented_client_and_sector_are_flagged():
    out = _check("I built a tool-calling LLM agent with LangChain for a fintech client.")
    assert any("'fintech' (sector)" in p for p in out) and any("'client' (client)" in p for p in out)
    assert all("is in neither the evidence nor the job description" in p for p in out)


def test_a_sector_or_client_word_the_jd_uses_is_allowed():
    jd = JD + " Our clients are in healthcare."
    assert _check("I built a tool-calling LLM agent with LangChain that clients in healthcare would recognise.",
                  jd=jd) == []


def test_an_invented_employer_or_product_is_flagged_but_the_company_and_the_evidence_are_not():
    out = _check("At Globex Corporation I built a tool-calling LLM agent with LangChain and AWS Bedrock.")
    assert any("'Globex' (name)" in p for p in out) and any("'Corporation' (name)" in p for p in out)
    assert not any("LangChain" in p or "Bedrock" in p or "AWS" in p for p in out)
    assert _check("At Acme I would use my LangChain agent work on the Data Scientist team.") == []     # company, role


def test_an_acronym_or_camel_case_name_is_a_name_even_at_the_start_of_a_sentence():
    assert any("'BERT' (name)" in p for p in _check("BERT fine-tuning was part of my tool-calling LLM agent work."))
    assert any("'PyTorch' (name)" in p for p in _check("PyTorch models fed my tool-calling LLM agent in LangChain."))
    assert _check("Often an ordinary capitalised word merely starts the sentence.") == []      # not a name


def test_a_capability_the_sentences_own_bullet_lacks_is_flagged_even_if_the_other_bullet_has_it():
    out = _check("I built a tool-calling LLM agent with LangChain and packaged it in Docker.")
    assert any("'docker'" in p.lower() for p in out)                     # Docker belongs to EV2, not to this sentence
    assert _check("I deployed the agent as a Docker container on AWS Lambda with ECR image pipelines.") == []


def test_a_capability_the_jd_names_is_allowed_even_when_the_bullet_does_not_say_it():
    assert _check("I built a tool-calling LLM agent with LangChain in Python.") == []          # Python is in the JD
    out = _check("I built a tool-calling LLM agent with LangChain and fine-tuning in Python.")
    assert any("fine-tuning" in p for p in out)                          # in neither


@pytest.mark.parametrize("word", ["production", "real-time", "end-to-end", "dashboards", "Kubernetes", "SQL", "pytest"])
def test_capability_words_not_backed_by_evidence_or_jd_are_flagged(word):
    out = _check(f"I built a tool-calling LLM agent with LangChain with {word} support.")
    assert any(word.lower() in p.lower() for p in out), out


def test_the_sentence_that_says_the_candidate_lacks_a_required_tool_may_say_how_it_would_be_used():
    para = "I have not used Kubernetes in production yet, and I would pick it up quickly."
    assert _check("Acme turns customer feedback into insight.", para, gaps=["kubernetes"]) == []
    assert any("'kubernetes'" in p.lower() for p in _check(para))        # without GAPS it is a claim about a tool


def test_a_sentence_matching_no_bullet_is_checked_against_everything_the_model_was_given():
    assert _check("Acme needs someone who can ship Docker images to AWS Lambda.") == []      # the hook/JD + EV2
    assert any("'mlflow'" in p.lower() for p in _check("Acme needs someone who knows MLflow."))


def test_problems_are_reported_once_per_term_per_sentence_and_name_the_sentence():
    out = _check("I built a tool-calling LLM agent with LangChain using Kubernetes and Kubernetes.")
    kube = [p for p in out if "kubernetes" in p.lower()]
    assert len(kube) == 1 and "I built a tool-calling LLM agent" in kube[0]


def test_no_evidence_means_everything_is_checked_against_the_jd_and_hook_alone():
    assert _check("Acme turns customer feedback into insight.", evidence=()) == []
    assert any("langchain" in p.lower() for p in _check("I used LangChain.", evidence=()))


def test_sentences_splits_on_terminal_punctuation():
    assert sentences("One. Two? Three!  Four") == ["One.", "Two?", "Three!", "Four"]


# ---------------------------------------------------------------- the last paragraph is a question
def test_the_call_to_action_paragraph_must_end_on_a_question():
    assert _style_problems(["Hook.", "Example.", "Could we talk for twenty minutes?"], ["Built a pipeline."]) == []
    assert _style_problems(["Hook.", "Example.", "Could we talk? I am free on Monday."], ["Built a pipeline."]) == [
        "last paragraph is not a call-to-action question"]


# ---------------------------------------------------------------- in the pipeline
LIE = "I built this agent for a fintech client."


def _drafts(*drafts):
    """A cover_letter handler answering with each draft in turn (the last one repeats); a draft is a function of the
    evidence lines the model was given, so the honest ones can quote them."""
    queue = list(drafts)
    return lambda ctx: {"paragraphs": (queue.pop(0) if len(queue) > 1 else queue[0])(ctx["evidence"])}


def _i(bullet: str) -> str:
    """A bullet as first-person prose in other words: the letter guard rejects a bullet pasted, with or without "I"."""
    return restate(bullet)


def _honest(ev):
    return ["Northwind Health AI builds tools I have worked on.", _i(ev[0]), _i(ev[1]), "Could we find twenty minutes to talk?"]


def _lying(ev):
    return ["Northwind Health AI builds tools I have worked on.", _i(ev[0]) + " " + LIE, _i(ev[1]),
            "Could we find twenty minutes to talk?"]


@pytest.mark.slow
def test_an_invented_client_is_retried_once_with_the_problem_listed_and_the_honest_draft_is_accepted(
        settings, fb_fixture, job, spy_llm):
    from careerops.tailoring import tailor
    llm = spy_llm({"cover_letter": _drafts(_lying, _honest)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    calls = [s for s in llm.sent if s["task"] == "cover_letter"]
    assert len(calls) == 2 and "YOUR LAST DRAFT BROKE THESE RULES" in calls[1]["user"]
    assert "'fintech' (sector)" in calls[1]["user"] and "'client' (client)" in calls[1]["user"]
    assert rep["letter"]["status"] == "ok" and rep["letter"]["problems"] == []
    assert LIE not in " ".join(rep["letter"]["paragraphs"])


@pytest.mark.slow
def test_an_invention_that_survives_the_retry_is_flagged_and_the_letter_is_the_fallback(settings, fb_fixture, job,
                                                                                         spy_llm):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    from careerops.tailoring import tailor
    llm = spy_llm({"cover_letter": _drafts(_lying)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert [s["task"] for s in llm.sent].count("cover_letter") == 3          # a draft, two retries, then stop
    letter = rep["letter"]
    assert letter["status"] == "fallback" and tailor.FALLBACK_MARK in letter["problems"]
    assert any("'fintech' (sector)" in p for p in letter["problems"])
    text = " ".join(letter["paragraphs"])
    assert "fintech" not in text and letter["paragraphs"][-1].endswith("?")
    from careerops.tailoring.letter_rules import clause
    assert all(clause(e["text"]) in text for e in letter["evidence"])         # the fallback uses the evidence as clauses
    assert rep["final"]["pages"] <= settings["scoring"]["max_pages"] and letter["pages"] == 1


@pytest.mark.slow
def test_the_evidence_ids_are_in_the_report_and_survive_a_regenerated_letter(settings, fb_fixture, job, spy_llm):
    from careerops.tailoring import tailor
    rep = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))
    before = [e["id"] for e in rep["letter"]["evidence"]]
    rep = tailor.regenerate_letter(job, fb_fixture, spy_llm(), settings, rep)
    assert len(before) == 2 and [e["id"] for e in rep["letter"]["evidence"]] == before
