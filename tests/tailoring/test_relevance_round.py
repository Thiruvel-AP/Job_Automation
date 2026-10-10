"""Relevance round (review of the Monzo run): the letter's evidence is ranked over ALL bullets by JD keywords AND company
domain; the profile facts use the same ranking; a letter sentence that is >= 70% one evidence bullet's words counts as pasted;
a project's award already in its name is printed once; a hook that says the company's mission "mirrors / reflects / aligns
with" the candidate's work must share a domain term with it. Every change tightens or re-ranks: no check is loosened.
No model, no network; the bank below is fictional, shaped like the real one (same ids)."""
from pathlib import Path

import pytest

from careerops.config import load_yaml
from careerops.tailoring import tailor
from careerops.tailoring.budget import letter_scores
from careerops.tailoring.domain import domain_counts, domain_groups, domain_overlap, jd_domain
from careerops.tailoring.grounding import hook_link_problems
from careerops.tailoring.letter_rules import (PASTE_OVERLAP, clause, fallback_letter, grammar_problems, i_clause,
                                              paste_overlap)
from careerops.tailoring.render import award_shown, env
from careerops.tailoring.scorer import Keyword

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "tailoring"
MONZO = (FIX / "jd_monzo.txt").read_text(encoding="utf-8")       # the same text as data/jds/inbox/live-001.txt


def _b(i, text, terms):
    return {"id": i, "text": text, "terms": terms}


BANK = {"deny_terms": [], "skills": {}, "profile_facts": [], "experience": [
    {"id": "az", "title": "Data Scientist", "org": "Northfield Research Lab", "bullets": [
        _b("az1", "Built a tool-calling LLM agent (LangChain, AWS Bedrock) for a multi-omics cancer research platform, with "
                  "three tools for gene alias resolution and dataset provenance.",
           ["llm", "agent", "langchain", "bedrock", "aws", "tool calling", "python"])]},
    {"id": "dc180", "title": "Data Consultant", "org": "Student Consulting Society", "bullets": [
        _b("dc1", "Proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama, PostgreSQL) for a start-up "
                  "client, presented in a live stakeholder demo.",
           ["mcp", "agentic", "agent", "python", "client", "stakeholder", "communication"])]},
    {"id": "avasoft", "title": "Software Engineer", "org": "Brightwave Apps", "bullets": [
        _b("av1", "Integrated a market-data feed and a bank-account linkage API into a Flutter mobile app.",
           ["api", "apis", "fintech", "financial services", "integration", "flutter", "mobile"])]}],
    "projects": [
        {"id": "lloyds", "name": "Northern Banking Group Datathon — 3rd place", "award": "3rd place", "bullets": [
            _b("p_ll1", "Anomaly detection on 200:1 class-imbalanced data with Isolation Forest and ensemble classifiers, "
                        "tracked in MLflow.",
               ["anomaly detection", "imbalanced data", "classification", "mlflow", "financial services"])]},
        {"id": "churn", "name": "Banking Customer Churn", "bullets": [
            _b("p_ch1", "XGBoost churn classifier in R (tidymodels) with Bayesian hyperparameter search and MCC evaluation.",
               ["r", "xgboost", "classification", "churn", "propensity", "customer behaviour", "machine learning"])]}]}

# the Monzo JD's keywords (the model suggests; code scores)
MONZO_KWS = [Keyword("SQL", 2, True), Keyword("A/B testing", 2, True), Keyword("quantitative analysis", 2, True),
             Keyword("Python", 1, False), Keyword("Looker", 1, False)]
ON_CV = {"az1", "dc1", "av1", "p_ll1", "p_ch1"}


# ---------------------------------------------------------------- 0. domain vocabulary
def test_the_monzo_jd_is_about_banking_and_customers():
    w = jd_domain(MONZO)
    assert w["finance"] == 1.0 and w["customer"] == 1.0 and "health" not in w
    assert domain_counts("Banks, banking and a bank-account.") == {"finance": 3}      # plurals and hyphenated words count
    assert domain_groups("A tool-calling LLM agent for a multi-omics cancer research platform") == {"health"}
    assert domain_overlap("fintech payments", {"finance": 1.0, "customer": 1.0}) == 1.0     # one group counts once
    assert jd_domain("") == {} and domain_groups("investigate the data") == set()      # "investigate" is not "invest"


# ---------------------------------------------------------------- 1. letter evidence over all bullets, by keywords + domain
def test_the_monzo_letter_is_written_from_the_bank_customer_and_fintech_bullets_not_the_llm_agent_ones():
    ev = tailor.letter_evidence(BANK, MONZO_KWS, ON_CV, MONZO)
    ids = [e["id"] for e in ev]
    assert len(ids) == 2 and set(ids) <= {"p_ch1", "p_ll1", "av1"} and not set(ids) & {"az1", "dc1"}
    assert ids[0] == "p_ch1"                                   # banking AND customers: two of the JD's domain groups
    assert all(e["text"] and "terms" in e for e in ev)       # canonical bank text, unchanged


def test_without_the_jd_domain_the_python_agent_bullets_win_which_is_what_the_domain_ranking_fixes():
    ids = [e["id"] for e in tailor.letter_evidence(BANK, MONZO_KWS, ON_CV)]        # no jd: keywords only
    assert set(ids) == {"az1", "dc1"}
    sc = letter_scores(BANK, MONZO_KWS, MONZO)
    assert sc["p_ch1"] > sc["p_ll1"] >= sc["av1"] > max(sc["az1"], sc["dc1"])


def test_a_noun_led_bullet_is_ranked_with_the_rest_and_the_two_are_from_different_entries():
    bank = {**BANK, "projects": [{"id": "x", "name": "Banking Customer Churn", "bullets": [
        _b("x1", "Churn model for bank customers.", ["churn"]), _b("x2", "Bank customer segmentation with k-means.", [])]}]}
    ev = tailor.letter_evidence(bank, MONZO_KWS, {"x1", "x2", "av1", "az1"}, MONZO)
    assert len({e["id"][0] for e in ev}) == 2 and ev[0]["id"] in {"x1", "x2"} and ev[1]["id"] == "av1"


def test_the_letter_evidence_is_still_only_from_the_cv_and_may_be_short():
    assert {e["id"] for e in tailor.letter_evidence(BANK, MONZO_KWS, {"az1", "dc1"}, MONZO)} == {"az1", "dc1"}
    assert [e["id"] for e in tailor.letter_evidence(BANK, MONZO_KWS, {"p_ch1"}, MONZO)] == ["p_ch1"]


def test_a_noun_led_bullet_becomes_a_first_person_clause_without_claiming_more_than_it_says():
    assert i_clause("XGBoost churn classifier in R (tidymodels) with MCC evaluation.") == \
        "I worked on XGBoost churn classifier in R (tidymodels) with MCC evaluation"
    assert i_clause("Anomaly detection on 200:1 data.") == "I worked on anomaly detection on 200:1 data"
    assert i_clause("Built a tool-calling agent.") == "I built a tool-calling agent"
    assert clause("LSTM forecaster with cyclic encoding.") == "LSTM forecaster with cyclic encoding"      # acronyms keep case
    ev = [{"id": "p_ch1", "text": BANK["projects"][1]["bullets"][0]["text"], "org": "Banking Customer Churn", "project": True},
          {"id": "av1", "text": BANK["experience"][2]["bullets"][0]["text"], "org": "Brightwave Apps"}]
    paras = fallback_letter({"company": "Monzo", "title": "Data Scientist"}, "Monzo is a bank.", ev, [], "CTA?", [])
    assert "in my Banking Customer Churn project, I worked on XGBoost churn classifier" in paras[0]
    assert paras[1].startswith("At Brightwave Apps, I integrated a market-data feed")


# ---------------------------------------------------------------- 2. profile facts: same ranking, fixture profile
def test_the_fixture_profile_gets_the_two_facts_most_relevant_to_the_bank_jd(settings, spy_llm):
    fb = load_yaml(settings.path("fact_bank"))                      # tests/fixtures/profile: four profile_facts
    facts = fb["profile_facts"]
    assert len(facts) > 2
    llm = spy_llm({"tailor_profile": lambda ctx: {"facts": ["F1", "F2"], "profile": "Data Scientist with mobile FinTech work."}})
    job = {"title": "Data Scientist", "company": "Monzo", "description": MONZO}
    p = tailor.make_profile(job, fb, [Keyword("SQL", 2, True)], "Data Scientist", llm)
    fintech = next(f for f in facts if "FinTech" in f)
    assert fintech in p["facts"] and len(p["facts"]) == 2 and "F3:" not in llm.sent[0]["user"]
    assert [f["text"] for f in llm.sent[0]["context"]["facts"]] == p["facts"]       # the model sees exactly those two
    assert p["facts"] == [facts[i] for i in sorted(tailor.pick_facts(facts, [Keyword("SQL", 2, True)], [], 2, MONZO))]


def test_two_or_fewer_profile_facts_are_all_used():
    assert tailor.pick_facts(["a", "b"], [], [], 2, MONZO) == [0, 1] and tailor.pick_facts(["a"], [], [], 2, MONZO) == [0]


# ---------------------------------------------------------------- 3. the paste check: >= 70% of a sentence's words
FULL_DC1 = ("Proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, PostgreSQL, GitHub "
            "Actions CI) for a Madrid-based product start-up client, presented in a live stakeholder demo.")
FULL_AZ1 = ("Built a tool-calling LLM agent (LangChain, AWS Bedrock, Claude Haiku 4.5) for a multi-omics cancer research "
            "platform, with three tools for gene alias resolution, scoring-methodology explanation and dataset provenance.")
# the Monzo run's draft 3 (data/outputs/Monzo_Data_Scientist_*/llm_raw/cover_letter-3.json)
DRAFT3 = ["Monzo simplifies banking to make money work for everyone, a goal that mirrors my work on a tool-calling LLM agent "
          "for a multi-omics cancer research platform.",
          "I proposed and prototyped an MCP-based agentic tool in Python for a Madrid-based product start-up client, "
          "presented in a live stakeholder demo."]


def test_monzo_draft_3_paragraph_2_is_rejected_as_a_pasted_bullet():
    problems = grammar_problems(DRAFT3, [FULL_AZ1, FULL_DC1])
    assert any("pastes an evidence bullet (" in p and "proposed and prototyped" in p.lower() for p in problems), problems
    assert paste_overlap(DRAFT3[1], FULL_DC1) >= PASTE_OVERLAP
    assert not any("Monzo simplifies" in p for p in problems)             # the hook is the writer's own sentence


def test_a_paraphrase_under_the_threshold_passes_and_the_verbatim_message_is_kept_for_exact_pastes():
    own = ("My MCP experience comes from a start-up client project, where I proposed and prototyped an agentic tool in "
           "Python and gave a live stakeholder demo of it.")
    assert paste_overlap(own, FULL_DC1) < PASTE_OVERLAP and grammar_problems([own], [FULL_DC1]) == []
    exact = grammar_problems([FULL_AZ1], [FULL_AZ1])
    assert any("verbatim" in p for p in exact) and not any("% of its words" in p for p in exact)


def test_bracketed_text_and_a_leading_I_do_not_count_and_short_sentences_are_not_judged():
    bullet = "Wrote a three-tier test framework (tool-level, API, live agent) of about 65 tests across 8 files."
    assert paste_overlap("I wrote a three-tier test framework of about 65 tests across 8 files.", bullet) == 1.0
    assert paste_overlap("I used Python.", "Used Python daily for analysis.") == 0.0         # fewer than 5 words
    assert paste_overlap("Brackets (tool-level, API, live agent) alone do not matter here at all today.", bullet) < 0.5


def test_a_draft_that_pastes_a_bullet_is_retried_with_the_reason_and_the_third_paste_falls_back(settings, spy_llm):
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": DRAFT3}})
    ev = [{"id": "az1", "text": FULL_AZ1, "terms": []}, {"id": "dc1", "text": FULL_DC1, "terms": []}]
    job = {"company": "Monzo", "title": "Data Scientist", "description": MONZO}
    notes, retry = [], {}
    paras, problems = tailor._letter(job, {"deny_terms": []}, [], "Monzo is a digital bank that simplifies banking to make "
                                     "money work for everyone.", ev, llm, settings, [], notes, "", retry)
    assert len(llm.sent) == 3 and tailor.FALLBACK_MARK in problems and retry["outcome"] == "fallback"
    assert all(any("pastes an evidence bullet (" in p for p in f["problems"]) for f in retry["failures"])
    assert "pastes an evidence bullet" in llm.sent[1]["user"]


# ---------------------------------------------------------------- 4. an award already in the project's name is printed once
LLOYDS = {"name": "Lloyd's Banking Group Datathon — 3rd place ", "award": "3rd place ", "stack": "MLflow", "github": None}


@pytest.mark.parametrize("project, want", [
    (LLOYDS, ""), ({**LLOYDS, "name": "Lloyd's Banking Group Datathon"}, "3rd place"),
    ({**LLOYDS, "name": "DATATHON — 3RD  PLACE"}, ""),                         # case and spacing do not matter
    ({"name": "X", "award": ""}, ""), ({"name": "X"}, ""), ({"name": "X", "award": "Winner"}, "Winner")])
def test_award_shown_drops_an_award_the_name_already_says(project, want):
    assert award_shown(project) == want


@pytest.mark.parametrize("template", ["cv.tex.j2", "cv2.tex.j2"])
def test_the_lloyds_entry_prints_its_award_once_in_both_cv_templates(template):
    def tex(project):
        ctx = {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"}, "profile": "Data scientist.",
               "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst", "bullets": ["Built a report."]}],
               "projects": [{**project, "bullets": ["Anomaly detection."]}], "skills": {"Languages": ["Python"]},
               "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": []}
        return env().get_template(template).render(**ctx)

    assert tex(LLOYDS).count("3rd place") == 1                               # in the name, not again beside it
    other = tex({**LLOYDS, "name": "Lloyd's Banking Group Datathon"})
    assert other.count("3rd place") == 1 and r"\textit{3rd place}" in other   # an award the name lacks is still shown
    gh = tex({**LLOYDS, "github": "https://github.com/x/y"})
    assert gh.count("3rd place") == 1 and "GitHub" in gh                      # the freed slot shows the link


@pytest.mark.slow
def test_the_lloyds_entry_prints_its_award_once_in_the_pdf(tmp_path):
    from careerops.tailoring.render import compile_tex, pdf_text
    ctx = {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"}, "profile": "Data scientist.",
           "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst", "bullets": ["Built a report."]}],
           "projects": [{**LLOYDS, "bullets": ["Anomaly detection."]}], "skills": {"Languages": ["Python"]},
           "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": []}
    text = pdf_text(compile_tex(env().get_template("cv2.tex.j2").render(**ctx), tmp_path, "cv"))
    assert text.count("3rd place") == 1


# ---------------------------------------------------------------- 5. hook sanity: mirrors / reflects / aligns needs a shared domain term
EV_AZ1 = [{"id": "az1", "text": FULL_AZ1, "terms": ["llm", "agent"]}, {"id": "dc1", "text": FULL_DC1, "terms": []}]
EV_FIN = [{"id": "av1", "text": BANK["experience"][2]["bullets"][0]["text"], "terms": ["fintech", "financial services"]},
          EV_AZ1[0]]


@pytest.mark.parametrize("verb", ["mirrors", "reflects", "aligns with", "mirrored", "reflecting", "aligns"])
def test_a_bank_mission_linked_to_a_cancer_research_agent_is_rejected(verb):
    hook = f"Monzo simplifies banking to make money work for everyone, a goal that {verb} my work on a tool-calling LLM agent for a multi-omics cancer research platform."
    problems = hook_link_problems(hook, EV_AZ1)
    assert len(problems) == 1 and "share no domain term" in problems[0]
    assert "finance" in problems[0] and "health" in problems[0]


def test_monzo_draft_3_hook_is_rejected():
    problems = hook_link_problems(DRAFT3[0], EV_AZ1)
    assert problems and "share no domain term" in problems[0]


@pytest.mark.parametrize("hook, evidence", [
    # a shared domain term: the bank hook and the fintech bullet
    ("Monzo builds a bank for everyone, which mirrors my work integrating a bank-account linkage API into a mobile app.", EV_FIN),
    # the work half names the bank domain itself
    ("Monzo simplifies banking, which reflects my work on churn for a banking customer base.", EV_AZ1),
    # no link verb: nothing to check
    ("Monzo simplifies banking, and I built a tool-calling LLM agent for a cancer research platform.", EV_AZ1),
    # the company half names no domain word, so there is nothing to compare
    ("Acme turns raw data into decisions, which mirrors my work on an agent for a cancer research platform.", EV_AZ1),
    # the work half names no domain word (Docker): not judged here
    ("Monzo simplifies banking, which mirrors my work on the agentic tool.", [EV_AZ1[1]]),
    # "matches" is not one of the three verbs
    ("Monzo simplifies banking, which matches my work on a cancer research platform.", EV_AZ1)])
def test_hooks_with_a_shared_domain_or_nothing_to_compare_pass(hook, evidence):
    assert hook_link_problems(hook, evidence) == []


def test_the_link_is_judged_against_the_bullet_the_claim_is_written_from_not_any_bullet():
    hook = ("Monzo simplifies banking, which mirrors my work on a tool-calling LLM agent for a multi-omics cancer research "
            "platform.")
    assert hook_link_problems(hook, EV_FIN)             # av1 is fintech, but the claim is about az1: still no shared term


def test_monzo_draft_3_fails_the_hook_check_inside_the_letter_and_the_retry_is_told_why(settings, spy_llm):
    own2 = ("My MCP experience comes from a start-up client project, where I proposed and prototyped an agentic tool in "
            "Python and gave a live stakeholder demo of it.")
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [DRAFT3[0], own2]}})
    ev = [{"id": "az1", "text": FULL_AZ1, "terms": []}, {"id": "dc1", "text": FULL_DC1, "terms": []}]
    job = {"company": "Monzo", "title": "Data Scientist", "description": MONZO}
    retry: dict = {}
    paras, problems = tailor._letter(job, {"deny_terms": []}, [], "Monzo simplifies banking to make money work for everyone.",
                                     ev, llm, settings, [], [], "", retry)
    assert any("share no domain term" in p for p in retry["failures"][0]["problems"])
    assert "share no domain term" in llm.sent[1]["user"] and tailor.FALLBACK_MARK in problems
