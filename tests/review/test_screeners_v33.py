"""v3.3 (review/screeners.py): what real screeners read besides keyword %: title match, years required against years shown, the degree, skills-only
required keywords and the knock-outs the ad states. Text fixtures only (the PDF tests are tests/test_ats_structure_v33.py)."""
from datetime import date

import pytest
import yaml

from careerops.discovery import gates
from careerops.review import screeners
from conftest import PROFILE_FIX
from test_ats_structure_v33 import clean_text

TODAY = date(2026, 11, 7)


# ------------------------------------------------------------------ C. the screener signals
def test_years_ignore_overlapping_ranges():
    assert screeners.merged_months([(0, 11), (6, 17)]) == 18
    assert screeners.merged_months([(0, 5), (24, 29)]) == 12
    assert screeners.merged_months([(0, 23), (3, 4), (5, 10)]) == 24
    assert screeners.merged_months([(0, 5), (6, 11)]) == 12                 # touching, not overlapping


def test_years_required_against_years_shown():
    kws = [{"term": "python", "required": True}, {"term": "machine learning", "required": True, "alts": ["ML"]}]
    jd = "You have 3+ years of experience with Python. At least 4 years in machine learning is needed. 2 years' commercial experience."
    assert screeners.jd_years(jd, kws) == [(None, 2), ("machine learning", 4), ("python", 3)]
    text = clean_text().replace("Machine Learning Engineer\n• ", "Machine Learning Engineer\n• Machine learning models in Python. ")
    got = {r.label: r.text() for r in screeners.years(jd, text, kws, TODAY)}
    # Acme Jan 2023 - Sep 2024 (21 months, Python), Globex Oct 2024 - Nov 2026 (26 months, Python, machine learning)
    assert got == {"experience 2+": "experience 2+ met", "machine learning 4+": "machine learning 4+ short by 22 months", "python 3+": "python 3+ met"}


def test_degree_met_not_met_and_desirable():
    text = clean_text()
    assert screeners.degree("A BSc in Computer Science or a related field is required.", text)[0] == "met"
    assert screeners.degree("You hold a PhD in machine learning.", text)[0] == "not met"
    assert screeners.degree("A PhD is desirable.", text)[0] == "not stated"
    assert screeners.degree("Join our graduate scheme.", text)[0] == "not stated"


def test_title_match_family_is_detected(settings, fb_roles):
    assert screeners.title_match("Senior AI Engineer", ["AI Engineer"], settings)[0] == "yes"
    assert screeners.title_match("AI Engineer", ["Data Scientist"], settings)[0] == "partial"
    assert screeners.title_match("AI Engineer", ["Plumber"], settings)[0] == "no"
    assert screeners.title_match("Head Chef", ["Data Scientist"], settings)[0] == "n/a"


@pytest.fixture
def fb_roles(settings):
    gates.load_roles(settings)


def test_skills_only_required_keywords_are_counted():
    kws = [{"term": "R", "required": True}, {"term": "python", "required": True}, {"term": "pandas", "required": False}]
    assert screeners.skills_only(kws, clean_text()) == ["R"]                 # python is in a bullet; pandas is not required


def test_knockouts_report_what_your_files_would_answer():
    applicant = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
    jd = "We are unable to offer visa sponsorship. You will work 4 days a week in the office in London. SC clearance is required."
    job = {"title": "Data Scientist", "location": "London, UK", "description": jd, "company": "Acme"}
    got = screeners.knockouts(jd, job, applicant, None)
    assert any(l.startswith("knock-out right to work / sponsorship") and "RISK" in l and "need it later" in l for l in got)
    assert any(l.startswith("knock-out on-site days") and "you will be asked" in l for l in got)
    assert any(l.startswith("knock-out security clearance") for l in got)
