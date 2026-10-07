"""Guard round from the review of calibration run 20261006-190956. Every change tightens: qualifiers survive a reword, the
letter's opening claim must come from the evidence, no "team team", no filler insertions, skills with commas show "/"."""
import pytest

from careerops.tailoring import tailor
from careerops.tailoring.grounding import FILLER_WORDS, opening_claim, opening_claim_problems, rejected_words
from careerops.tailoring.render import skill_display
from careerops.tailoring.rewrite_rules import canonical_case, canonical_forms, filler_problems, qualifier_problems
from careerops.tailoring.scorer import Keyword

# ---------------------------------------------------------------- the bank's bullets and the three runs' evidence
FL3 = {"id": "fl3", "text": "Contributed to a team LoRA fine-tune of an instruction-tuned Llama model for a maths-focused task.",
       "terms": ["fine-tuning", "lora", "peft", "parameter-efficient fine-tuning", "task-specific fine-tuning", "llama", "llm"]}
FL2 = {"id": "fl2", "text": "Built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain and LangGraph.",
       "terms": ["nlp", "rag", "retrieval", "llm", "langchain", "langgraph", "hugging face", "transformers", "agentic"]}
P_CH1 = {"id": "p_ch1", "text": "XGBoost churn classifier in R (tidymodels) with two-stage Bayesian hyperparameter search, SMOTE "
                                "inside the recipe and MCC evaluation.",
         "terms": ["r", "xgboost", "classification", "churn", "propensity", "hyperparameter tuning", "customer behaviour",
                   "classical ML", "machine learning"]}
FL1 = {"id": "fl1", "text": "Built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI "
                            "services in Docker.",
       "terms": ["machine learning", "deep learning", "scikit-learn", "tensorflow", "keras", "fastapi", "docker", "deployment",
                 "model deployment", "python"]}
DC1 = {"id": "dc1", "text": "Proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, "
                            "GitHub Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.",
       "terms": ["mcp", "agentic", "agent", "python", "prototype", "stakeholder", "client", "consulting", "ci", "postgresql",
                 "open-source llm", "open-weight", "communication", "presentation", "stakeholder communication"]}
AZ4 = {"id": "az4", "text": "Wrote a three-tier test framework (tool-level, API/integration, live agent) of ~65 tests across 8 "
                            "files, validated across the LLM provider migration from Groq to AWS Bedrock after a rate-limit "
                            "evaluation.",
       "terms": ["testing", "evaluation", "pytest", "integration testing", "llm evaluation", "quality"]}


def _bank(*bullets):
    return {"deny_terms": [], "experience": [{"id": "e", "org": "Acme", "bullets": list(bullets)}], "projects": [],
            "skills": {"Tools": ["XGBoost", "MLflow", "PyTorch"]}, "profile_facts": []}


def _ev(*bullets):
    return [{"id": b["id"], "text": b["text"], "terms": b["terms"]} for b in bullets]


# ---------------------------------------------------------------- 1. qualifiers
def test_the_fl3_reword_that_drops_contributed_and_team_is_rejected_and_the_original_kept():
    new = "Fine-tuned a LoRA adapter on an instruction-tuned Llama model for a maths-focused task."
    kws = [Keyword("fine-tuning", 1, True), Keyword("lora", 1, True)]
    acc, viol, _ = tailor.guard_rephrases(_bank(FL3), {"fl3": new}, kws)
    assert acc == {}                                                          # the canonical bullet stays
    assert "fl3: dropped the qualifier 'contributed'" in viol and "fl3: dropped the qualifier 'team'" in viol


def test_a_reword_that_keeps_the_qualifiers_passes_them():
    new = "Contributed to a team LoRA fine-tune of an instruction-tuned Llama model for a maths-focused LLM task."
    assert qualifier_problems(new, FL3["text"]) == []
    assert qualifier_problems("Contributing to a team LoRA fine-tune.", FL3["text"]) == []        # a form of 'contributed'


@pytest.mark.parametrize("source, kept, dropped", [
    ("MSc coursework project on churn.", "An MSc coursework project on churn.", "An MSc project on churn."),
    ("Prototyped a tool for a client.", "Prototype of a tool for a client.", "Built a tool for a client."),
    ("Delivered a proof of concept for a client.", "Delivered a PoC for a client.", "Delivered a demo for a client."),
    ("Won a hackathon with a RAG app.", "Hackathon-winning RAG app.", "Won a prize with a RAG app."),
    ("Assisted the data team with SQL.", "Assisting the data team with SQL.", "Wrote SQL for the data team."),
    ("Supported the analytics team.", "Supporting the analytics team.", "Ran the analytics team."),
    ("Helped migrate the pipeline.", "Helped migrate the pipeline.", "Migrated the pipeline.")])
def test_every_listed_qualifier_must_survive(source, kept, dropped):
    assert qualifier_problems(kept, source) == []
    assert qualifier_problems(dropped, source), (source, dropped)


def test_a_source_without_a_qualifier_asks_nothing_of_the_reword():
    assert qualifier_problems("Wrote the whole thing.", "Built a forecasting service in Python.") == []


def test_the_dc1_prototyping_rewrite_the_wording_pass_makes_keeps_its_qualifier():
    assert qualifier_problems("Prototyping an MCP-based agentic tool in Python for a start-up client.", DC1["text"]) == []


# ---------------------------------------------------------------- 4. keyword insertion style
@pytest.mark.parametrize("new", [
    "Built NLP workflows, incorporating llm and retrieval capabilities.",
    "Contributed to a LoRA fine-tune, applying llm techniques.",
    "Built agents, leveraging advanced agentic capabilities.",
    "Wrote a framework using building with pytest.",
    "Presented the MCP tool in a live communication demo.",
])
def test_filler_patterns_a_reword_adds_are_rejected(new):
    assert filler_problems(new, "Built a thing.")


def test_a_filler_pattern_the_source_already_has_is_not_added_by_the_reword():
    src = "Gave a live communication demo of the tool."
    assert filler_problems("Gave a live communication demo of the MCP tool.", src) == []


def test_filler_in_a_reword_is_a_guard_violation_so_the_canonical_bullet_stays():
    kws = [Keyword("llm", 1, True), Keyword("retrieval", 1, True)]
    new = "Built NLP and agentic RAG workflows with LangChain and LangGraph, incorporating llm and retrieval capabilities."
    acc, viol, _ = tailor.guard_rephrases(_bank(FL2), {"fl2": new}, kws)
    assert acc == {} and any("filler pattern" in v for v in viol)


def test_an_inserted_keyword_takes_its_canonical_casing():
    forms = canonical_forms(_bank(FL2))
    assert canonical_case("Built llm-powered nlp and rag workflows on aws with genai and xgboost.", forms) == \
        "Built LLM-powered NLP and RAG workflows on AWS with GenAI and XGBoost."
    assert canonical_case("Used mlflow, pytorch and scikit-learn.", forms) == "Used MLflow, PyTorch and scikit-learn."
    assert canonical_case("Built a model of a ragtime band.", forms) == "Built a model of a ragtime band."      # whole words


def test_an_accepted_reword_is_written_in_canonical_casing():
    kws = [Keyword("llm", 1, True)]
    new = "Built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain and LangGraph for an llm app."
    acc, _, _ = tailor.guard_rephrases(_bank(FL2), {"fl2": new}, kws)
    assert acc == {"fl2": new.replace(" llm ", " LLM ")}


# ---------------------------------------------------------------- 3. team team
@pytest.mark.parametrize("raw, want", [("Data Science team", "Data Science"), ("Modeling team", "Modeling"),
                                       ("Modeling Team.", "Modeling"), ("Data Science", "Data Science"), ("team", ""),
                                       ("Teamwork platform", "Teamwork platform")])
def test_a_trailing_team_is_stripped_from_the_team_name(raw, want):
    assert tailor.strip_team_word(raw) == want


def test_the_greeting_and_the_closing_never_say_team_team(settings):
    for team in ("Data Science team", "Data Science", "Modeling team"):
        g, c = tailor.letter_greeting(settings, "Chattermill", team), tailor.letter_closing(settings, "Chattermill", team)
        assert "team team" not in g.lower() and "team team" not in c.lower()
        assert g.lower().count("team") == 1 and c.lower().count("team") == 1
    assert tailor.letter_greeting(settings, "Chattermill", "Data Science team") == "Hi Chattermill Data Science team"
    assert tailor.letter_closing(settings, "Quantcast", "Modeling team").endswith("on the Modeling team at Quantcast?")


def test_the_team_the_model_names_is_cleaned_when_it_is_read(job):
    jd = {**job, "company": "Chattermill", "description": "Join the Data Science team at Chattermill."}
    assert tailor.clean_team("Data Science team", jd) == "Data Science"


# ---------------------------------------------------------------- 5. skills with commas
def test_a_skill_entry_with_commas_renders_with_slashes():
    assert skill_display("S3, SQS, DynamoDB (MSc coursework)") == "S3/SQS/DynamoDB (MSc coursework)"
    assert skill_display("Python (pandas, numpy)") == "Python (pandas, numpy)"          # commas inside brackets stay
    assert skill_display("Docker") == "Docker" and skill_display("TensorFlow / Keras") == "TensorFlow / Keras"


def test_the_cv_shows_the_comma_skill_with_slashes_in_the_rendered_pdf(fb_fixture, settings, tmp_path):
    from careerops.tailoring.render import pdf_text, render_cv
    fb = dict(fb_fixture)
    ctx = tailor._context(fb, {"experience": {}, "projects": []}, {}, "Profile.", [], "AI Engineer",
                          {"Cloud": ["AWS Lambda", "S3, SQS, DynamoDB (MSc coursework)", "Docker"]})
    text = pdf_text(render_cv(ctx, tmp_path, "cv"))
    assert "S3/SQS/DynamoDB (MSc coursework)" in text and "S3, SQS" not in text


# ---------------------------------------------------------------- 2. the letter's opening claim (run 190956's three hooks)
CHATTERMILL = ("Chattermill's mission to turn every customer touchpoint into actionable insight mirrors my work building NLP "
               "pipelines that surface hidden sentiment from unstructured data.")
LENDABLE = ("Lendable's ambition to deliver instant credit and savings solutions mirrors my recent work building an agentic tool "
            "that instantly adapts to new LLM providers, ensuring customers receive timely, accurate responses.")
QUANTCAST = ("Optimising real-time ad auctions demands models that learn quickly and deploy instantly — that's exactly what "
             "I built with scikit-learn pipelines and LSTM, BiRNN, CNN architectures in TensorFlow/Keras, wrapped as FastAPI "
             "services in Docker.")
HOOKS = {
    "Chattermill": "Chattermill builds a Customer Experience Intelligence platform that continuously analyses explicit and "
                   "implicit customer feedback for large brands such as Uber, Amazon, Wise, and HelloFresh.",
    "Lendable": "Lendable builds fintech technology to give customers instant credit and savings solutions, rebuilding core "
                "consumer finance products and expanding into UK and US markets.",
    "Quantcast": "Quantcast builds AI-powered advertising technology that optimizes digital ad auctions in real time, "
                 "delivering measurable outcomes for marketers worldwide."}
EVIDENCE = {"Chattermill": _ev(FL2, P_CH1), "Lendable": _ev(DC1, AZ4), "Quantcast": _ev(FL1, P_CH1)}
REST = {   # the evidence paragraphs of each run's letter, each worded from its own bullet only (they passed in 190956)
    "Chattermill": ["I built NLP and agentic RAG workflows with Hugging Face Transformers, Ollama, LangChain and LangGraph.",
                    "I built an XGBoost churn classifier in R (tidymodels) with two-stage Bayesian hyperparameter search, "
                    "SMOTE inside the recipe and MCC evaluation."],
    "Lendable": ["I proposed and prototyped an MCP-based agentic tool in Python using FastMCP, Ollama gpt-oss:20b, PostgreSQL "
                 "and GitHub Actions CI for a Madrid-based product start-up client, and presented it in a live stakeholder "
                 "demo.",
                 "I wrote a three-tier test framework of about 65 tests across eight files, validating the LLM provider "
                 "migration from Groq to AWS Bedrock after a rate-limit evaluation."],
    "Quantcast": ["I built scikit-learn pipelines and LSTM/BiRNN/CNN models in TensorFlow/Keras, deployed as FastAPI services "
                  "in Docker.",
                  "I built an XGBoost churn classifier in R (tidymodels) with two-stage Bayesian hyperparameter search, "
                  "SMOTE inside the recipe and MCC evaluation."]}
OPENING = {"Chattermill": CHATTERMILL, "Lendable": LENDABLE, "Quantcast": QUANTCAST}
JOBS = {"Chattermill": "Data Scientist", "Lendable": "AI Engineer", "Quantcast": "Machine Learning Engineer"}


def _run_letter(settings, spy_llm, name, body, team=""):
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": body}})
    job = {"company": name, "title": JOBS[name], "description": HOOKS[name] + " " + EVIDENCE[name][0]["text"]}
    notes, retry = [], {}
    paras, problems = tailor._letter(job, {"deny_terms": []}, [], HOOKS[name], EVIDENCE[name], llm, settings, [], notes, team,
                                     retry)
    return paras, problems, notes, retry, llm


def test_the_claim_half_starts_after_the_company_clause():
    assert opening_claim(CHATTERMILL).startswith("my work building NLP pipelines")
    assert opening_claim(QUANTCAST).startswith("that's exactly what I built with")
    assert opening_claim("Acme is hiring and my work fits.") == ""                       # no turn: nothing to check here


def test_chattermills_claim_names_capabilities_the_two_bullets_do_not_have():
    problems = opening_claim_problems(CHATTERMILL, EVIDENCE["Chattermill"])
    named = rejected_words(problems)
    assert "pipelines" in named and any("sentiment" in p for p in problems) and any("unstructured" in p for p in problems)
    assert not any("'NLP'" in p for p in problems)                                           # NLP is in fl2


def test_lendables_claim_is_rejected_for_instantly_customers_and_accurate_responses():
    problems = opening_claim_problems(LENDABLE, EVIDENCE["Lendable"])
    text = " ".join(problems)
    for word in ("customers", "timely", "accurate", "responses"):
        assert f"'{word}'" in text, word
    assert "'agentic'" not in text and "'providers'" not in text                             # both are in the bullets


def test_quantcasts_claim_is_rejected_for_architectures_and_wrapped():
    text = " ".join(opening_claim_problems(QUANTCAST, EVIDENCE["Quantcast"]))
    assert "'architectures'" in text and "'wrapped'" in text


def test_a_claim_made_only_of_evidence_words_passes():
    ok = ("Chattermill's mission to understand customer feedback mirrors my work building NLP and agentic RAG workflows "
          "with LangChain and LangGraph.")
    assert opening_claim_problems(ok, EVIDENCE["Chattermill"]) == []


def test_the_new_vague_phrases_are_listed_and_matched_in_either_number():
    for phrase in ("actionable insights", "user-centric", "tangible outcomes", "instantly", "accurately", "precise",
                   "smooth integration", "efficient", "exactly what I built"):
        assert phrase in FILLER_WORDS
        problems = tailor._style_problems([f"We turn data into {phrase} for users.", "Could we talk?"], [FL2["text"]])
        assert any(f"vague filler '{phrase}'" in p for p in problems), phrase
    assert any("actionable insights" in p for p in tailor._style_problems(["It gives actionable insight.", "Talk?"], ["x"]))
    assert not tailor._style_problems(["It gives precise results.", "Talk?"], ["Reported precise results."])   # evidence says it


@pytest.mark.parametrize("name", ["Chattermill", "Lendable", "Quantcast"])
def test_each_run_190956_hook_is_rejected_and_the_retry_names_the_phrase(settings, spy_llm, name):
    paras, problems, notes, retry, llm = _run_letter(settings, spy_llm, name, [OPENING[name], *REST[name]])
    assert retry["count"] == 1 and retry["rejected_words"]                                  # rejected, retried once, named
    assert "Do NOT use them" in llm.sent[1]["user"] and retry["rejected_words"][0] in llm.sent[1]["user"]
    assert len(llm.sent) == 2 and retry["outcome"] == "fallback" and tailor.FALLBACK_MARK in problems   # template letter


def test_a_hook_that_passes_is_left_alone_and_costs_no_retry(settings, spy_llm):
    good = ("Chattermill's mission to understand customer feedback mirrors my work building NLP and agentic RAG workflows "
            "with LangChain and LangGraph.")
    paras, problems, _, retry, llm = _run_letter(settings, spy_llm, "Chattermill", [good, *REST["Chattermill"]])
    assert problems == [] and retry["count"] == 0 and paras[0] == good and len(llm.sent) == 1
