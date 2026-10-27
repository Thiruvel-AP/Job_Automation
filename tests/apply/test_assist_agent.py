"""The assist's order of work: the direct ATS address, then the existing filler (Greenhouse, Lever, Ashby), then the form agent when no form is found or for
any other vendor; the Ashby email-merge warning; the pasted fallback file. Chromium for the real pages, a fake page for the order of addresses."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import formfind, knockout
from careerops.apply.visa import plan_for
from careerops.discovery import workcountry
from careerops.discovery.workcountry import Rules

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
BOOK = answers_mod.load(PROFILE_FIX / "answers.yaml")
PLAN = plan_for(APPLICANT, workcountry._mk(Rules(home="GB", label="Remote"), "GB", "onsite", "test"))
EPIC = "https://www.epicgames.com/site/en-US/careers/jobs/5678901004?gh_jid=5678901004"
VALUES_JS = "() => Object.fromEntries([...document.querySelectorAll('input,textarea')].map(e => [e.id, e.type === 'file' ? (e.files[0]||{}).name || '' : e.value]))"


@pytest.fixture
def cv(tmp_path):
    p = tmp_path / "Ada Lovelace CV.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(p)


@pytest.fixture(autouse=True)
def no_real_files_or_plan(monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: PLAN)


def job_for(url, tmp_path, **kw):
    return {"id": "x:acme:1", "company": "Acme", "title": "Data Scientist", "source": "manual", "location": "Bristol, UK", "description": "",
            "out_dir": str(tmp_path), "apply_url": url, "url": url, "apply_kind": "employer_other", **kw}


# ------------------------------------------------------------------ a fake page: the order of addresses
class El:
    def __init__(self, typ):
        self.typ = typ

    def is_visible(self):
        return True

    def get_attribute(self, name):
        return self.typ if name == "type" else None


class FakePage:
    """No evaluate(): not a browser page, so the form agent is not used."""

    def __init__(self):
        self.visited = []
        self.url = ""
        self.context = SimpleNamespace(pages=[self])

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, sel="body"):
        return ""

    def query_selector(self, sel):
        return None

    def query_selector_all(self, sel):
        return [El("email"), El("text")] if sel == formfind.FIELDS else []


def run_fake(monkeypatch, tmp_path, url, history=None, **jobkw):
    page = FakePage()
    monkeypatch.setattr(assist_mod, "fill_page", lambda *a, **k: {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []})
    r = assist_mod.assist(job_for(url, tmp_path, **jobkw), {"cv_pdf": None, "letter_pdf": None}, APPLICANT, tmp_path / "profile",
                          launch=lambda d: (page, lambda: None), ask=lambda p: "", book=BOOK, history=history)
    return page, r


def test_an_epic_style_job_is_opened_at_the_job_boards_address(monkeypatch, tmp_path):
    page, _ = run_fake(monkeypatch, tmp_path, EPIC, id="greenhouse:epicgames:5678901004")
    assert page.visited == ["https://job-boards.greenhouse.io/epicgames/jobs/5678901004"]


def test_the_board_token_can_also_come_from_the_known_boards(monkeypatch, tmp_path):
    page = FakePage()
    monkeypatch.setattr(assist_mod, "fill_page", lambda *a, **k: {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []})
    assist_mod.assist(job_for(EPIC, tmp_path), {"cv_pdf": None}, APPLICANT, tmp_path / "p", launch=lambda d: (page, lambda: None), ask=lambda p: "",
                      book=BOOK, tokens=["epicgames"])
    assert page.visited == ["https://job-boards.greenhouse.io/epicgames/jobs/5678901004"]


def test_a_greenhouse_job_without_a_known_board_is_opened_where_it_is(monkeypatch, tmp_path):
    page, _ = run_fake(monkeypatch, tmp_path, EPIC)
    assert page.visited == [EPIC]


# ------------------------------------------------------------------ Ashby merges candidates by email
def test_an_ashby_job_warns_when_the_tracker_already_has_an_application_to_the_company(monkeypatch, tmp_path, capsys):
    run_fake(monkeypatch, tmp_path, "https://jobs.ashbyhq.com/acme/1-2", history=lambda company: company == "Acme")
    assert "Ashby merges candidates by email; an earlier application to Acme exists." in capsys.readouterr().out


def test_no_warning_without_an_earlier_application_and_none_for_other_vendors(monkeypatch, tmp_path, capsys):
    run_fake(monkeypatch, tmp_path, "https://jobs.ashbyhq.com/acme/1-2", history=lambda company: False)
    run_fake(monkeypatch, tmp_path, "https://boards.greenhouse.io/acme/jobs/1", history=lambda company: True)
    run_fake(monkeypatch, tmp_path, "https://jobs.lever.co/acme/1", history=lambda company: True)
    assert "merges candidates" not in capsys.readouterr().out


# ------------------------------------------------------------------ a real page
@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def run_real(page, url, tmp_path, cv, **kw):
    return assist_mod.assist(job_for(url, tmp_path), {"cv_pdf": cv, "letter_pdf": None}, APPLICANT, tmp_path / "profile",
                             launch=lambda d: (page, lambda: None), ask=lambda p: "", book=BOOK, history=lambda c: False, **kw)


@pytest.mark.slow
def test_an_unknown_vendor_is_filled_by_the_form_agent_and_stops_before_the_submit(chromium, cv, tmp_path, capsys):
    html = (FIX / "agent_site.html").read_text(encoding="utf-8")
    page = chromium.new_page()
    page.route("https://careers.acme.example/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    r = run_real(page, "https://careers.acme.example/jobs/1", tmp_path, cv)
    out = capsys.readouterr().out
    assert r["agent"] == "done"
    assert page.evaluate("() => window.__submits") == 0
    page.evaluate("() => document.getElementById('back').click()")
    assert page.evaluate(VALUES_JS)["name"] == "Ada Lovelace"
    page.evaluate("() => document.getElementById('next1').click()")
    v = page.evaluate(VALUES_JS)
    assert v["phone"] == APPLICANT["phone"] and v["notice"] == "Available immediately"
    assert "Form agent: done" in out and "Submit controls (listed, never clicked by the agent)" in out and "ATTESTATIONS" in out
    assert (tmp_path / "formagent.jsonl").is_file()
    assert any("Describe an LLM feature" in m for m in r["manual"])
    page.close()


@pytest.mark.slow
def test_a_standard_vendor_whose_form_is_not_found_is_given_to_the_agent_to_reach(chromium, cv, tmp_path, monkeypatch):
    monkeypatch.setattr(formfind, "FORM_WAIT_S", 1)
    html = (FIX / "agent_site.html").read_text(encoding="utf-8")
    page = chromium.new_page()
    page.route("https://boards.greenhouse.io/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    r = run_real(page, "https://boards.greenhouse.io/acme/jobs/1?start", tmp_path, cv)
    assert "not_found" not in r and page.evaluate("() => window.__clicks") == ["apply"]          # "Start application": the agent found the way in
    assert page.evaluate(VALUES_JS)["name"] == "Ada Lovelace"                                      # then the existing filler filled page 1
    assert page.evaluate("() => window.__submits") == 0
    page.close()


@pytest.mark.slow
def test_no_form_and_no_way_in_is_still_form_not_found(chromium, cv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(formfind, "FORM_WAIT_S", 1)
    page = chromium.new_page()
    page.route("https://boards.greenhouse.io/**", lambda route: route.fulfill(body="<html><body><h1>Closed</h1><p>Nothing here</p></body></html>", content_type="text/html"))
    r = run_real(page, "https://boards.greenhouse.io/acme/jobs/1", tmp_path, cv)
    assert "FORM NOT FOUND https://boards.greenhouse.io/acme/jobs/1" in capsys.readouterr().out and r["not_found"]
    page.close()


# ------------------------------------------------------------------ the knock-out pre-scan, before anything is filled
@pytest.fixture
def knock_page(chromium):
    pg = chromium.new_page()
    pg.goto((FIX / "knock_form.html").resolve().as_uri())
    yield pg
    pg.close()


@pytest.mark.slow
def test_skipping_at_a_knock_out_fills_and_uploads_nothing(knock_page, cv, capsys):
    with pytest.raises(knockout.SkipJob):
        assist_mod.fill_page(knock_page, APPLICANT, cv, None, job=None, book=BOOK, knock_ask=lambda p: "s")
    v = knock_page.evaluate(VALUES_JS)
    assert v["first_name"] == "" and v["email"] == "" and v["resume"] == ""
    assert 'KNOCK-OUT: "Will you now or in the future require sponsorship to work in the UK?"' in capsys.readouterr().out


@pytest.mark.slow
def test_continuing_at_a_knock_out_fills_the_form_with_the_combined_answer_yes(knock_page, cv):
    assist_mod.fill_page(knock_page, APPLICANT, cv, None, job=None, book=BOOK, knock_ask=lambda p: "c")
    assert knock_page.evaluate(VALUES_JS)["first_name"] == "Ada"
    assert knock_page.evaluate("() => document.querySelector('input[value=Yes]').checked") is True              # now=No, future=Yes: "now or in the future" is Yes


@pytest.mark.slow
def test_the_assist_asks_once_and_a_skip_ends_the_job_as_not_submitted(chromium, cv, tmp_path, capsys):
    page = chromium.new_page()
    answers = []

    def ask(prompt):
        answers.append(prompt)
        return "s" if "KNOCK-OUT" in prompt else ""
    r = assist_mod.assist(job_for((FIX / "knock_form.html").resolve().as_uri(), tmp_path, apply_kind="greenhouse"), {"cv_pdf": cv, "letter_pdf": None},
                          APPLICANT, tmp_path / "profile", launch=lambda d: (page, lambda: None), ask=ask, book=BOOK, history=lambda c: False)
    assert r["skipped"].startswith("KNOCK-OUT") and r["confirmation"] == "x"
    assert not any("After submitting" in a for a in answers)                  # nothing to confirm: the job was skipped
    assert page.evaluate(VALUES_JS)["first_name"] == ""
    page.close()
