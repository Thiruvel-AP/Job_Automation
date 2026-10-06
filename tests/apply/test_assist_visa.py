"""Apply assist, visa questions: answered from applicant.yaml -> visa by type, every one highlighted and printed, never
submitted. Local fixture form; no network. Chromium missing = the test fails (a skip would hide a broken assist)."""
from pathlib import Path

import pytest
import yaml
from playwright.sync_api import sync_playwright

from careerops.apply.assist import classify_visa, fill_page, visa_value
from careerops.config import ROOT

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
SUMMARY = "I am on the UK Graduate visa and will not need sponsorship for 2 years."
MAGENTA = "rgb(255, 0, 255)"


@pytest.fixture
def applicant():
    a = yaml.safe_load((ROOT / "config/profile/applicant.yaml").read_text(encoding="utf-8"))
    a["visa"] = {"summary": SUMMARY, "answers": {"sponsorship_now": "No", "sponsorship_future": "Yes",
                                                  "right_to_work_now": "Yes"}}
    return a


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


def test_each_visa_question_type_is_classified_from_its_wording():
    assert classify_visa("Will you now or in the future require visa sponsorship?") == "sponsorship_future"
    assert classify_visa("Will you require sponsorship in the future?") == "sponsorship_future"
    assert classify_visa("Do you currently require sponsorship to work in the UK?") == "sponsorship_now"
    assert classify_visa("Do you currently have the right to work in the UK?") == "right_to_work_now"
    assert classify_visa("Do you have the right to work in the UK?") is None          # no "now"/"currently": unclear
    assert classify_visa("Right to work share code") is None
    assert classify_visa("Describe your immigration status") is None


def test_ask_and_missing_answers_are_empty_and_a_yaml_boolean_is_read_as_yes_no():
    assert visa_value({"visa": {"answers": {"sponsorship_now": "ask"}}}, "sponsorship_now") is None
    assert visa_value({"visa": {"answers": {"sponsorship_now": " ASK "}}}, "sponsorship_now") is None
    assert visa_value({"visa": {"answers": {}}}, "sponsorship_now") is None
    assert visa_value({}, "sponsorship_now") is None and visa_value({"visa": {"answers": {"x": "Yes"}}}, None) is None
    assert visa_value({"visa": {"answers": {"sponsorship_now": False, "sponsorship_future": True}}},
                      "sponsorship_now") == "No"      # an unquoted No/Yes in YAML is a boolean


def test_free_text_gets_the_summary_dropdowns_and_radios_get_the_answer_for_their_type(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#free") == SUMMARY
    assert page.input_value("#future") == "Yes"                                           # sponsorship_future
    assert page.input_value("#now") == "No"                                               # sponsorship_now
    assert page.is_checked("input[name=rtw][value=yes]") and not page.is_checked("input[name=rtw][value=no]")
    assert page.is_checked("input[name=fut_radio][value=yes]")                            # sponsorship_future
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER: Will you now or in the future require visa sponsorship? -> Yes" in out
    assert "CHECK VISA ANSWER: Do you currently have the right to work in the UK? -> Yes" in out
    assert page.evaluate("document.body.dataset.submitted") is None                       # nothing submitted
    assert {v["question"] for v in r["visa"]} >= {"Do you currently have the right to work in the UK?"}


def test_an_unclear_type_and_a_share_code_are_left_empty_but_highlighted_and_printed(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#unclear") == "" and page.input_value("#share") == ""
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER: Do you have the right to work in the UK? -> EMPTY" in out
    assert "CHECK VISA ANSWER: Right to work share code -> EMPTY" in out
    assert any("share code" in m.lower() for m in r["manual"]) and any("right to work" in m.lower() for m in r["manual"])
    for sel in ("#free", "#future", "#now", "#unclear", "#share", "input[name=rtw][value=yes]", "input[name=fut_radio][value=no]"):
        assert _outline(page, sel) == MAGENTA, sel                                        # every visa field is highlighted


def test_an_ask_answer_leaves_that_field_empty_and_still_prints_it(page, applicant, capsys):
    applicant["visa"]["answers"].update(sponsorship_now="ask", right_to_work_now="ask")
    fill_page(page, applicant, None, None)
    assert page.input_value("#now") == "" and not page.is_checked("input[name=rtw][value=yes]")
    assert not page.is_checked("input[name=rtw][value=no]")
    assert page.input_value("#future") == "Yes"                                           # the others are unaffected
    out = capsys.readouterr().out
    assert "CHECK VISA ANSWER: Do you currently require sponsorship to work in the UK? -> EMPTY" in out
    assert "CHECK VISA ANSWER: Do you currently have the right to work in the UK? -> EMPTY" in out


def test_no_visa_block_means_nothing_is_filled_and_every_visa_field_is_still_flagged(page, applicant, capsys):
    applicant.pop("visa")
    fill_page(page, applicant, None, None)
    assert page.input_value("#free") == "" and page.input_value("#future") == ""
    assert capsys.readouterr().out.count("-> EMPTY") == 7                                 # 5 fields + 2 radio groups


def test_other_never_fill_matches_stay_empty_and_are_orange_not_visa_coloured(page, applicant, capsys):
    r = fill_page(page, applicant, None, None)
    assert page.input_value("#salary") == "" and page.input_value("#nat") == ""
    assert "Salary expectations" in r["manual"] and "Nationality" in r["manual"]
    assert _outline(page, "#salary") == "rgb(255, 165, 0)" and _outline(page, "#nat") == "rgb(255, 165, 0)"
    assert "Salary" not in capsys.readouterr().out                                        # not a visa field: no CHECK line


def test_an_existing_answer_in_a_visa_text_box_is_not_overwritten(page, applicant, capsys):
    page.fill("#free", "Already typed")
    fill_page(page, applicant, None, None)
    assert page.input_value("#free") == "Already typed"
    assert "visa or immigration status -> Already typed" in capsys.readouterr().out
