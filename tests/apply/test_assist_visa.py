"""Apply assist, visa questions: answered from the visa plan of the job's work location W (applicant.yaml visa.by_country, booleans only), every
one highlighted and printed with W (the remote label for a remote role at home, never the home country), never submitted. Local fixture form; no network. Chromium missing = the test fails (a skip would hide a
broken assist)."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops.apply import visa
from careerops.apply.assist import about_another_country, classify_visa, fill_page, visa_value
from careerops.discovery import workcountry
from careerops.discovery.workcountry import Rules

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
GB_SUMMARY = ("I have completed my MSc in Data Science at the University of Northfield and am moving onto the UK Graduate visa (2 years), "
              "so I won't need sponsorship for the next 2 years. After that I'd welcome Skilled Worker sponsorship and am flexible on how "
              "we arrange it.")
MAGENTA = "rgb(255, 0, 255)"


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


@pytest.fixture
def applicant():
    return yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.goto((FIX / "visa_form.html").resolve().as_uri())
        yield pg
        browser.close()


def _outline(page, selector):
    return page.evaluate("s => getComputedStyle(document.querySelector(s)).outlineColor", selector)


def plan_for(applicant, code, mode="onsite"):
    return visa.plan_for(applicant, workcountry._mk(Rules(home="GB", label="Remote"), code, mode, "test"))


def test_each_visa_question_type_is_classified_from_its_wording():
    assert classify_visa("Will you now or in the future require visa sponsorship?") == "sponsorship_future"
    assert classify_visa("Will you require sponsorship in the future?") == "sponsorship_future"
    assert classify_visa("Do you currently require sponsorship to work in the UK?") == "sponsorship_now"
    assert classify_visa("Do you currently have the right to work in the UK?") == "right_to_work_now"
    assert classify_visa("Do you have the right to work in the UK?") is None          # no "now"/"currently": unclear
    assert classify_visa("Right to work share code") is None
    assert classify_visa("Describe your immigration status") is None


def test_answers_come_from_the_booleans_of_the_plan_and_nothing_else(applicant):
    gb = plan_for(applicant, "GB")
    assert [visa_value(gb, k) for k in ("sponsorship_now", "sponsorship_future", "right_to_work_now")] == ["No", "Yes", "Yes"]
    ie = plan_for(applicant, "IE")
    assert [visa_value(ie, k) for k in ("sponsorship_now", "sponsorship_future", "right_to_work_now")] == ["Yes", "Yes", "No"]
    assert visa_value(None, "sponsorship_now") is None and visa_value(gb, None) is None and visa_value(gb, "salary") is None
    applicant["visa"]["by_country"]["GB"].update(sponsorship_now="ask", right_to_work="Yes")        # a string is not a boolean: left to you
    gb = plan_for(applicant, "GB")
    assert visa_value(gb, "sponsorship_now") is None and visa_value(gb, "right_to_work_now") is None
    assert visa_value(gb, "sponsorship_future") == "Yes"


def test_a_question_about_another_country_is_not_answered_for_w(applicant):
    ie = plan_for(applicant, "IE")
    assert about_another_country("Do you currently require sponsorship to work in the UK?", ie) == "United Kingdom"
    assert about_another_country("Do you currently require sponsorship to work in Ireland?", ie) is None
    assert about_another_country("Will you now or in the future require visa sponsorship?", ie) is None
    assert about_another_country("Right to work in the UK?", plan_for(applicant, "GB")) is None
    assert about_another_country("Right to work in the UK?", None) is None


def test_free_text_gets_the_summary_dropdowns_and_radios_get_the_answer_for_their_type(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#free") == GB_SUMMARY
    assert page.input_value("#future") == "Yes"                                           # sponsorship_future
    assert page.input_value("#now") == "No"                                               # sponsorship_now
    assert page.is_checked("input[name=rtw][value=yes]") and not page.is_checked("input[name=rtw][value=no]")
    assert page.is_checked("input[name=fut_radio][value=yes]")                            # sponsorship_future
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER [W=GB]: Will you now or in the future require visa sponsorship? -> Yes" in out
    assert "CHECK VISA ANSWER [W=GB]: Do you currently have the right to work in the UK? -> Yes" in out
    assert page.evaluate("document.body.dataset.submitted") is None                       # nothing submitted
    assert {v["question"] for v in r["visa"]} >= {"Do you currently have the right to work in the UK?"}
    assert {v["work_location"] for v in r["visa"]} == {"GB"}


def test_an_irish_job_is_answered_from_the_default_block_with_ireland_in_the_summary(page, applicant, capsys):
    r = fill_page(page, applicant, None, None, plan=plan_for(applicant, "IE"))
    assert page.input_value("#free") == "I would need a work visa for Ireland and am open to sponsorship."
    assert page.input_value("#future") == "Yes" and page.is_checked("input[name=fut_radio][value=yes]")
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER [W=IE]: Will you now or in the future require visa sponsorship? -> Yes" in out
    # the form asks about the UK, W is Ireland: not answered, said so, still highlighted
    assert page.input_value("#now") == "" and not page.is_checked("input[name=rtw][value=yes]") and not page.is_checked("input[name=rtw][value=no]")
    assert "CHECK VISA ANSWER [W=IE]: Do you currently require sponsorship to work in the UK? -> EMPTY" in out
    assert "the question is about United Kingdom, W is Ireland: left to you" in out
    assert _outline(page, "#now") == MAGENTA and {v["work_location"] for v in r["visa"]} == {"IE"}


def test_a_remote_role_at_home_is_answered_from_the_home_block_and_printed_with_the_remote_label_only(page, applicant, capsys):
    plan = plan_for(applicant, "GB", "remote")
    r = fill_page(page, applicant, None, None, plan=plan)
    assert page.input_value("#future") == "Yes" and page.input_value("#now") == "No"                  # the home block's booleans
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER [W=Remote]: Will you now or in the future require visa sponsorship? -> Yes" in out
    assert "[W=GB]" not in out and {v["work_location"] for v in r["visa"]} == {"Remote"}


def test_an_unclear_type_and_a_share_code_are_left_empty_but_highlighted_and_printed(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#unclear") == "" and page.input_value("#share") == ""
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER [W=GB]: Do you have the right to work in the UK? -> EMPTY" in out
    assert "CHECK VISA ANSWER [W=GB]: Right to work share code -> EMPTY" in out
    assert any("share code" in m.lower() for m in r["manual"]) and any("right to work" in m.lower() for m in r["manual"])
    for sel in ("#free", "#future", "#now", "#unclear", "#share", "input[name=rtw][value=yes]", "input[name=fut_radio][value=no]"):
        assert _outline(page, sel) == MAGENTA, sel                                        # every visa field is highlighted


def test_an_ask_answer_leaves_that_field_empty_and_still_prints_it(page, applicant, capsys):
    applicant["visa"]["by_country"]["GB"].update(sponsorship_now="ask", right_to_work="ask")
    fill_page(page, applicant, None, None)
    assert page.input_value("#now") == "" and not page.is_checked("input[name=rtw][value=yes]")
    assert not page.is_checked("input[name=rtw][value=no]")
    assert page.input_value("#future") == "Yes"                                           # the others are unaffected
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER [W=GB]: Do you currently require sponsorship to work in the UK? -> EMPTY" in out
    assert "CHECK VISA ANSWER [W=GB]: Do you currently have the right to work in the UK? -> EMPTY" in out


def test_no_visa_block_means_nothing_is_filled_and_every_visa_field_is_still_flagged(page, applicant, capsys):
    applicant.pop("visa")
    fill_page(page, applicant, None, None)
    assert page.input_value("#free") == "" and page.input_value("#future") == ""
    out = capsys.readouterr().out
    assert out.count("-> EMPTY") == 7 and "[W=?]" in out                                  # 5 fields + 2 radio groups


def test_other_never_fill_matches_stay_empty_and_are_orange_not_visa_coloured(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#salary") == "" and page.input_value("#nat") == ""
    assert "Salary expectations" in r["manual"] and "Nationality" in r["manual"]
    assert _outline(page, "#salary") == "rgb(255, 165, 0)" and _outline(page, "#nat") == "rgb(255, 165, 0)"
    assert not [l for l in capsys.readouterr().out.splitlines() if "Salary" in l and "CHECK" in l]       # not a visa field: no CHECK line (the field table lists it)


def test_an_existing_answer_in_a_visa_text_box_is_not_overwritten(page, applicant, capsys):
    page.fill("#free", "Already typed")
    fill_page(page, applicant, None, None)
    assert page.input_value("#free") == "Already typed"
    assert "visa or immigration status -> Already typed" in capsys.readouterr().out
