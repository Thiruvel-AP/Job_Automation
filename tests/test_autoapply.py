"""careerops/autoapply.py on local fixture forms (tests/fixtures/apply/auto_*.html) in a headless Chromium: no network, no real site. Fills from the
fictional applicant and answers files, photographs, and (when told to) submits; never solves a challenge, never invents an answer. Chromium missing
= the tests fail (a skip would hide a broken auto-apply)."""
import ast
import csv
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from challenge_fakes import ChallengeEl, ChallengePage
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops import autoapply
from careerops.apply import answers, autolog, formfind
from careerops.apply.handoff import Handoff
from careerops.apply.errors import ArchiveError

FIX = Path(__file__).resolve().parent / "fixtures" / "apply"
ROOT = Path(autoapply.__file__).resolve().parent.parent
NOW = lambda: datetime(2026, 10, 21, 10, 0, 0)          # noqa: E731
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
STATE_JS = """() => { const v = {}; for (const e of document.querySelectorAll('input,select,textarea')) {
  if (e.type === 'radio') { if (e.checked) v[e.name] = e.value; }
  else if (e.type === 'checkbox') v[e.name] = e.checked;
  else if (e.type === 'file') v[e.name] = e.files.length ? e.files[0].name : '';
  else v[e.name || e.id] = e.value; }
  return {clicks: window.__clicks || 0, touched: window.__touched || 0, values: v, url: location.href}; }"""

pytestmark = pytest.mark.slow          # these tests drive Chromium


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


class Run:
    """One call of autoapply.run against a fixture page, with everything it did recorded."""

    def __init__(self, chromium, settings, tmp_path, job, monkeypatch):
        self.chromium, self.settings, self.tmp, self.job = chromium, settings, tmp_path, job
        self.events: list[str] = []
        self.prompts: list[str] = []
        self.out: list[str] = []
        self.state: dict = {}
        self.submit_calls = 0
        real = autoapply._click_submit

        def spy(button):
            self.submit_calls += 1
            self.events.append("submit clicked")
            real(button)
        monkeypatch.setattr(autoapply, "_click_submit", spy)
        monkeypatch.setattr(autoapply, "SETTLE_MS", 50)
        monkeypatch.setattr(autoapply, "CONFIRM_SECONDS", 2)

    def launch(self, profile_dir):
        page = self.chromium.new_page()
        self.profile = profile_dir

        def close():
            try:
                self.state = page.evaluate(STATE_JS)
            except Exception:  # noqa: BLE001  (the page navigated: only the url matters then)
                self.state = {"url": page.url, "values": {}, "clicks": 0, "touched": 0}
            self.events.append("browser closed")
            page.close()
        return autoapply.Session(page, close)

    def handoff(self, known: str | None = None) -> Handoff:
        """A Handoff whose 'browser' is a list (the pages it was asked to open) and whose `known` can be forced."""
        self.opened: list[str] = []
        h = Handoff(self.settings, opener=lambda url: self.opened.append(url) or True, out=lambda s: self.out.append(s))
        if known:
            h.known = lambda url: known
        return h

    def run(self, page: str, replies: dict | None = None, *, submit=True, confirm=False, rep=None, history=None, throttle=None, handoff=None, failures=None, **extra):
        replies = replies or {}

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            self.events.append("asked: " + prompt.strip()[:40])
            for key, reply in replies.items():
                if key in prompt:
                    return reply.pop(0) if isinstance(reply, list) else reply
            return ""
        base, _, query = str(page).partition("?")
        target = Path(base) if Path(base).is_absolute() else FIX / base
        job = {**self.job, "apply_url": target.resolve().as_uri() + ("?" + query if query else "")}
        return autoapply.run(job, rep or self.rep(), APPLICANT, self.settings, submit=submit, confirm=confirm, ask=ask,
                             out=lambda s: self.out.append(s), launch=self.launch, history=history or (lambda c: False), throttle=throttle,
                             ats_strict=74.2, now=NOW, handoff=handoff, failures=failures, **extra)

    def rep(self, letter: bool = False) -> dict:
        out = Path(self.job["out_dir"])
        cv = out / "Ada Lovelace CV.pdf"
        cv.write_bytes(b"%PDF-1.4\n%%EOF")
        rep = {"cv_pdf": str(cv), "letter_pdf": None, "letter": {"status": "off"}}
        if letter:
            pdf = out / "Ada Lovelace Cover Letter.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF")
            rep.update(letter_pdf=str(pdf), letter={"status": "ok", "paragraphs": ["Dear team,", "I would like to work at Northwind."]})
        return rep


@pytest.fixture
def world(chromium, settings, job, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    book = yaml.safe_load((PROFILE_FIX / "answers.yaml").read_text(encoding="utf-8"))
    for e in book["answers"]:
        if e["id"] == "privacy_consent":
            e["answer"] = "Yes"
    (tmp_path / "answers.yaml").write_text(yaml.safe_dump(book), encoding="utf-8")
    settings.raw["paths"]["answers"] = str(tmp_path / "answers.yaml")
    settings.raw["apply"]["confirm_first_n"] = 0
    monkeypatch.setattr(autoapply, "check_job_files", lambda *a, **k: None)          # the wrong-file guard has its own tests (tests/apply)
    monkeypatch.setattr(autoapply.knockout, "scan", lambda *a, **k: [])             # the fixture asks "now or in the future" sponsorship, which is a knock-out for this applicant: tests/apply/test_knockout_flow.py
    return Run(chromium, settings, tmp_path, {**job, "out_dir": str(out), "source": "greenhouse"}, monkeypatch)


def log_rows(settings):
    return autolog.rows(settings)


# ------------------------------------------------------------------ happy path
def test_a_form_is_filled_from_the_applicant_and_the_answers_file_submitted_and_confirmed(world):
    r = world.run("auto_form.html", {"Submit?": "y"})
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1
    assert world.state["url"].endswith("auto_done.html") and "auto-20261021-100000-confirmation.png" in r.screenshots[-1]
    assert r.confirmation.startswith("auto-submitted 2026-10-21 10:00")
    assert [Path(s).name for s in r.screenshots] == ["auto-20261021-100000-form.png", "auto-20261021-100000-confirmation.png"]
    assert all(Path(s).is_file() and Path(s).stat().st_size > 1000 for s in r.screenshots)
    assert world.prompts == []                                    # confirm_first_n is 0 here: no question when every answer is known


def test_every_field_holds_what_applicant_yaml_or_answers_yaml_says(world):
    world.run("auto_form.html", submit=False)
    v = world.state["values"]
    assert (v["first_name"], v["last_name"], v["email"], v["phone"]) == ("Ada", "Lovelace", "ada.lovelace@example.org", "+44 7700 900123")
    assert v["urls[LinkedIn]"] == APPLICANT["linkedin"]
    assert v["resume"] == "Ada Lovelace CV.pdf" and v["cover_letter"] == ""          # no letter PDF for this job: nothing uploaded
    assert v["visa_future"] == "Yes"                                                  # applicant.yaml visa.by_country.GB.sponsorship_future
    assert v["notice"] == "Available immediately"
    assert v["salary"] == "50000"                                                     # a number field takes answer_numeric
    assert v["how"] == "Greenhouse" and v["gender"] == "Female"                       # {source}; an option the entry lists
    assert v["county"] == ""                                                          # "Essex" is not the word "sex": no answer, optional, empty
    assert v["relocate"] == "Yes"


def test_the_letter_pdf_is_uploaded_only_when_one_exists(world):
    world.run("auto_form.html", submit=False, rep=world.rep(letter=True))
    assert world.state["values"]["cover_letter"] == "Ada Lovelace Cover Letter.pdf"


def test_the_browser_profile_is_under_data_secrets_browser(world):
    world.run("auto_form.html", submit=False)
    assert world.profile == world.settings.path("secrets") / "browser"
    from careerops.config import load_settings
    shipped = load_settings().path("secrets") / "browser"
    assert shipped.relative_to(ROOT).as_posix() == "data/secrets/browser"


# ------------------------------------------------------------------ no-submit, confirmation, first N
def test_no_submit_fills_and_photographs_but_never_clicks_submit(world):
    r = world.run("auto_form.html", submit=False)
    assert r.status == autolog.NOT_SUBMITTED and world.submit_calls == 0
    assert world.state["clicks"] == 0 and world.state["url"].endswith("auto_form.html")
    assert [Path(s).name for s in r.screenshots] == ["auto-20261021-100000-form.png"]
    assert world.prompts == []                                    # --no-submit never asks Submit?


def test_the_first_n_auto_submits_ever_ask_before_clicking(world):
    world.settings.raw["apply"]["confirm_first_n"] = 3
    for i in range(2):
        autolog.append(world.settings, job={"id": f"greenhouse:{i}", "company": "X", "title": "Y"}, ats_strict=75, screenshots=[],
                       result=autolog.SUBMITTED, when=datetime(2026, 10, 1, 9, i))
    r = world.run("auto_form.html", {"Submit?": "n"})
    assert r.status == autolog.DECLINED and world.submit_calls == 0 and world.state["clicks"] == 0
    assert any("Submit? [y/N]" in p for p in world.prompts) and any("auto-20261021-100000-form.png" in o for o in world.out)
    r = world.run("auto_form.html", {"Submit?": "y"})
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1


def test_after_n_auto_submits_it_no_longer_asks_unless_confirm_is_given(world):
    world.settings.raw["apply"]["confirm_first_n"] = 3
    for i in range(3):
        autolog.append(world.settings, job={"id": f"greenhouse:{i}", "company": "X", "title": "Y"}, ats_strict=75, screenshots=[],
                       result=autolog.UNCONFIRMED if i else autolog.SUBMITTED, when=datetime(2026, 10, 1, 9, i))
    assert world.run("auto_form.html").status == autolog.SUBMITTED and not any("Submit?" in p for p in world.prompts)
    autolog.append(world.settings, job={"id": "greenhouse:9", "company": "X", "title": "Y"}, ats_strict=75, screenshots=[], result=autolog.NOT_SUBMITTED)
    r = world.run("auto_form.html", {"Submit?": ""}, confirm=True)                # --confirm asks every time; blank is N
    assert r.status == autolog.DECLINED and any("--confirm" in p for p in world.prompts)


def test_no_confirmation_page_within_the_wait_is_submit_unconfirmed(world):
    r = world.run("auto_form.html?noconfirm")
    assert r.status == autolog.UNCONFIRMED and world.submit_calls == 1
    assert "check the form" in r.reason and len(r.screenshots) == 2


# ------------------------------------------------------------------ CAPTCHA: stop, touch nothing
@pytest.mark.parametrize("variant", ["auto_captcha.html", "auto_captcha.html?hcaptcha", "auto_captcha.html?turnstile", "auto_captcha.html?text"])
def test_a_captcha_stops_the_run_before_anything_is_filled_or_clicked(world, variant):
    r = world.run(variant)
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 0
    assert "CAPTCHA" in r.reason or "verify you are human" in r.reason
    assert world.state["touched"] == 0 and world.state["clicks"] == 0
    assert world.state["values"]["first_name"] == "" and world.state["values"]["email"] == ""          # not even the form was filled


def test_an_invisible_recaptcha_is_not_a_hand_off_the_form_is_filled_and_nothing_on_it_is_touched(world):
    r = world.run("auto_captcha.html?invisible", submit=False)
    assert r.status == autolog.NOT_SUBMITTED and world.submit_calls == 0
    assert world.state["values"]["first_name"] == "Ada" and world.state["touched"] == 0


def test_a_bot_check_hands_the_page_to_the_users_own_browser_and_closes_the_automated_one(world):
    h = world.handoff()
    r = world.run("auto_captcha.html", handoff=h)
    assert r.status == autolog.NEEDS_USER and r.reason.startswith("HAND-OFF") and world.submit_calls == 0
    assert len(world.opened) == 1 and world.opened[0].endswith("auto_captcha.html")           # the apply URL, in the person's normal browser
    assert not any(e.startswith("asked: Finish by hand") for e in world.events) and world.events[-1] == "browser closed"
    assert any(o.strip().startswith("CV: ") and "Ada Lovelace CV.pdf" in o for o in world.out)       # the CV path is printed
    paste = Path(world.job["out_dir"]) / "answers_to_paste.txt"
    text = paste.read_text(encoding="utf-8")
    assert "ada.lovelace@example.org" in text and "Available immediately" in text
    assert world.state["touched"] == 0 and world.state["clicks"] == 0                          # nothing on the challenge was touched


def test_error_verifying_your_application_is_a_hand_off_too(world):
    r = world.run("auto_verify_error.html", handoff=world.handoff())
    assert r.status == autolog.NEEDS_USER and "error verifying your application" in r.reason and world.submit_calls == 0
    assert len(world.opened) == 1 and world.state["values"]["first_name"] == "" and world.state["touched"] == 0


def test_a_host_that_handed_over_before_opens_no_browser_at_all(world):
    def launch(profile_dir):
        raise AssertionError("an automated browser was opened for a host that is already handed over")
    world.launch = launch
    h = world.handoff(known="a bot check turned this browser away")
    r = world.run("auto_form.html", handoff=h)
    assert r.status == autolog.NEEDS_USER and r.reason.startswith("HAND-OFF") and len(world.opened) == 1
    assert (Path(world.job["out_dir"]) / "answers_to_paste.txt").is_file() and log_rows(world.settings)[-1]["result"] == "needs_user"


def test_a_job_page_with_no_form_says_form_not_found_with_the_url(world, monkeypatch):
    monkeypatch.setattr(formfind, "FORM_WAIT_S", 1)
    r = world.run("auto_noform.html")
    assert r.status == autolog.NEEDS_USER and r.reason.startswith("FORM NOT FOUND ") and r.reason.endswith("auto_noform.html")
    assert r.filled == [] and world.submit_calls == 0


def test_a_challenge_that_appears_after_the_click_is_not_touched_and_hands_over(world):
    r = world.run("auto_form.html?captcha_after")
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 1 and "after the click" in r.reason
    assert world.state["touched"] == 0 and world.events[-1] == "browser closed"


def test_the_needs_user_row_is_logged_with_its_screenshot(world):
    world.run("auto_captcha.html")
    row = log_rows(world.settings)[-1]
    assert row["result"] == "needs_user" and row["job id"] == world.job["id"] and row["ATS strict"] == "74.2"
    assert row["screenshots"].endswith("-stopped.png") and Path(row["screenshots"]).is_file()


# ------------------------------------------------------------------ unknown questions: stop and ask, never invent
def test_an_unknown_required_question_stops_and_asks_and_nothing_is_invented(world):
    before = (world.tmp / "answers.yaml").read_bytes()
    r = world.run("auto_form.html?unknown")
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 0 and "driving licence" in r.reason
    assert world.state["values"]["licence"] == ""
    asked = [p for p in world.prompts if "Your answer" in p]
    assert len(asked) == 1 and any("QUESTION WITH NO ANSWER: Do you hold a full UK driving licence?" in o for o in world.out)
    assert (world.tmp / "answers.yaml").read_bytes() == before                                   # nothing saved


def test_a_typed_answer_is_used_once_and_saved_only_when_the_user_says_save(world):
    before = (world.tmp / "answers.yaml").read_bytes()
    r = world.run("auto_form.html?unknown", {"Your answer": "Yes", "keep this answer": "no thanks"})
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1
    assert (world.tmp / "answers.yaml").read_bytes() == before
    r = world.run("auto_form.html?unknown", {"Your answer": "Yes", "keep this answer": "save"})
    assert r.status == autolog.SUBMITTED, r.reason
    book = answers.load(world.tmp / "answers.yaml")
    assert book.resolve("Do you hold a full UK driving licence?", world.job).text == "Yes"
    asked_so_far = len(world.prompts)
    r = world.run("auto_form.html?unknown")                                                       # no prompt for it any more
    assert r.status == autolog.SUBMITTED and len(world.prompts) == asked_so_far


def test_an_attestation_is_never_ticked_even_when_answers_yaml_says_yes_and_the_run_waits_for_the_human(world):
    r = world.run("auto_form.html?attest")                                    # the world's answers.yaml has `privacy_consent: Yes`
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 0
    assert "required fields still empty" in r.reason and "privacy notice" in r.reason
    assert not any("Your answer for this form" in p for p in world.prompts)                          # no question was asked about it
    assert world.state["values"].get("privacy") in (None, False, "")                                   # and it was not ticked


def test_an_answer_that_matches_no_option_is_not_guessed(world):
    book = yaml.safe_load((world.tmp / "answers.yaml").read_text(encoding="utf-8"))
    next(e for e in book["answers"] if e["id"] == "how_heard")["answer"] = "A friend"
    (world.tmp / "answers.yaml").write_text(yaml.safe_dump(book), encoding="utf-8")
    r = world.run("auto_form.html")
    assert r.status == autolog.NEEDS_USER and "none of the options" not in r.reason and "How did you hear" in r.reason
    assert any("no option matches it" in o for o in world.out) and world.state["values"]["how"] == ""


def test_an_optional_question_with_no_answer_is_left_empty_without_asking(world):
    r = world.run("auto_form.html")
    assert r.status == autolog.SUBMITTED and not any("Your answer" in p for p in world.prompts)
    assert any("County" in b for b in r.blank)


def test_previously_applied_is_answered_no_only_when_the_company_has_no_earlier_application(world):
    page = world.tmp / "previously.html"
    page.write_text('<html><body><form id="f" novalidate><label for="a">Have you applied to us before? *</label><input id="a" name="a" required>'
                    '<label for="e">Email *</label><input id="e" type="email" name="e" required><label for="r">CV *</label>'
                    '<input id="r" type="file" name="r" required><button type="submit">Submit application</button></form>'
                    '<script>window.__clicks=0</script></body></html>', encoding="utf-8")
    r = world.run(page, submit=False, history=lambda c: False)
    assert r.status == autolog.NOT_SUBMITTED and world.state["values"]["a"] == "No"
    r = world.run(page, submit=False, history=lambda c: True)
    assert r.status == autolog.NEEDS_USER and world.state["values"]["a"] == ""


def test_the_history_of_a_company_is_the_database_and_the_archive_folders(settings, db, job):
    seen = autoapply.default_history(db, settings)
    assert seen("Acme") is False and seen("") is True
    (settings.path("applications") / "Acme Ltd" / "AI Engineer").mkdir(parents=True)
    assert autoapply.default_history(db, settings)("ACME  LTD") is True
    db.upsert_job(job)
    assert autoapply.default_history(db)("Northwind Health AI") is False
    db.update_job(job["id"], status="applied", applied_at="2026-10-01T10:00:00+00:00")
    assert autoapply.default_history(db)("Northwind Health AI") is True


# ------------------------------------------------------------------ the rest of the safety rails
def test_wrong_files_or_an_unknown_country_refuse_before_a_browser_opens(world, monkeypatch):
    import careerops.apply.guard as guard
    monkeypatch.setattr(autoapply, "check_job_files", guard.check_job_files)
    world.launch = lambda profile: pytest.fail("a browser was opened")
    with pytest.raises(ArchiveError):
        world.run("auto_form.html", rep={**world.rep(), "cv_pdf": str(world.tmp / "elsewhere.pdf")})
    assert log_rows(world.settings) == []


def test_every_outcome_is_one_row_of_the_auto_log(world):
    world.run("auto_form.html", submit=False)
    world.run("auto_form.html")
    world.run("auto_form.html?noconfirm")
    rows = log_rows(world.settings)
    assert [r["result"] for r in rows] == ["not_submitted", "submitted", "submit_unconfirmed"]
    assert rows[1]["job id"] == world.job["id"] and rows[1]["company"] == world.job["company"] and rows[1]["role"] == world.job["title"]


def test_requests_to_one_host_are_a_second_apart():
    slept, now = [], [100.0]
    t = autoapply.Throttle(1.0, clock=lambda: now[0], sleep=lambda s: (slept.append(round(s, 3)), now.__setitem__(0, now[0] + s)))
    t.wait("https://boards.greenhouse.io/a/jobs/1")
    now[0] += 0.4
    t.wait("https://boards.greenhouse.io/a/jobs/1")
    t.wait("https://jobs.lever.co/b/2")                             # another host: no wait
    now[0] += 5
    t.wait("https://boards.greenhouse.io/a/jobs/1")                 # long enough ago: no wait
    assert slept == [0.6]


def test_the_page_is_asked_for_through_the_throttle(world):
    calls = []

    class Spy(autoapply.Throttle):
        def wait(self, url):
            calls.append(url)
    world.run("auto_form.html", throttle=Spy())
    assert len(calls) >= 2 and calls[0].endswith("auto_form.html")                 # the page, then the submit click


# ------------------------------------------------------------------ the page checks without a browser
def FakePage(selectors=(), text=""):                       # noqa: N802  (a page where each selector matches one displayed 304x78 frame)
    return ChallengePage({s: [ChallengeEl()] for s in selectors}, text)


def test_every_challenge_marker_is_recognised_by_looking_only():
    for sel in autoapply.CAPTCHA_SELECTORS:
        assert autoapply.captcha_reason(FakePage([sel])), sel
    for words in ("Please verify you are human", "Verify that you're human", "I'm not a robot", "Checking if you are human before continuing"):
        assert autoapply.captcha_reason(FakePage(text=words)), words
    assert autoapply.captcha_reason(FakePage(text="Submit your application. We are a human-centred company.")) is None


# ------------------------------------------------------------------ the only module that submits
def _calls_named(tree: ast.AST, attr: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
            yield node


def test_click_on_a_submit_control_appears_only_in_autoapply_py():
    others = []
    for folder in ("careerops", "scripts"):
        for p in sorted((ROOT / folder).rglob("*.py")):
            if p.name == "autoapply.py" or "__pycache__" in p.parts:
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(src.splitlines(), 1):
                if "click(" in line:
                    others.append((f"{p.relative_to(ROOT)}:{n}", line.strip()))
                if "submit" in line.lower() and any(w in line for w in ("query_selector", ".locator(", "get_by_role", ".press(")):
                    others.append((f"{p.relative_to(ROOT)}:{n}", line.strip()))
    # the one other click in the package is assist's "Apply" link that opens a form: guarded by OPEN_FORM_LINK (an "Apply" text, no "submit");
    # Ashby / Lever forms are reached by address (formfind.form_url), a bot check or sign-in is handed over and never clicked.
    # The form agent (apply/formagent.py) also clicks, but only an element that has a ref, and no submit control has one: tests/apply/test_formagent.py
    # checks where its clicks are.
    # v2.4.1: two modules click things that can never submit: an item of an open drop-down list (`[role=option]`, refused when its text names the
    # geolocation or a cloud drive) and the "Enter manually" button of a Cover Letter block (found by its exact text). Their clicks are listed here
    # one by one, so a new click in either module fails this test.
    allowed = {"careerops/apply/shortfields.py": {"el.click()", "o.click()"}, "careerops/apply/letter_demand.py": {"b.click(); return true;"}}
    assert [w for w, line in others if not w.startswith(("careerops/apply/assist.py", "careerops/apply/formagent.py", *allowed))] == []
    for path, lines in allowed.items():
        found = {line for w, line in others if w.startswith(path)}
        assert found and all(any(a in line for a in lines) for line in found), (path, found)
    assert all("OPEN_FORM_LINK" in line for w, line in others if w.startswith("careerops/apply/assist.py"))


def test_inside_autoapply_the_submit_button_is_clicked_in_exactly_one_place():
    tree = ast.parse((ROOT / "careerops" / "autoapply.py").read_text(encoding="utf-8"))
    where = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for call in _calls_named(fn, "click"):
            where.setdefault(fn.name, []).append(ast.unparse(call))
    assert where == {"_click_submit": ["button.click()"], "open_form": ["a.click()"], "_combobox": ["next((o for t, o in options if t == pick)).click()"]}
    submit_calls = [c for c in _calls_named(tree, "_click_submit")] + \
                   [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_click_submit"]
    assert len(submit_calls) == 1


def test_the_hard_rules_name_the_same_single_submitter():
    text = (ROOT / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "only by `careerops/autoapply.py`" in text
