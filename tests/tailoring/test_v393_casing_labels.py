"""v3.9.3: a model-filled letter slot spells tool names as the fact bank does ("python" -> "Python"), and a Skills item equal to ANY printed label
is dropped."""
import pytest

from careerops.llm import MockLLM
from careerops.tailoring import reqmap
from careerops.tailoring import template_letter as TL

BANK = {"skills": {"core": ["Python", "PyTorch", "pandas", "R", "Statistics"], "Data": ["AWS Lambda", "AWS"]},
        "families": [{"id": "data_scientist", "skill_lines": [{"label": "Tools", "items": "scikit-learn (XGBoost), Go"}]}]}


# ------------------------------------------------------------------ 2. slot casing from the fact bank
def test_proficiency_with_python_and_pandas_renders_in_the_banks_casing_and_passes():
    ad = "You will need proficiency with Python and pandas to build our forecasting models."
    slot = TL._clean_slot("ad_requirement", "proficiency with python and pandas", TL.bank_casing(BANK))
    assert slot == "proficiency with Python and pandas"
    assert TL.requirement_problems(slot, ad, "Acme", "Data Scientist") == []


@pytest.mark.parametrize("raw,want", [
    ("experience with pytorch and xgboost", "experience with PyTorch and XGBoost"),       # a family skill-line item, inside its bracket
    ("deploying on aws lambda", "deploying on AWS Lambda"),                               # the longest name wins
    ("a go-to r workflow and pandas", "a go-to r workflow and pandas"),                    # one-letter and lower-case two-letter names: ordinary words
    ("pythonic code", "pythonic code"),                                                    # whole words only
])
def test_bank_case_whole_words_longest_first(raw, want):
    assert TL.bank_case(raw, TL.bank_casing(BANK)) == want


def test_a_sentence_slot_keeps_its_capital_and_the_bank_spelling():
    assert TL._clean_slot("company_problem_sentence", "pandas pipelines feed pytorch models", TL.bank_casing(BANK)) == "Pandas pipelines feed PyTorch models."
    assert TL._clean_slot("question", "how the team tests python services", TL.bank_casing(BANK)) == "how the team tests Python services"


@pytest.mark.slow
def test_a_lower_case_python_slot_renders_in_the_banks_casing_and_the_letter_passes_its_fact_check(fb_fixture, settings, tmp_path):
    """The EDITED letter failed its fact check on "proficiency with python". The slot is re-cased before the check, which is unchanged."""
    from test_template_letter import AD, QUESTION, SENT, bank, KWS

    ad = AD + "\nYou bring proficiency with Python and pandas.\n"
    jb = {"id": "manual:tpl393", "company": "Northwind Health AI", "title": "AI Engineer", "description": ad}
    llm = MockLLM({"cover_letter": lambda ctx: {"company_problem_sentence": SENT, "ad_requirement": "proficiency with python and pandas", "question": QUESTION}})
    fb = bank(fb_fixture)
    t = TL.find(fb, ["ai_engineer"])
    _, letter = TL.make(t, jb, fb, KWS, llm, settings, tmp_path, "Ada Lovelace Cover Letter", "", {"headline": "AI Engineer", "number": ""}, "LLM agents and AWS")
    assert letter["slots"]["ad_requirement"] == "proficiency with Python and pandas" and letter["slot_source"]["ad_requirement"] == "model"
    assert "Your ad asks for proficiency with Python and pandas" in " ".join(letter["paragraphs"])
    assert not any("ad_requirement" in p or "python" in p for p in letter["template_problems"]), letter["template_problems"]


# ------------------------------------------------------------------ 3. a label repeated as an item in another line
def test_an_item_equal_to_another_rows_label_is_removed_from_that_row():
    rows = {"Machine learning concepts": "Supervised learning, Model evaluation, Feature engineering",
            "Machine Learning, Deep Learning & Statistics": "Machine learning concepts, PyTorch, Statistics",
            "Tools": "Docker, Git"}
    out = reqmap.drop_label_echoes(rows)
    assert out["Machine Learning, Deep Learning & Statistics"] == "PyTorch, Statistics"
    assert out["Machine learning concepts"] == rows["Machine learning concepts"] and out["Tools"] == "Docker, Git"
    assert list(out) == list(rows)


def test_a_row_left_empty_by_the_echo_is_dropped_and_rows_without_one_are_untouched():
    rows = {"Statistical analyses": "Hypothesis testing, Bootstrap", "Other": "statistical analysis", "Tools": "Docker (Compose), Git"}
    assert reqmap.drop_label_echoes(rows) == {"Statistical analyses": "Hypothesis testing, Bootstrap", "Tools": "Docker (Compose), Git"}
