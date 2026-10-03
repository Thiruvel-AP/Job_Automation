"""Selection happens in code: bullets are ranked by weighted keyword overlap (text + terms), the most recent entry keeps
2-4, the others 1-2, exactly 2 projects; the model is never asked to choose, and the shrink loop enforces one page."""
import copy
import json
from pathlib import Path

import pytest

from careerops.tailoring import budget, selection, tailor
from careerops.tailoring.render import page_count, pdf_text
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import MOST_RECENT_BULLETS, OTHER_BULLETS, missing_keywords, select_cv, selected_ids


def _kws(*terms, weight=2.0):
    return [Keyword(t, weight, True) for t in terms]


# ---------------------------------------------------------------- ranking
def test_every_bullet_is_ranked_and_experience_and_projects_compete(fb_fixture):
    sc = budget.bullet_scores(fb_fixture, _kws("pytorch", "lstm", "langgraph", "pandas"))
    every = {b["id"] for sec in ("experience", "projects") for e in fb_fixture[sec] for b in e["bullets"]}
    assert set(sc) == every
    assert sc["p2a"] > sc["a4"] and sc["p3a"] > 0 and sc["b1"] > 0          # terms count too, not only the text


def test_bullet_text_and_terms_both_count_and_ties_are_stable(fb_fixture):
    sc = budget.bullet_scores(fb_fixture, _kws("mlflow"))
    assert sc["p1d"] == sc["p2c"] > 0 and sc["a1"] == 0
    assert budget.bullet_scores(fb_fixture, _kws("mlflow")) == sc


# ---------------------------------------------------------------- select_cv
def test_the_most_recent_entry_gets_2_to_4_bullets_the_others_1_to_2_and_exactly_2_projects(fb_fixture):
    sel = select_cv(fb_fixture, _kws("python", "docker", "langchain", "pytest", "sql", "mlflow", "pytorch"))
    sizes = {eid: len(ids) for eid, ids in sel["experience"].items()}
    assert set(sizes) == {"ex1", "ex2", "ex3", "ex4"}                      # every entry is on the CV
    assert 2 <= sizes["ex1"] <= 4 and all(1 <= n <= 2 for e, n in sizes.items() if e != "ex1")
    assert len(sel["projects"]) == 2 and len(set(sel["projects"])) == 2
    assert (MOST_RECENT_BULLETS, OTHER_BULLETS) == ((2, 4), (1, 2))


def test_a_jd_with_no_overlap_still_gets_the_minimum_from_every_entry(fb_fixture):
    sel = select_cv(fb_fixture, _kws("zzz-nothing"))
    assert [len(sel["experience"][e]) for e in ("ex1", "ex2", "ex3", "ex4")] == [2, 1, 1, 1]
    assert sel["experience"]["ex1"] == ["a1", "a2"]                         # ties keep fact-bank order


def test_an_entry_keeps_as_many_bullets_as_overlap_the_jd_up_to_its_maximum(fb_fixture):
    one = select_cv(fb_fixture, _kws("sql"))                                # ex2 has one SQL bullet (b1)
    assert one["experience"]["ex2"] == ["b1"]
    many = select_cv(fb_fixture, _kws("sql", "communication", "stakeholders", "documentation", "reporting"))
    assert len(many["experience"]["ex2"]) == 2 and many["experience"]["ex2"][0] == "b1"


def test_bullets_are_ordered_most_relevant_first(fb_fixture):
    sel = select_cv(fb_fixture, _kws("pytest", "docker", "iam", "aws lambda"))
    sc = budget.bullet_scores(fb_fixture, _kws("pytest", "docker", "iam", "aws lambda"))
    ids = sel["experience"]["ex1"]
    assert [sc[i] for i in ids] == sorted((sc[i] for i in ids), reverse=True) and ids[0] == "a3"


def test_projects_are_the_two_most_relevant_and_the_family_prior_only_breaks_ties(fb_fixture):
    kws = _kws("langgraph", "mcp", "rag")
    assert select_cv(fb_fixture, kws, preferred=["p1", "p2"])["projects"][0] == "p3"       # relevance beats the prior
    tie = select_cv(fb_fixture, _kws("zzz-nothing"), preferred=["p3", "p4"])["projects"]
    assert tie == ["p3", "p4"]                                                             # exact ties: the prior decides


def test_a_bank_with_fewer_than_two_projects_gives_what_it_has(fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    fb["projects"] = fb["projects"][:1]
    assert select_cv(fb, _kws("python"))["projects"] == ["p1"]


def test_selected_ids_are_in_cv_order_with_every_bullet_of_each_chosen_project(fb_fixture):
    sel = {"experience": {"ex1": ["a3", "a1"], "ex2": ["b2"], "ex3": [], "ex4": ["d1"]}, "projects": ["p3", "p1"]}
    assert selected_ids(fb_fixture, sel) == ["a3", "a1", "b2", "d1", "p3a", "p3b", "p1a", "p1b", "p1c", "p1d"]


def test_selection_is_deterministic(fb_fixture):
    kws = _kws("python", "docker", "mlflow", "sql")
    assert select_cv(fb_fixture, kws) == select_cv(fb_fixture, kws)


# ---------------------------------------------------------------- the bullets' own words
def test_missing_keywords_are_the_ones_the_terms_evidence_and_the_text_does_not_say(fb_fixture):
    a4 = tailor.bullet_index(fb_fixture)["a4"]               # text: "test framework ... ~65 tests"; terms: pytest, ...
    kws = [Keyword("pytest", 2, True), Keyword("docker", 2, True), Keyword("integration testing", 1, False),
           Keyword("Kubernetes", 1, False)]
    assert missing_keywords(a4, kws) == ["pytest", "integration testing"]    # docker/kubernetes: no evidence at all


def test_a_keyword_the_text_already_says_is_not_missing_and_one_word_per_any_of_keyword(fb_fixture):
    a3 = tailor.bullet_index(fb_fixture)["a3"]               # text says Docker; terms say serverless, mlops, ...
    assert missing_keywords(a3, [Keyword("docker", 2, True)]) == []
    k = Keyword("serverless computing", 1, False, ["serverless", "FaaS"])
    assert missing_keywords(a3, [k]) == ["serverless"]       # the first member the terms evidence, once
    assert missing_keywords(a3, [Keyword("llms", 1, False, ["docker"])]) == []     # an alternative the text says


def test_chunks_split_in_order_into_batches_of_at_most_n():
    assert [list(c) for c in selection.chunks(list("abcdefghijk"), 5)] == [list("abcde"), list("fghij"), ["k"]]
    assert list(selection.chunks([], 5)) == []


# ---------------------------------------------------------------- in the pipeline
def test_the_model_is_never_asked_to_select_and_the_cv_follows_the_code_selection(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert "tailor_cv" not in [s["task"] for s in llm.sent]
    kws = [tailor.Keyword(**k) for k in rep["keywords"]]
    first = rep["attempts"][0]
    assert first["proposal"]["selection"] == select_cv(fb_fixture, kws)    # the family's project ids are not in this bank
    assert len(rep["context"]["projects"]) == 2
    saved = json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    assert saved["attempts"][0]["selection"] == first["selection"]


def test_two_runs_of_one_job_choose_the_same_bullets(settings, fb_fixture, job, spy_llm):
    a = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))["context"]
    b = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))["context"]
    ids = lambda c: [e["bullet_ids"] for e in c["experience"] + c["projects"]]
    assert ids(a) == ids(b)


def test_the_shrink_loop_brings_the_selection_to_one_page(one_page_run):
    rep, _ = one_page_run
    assert max(a["shrinks"] for a in rep["attempts"]) >= 1, "fixture is too short to exercise the shrink loop"
    assert rep["final"]["pages"] == 1 and page_count(Path(rep["cv_pdf"])) == 1
    first = rep["context"]["experience"][0]
    assert len(first["bullets"]) >= 2 and all(len(e["bullets"]) >= 1 for e in rep["context"]["experience"])


def test_shrinking_drops_the_least_relevant_bullet_and_keeps_two_for_the_most_recent_entry_until_it_must():
    kws = _kws("python")
    ctx = {"experience": [{"bullet_ids": ["x1", "x2"], "bullets": ["Used Python.", "Wrote prose."]},
                          {"bullet_ids": ["y1", "y2"], "bullets": ["Wrote prose too.", "Used Python daily."]}],
           "projects": [{"bullet_ids": ["p1"], "bullets": ["Only bullet."]}]}
    assert tailor._shrink(ctx, kws) and ctx["experience"][1]["bullet_ids"] == ["y2"]     # y1: 0 hits, and not the first entry
    assert tailor._shrink(ctx, kws) and ctx["experience"][0]["bullet_ids"] == ["x1"]     # nothing else can go: relax the floor
    assert not tailor._shrink(ctx, kws)                                                  # one bullet left everywhere


# ---------------------------------------------------------------- the attempt loop
def _below_band(settings):
    settings.raw["scoring"]["band"] = [0.95, 1.0]           # the broad-JD CV scores ~0.83: below, with a ceiling of 1.0


def test_a_below_band_attempt_ranks_the_next_one_with_the_missed_keywords_counted_double(settings, fb_fixture,
                                                                                            broad_job, broad_llm,
                                                                                            monkeypatch):
    _below_band(settings)
    seen = []
    real = tailor.select_cv
    monkeypatch.setattr(tailor, "select_cv", lambda fb, kws, pref=None: seen.append({k.term: k.weight for k in kws})
                        or real(fb, kws, pref))
    rep = tailor.tailor_job(broad_job, fb_fixture, broad_llm, settings, settings.path("outputs"))
    first = rep["attempts"][0]
    assert first["band"] == "below" and first["misses"]
    assert len(seen) >= 2
    for term in first["misses"]:
        assert seen[1][term] == 2 * seen[0][term]                      # fixable and missed: ranked double
    assert all(seen[1][t] == seen[0][t] for t in seen[0] if t not in first["misses"] + first["wording_misses"])


def test_attempts_do_not_repeat_themselves_when_the_selection_would_not_change(settings, fb_fixture, broad_job,
                                                                                broad_llm):
    _below_band(settings)
    rep = tailor.tailor_job(broad_job, fb_fixture, broad_llm, settings, settings.path("outputs"))
    assert len(rep["attempts"]) == 1 and rep["final"]["chosen_attempt"] == 1


def test_later_attempts_reuse_rephrase_answers_and_the_profile_is_written_once(settings, fb_fixture, broad_job,
                                                                                broad_llm, monkeypatch):
    _below_band(settings)
    real, calls = tailor.select_cv, []

    def rotating(fb, kws, preferred=None):                 # a different selection each time, as a real boost may give
        sel = real(fb, kws, preferred)
        calls.append(1)
        ids = sel["experience"]["ex1"]
        k = len(calls) % len(ids)
        sel["experience"]["ex1"] = ids[k:] + ids[:k]
        return sel

    monkeypatch.setattr(tailor, "select_cv", rotating)
    llm = broad_llm
    rep = tailor.tailor_job(broad_job, fb_fixture, llm, settings, settings.path("outputs"))
    assert len(rep["attempts"]) == settings["scoring"]["max_attempts"] == 3
    tasks = [s["task"] for s in llm.sent]
    assert tasks.count("tailor_profile") == 1
    first, later = rep["attempts"][0]["rephrase"], rep["attempts"][1:]
    assert first["asked"] == first["eligible"] > 0 and first["calls"] >= 1
    assert all(a["rephrase"]["asked"] == 0 and a["rephrase"]["cached"] == a["rephrase"]["eligible"] for a in later)
    assert tasks.count("rephrase_bullets") == first["calls"]               # nothing was asked twice
