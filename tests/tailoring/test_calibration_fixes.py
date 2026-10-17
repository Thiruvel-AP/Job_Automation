"""Calibration fixes: keyword collapse on generic head words, skills de-duplicated on the CV, and the reason a letter fell
back. (Frozen keyword files are tested in tests/reporting/test_calibrate.py.) No network, no model."""
import pytest
import copy
from pathlib import Path

from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword, drop_contained
from careerops.tailoring.tailor import dedupe_skills, letter_summary, tailor_job


# ---------------------------------------------------------------- keywords: generic head words collapse
def test_retrieval_pipelines_collapses_into_retrieval():
    kws, notes = drop_contained([Keyword("retrieval pipelines", 2, True), Keyword("retrieval", 1, False)])
    assert [k.term for k in kws] == ["retrieval"]
    assert kws[0].required and kws[0].weight == 2                   # the kept term takes the heavier weight
    assert notes == ["dropped 'retrieval pipelines' (contains 'retrieval' plus only generic head words)"]


def test_it_also_collapses_when_the_kept_term_is_an_any_of_keyword():
    # "retrieval" was merged with "RAG" into one any-of keyword
    kws, notes = drop_contained([Keyword("retrieval pipelines", 2, True), Keyword("retrieval", 1, False, ["RAG"])])
    assert [k.term for k in kws] == ["retrieval"] and kws[0].alts == ["RAG"] and kws[0].required
    assert notes == ["dropped 'retrieval pipelines' (contains 'retrieval' plus only generic head words)"]


def test_every_generic_head_word_collapses_and_each_drop_is_reported():
    heads = ["pipelines", "systems", "frameworks", "tasks", "approaches", "workflows", "features", "architectures", "loops",
             "environments"]
    kws, notes = drop_contained([Keyword("search", 1, False, ["lookup"])] + [Keyword(f"search {h}", 1, False) for h in heads])
    assert [k.term for k in kws] == ["search"] and len(notes) == len(heads)
    assert all("generic head words" in n for n in notes)


def test_a_term_with_a_meaningful_extra_word_is_not_collapsed_into_an_any_of_keyword():
    kws, notes = drop_contained([Keyword("retrieval evaluation", 1, False), Keyword("retrieval", 1, False, ["RAG"])])
    assert {k.term for k in kws} == {"retrieval evaluation", "retrieval"} and notes == []


# ---------------------------------------------------------------- skills render once
def test_dedupe_skills_is_case_insensitive_within_and_across_groups_and_keeps_the_first():
    out = dedupe_skills({"Languages": ["Python", "SQL", "python"], "Tools": ["Docker", "PYTHON", "sql", "Git"],
                         "Again": ["DOCKER", "git"]})
    assert out == {"Languages": ["Python", "SQL"], "Tools": ["Docker", "Git"]}          # "Again" is empty: dropped


@pytest.mark.slow
def test_a_skill_listed_twice_renders_once_on_the_cv(settings, fb_fixture, job, spy_llm):
    fb = copy.deepcopy(fb_fixture)
    fb["skills"]["ML & Stats"] += ["Zorblax", "ZORBLAX"]
    fb["skills"]["Engineering"] += ["zorblax"]
    rep = tailor_job(job, fb, spy_llm(), settings, settings.path("outputs"))
    groups = rep["context"]["skills"]                                                   # the four fixed groups of cv.skill_groups
    names = [s.casefold() for items in groups.values() for s in items]
    assert names.count("zorblax") == 1 and len(names) == len(set(names))
    home = [g for g, items in groups.items() if "Zorblax" in items]                     # the first occurrence stays, in its own casing
    assert home == ["Machine Learning, Deep Learning & Statistics"]
    assert pdf_text(Path(rep["cv_pdf"])).casefold().count("zorblax") == 1


# ---------------------------------------------------------------- the letter's rejection reason
def test_letter_summary_names_the_first_rejection_reason():
    fell = {"status": "fallback", "problems": ["sentence 2: claim not evidenced", "banned closer", "used fallback letter"]}
    assert letter_summary(fell) == "fallback (sentence 2: claim not evidenced)"
    assert letter_summary({"status": "ok", "problems": []}) == "ok"
    assert letter_summary({"status": "fallback", "problems": ["used fallback letter"]}) == "fallback (no reason recorded)"
