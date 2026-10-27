"""The knock-out pre-scan (apply/knockout.py): before anything is drafted or filled, the whole form is scanned for questions that decide an application
outright (sponsorship and work authorisation, years of experience, degree, salary floor, on-site / relocation, clearance) and each is checked against what
the user's own files would answer. A likely mismatch stops the job: `KNOCK-OUT: "<question>" -> your answer "<answer>" may auto-reject. [c]ontinue / [s]kip job`.
A missing CV section is never evidence of lacking a degree. In auto mode a knock-out always goes to the human. No network, no model."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import knockout
from careerops.apply.visa import plan_for, work_for
from careerops.config import load_yaml

APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
FB = load_yaml(PROFILE_FIX / "fact_bank.yaml")
JOB = {"id": "x:graphcore:1", "company": "Graphcore", "title": "ML Engineer", "location": "Bristol, UK", "description": "", "source": "greenhouse"}
PLAN = plan_for(APPLICANT, work_for(JOB, APPLICANT))
YES_NO = ["Yes", "No"]


def book(*rows):
    return answers_mod.AnswerBook([answers_mod.Entry(id=i, patterns=tuple(answers_mod.norm(m) for m in match), answer=a) for i, match, a in rows])


def scan(question, options=None, *, applicant=APPLICANT, bk=None, fb=FB, year=2026):
    return knockout.scan([(question, options if options is not None else YES_NO)], applicant, bk or answers_mod.AnswerBook(), JOB, PLAN, fb, today_year=year)


# ------------------------------------------------------------------ sponsorship and work authorisation
def test_a_no_sponsorship_posting_is_a_knock_out_for_someone_who_will_need_it_later():
    k = scan("Do you require visa sponsorship? Graphcore is unable to offer visa sponsorship for this role.")
    assert len(k) == 1 and k[0].kind == "sponsorship"
    assert k[0].answer == "No" and "future" in k[0].why                      # now=No, future=Yes in the visa block: the wording still rejects a later need


def test_a_combined_sponsorship_question_is_yes_when_either_is_yes_and_warns():
    q = "Will you now or in the future require sponsorship to work in the UK?"
    assert assist_mod.sponsorship_answer(PLAN, q) == "Yes"                    # now=No, future=Yes
    k = scan(q)
    assert [(x.kind, x.answer) for x in k] == [("sponsorship", "Yes")] and "auto-reject" in k[0].line()


def test_the_combined_answer_is_yes_when_only_the_first_is_yes_and_no_when_both_are_no():
    only_now = type("P", (), {"answers": {"sponsorship_now": True, "sponsorship_future": False}})()
    neither = type("P", (), {"answers": {"sponsorship_now": False, "sponsorship_future": False}})()
    q = "Do you now or in the future require sponsorship?"
    assert assist_mod.sponsorship_answer(only_now, q) == "Yes" and assist_mod.sponsorship_answer(neither, q) == "No"
    assert assist_mod.sponsorship_answer(PLAN, "Do you currently require sponsorship?") == "No"             # a plain "now" question is not combined
    assert assist_mod.sponsorship_answer(PLAN, "Will you require sponsorship in the future?") == "Yes"


def test_no_knock_out_when_nothing_would_be_rejected():
    assert scan("Do you currently require sponsorship to work in the UK?") == []                # now=No
    assert scan("Do you currently have the right to work in the UK?") == []                   # Yes


def test_a_right_to_work_no_is_a_knock_out():
    other = {"visa": {"home": "GB", "by_country": {"GB": {"right_to_work": False, "sponsorship_now": True, "sponsorship_future": True, "summary": "x"}}}}
    plan = plan_for(other, work_for(JOB, other))
    k = knockout.scan([("Do you currently have the right to work in the UK?", YES_NO)], other, answers_mod.AnswerBook(), JOB, plan, FB)
    assert [(x.kind, x.answer) for x in k] == [("work authorisation", "No")]


# ------------------------------------------------------------------ years, degree, salary, location, clearance
def test_years_of_experience_below_the_minimum_is_a_knock_out_and_enough_is_not():
    q = "How many years of experience do you have in machine learning? (minimum 5+ years)"
    assert [x.kind for x in scan(q, [], applicant={**APPLICANT, "years_experience": 3})] == ["years of experience"]
    assert scan(q, [], applicant={**APPLICANT, "years_experience": 7}) == []
    assert scan(q, [], applicant=APPLICANT, fb={}) == []                                         # nothing known about the user's years: asked later, not a knock-out


def test_years_can_come_from_the_fact_banks_dated_roles():
    fb = {"experience": [{"title": "Analyst", "dates": "2023 - 2025"}, {"title": "Intern", "dates": "Jun 2022 - Sep 2022"}]}
    k = scan("Do you have at least 5 years of professional experience?", applicant=APPLICANT, fb=fb, year=2026)
    assert [x.kind for x in k] == ["years of experience"]


def test_a_degree_the_fact_bank_does_not_show_is_a_knock_out():
    assert [x.kind for x in scan("Do you hold a PhD?")] == ["degree"]                           # the fixture bank has an MSc only
    assert scan("Do you have a Master's degree?") == []
    assert scan("Have you completed a degree?") == []


def test_a_missing_education_section_is_never_evidence_of_lacking_the_degree():
    assert scan("Do you hold a PhD?", fb={}) == []
    assert scan("Do you hold a PhD?", fb={"education": []}) == []
    assert scan("Do you hold a PhD?", fb={"experience": [{"title": "x", "dates": "2020 - 2022"}]}) == []


def test_an_answers_yaml_no_to_a_degree_question_is_a_knock_out_even_without_a_fact_bank():
    k = scan("Do you hold a PhD?", bk=book(("phd", ["hold a phd"], "No")), fb={})
    assert [(x.kind, x.answer) for x in k] == [("degree", "No")]


def test_a_salary_floor_above_the_users_number_is_a_knock_out():
    bk = answers_mod.AnswerBook([answers_mod.Entry("salary", ("salary expectation",), "open", numeric="50000")])
    assert [x.kind for x in scan("Salary expectation: the minimum salary for this role is £60,000", [], bk=bk)] == ["salary floor"]
    assert scan("Salary expectation: the minimum salary for this role is £45,000", [], bk=bk) == []


def test_on_site_and_relocation_answered_no_is_a_knock_out():
    q = "This role is on-site in London 4 days a week. Are you able to commute or relocate?"
    assert [(x.kind, x.answer) for x in scan(q, bk=book(("loc", ["relocate"], "No")))] == [("on-site / location", "No")]
    assert scan(q, bk=book(("loc", ["relocate"], "Yes"))) == []
    assert scan(q) == []                                                                            # no answer on file: asked later


def test_clearance_answered_no_is_a_knock_out():
    q = "Do you hold active SC security clearance?"
    assert [(x.kind, x.answer) for x in scan(q, bk=book(("sc", ["clearance"], "No")))] == [("clearance", "No")]
    assert scan(q, bk=book(("sc", ["clearance"], "Yes"))) == []


def test_an_ordinary_question_is_not_a_knock_out():
    for q in ("Full name", "Email", "Describe a project you are proud of", "How did you hear about us?", "Your LinkedIn profile"):
        assert scan(q, []) == [], q


# ------------------------------------------------------------------ what is printed and asked
def test_the_line_names_the_question_and_the_answer():
    k = scan("Will you now or in the future require sponsorship to work in the UK?")[0]
    assert k.line() == ('KNOCK-OUT: "Will you now or in the future require sponsorship to work in the UK?" → your answer "Yes" may auto-reject. '
                        "[c]ontinue / [s]kip job")


def test_the_person_continues_with_c_and_anything_else_skips_the_job():
    knocks = scan("Will you now or in the future require sponsorship to work in the UK?")
    said, out = [], []
    assert knockout.confirm(knocks, lambda p: (said.append(p), "c")[1], out.append) is True and "KNOCK-OUT" in out[0] and "[c]ontinue" in said[0]
    for reply in ("s", "", "x", "continue anyway please"):
        assert knockout.confirm(knocks, lambda p, r=reply: r, lambda s: None) is False
    assert knockout.confirm([], lambda p: pytest.fail("asked with nothing to ask"), lambda s: None) is True
    assert knockout.confirm(knocks, lambda p: "C", lambda s: None) is True


def test_in_auto_mode_a_knock_out_always_goes_to_the_human():
    knocks = scan("Will you now or in the future require sponsorship to work in the UK?")
    reason = knockout.stop_reason(knocks)
    assert reason.startswith("KNOCK-OUT") and "sponsorship" in reason
    assert knockout.stop_reason([]) == ""
