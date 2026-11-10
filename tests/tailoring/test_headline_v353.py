"""v3.5.3 (10 Nov 2026, fix 2): the headline is "<family title> | <hook headline>". With no hook that passes, the review shows a red warning ("no hook:
the headline has no number") and the profile still opens with the family's opening sentence (`profile_opening`), never mid-paragraph."""
import copy
from pathlib import Path

import pytest

from careerops import hook as H
from careerops.llm.mock import MockLLM
from careerops.review import workflow
from careerops.tailoring import doccheck, render, tailor
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

OPENING = "AI engineer with an MSc in Data Science who takes LLM agents from prototype to deployment, with automated tests around them."
BODY = "Built a tool-calling LLM agent on AWS Bedrock for a multi-omics cancer research platform and deployed it on AWS Lambda."


def test_the_shown_headline_is_the_family_title_then_the_hook():
    ctx = {"hook_headline": "LLM agent checked by ~65 tests", "headline_title": "AI Engineer", "id": {"headline": "AI Engineer"}}
    assert tailor.shown_headline(ctx) == "AI Engineer | LLM agent checked by ~65 tests"
    assert tailor.shown_headline({**ctx, "headline_title": ""}) == "LLM agent checked by ~65 tests"
    assert tailor.shown_headline({"id": {"headline": "AI Engineer"}}) == "AI Engineer"              # no hook: the family title alone, no number
    head = tailor.letter_header(ctx, [Keyword("python", 1.0, True)])
    assert head == {"headline": "AI Engineer | LLM agent checked by ~65 tests", "number": ""}


def test_the_template_prints_the_title_bar_hook_line_and_nothing_else_without_a_hook():
    env = render.env()
    base = dict(id={"name": "A B", "location": "X", "email": "a@b.c", "headline": "AI Engineer"}, profile="p", skills={"Core": ["Python"]},
                experience=[], projects=[], education=[], certifications=[])
    with_hook = env.get_template("cv2.tex.j2").render(**base, hook_headline="LLM agent checked by ~65 tests", hook_number="~65",
                                                      headline_title="AI Engineer")
    assert r"\textbf{AI Engineer | LLM agent checked by \num{\textasciitilde{}65} tests}" in with_hook
    bare = env.get_template("cv2.tex.j2").render(**base)
    assert r"\small\textbf{AI Engineer}}" in bare and "AI Engineer |" not in bare                     # no hook: the title alone
    one = env.get_template("cv.tex.j2").render(**base, hook_headline="LLM agent checked by ~65 tests", hook_number="~65",
                                               headline_title="AI Engineer")
    assert "AI Engineer | LLM agent checked by" in one


def test_no_hook_review_warning_is_red_on_a_terminal_and_plain_otherwise():
    rep = {"hook": None, "hook_family_headline": "AI Engineer", "hook_notes": ["no hook_library entry hits at least 2 of the ad's top 10 keywords"]}
    red = workflow.hook_lines(rep, color=True)
    assert red[0] == "\033[31m!! no hook: the headline has no number (the CV headline is the role family's 'AI Engineer' alone)\033[0m"
    plain = workflow.hook_lines(rep, color=False)
    assert plain[0] == "!! no hook: the headline has no number (the CV headline is the role family's 'AI Engineer' alone)"
    assert plain[1].startswith("Hook: none qualifies")
    assert workflow.hook_lines({"hook": {"hook_id": "h", "problem": "p", "proof_bullet_id": "a4", "org": "O", "number": "~65", "source": "library",
                                         "headline": "x", "letter_sentence": "y"}}, color=True)[0].startswith("Hook h")      # a hook: no warning


def _rep(profile: str) -> dict:
    return {"context": {"profile": profile, "id": {"headline": "AI Engineer"}}, "hook": None, "headline": "AI Engineer",
            "family_line": BODY, "family_opening": OPENING}


def test_the_document_check_wants_the_opening_sentence_first_when_there_is_no_hook():
    assert doccheck.hook_problems(_rep(f"{OPENING} {BODY} More.")) == []
    assert doccheck.hook_problems(_rep(f"{BODY} More.")) == ["HOOK profile sentence 1 is not the role family's opening sentence (profile_opening)"]
    older = {**_rep(f"{BODY} More."), "family_opening": ""}                     # a bank family without profile_opening: the body is checked
    assert doccheck.hook_problems(older) == []


def _family_bank(fb: dict, opening: str | None) -> dict:
    fb = copy.deepcopy(fb)
    fb.pop("hook_library", None)                                               # no hook can qualify
    fam = {"id": "ai_engineer", "match": ["ai engineer", "llm", "agent"], "title": "AI Engineer", "profile_body": BODY,
           "skill_lines": [{"label": "LLMs & Agents", "items": "LangChain, LangGraph, AWS Bedrock, tool calling"},
                           {"label": "Engineering", "items": "Python, FastAPI, Docker, pytest"}]}
    if opening:
        fam["profile_opening"] = opening
    fb["families"] = [fam]
    return fb


@pytest.mark.slow
def test_with_no_hook_the_profile_opens_with_the_familys_opening_sentence(settings, fb_fixture, job):
    rep = tailor.tailor_job(job, _family_bank(fb_fixture, OPENING), MockLLM(), settings, settings.path("outputs"))
    assert rep["hook"] is None and rep["family_opening"] == OPENING
    assert rep["context"]["profile"].startswith(f"{OPENING} {BODY}")
    assert "headline_title" not in rep["context"] and rep["context"]["id"]["headline"] == rep["headline"]
    flat = " ".join(pdf_text(Path(rep["cv_pdf"])).split())
    assert OPENING in flat and " | " not in flat.split("Bristol")[0]          # the headline line: the family title alone
    assert doccheck.hook_problems(rep) == []
    assert workflow.hook_lines(rep, color=False)[0].startswith("!! no hook: the headline has no number")


@pytest.mark.slow
def test_a_family_without_an_opening_sentence_is_reported(settings, fb_fixture, job):
    rep = tailor.tailor_job(job, _family_bank(fb_fixture, None), MockLLM(), settings, settings.path("outputs"))
    assert rep["family_opening"] == "" and rep["context"]["profile"].startswith(BODY)
    assert any("has no profile_opening" in w for w in rep["analysis_warnings"])


@pytest.mark.slow
def test_with_a_hook_the_cv_headline_is_title_bar_hook_and_the_letter_header_matches(settings, fb_fixture, job):
    s = settings
    s.raw["letter"]["mode"] = "company"
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), s, s.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    shown = f"{rep['context']['headline_title']} | {hk.headline}"
    lines = [" ".join(ln.split()) for ln in pdf_text(Path(rep["cv_pdf"])).splitlines() if ln.strip()]
    assert lines[1] == " ".join(shown.split()) and rep["context"]["headline_title"] == rep["headline"]
    assert rep["context"]["profile"].startswith(hk.profile_line)                # the hook's sentence opens the profile, not the family opening
    assert rep["letter"]["status"] == "ok", rep["letter"].get("reason")
    tex = Path(rep["letter_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    head = tailor.letter_header(rep["context"], [Keyword(**k) for k in rep["keywords"]])
    assert render.hook_tex(head["headline"], head["number"], "num") in tex and head["headline"].startswith(rep["headline"] + " | ")
