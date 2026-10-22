"""Apply assist, equal-opportunities questions, on local forms (Chromium; no network): only a question listed in applicant.yaml
`equal_opportunities` is answered, with the first form option equal to a match_any text; no match or an unlisted question stays empty; every
such field is outlined blue and printed as CHECK EO ANSWER; nothing is inferred and nothing is submitted. Chromium missing = the test fails
(a skip would hide a broken assist)."""
import html

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops.apply import answers
from careerops.apply.assist import fill_page

pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)
BLUE = "rgb(0, 0, 255)"
UK_STYLE = ["Please select", "White – English, Welsh, Scottish, Northern Irish or British", "Mixed – White and Asian",
            "Asian or Asian British – Indian", "Asian or Asian British – Pakistani", "Black or Black British – African", "Prefer not to say"]
US_STYLE = ["Hispanic or Latino", "White", "Black or African American", "American Indian or Alaska Native", "Asian",
            "Native Hawaiian or Other Pacific Islander", "Two or More Races"]


@pytest.fixture
def applicant():
    return yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        yield pg
        browser.close()


def select(id_, label, options):
    return (f'<label for="{id_}">{html.escape(label)}</label><select id="{id_}" name="{id_}">'
            + "".join(f"<option>{html.escape(o)}</option>" for o in options) + "</select>")


def radios(name, legend, options):
    return (f'<fieldset id="{name}"><legend>{html.escape(legend)}</legend>'
            + "".join(f'<label><input type="radio" name="{name}" value="{i}"> {html.escape(o)}</label>' for i, o in enumerate(options))
            + "</fieldset>")


def form(page, *parts):
    page.set_content("<html><body><form id='f'>" + "".join(parts) + "<button type='submit' id='submit'>Submit</button></form>"
                     "<script>document.getElementById('f').addEventListener('submit', e => { e.preventDefault(); document.body.dataset.submitted='1'; });"
                     "</script></body></html>")


def outline(page, selector):
    return page.evaluate("s => getComputedStyle(document.querySelector(s)).outlineColor", selector)


def checked(page, name):
    return page.evaluate("n => { const r = document.querySelector(`input[name=${n}]:checked`); "
                         "return r ? r.parentElement.innerText.trim() : null }", name)


def test_a_uk_style_list_gets_the_asian_indian_option_even_with_an_en_dash(page, applicant, capsys):
    form(page, select("eth", "What is your ethnicity?", UK_STYLE))
    r = fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#eth", "e => e.value") == "Asian or Asian British – Indian"
    assert "CHECK EO ANSWER [ethnicity]: What is your ethnicity? -> Asian or Asian British – Indian" in capsys.readouterr().out
    assert r["eo"] == [{"question": "What is your ethnicity?", "key": "ethnicity", "value": "Asian or Asian British – Indian", "options": UK_STYLE}]
    assert outline(page, "#eth") == BLUE
    assert any("= eo" in f for f in r["filled"]) and page.evaluate("document.body.dataset.submitted") is None      # nothing submitted


def test_a_us_style_radio_list_gets_asian_and_never_american_indian(page, applicant, capsys):
    form(page, radios("race", "Race / Ethnicity", US_STYLE))
    r = fill_page(page, applicant, None, None)
    assert checked(page, "race") == "Asian"
    assert "CHECK EO ANSWER [ethnicity]: Race / Ethnicity -> Asian" in capsys.readouterr().out
    assert r["eo"][0]["value"] == "Asian"
    assert outline(page, "input[name=race]") == BLUE                              # the whole group is highlighted


def test_a_list_with_no_matching_option_is_left_empty_and_printed_with_the_options(page, applicant, capsys):
    form(page, select("eth", "Ethnicity", ["", "White", "Black", "Mixed", "Other", "Prefer not to say"]))
    r = fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#eth", "e => e.value") == ""
    out = capsys.readouterr().out
    assert "CHECK EO ANSWER [ethnicity]: Ethnicity -> EMPTY (no option matched match_any: left blank; options: White | Black | Mixed | Other | Prefer not to say)" in out
    assert r["eo"][0]["value"] is None and "Ethnicity" in r["manual"] and outline(page, "#eth") == BLUE


def test_a_radio_list_with_no_match_checks_nothing(page, applicant, capsys):
    form(page, radios("eth", "Ethnicity", ["White", "Black", "Mixed", "Prefer not to say"]))
    fill_page(page, applicant, None, None)
    assert checked(page, "eth") is None
    assert "-> EMPTY (no option matched" in capsys.readouterr().out


def test_a_question_that_is_not_listed_stays_empty_whatever_else_is_known(page, applicant, capsys):
    form(page, select("gen", "Gender", ["", "Man", "Woman", "Non-binary", "Prefer not to say"]),
         select("dis", "Do you have a disability?", ["", "Yes", "No", "Prefer not to say"]),
         radios("rel", "Religion or belief", ["None", "Christian", "Hindu", "Prefer not to say"]),
         '<label for="pn">Preferred pronouns</label><input id="pn" name="pn">')
    r = fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#gen", "e => e.value") == "" and page.eval_on_selector("#dis", "e => e.value") == ""
    assert checked(page, "rel") is None and page.input_value("#pn") == ""
    out = capsys.readouterr().out
    for q in ("Gender", "Do you have a disability?", "Religion or belief", "Preferred pronouns"):
        assert f"CHECK EO ANSWER [not listed]: {q} -> EMPTY (not listed in equal_opportunities: left blank)" in out
    assert [e["key"] for e in r["eo"]] == [None] * 4 and {"Gender", "Preferred pronouns"} <= set(r["manual"])
    for sel in ("#gen", "#dis", "input[name=rel]", "#pn"):
        assert outline(page, sel) == BLUE


def test_without_an_equal_opportunities_block_everything_is_blank_even_ethnicity(page, applicant, capsys):
    del applicant["equal_opportunities"]
    form(page, select("eth", "Ethnicity", UK_STYLE))
    fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#eth", "e => e.value") == "Please select"
    assert "CHECK EO ANSWER [not listed]: Ethnicity -> EMPTY" in capsys.readouterr().out


def test_a_text_box_for_ethnicity_is_left_empty_not_typed_into(page, applicant, capsys):
    form(page, '<label for="eth">Ethnicity</label><input id="eth" name="eth">')
    fill_page(page, applicant, None, None)
    assert page.input_value("#eth") == ""
    assert "CHECK EO ANSWER [ethnicity]: Ethnicity -> EMPTY (no options to choose from (a text box): left blank)" in capsys.readouterr().out


def test_the_match_any_order_beats_the_order_of_the_form(page, applicant):
    form(page, select("eth", "Ethnicity", ["Asian", "Indian", "Asian or Asian British - Indian"]))
    fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#eth", "e => e.value") == "Asian or Asian British - Indian"


def test_an_invalid_block_fills_nothing(page, applicant, capsys):
    applicant["equal_opportunities"]["ethnicity"]["match_any"] = []
    form(page, select("eth", "Ethnicity", UK_STYLE))
    fill_page(page, applicant, None, None)
    assert page.eval_on_selector("#eth", "e => e.value") == "Please select"
    assert "[not listed]" in capsys.readouterr().out


def test_other_fields_are_untouched_and_visa_and_eo_do_not_mix(page, applicant, capsys):
    form(page, '<label for="first_name">First Name</label><input id="first_name" name="first_name">',
         select("sp", "Will you now or in the future require visa sponsorship?", ["", "Yes", "No"]),
         select("eth", "Ethnicity", UK_STYLE), '<label for="nat">Nationality</label><input id="nat" name="nat">')
    fill_page(page, applicant, None, None)
    out = capsys.readouterr().out
    assert page.input_value("#first_name") == "Ada" and page.eval_on_selector("#sp", "e => e.value") == "Yes"
    assert "CHECK VISA ANSWER" in out and "CHECK EO ANSWER [ethnicity]" in out and page.input_value("#nat") == ""
    assert "CHECK EO ANSWER" not in out.split("CHECK EO ANSWER", 1)[1]                                   # one EO line: the ethnicity select


# ------------------------------------------------------------------ a free-text box for an eo_* question gets the answers.yaml answer
def gender_book(answer="Male"):
    return answers.AnswerBook([answers.Entry("eo_gender", ("gender", "sex"), answer, None, {}, ("Man", "Male"))])


def test_a_gender_text_box_is_typed_with_the_answers_yaml_answer(page, applicant, capsys):
    form(page, '<label for="gen">Gender</label><input id="gen" name="gen">')
    r = fill_page(page, applicant, None, None, book=gender_book())
    assert page.input_value("#gen") == "Male"
    assert "CHECK EO ANSWER [eo_gender]: Gender -> Male" in capsys.readouterr().out
    assert r["eo"] == [{"question": "Gender", "key": "eo_gender", "value": "Male", "options": []}]
    assert "Gender = eo" in r["filled"] and outline(page, "#gen") == BLUE and page.evaluate("document.body.dataset.submitted") is None


def test_a_gender_text_area_is_typed_too(page, applicant):
    form(page, '<label for="gen">What is your gender?</label><textarea id="gen" name="gen"></textarea>')
    fill_page(page, applicant, None, None, book=gender_book("Female"))
    assert page.input_value("#gen") == "Female"


def test_an_eo_text_box_that_already_has_text_is_left_as_it_is(page, applicant, capsys):
    form(page, '<label for="gen">Gender</label><input id="gen" name="gen" value="my own words">')
    r = fill_page(page, applicant, None, None, book=gender_book())
    assert page.input_value("#gen") == "my own words" and r["eo"][0]["value"] is None
    assert "-> EMPTY (the text box already holds an answer: left as it is)" in capsys.readouterr().out


def test_an_unlisted_eo_text_box_stays_empty_even_when_another_question_is_listed(page, applicant, capsys):
    form(page, '<label for="pn">Preferred pronouns</label><input id="pn" name="pn">')
    fill_page(page, applicant, None, None, book=gender_book())
    assert page.input_value("#pn") == "" and "[not listed]: Preferred pronouns -> EMPTY" in capsys.readouterr().out


def test_an_eo_entry_with_a_blank_answer_types_nothing(page, applicant):
    form(page, '<label for="gen">Gender</label><input id="gen" name="gen">')
    fill_page(page, applicant, None, None, book=answers.AnswerBook([answers.Entry("eo_gender", ("gender",), "", None, {}, ("Man", "Male"))]))
    assert page.input_value("#gen") == ""
