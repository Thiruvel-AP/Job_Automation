"""Skills (round of 17 Oct, item 5): a skill appears once ("Kubernetes (AWS EKS)" and "Kubernetes", "AWS" and "AWS Lambda" were both
on the CV), each group is ordered by the job's keyword weight and then by fact-bank order, all four groups render ("Client Delivery &
Communication" was missing), and TypeScript / JavaScript join the Python group only when the ad asks for them. No network, no model.
"""
import pytest

from careerops.config import load_settings
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.skills import cv_skill_groups, skill_group_config

ML, PYTHON, LLM, CLIENT = ("Machine Learning, Deep Learning & Statistics", "Python, Data Quality & Cloud", "NLP, LLMs & Evaluation",
                           "Client Delivery & Communication")


def cfg():
    return skill_group_config(load_settings())


def bullet(bid, terms):
    return {"id": bid, "text": f"Did {bid}.", "terms": terms}


def bank(**skills):
    base = {"core": ["Python", "SQL", "Machine Learning", "Docker", "Kubernetes", "AWS", "Git"],
            "Languages": ["Python", "SQL", "R", "TypeScript", "JavaScript", "Swift", "Dart"],
            "ML & Statistics": ["scikit-learn", "XGBoost", "PyTorch", "statistics"],
            "LLMs & Agents": ["LangChain", "RAG", "LLMs"],
            "Cloud & MLOps": ["AWS Lambda", "Docker", "GitHub Actions", "MLflow", "AWS ECS", "Kubernetes (AWS EKS)", "pytest"],
            "Mobile & Web": ["React", "Flutter"]}
    base.update(skills)
    return {"deny_terms": [], "skills": base, "profile_facts": [],
            "experience": [{"id": "e1", "title": "t", "org": "o", "bullets": [
                bullet("b1", ["stakeholder communication", "client", "presentation", "python"]),
                bullet("b2", ["requirements", "stakeholder", "consulting", "ownership", "delivery"]),
                bullet("b3", ["code review", "collaboration"])]}], "projects": []}


def flat(groups):
    return [s for items in groups.values() for s in items]


def run(fb=None, kws=(), jd=""):
    return cv_skill_groups(fb or bank(), list(kws), cfg(), jd)


# ---------------------------------------------------------------- the shipped configuration
def test_the_shipped_settings_define_the_four_groups_umbrellas_and_the_asked_for_languages():
    c = cfg()
    assert [g["label"] for g in c["skill_groups"]] == [ML, PYTHON, LLM, CLIENT]
    assert "AWS" in c["skill_umbrellas"] and {"TypeScript", "JavaScript"} <= set(c["skills_when_asked"])


# ---------------------------------------------------------------- a skill appears once
def test_a_skill_and_its_qualified_form_are_one_skill_and_the_specific_one_stays():
    groups, _, _ = run()
    names = flat(groups)
    assert "Kubernetes (AWS EKS)" in names and "Kubernetes" not in names
    assert len({n.casefold() for n in names}) == len(names)


def test_an_umbrella_name_goes_when_a_specific_skill_of_it_is_listed():
    groups, _, _ = run()
    names = flat(groups)
    assert "AWS Lambda" in names and "AWS ECS" in names and "AWS" not in names


def test_an_umbrella_name_alone_stays():
    fb = bank(**{"Cloud & MLOps": ["Docker", "GitHub Actions"]})
    assert "AWS" in flat(run(fb)[0])


def test_the_dropped_umbrella_hands_its_pin_to_the_specific_skill_so_the_cap_cannot_cut_it():
    """`skills.core` AWS and Kubernetes are pinned; AWS Lambda and Kubernetes (AWS EKS) stand for them now."""
    groups, _, _ = cv_skill_groups(bank(), [], {**cfg(), "skill_group_max": 3}, "")
    assert {"AWS Lambda", "Kubernetes (AWS EKS)"} <= set(groups[PYTHON])


def test_a_skill_that_only_shares_its_first_letters_with_another_is_not_an_umbrella():
    fb = bank(**{"Languages": ["R", "R Markdown", "SQL", "PostgreSQL"]})
    names = flat(run(fb)[0])
    assert "R" in names and "R Markdown" in names and "SQL" in names and "PostgreSQL" in names


# ---------------------------------------------------------------- order inside a group
def test_inside_a_group_skills_are_ordered_by_keyword_weight_for_the_job_then_by_bank_order():
    kws = [Keyword("MLflow", 3, True), Keyword("pytest", 2, True), Keyword("GitHub Actions", 2, True)]
    groups, _, _ = run(kws=kws)
    cloud = [s for s in groups[PYTHON] if s in ("AWS Lambda", "GitHub Actions", "MLflow", "AWS ECS", "pytest")]
    assert cloud == ["MLflow", "GitHub Actions", "pytest", "AWS Lambda", "AWS ECS"]       # 3, then the two 2s in bank order, then the 0s in bank order


def test_a_pinned_skill_has_no_edge_over_a_heavier_one_and_equal_weights_keep_bank_order():
    fb = bank(**{"Cloud & MLOps": ["MLflow", "pytest", "GitHub Actions"]})
    groups, _, _ = run(fb, [Keyword("pytest", 3, True)])
    assert groups[PYTHON] == ["pytest", "Python", "SQL", "Docker", "Kubernetes", "AWS", "Git", "R", "Swift", "Dart", "MLflow", "GitHub Actions"]


# ---------------------------------------------------------------- all four groups
def test_all_four_groups_render_even_when_the_bank_has_no_skill_row_for_client_delivery():
    groups, _, empty = run()
    assert list(groups) == [ML, PYTHON, LLM, CLIENT] and empty == []
    assert groups[CLIENT] and all(isinstance(s, str) and s for s in groups[CLIENT])


def test_the_client_group_holds_terms_the_user_wrote_in_the_bank_and_nothing_else():
    groups, _, _ = run()
    written = {t.casefold() for e in bank()["experience"] for b in e["bullets"] for t in b["terms"]}
    assert groups[CLIENT] and all(s.casefold() in written for s in groups[CLIENT])
    assert "Stakeholder communication" in groups[CLIENT] and "Stakeholder" not in groups[CLIENT] and "Communication" not in groups[CLIENT]
    assert "Python" not in groups[CLIENT] and "Code review" not in groups[CLIENT]


def test_a_bank_skill_row_for_the_group_is_used_instead_of_the_terms():
    fb = bank(**{"Client Delivery": ["Stakeholder workshops", "Requirements gathering"]})
    assert run(fb)[0][CLIENT] == ["Stakeholder workshops", "Requirements gathering"]


def test_the_client_terms_never_include_a_deny_listed_word():
    fb = bank()
    fb["deny_terms"] = ["consulting"]
    assert not any("onsulting" in s for s in run(fb)[0][CLIENT])


def test_a_group_with_no_terms_and_no_skills_is_still_left_off_and_reported():
    fb = bank()
    fb["experience"][0]["bullets"] = [bullet("b3", ["code review"])]
    groups, _, empty = run(fb)
    assert CLIENT not in groups and empty == [CLIENT]


# ---------------------------------------------------------------- languages only when the ad asks
def test_typescript_and_javascript_are_left_off_when_the_ad_does_not_ask_for_them():
    names = flat(run(jd="We need Python, SQL and scikit-learn.")[0])
    assert "TypeScript" not in names and "JavaScript" not in names and "Python" in names and "R" in names


def test_typescript_joins_the_python_group_when_the_ad_asks_for_it():
    groups, _, _ = run(jd="You will write TypeScript services and Python jobs.")
    assert "TypeScript" in groups[PYTHON] and "JavaScript" not in flat(groups)


def test_the_ad_asking_for_both_brings_both():
    groups, _, _ = run(jd="Experience with JavaScript or TypeScript.")
    assert {"TypeScript", "JavaScript"} <= set(groups[PYTHON])


def test_a_language_is_matched_as_a_whole_word_in_the_ad():
    assert "JavaScript" not in flat(run(jd="Our JavaScriptless culture.")[0])
