"""v3.5.3 (10 Nov 2026, fix 5): the letter is the user's `letter_template` for the family, written once in the fact bank; the model fills only
{company_problem_sentence}, {why_sentence} and {question} ({company} is code), each slot checked, and the whole letter goes through the fact check.
A family with no template gets the older letter, marked DRAFT (approval needs [e]). Fictional bank (tests/fixtures/tailoring/fact_bank_fixture.yaml)."""
import copy
from pathlib import Path

import pytest

from careerops.llm.mock import MockLLM
from careerops.review import workflow
from careerops.tailoring import company_letter as CL
from careerops.tailoring import doccheck
from careerops.tailoring import template_letter as TL
from careerops.tailoring.scorer import Keyword

FIXED_P1 = ("For my MSc dissertation at Northwind Research Lab I built a tool-calling LLM agent with LangChain on AWS Bedrock for a multi-omics "
            "cancer research platform, deployed it as a Docker container on AWS Lambda and checked it with a three-tier framework of ~65 tests across 8 files.")
FIXED_P3 = ("I care about the parts that keep an agent dependable: a deterministic guard architecture, a local 19,213-gene parquet fallback behind "
            "circuit breakers, and answers that stay reproducible during upstream outages. I also surveyed and explored 14 heterogeneous source "
            "datasets and set metadata boundaries for downstream analysis. At Bristol Pro Bono Consulting I cleaned and joined donor, event and survey "
            "exports with pandas and SQL, delivered a reporting notebook that the trustees reused, and presented segmentation findings to "
            "non-technical stakeholders. At Harbour Pay I built payment-flow screens in Swift for a FinTech iOS app and fixed the top three crash "
            "causes across two releases.")
TEMPLATE = f"""Hi {{company}} team,

{{company_problem_sentence}} {FIXED_P1}

{FIXED_P3}

{{why_sentence}}

Could we spend 20 minutes on {{question}}?

Regards,
Ada Lovelace
"""
AD = (Path(__file__).parents[1] / "fixtures" / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")
KWS = [Keyword("llm", 3.0, True), Keyword("aws", 2.0, True), Keyword("docker", 2.0, True), Keyword("python", 2.0, True)]


def bank(fb_fixture, template: str = TEMPLATE, where: str = "top") -> dict:
    fb = copy.deepcopy(fb_fixture)
    if where == "top":
        fb["letter_templates"] = {"ai_engineer": template}
    else:
        fb["families"] = [{"id": "ai_engineer", "title": "AI Engineer", "letter_template": template}]
    return fb


def job() -> dict:
    return {"id": "manual:tpl353", "company": "Northwind Health AI", "title": "AI Engineer", "description": AD}


# ------------------------------------------------------------------ the template itself
def test_a_template_parses_into_greeting_named_paragraphs_and_sign_off():
    t = TL.parse("ai_engineer", TEMPLATE)
    assert t.errors == [] and t.greeting == "Hi {company} team" and (t.signoff, t.name) == ("Regards", "Ada Lovelace")
    assert t.names == ["P1", "P3", "P5", "P7"] and len(t.paragraphs) == 4
    five = TEMPLATE.replace(FIXED_P3, FIXED_P3 + "\n\nAt Harbour Pay I integrated REST APIs.")
    assert TL.parse("x", five).names == ["P1", "P3", "P4", "P5", "P7"]


@pytest.mark.parametrize("change,error", [
    (("{question}", "the next step"), "{question} must appear exactly once"),
    (("{why_sentence}", "{why_sentence} {mood}"), "unknown slot(s) {mood}"),
    (("Hi {company} team,\n", "Hi {company} team,\n\nThank you for reading.\n"), "{company_problem_sentence} must be in the first body paragraph"),
    (("Hi {company} team,\n", "Thank you for reading.\n"), "no greeting line"),
    (("Regards,\nAda Lovelace", "Ada"), "no sign-off"),
])
def test_a_broken_template_is_refused_with_the_reason(change, error):
    t = TL.parse("x", TEMPLATE.replace(*change))
    assert any(error in e for e in t.errors), t.errors


def test_the_template_is_found_for_the_role_family_first_then_the_cvs_bank_family(fb_fixture):
    fb = bank(fb_fixture)
    assert TL.find(fb, ["forward_deployed_engineer", "ai_engineer"]).family == "ai_engineer"
    fb["letter_templates"]["forward_deployed_engineer"] = TEMPLATE.replace("I care about", "I value")
    assert TL.find(fb, ["forward_deployed_engineer", "ai_engineer"]).family == "forward_deployed_engineer"
    assert TL.find(bank(fb_fixture, where="family"), ["ai_engineer"]).family == "ai_engineer"     # families[*].letter_template, as the user wrote it
    assert TL.find(fb_fixture, ["ai_engineer"]) is None


# ------------------------------------------------------------------ the slot checks
SENT = "Northwind Health AI builds clinical decision-support tools that help oncology researchers find the right cancer cell-line models faster, using large language models over multi-omics data."


def test_the_company_problem_sentence_is_an_ad_sentence_lightly_shortened():
    ad = CL.ad_sentences(AD)
    assert TL.problem_sentence_problems(SENT, ad, "Northwind Health AI") == []
    short = "Northwind Health AI builds clinical decision-support tools that help oncology researchers find the right cancer cell-line models faster."
    assert TL.problem_sentence_problems(short, ad, "Northwind Health AI") == []                 # words dropped from the end
    we = "Northwind Health AI are hiring an AI Engineer to build and evaluate LLM agents that sit on top of Northwind Health AI's research data platform."
    assert TL.problem_sentence_problems(we, ad, "Northwind Health AI") == []                    # "we" / "our" as the company's name
    added = SENT.replace("faster", "much faster and more safely")
    assert any("not a sentence of the ad" in p for p in TL.problem_sentence_problems(added, ad, "Northwind Health AI"))
    cut = "Northwind Health AI builds tools for oncology researchers."                              # too little of the sentence kept
    assert any("not a sentence of the ad" in p for p in TL.problem_sentence_problems(cut, ad, "Northwind Health AI"))
    you = "You will work end-to-end, from prototyping to production deployment on AWS, alongside scientists and product stakeholders."
    assert any("'You'" in p for p in TL.problem_sentence_problems(you, ad, "Northwind Health AI"))


def test_the_question_uses_the_ads_words_only():
    assert TL.question_problems("the first evaluation suites the AI Engineer would write for LLM agents", AD, "Northwind Health AI", "AI Engineer") == []
    assert any("names 'Snowflake'" in p for p in TL.question_problems("moving the agents to Snowflake", AD, "Northwind Health AI", "AI Engineer"))
    assert any("number" in p for p in TL.question_problems("the 40 agents in production", AD, "Northwind Health AI", "AI Engineer"))
    assert any("speaks about you" in p for p in TL.question_problems("what I built on AWS", AD, "Northwind Health AI", "AI Engineer"))


def test_the_fact_check_reads_the_template_text_against_the_bank_and_the_slots_against_the_ad(fb_fixture):
    t = TL.parse("ai_engineer", TEMPLATE)
    values = {"company_problem_sentence": SENT, "why_sentence": "I want to build LLM agents with tool calling using LangChain or LangGraph.",
              "question": "the first evaluation suites the AI Engineer would write"}
    paragraphs = TL.fill(t, "Northwind Health AI", values)
    assert TL.fact_problems(paragraphs, values, fb_fixture, KWS, AD, "Northwind Health AI", "AI Engineer") == []   # "Could we spend 20 minutes": the ask
    wrong = TL.fill(TL.parse("x", TEMPLATE.replace("~65 tests", "~90 tests").replace("Harbour Pay I", "Snowflake I")), "Northwind Health AI", values)
    found = TL.fact_problems(wrong, values, fb_fixture, KWS, AD, "Northwind Health AI", "AI Engineer")
    assert any("new numbers ['90']" in p for p in found) and any("names 'Snowflake'" in p for p in found)
    bad = {**values, "question": "the 40 agents the AI Engineer would own"}
    found = TL.fact_problems(TL.fill(t, "Northwind Health AI", bad), bad, fb_fixture, KWS, AD, "Northwind Health AI", "AI Engineer")
    assert any(p.startswith("question gives the number(s) 40") for p in found)


# ------------------------------------------------------------------ the letter (xelatex)
def make(fb, settings, tmp_path, llm):
    t = TL.find(fb, ["ai_engineer"])
    return TL.make(t, job(), fb, KWS, llm, settings, tmp_path, "Ada Lovelace Cover Letter", "", {"headline": "AI Engineer", "number": ""},
                   "LLM agents and AWS")


@pytest.mark.slow
def test_the_model_fills_only_the_slots_and_the_template_text_is_kept_word_for_word(fb_fixture, settings, tmp_path):
    why = "Building and evaluating LLM agents that sit on top of the research data platform is why I applied."
    question = "the first evaluation suites the AI Engineer would write for LLM agents in production"

    def answer(ctx):
        assert ctx["mode"] == "template_slots" and set(ctx["slots"]) <= set(TL.SLOTS)
        return {"company_problem_sentence": SENT, "why_sentence": why, "question": question}

    llm = MockLLM({"cover_letter": answer})
    pdf, letter = make(bank(fb_fixture), settings, tmp_path, llm)
    assert letter["status"] == "ok" and letter["compose"] == "template" and letter["template_family"] == "ai_engineer"
    assert llm.calls.count("cover_letter") == 1 and letter["slot_source"] == {k: "model" for k in TL.SLOTS}
    assert letter["names"] == ["P1", "P3", "P5", "P7"]
    assert letter["paragraphs"] == [f"{SENT} {FIXED_P1}", FIXED_P3, why, f"Could we spend 20 minutes on {question}?"]
    assert letter["draft"] is False and letter["template_problems"] == [] and 200 <= letter["words"] <= 320, letter["template_problems"]
    text = (tmp_path / "letter.txt").read_text(encoding="utf-8")
    assert text.startswith("Hi Northwind Health AI team,") and text.rstrip().endswith("Regards,\n\nAda Lovelace")
    assert pdf is not None and pdf.exists()


@pytest.mark.slow
def test_a_slot_the_model_gets_wrong_three_times_is_filled_in_code_never_with_the_models_words(fb_fixture, settings, tmp_path):
    def answer(ctx):
        return {"company_problem_sentence": "Northwind Health AI is the best employer in the world.",
                "why_sentence": "I led 12 agent teams at Northwind Health AI, which is why I applied.", "question": "what I shipped at Snowflake"}

    llm = MockLLM({"cover_letter": answer})
    _, letter = make(bank(fb_fixture), settings, tmp_path, llm)
    assert llm.calls.count("cover_letter") == TL.TRIES and letter["slot_source"] == {k: "code" for k in TL.SLOTS}
    body = " ".join(letter["paragraphs"])
    assert "best employer" not in body and "12 agent teams" not in body and "Snowflake" not in body
    assert letter["slots"]["company_problem_sentence"] == SENT                    # the ad's own About sentence
    assert letter["slots"]["question"] == "the first problem the AI Engineer would own, around LLM agents and AWS"
    assert any("filled in code after 3 rejected" in n for n in letter["notes"])


@pytest.mark.slow
def test_the_mock_run_fills_the_problem_slot_from_the_ad(fb_fixture, settings, tmp_path):
    _, letter = make(bank(fb_fixture), settings, tmp_path, MockLLM())
    assert letter["status"] == "ok" and letter["slot_source"]["company_problem_sentence"] == "model"
    assert letter["slots"]["company_problem_sentence"] == SENT


@pytest.mark.slow
def test_a_template_claim_the_bank_cannot_back_makes_the_letter_a_draft(fb_fixture, settings, tmp_path):
    fb = bank(fb_fixture, TEMPLATE.replace("~65 tests", "~90 tests"))
    _, letter = make(fb, settings, tmp_path, MockLLM())
    assert letter["status"] == "ok" and letter["draft"] is True and any("90" in p for p in letter["template_problems"])
    rep = {"letter": letter, "context": {"id": {}}, "keywords": [], "company": "Northwind Health AI", "title": "AI Engineer"}
    problems = doccheck.letter_problems(rep, settings, None, fb, (AD,), [])
    assert problems[0].startswith("LETTER is a DRAFT (the fact check failed") and any("90" in p for p in problems[1:])
    lines = workflow.company_letter_lines(letter)
    assert lines[0].startswith("!! LETTER DRAFT (needs [e])") and lines[1].startswith("Letter (template 'ai_engineer')")
    assert any(l.startswith("  {company_problem_sentence} (model): ") for l in lines)


# ------------------------------------------------------------------ no template: the older letter, marked DRAFT
def test_a_family_without_a_template_gets_the_older_letter_marked_draft(monkeypatch, fb_fixture, settings):
    seen = {}

    def free(*a, **k):
        seen["called"] = True
        return None, CL._letter_dict("ok", paragraphs=["x"], names=["P1"])

    monkeypatch.setattr(CL, "_make_free", free)
    _, letter = CL.make(job(), fb_fixture, KWS, None, "", [], MockLLM(), None, Path("."), "L", "", [], families=["data_scientist"])
    assert seen["called"] and letter["draft"] is True and "no letter_template for the family data_scientist" in letter["draft_reason"]
    rep = {"letter": {**letter, "mode": "company", "names": ["P1"], "paragraphs": ["x"]}, "context": {"id": {}}, "keywords": []}
    assert doccheck.letter_problems(rep, settings, None, None, (), [])[0].startswith("LETTER is a DRAFT (no letter_template")


def test_an_edited_template_letter_keeps_its_own_paragraphs():
    assert CL.required_names({"compose": "template", "names": ["P1", "P3", "P5", "P7"]}) == ("P1", "P3", "P5", "P7")
    assert CL.required_names({"names": ["P1", "P2", "P3", "P4", "P5", "P7"]}) == ("P1", "P3", "P4", "P5", "P7")


def test_the_mock_answers_the_slot_task_with_an_ad_sentence():
    out = MockLLM().json("cover_letter", "", "", {"mode": "template_slots", "about_sentences": ["About sentence."], "ad_sentences": ["Ad."]})
    assert out == {"company_problem_sentence": "About sentence.", "why_sentence": "", "question": ""}
