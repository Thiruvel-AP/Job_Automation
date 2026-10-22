"""Submit failures and the hand-off (CLAUDE.md: "A form that cannot be submitted is a bug to report, not to retry"). A verification error after the
submit, a field LOST after the re-fill, an upload the form refused and FORM NOT FOUND are counted per job in data/live/submit_failures.csv. After
two (or at once for a host on the hand-off list) the job goes to the person's own browser with answers_to_paste.txt; there is never a third attempt,
never a changed profile, user agent or proxy, and nothing is ever signed in to. Fake pages only: no browser, no network."""
import csv
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PROFILE_FIX

from careerops import autoapply
from careerops.apply import answers, autolog, failures as F, freetext
from careerops.apply.handoff import Handoff, copy_to_clipboard, paste_text
from test_handoff import APPLICANT, Page

NOW = datetime(2026, 10, 22, 9, 30, 0)
URL = "https://boards.greenhouse.io/acme/jobs/1"
LEVER = "https://jobs.lever.co/valarian/abc"


# ------------------------------------------------------------------ the log
def test_a_failure_is_one_row_with_time_job_host_kind_and_detail(settings):
    log = F.Failures(settings, now=lambda: NOW)
    log.record("manual:a", URL, F.FIELD_LOST, "the form emptied field(s) after they were filled: Email")
    path = settings.path("live") / "submit_failures.csv"
    rows = list(csv.reader(path.open(encoding="utf-8", newline="")))
    assert rows[0] == ["time", "job id", "host", "kind", "detail"]
    assert rows[1] == ["2026-10-22T09:30:00", "manual:a", "boards.greenhouse.io", "field_lost", "the form emptied field(s) after they were filled: Email"]


def test_failures_are_counted_per_job_and_the_file_is_only_appended_to(settings):
    log = F.Failures(settings, now=lambda: NOW)
    assert log.count("manual:a") == 0
    log.record("manual:a", URL, F.NOT_FOUND, "FORM NOT FOUND x")
    log.record("manual:b", URL, F.NOT_FOUND, "FORM NOT FOUND x")
    log.record("manual:a", URL, F.UPLOAD_REFUSED, "the form refused an upload")
    again = F.Failures(settings)                                             # a later process reads the same file
    assert again.count("manual:a") == 2 and again.count("manual:b") == 1 and again.kinds("manual:a") == ["form_not_found", "upload_refused"]
    assert len(list(csv.reader((settings.path("live") / "submit_failures.csv").open(encoding="utf-8")))) == 4


def test_a_detail_with_commas_quotes_and_newlines_stays_one_row(settings):
    log = F.Failures(settings, now=lambda: NOW)
    log.record("manual:a", URL, F.FIELD_LOST, 'Email, "Phone"\nsecond line')
    assert log.count("manual:a") == 1 and log.rows("manual:a")[0]["detail"] == 'Email, "Phone"\nsecond line'


@pytest.mark.parametrize("text, kind", [
    ("FORM NOT FOUND https://boards.greenhouse.io/acme/jobs/1", "form_not_found"),
    ("the form emptied field(s) after they were filled: Email; Phone", "field_lost"),
    ("the page says 'error verifying your application' (a bot check turned this browser away)", "verification_error"),
    ("the form refused an upload: file type not allowed", "upload_refused"),
    ("a required question with no answer: Why us?", ""),                       # a question for the person is not a failure of the form
    ("required fields still empty: Visa", ""),
])
def test_what_kind_of_failure_a_message_is(text, kind):
    assert F.kind_of(text) == kind


def test_the_limit_is_two_and_a_setting(settings):
    assert F.Failures(settings).limit == 2
    settings.raw["apply"]["max_submit_failures"] = 3
    assert F.Failures(settings).limit == 3
    del settings.raw["apply"]["max_submit_failures"]
    assert F.Failures(settings).limit == 2


# ------------------------------------------------------------------ the host list
def test_lever_is_on_the_shipped_hand_off_list(settings):
    assert "jobs.lever.co" in settings.raw["apply"]["handoff_hosts"]


def test_a_listed_host_is_known_to_the_hand_off_without_ever_having_handed_a_job_over(settings):
    h = Handoff(settings, opener=lambda u: True, out=lambda s: None)
    assert "apply.handoff_hosts" in h.known(LEVER) and "jobs.lever.co" in h.known(LEVER)
    assert h.known(URL) is None and h.hosts() == {}


def test_the_list_is_edited_in_settings(settings):
    settings.raw["apply"]["handoff_hosts"] = ["boards.greenhouse.io"]
    h = Handoff(settings, opener=lambda u: True, out=lambda s: None)
    assert h.known(URL) and h.known(LEVER) is None
    settings.raw["apply"].pop("handoff_hosts")
    assert Handoff(settings, opener=lambda u: True, out=lambda s: None).known(LEVER) is None


# ------------------------------------------------------------------ the hand-off: browser, clipboard, paths, paste file
@pytest.fixture
def world(settings, job, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cv, letter = out / "Ada Lovelace CV.pdf", out / "Ada Lovelace Cover Letter.pdf"
    cv.write_bytes(b"%PDF")
    letter.write_bytes(b"%PDF")
    book = answers.load(PROFILE_FIX / "answers.yaml")
    opened, lines, clips = [], [], []
    h = Handoff(settings, opener=lambda url: opened.append(url) or True, out=lines.append, clip=lambda text: clips.append(text) or True)
    rep = {"cv_pdf": str(cv), "letter_pdf": str(letter),
           "letter": {"status": "ok", "paragraphs": ["Dear team,", "I would like to work at Northwind."]}}
    plan = SimpleNamespace(describe=lambda: "Right to work in the UK: yes. Needs sponsorship: no.", summary="I have the right to work in the UK.")
    return SimpleNamespace(h=h, opened=opened, lines=lines, clips=clips, rep=rep, book=book, plan=plan, out=out, settings=settings,
                           job={**job, "out_dir": str(out), "apply_url": URL})


def test_the_hand_off_opens_the_url_copies_the_first_answer_prints_both_paths_and_writes_the_paste_file(world):
    path = world.h.run(world.job, world.rep, APPLICANT, world.book, world.plan, URL, "2 failed attempts on this job")
    assert world.opened == [URL]
    assert world.clips == ["Ada Lovelace"]
    text = "\n".join(world.lines)
    assert str(world.out / "Ada Lovelace CV.pdf") in text and str(world.out / "Ada Lovelace Cover Letter.pdf") in text
    assert "Copied to the clipboard: Ada Lovelace" in text and str(path) in text


def test_the_paste_file_holds_every_prepared_answer(world):
    lib = freetext.Library(world.settings.path("answers_library"))
    lib.add(freetext.Record.new("Describe a project you are proud of", "I built a churn model on 7,043 customers.", "project", "Northwind Health AI",
                                world.job["id"], ["p1a"], datetime(2026, 10, 22).date()))
    path = world.h.run(world.job, world.rep, APPLICANT, world.book, world.plan, URL, "2 failed attempts on this job", filled=["First name -> Ada"])
    text = path.read_text(encoding="utf-8")
    for want in ("Ada Lovelace",                                                     # name
                 "ada.lovelace@example.org",                                        # e-mail
                 "+44 7700 900123",                                                 # phone
                 "https://www.linkedin.com/in/ada-lovelace-test/",                  # links
                 "Bristol, UK",                                                     # location
                 "Salary expectation:", "I'd welcome a discussion about salary",    # salary
                 "Notice period", "Available immediately",                          # notice
                 "Right to work in the UK: yes. Needs sponsorship: no.", "I have the right to work in the UK.",    # visa text
                 "I would like to work at Northwind.",                              # cover letter
                 "Describe a project you are proud of", "I built a churn model on 7,043 customers.",             # free-text answer
                 "First name -> Ada"):                                              # what had been filled
        assert want in text, want
    assert str(world.out / "Ada Lovelace CV.pdf") in text


def test_a_blank_salary_is_said_to_be_blank_not_made_up(world):
    world.book = answers.AnswerBook([answers.Entry("salary_expectation", ("salary expectation",), "", None, {}, ())])
    text = world.h.run(world.job, world.rep, APPLICANT, world.book, world.plan, URL, "why").read_text(encoding="utf-8")
    assert "Salary expectation: (blank in answers.yaml: yours to answer)" in text


def test_the_clipboard_is_clip_exe_and_a_missing_clip_exe_is_said_not_raised(monkeypatch, world):
    calls = []
    monkeypatch.setattr("careerops.apply.handoff.shutil.which", lambda name: "/mnt/c/Windows/System32/clip.exe" if name == "clip.exe" else None)
    monkeypatch.setattr("careerops.apply.handoff.subprocess.run", lambda cmd, **kw: calls.append((cmd, kw)) or SimpleNamespace(returncode=0))
    assert copy_to_clipboard("Ada Lovelace") is True
    assert calls[0][0] == ["/mnt/c/Windows/System32/clip.exe"] and "Ada Lovelace".encode("utf-16-le") in calls[0][1]["input"]
    monkeypatch.setattr("careerops.apply.handoff.shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert copy_to_clipboard("Ada Lovelace") is False
    h = Handoff(world.settings, opener=lambda u: True, out=world.lines.append)
    h.run(world.job, world.rep, APPLICANT, world.book, world.plan, URL, "why")
    assert any("clip.exe" in l and "not copied" in l for l in world.lines)


def test_no_credential_proxy_or_browser_setting_is_touched_by_the_failure_code():
    import ast

    names = set()
    for node in ast.walk(ast.parse(Path(F.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg.lower())
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.name.lower() for a in node.names)
    assert not names & {"fill", "click", "type", "launch", "launch_persistent_context", "new_context", "user_agent", "proxy", "password", "playwright", "requests"}


# ------------------------------------------------------------------ autoapply: two failures -> hand-off offered; never a third attempt
@pytest.fixture
def auto(settings, world, monkeypatch):
    monkeypatch.setattr(autoapply, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(autoapply.visa_mod, "require_plan", lambda job, applicant: world.plan)
    monkeypatch.setattr(autoapply.Throttle, "wait", lambda self, url: None)
    state = SimpleNamespace(launched=0, prompts=[], answers={}, out=[])

    def attempt(page, url=URL, replies=None, failures=True, submit=True):
        replies = dict(replies or {})

        def ask(prompt):
            state.prompts.append(prompt)
            for key, reply in replies.items():
                if key in prompt:
                    return reply.pop(0) if isinstance(reply, list) else reply
            return ""

        def launch(profile_dir):
            state.launched += 1
            return autoapply.Session(page, lambda: None)
        job = {**world.job, "apply_url": url}
        return autoapply.run(job, world.rep, APPLICANT, settings, submit=submit, ask=ask, out=state.out.append, launch=launch,
                             history=lambda c: False, ats_strict=74.2, now=lambda: NOW, handoff=world.h,
                             failures=F.Failures(settings, now=lambda: NOW) if failures else None)
    state.attempt = attempt
    return state


def no_form():
    return Page(form_after_ms=None)


def test_the_first_failure_is_recorded_and_no_hand_off_is_offered(auto, world, settings):
    res = auto.attempt(no_form())
    assert res.status == autolog.NEEDS_USER and "FORM NOT FOUND" in res.reason and not res.handed_off
    assert F.Failures(settings).count(world.job["id"]) == 1 and world.opened == []
    assert not any("[h]" in p for p in auto.prompts)


def test_the_second_failure_offers_the_hand_off_and_h_does_it(auto, world, settings):
    auto.attempt(no_form())
    res = auto.attempt(no_form(), replies={"[h]": "h"})
    assert any("[h] hand off" in p for p in auto.prompts)
    assert res.handed_off and world.opened == [URL] and (world.out / "answers_to_paste.txt").is_file() and world.clips == ["Ada Lovelace"]
    assert F.Failures(settings).count(world.job["id"]) == 2
    assert any("Ada Lovelace CV.pdf" in l for l in world.lines)


def test_declining_the_offer_leaves_the_job_alone_but_there_is_still_no_third_attempt(auto, world, settings):
    auto.attempt(no_form())
    res = auto.attempt(no_form(), replies={"[h]": ""})
    assert not res.handed_off and world.opened == []
    launched = auto.launched
    third = auto.attempt(no_form())
    assert auto.launched == launched                                         # no browser was opened for a third attempt
    assert third.handed_off and world.opened == [URL] and "no third attempt" in third.reason.lower()
    assert F.Failures(settings).count(world.job["id"]) == 2                  # and the third was not counted as another failure


def test_after_the_hand_off_nothing_is_clicked_filled_or_launched(auto, world, settings, monkeypatch):
    clicks = []
    monkeypatch.setattr(autoapply, "_click_submit", lambda b: clicks.append(b))
    for _ in range(2):
        auto.attempt(no_form(), replies={"[h]": "h"})
    launched = auto.launched
    again = auto.attempt(Page(), submit=True)
    assert auto.launched == launched and clicks == [] and again.handed_off


def test_failures_of_one_job_do_not_stop_another_job(auto, world, settings):
    for _ in range(2):
        auto.attempt(no_form(), replies={"[h]": ""})
    other = {**world.job, "id": "manual:other"}
    log = F.Failures(settings)
    assert log.count("manual:other") == 0 and log.count(world.job["id"]) == 2 and other["id"] != world.job["id"]


def test_a_host_on_the_list_goes_straight_to_the_hand_off_without_a_browser(auto, world, settings):
    res = auto.attempt(Page(url=LEVER), url=LEVER)
    assert auto.launched == 0 and res.handed_off and world.opened == [LEVER] and "apply.handoff_hosts" in res.reason
    assert (world.out / "answers_to_paste.txt").is_file() and F.Failures(settings).count(world.job["id"]) == 0


def test_a_lost_field_after_the_refill_counts_as_a_failure(auto, world, settings, monkeypatch):
    monkeypatch.setattr(autoapply, "open_form", lambda page, url, throttle, kind=None: None)
    monkeypatch.setattr(autoapply, "fill_form", lambda page, ctx, quiet_ms=0: None)
    monkeypatch.setattr(autoapply, "verify_filled", lambda page, ctx: ["Email", "Phone"])
    res = auto.attempt(Page())
    assert "emptied field" in res.reason and F.Failures(settings).kinds(world.job["id"]) == ["field_lost"]


def test_an_upload_the_form_refused_counts_as_a_failure(auto, world, settings, monkeypatch):
    monkeypatch.setattr(autoapply, "open_form", lambda page, url, throttle, kind=None: None)

    def fill(page, ctx, quiet_ms=0):
        raise autoapply.NeedsUser("the form refused an upload: file type not allowed")
    monkeypatch.setattr(autoapply, "fill_form", fill)
    auto.attempt(Page())
    assert F.Failures(settings).kinds(world.job["id"]) == ["upload_refused"]


def test_an_error_verifying_page_is_a_failure_and_the_existing_hand_off_still_happens_at_once(auto, world, settings):
    res = auto.attempt(Page(body="error verifying your application"))
    assert res.handed_off and world.opened == [URL] and F.Failures(settings).kinds(world.job["id"]) == ["verification_error"]


def test_a_question_nobody_answered_is_not_a_failure(auto, world, settings, monkeypatch):
    monkeypatch.setattr(autoapply, "open_form", lambda page, url, throttle, kind=None: None)

    def fill(page, ctx, quiet_ms=0):
        raise autoapply.NeedsUser("a required question has no answer: Why us?")
    monkeypatch.setattr(autoapply, "fill_form", fill)
    auto.attempt(Page())
    assert F.Failures(settings).count(world.job["id"]) == 0


def test_no_failures_object_means_the_old_behaviour(auto, world):
    res = auto.attempt(no_form(), failures=False)
    assert res.status == autolog.NEEDS_USER and not res.handed_off and world.opened == []
