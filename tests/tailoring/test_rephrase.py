"""Rephrase: bullets that can carry a JD keyword their text lacks are reworded by the model in batches of <= 5
(text, terms, the keywords it may introduce) -> {id: new text}; every rewrite goes through the faithfulness guard;
coverage is logged."""
import json
import math
from pathlib import Path

import pytest

from careerops.tailoring import prompts as P
from careerops.tailoring import tailor
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import missing_keywords, selected_ids
from careerops.tailoring.shapes import normalise_rephrase

def _all_terms(fb):
    """Every `terms` entry in the bank, as the JD keywords: nearly every bullet can then carry a keyword it lacks."""
    return sorted({t for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]
                   for t in b.get("terms", [])})


def _kws(*terms):
    return [Keyword(t, 2.0, True) for t in terms]


def _everything(fb):
    return {"experience": {e["id"]: [b["id"] for b in e["bullets"]] for e in fb["experience"]},
            "projects": [p["id"] for p in fb["projects"]]}


def _ask(fb, kws, llm, sel=None, cache=None, job=None):
    notes: list[str] = []
    job = job or {"title": "AI Engineer", "company": "Northwind"}
    proposed, stats = tailor.propose_rephrases(job, fb, sel or _everything(fb), kws, llm, cache if cache is not None
                                               else {}, notes)
    return proposed, stats, notes


# ---------------------------------------------------------------- batches
def test_bullets_go_to_the_model_in_batches_of_at_most_five(fb_fixture, spy_llm):
    kws = _kws(*_all_terms(fb_fixture))
    sel = _everything(fb_fixture)
    bidx = tailor.bullet_index(fb_fixture)
    offers, _ = tailor.placement_offers(fb_fixture, sel, kws)      # each missed keyword goes to one bullet, <= 2 per bullet
    eligible = [i for i in selected_ids(fb_fixture, sel) if offers.get(i)]
    assert len(eligible) > 10 and len(eligible) < len([i for i in selected_ids(fb_fixture, sel)
                                                       if missing_keywords(bidx[i], kws)])      # the fixture needs several batches
    llm = spy_llm()
    _, stats, _ = _ask(fb_fixture, kws, llm)
    sizes = [len(s["context"]["bullets"]) for s in llm.sent]
    assert max(sizes) == 5 and sum(sizes) == len(eligible) and len(sizes) == math.ceil(len(eligible) / 5)
    assert stats["calls"] == len(sizes) and stats["eligible"] == stats["asked"] == len(eligible)
    assert {s["task"] for s in llm.sent} == {"rephrase_bullets"}


def test_a_bullet_that_cannot_carry_a_keyword_is_not_sent(fb_fixture, spy_llm):
    llm = spy_llm()
    _, stats, _ = _ask(fb_fixture, _kws("zzz-nothing"), llm)
    assert llm.sent == [] and stats["eligible"] == 0 and stats["calls"] == 0


def test_each_bullet_carries_its_text_its_terms_and_the_keywords_it_may_introduce(fb_fixture, spy_llm):
    llm = spy_llm()
    _ask(fb_fixture, _kws("pytest", "integration testing", "docker"), llm, {"experience": {"ex1": ["a3", "a4"]},
                                                                           "projects": []})
    call = llm.sent[0]
    item = next(b for b in call["context"]["bullets"] if b["id"] == "a4")
    bidx = tailor.bullet_index(fb_fixture)
    assert item == {"id": "a4", "text": bidx["a4"]["text"], "terms": bidx["a4"]["terms"],
                    "introduce": ["pytest", "integration testing"]}
    assert [b["id"] for b in call["context"]["bullets"]] == ["a4"]           # a3 already says Docker
    user = call["user"]
    assert "id: a4" in user and bidx["a4"]["text"] in user and "MAY INTRODUCE: pytest, integration testing" in user
    assert "TARGET ROLE: AI Engineer" in user and "Never mention:" in call["system"]
    assert "{deny}" not in call["system"]


def test_the_deny_list_is_in_the_system_prompt(fb_fixture, spy_llm):
    fb = dict(fb_fixture, deny_terms=["kubernetes", "terraform"])
    llm = spy_llm()
    _ask(fb, _kws("pytest"), llm, {"experience": {"ex1": ["a4"]}, "projects": []})
    assert "Never mention: kubernetes, terraform." in llm.sent[0]["system"]


def test_the_prompt_states_the_rules():
    s = P.REPHRASE_SYSTEM
    for must in ("never add a fact", "at most 30 words", "past-tense action verb", "MAY INTRODUCE", "Keep every number",
                 "unchanged", "{deny}"):
        assert must in s, must


# ---------------------------------------------------------------- answers
def test_the_answer_maps_bullet_ids_to_new_text(fb_fixture, spy_llm):
    new = "Wrote a three-tier pytest framework with integration testing across 8 files."
    llm = spy_llm({"rephrase_bullets": lambda ctx: {"a4": new}})
    proposed, stats, _ = _ask(fb_fixture, _kws("pytest"), llm, {"experience": {"ex1": ["a4"]}, "projects": []})
    assert proposed == {"a4": new}


def test_unchanged_text_empty_answers_and_unknown_ids_propose_nothing(fb_fixture, spy_llm):
    bidx = tailor.bullet_index(fb_fixture)
    llm = spy_llm({"rephrase_bullets": lambda ctx: {"a4": bidx["a4"]["text"], "zz9": "invented", "a3": ""}})
    proposed, _, notes = _ask(fb_fixture, _kws("pytest", "iam"), llm, {"experience": {"ex1": ["a3", "a4"]},
                                                                      "projects": []})
    assert proposed == {} and any("zz9" in n for n in notes)


@pytest.mark.parametrize("answer, expect", [
    ({"a4": "new"}, {"a4": "new"}),
    ({"rephrased": {"a4": "new"}}, {"a4": "new"}),
    ({"rephrase": [{"id": "a4", "text": "new"}]}, {"a4": "new"}),
    ([{"id": "a4", "rephrased": "new"}, {"nope": 1}], {"a4": "new"}),
    ({"a4": {"text": "new"}}, {"a4": "new"}),
    ({"a4": 5, "a5": None, "zz": "x"}, {}),
    ("text", {}), (None, {}), (7, {})])
def test_normalise_rephrase_reads_the_shapes_models_use(answer, expect):
    out, notes = normalise_rephrase(answer, {"a4", "a5"})
    assert out == expect
    if not expect:
        assert notes or answer in ("text",)


def test_a_failed_batch_keeps_the_canonical_wording_and_is_not_cached(fb_fixture, spy_llm):
    def boom(ctx):
        raise RuntimeError("provider down")

    cache: dict = {}
    proposed, stats, notes = _ask(fb_fixture, _kws("pytest"), spy_llm({"rephrase_bullets": boom}),
                                  {"experience": {"ex1": ["a4"]}, "projects": []}, cache)
    assert proposed == {} and cache == {} and stats["failed_calls"] == 1
    assert any("failed (provider down); canonical wording kept" in n for n in notes)


def test_a_later_attempt_never_asks_the_same_question_twice(fb_fixture, spy_llm):
    llm = spy_llm({"rephrase_bullets": lambda ctx: {b["id"]: b["text"].rstrip(".") + ", using " + b["introduce"][0] + "."
                                                    for b in ctx["bullets"]}})
    cache: dict = {}
    sel = {"experience": {"ex1": ["a4"]}, "projects": []}
    first, s1, _ = _ask(fb_fixture, _kws("pytest"), llm, sel, cache)
    second, s2, _ = _ask(fb_fixture, _kws("pytest"), llm, sel, cache)
    assert first == second and len(llm.sent) == 1
    assert (s1["asked"], s1["cached"], s2["asked"], s2["cached"]) == (1, 0, 0, 1)


# ---------------------------------------------------------------- the guard
LIES = {"a4": "Wrote a pytest framework with Kubernetes, cutting release time 40%.",           # deny + number
        "a3": "Deployed the agent with Terraform and integration testing on AWS Lambda.",     # deny + unevidenced keyword
        "a1": "Built a tool-calling LLM agent (LangChain, AWS Bedrock) for a multi-omics cancer research platform, "
              "covering agentic workflows, used by the whole research team."}                 # honest (keeps 'team')


def test_every_rewrite_goes_through_the_faithfulness_check(fb_fixture):
    fb = dict(fb_fixture, deny_terms=["kubernetes", "terraform"])
    kws = _kws("pytest", "integration testing", "agentic")
    accepted, viol, _ = tailor.guard_rephrases(fb, LIES, kws)
    assert list(accepted) == ["a1"] and accepted["a1"] == LIES["a1"]
    assert any(v.startswith("a4:") and "kubernetes" in v for v in viol) and any("40%" in v for v in viol)
    assert any(v.startswith("a3:") and "terraform" in v for v in viol)
    assert any(v.startswith("a3:") and "integration testing" in v and "not evidenced" in v for v in viol)   # a3 is not about testing


def test_a_rejected_rewrite_leaves_the_canonical_bullet_on_the_cv(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"rephrase_bullets": lambda ctx: {b["id"]: b["text"].rstrip(".") + ", cutting cost 40%."
                                                    for b in ctx["bullets"]}})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    bidx = tailor.bullet_index(fb_fixture)
    for e in rep["context"]["experience"] + rep["context"]["projects"]:
        assert e["bullets"] == [bidx[i]["text"] for i in e["bullet_ids"]]
    rec = rep["attempts"][0]
    assert rec["rephrase"]["proposed"] > 0 and rec["rephrase"]["accepted"] == 0
    assert rec["rephrase"]["blocked"] == rec["rephrase"]["proposed"]
    assert any("new numbers ['40%']" in v for v in rec["violations"])


def test_an_accepted_rewrite_reaches_the_cv_and_the_coverage_counts_it(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"rephrase_bullets": lambda ctx: {b["id"]: b["text"].rstrip(".") + ", covering "
                                                    + ", ".join(b["introduce"]) + "." for b in ctx["bullets"]}})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    rec = rep["attempts"][0]
    cov, stats = rec["rephrase_coverage"], rec["rephrase"]
    assert stats["accepted"] > 0 and stats["blocked"] == stats["proposed"] - stats["accepted"]
    assert cov["eligible"] > 0 and cov["rephrased"] > 0 and 0 < cov["ratio"] <= 1
    assert rep["rephrase_coverage"] == next(a for a in rep["attempts"]
                                            if a["attempt"] == rep["final"]["chosen_attempt"])["rephrase_coverage"]
    assert rep["rephrase"] == next(a for a in rep["attempts"]
                                   if a["attempt"] == rep["final"]["chosen_attempt"])["rephrase"]
    saved = json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    assert set(saved["rephrase"]) >= {"eligible", "asked", "cached", "calls", "failed_calls", "proposed", "accepted",
                                      "blocked"}


# ---------------------------------------------------------------- coverage
def test_rephrase_coverage_is_rephrased_over_eligible(fb_fixture):
    kws = [Keyword("pytest", 2.0, True), Keyword("docker", 2.0, True)]
    bidx = tailor.bullet_index(fb_fixture)
    ctx = {"experience": [{"bullet_ids": ["a3", "a4"], "bullets": [bidx["a3"]["text"], bidx["a4"]["text"]]}],
           "projects": [{"bullet_ids": ["p1c"], "bullets": [bidx["p1c"]["text"]]}]}
    # a3: terms say docker, text says Docker literally -> not eligible. a4: terms say pytest, text does not -> eligible.
    # p1c: terms say pytest, text says "pytest suite" -> not eligible.
    assert tailor.rephrase_coverage(ctx, fb_fixture, kws) == {"eligible": 1, "rephrased": 0, "ratio": 0.0}
    ctx["experience"][0]["bullets"][1] = "Wrote a three-tier pytest framework across 8 files."
    assert tailor.rephrase_coverage(ctx, fb_fixture, kws) == {"eligible": 1, "rephrased": 1, "ratio": 1.0}


def test_coverage_is_none_when_nothing_is_eligible(fb_fixture):
    ctx = {"experience": [{"bullet_ids": ["a1"], "bullets": [tailor.bullet_index(fb_fixture)["a1"]["text"]]}],
           "projects": []}
    assert tailor.rephrase_coverage(ctx, fb_fixture, [Keyword("zzz", 1.0, False)])["ratio"] is None
