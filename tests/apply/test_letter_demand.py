"""F1 (v2.4.1): a cover letter when the form asks for one (letter.mode if_field). RightShip's Greenhouse form has a Cover Letter block (Attach / Dropbox /
Google Drive / Enter manually) and the run said "no letter". With the control found and no letter on file, the maker writes one now; if it fails its guards the
exact reason is printed (optional: left empty; required: hand-off); a file input gets the PDF through the input itself, "Enter manually" gets clicked and
letter.txt pasted; the Dropbox and Google Drive buttons are never clicked. Local forms in Chromium, no network, nothing submitted."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS

from careerops.apply import assist as assist_mod
from careerops.apply import letter_demand as LD
from careerops.apply.letter_demand import Made

pytestmark = pytest.mark.slow          # these tests drive Chromium

LETTER = "Hi RightShip team,\n\nThe fleet data problem is yours; mine was an LLM agent behind 65 tests.\n\nRegards,\n\nAda Lovelace"
REASON = "3 draft(s) rejected by the company guards; last: P4: number(s) about you not in the chosen bullets: ['20%']"

BUTTONS = ('<div><button type="button">Attach</button><button type="button" onclick="window.__clicked.push(\'dropbox\')">Dropbox</button>'
           '<button type="button" onclick="window.__clicked.push(\'drive\')">Google Drive</button>'
           '<button type="button" id="manual" onclick="document.getElementById(\'cl_text\').style.display=\'block\'">Enter manually</button></div>')
RESUME = '<div class="f"><h4>Resume/CV*</h4><label for="resume">Resume/CV</label><input type="file" id="resume" name="resume"></div>'


def cover_block(file_input: bool, required: bool) -> str:
    star = "*" if required else ""
    inp = '<input type="file" id="cover_letter" name="cover_letter" style="display:none">' if file_input else ""
    area = f'<textarea id="cl_text" aria-label="Cover Letter" {"required" if required else ""} style="display:none"></textarea>'
    return f'<div class="f"><h4>Cover Letter{star}</h4>{BUTTONS}{inp}{area}</div>'


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
    cv, letter = tmp_path / "Ada CV.pdf", tmp_path / "Ada Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter.write_bytes(b"%PDF-1.4\n%%EOF")
    (tmp_path / "letter.txt").write_text(LETTER, encoding="utf-8")
    return str(cv), str(letter)


def form(page, *parts):
    page.set_content("<html><body><form id='f'>" + "".join(parts) + "<button type='submit'>Submit</button></form><script>window.__clicked = [];</script></body></html>")


def clicked(page) -> list:
    return page.evaluate("window.__clicked")


def file_name(page, selector):
    return page.eval_on_selector(selector, "e => e.files.length ? e.files[0].name : ''")


# ------------------------------------------------------------------ finding the control
def test_the_cover_letter_file_input_is_found_and_the_block_is_required_or_not(page):
    form(page, RESUME, cover_block(True, False))
    ctl = LD.find(page)
    assert ctl.file_input and ctl.manual_button and ctl.any and not ctl.required


def test_a_form_with_no_cover_letter_control_has_none(page):
    form(page, RESUME)
    assert not LD.find(page).any


def test_a_block_with_only_enter_manually_is_found(page):
    form(page, RESUME, cover_block(False, True))
    ctl = LD.find(page)
    assert ctl.manual_button and not ctl.file_input and ctl.required


# ------------------------------------------------------------------ providing the letter
def test_a_letter_that_exists_is_used_and_the_maker_is_not_called(page, pdfs):
    form(page, RESUME, cover_block(True, False))
    calls = []
    got = LD.provide(page, {}, pdfs[1], LETTER, lambda ctl: calls.append(ctl), lambda s: None)
    assert got.pdf == pdfs[1] and not got.made and calls == []


def test_without_a_letter_the_maker_writes_one_now_when_the_form_asks(page, pdfs):
    form(page, RESUME, cover_block(True, False))
    seen, out = [], []

    def maker(ctl):
        seen.append(ctl)
        return Made(pdfs[1], LETTER, "", {"letter": {"paragraphs": ["x"]}})
    got = LD.provide(page, {}, None, None, maker, out.append)
    assert len(seen) == 1 and seen[0].file_input and got.pdf == pdfs[1] and got.text == LETTER and got.made and not got.handoff
    assert any("written now because the form asks" in line for line in out)


def test_the_maker_is_not_asked_when_the_form_has_no_cover_letter_field(page):
    form(page, RESUME)
    got = LD.provide(page, {}, None, None, lambda ctl: pytest.fail("the form does not ask for a letter"), lambda s: None)
    assert got.pdf is None and not got.handoff


def test_a_guard_failure_prints_the_exact_reason_and_an_optional_field_stays_empty(page):
    form(page, RESUME, cover_block(True, False))
    out = []
    got = LD.provide(page, {}, None, None, lambda ctl: Made(None, None, REASON), out.append)
    assert got.pdf is None and not got.handoff and got.note == REASON
    assert any(REASON in line for line in out) and not any(line.strip().lower().endswith("no letter") for line in out)
    assert any("optional" in line for line in out)


def test_a_required_letter_that_fails_its_guards_is_handed_off_with_the_reason(page):
    form(page, RESUME, cover_block(True, True))
    out = []
    got = LD.provide(page, {}, None, None, lambda ctl: Made(None, None, REASON), out.append)
    assert got.handoff and REASON in got.handoff and any(REASON in line for line in out)


def test_with_no_maker_the_reason_on_file_is_printed(page):
    form(page, RESUME, cover_block(True, False))
    out = []
    LD.provide(page, {}, None, None, None, out.append, reason="letter.mode is `off`: set `letter.mode: if_field`")
    assert any("letter.mode: if_field" in line for line in out)


# ------------------------------------------------------------------ delivering it
def test_a_file_input_gets_the_pdf_through_the_input_and_no_drive_button_is_clicked(page, applicant, pdfs):
    form(page, RESUME, cover_block(True, False))
    got = LD.provide(page, {}, pdfs[1], LETTER, None, lambda s: None)
    r = assist_mod.fill_page(page, applicant, pdfs[0], got.pdf, letter_text=got.text, quiet_ms=50)
    assert file_name(page, "#cover_letter") == "Ada Cover Letter.pdf" and file_name(page, "#resume") == "Ada CV.pdf"
    assert "Cover Letter ← letter" in " ".join(r["uploaded"]) or any("letter" in u for u in r["uploaded"])
    assert clicked(page) == []                                                  # never Dropbox, never Google Drive
    assert page.eval_on_selector("#cl_text", "e => e.style.display") == "none"    # "Enter manually" was not needed


def test_with_only_enter_manually_the_button_is_clicked_and_letter_txt_is_pasted(page, pdfs):
    form(page, RESUME, cover_block(False, True))
    got = LD.provide(page, {}, pdfs[1], LETTER, None, lambda s: None)
    assert page.input_value("#cl_text") == LETTER and got.pdf == pdfs[1]
    assert clicked(page) == []


def test_letter_text_comes_from_letter_txt_beside_the_pdf(pdfs):
    assert LD.letter_text_of({}, pdfs[1]) == LETTER
    assert LD.letter_text_of({"letter": {"paragraphs": ["a", "b"]}}, None) == "a\n\nb"


def test_dropbox_and_drive_buttons_are_refused_by_the_click_guard():
    from careerops.apply import shortfields as sf
    assert sf.clickable_text("Enter manually")
    assert not sf.clickable_text("Dropbox") and not sf.clickable_text("Google Drive")
