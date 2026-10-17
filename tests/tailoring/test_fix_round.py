"""Fix round from calibration report 20261006-151213: the projects cap and fill order, the wording pass, and the letter's
AVOID list and named-words retry. No network, no model (MockLLM / SpyLLM on the fictional fact banks)."""
import copy
import re
from pathlib import Path

import pytest
from conftest import restate

from careerops.tailoring import tailor
from careerops.tailoring.grounding import avoid_words, grounding_problems, rejected_words
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import TWO_PAGE, fill_order, select_cv


def _kws(*terms):
    return [Keyword(t, 1.0, True) for t in terms]


def _b(bid, *words):
    return {"id": bid, "text": " ".join(words) + " work", "terms": []}


# ---------------------------------------------------------------- 2. the projects: a cap, and the fill order
def _bank():
    def proj(pid, n, tag):
        return {"id": pid, "bullets": [_b(f"{pid}{i}", f"{tag}{i}", f"x{pid}{i}") for i in range(n)]}

    return {"skills": {}, "experience": [{"id": "e1", "bullets": [_b("a0", "ea0", "ya0"), _b("a1", "ea1", "ya1")]}],
            "projects": [proj("p1", 4, "one"), proj("p2", 4, "two"), proj("p3", 3, "three"), proj("p4", 4, "four"),
                         proj("p5", 3, "five"), proj("p6", 3, "six"), proj("p7", 3, "seven")]}


def test_the_selection_never_holds_more_projects_than_the_layout_with_two_or_three_bullets_each():
    fb = _bank()
    kws = _kws(*[f"{t}{i}" for t in ("one", "two", "three", "four", "five", "six", "seven") for i in range(4)])
    sel = select_cv(fb, kws, layout=TWO_PAGE)
    assert len(sel["projects"]) == TWO_PAGE.projects == 5                        # 7 projects in the bank, 5 on the CV
    assert all(2 <= len(sel["project_bullets"][p]) <= 3 for p in sel["projects"])


def test_fill_adds_a_further_project_before_more_bullets_of_the_shown_ones():
    fb = _bank()
    # the 4th project is the one the JD talks about most, but it did not make the three: with room for a 4th it still comes first
    kws = _kws(*[f"{t}{i}" for t in ("one", "two", "three") for i in range(2)], *[f"four{i}" for i in range(4)],
               "four0")
    sel = {"experience": {"e1": ["a0", "a1"]}, "projects": ["p1", "p2", "p3"],
           "project_bullets": {"p1": ["p10", "p11"], "p2": ["p20", "p21"], "p3": ["p30", "p31"]}}
    order = fill_order(fb, kws, sel, max_projects=4)
    chosen_rest = {"p12", "p13", "p22", "p23", "p32"}
    first_chosen = min(order.index(b) for b in chosen_rest)
    assert all(order.index(b) < first_chosen for b in order if b.startswith("p4"))
    assert chosen_rest <= set(order) and {"p40", "p41", "p42", "p43"} <= set(order)
    assert not any(b.startswith(("p5", "p6", "p7")) for b in order)               # the maximum of 4 projects is not exceeded


def test_without_a_maximum_no_further_project_enters_the_fill():
    fb = _bank()
    sel = {"experience": {"e1": ["a0", "a1"]}, "projects": ["p1", "p2", "p3"],
           "project_bullets": {"p1": ["p10", "p11"], "p2": ["p20", "p21"], "p3": ["p30", "p31"]}}
    order = fill_order(fb, _kws("four0", "four1"), sel)                  # no max_projects: only the shown projects and the jobs
    assert order and not any(b.startswith("p4") for b in order)
    assert any(b.startswith("p4") for b in fill_order(fb, _kws("four0"), sel, max_projects=4))


def test_the_defaults_start_with_five_projects_and_may_fill_to_six(settings):
    sc = settings["scoring"]
    assert sc["cv_projects"] == 5 and sc["cv_projects_max"] == 6 and "cv_fill_extra_projects" not in sc


# ---------------------------------------------------------------- 3. the wording pass
JD_PLAIN = "Requirements\nPython\n"
JD ="Requirements\nPrototyping\nPython\n"


@pytest.fixture
def prototype_bank(fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    b1 = next(b for e in fb["experience"] for b in e["bullets"] if b["id"] == "b1")
    b1["text"] = "Prototyped a reporting notebook with pandas and SQL that the charity's trustees reused each quarter."
    b1["terms"] = ["pandas", "sql", "reporting"]              # the terms do not evidence "prototyping": the first rephrase skips it
    return fb


def _kw_handler(*terms):
    return lambda ctx: {"required": list(terms), "preferred": [], "company_hook": "", "team": ""}


def _exact_form(ctx):
    """A model that does what the EXACT FORM line asks, and nothing else."""
    out = {}
    for b in ctx["bullets"]:
        if b.get("exact_form"):
            out[b["id"]] = b["text"].replace("Prototyped", "Prototyping", 1)
    return out


def _run(settings, fb, job, llm, jd=JD):
    settings.raw["scoring"]["band"] = [0.0, 1.0]       # two keywords swing the score 50% <-> 100%: no band revert in these tests
    settings.raw["hook"] = {"enabled": False}          # the hook's problem clause is made of the JD keywords: it would be the literal hit these tests need missing
    return tailor.tailor_job({**job, "description": jd + "x" * 900}, fb, llm, settings, settings.path("outputs"))


@pytest.mark.slow
def test_a_wording_miss_gets_the_exact_form_in_the_one_bullet_that_carries_its_stem(settings, prototype_bank, job, spy_llm):
    llm = spy_llm({"extract_keywords": _kw_handler("Prototyping", "Python"), "rephrase_bullets": _exact_form})   # taxonomy: lower case
    rep = _run(settings, prototype_bank, job, llm)
    first = rep["attempts"][0]["wording"]
    assert first["terms"] == ["prototyping"] and first["accepted"] == ["b1"] and first["blocked"] == []
    asked = [s for s in llm.sent if s["task"] == "rephrase_bullets" and any(b.get("exact_form") for b in s["context"]["bullets"])]
    assert len(asked) == 1 and [b["id"] for b in asked[0]["context"]["bullets"]] == ["b1"]     # only that bullet
    assert "EXACT FORM: prototyping" in asked[0]["user"] and "EXACT FORM" in asked[0]["system"]
    assert "Prototyping" in pdf_text(Path(rep["cv_pdf"])) and rep["final"]["wording_misses"] == []
    assert rep["final"]["score"] > 0 and rep["attempts"][0]["wording"]["kept"] is True


@pytest.mark.slow
def test_the_exact_form_rewrite_goes_through_the_guard_and_an_invented_number_is_blocked(settings, prototype_bank, job,
                                                                                         spy_llm):
    def lying(ctx):
        return {b["id"]: b["text"].replace("Prototyped", "Prototyping", 1).rstrip(".") + ", saving 40% of the effort."
                for b in ctx["bullets"] if b.get("exact_form")}

    llm = spy_llm({"extract_keywords": _kw_handler("Prototyping", "Python"), "rephrase_bullets": lying})
    rep = _run(settings, prototype_bank, job, llm)
    w = rep["attempts"][0]["wording"]
    assert w["accepted"] == [] and w["blocked"] == ["b1"]
    text = pdf_text(Path(rep["cv_pdf"]))
    assert "40%" not in text and "Prototyped a reporting notebook" in text           # the canonical bullet stays
    assert "prototyping" in rep["final"]["wording_misses"]


@pytest.mark.slow
def test_no_wording_pass_when_every_keyword_is_already_in_its_exact_form(settings, prototype_bank, job, spy_llm):
    llm = spy_llm({"extract_keywords": _kw_handler("Python")})
    rep = _run(settings, prototype_bank, job, llm, JD_PLAIN)
    assert rep["attempts"][0]["wording"]["terms"] == [] and rep["attempts"][0]["wording"]["asked"] == 0
    assert not any(b.get("exact_form") for s in llm.sent if s["task"] == "rephrase_bullets" for b in s["context"]["bullets"])


def test_a_keyword_only_the_skills_carry_is_reported_not_asked_for(fb_fixture):
    kws = [Keyword("prompts", 2.0, True)]
    ctx = {"experience": [{"bullet_ids": ["x1"], "bullets": ["Built a service in Python."]}], "projects": []}
    plan, unplaced = tailor.wording_targets(ctx, kws, ["prompts"])
    assert plan == {} and unplaced == ["prompts"]
    ctx["experience"][0]["bullets"][0] = "Wrote the prompt templates for a service."
    plan, unplaced = tailor.wording_targets(ctx, kws, ["prompts"])
    assert plan == {"x1": ["prompts"]} and unplaced == []


# ---------------------------------------------------------------- 4. the letter: AVOID list and the named-words retry
ev = [{"id": "a1", "text": "Built a tool-calling LLM agent for a research platform.", "terms": ["llm", "agent"]},
      {"id": "b1", "text": "Cleaned and joined exports with pandas and SQL.", "terms": ["pandas", "sql"]}]


def test_the_avoid_list_is_the_filler_and_capability_words_nothing_supports():
    words = avoid_words(ev, "We build data tools.", "", "Northwind AI Engineer")
    for w in ("real-time", "end-to-end", "scalable", "robust", "seamless", "cutting-edge", "production"):
        assert w in words
    ev2 = [{**ev[0], "text": ev[0]["text"] + " Ran it in production with real-time updates."}, ev[1]]
    again = avoid_words(ev2, "A robust platform.", "", "")
    assert "production" not in again and "real-time" not in again and "robust" not in again   # the evidence / JD says them
    assert "end-to-end" in again


def test_rejected_words_are_read_from_the_guards_problems():
    probs = ["'real-time' (capability) is in neither the evidence nor the job description: 'x'",
             "vague filler 'scalable' is not in the evidence", "deny-listed 'kubernetes'", "142 words > 140",
             "'fintech' (sector) is in neither the evidence nor the job description: 'y'"]
    assert rejected_words(probs) == ["real-time", "scalable", "kubernetes", "fintech"]
    problems = grounding_problems(["I built a real-time agent."], ev, "We build data tools.", "", "", [])
    assert rejected_words(problems) == ["real-time"]


def _drafts(*drafts):
    queue = list(drafts)
    return lambda ctx: {"paragraphs": (queue.pop(0) if len(queue) > 1 else queue[0])(ctx["evidence"])}


def _i(bullet: str) -> str:
    return restate(bullet)          # first-person prose in other words: a bullet pasted, even with "I" in front, is rejected


def _with_filler(e):
    return ["Northwind Health AI builds tools I have worked on.", _i(e[0]) + " It was a real-time, scalable system.", _i(e[1])]


def _clean(e):
    return ["Northwind Health AI builds tools I have worked on.", _i(e[0]), _i(e[1])]


@pytest.mark.slow
def test_the_prompt_lists_the_unsupported_words_under_avoid(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"cover_letter": _drafts(_clean)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    call = [s for s in llm.sent if s["task"] == "cover_letter"][0]
    line = next(l for l in call["user"].splitlines() if l.startswith("AVOID"))
    assert "real-time" in line and "robust" in line and "scalable" in line     # "end-to-end" is in this JD: not listed
    assert call["context"]["avoid"] and "AVOID" in call["system"]
    assert rep["letter"]["status"] == "ok" and rep["letter"]["retry"]["count"] == 0


@pytest.mark.slow
def test_a_rejected_draft_is_retried_once_naming_the_words_and_the_retry_is_logged(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"cover_letter": _drafts(_with_filler, _clean)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    calls = [s for s in llm.sent if s["task"] == "cover_letter"]
    assert len(calls) == 2
    second = calls[1]["user"]
    assert "real-time" in second and "scalable" in second
    assert "Do not use 'real-time' unless it appears in the evidence bullets" in second
    assert "YOUR LAST DRAFT BROKE THESE RULES" in second
    letter = rep["letter"]
    assert letter["status"] == "ok" and letter["problems"] == []
    r = letter["retry"]
    assert r["count"] == 1 and {"real-time", "scalable"} <= set(r["rejected_words"]) and r["outcome"] == "ok after retry"
    assert r["first_problems"] and any("retry 1 of 2, naming" in n for n in letter["notes"])


@pytest.mark.slow
def test_a_second_rejection_falls_back_and_the_retry_is_still_logged(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    llm = spy_llm({"cover_letter": _drafts(_with_filler)})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert [s["task"] for s in llm.sent].count("cover_letter") == 3               # a draft, two retries, then the fallback
    letter = rep["letter"]
    assert letter["status"] == "fallback" and letter["retry"]["count"] == 2 and letter["retry"]["outcome"] == "fallback"
    assert "real-time" in letter["retry"]["rejected_words"]
    assert "real-time" not in " ".join(letter["paragraphs"])


@pytest.mark.slow
def test_the_retry_reaches_the_regenerated_letter_report_too(settings, fb_fixture, job, spy_llm):
    rep = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))
    llm = spy_llm({"cover_letter": _drafts(_with_filler, _clean)})
    rep = tailor.regenerate_letter(job, fb_fixture, llm, settings, rep)
    assert rep["letter"]["retry"]["count"] == 1
