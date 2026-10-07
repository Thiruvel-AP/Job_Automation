"""Letter template + CV filler round (review of run 20261006-201304): letter evidence is verb-led bullets only, the fallback
letter is first person and never pastes a bullet twice, gaps keep their casing, and a CV rewording does not end in a keyword
-carrying participial clause or a bare keyword. No model, no network."""
import pytest

from careerops.tailoring import tailor
from careerops.tailoring.grounding import repeated_evidence_problems
from careerops.tailoring.letter_rules import (clause, fallback_letter, gap_display, grammar_problems, org_label,
                                              starts_with_past_verb)
from careerops.tailoring.rewrite_rules import bare_keyword_problems, filler_problems
from careerops.tailoring.scorer import Keyword

ORG = {"fl": "Self-employed, Chennai", "az": "AstraZeneca · GeneTraceAI, University of Bristol",
       "dc": "180 Degrees Consulting Bristol"}
TXT = {
    "fl2": "Built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain and LangGraph.",
    "fl1": "Built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services in Docker.",
    "az1": "Built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research platform, "
           "with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.",
    "dc1": "Proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions "
           "CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.",
    "az4": "Wrote a three-tier test framework (tool-level, API/integration, live agent) of ~65 tests across 8 files, validated "
           "across the LLM provider migration from Groq to AWS Bedrock after a rate-limit evaluation.",
    "p_ch1": "XGBoost churn classifier in R (tidymodels) with two-stage Bayesian hyperparameter search, SMOTE inside the recipe "
             "and MCC evaluation."}
HOOK = {
    "Chattermill": "Chattermill builds a Customer Experience Intelligence platform that continuously analyses explicit and "
                   "implicit customer feedback for large brands such as Uber, Amazon, Wise, and HelloFresh.",
    "Creditspring": "Creditspring offers subscription-based short-term financial products to improve members' financial "
                    "stability and resilience.",
    "Heathrow Airport Holdings": "Heathrow Airport Holdings develops analytical models and data science solutions to improve "
                                 "airport operations and inform business decisions.",
    "Lendable": "Lendable builds fintech technology to give customers instant credit and savings solutions, rebuilding core "
                "consumer finance products and expanding into UK and US markets.",
    "Quantcast": "Quantcast builds AI-powered advertising technology that optimizes digital ad auctions in real time, "
                 "delivering measurable outcomes for marketers worldwide.",
    "Trainline": "Trainline empowers greener travel choices by connecting millions of travellers to rail and coach journeys "
                 "through its mobile app, website and channels.",
    "Zone & Co": "Zone & Co builds an ERP-native AI platform that automates, connects, and scales financial workflows directly "
                 "within Oracle NetSuite, delivering real-time financial intelligence to finance teams worldwide."}
CASES = {"Chattermill": ("fl2", "az1", []), "Creditspring": ("fl1", "dc1", []), "Heathrow Airport Holdings": ("fl1", "dc1", []),
         "Lendable": ("dc1", "az4", []), "Quantcast": ("fl1", "az1", ["Java"]), "Trainline": ("az1", "dc1", ["Power BI", "DBT"]),
         "Zone & Co": ("az1", "dc1", ["azure", "AutoGen", "DSPy"])}


def _ev(*ids):
    return [{"id": i, "text": TXT[i], "org": ORG[i[:2]]} for i in ids]


def _letter(company):
    a, b, gaps = CASES[company]
    return fallback_letter({"company": company, "title": "X"}, HOOK[company], _ev(a, b), gaps, "CTA?", [])


# The fallback letter of each of the seven calibration companies, word for word (checked by eye for grammar)
SNAPSHOTS = {
    'Chattermill': [
        "Chattermill builds a Customer Experience Intelligence platform that continuously analyses explicit and implicit customer feedback for large brands such as Uber, Amazon, Wise, and HelloFresh. I'd like to bring my experience to that work: in my freelance AI/ML work, I built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain and LangGraph.",
        'At AstraZeneca, I built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research platform, with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.',
        'CTA?',
    ],
    'Creditspring': [
        "Creditspring offers subscription-based short-term financial products to improve members' financial stability and resilience. I'd like to bring my experience to that work: in my freelance AI/ML work, I built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services in Docker.",
        'At 180 Degrees Consulting Bristol, I proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.',
        'CTA?',
    ],
    'Heathrow Airport Holdings': [
        "Heathrow Airport Holdings develops analytical models and data science solutions to improve airport operations and inform business decisions. I'd like to bring my experience to that work: in my freelance AI/ML work, I built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services in Docker.",
        'At 180 Degrees Consulting Bristol, I proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.',
        'CTA?',
    ],
    'Lendable': [
        "Lendable builds fintech technology to give customers instant credit and savings solutions, rebuilding core consumer finance products and expanding into UK and US markets. I'd like to bring my experience to that work: at 180 Degrees Consulting Bristol, I proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.",
        'At AstraZeneca, I wrote a three-tier test framework (tool-level, API/integration, live agent) of ~65 tests across 8 files, validated across the LLM provider migration from Groq to AWS Bedrock after a rate-limit evaluation.',
        'CTA?',
    ],
    'Quantcast': [
        "Quantcast builds AI-powered advertising technology that optimizes digital ad auctions in real time, delivering measurable outcomes for marketers worldwide. I'd like to bring my experience to that work: in my freelance AI/ML work, I built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services in Docker.",
        'At AstraZeneca, I built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research platform, with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.',
        'I have not used Java yet.',
        'CTA?',
    ],
    'Trainline': [
        "Trainline empowers greener travel choices by connecting millions of travellers to rail and coach journeys through its mobile app, website and channels. I'd like to bring my experience to that work: at AstraZeneca, I built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research platform, with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.",
        'At 180 Degrees Consulting Bristol, I proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.',
        'I have not used Power BI yet.',
        'CTA?',
    ],
    'Zone & Co': [
        "Zone & Co builds an ERP-native AI platform that automates, connects, and scales financial workflows directly within Oracle NetSuite, delivering real-time financial intelligence to finance teams worldwide. I'd like to bring my experience to that work: at AstraZeneca, I built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research platform, with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.",
        'At 180 Degrees Consulting Bristol, I proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.',
        'I have not used Azure yet.',
        'CTA?',
    ],
}


@pytest.mark.parametrize("company", list(CASES))
def test_the_fallback_letter_of_each_calibration_company_matches_its_snapshot(company):
    assert _letter(company) == SNAPSHOTS[company]


@pytest.mark.parametrize("company", list(CASES))
def test_no_fallback_paragraph_starts_with_a_bare_verb_or_repeats_a_bullet_or_says_the_broken_phrases(company):
    paras = _letter(company)
    a, b, gaps = CASES[company]
    for p in paras:
        assert not starts_with_past_verb(p), p                                  # "Built ..." has no subject
        low = p.lower()
        assert "my work on built" not in low and "mirrors my work" not in low and "on built" not in low
    text = " ".join(paras)
    for i in (a, b):
        assert TXT[i] not in text and text.count(TXT[i][1:].rstrip(".")[:40]) == 1     # a bullet appears once, as a clause
    assert len(paras) == 3 + bool(gaps)                                          # P1, P2, [P3 gap], P4
    assert paras[0].count("I'd like to bring my experience to that work: ") == 1 and "Self-employed" not in paras[0]
    assert paras[1].startswith(("At ", "In my ")) and ", I " in paras[1] and paras[-1] == "CTA?"


def test_the_gap_paragraph_only_exists_when_there_is_a_gap_and_is_cased_canonically():
    assert not any("I have not used" in p for p in _letter("Chattermill"))
    assert "I have not used Azure yet." in _letter("Zone & Co")           # the model's "azure" is written "Azure"
    assert "I have not used Power BI yet." in _letter("Trainline")
    assert "I have not used Java yet." in _letter("Quantcast")


def test_the_clause_is_the_bullet_with_a_lower_case_first_letter_and_no_final_period():
    assert clause("Built NLP and RAG workflows.") == "built NLP and RAG workflows"
    assert clause("Proposed and prototyped a tool.  ") == "proposed and prototyped a tool"


def test_the_second_bullet_must_differ_from_the_first_and_one_bullet_gives_a_short_letter():
    one = fallback_letter({"company": "Acme", "title": "DS"}, "Acme builds things.", _ev("fl1", "fl1"), [], "CTA?", [])
    assert len(one) == 2 and one[0].count("scikit-learn") == 1               # the same bullet twice is not pasted twice
    only = fallback_letter({"company": "Acme", "title": "DS"}, "Acme builds things.", _ev("fl1"), ["Azure"], "CTA?", [])
    assert [p.split()[0] for p in only] == ["Acme", "I", "CTA?"]


@pytest.mark.parametrize("org, want", [
    ("Self-employed, Chennai", "in my freelance AI/ML work"), ("Self-employed", "in my freelance AI/ML work"),
    ("AstraZeneca · GeneTraceAI, University of Bristol", "at AstraZeneca"), ("Avasoft", "at Avasoft"),
    ("180 Degrees Consulting Bristol", "at 180 Degrees Consulting Bristol")])
def test_an_org_is_labelled_for_the_letter_not_pasted_raw(org, want):
    assert org_label(org) == want


def test_the_fallback_letter_says_freelance_work_and_at_astrazeneca_not_the_raw_orgs():
    text = " ".join(fallback_letter({"company": "Acme", "title": "DS"}, "Acme builds things.", _ev("fl1", "az1"), [], "CTA?", []))
    assert "in my freelance AI/ML work, I built scikit-learn pipelines" in text and "At AstraZeneca, I built a tool-calling" in text
    assert "Self-employed" not in text and "GeneTraceAI, University" not in text and "Chennai" not in text
    second_first = fallback_letter({"company": "Acme", "title": "DS"}, "Acme builds things.", _ev("az1", "fl1"), [], "CTA?", [])
    assert second_first[1].startswith("In my freelance AI/ML work, I built scikit-learn")      # capitalised at a sentence start


def test_a_project_bullet_is_placed_in_its_project_not_at_an_employer():
    ev = [{"id": "p1", "text": "Built an MCP server.", "org": "mcp_searchagent", "project": True}, *_ev("az4")]
    paras = fallback_letter({"company": "Acme", "title": "DS"}, "Acme builds things.", ev, [], "CTA?", [])
    assert "in my mcp_searchagent project, I built an MCP server." in paras[0]
    assert paras[1].startswith("At AstraZeneca")


def test_a_missing_or_deny_listed_hook_becomes_the_role_sentence():
    ev = _ev("fl1")
    job = {"company": "Acme", "title": "Data Scientist"}
    assert fallback_letter(job, "", ev, [], "CTA?", [])[0].startswith("Acme's Data Scientist role sits close to the work")
    assert fallback_letter(job, "Acme runs Kubernetes at scale.", ev, [], "CTA?", ["kubernetes"])[0].startswith("Acme's Data")


# ---------------------------------------------------------------- 1. letter evidence: past-tense verb-led bullets only
@pytest.mark.parametrize("text, ok", [
    ("Built a tool.", True), ("Wrote a framework.", True), ("Designed a guard.", True), ("Deployed the agent.", True),
    ("Proposed and prototyped a tool.", True), ("Contributed to a team fine-tune.", True), ("Led a team of five.", True),
    ("XGBoost churn classifier in R.", False), ("LSTM forecaster with cyclic encoding.", False),
    ("Full-duplex voice agent.", False), ("Per-gene models for 11,459 genes.", False), ("Deterministic parser.", False),
    ("MCP server with stdio client.", False), ("Anomaly detection pipeline.", False), ("", False)])
def test_only_a_past_tense_verb_counts_as_the_start_of_a_letter_bullet(text, ok):
    assert starts_with_past_verb(text) is ok


def _bank():
    def b(i, text, terms):
        return {"id": i, "text": text, "terms": terms}
    return {"deny_terms": [], "skills": {}, "profile_facts": [], "experience": [
        {"id": "e1", "org": "Acme", "bullets": [b("v1", "Built a churn model in Python.", ["python"])]},
        {"id": "e2", "org": "Beta", "bullets": [b("v2", "Wrote the reporting pipeline in SQL.", ["sql"])]}],
        "projects": [{"id": "p1", "name": "Churn", "bullets": [
            b("n1", "XGBoost churn classifier in R with Bayesian hyperparameter search and python.", ["xgboost", "python", "r"]),
            b("n2", "LSTM forecaster in python with cyclic encoding.", ["lstm", "python"])]}]}


def test_noun_led_bullets_are_not_letter_evidence_even_when_they_match_the_job_best():
    kws = [Keyword("python", 2, True), Keyword("xgboost", 2, True), Keyword("lstm", 2, True), Keyword("sql", 1, False)]
    ev = tailor.letter_evidence(_bank(), kws, {"v1", "v2", "n1", "n2"})
    assert [e["id"] for e in ev] == ["v1", "v2"]
    assert all(starts_with_past_verb(e["text"]) for e in ev)


def test_when_the_cv_has_fewer_than_two_verb_led_bullets_the_banks_best_verb_led_ones_fill_up():
    kws = [Keyword("python", 2, True), Keyword("xgboost", 2, True)]
    ev = tailor.letter_evidence(_bank(), kws, {"v1", "n1", "n2"})            # one verb-led bullet on the CV
    assert [e["id"] for e in ev] == ["v1", "v2"]


# ---------------------------------------------------------------- 3. gap casing
@pytest.mark.parametrize("gap, jd, want", [
    ("azure", "We use Azure and AWS.", "Azure"), ("azure", "experience with azure", "Azure"),
    ("power bi", "Power BI reporting", "Power BI"), ("autogen", "frameworks such as AutoGen or CrewAI", "AutoGen"),
    ("dspy", "", "DSPy"), ("DSPy", "DSPy", "DSPy"), ("dbt", "", "dbt"), ("snowflake", "snowflake", "Snowflake"),
    ("quantum widgets", "We love Quantum Widgets", "Quantum Widgets"), ("quantum widgets", "", "quantum widgets")])
def test_a_gap_is_written_in_the_jds_casing_or_a_canonical_spelling(gap, jd, want):
    assert gap_display(gap, jd) == want


def test_the_model_is_given_and_the_letter_says_the_gap_in_its_casing(settings, spy_llm):
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": ["I have not used azure yet."]}})
    job = {"company": "Zone", "title": "AI Engineer", "description": "Azure experience wanted."}
    tailor._letter(job, {"deny_terms": []}, [], HOOK["Zone & Co"], _ev("az1", "dc1"), llm, settings, ["azure"], [], "", {})
    assert llm.sent[0]["context"]["gaps"] == ["Azure"] and "Azure" in llm.sent[0]["user"]


# ---------------------------------------------------------------- a model draft's grammar
@pytest.mark.parametrize("para, what", [
    ("My work on built scikit-learn pipelines mirrors that.", "my work on"),
    ("Zone mirrors my work a tool-calling LLM agent.", "mirrors my work"),
    ("Built scikit-learn pipelines and LSTM models in Docker.", "bare past-tense verb"),
    ("Proposed and prototyped an MCP-based agentic tool in Python.", "bare past-tense verb"),
    (TXT["fl2"], "pastes an evidence bullet verbatim")])
def test_a_model_draft_with_these_shapes_is_rejected(para, what):
    problems = grammar_problems([para], [TXT["fl1"], TXT["fl2"], TXT["dc1"]])
    assert any(what in p for p in problems), (para, problems)


@pytest.mark.parametrize("para", [
    "Zone mirrors my work on a tool-calling LLM agent.", "Chattermill's focus mirrors my work building NLP workflows.",
    "I built scikit-learn pipelines and deployed them as FastAPI services.", "At Acme, I wrote the reporting pipeline."])
def test_grammatical_first_person_prose_passes(para):
    assert grammar_problems([para], [TXT["fl1"], TXT["fl2"]]) == []


@pytest.mark.parametrize("company, para", [
    ("Creditspring", "Creditspring offers short-term products. My work on built scikit-learn pipelines mirrors that."),
    ("Zone & Co", "Zone & Co builds an ERP-native platform mirrors my work a tool-calling LLM agent.")])
def test_run_201304_drafts_with_broken_grammar_are_rejected_twice_and_the_template_letter_is_used(settings, spy_llm, company,
                                                                                                    para):
    a, b, gaps = CASES[company]
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [para, "I built scikit-learn pipelines in Docker."]}})
    job = {"company": company, "title": "X", "description": HOOK[company]}
    bank = {"deny_terms": [], "projects": [], "experience": [
        {"id": k, "org": o, "bullets": [{"id": i, "text": TXT[i], "terms": []} for i in TXT if i[:2] == k]} for k, o in ORG.items()]}
    paras, problems = tailor._letter(job, bank, [], HOOK[company], _ev(a, b), llm, settings, gaps, [], "", {})
    assert tailor.FALLBACK_MARK in problems and len(llm.sent) == 3
    assert paras[:-1] == SNAPSHOTS[company][:-1]


# ---------------------------------------------------------------- the hook and paragraph 2 use different bullets
EV2 = [{"id": "fl1", "text": TXT["fl1"], "terms": []}, {"id": "az1", "text": TXT["az1"], "terms": []}]
HOOK_FL1 = "Quantcast optimises ad auctions, and I built scikit-learn pipelines and LSTM models in TensorFlow Keras for that."
SECOND_FL1 = "I deployed scikit-learn pipelines and LSTM/BiRNN/CNN models as FastAPI services in Docker."
SECOND_AZ1 = "I built a tool-calling LLM agent with LangChain and AWS Bedrock for a multi-omics cancer research platform."


def test_one_bullet_told_in_both_the_hook_and_paragraph_2_is_a_problem():
    problems = repeated_evidence_problems([HOOK_FL1, SECOND_FL1], EV2)
    assert len(problems) == 1 and "same evidence bullet" in problems[0] and "scikit-learn" in problems[0]


def test_a_hook_and_paragraph_2_from_different_bullets_pass():
    assert repeated_evidence_problems([HOOK_FL1, SECOND_AZ1], EV2) == []
    assert repeated_evidence_problems([HOOK_FL1], EV2) == [] and repeated_evidence_problems([HOOK_FL1, SECOND_FL1], EV2[:1]) == []
    assert repeated_evidence_problems(["A hook naming no bullet at all.", SECOND_FL1], EV2) == []


def test_a_model_letter_that_repeats_a_bullet_is_retried_with_the_reason_and_then_accepted(settings, spy_llm):
    drafts = iter([[HOOK_FL1, SECOND_FL1], [HOOK_FL1, SECOND_AZ1]])
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": next(drafts)}})
    job = {"company": "Quantcast", "title": "X", "description": HOOK["Quantcast"] + " Optimises ad auctions."}
    paras, problems = tailor._letter(job, {"deny_terms": []}, [], HOOK["Quantcast"], _ev("fl1", "az1"), llm, settings, [], [], "", {})
    assert len(llm.sent) == 2 and "same evidence bullet" in llm.sent[1]["user"]
    assert not any("same evidence bullet" in p for p in problems) and paras[:2] == [HOOK_FL1, SECOND_AZ1]


def test_the_letter_prompt_tells_the_model_to_use_a_different_line_in_the_hook_and_paragraph_2():
    from careerops.tailoring import prompts
    assert "OTHER" in prompts.LETTER_SYSTEM and "never tell the same line twice" in prompts.LETTER_SYSTEM


def test_the_cover_letter_token_cap_leaves_room_for_reasoning(settings):
    assert settings["llm"]["max_tokens"]["cover_letter"] == 3000
    ctx = min(p["context_tokens"] for p in settings["llm"]["providers"] if p["name"] == "groq")
    assert ctx >= 3000 + 6000                                  # the cap plus a generous letter prompt fits the context


# ---------------------------------------------------------------- 4. CV filler (strings of run 201304)
RUN_201304 = [   # (what the model wrote, the source bullet)
    ("Built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain, and LangGraph, emphasizing "
     "retrieval capabilities.", TXT["fl2"]),
    ("Built an LSTM forecaster with cyclic date encoding and time-series cross-validated grid search, served via TensorFlow "
     "Serving REST API, showcasing model serving.",
     "LSTM forecaster with cyclic date encoding and time-series cross-validated grid search, served through a TensorFlow "
     "Serving REST API."),
    ("Built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, and deployed them as FastAPI services in "
     "Docker, demonstrating model deployment.", TXT["fl1"]),
    ("Built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services in Docker, "
     "demonstrating machine learning deployment.", TXT["fl1"]),
    ("Built a JavaScript web-scraping pipeline that automated manual data collection and surfaced market-trend signals for "
     "pricing decisions, forming a data pipeline.",
     "Built a JavaScript web-scraping pipeline that automated a manual collection process and surfaced market-trend signals "
     "used for pricing decisions."),
    ("Built per-gene protein-prediction models for 11,459 genes using OLS and a GPU Random Forest ensemble, training with "
     "resumable chunked GPU on a free T4, achieving regression.",
     "Per-gene protein-prediction models for 11,459 genes (OLS + GPU Random Forest ensemble), with resumable chunked GPU "
     "training on a free T4."),
    ("Deployed the agent as a Docker container on AWS Lambda using ECR pipelines, IAM roles, and least-privilege policies, "
     "demonstrating containerization and serverless production deployment.",
     "Deployed the agent as a Docker container on AWS Lambda (Lambda Web Adapter) with ECR image pipelines, IAM execution "
     "roles and least-privilege policies."),
]


@pytest.mark.parametrize("new, source", RUN_201304)
def test_a_trailing_keyword_carrying_participial_clause_is_rejected(new, source):
    assert any("trailing participial clause" in p for p in filler_problems(new, source)), new


@pytest.mark.parametrize("word", ["demonstrating", "showcasing", "emphasizing", "emphasising", "achieving", "forming",
                                  "highlighting", "leveraging"])
def test_every_listed_participle_is_caught(word):
    assert filler_problems(f"Built a pipeline in Python, {word} reliable delivery.", "Built a pipeline in Python.")


def test_a_participial_clause_the_source_already_has_or_a_mid_sentence_one_is_left_alone():
    src = "Built a pipeline in Python, achieving a 40% faster load."
    assert filler_problems("Built a data pipeline in Python, achieving a 40% faster load.", src) == []
    assert filler_problems("Built a pipeline demonstrating reuse across three teams in Python.",
                           "Built a pipeline in Python.") == []


def test_a_bare_keyword_attached_after_the_end_of_the_source_phrase_is_rejected():
    src = ("Per-gene protein-prediction models for 11,459 genes (OLS + GPU Random Forest ensemble), with resumable chunked GPU "
           "training on a free T4.")
    new = ("Designed per-gene protein-prediction models for 11,459 genes using OLS and a GPU Random Forest ensemble, training "
           "resumable chunked GPU models on a free T4 regression.")
    assert bare_keyword_problems(new, src, [Keyword("regression", 1, False)]) == [
        "bare keyword 'regression' tacked on after 'T4'"]
    assert bare_keyword_problems(new, src, [Keyword("classification", 1, False)]) == []              # not in the text


def test_a_keyword_worked_into_the_sentence_is_not_a_bare_attachment():
    src = "Built an XGBoost churn classifier in R with MCC evaluation."
    new = "Built an XGBoost churn classification model in R with MCC evaluation."
    assert bare_keyword_problems(new, src, [Keyword("classification", 1, False)]) == []


@pytest.mark.parametrize("new, source", RUN_201304[:1] + RUN_201304[5:6])
def test_the_guard_keeps_the_original_bullet_when_the_reword_carries_filler(new, source):
    bank = {"deny_terms": [], "skills": {}, "profile_facts": [], "projects": [], "experience": [
        {"id": "e", "org": "x", "bullets": [{"id": "b", "text": source,
                                             "terms": ["retrieval", "regression", "gpu", "nlp", "rag", "ols"]}]}]}
    kws = [Keyword("retrieval", 1, True), Keyword("regression", 1, True)]
    acc, viol, _ = tailor.guard_rephrases(bank, {"b": new}, kws)
    assert acc == {} and any("filler pattern" in v or "bare keyword" in v for v in viol)
