"""Lever forms: check boxes and radio buttons are never set by code (a programmatic click triggers Lever's hCaptcha: the 22 Oct Valarian "error
verifying your application"). They are listed under YOU ANSWER with the recommended value. The same form on another host is filled as before. The
fixture is served by a Playwright route at https://jobs.lever.co/... so the page really is on a Lever host. Chromium missing = the tests fail."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import filler

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
HTML = (FIX / "lever_checks.html").read_text(encoding="utf-8")
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
JOB = {"id": "lever:valarian:1", "company": "Valarian", "title": "MLOps Engineer", "source": "lever"}
STATE_JS = "() => ({yes: document.querySelector('input[value=Yes]').checked, no: document.querySelector('input[value=No]').checked, licence: licence.checked})"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def open_at(chromium, url):
    pg = chromium.new_page()
    pg.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
    pg.goto(url)
    return pg


@pytest.fixture
def lever(chromium):
    pg = open_at(chromium, "https://jobs.lever.co/valarian/1/apply")
    yield pg
    pg.close()


@pytest.fixture
def elsewhere(chromium):
    pg = open_at(chromium, "https://careers.valarian.example/apply")
    yield pg
    pg.close()


@pytest.fixture
def cv(tmp_path):
    p = tmp_path / "Ada Lovelace CV.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(p)


@pytest.fixture
def book(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("answers:\n  - {id: licence, match: ['driving licence'], answer: 'Yes'}\n", encoding="utf-8")
    return answers_mod.load(p)


def test_on_a_lever_page_the_assist_sets_no_radio_and_no_check_box(lever, cv, book, capsys):
    report = assist_mod.fill_page(lever, APPLICANT, cv, None, job=None, book=book)
    assert lever.evaluate(STATE_JS) == {"yes": False, "no": False, "licence": False}
    assert lever.evaluate("() => window.__submits") == 0
    listed = " ".join(report["manual"])
    assert "right to work in the UK" in listed and "-> Yes" in listed                      # the visa plan's answer, recommended
    assert "driving licence" in listed
    assert any("right to work" in r and "Yes" in r for r in report["recommended"])
    assert any("driving licence" in r and "Yes" in r for r in report["recommended"])
    assert "Lever" in capsys.readouterr().out


def test_the_same_form_on_another_host_is_filled_as_before(elsewhere, cv, book):
    assist_mod.fill_page(elsewhere, APPLICANT, cv, None, job=None, book=book)
    assert elsewhere.evaluate(STATE_JS) == {"yes": True, "no": False, "licence": True}


def test_on_a_lever_page_the_auto_apply_sets_nothing_and_the_required_radio_stops_the_run(lever, cv, book):
    ctx = filler.Ctx(JOB, APPLICANT, book, Path("answers.yaml"), None, cv, None, None, ask=lambda p: "", out=lambda s: None,
                     history=lambda c: False, result=type("R", (), {"filled": [], "blank": []})())
    autoapply.fill_form(lever, ctx)
    assert lever.evaluate(STATE_JS) == {"yes": False, "no": False, "licence": False}
    assert any("driving licence" in r and "Yes" in r for r in ctx.recommended)
    assert any("right to work" in x for x in autoapply.still_empty(lever))            # required and empty: the human answers it
    assert lever.evaluate("() => window.__submits") == 0


def test_the_lever_check_is_by_host_not_by_a_flag_the_caller_can_forget():
    assert filler.lever_page(type("P", (), {"url": "https://jobs.lever.co/a/b/apply"})())
    assert filler.lever_page(type("P", (), {"url": "https://jobs.eu.lever.co/a/b/apply"})())
    assert not filler.lever_page(type("P", (), {"url": "https://boards.greenhouse.io/a/jobs/1"})())
    assert not filler.lever_page(type("P", (), {"url": "https://lever.co.evil.example/a"})())
    assert not filler.lever_page(object())
