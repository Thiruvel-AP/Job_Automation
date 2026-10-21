"""Free-text questions through the gated auto-apply and the assist, in headless Chromium on a fixture form (no network, MockLLM as the model).
A NEW draft is used only on the person's `y` (so it can never be submitted without it); an answer they approved before is reused without a
question and the form goes through. Chromium missing = the tests fail."""
import functools
from datetime import date
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from test_autoapply import APPLICANT, FIX, chromium, world  # noqa: F401  (the fixtures and the runner of the auto-apply tests)

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import autolog
from careerops.apply import freetext as ft
from careerops.llm import MockLLM

pytestmark = pytest.mark.slow          # these tests drive Chromium

FB = yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))
JD = (PROFILE_FIX.parent / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")
REP = {"keywords": [{"term": "llm", "weight": 3.0, "required": True, "alts": []}], "hook": {"proof_bullet_id": "az1"}}
ANSWER_1 = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab for a multi-omics cancer research platform."
ANSWER_2 = "For me the hardest part was keeping answers reproducible when upstream APIs went down."
VALUES_JS = "() => Object.fromEntries([...document.querySelectorAll('textarea')].map(e => [e.name, e.value]))"


def llm_for(*answers):
    queue = list(answers)
    llm = MockLLM({"free_text_answer": lambda ctx: {"answer": queue.pop(0) if len(queue) > 1 else queue[0], "ad_phrases": []}})
    return llm


def free(world, llm):
    book = answers_mod.load(PROFILE_FIX / "answers.yaml")
    return ft.for_job({**world.job, "description": JD}, REP, FB, llm, world.settings, book, ask=input, today=date(2026, 10, 21))


def run(world, monkeypatch, freetext, replies):
    monkeypatch.setattr(autoapply, "run", functools.partial(autoapply.run, freetext=freetext))
    return world.run("freetext_form.html", replies, submit=True)


@pytest.fixture
def seeded_questions(world):
    """The fixture form's own two questions are not the library seeds: nothing is reused until the person approves something."""
    return world


def test_a_new_draft_is_not_used_without_y_and_nothing_is_submitted(world, monkeypatch):
    r = run(world, monkeypatch, free(world, llm_for(ANSWER_1, ANSWER_2)), {"[y] use": ""})
    assert r.status == autolog.NEEDS_USER and "no answer for the required question" in r.reason and world.submit_calls == 0
    assert world.state["values"].get("q1", "") == ""


def test_a_new_draft_goes_in_on_y_is_stored_and_the_form_is_submitted(world, monkeypatch):
    r = run(world, monkeypatch, free(world, llm_for(ANSWER_1, ANSWER_2)), {"[y] use": ["y", "y"], "Submit?": "y"})
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1 and world.state["url"].endswith("auto_done.html")
    assert sum("[y] use" in p for p in world.prompts) == 2                       # one approval per new draft, asked before the submit
    ev = world.events
    assert ev.index("submit clicked") > max(i for i, e in enumerate(ev) if "[y] use" in e)       # both approvals come before the click
    assert sum("free text (approved)" in f for f in r.filled) == 2
    assert [rec.answer for rec in ft.Library(world.settings.path("answers_library")).records()][-2:] == [ANSWER_1, ANSWER_2]


def test_what_is_filled_is_the_approved_text_and_the_optional_question_stays_empty(world, monkeypatch):
    monkeypatch.setattr(autoapply, "run", functools.partial(autoapply.run, freetext=free(world, llm_for(ANSWER_1, ANSWER_2))))
    r = world.run("freetext_form.html", {"[y] use": ["y", "y"]}, submit=False)
    v = world.state["values"]
    assert r.status == autolog.NOT_SUBMITTED and (v["q1"], v["q2"], v["q3"]) == (ANSWER_1, ANSWER_2, "")

def test_an_approved_answer_is_reused_without_a_question_and_the_form_goes_through(world, monkeypatch):
    run(world, monkeypatch, free(world, llm_for(ANSWER_1, ANSWER_2)), {"[y] use": ["y", "y"], "Submit?": "y"})
    world.prompts.clear()
    world.submit_calls = 0
    never = llm_for("never used")
    r = run(world, monkeypatch, free(world, never), {"Submit?": "y"})
    assert r.status == autolog.SUBMITTED and world.submit_calls == 1
    assert not any("[y] use" in p for p in world.prompts)                          # reused: no question about the answer
    assert sum("free text (reused)" in f for f in r.filled) == 2


def test_a_draft_the_guard_rejects_is_never_shown_and_the_run_stops_for_the_person(world, monkeypatch):
    r = run(world, monkeypatch, free(world, llm_for("I built it on Kubernetes at Google serving 3.1m users.")), {})
    assert r.status == autolog.NEEDS_USER and world.submit_calls == 0
    assert not any("[y] use" in p for p in world.prompts)


def test_the_assist_drafts_too_and_leaves_what_it_could_not_answer_marked(chromium, tmp_path):
    page = chromium.new_page()
    page.goto((FIX / "freetext_form.html").resolve().as_uri())
    asked = []
    f = ft.for_job({"id": "manual:x", "company": "Northwind Health AI", "title": "AI Engineer", "description": JD}, REP, FB, llm_for(ANSWER_1, ANSWER_2),
                   type("S", (), {"path": lambda self, k: tmp_path / "library.jsonl"})(), answers_mod.load(PROFILE_FIX / "answers.yaml"),
                   ask=lambda p: asked.append(p) or ("y" if "[y] use" in p else ""))
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    report = assist_mod.fill_page(page, APPLICANT, str(cv), None, job=None, freetext=f)
    values = page.evaluate(VALUES_JS)
    page.close()
    assert values["q1"] == ANSWER_1 and values["q2"] == ANSWER_2 and values["q3"] == ""
    assert report["required_empty"] == [] and len(asked) == 2
