"""CV-2: letter greeting/sign-off from settings, the team name, and the optional gap sentence."""
import re
from pathlib import Path

import pytest

from careerops.tailoring import prompts as P
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword, gap_terms, norm_literal
from careerops.tailoring.tailor import _lack_problems, letter_greeting, regenerate_letter, required_gaps, tailor_job

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "tailoring"

JD_NO_GAP = "About the role\nWe build data tools.\n\nRequirements\n- Python\n- LangChain\n"
JD_GAP = "About the role\nWe build data tools.\n\nRequirements\n- Python\n- Kubernetes\n"
APOLOGY = ("sorry", "apolog", "unfortunately", "regret")


def _run(settings, fb, job, llm, jd=None):
    job = {**job, "description": jd if jd is not None else job["description"]}
    return tailor_job(job, fb, llm, settings, settings.path("outputs")), job


def _letter_ctx(llm):
    return [s["context"] for s in llm.sent if s["task"] == "cover_letter"][-1]


def _team(name):
    return {"extract_keywords": lambda ctx: {"required": [], "preferred": [], "company_hook": "", "team": name}}


# ---------------------------------------------------------------- greeting
def test_settings_carry_the_letter_block(settings):
    assert settings["letter"] == {
        "greeting": "Hi {company}{team} team", "greeting_fallback": "Dear {company} hiring team",
        "closing": "Could we find twenty minutes to talk about where I could contribute first on the {team} team at {company}?",
        "closing_fallback": "Could we find twenty minutes to talk about where I could contribute first at {company}?",
        "signoff": "With regards"}


def test_greeting_uses_the_team_when_there_is_one_and_the_fallback_when_not(settings):
    assert letter_greeting(settings, "Lendable", " AI Accelerator") == "Hi Lendable AI Accelerator team"
    assert letter_greeting(settings, "Lendable", "AI Accelerator ") == "Hi Lendable AI Accelerator team"
    assert letter_greeting(settings, "Lendable", "") == "Dear Lendable hiring team"


def test_letter_without_a_team_says_dear_hiring_team_and_signs_off_from_settings(settings, fb_fixture, job, spy_llm):
    rep, _ = _run(settings, fb_fixture, job, spy_llm())
    text = pdf_text(Path(rep["letter_pdf"]))
    assert "Dear Northwind Health AI hiring team," in text
    assert "With regards," in text and "Kind regards" not in text


def test_letter_with_a_team_named_in_the_jd_says_hi_team(settings, fb_fixture, job, spy_llm):
    jd = job["description"] + "\nYou will join the AI Accelerator group.\n"
    rep, _ = _run(settings, fb_fixture, job, spy_llm(_team(" AI Accelerator")), jd)
    assert rep["team"] == "AI Accelerator"
    assert "Hi Northwind Health AI AI Accelerator team," in pdf_text(Path(rep["letter_pdf"]))


def test_a_team_the_jd_never_mentions_is_dropped(settings, fb_fixture, job, spy_llm):
    rep, _ = _run(settings, fb_fixture, job, spy_llm(_team("Phantom Squad")))
    assert rep["team"] == ""
    assert "Dear Northwind Health AI hiring team," in pdf_text(Path(rep["letter_pdf"]))


def test_regenerated_letter_keeps_the_team_greeting(settings, fb_fixture, job, spy_llm):
    jd = job["description"] + "\nYou will join the AI Accelerator group.\n"
    llm = spy_llm(_team("AI Accelerator"))
    rep, job2 = _run(settings, fb_fixture, job, llm, jd)
    rep = regenerate_letter(job2, fb_fixture, llm, settings, rep)
    assert "Hi Northwind Health AI AI Accelerator team," in pdf_text(Path(rep["letter_pdf"]))


# ---------------------------------------------------------------- gaps
def test_no_gap_sentence_when_gaps_is_empty(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, JD_NO_GAP)
    assert rep["genuine_gaps"] == [] and _letter_ctx(llm)["gaps"] == []
    assert "GAPS" in llm.sent[-1]["user"] and "(none)" in llm.sent[-1]["user"]
    text = " ".join(rep["letter"]["paragraphs"]).lower()
    assert "kubernetes" not in text and "not yet" not in text
    assert len(rep["letter"]["paragraphs"]) == 3 and rep["letter"]["status"] == "ok"


def test_a_required_gap_reaches_the_prompt_and_gets_one_short_plain_sentence(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, JD_GAP)
    assert _letter_ctx(llm)["gaps"] == ["kubernetes"]                 # the taxonomy's spelling
    assert "kubernetes" in llm.sent[-1]["user"].lower()
    gap = [s for p in rep["letter"]["paragraphs"] for s in p.split(". ") if "kubernetes" in s.lower()]
    assert len(gap) == 1 and len(gap[0].split()) <= 25
    assert not any(a in gap[0].lower() for a in APOLOGY)
    # 'kubernetes' is on the deny list; naming it as a gap is allowed, so the model's letter stands
    assert rep["letter"]["status"] == "ok" and rep["letter"]["problems"] == []


def test_only_required_gaps_are_passed(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, "Requirements\n- Python\n\nNice to have\n- Kubernetes\n")
    assert "kubernetes" in rep["genuine_gaps"] and _letter_ctx(llm)["gaps"] == []


def test_deny_listed_gap_term_is_still_blocked_when_the_letter_claims_it(settings, fb_fixture, job, spy_llm):
    lying = {"cover_letter": lambda ctx: {"paragraphs": [
        "Northwind builds tools I already work on.", "I have run Kubernetes clusters in production for two years.",
        "Could we talk for twenty minutes?"]}}
    rep, _ = _run(settings, fb_fixture, job, spy_llm(lying), JD_GAP)
    assert rep["letter"]["status"] == "fallback"
    assert any("deny-listed 'kubernetes'" in p for p in rep["letter"]["problems"])


def test_deny_listed_term_that_is_not_a_gap_is_still_blocked(settings, fb_fixture, job, spy_llm):
    sneaky = {"cover_letter": lambda ctx: {"paragraphs": [
        "Northwind builds tools I already work on.", "I have not yet used Terraform, but I learn quickly.",
        "Could we talk for twenty minutes?"]}}
    rep, _ = _run(settings, fb_fixture, job, spy_llm(sneaky), JD_GAP)
    assert rep["letter"]["status"] == "fallback"
    assert any("deny-listed 'terraform'" in p for p in rep["letter"]["problems"])


# ---------------------------------------------------------------- gaps are tools, not broad skills (CV-2 item 5)
CHATTERMILL = (FIX / "jd_chattermill.txt").read_text(encoding="utf-8")
LIE = {"cover_letter": lambda ctx: {"paragraphs": [
    "Chattermill turns feedback into insight, which is the work I do.", "I built a tool-calling LLM agent.",
    "I have not used statistics in production, but I learn quickly.", "Could we talk for twenty minutes?"]}}


def test_chattermill_broad_skills_are_not_gaps_so_no_gap_sentence(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, CHATTERMILL)
    # the literal terms are missing from the fact bank (the old behaviour called these genuine gaps) ...
    assert {"statistical modelling", "causal inference"} <= {g.lower() for g in rep["genuine_gaps"]}
    # ... but they are broad skills, so nothing reaches the letter
    assert _letter_ctx(llm)["gaps"] == []
    assert "(none)" in llm.sent[-1]["user"]
    text = " ".join(rep["letter"]["paragraphs"]).lower()
    assert not re.search(r"have not|haven't|never used|no experience", text)
    assert rep["letter"]["status"] == "ok"


def test_a_letter_that_says_the_candidate_lacks_statistics_is_rejected(settings, fb_fixture, job, spy_llm):
    rep, _ = _run(settings, fb_fixture, job, spy_llm(LIE), CHATTERMILL)
    assert rep["letter"]["status"] == "fallback"
    assert any("claims a gap not in GAPS" in p and "statistics" in p for p in rep["letter"]["problems"])
    assert "statistics" not in " ".join(rep["letter"]["paragraphs"]).lower()


def test_required_gaps_keeps_only_tools_platforms_languages_frameworks():
    kws = [Keyword(t, 2.0, True) for t in ("kubernetes", "statistical modelling", "communication", "azure",
                                           "LLM agents")] + [Keyword("terraform", 1.0, False)]
    gaps = ["kubernetes", "statistical modelling", "communication", "azure", "LLM agents", "terraform"]
    assert required_gaps(gaps, kws) == ["kubernetes", "azure"]       # not preferred, not broad, not an LLM-only term


@pytest.mark.parametrize("term, cat", [("kubernetes", "tool"), ("azure", "platform"), ("python", "language"),
                                       ("langchain", "framework")])
def test_taxonomy_tags_the_gap_eligible_terms(term, cat):
    assert norm_literal(term) in gap_terms(), cat


@pytest.mark.parametrize("term", ["statistics", "communication", "research", "leadership", "machine learning",
                                  "stakeholders"])
def test_broad_skills_are_not_gap_eligible(term):
    assert term not in gap_terms()


@pytest.mark.parametrize("sentence", [
    "I have not used Kubernetes in production yet.",
    "I haven’t used Kubernetes in production yet.",                  # curly apostrophe
    "I have never used Kubernetes, but I would pick it up quickly.",
])
def test_lack_sentence_naming_a_gap_is_allowed(sentence):
    assert _lack_problems(sentence, ["kubernetes"]) == []


@pytest.mark.parametrize("sentence, gaps", [
    ("I have not used statistics in production.", []),
    ("I have not used statistics in production.", ["kubernetes"]),
    ("I have no experience of research methods.", ["kubernetes"]),
    ("I have not used Kubernetes or Azure yet.", ["kubernetes"]),    # names a second tool that is not a gap
    ("I have never used Kubernetes.", []),
])
def test_lack_sentence_not_in_gaps_is_rejected(sentence, gaps):
    assert len(_lack_problems(sentence, gaps)) == 1


def test_sentences_without_a_lack_claim_are_never_flagged():
    assert _lack_problems("I used Azure and statistics daily. Could we talk?", []) == []


# ---------------------------------------------------------------- prompt text
def test_letter_prompt_follows_the_user_format():
    s = P.LETTER_SYSTEM.lower()
    for must in ("45 words", "25 words", "gaps", "question", "never describe a metric as measured"):
        assert must in s, must
    assert "{gaps}" in P.LETTER_USER


def test_keywords_prompt_asks_for_the_team():
    assert '"team"' in P.KEYWORDS_SYSTEM and "AI Accelerator" in P.KEYWORDS_SYSTEM


def test_cv_prompts_and_selection_follow_the_user_format():
    assert "45 words" in P.PROFILE_SYSTEM and "starting with the HEADLINE" in P.PROFILE_SYSTEM
    assert "{headline}" in P.PROFILE_USER and "past-tense" in P.REPHRASE_SYSTEM
    from careerops.tailoring.selection import MOST_RECENT_BULLETS, OTHER_BULLETS, PROJECTS
    assert (MOST_RECENT_BULLETS, OTHER_BULLETS, PROJECTS) == ((2, 4), (1, 2), 2)     # 2-4 / 1-2 bullets, exactly 2 projects
