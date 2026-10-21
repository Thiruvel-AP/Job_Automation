"""A browser the person closed after submitting must not lose the application record: `assist()` still asks for the confirmation, and
`live` still marks the job applied (or asks about it on the next run when it crashed in between). No browser is started: the
launcher is a fake."""
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from careerops.apply import assist as assist_mod
from careerops.apply.live import Hooks, Live
from test_live import Fake, inbox, put, archive, session, yes  # noqa: F401  (fixtures and helpers of the live tests)

CLOSED = "Target page, context or browser has been closed"


class Page:
    """A page with no fields; `broken` makes every call raise what Playwright raises once the person has closed the window."""

    def __init__(self, broken=False):
        self.broken = broken

    def _call(self, *a, **k):
        if self.broken:
            raise PlaywrightError(CLOSED)

    goto = wait_for_timeout = _call

    def query_selector(self, sel):
        self._call()
        return object()                        # "the form is already here": no Apply button is looked for

    def query_selector_all(self, sel):
        self._call()
        return []


def launcher(*, close_raises=True, page_broken=False):
    def launch(profile_dir):
        def close():
            if close_raises:
                raise PlaywrightError(CLOSED)
        return Page(page_broken), close
    return launch


@pytest.fixture(autouse=True)
def no_files_or_visa_plan(monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    plan = SimpleNamespace(work=SimpleNamespace(line=lambda: "W=GB"), describe=lambda: "plan")
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: plan)
    monkeypatch.setattr(assist_mod, "fill_page", lambda page, *a, **k: {"filled": ["Email"], "manual": [], "uploaded": [], "required_empty": [],
                                                                         "visa": [], "eo": []})


JOB = {"id": "manual:x1", "apply_url": "https://acme.example/apply", "url": "https://acme.example/job"}


def run(**kw):
    asked = []
    r = assist_mod.assist(JOB, {"cv_pdf": "/x/cv.pdf"}, {}, "profile", launch=launcher(**kw), ask=lambda p: asked.append(p) or "REF-9")
    return r, asked


def test_a_browser_that_cannot_be_closed_any_more_still_reaches_the_confirmation_prompt():
    r, asked = run(close_raises=True)
    assert r["confirmation"] == "REF-9" and len(asked) == 1


def test_a_browser_closed_before_the_prompt_still_reaches_it():
    r, asked = run(close_raises=True, page_broken=True)
    assert r["confirmation"] == "REF-9" and len(asked) == 1 and r["filled"] == []


def test_a_playwright_error_is_the_only_thing_swallowed(monkeypatch):
    def launch(profile_dir):
        return Page(), lambda: (_ for _ in ()).throw(RuntimeError("a real bug"))
    with pytest.raises(RuntimeError):
        assist_mod.assist(JOB, {}, {}, "profile", launch=launch, ask=lambda p: "")


def test_live_marks_the_job_applied_when_the_browser_close_failed_and_the_user_says_y(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = Fake(db, settings, yes())
    fake.assist = lambda jid: (fake.calls.append(("assist", jid)) or
                               assist_mod.assist(JOB, {}, {}, "profile", launch=launcher(), ask=lambda p: ""))
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert fake.names() == ["tailor", "review", "preflight", "assist", "mark_applied"]
    assert [j["status"] for j in db.jobs()] == ["applied"]
