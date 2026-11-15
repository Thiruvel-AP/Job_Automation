"""v2.5 end to end: assist() with a planner. The questions (a headless pre-scan here), the letter, the drafts, the plan and answers_to_paste.txt all
exist BEFORE the visible browser is launched; then the form is filled from the plan. A context closed mid-fill ends the job with `BROWSER CLOSED:
nothing submitted` and no confirmation prompt. Pages are served by a Playwright route (no network); MockLLM; nothing is submitted."""
import pytest
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS
from test_freetext import AD_PHRASE, FB, REP, WHY, llm_with
from test_greenhouse_form import APPLICANT, JOB_ROW, PAGE, book, plan_for

from careerops.apply import answerplan as AP
from careerops.apply import assist as assist_mod
from careerops.apply import freetext as ft
from careerops.apply import planfill, planfirst, planned
from careerops.apply.letter_demand import Made

pytestmark = pytest.mark.slow          # these tests drive Chromium

URL = "https://forms.test/northwind/apply"


@pytest.fixture
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=CHROMIUM_ARGS)
        yield b
        b.close()


@pytest.fixture
def world(browser, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(planned, "check_job_files", lambda *a, **k: None)
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: plan_for())
    out = tmp_path / "out"
    out.mkdir()
    cv = out / "Ada CV.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    letter = out / "Ada Cover Letter.pdf"
    events: list[str] = []

    def served(name):
        def launch(*_):
            ctx = browser.new_context()
            ctx.route("https://forms.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE))
            events.append(name + (" (answers_to_paste.txt already written)" if (out / "answers_to_paste.txt").is_file() else ""))
            return ctx.new_page(), ctx.close
        return launch

    def maker(ctl):
        events.append("letter")
        letter.write_bytes(b"%PDF-1.4\n%%EOF")
        return Made(str(letter), "Hi Northwind team,\n\nBody.\n\nRegards,\n\nAda", "", None)

    job = {**JOB_ROW, "url": URL, "apply_url": URL, "out_dir": str(out), "apply_kind": None}
    free = ft.for_job(job, REP, FB, llm_with(WHY, ad_phrases=[AD_PHRASE]), settings, book(), ask=lambda p: "y", out=lambda s: None)

    def run(ask, **kw):
        return assist_mod.assist(job, {"cv_pdf": str(cv)}, APPLICANT, tmp_path / "profile", launch=served("browser"), ask=ask, book=book(),
                                 freetext=free, letter_maker=maker, planner=planfirst.Planner(None, served("headless pre-scan"), None), **kw)
    return type("W", (), {"run": staticmethod(run), "events": events, "out": out})


def script(events, *replies):
    queue = list(replies)

    def ask(prompt):
        events.append("ask: " + prompt.strip()[:40])
        return queue.pop(0) if queue else ""
    return ask


def test_everything_is_planned_in_the_terminal_before_the_browser_opens(world, capsys):
    r = world.run(script(world.events, "y", "y", "REF-7"))          # the plan, the draft (freetext's own [y] use), the confirmation
    ev = world.events
    browser_at = next(i for i, e in enumerate(ev) if e.startswith("browser"))
    assert ev[0] == "headless pre-scan" and ev.index("letter") < browser_at
    assert any(e.startswith("ask: [y] fill these") for e in ev[:browser_at])
    assert ev[browser_at] == "browser (answers_to_paste.txt already written)"
    text = capsys.readouterr().out
    assert text.index("ANSWER PLAN") < text.index("Filling the form from the plan") < text.index("Form ready: tick the declarations and press Submit yourself")
    assert r["confirmation"] == "REF-7" and any(f.startswith("Location (City)") for f in r["filled"])
    assert "What motivated you to apply for this position?" not in r["required_empty"]


def test_q_at_the_plan_opens_no_browser(world):
    r = world.run(script(world.events, "q"))
    assert not any(e.startswith("browser") for e in world.events) and r["confirmation"] == "x" and "no browser" in r["skipped"]


def test_a_context_closed_mid_fill_is_not_submitted_and_asks_nothing_more(world, monkeypatch, capsys):
    real, calls = planfill.Filler.put, []

    def put(self, row, dom):
        calls.append(row.n)
        if len(calls) == 3:
            self.page.context.close()
        return real(self, row, dom)
    monkeypatch.setattr(planfill.Filler, "put", put)
    ask = script(world.events, "y", "y")
    r = world.run(ask)
    assert r["browser_closed"] and r["confirmation"] == "x"
    assert not any("After submitting" in e for e in world.events)
    assert (world.out / "answers_to_paste.txt").is_file()
    assert "BROWSER CLOSED: nothing submitted" in capsys.readouterr().out


def test_the_headless_record_asks_nothing_and_fills_the_draft_marked_not_approved(world, monkeypatch, tmp_path):
    from careerops.apply import dryrun

    monkeypatch.setattr(dryrun, "folder_for", lambda rep, job, today=None: tmp_path / "rec")
    r = world.run(lambda p: pytest.fail(f"the record run asks nothing: {p}"), record=True)
    assert r["confirmation"] == "x" and (tmp_path / "rec" / "fields.md").is_file() and (tmp_path / "rec" / "plan.txt").is_file()
    motivation = next(x for x in r["required_table"] if x["label"].startswith("What motivated you"))
    assert motivation["status"] == "VERIFIED" and "DRAFT, not approved" in motivation["source"]
