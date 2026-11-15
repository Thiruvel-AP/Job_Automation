"""Apply assist, cover letter: the letter PDF goes to a cover-letter FILE field (printed `Cover Letter ← letter`); a form that has a cover-letter
TEXT box instead gets the letter text; a form with both gets the file only. Local forms in Chromium (no network); nothing is submitted. Chromium
missing = the test fails (a skip would hide a broken assist)."""
import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS

from careerops.apply.assist import fill_page

pytestmark = pytest.mark.slow          # these tests drive Chromium
LETTER = "Dear Acme team,\n\nI would like to work at Acme.\n\nCould we take 20 minutes to talk about forecasting?"
COVER_FILE = '<label for="cl">Cover Letter</label><input type="file" id="cl" name="cl">'
COVER_TEXT = '<label for="clt">Cover Letter</label><textarea id="clt" name="clt"></textarea>'
RESUME = '<label for="cv">Resume/CV</label><input type="file" id="cv" name="cv">'


@pytest.fixture
def applicant():
    return yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        pg = browser.new_page()
        yield pg
        browser.close()


@pytest.fixture
def pdfs(tmp_path):
    cv, letter = tmp_path / "Ada Lovelace CV.pdf", tmp_path / "Ada Lovelace Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(cv), str(letter)


def form(page, *parts):
    page.set_content("<html><body><form id='f'>" + "".join(parts) + "<button type='submit' id='submit'>Submit</button></form>"
                     "<script>document.getElementById('f').addEventListener('submit', e => { e.preventDefault(); document.body.dataset.submitted='1'; });"
                     "</script></body></html>")


def file_name(page, selector):
    return page.eval_on_selector(selector, "e => e.files.length ? e.files[0].name : ''")


def test_the_letter_pdf_goes_to_the_cover_letter_file_field(page, applicant, pdfs):
    form(page, COVER_FILE, RESUME)
    r = fill_page(page, applicant, pdfs[0], pdfs[1], letter_text=LETTER)
    assert file_name(page, "#cl") == "Ada Lovelace Cover Letter.pdf" and file_name(page, "#cv") == "Ada Lovelace CV.pdf"
    assert r["uploaded"] == ["Cover Letter ← letter", "Resume/CV ← CV"]
    assert page.evaluate("document.body.dataset.submitted") is None


def test_a_cover_letter_text_box_gets_the_letter_text(page, applicant, pdfs):
    form(page, COVER_TEXT, RESUME)
    r = fill_page(page, applicant, pdfs[0], pdfs[1], letter_text=LETTER)
    assert page.input_value("#clt") == LETTER and "Cover Letter = cover letter text" in r["filled"]
    assert r["uploaded"] == ["Resume/CV ← CV"]


def test_with_both_the_file_gets_the_letter_and_the_text_box_stays_empty(page, applicant, pdfs):
    form(page, COVER_TEXT, COVER_FILE, RESUME)                  # the text box comes first in the page on purpose
    r = fill_page(page, applicant, pdfs[0], pdfs[1], letter_text=LETTER)
    assert file_name(page, "#cl") == "Ada Lovelace Cover Letter.pdf" and page.input_value("#clt") == ""
    assert "Cover Letter ← letter" in r["uploaded"]


def test_without_a_letter_pdf_a_text_box_still_gets_the_text(page, applicant, pdfs):
    form(page, COVER_TEXT, COVER_FILE, RESUME)
    r = fill_page(page, applicant, pdfs[0], None, letter_text=LETTER)
    assert page.input_value("#clt") == LETTER and "file: Cover Letter" in r["manual"]          # the empty file field is left to the person


def test_a_text_box_that_already_holds_text_is_not_overwritten(page, applicant, pdfs):
    form(page, '<label for="clt">Cover Letter</label><textarea id="clt" name="clt">my own words</textarea>')
    fill_page(page, applicant, pdfs[0], pdfs[1], letter_text=LETTER)
    assert page.input_value("#clt") == "my own words"
