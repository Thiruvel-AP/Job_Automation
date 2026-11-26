"""Workday account wall: it is `manual_apply_account_required`, never `FORM NOT FOUND`.
No browser, no network, no real settings.yaml — fakes follow tests/apply/test_route_nov13.py."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from careerops.apply import assist as assist_mod
from careerops.apply import planned
from careerops.apply.errors import AccountRequired
from careerops.apply.formagent import SIGN_IN_NEED


URL = "https://lseg.wd3.myworkdayjobs.com/manual:1d576f2fd4a5"
APPLICANT = {"visa": {"by_country": {"GB": {"right_to_work": True, "sponsorship_now": False,
                                          "summary": "I hold a Skilled Worker visa"}}},
             "location": "London, UK"}


class FakePlanner:
    fetch_json = None
    headless = None
    book_path = None


class PasswordPage:
    """A page with an Apply button; after Apply is clicked it shows email + password fields."""

    def __init__(self, url=URL):
        self.url = url
        self.clicked = []
        self.filled = {}
        self.apply_clicked = False

    def query_selector_all(self, sel):
        if sel == "input[type=password]" and self.apply_clicked:
            return [object()]
        return []

    def query_selector(self, sel):
        return None

    def inner_text(self, sel="body"):
        return ""

    def evaluate(self, script):
        return ""

    def goto(self, url, **k):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def fill(self, *a, **k):
        raise AssertionError("nothing may be typed on a sign-in page")

    def click(self, *a, **k):
        self.clicked.append(a[0] if a else "")
        self.apply_clicked = True


class FakeAgent:
    """The agent clicks Apply, then stops at the account wall (sign-in page)."""

    def __init__(self, page):
        self.page = page

    def run(self, goal):
        if goal == "reach":
            self.page.click("Apply")
            return SimpleNamespace(outcome="need_user", reason=SIGN_IN_NEED, sign_in=True)
        return SimpleNamespace(outcome="done", sign_in=False)


class NoWayAgent:
    """The agent finds no way in at all (not a sign-in wall)."""

    def __init__(self, page):
        self.page = page

    def run(self, goal):
        return SimpleNamespace(outcome="need_user",
                               reason="no way to the application form was found on this page", sign_in=False)


def _job(tmp_path, url=URL, **kw):
    return {"id": "manual:1d576f2fd4a5", "company": "LSEG", "title": "Data Scientist",
            "source": "manual", "location": "London, UK", "description": "",
            "out_dir": str(tmp_path), "apply_url": url, "url": url,
            "apply_kind": "employer_other", **kw}


@pytest.fixture
def no_files_or_plan(monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(planned, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: SimpleNamespace(
        work=SimpleNamespace(line=lambda: "W=GB", shown="GB"), describe=lambda: "plan"))


def test_planned_run_on_workday_clicks_apply_never_account_and_hands_off(tmp_path, monkeypatch, no_files_or_plan):
    """Ticket 002: planned.run on Workday — the agent clicks Apply only, hits the account wall,
    account_required is set, handoff.run is called with remember=False, no failure row."""
    page = PasswordPage()

    def make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext):
        return FakeAgent(page)

    monkeypatch.setattr(assist_mod, "make_agent", make_agent)
    monkeypatch.setattr(planned.formfind, "blocker", lambda page: None)
    monkeypatch.setattr(planned.formscan, "reach", lambda page, url, agent=None, out=print:
                        (agent(page) if agent else None) or SimpleNamespace(ok=False))

    seen = {}

    class Handoff:
        def why(self, url):
            return ""

        def run(self, job, rep, applicant, book, plan, url, why, filled, remember=True, drafts=None):
            seen.update(url=url, why=why, remember=remember)

    class Failures:
        limit = 2
        def gate(self, job_id):
            return None
        def record(self, job_id, url, kind, text):
            return 1

    job = _job(tmp_path)
    r = planned.run(job, {}, APPLICANT, "profile", plan=None, cv=None, letter=None, letter_text=None,
                    launch=lambda d: (page, lambda: None), ask=lambda p: "", book=None,
                    history=None, freetext=None, handoff=Handoff(), failures=Failures(),
                    fact_bank=None, tokens=(), letter_maker=None, record=False,
                    planner=FakePlanner(), url=URL)
    assert r["account_required"] and r["confirmation"] == "x"
    assert seen["remember"] is False
    assert seen["url"] == URL
    assert not seen["why"].startswith("FORM NOT FOUND")
    # nothing typed: no fill calls on the page after the click
    assert page.filled == {}
    # only Apply was clicked, never Create Account / Sign In
    assert page.clicked == ["Apply"]


def test_account_required_calls_pack_once(tmp_path, monkeypatch, no_files_or_plan):
    """account_required(..., pack=...) calls pack once. Without pack behaviour is unchanged."""
    called = []

    def pack(why):
        called.append(why)

    r = {}
    res = assist_mod.account_required(r, "the page has a password field", URL, ask=lambda p: "", pack=pack)
    assert res["account_required"] and called == ["the page has a password field"] and res["confirmation"] == "x"

    r2 = {}
    assist_mod.account_required(r2, "why", URL, ask=lambda p: "")
    assert r2["account_required"] and called == ["the page has a password field"]


def test_agent_branch_sign_in_raises_account_required(tmp_path, monkeypatch, no_files_or_plan):
    """Ticket 001: agent branch, non-Workday "other" vendor — a fake agent returns need_user with
    SIGN_IN_NEED. Result: account_required, no not_found, no failure row."""
    page = PasswordPage()
    page.url = "https://careers.acme.example/jobs/1"

    def make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext):
        return FakeAgent(page)

    monkeypatch.setattr(assist_mod, "make_agent", make_agent)
    monkeypatch.setattr(assist_mod.formfind, "blocker", lambda page: None)

    seen = {}

    class Handoff:
        def why(self, url):
            return ""

        def run(self, job, rep, applicant, book, plan, url, why, filled, remember=True, drafts=None):
            seen.update(url=url, why=why, remember=remember)

    job = _job(tmp_path, url="https://careers.acme.example/jobs/1")
    r = assist_mod.assist(job, {"cv_pdf": None, "letter_pdf": None}, APPLICANT, "profile",
                          launch=lambda d: (page, lambda: None), ask=lambda p: "",
                          book=None, history=None, freetext=None, handoff=Handoff(), failures=None,
                          planner=None)
    assert r["account_required"]
    assert "not_found" not in r


def test_agent_branch_no_way_in_is_form_not_found_with_reason(tmp_path, monkeypatch, no_files_or_plan):
    """Ticket 001: agent branch with need_user "no way to the application form": FORM NOT FOUND
    that includes the agent's reason, counted once."""
    page = PasswordPage()
    page.url = URL

    def make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext):
        return NoWayAgent(page)

    monkeypatch.setattr(assist_mod, "make_agent", make_agent)
    monkeypatch.setattr(assist_mod.formfind, "blocker", lambda page: None)

    class Failures:
        limit = 2
        def record(self, job_id, url, kind, text):
            return 1
        def gate(self, job_id):
            return None

    seen = {}
    class Handoff:
        def why(self, url):
            return ""

        def run(self, job, rep, applicant, book, plan, url, why, filled, remember=True, drafts=None):
            seen.update(url=url, why=why)

    job = _job(tmp_path)
    r = assist_mod.assist(job, {"cv_pdf": None, "letter_pdf": None}, APPLICANT, "profile",
                          launch=lambda d: (page, lambda: None), ask=lambda p: "",
                          book=None, history=None, freetext=None, handoff=Handoff(), failures=Failures(),
                          planner=None)
    assert r["not_found"] and "FORM NOT FOUND" in r["not_found"] and "form agent:" in r["not_found"]


def test_planned_run_password_field_after_reach_is_account_required(tmp_path, monkeypatch, no_files_or_plan):
    """Ticket 001: planned.run with a fake page that has a password field after reach:
    account_required, no failure row."""
    page = PasswordPage()

    def make_agent(page, applicant, book, job, plan, cv, letter, letter_text, history, freetext):
        return FakeAgent(page)

    monkeypatch.setattr(assist_mod, "make_agent", make_agent)
    monkeypatch.setattr(planned.formfind, "blocker", lambda page: None)
    monkeypatch.setattr(planned.formscan, "reach", lambda page, url, agent=None, out=print:
                        (agent(page) if agent else None) or SimpleNamespace(ok=False))

    seen = {}
    class Handoff:
        def why(self, url):
            return ""

        def run(self, job, rep, applicant, book, plan, url, why, filled, remember=True, drafts=None):
            seen.update(url=url, why=why, remember=remember)

    job = _job(tmp_path)
    r = planned.run(job, {}, APPLICANT, "profile", plan=None, cv=None, letter=None, letter_text=None,
                    launch=lambda d: (page, lambda: None), ask=lambda p: "", book=None,
                    history=None, freetext=None, handoff=Handoff(), failures=None,
                    fact_bank=None, tokens=(), letter_maker=None, record=False,
                    planner=FakePlanner(), url=URL)
    assert r["account_required"]
    assert seen["remember"] is False
    assert page.clicked == ["Apply"]
    assert page.clicked == ["Apply"]
