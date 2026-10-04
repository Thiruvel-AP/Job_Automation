"""The benefits section is cut before keywords are read, and the letter's name check understands possessives. No model."""
import pytest

from careerops.tailoring import tailor
from careerops.tailoring.faithfulness import check
from careerops.tailoring.grounding import grounding_problems
from careerops.tailoring.jdtext import strip_benefits
from careerops.tailoring.scorer import Keyword

JD = """About the role
We need a data scientist with Python and SQL.

What you'll do
- Build forecasting models

Benefits & perks
- Private healthcare
- 25 days holiday
Flexible working and a generous pension

Who you are
- Strong statistics
"""


# ---------------------------------------------------------------- benefits section
def test_the_benefits_section_is_cut_up_to_the_next_heading():
    text, note = strip_benefits(JD)
    assert "healthcare" not in text and "pension" not in text and "holiday" not in text
    assert "Python and SQL" in text and "Build forecasting models" in text and "Strong statistics" in text
    assert note.startswith("cut the benefits section 'Benefits & perks'")


@pytest.mark.parametrize("heading", ["Benefits", "Our perks", "What we offer", "What we offer you:", "Why join us?",
                                     "Why join Acme", "Rewards & recognition", "**Perks and benefits**", "## Benefits"])
def test_every_benefits_style_heading_is_recognised(heading):
    text, note = strip_benefits(f"The role\nPython.\n\n{heading}\nFree fruit\n\nRequirements:\n- SQL\n")
    assert "fruit" not in text and "SQL" in text and note


def test_ordinary_sentences_after_the_benefits_are_kept():
    text, _ = strip_benefits("Role\nPython.\n\nOur Perks\nOptional private healthcare plan\n\n"
                             "Proficiency in PyTorch or TensorFlow.\n")
    assert "healthcare" not in text and "Proficiency in PyTorch or TensorFlow." in text


def test_a_section_running_to_the_end_is_cut_and_a_jd_without_one_is_untouched():
    assert strip_benefits("Role\nPython\n\nBenefits\nFree fruit\nGym")[0] == "Role\nPython\n"
    plain = "Role\nWe offer a great salary and benefits include a pension.\nPython\n"      # a sentence, not a heading
    assert strip_benefits(plain) == (plain, None)


def test_keywords_from_the_benefits_section_never_reach_the_model_or_the_score(settings, job, spy_llm):
    j = {**job, "description": JD}
    llm = spy_llm({"extract_keywords": lambda ctx: {"required": ["Python", "private healthcare", "flexible working"],
                                                     "preferred": ["pension"], "company_hook": "", "team": ""}})
    notes: list[str] = []
    kws, _, _ = tailor.analyse(j, llm, settings, notes)
    prompt = [s for s in llm.sent if s["task"] == "extract_keywords"][0]["user"]
    assert "healthcare" not in prompt and "pension" not in prompt and "Python" in prompt
    terms = {k.term.lower() for k in kws}
    assert "python" in terms and not terms & {"private healthcare", "flexible working", "pension", "holiday"}
    assert any("cut the benefits section" in n for n in notes)


# ---------------------------------------------------------------- possessives in the letter's name check
HOOK = "Chattermill turns customer feedback into insight."
EVIDENCE = [{"text": "Built a churn model for a retail bank.", "terms": ["churn"]}]
JD_TEXT = "Chattermill is hiring a Data Scientist. Zone & Co is our parent group."


def _problems(sentence, company="Chattermill"):
    return grounding_problems([sentence], EVIDENCE, JD_TEXT, HOOK, f"{company} Data Scientist")


@pytest.mark.parametrize("sentence", ["Chattermill's work on customer feedback is what drew me to the role.",
                                      "Chattermill’s work on customer feedback is what drew me to the role.",
                                      "What drew me to the role is Chattermill's focus on customer feedback."])
def test_the_company_name_with_a_possessive_passes(sentence):
    assert _problems(sentence) == []


def test_the_company_name_and_its_parts_are_always_allowed():
    assert grounding_problems(["Zone & Co's platform is where I would like to work."], EVIDENCE, "A role.", "",
                              "Zone & Co Data Scientist") == []
    assert grounding_problems(["I would like to work at Zone."], EVIDENCE, "A role.", "", "Zone & Co Data Scientist") == []


@pytest.mark.parametrize("sentence,name", [
    ("I built a churn model for a fintech client called Acme.", "Acme"),
    ("My churn work for Acme's customers is what I would bring.", "Acme"),
    ("I used my churn model at Barclays’ retail bank.", "Barclays"),
    ("Chattermill's work reminds me of my time at Spotify.", "Spotify")])
def test_a_real_invented_name_is_still_caught_even_with_a_possessive(sentence, name):
    out = _problems(sentence)
    assert any(f"'{name}' (name)" in p for p in out), out


def test_an_invented_client_and_sector_are_still_caught():
    out = _problems("I built a churn model for a fintech client called Acme.")
    assert any("'fintech' (sector)" in p for p in out) and any("'client' (client)" in p for p in out)


def test_ive_and_im_with_curly_apostrophes_are_not_mistaken_for_names():
    assert _problems("I’ve built churn models, and I’m keen to apply them at Chattermill.") == []


def test_faithfulness_still_flags_an_invented_name_in_a_reworded_bullet_and_ignores_a_possessive_company():
    kws = [Keyword("churn", 1.0, True)]
    known = check("Built a churn model for Chattermill’s customers.", "Built a churn model.", [], [], kws,
                  vocabulary="Chattermill turns feedback into insight")
    assert known.warnings == []
    invented = check("Built a churn model for Acme’s customers.", "Built a churn model.", [], [], kws,
                     vocabulary="Chattermill turns feedback into insight")
    assert any("unrecognised name 'Acme'" in w for w in invented.warnings)
