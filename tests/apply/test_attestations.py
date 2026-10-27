"""Attestations ("I acknowledge the privacy notice", "I confirm all answers are true") are never answered by code, even when answers.yaml has a Yes
for them: they are outlined magenta and listed for the human, and the auto-apply does not submit until they are set (the required check box is
still empty). Next to an upload the PDF is uploaded and "Enter manually" is never used. A fixture form in headless Chromium."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import attest, filler

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
JOB = {"id": "greenhouse:acme:1", "company": "Acme", "title": "Data Scientist", "source": "greenhouse"}
STATE_JS = "() => ({truth: truth.checked, privacy: privacy.checked, newsletter: newsletter.checked, clicks: window.__clicks, file: (resume.files[0] || {}).name || ''})"


def yes_book(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("answers:\n  - {id: privacy, match: ['privacy notice', 'acknowledge'], answer: 'Yes'}\n"
                 "  - {id: truth, match: ['answers are true', 'confirm all answers'], answer: 'Yes'}\n"
                 "  - {id: alerts, match: ['job alerts'], answer: 'No'}\n", encoding="utf-8")
    return answers_mod.load(p)


@pytest.mark.parametrize("text", [
    "I acknowledge the privacy notice", "I acknowledge that I have read the Privacy Policy", "I confirm all answers are true and complete",
    "I confirm that the information I have provided is accurate", "I certify that my answers are correct", "I agree to the terms and conditions",
    "I consent to the processing of my personal data", "By ticking you accept the data protection statement", "I declare the above is true",
])
def test_these_are_attestations(text):
    assert attest.is_attestation(text), text


@pytest.mark.parametrize("text", ["Send me job alerts", "I have a UK driving licence", "Are you over 18?", "Do you require sponsorship?", "Remote", "First name"])
def test_these_are_not(text):
    assert not attest.is_attestation(text), text


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(chromium):
    pg = chromium.new_page()
    pg.goto((FIX / "attest_form.html").resolve().as_uri())
    yield pg
    pg.close()


@pytest.fixture
def cv(tmp_path):
    p = tmp_path / "Ada Lovelace CV.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(p)


@pytest.mark.slow
def test_the_assist_never_ticks_an_attestation_even_when_answers_yaml_says_yes_and_lists_both_in_magenta(page, cv, tmp_path, capsys):
    report = assist_mod.fill_page(page, APPLICANT, cv, None, job=None, book=yes_book(tmp_path))
    s = page.evaluate(STATE_JS)
    assert s["truth"] is False and s["privacy"] is False
    assert len(report["attestations"]) == 2
    assert any("true and complete" in a for a in report["attestations"]) and any("privacy notice" in a for a in report["attestations"])
    assert "magenta" in page.evaluate("() => truth.style.outline") and "magenta" in page.evaluate("() => privacy.style.outline")
    assert any("true and complete" in x for x in report["required_empty"])          # still required and empty: the human sets it


@pytest.mark.slow
def test_a_check_box_that_is_not_an_attestation_still_follows_answers_yaml(page, cv, tmp_path):
    assist_mod.fill_page(page, APPLICANT, cv, None, job=None, book=yes_book(tmp_path))
    assert page.evaluate(STATE_JS)["newsletter"] is False                            # the answer is No: left unticked


@pytest.mark.slow
def test_the_auto_apply_fill_does_not_tick_them_and_the_form_is_not_complete(page, cv, tmp_path):
    ctx = filler.Ctx(JOB, APPLICANT, yes_book(tmp_path), Path("answers.yaml"), None, cv, None, None, ask=lambda p: "", out=lambda s: None,
                     history=lambda c: False, result=type("R", (), {"filled": [], "blank": []})())
    autoapply.fill_form(page, ctx)
    s = page.evaluate(STATE_JS)
    assert s["truth"] is False and s["privacy"] is False
    assert len(ctx.attestations) == 2
    empty = autoapply.still_empty(page)
    assert any("true and complete" in x for x in empty) and any("privacy notice" in x for x in empty)        # so the run stops: the human decides
    assert "SUBMITTED" not in s["clicks"]


@pytest.mark.slow
def test_the_pdf_is_uploaded_next_to_the_upload_buttons_and_enter_manually_is_never_pressed(page, cv, tmp_path):
    report = assist_mod.fill_page(page, APPLICANT, cv, None, job=None, book=yes_book(tmp_path))
    s = page.evaluate(STATE_JS)
    assert s["file"] == Path(cv).name and report["uploaded"]
    assert s["clicks"] == []                                                          # not Attach, not Dropbox, not Enter manually


def test_a_final_report_names_the_attestations(capsys):
    lines = assist_mod.attestation_lines(["I confirm all answers are true", "I acknowledge the privacy notice"])
    text = "\n".join(lines)
    assert "ATTESTATIONS" in text and "\033[35m" in text and "privacy notice" in text and "auto-submit waits" in text
    assert assist_mod.attestation_lines([]) == []
