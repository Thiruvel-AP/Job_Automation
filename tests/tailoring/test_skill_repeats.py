"""No skill appears twice in the CV's Skills section (Document rules, A3).

The canonical key is lower case with hyphens, dots and plurals stripped, and a few aliases mapped: `AWS Lambda` is `Lambda`,
`TensorFlow/Keras` covers `TensorFlow`, `Kubernetes (AWS EKS)` covers `Kubernetes`. Generated groups keep the first occurrence by group
order. A family's verbatim `skill_lines` are the user's own text and are never rewritten: a repeat in them is reported, with where to fix
it, and `careerops doctor` lists every repeat in the fact bank. `skills_always` basics are placed once. No network, no model."""
import copy

import pytest
import yaml

from careerops import setup
from careerops.tailoring import skills as S
from careerops.tailoring.scorer import Keyword


@pytest.mark.parametrize("a,b", [("AWS Lambda", "Lambda"), ("TensorFlow/Keras", "TensorFlow"), ("Kubernetes (AWS EKS)", "Kubernetes"),
                                 ("scikit-learn", "Scikit Learn"), ("Node.js", "nodejs"), ("LLMs", "LLM"), ("REST APIs", "rest api"),
                                 ("Machine Learning", "machine-learning"), ("TensorFlow/Keras", "Keras"), ("pandas", "Pandas"),
                                 ("Amazon S3", "S3"), ("GCP Cloud Run", "Cloud Run")])
def test_these_pairs_are_the_same_skill(a, b):
    assert S.covers(a) & S.covers(b), (S.covers(a), S.covers(b))


@pytest.mark.parametrize("a,b", [("CI/CD", "CI"), ("Python", "PyTorch"), ("AWS", "AWS Lambda"), ("SQL", "NoSQL"), ("R", "Redis"),
                                 ("Java", "JavaScript"), ("Kubernetes (AWS EKS)", "AWS")])
def test_these_pairs_are_not(a, b):
    assert not (S.covers(a) & S.covers(b)), (S.covers(a), S.covers(b))


def test_items_of_a_line_are_split_at_commas_outside_brackets_and_the_bracket_lists_are_skills_too():
    line = "AWS (Bedrock, Lambda, S3), GCP (Cloud Run), Docker, Kubernetes (AWS EKS), serverless deployment (Lambda, Cloud Run)"
    items = S.line_items(line)
    assert items[:5] == ["AWS", "Bedrock", "Lambda", "S3", "GCP"] and "Kubernetes" in items and items.count("Lambda") == 2
    assert "Cloud Run" in items and "serverless deployment" in items


# ------------------------------------------------------------------ generated groups: the first occurrence stays
def test_a_repeat_in_generated_groups_is_removed_keeping_the_first_group_that_has_it():
    rows = {"Cloud": ["AWS Lambda", "Docker"], "Tools": ["Lambda", "Git", "docker"], "ML": ["TensorFlow", "TensorFlow/Keras", "Keras"]}
    out = S.dedupe_skills(rows)
    assert out == {"Cloud": ["AWS Lambda", "Docker"], "Tools": ["Git"], "ML": ["TensorFlow", "Keras"]}      # Keras alone is not a repeat of TensorFlow
    assert S.row_repeats(out) == []


def test_the_generated_cv_groups_use_the_canonical_key(settings, fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    fb["skills"] = {"core": ["Python", "AWS Lambda"], "Cloud": ["Lambda", "Docker"], "Tools": ["docker", "Git"]}
    cfg = {"skill_groups": [{"label": "Languages", "skills": ["Python"]}, {"label": "Cloud", "skills": ["AWS Lambda", "Lambda", "Docker"], "default": True},
                            {"label": "Tools", "skills": ["Git"]}], "skills_always": []}
    groups, _, _ = S.cv_skill_groups(fb, [Keyword("Python", 3, True)], cfg, "")
    flat = [s for items in groups.values() for s in items]
    assert sorted(flat) == ["AWS Lambda", "Docker", "Git", "Python"] and S.row_repeats(groups) == []


def test_a_skills_always_basic_is_placed_once_in_the_first_group_that_names_it(settings, fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    fb["skills"] = {"core": ["Python", "Docker"], "Languages": ["Python", "SQL"], "Cloud": ["Docker", "Python"]}
    cfg = {"skill_groups": [{"label": "Languages", "skills": ["Python", "SQL"], "default": True}, {"label": "Cloud", "skills": ["Docker"]}],
           "skills_always": ["Python", "Docker"]}
    groups, _, _ = S.cv_skill_groups(fb, [], cfg, "")
    assert groups["Languages"].count("Python") == 1 and sum(g.count("Python") for g in groups.values()) == 1
    assert sum(g.count("Docker") for g in groups.values()) == 1 and S.row_repeats(groups) == []


# ------------------------------------------------------------------ verbatim family lines: reported, never edited
LINES = {"Cloud & Deployment": "AWS (Bedrock, Lambda, S3), Docker, Kubernetes (AWS EKS)",
         "Software": "Python, Lambda, TensorFlow/Keras, Kubernetes"}


def test_a_repeat_in_verbatim_lines_is_reported_with_both_groups_and_where_to_fix_it():
    found = S.row_repeats(LINES)
    assert [(r.name, r.first, r.second) for r in found] == [("Lambda", "Cloud & Deployment", "Software"),
                                                           ("Kubernetes", "Cloud & Deployment", "Software")]
    assert S.repeat_problems(LINES, family="data_scientist")[0] == (
        'SKILLS repeat "Lambda" in Cloud & Deployment and Software: fix in fact_bank.yaml families.data_scientist.skill_lines')


def test_a_repeat_inside_one_group_is_reported_too():
    found = S.row_repeats({"Cloud": "AWS (Lambda), serverless deployment (Lambda, Cloud Run)"})
    assert [(r.name, r.first, r.second) for r in found] == [("Lambda", "Cloud", "Cloud")]
    assert S.repeat_problems({"Cloud": "AWS (Lambda), Lambda"})[0].startswith('SKILLS repeat "Lambda" in Cloud and Cloud')


def test_the_verbatim_lines_are_returned_untouched():
    lines = copy.deepcopy(LINES)
    S.row_repeats(lines)
    S.repeat_problems(lines, family="x")
    assert lines == LINES


def test_lines_without_a_repeat_pass():
    assert S.row_repeats({"A": "Python, SQL", "B": "Docker, AWS (Lambda, S3)"}) == []
    assert S.repeat_problems({"A": ["Python", "SQL"], "B": ["Docker"]}) == []


# ------------------------------------------------------------------ doctor lists every repeat in the fact bank
def test_doctor_lists_every_repeat_in_the_banks_family_skill_lines(tmp_path):
    fb = {"families": [{"id": "ai_engineer", "match": ["ai"], "skill_lines": [{"label": "Cloud", "items": "AWS (Lambda, S3), Docker"},
                                                                              {"label": "SW", "items": "Python, Lambda, docker"}]},
                       {"id": "data_scientist", "match": ["ds"], "skill_lines": [{"label": "ML", "items": "Python, SQL"}]}]}
    path = tmp_path / "fact_bank.yaml"
    path.write_text(yaml.safe_dump(fb), encoding="utf-8")
    rows = setup.skill_repeat_checks(path)
    assert [(ok, name) for name, ok, _ in rows] == [
        (False, 'SKILLS repeat "Lambda" in Cloud and SW: fix in fact_bank.yaml families.ai_engineer.skill_lines'),
        (False, 'SKILLS repeat "Docker" in Cloud and SW: fix in fact_bank.yaml families.ai_engineer.skill_lines')]
    fb["families"][0]["skill_lines"][1]["items"] = "Python, Git"
    path.write_text(yaml.safe_dump(fb), encoding="utf-8")
    assert setup.skill_repeat_checks(path) == [("no skill repeats in the fact bank's family skill lines (2 families)", True, "")]
