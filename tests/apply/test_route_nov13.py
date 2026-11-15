"""13 Nov live run: a sign-in page is a hard stop (nothing typed), a job board's form is never entered (apply pack + listing), and a page with
no application form never says "nothing blocks auto-submit". No browser is started: the pages and the launcher are fakes."""
from __future__ import annotations

import pytest

from careerops.apply import assist as assist_mod
from careerops.apply import formfind, requiredgate, route
from careerops.apply.errors import AccountRequired
from types import SimpleNamespace

LOGIN = "https://secure.reed.co.uk/login?returnurl=%2fjobs%2f123"


class Typed(Exception):
    pass


class Page:
    """Records every call that would type or click; reading calls are free."""

    def __init__(self, url, password=False):
        self.url, self.password, self.acted = url, password, []

    def query_selector_all(self, sel):
        return [object()] if self.password and "password" in sel else []

    def inner_text(self, sel):
        return ""

    def wait_for_timeout(self, ms):
        pass

    def goto(self, url, **k):
        self.url = url

    def fill(self, *a, **k):
        self.acted.append(("fill", a))

    type = click = fill


@pytest.mark.parametrize("url", [LOGIN, "https://example.com/users/sign_in", "https://careers.acme.com/account/login.aspx",
                                 "https://login.acme.com/start", "https://idp.acme.com/authorize?client_id=1&state=x&redirect_uri=https://a.b",
                                 "https://idp.acme.com/x?code_challenge=abc&state=1"])
def test_auth_urls_are_recognised(url):
    assert route.auth_url_reason(url)


@pytest.mark.parametrize("url", ["https://boards.greenhouse.io/acme/jobs/1", "https://jobs.lever.co/acme/abc/apply",
                                 "https://careers.acme.com/jobs/account-executive-data", "https://acme.com/jobs?state=CA", "https://www.reed.co.uk/jobs/ds/1"])
def test_ordinary_application_urls_are_not_auth(url):
    assert route.auth_url_reason(url) is None


def test_a_password_field_makes_any_page_a_sign_in_page():
    assert route.page_reason(Page("https://acme.example/apply", password=True)) == "the page has a password field"
    assert route.page_reason(Page("https://acme.example/apply")) is None


def test_wait_for_form_stops_at_a_sign_in_page_and_types_nothing():
    page = Page(LOGIN)
    with pytest.raises(AccountRequired) as e:
        formfind.wait_for_form(page, 1)
    assert page.acted == [] and "secure." in e.value.reason


# ---- assist(): no browser at all for a sign-in address or a job board
@pytest.fixture(autouse=True)
def no_files_or_visa_plan(monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    plan = SimpleNamespace(work=SimpleNamespace(line=lambda: "W=GB"), describe=lambda: "plan")
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: plan)
    monkeypatch.setattr(assist_mod.stale, "require_fresh", lambda *a, **k: None)


def launch_must_not_run(profile_dir):
    raise AssertionError("a browser was opened")


def test_a_sign_in_apply_address_opens_no_browser_and_prints_the_listing(capsys):
    job = {"id": "reed:1", "apply_url": LOGIN, "url": "https://www.reed.co.uk/jobs/1", "apply_kind": "employer_other"}
    r = assist_mod.assist(job, {"cv_pdf": "/x/cv.pdf"}, {}, "profile", launch=launch_must_not_run, ask=lambda p: pytest.fail(p))
    assert r["account_required"] and r["confirmation"] == "x"
    out = capsys.readouterr().out
    assert route.ACCOUNT_STATUS in out and LOGIN in out


def test_a_job_board_listing_builds_the_pack_and_never_enters_the_form(tmp_path):
    seen = {}

    class Handoff:
        def why(self, url):
            return ""

        def run(self, job, rep, applicant, book, plan, url, why, filled, remember=True, drafts=None):
            seen.update(url=url, why=why, remember=remember)

    job = {"id": "reed:2", "apply_url": "https://www.reed.co.uk/jobs/data-scientist/2", "url": "https://www.reed.co.uk/jobs/data-scientist/2",
           "apply_kind": "aggregator_only"}
    r = assist_mod.assist(job, {"cv_pdf": "/x/cv.pdf"}, {}, "profile", launch=launch_must_not_run, ask=lambda p: pytest.fail(p), handoff=Handoff())
    assert r["aggregator"] and r["confirmation"] == "x"
    assert seen["url"].startswith("https://www.reed.co.uk/") and seen["remember"] is False and "apply pack" in seen["why"]


def test_an_aggregator_job_with_an_employer_address_goes_to_that_address():
    job = {"id": "reed:3", "apply_url": "https://www.reed.co.uk/jobs/3", "url": "https://acme.example/careers/3", "apply_kind": "aggregator_only"}
    assert route.employer_url(job) == "https://acme.example/careers/3"
    assert route.employer_url({"id": "greenhouse:acme:42", "apply_url": "https://www.indeed.co.uk/x", "url": ""}) == "https://job-boards.greenhouse.io/acme/jobs/42"
    assert route.employer_url({"id": "reed:4", "apply_url": "https://www.reed.co.uk/jobs/4", "url": "https://www.cv-library.co.uk/job/4"}) == ""
    assert route.is_aggregator({"id": "x", "apply_url": "https://uk.indeed.com/viewjob?jk=1", "apply_kind": "employer_other"})


# ---- no form: never "nothing blocks"
def test_a_page_without_a_form_is_never_clear_to_submit():
    assert not requiredgate.form_recognised(["text", "text"]) and requiredgate.form_recognised(["file"]) and requiredgate.form_recognised(["text"] * 3)
    lines = requiredgate.render(requiredgate.no_form_rows(), color=False)
    text = " ".join(lines)
    assert "NO APPLICATION FORM FOUND" in text and "nothing here blocks" not in text.lower()
    assert requiredgate.blocking(requiredgate.no_form_rows())
