"""Fill order and read-back (apply/verify.py), round of 22 Oct (the loveholidays Ashby form): the "Autofill from resume" control never gets a file;
files go first and the form is left to settle before any text field is filled; every field filled is read back, an empty one is filled once
more, and VERIFIED / LOST is printed (LOST = REQUIRED + EMPTY); a cover-letter text box gets the letter text and a cover-letter file field the
PDF, in the assist and in the auto-apply. Local forms in headless Chromium (no network, nothing submitted); Chromium missing = the tests fail."""
import time
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import verify
from careerops.apply.assist import fill_page

pytestmark = pytest.mark.slow          # these tests drive Chromium
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
BOOK = answers_mod.load(PROFILE_FIX / "answers.yaml")
JOB = {"id": "ashby:loveholidays:1", "company": "loveholidays", "title": "Data Scientist", "source": "ashby"}
LETTER = "Dear loveholidays team,\n\nI would like to work on your pricing models.\n\nCould we take 20 minutes to talk about demand forecasting?"
UPLOADS_JS = "() => window.__uploads"

AUTOFILL = ('<div class="autofill"><h2>Autofill from resume</h2><p>Upload your resume and we fill the form for you.</p>'
            '<input type="file" id="u1" name="upload1"></div>')
RESUME = '<div><label for="_systemfield_resume">Resume</label><input type="file" id="_systemfield_resume" name="_systemfield_resume"></div>'
FIELDS = ('<label for="nm">Name</label><input id="nm" name="_systemfield_name" type="text">'
          '<label for="em">Email</label><input id="em" name="_systemfield_email" type="email">'
          '<label for="sal">Salary expectation</label><input id="sal" name="salary" type="text">'
          '<label for="np">Notice period</label><input id="np" name="notice" type="text">')
# parsing a resume re-renders the form: every box is emptied half a second after a file is chosen, and the DOM changes while it happens
PARSES = """
  window.__uploads = {};
  document.querySelectorAll('input[type=file]').forEach(f => f.addEventListener('change', () => {
    window.__uploads[f.id] = f.files[0] ? f.files[0].name : '';
    document.getElementById('status').textContent = 'Parsing your resume...';
    setTimeout(() => {
      document.querySelectorAll('#application input:not([type=file]), #application textarea').forEach(e => { e.value = ''; });
      document.getElementById('status').textContent = 'Parsed';
    }, 500);
  }));"""
UPLOADS_ONLY = "window.__uploads = {}; document.querySelectorAll('input[type=file]').forEach(f => f.addEventListener('change', () => { window.__uploads[f.id] = f.files[0].name; }));"


def form(page, *parts, script=UPLOADS_ONLY):
    page.set_content("<html><body><p id='status'></p><form id='application' novalidate>" + "".join(parts)
                     + "<button type='submit' id='submit'>Submit</button></form>"
                     "<script>document.getElementById('application').addEventListener('submit', e => { e.preventDefault(); document.body.dataset.submitted='1'; });"
                     + script + "</script></body></html>")


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        yield browser
        browser.close()


@pytest.fixture
def page(chromium):
    pg = chromium.new_page()
    yield pg
    pg.close()


@pytest.fixture
def pdfs(tmp_path):
    cv, letter = tmp_path / "Ada Lovelace CV.pdf", tmp_path / "Ada Lovelace Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(cv), str(letter)


def ctx_for(cv=None, letter_pdf=None, letter_text=None, lines=None):
    out = (lambda s: lines.append(s)) if lines is not None else (lambda s: None)
    return autoapply.Ctx(JOB, APPLICANT, BOOK, Path("answers.yaml"), None, cv, letter_pdf, letter_text, ask=lambda p: "", out=out,
                         history=lambda c: False, result=type("R", (), {"filled": [], "blank": []})())


# ------------------------------------------------------------------ the autofill control is never used
def test_the_autofill_upload_never_gets_the_cv_only_the_real_resume_field(page, pdfs, capsys):
    form(page, AUTOFILL, FIELDS, RESUME)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    assert page.evaluate(UPLOADS_JS) == {"_systemfield_resume": "Ada Lovelace CV.pdf"}
    assert r["uploaded"] == ["Resume ← CV"]
    assert len(r["skipped"]) == 1 and r["skipped"][0].endswith("(autofill)")
    assert "skipped the autofill upload control (never used)" in capsys.readouterr().out
    assert page.evaluate("document.body.dataset.submitted") is None


def test_a_form_with_only_the_autofill_control_gets_no_file_at_all(page, pdfs):
    form(page, AUTOFILL, FIELDS)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    assert page.evaluate(UPLOADS_JS) == {} and r["uploaded"] == []


@pytest.mark.parametrize("markup", [
    '<input type="file" id="x" name="autofill_upload">',
    '<label for="x">Autofill with your CV</label><input type="file" id="x" name="up">',
    '<section><h3>Autofill from resume</h3><span>Save time</span><input type="file" id="x" name="up"></section>',
])
def test_is_autofill_recognises_the_control_by_its_name_its_label_or_the_block_around_it(page, markup):
    form(page, markup)
    assert verify.is_autofill(page.query_selector("#x")) is True


def test_is_autofill_does_not_mistake_the_resume_field_next_to_the_autofill_block(page):
    form(page, '<div class="wrap"><div><h3>Autofill from resume</h3><input type="file" id="a" name="up1"></div>'
               '<div><label for="r">Resume</label><input type="file" id="r" name="up2"></div></div>')
    assert verify.is_autofill(page.query_selector("#a")) is True and verify.is_autofill(page.query_selector("#r")) is False


# ------------------------------------------------------------------ files first, then wait, then the text fields
def test_a_form_that_clears_its_fields_after_an_upload_still_ends_with_salary_and_notice_filled(page, pdfs, capsys):
    form(page, AUTOFILL, FIELDS, RESUME, script=PARSES)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=1000)         # the form is quiet 0.5 s after the wipe: waits for it
    page.wait_for_timeout(1000)                                                   # a wipe that came after the fill would show now
    v = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('#application input:not([type=file])')].map(e => [e.name, e.value]))")
    assert v["salary"].startswith("I'd welcome a discussion about salary") and v["notice"] == "Available immediately"
    assert v["_systemfield_name"] == "Ada Lovelace" and v["_systemfield_email"] == APPLICANT["email"]
    assert page.evaluate(UPLOADS_JS) == {"_systemfield_resume": "Ada Lovelace CV.pdf"}          # and the autofill control was never used
    out = capsys.readouterr().out
    assert "LOST" not in out and "filled again" not in out and r["lost"] == [] and len(r["verified"]) == 4
    assert r["required_empty"] == []


def test_the_text_fields_are_filled_after_the_files_even_when_the_file_field_is_last_in_the_page(page, pdfs):
    form(page, FIELDS, RESUME, script=UPLOADS_ONLY + "document.querySelectorAll('input').forEach(i => i.addEventListener(i.type === 'file' ? 'change' : 'input', "
                                                    "() => (window.__order = window.__order || []).push(i.type)));")
    fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    order = page.evaluate("() => window.__order")
    assert order[0] == "file" and "file" not in order[1:]


# ------------------------------------------------------------------ read back, fill once more, report
WIPE_ONCE = """document.getElementById('np').addEventListener('input', e => { if (!window.__wiped) { window.__wiped = 1; e.target.value = ''; } });"""
WIPE_ALWAYS = """document.getElementById('np').addEventListener('input', e => { e.target.value = ''; });"""


def test_a_field_the_form_emptied_is_filled_once_more_and_reported_as_verified(page, pdfs, capsys):
    form(page, FIELDS, RESUME, script=UPLOADS_ONLY + WIPE_ONCE)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    assert page.input_value("#np") == "Available immediately" and r["lost"] == []
    out = capsys.readouterr().out
    assert "VERIFIED  Notice period (filled again)" in out and "VERIFIED  Salary expectation\n" in out and "LOST" not in out


def test_a_field_that_stays_empty_is_lost_and_listed_under_required_empty(page, pdfs, capsys):
    form(page, FIELDS, RESUME, script=UPLOADS_ONLY + WIPE_ALWAYS)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    assert page.input_value("#np") == "" and r["lost"] == ["Notice period"] and "Notice period" in r["required_empty"]
    assert "Notice period" not in r["verified"] and "Salary expectation" in r["verified"]
    out = capsys.readouterr().out
    assert "LOST      Notice period" in out and "VERIFIED  Email" in out
    assert page.eval_on_selector("#np", "e => getComputedStyle(e).outlineColor") == "rgb(255, 0, 0)"


def test_a_lost_field_is_listed_once_even_when_it_was_already_required_and_empty(page, pdfs):
    form(page, FIELDS.replace('id="np"', 'id="np" required'), RESUME, script=UPLOADS_ONLY + WIPE_ALWAYS)
    r = fill_page(page, APPLICANT, pdfs[0], None, book=BOOK, quiet_ms=50)
    assert r["required_empty"].count("Notice period") == 1


def test_a_select_the_form_resets_is_selected_again(page, pdfs, capsys):
    form(page, '<label for="rel">Are you willing to relocate?</label><select id="rel" name="rel"><option value="">Please select</option>'
               '<option>Yes</option><option>No</option></select>',
         script="document.getElementById('rel').addEventListener('change', e => { if (!window.__w) { window.__w = 1; e.target.value = ''; } });")
    r = fill_page(page, APPLICANT, None, None, book=BOOK, quiet_ms=50)
    assert page.eval_on_selector("#rel", "e => e.value") == "Yes" and r["lost"] == []
    assert "VERIFIED  Are you willing to relocate? (filled again)" in capsys.readouterr().out


def test_wait_quiet_returns_once_the_page_has_stopped_changing(page):
    form(page, "", script="let n = 0; const t = setInterval(() => { document.getElementById('status').textContent = ++n; if (n >= 10) clearInterval(t); }, 100);")
    start = time.monotonic()
    assert verify.wait_quiet(page, quiet_ms=400, limit_ms=10000) is True
    assert 1.0 <= time.monotonic() - start < 8                                    # ten changes at 100 ms, then 400 ms of quiet


def test_wait_quiet_gives_up_on_a_page_that_never_settles(page):
    form(page, "", script="let n = 0; setInterval(() => { document.getElementById('status').textContent = ++n; }, 50);")
    for _ in range(20):                                                           # under load a single run once said True
        assert verify.wait_quiet(page, quiet_ms=500, limit_ms=1200) is False


# ------------------------------------------------------------------ the auto-apply fills in the same order
def test_the_auto_apply_skips_the_autofill_control_waits_and_reads_back(page, pdfs):
    lines: list[str] = []
    form(page, AUTOFILL, FIELDS, RESUME, script=PARSES)
    ctx = ctx_for(cv=pdfs[0], lines=lines)
    autoapply.fill_form(page, ctx, quiet_ms=1000)
    assert page.evaluate(UPLOADS_JS) == {"_systemfield_resume": "Ada Lovelace CV.pdf"}
    assert page.input_value("#sal").startswith("I'd welcome") and page.input_value("#np") == "Available immediately"
    assert autoapply.verify_filled(page, ctx) == []
    assert any("VERIFIED  Notice period" in s for s in lines) and any("skipped the autofill upload control" in s for s in lines)


def test_the_auto_apply_reports_a_field_the_form_keeps_emptying(page, pdfs):
    form(page, FIELDS, RESUME, script=UPLOADS_ONLY + WIPE_ALWAYS)
    ctx = ctx_for(cv=pdfs[0])
    autoapply.fill_form(page, ctx, quiet_ms=50)
    assert autoapply.verify_filled(page, ctx) == ["Notice period"]


# ------------------------------------------------------------------ the cover letter: text box -> text, file field -> PDF
COVER_TEXT = '<label for="clt">Cover letter</label><textarea id="clt" name="clt"></textarea>'
COVER_FILE = '<label for="clf">Cover letter</label><input type="file" id="clf" name="clf">'


def test_the_assist_pastes_the_letter_into_a_cover_letter_text_box_and_it_survives_the_upload_wipe(page, pdfs):
    form(page, COVER_TEXT, FIELDS, RESUME, script=PARSES)
    r = fill_page(page, APPLICANT, pdfs[0], pdfs[1], letter_text=LETTER, book=BOOK, quiet_ms=1000)
    assert page.input_value("#clt") == LETTER and "Cover letter = cover letter text" in r["filled"] and "Cover letter" in r["verified"]
    assert page.evaluate(UPLOADS_JS) == {"_systemfield_resume": "Ada Lovelace CV.pdf"}          # the PDF is for a file field only


def test_the_assist_uploads_the_letter_pdf_to_a_cover_letter_file_field(page, pdfs):
    form(page, COVER_FILE, FIELDS, RESUME)
    r = fill_page(page, APPLICANT, pdfs[0], pdfs[1], letter_text=LETTER, book=BOOK, quiet_ms=50)
    assert page.evaluate(UPLOADS_JS) == {"clf": "Ada Lovelace Cover Letter.pdf", "_systemfield_resume": "Ada Lovelace CV.pdf"}
    assert "Cover letter ← letter" in r["uploaded"]


def test_the_auto_apply_pastes_the_letter_into_a_text_box(page, pdfs):
    form(page, COVER_TEXT, FIELDS, RESUME, script=PARSES)
    ctx = ctx_for(cv=pdfs[0], letter_pdf=pdfs[1], letter_text=LETTER)
    autoapply.fill_form(page, ctx, quiet_ms=1000)
    assert page.input_value("#clt") == LETTER and autoapply.verify_filled(page, ctx) == []


def test_the_auto_apply_uploads_the_letter_pdf_to_a_file_field(page, pdfs):
    form(page, COVER_FILE, FIELDS, RESUME)
    ctx = ctx_for(cv=pdfs[0], letter_pdf=pdfs[1], letter_text=LETTER)
    autoapply.fill_form(page, ctx, quiet_ms=50)
    assert page.evaluate(UPLOADS_JS) == {"clf": "Ada Lovelace Cover Letter.pdf", "_systemfield_resume": "Ada Lovelace CV.pdf"}
