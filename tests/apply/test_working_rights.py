"""Working-rights questions are answered from the visa plan (applicant.yaml) and from nothing else (`assist.rights_pick`); the visa TYPE question is a
different target. No browser: the lists are plain strings."""
import copy

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops.apply import assist, shortfields, visa
from careerops.discovery import workcountry
from careerops.discovery.workcountry import Rules

RIGHTSHIP = "What are your working rights in the country where this role is advertised for?"
STATUSES = ["Citizen", "Permanent Resident", "Work Visa", "Require sponsorship"]
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


def plan(applicant=None, code="GB"):
    return visa.plan_for(applicant or APPLICANT, workcountry._mk(Rules(home="GB", label="Remote"), code, "onsite", "test"))


def test_the_rightship_label_is_a_visa_question_and_picks_work_visa():
    assert assist.is_visa_question(RIGHTSHIP + "*")
    assert assist.rights_pick(plan(), RIGHTSHIP, STATUSES) == ("Work Visa", "")


def test_the_visa_type_question_is_not_one_of_them_and_stays_separate():
    q = "If you answered 'Work Visa' in the previous question, please specify the type of visa that you are currently holding."
    assert not assist.is_visa_question(q) and shortfields.is_visa_type(q)
    assert assist.rights_pick(plan(), q, STATUSES)[0] is None or not assist.WORKING_RIGHTS.search(q)


def test_a_yes_no_list_with_the_words_working_rights_uses_right_to_work():
    assert assist.rights_pick(plan(), "Do you have working rights in the UK?", ["Yes", "No"]) == ("Yes", "")
    other = copy.deepcopy(APPLICANT)
    other["visa"]["by_country"]["GB"]["right_to_work"] = False
    assert assist.rights_pick(plan(other), "Do you have working rights in the UK?", ["Yes", "No"])[0] == "No"


def test_a_plain_right_to_work_yes_no_is_still_unclear_and_left_to_the_person():
    pick, why = assist.rights_pick(plan(), "Do you have the right to work in the UK?", ["Yes", "No"])
    assert pick is None and "left to you" in why
    assert assist.classify_visa("Do you have the right to work in the UK?") is None


def test_needing_sponsorship_picks_the_sponsorship_option():
    other = copy.deepcopy(APPLICANT)
    other["visa"]["by_country"]["GB"].update(right_to_work=False, sponsorship_now=True)
    assert assist.rights_pick(plan(other), RIGHTSHIP, STATUSES)[0] == "Require sponsorship"


@pytest.mark.parametrize("options", [["Citizen", "Permanent Resident"], ["Work Visa", "Student Visa"], []])
def test_no_option_or_more_than_one_fits_means_nothing_is_picked(options):
    pick, why = assist.rights_pick(plan(), RIGHTSHIP, options)
    assert pick is None and why


def test_a_summary_that_does_not_say_you_hold_a_visa_picks_nothing():
    other = copy.deepcopy(APPLICANT)
    other["visa"]["by_country"]["GB"]["summary"] = "I live in the UK."
    assert assist.rights_pick(plan(other), RIGHTSHIP, STATUSES)[0] is None


def test_a_question_about_another_country_and_a_missing_plan_pick_nothing():
    assert assist.rights_pick(plan(), "What are your working rights in Germany?", STATUSES)[0] is None
    assert assist.rights_pick(None, RIGHTSHIP, STATUSES)[0] is None
    assert assist.rights_pick(plan(), "Which city do you live in?", STATUSES)[0] is None
