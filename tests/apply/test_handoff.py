"""Finding the application form (apply/formfind.py) and handing a bot check or sign-in wall to the person (apply/handoff.py), with fake pages:
no browser, no network. The Chromium versions are in tests/test_autoapply.py."""
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from challenge_fakes import ChallengeEl, ChallengePage
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops.apply import answers, assist as assist_mod, formfind, handoff as handoff_mod
from careerops.apply.handoff import Handoff, paste_text

APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


class El:
    def __init__(self, typ="text", text="", visible=True, on_click=None):
        self.typ, self.text, self.visible, self.on_click = typ, text, visible, on_click

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.typ if name == "type" else None

    def inner_text(self):
        return self.text

    def click(self):
        if self.on_click:
            self.on_click()


class Page:
    """A page that shows its form fields only after `form_after_ms` of waiting (None = never), and records what was asked of it."""

    def __init__(self, url="https://boards.greenhouse.io/acme/jobs/1", form_after_ms=0, body="", apply_link=False, popups=()):
        self.url, self.form_after_ms, self.body, self.t = url, form_after_ms, body, 0
        self.visited, self.clicked, self.selectors = [], [], []
        self.link = El("a", "Apply", on_click=self._opened) if apply_link else None
        self.context = SimpleNamespace(pages=[self, *popups])
        if apply_link:
            self.form_after_ms = None

    def _opened(self):
        self.clicked.append("Apply")
        self.form_after_ms = self.t

    def goto(self, url, wait_until=None):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        self.t += ms

    def inner_text(self, sel="body"):
        return self.body

    def query_selector(self, sel):
        self.selectors.append(sel)
        return None

    def query_selector_all(self, sel):
        if sel == formfind.FIELDS:
            return [El("email"), El("text")] if self.form_after_ms is not None and self.t >= self.form_after_ms else []
        return [self.link] if self.link and sel == "a, button" else []


# ------------------------------------------------------------------ where the form is
@pytest.mark.parametrize("url,kind,want", [
    ("https://jobs.ashbyhq.com/acme/1234-abcd", None, "https://jobs.ashbyhq.com/acme/1234-abcd/application"),
    ("https://jobs.ashbyhq.com/acme/1234-abcd/", "ashby", "https://jobs.ashbyhq.com/acme/1234-abcd/application"),
    ("https://jobs.ashbyhq.com/acme/1234-abcd/application", None, "https://jobs.ashbyhq.com/acme/1234-abcd/application"),
    ("https://jobs.lever.co/acme/99ab", None, "https://jobs.lever.co/acme/99ab/apply"),
    ("https://jobs.lever.co/acme/99ab/apply?lever-source=x", None, "https://jobs.lever.co/acme/99ab/apply?lever-source=x"),
    ("https://jobs.lever.co/acme/99ab?lever-source=x", None, "https://jobs.lever.co/acme/99ab/apply?lever-source=x"),
    ("https://boards.greenhouse.io/acme/jobs/1", None, "https://boards.greenhouse.io/acme/jobs/1"),
    ("https://careers.example.com/jobs/1", None, "https://careers.example.com/jobs/1"),
])
def test_the_form_address_per_ats(url, kind, want):
    assert formfind.form_url(url, kind) == want


def test_ashby_is_opened_at_application_and_lever_at_apply():
    for url, tail in (("https://jobs.ashbyhq.com/acme/1", "/application"), ("https://jobs.lever.co/acme/2", "/apply")):
        page, waits = Page(url), []
        assert formfind.open_form(page, url, wait=waits.append) is None
        assert page.visited == [url + tail] and waits == [url + tail]                 # the throttle is asked about the page it opens


def test_greenhouse_waits_for_the_form_to_appear():
    page = Page(form_after_ms=6000)
    assert formfind.open_form(page, "https://boards.greenhouse.io/acme/jobs/1") is None
    assert 6000 <= page.t < 15000 and page.visited == ["https://boards.greenhouse.io/acme/jobs/1"]


def test_no_form_field_after_15_seconds_is_form_not_found_with_the_url():
    page = Page(form_after_ms=None)
    assert formfind.open_form(page, "https://boards.greenhouse.io/acme/jobs/1") == "FORM NOT FOUND https://boards.greenhouse.io/acme/jobs/1"
    assert page.t >= formfind.FORM_WAIT_S * 1000 - formfind.POLL_MS


def test_an_apply_link_is_followed_once_by_the_callers_click_and_nothing_else_is_clicked():
    page = Page("https://careers.example.com/jobs/1", apply_link=True)
    assert formfind.open_form(page, page.url, follow=lambda a: a.click()) is None and page.clicked == ["Apply"]
    never = Page("https://careers.example.com/jobs/1", apply_link=True)
    assert formfind.open_form(never, never.url) == "FORM NOT FOUND https://careers.example.com/jobs/1" and never.clicked == []     # no follow handed in


def test_a_search_box_or_one_text_field_is_not_a_form():
    class One(Page):
        def query_selector_all(self, sel):
            return [El("text")] if sel == formfind.FIELDS else []
    assert not formfind.has_form(One())
    assert formfind.has_form(Page())


# ------------------------------------------------------------------ what is handed over
def test_a_bot_check_a_verify_error_and_a_sign_in_wall_are_blockers():
    assert "CAPTCHA" in formfind.blocker(ChallengePage({RECAPTCHA: [ChallengeEl()]}))
    assert "verify you are human" in formfind.blocker(Page(body="Please verify you are human"))
    assert "error verifying your application" in formfind.blocker(Page(body="There was an Error verifying your application."))
    assert formfind.blocker(Page(body="Apply for Data Scientist")) is None


@pytest.mark.parametrize("host,word", [("accounts.google.com", "Google"), ("login.microsoftonline.com", "Microsoft"), ("login.live.com", "Microsoft")])
def test_a_google_or_microsoft_sign_in_page_or_popup_is_a_blocker(host, word):
    assert word in formfind.blocker(Page(f"https://{host}/signin"))
    popup = SimpleNamespace(url=f"https://{host}/o/oauth2/auth?client=x")
    assert word in formfind.blocker(Page(popups=[popup]))
    assert formfind.blocker(Page("https://accounts.google.com.evil.example/")) is None          # only the real hosts and their sub-domains


def test_wait_for_form_raises_hand_off_when_a_check_shows_up_before_the_form():
    page = Page(form_after_ms=None, body="Checking that you are human")
    with pytest.raises(formfind.HandOff, match="verify you are human"):
        formfind.wait_for_form(page)


# ------------------------------------------------------------------ the hand-off itself
@pytest.fixture
def world(settings, job, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cv = out / "Ada Lovelace CV.pdf"
    cv.write_bytes(b"%PDF")
    book = answers.load(PROFILE_FIX / "answers.yaml")
    opened, lines = [], []
    h = Handoff(settings, opener=lambda url: opened.append(url) or True, out=lines.append)
    rep = {"cv_pdf": str(cv), "letter_pdf": None, "letter": {"status": "ok", "paragraphs": ["Dear team,", "I would like to work at Northwind."]}}
    return SimpleNamespace(h=h, opened=opened, lines=lines, rep=rep, book=book, job={**job, "out_dir": str(out)}, out=out, settings=settings)


def hand(w, url="https://boards.greenhouse.io/acme/jobs/1", reason="a CAPTCHA / bot check is on the page", **kw):
    return w.h.run(w.job, w.rep, APPLICANT, w.book, None, url, reason, **kw)


def test_the_hand_off_opens_the_url_prints_the_cv_path_and_writes_the_answers_to_paste(world):
    path = hand(world, filled=["First name -> Ada"])
    assert world.opened == ["https://boards.greenhouse.io/acme/jobs/1"]
    assert any("Ada Lovelace CV.pdf" in l for l in world.lines) and any(str(path) in l for l in world.lines)
    assert path == world.out / "answers_to_paste.txt"
    text = path.read_text(encoding="utf-8")
    for want in ("ada.lovelace@example.org", "+44 7700 900123", "Ada Lovelace", "Available immediately", "I would like to work at Northwind.",
                 "First name -> Ada", "https://www.linkedin.com/in/ada-lovelace-test/", "Bristol, UK"):
        assert want in text, want
    assert "(blank in answers.yaml: yours to answer)" in text           # current_salary has no answer: said so, not made up
    assert "a CAPTCHA / bot check is on the page" in text


def test_the_paste_file_has_the_equal_opportunities_options_from_both_files(world):
    text = paste_text(world.job, world.rep, APPLICANT, world.book, None, "https://x.test/", "why", [])
    assert "ethnicity: Asian - Indian" in text and "eo_gender: Female" in text and "Female | Woman" in text


def test_the_host_is_recorded_and_the_next_job_on_it_goes_straight_to_hand_off(world):
    assert world.h.known("https://boards.greenhouse.io/acme/jobs/2") is None
    hand(world)
    assert world.h.known("https://boards.greenhouse.io/other/jobs/9") == "a CAPTCHA / bot check is on the page"
    assert world.h.known("https://jobs.lever.co/acme/1") is None
    again = Handoff(world.settings, opener=lambda url: True, out=lambda s: None)            # a new process reads the file
    assert again.known("https://boards.greenhouse.io/x/jobs/3")


def test_a_known_host_hand_off_does_not_record_again(world):
    hand(world)
    first = world.h.hosts()["boards.greenhouse.io"]
    hand(world, reason="something else", remember=False)
    assert world.h.hosts()["boards.greenhouse.io"] == first


def test_the_opener_runs_explorer_exe_with_the_url_and_refuses_anything_but_http(monkeypatch):
    calls = []
    monkeypatch.setattr(handoff_mod.shutil, "which", lambda name: "/mnt/c/Windows/explorer.exe" if name == "explorer.exe" else None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    assert handoff_mod.open_in_browser("https://boards.greenhouse.io/acme/jobs/1")
    assert calls == [["/mnt/c/Windows/explorer.exe", "https://boards.greenhouse.io/acme/jobs/1"]]
    assert not handoff_mod.open_in_browser("file:///etc/passwd") and not handoff_mod.open_in_browser("javascript:alert(1)")
    assert len(calls) == 1


def test_no_credential_or_challenge_is_ever_typed_by_the_hand_off_code():
    from pathlib import Path
    for mod in (formfind, handoff_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert ".fill(" not in src and ".click(" not in src.replace("click(a)", "") and "type(" not in src.replace("Callable", "")


# ------------------------------------------------------------------ the assist (a human submits)
@pytest.fixture
def assisted(monkeypatch, world):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    plan = SimpleNamespace(work=SimpleNamespace(line=lambda: "W=GB"), describe=lambda: "plan", summary="")
    real_require, use_real = assist_mod.require_plan, []              # a page that is really filled needs the real plan
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: real_require(job, applicant) if use_real else plan)

    def run(page, handoff=None, launch=True, row=False, url=None, real_plan=False):
        use_real[:] = [1] if real_plan else []
        closed = []

        def start(profile_dir):
            if not launch:
                raise AssertionError("a browser was opened")
            return page, lambda: closed.append(1)
        job = {**world.job, "apply_url": url or getattr(page, "url", "https://boards.greenhouse.io/acme/jobs/1"), "apply_kind": "greenhouse"}
        job = to_row(job) if row else job                              # `live` and the CLI hand the assist a sqlite3.Row
        r = assist_mod.assist(job, world.rep, APPLICANT, world.out, launch=start, ask=lambda p: "", book=world.book, handoff=handoff)
        return r, closed
    return run


def test_the_assist_says_form_not_found_and_never_prints_an_empty_filled_list(assisted, capsys):
    r, closed = assisted(Page(form_after_ms=None))
    out = capsys.readouterr().out
    assert "FORM NOT FOUND https://boards.greenhouse.io/acme/jobs/1" in out and "Filled:" not in out and r["not_found"]
    assert closed == [1]


def test_the_assist_hands_a_bot_check_over_closes_the_automated_browser_and_still_asks_for_the_confirmation(assisted, world, capsys):
    r, closed = assisted(Page(body="Please verify you are human"), handoff=world.h)
    assert r["handoff"] and world.opened == ["https://boards.greenhouse.io/acme/jobs/1"] and closed == [1]       # closed once, not twice
    assert (world.out / "answers_to_paste.txt").is_file() and "Filled:" not in capsys.readouterr().out
    assert r["confirmation"] == "" and world.h.known("https://boards.greenhouse.io/x/jobs/2")


def test_the_assist_opens_no_browser_for_a_host_that_handed_over_before(assisted, world):
    world.h.record("https://boards.greenhouse.io/acme/jobs/1", "earlier bot check")
    r, closed = assisted(Page(), handoff=world.h, launch=False)
    assert r["handoff"] and world.opened and closed == []


# ------------------------------------------------------------------ equal opportunities from answers.yaml
GENDER = ["Man", "Woman", "Non-binary", "Prefer not to say"]


def book_with(**entry):
    return answers.AnswerBook([answers.Entry(entry.get("id", "eo_gender"), tuple(entry.get("patterns", ("gender", "sex"))),
                                             entry.get("answer", "Man"), None, {}, tuple(entry.get("options", ("Man", "Male"))))])


def test_man_woman_options_pick_man_from_the_answers_file():
    from careerops.apply import eo
    listed = eo.entries(APPLICANT, book_with())
    entry = eo.entry_for("Gender", listed)
    assert entry is not None and entry.key == "eo_gender" and eo.choose(GENDER, entry) == "Man"
    assert eo.entry_for("What sex were you assigned?", listed) is entry and eo.entry_for("Which county do you live in? Essex", listed) is None


def test_eo_options_are_the_users_order_and_the_answer_when_there_are_none():
    from careerops.apply import eo
    (entry,) = eo.book_entries(book_with(options=("Male", "Man")))
    assert eo.choose(["Man", "Male", "Woman"], entry) == "Male"
    (entry,) = eo.book_entries(book_with(options=(), answer="Man"))
    assert entry.match_any == ("Man",) and eo.choose(GENDER, entry) == "Man"
    assert eo.book_entries(book_with(options=(), answer="")) == []                      # neither: nothing is guessed
    assert eo.book_entries(book_with(id="notice_period")) == []                         # only eo_* entries


def test_applicant_yaml_wins_and_comes_first(applicant=None):
    from careerops.apply import eo
    listed = eo.entries(APPLICANT, book_with(id="eo_ethnicity", patterns=("ethnicity",), answer="Other", options=("Other",)))
    assert [e.key for e in listed] == ["ethnicity", "eo_ethnicity"] and eo.entry_for("Ethnicity", listed).key == "ethnicity"
    assert eo.entries(APPLICANT) == eo.entries(APPLICANT, None)


# ------------------------------------------------------------------ only a VISIBLE challenge hands the page over
RECAPTCHA = 'iframe[src*="recaptcha"]'
ANCHOR, BFRAME = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=x&", "https://www.google.com/recaptcha/api2/bframe?hl=en"
VISIBLE = [
    ("a reCAPTCHA checkbox", RECAPTCHA, ChallengeEl(304, 78, src=ANCHOR + "size=normal")),
    ("a reCAPTCHA puzzle", RECAPTCHA, ChallengeEl(400, 580, src=BFRAME)),
    ("an hCaptcha checkbox", 'iframe[src*="hcaptcha"]', ChallengeEl(303, 78, src="https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html")),
    ("an hCaptcha puzzle", 'iframe[src*="hcaptcha"]', ChallengeEl(500, 570, src="https://newassets.hcaptcha.com/hcaptcha-challenge.html")),
    ("a Turnstile frame", 'iframe[src*="challenges.cloudflare.com"]', ChallengeEl(300, 65)),
    ("a Turnstile widget", ".cf-turnstile", ChallengeEl(300, 65, tag="div")),
    ("a small Turnstile widget", ".cf-turnstile", ChallengeEl(80, 40, tag="div")),               # any displayed Turnstile widget counts
    ("a frame exactly 100x60", RECAPTCHA, ChallengeEl(100, 60, src=ANCHOR + "size=normal")),
]
NOT_VISIBLE = [
    ("the invisible v3 badge frame", RECAPTCHA, ChallengeEl(256, 60, src=ANCHOR + "size=invisible")),
    ("the invisible enterprise badge frame", RECAPTCHA, ChallengeEl(256, 60, src="https://www.google.com/recaptcha/enterprise/anchor?k=x&size=invisible")),
    ("a challenge frame that is not displayed", RECAPTCHA, ChallengeEl(400, 580, visible=False, src=BFRAME)),
    ("a frame 90x50", RECAPTCHA, ChallengeEl(90, 50, src=ANCHOR + "size=normal")),
    ("a frame 100 wide and 59 high", RECAPTCHA, ChallengeEl(100, 59, src=ANCHOR + "size=normal")),
    ("a container marked data-size=invisible", ".g-recaptcha", ChallengeEl(304, 78, tag="div", data_size="invisible")),
    ("a submit button that carries the site key", "[data-sitekey]", ChallengeEl(300, 80, tag="button")),
    ("a Turnstile widget that is not displayed", ".cf-turnstile", ChallengeEl(300, 65, visible=False, tag="div")),
]


@pytest.mark.parametrize("sel,el", [c[1:] for c in VISIBLE], ids=[c[0] for c in VISIBLE])
def test_a_displayed_challenge_is_a_blocker(sel, el):
    assert "CAPTCHA" in formfind.blocker(ChallengePage({sel: [el]}))


@pytest.mark.parametrize("sel,el", [c[1:] for c in NOT_VISIBLE], ids=[c[0] for c in NOT_VISIBLE])
def test_an_invisible_or_hidden_challenge_is_not_a_blocker(sel, el):
    assert formfind.blocker(ChallengePage({sel: [el]})) is None


def test_what_recaptcha_v3_puts_on_a_page_together_does_not_hand_off_but_one_visible_frame_among_them_does():
    v3 = [ChallengeEl(256, 60, src=ANCHOR + "size=invisible"), ChallengeEl(400, 580, visible=False, src=BFRAME)]
    assert formfind.blocker(ChallengePage({RECAPTCHA: v3, "[data-sitekey]": [ChallengeEl(300, 80, tag="button")]})) is None
    assert "CAPTCHA" in formfind.blocker(ChallengePage({RECAPTCHA: [*v3, ChallengeEl(304, 78, src=ANCHOR + "size=normal")]}))


def test_the_words_verify_you_are_human_still_hand_off_with_an_invisible_badge_on_the_page():
    page = ChallengePage({RECAPTCHA: [ChallengeEl(256, 60, src=ANCHOR + "size=invisible")]}, text="Verify you are human")
    assert "verify you are human" in formfind.blocker(page)


# ------------------------------------------------------------------ a sqlite3.Row is a job too (live run, 22 Oct: `job.get` on a Row)
def to_row(job: dict) -> sqlite3.Row:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(f"create table jobs ({', '.join(job)})")
    con.execute(f"insert into jobs values ({', '.join('?' * len(job))})", [None if v is None else str(v) for v in job.values()])
    row = con.execute("select * from jobs").fetchone()
    assert not hasattr(row, "get")
    return row


def test_the_assist_takes_a_sqlite_row_through_to_the_hand_off(assisted, world):
    r, closed = assisted(Page(body="Please verify you are human"), handoff=world.h, row=True)
    assert r["handoff"] and closed == [1] and world.opened == ["https://boards.greenhouse.io/acme/jobs/1"]
    assert (world.out / "answers_to_paste.txt").is_file()
    assert world.h.hosts()["boards.greenhouse.io"]["job"] == world.job["id"]                 # the id was read from the Row


def test_a_row_for_a_host_that_handed_over_before_gets_its_paste_file_too(assisted, world):
    world.h.record("https://boards.greenhouse.io/acme/jobs/1", "earlier bot check")
    r, closed = assisted(Page(), handoff=world.h, launch=False, row=True)
    assert r["handoff"] and (world.out / "answers_to_paste.txt").is_file()


def test_handoff_run_takes_a_row_by_itself(world):
    world.job = to_row(world.job)
    assert hand(world) == world.out / "answers_to_paste.txt"
    assert "Northwind" in (world.out / "answers_to_paste.txt").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the assist in Chromium: visible challenge = hand-off, invisible = fill on
CAPTCHA_PAGE = Path(__file__).resolve().parents[1] / "fixtures" / "apply" / "auto_captcha.html"


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.mark.slow
@pytest.mark.parametrize("query", ["", "?hcaptcha", "?turnstile", "?text"])
def test_the_assist_hands_a_visible_challenge_over_and_touches_nothing(assisted, world, chromium, query):
    page = chromium.new_page()
    r, closed = assisted(page, handoff=world.h, url=CAPTCHA_PAGE.as_uri() + query, row=True, real_plan=True)
    assert r["handoff"] and len(world.opened) == 1 and closed == [1]
    assert page.evaluate("window.__touched") == 0 and page.input_value("#first_name") == ""
    page.close()


@pytest.mark.slow
def test_the_assist_fills_the_form_past_an_invisible_recaptcha(assisted, world, chromium, capsys):
    page = chromium.new_page()
    r, closed = assisted(page, handoff=world.h, url=CAPTCHA_PAGE.as_uri() + "?invisible", row=True, real_plan=True)
    assert "handoff" not in r and world.opened == []
    assert page.input_value("#first_name") == "Ada" and page.input_value("#email") == APPLICANT["email"]
    assert page.evaluate("window.__touched") == 0 and page.evaluate("window.__clicks") == 0              # the badge, the hidden frame and the button: untouched
    out = capsys.readouterr().out
    assert "Filled:" in out and "Uploaded:" in out and "HAND-OFF" not in out
    page.close()
