"""Hooks come from `hook_library`, never from a model (round of 17 Oct, item 1).

The pipeline picks the library entry with the highest sum of keyword weight x hit over its `terms` (a tie goes to the earlier entry)
and uses its `headline`, `profile_line` and `letter_line` word for word, with the entry's `proof` bullet first in its role. There is
no `hook` model call. Evidence for the rule: a model wrote "Forecasting accuracy in production: ~65 tests validate LLM migration",
which ties the tests to forecasting. No network, no model; the fictional bank is tests/fixtures/tailoring/.
"""
import copy
from pathlib import Path

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.layout import with_openers
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

GOLDEN = Path(__file__).parent / "golden" / "anaplan"
LIBRARY = load_yaml(Path(__file__).parent / "fixtures" / "tailoring" / "hook_library_fixture.yaml")
ANAPLAN_JD = (GOLDEN / "jd.txt").read_text(encoding="utf-8")


def golden_keywords() -> list[Keyword]:
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


def anaplan_job() -> dict:
    return {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan",
            "title": "Data Scientist, Functional Analytics (REQ 26490)", "location": "London, UK",
            "url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002", "description": ANAPLAN_JD}


@pytest.fixture
def bank(fb_fixture) -> dict:
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = copy.deepcopy(LIBRARY)
    return fb


def entry(eid, proof, terms, headline="Headline", profile="Profile line.", letter="Letter line."):
    return {"id": eid, "proof": proof, "terms": terms, "headline": headline, "profile_line": profile, "letter_line": letter}


# ---------------------------------------------------------------- the pick
def test_the_entry_with_the_highest_keyword_score_wins(bank):
    kws = [Keyword("forecasting", 3, True), Keyword("demand", 2, True), Keyword("client", 2, True)]
    picked = H.pick_library_entry(bank, kws)
    assert picked["entry"]["id"] == "h_forecast" and picked["score"] == 5.0           # forecasting 3 + demand 2 beats client 2


def test_the_score_is_the_sum_of_weight_times_hit_over_the_entry_terms(bank):
    kws = [Keyword("testing", 2, True), Keyword("mlops", 2, False), Keyword("kubernetes", 3, True)]
    assert H.library_scores(bank, kws) == {"h_prod_tests": 4.0, "h_forecast": 0.0, "h_client": 0.0}


def test_a_tie_goes_to_the_entry_earlier_in_the_library(bank):
    kws = [Keyword("alpha", 2, True), Keyword("beta", 2, True)]
    bank["hook_library"] = [entry("late", "a4", ["alpha", "beta"]), entry("early", "p2a", ["alpha", "beta"])]
    bank["hook_library"].reverse()                                                    # now: early, late
    assert H.pick_library_entry(bank, kws)["entry"]["id"] == "early"
    bank["hook_library"].reverse()
    assert H.pick_library_entry(bank, kws)["entry"]["id"] == "late"


def test_no_entry_that_scores_nothing_is_picked(bank):
    assert H.pick_library_entry(bank, [Keyword("kubernetes", 3, True)]) is None


def test_an_entry_whose_proof_bullet_is_not_in_the_bank_or_names_a_deny_listed_term_is_skipped(bank):
    bank["hook_library"] = [entry("gone", "zz9", ["alpha", "beta"]), entry("denied", "a3", ["alpha", "beta"], headline="Docker and kubernetes at scale"),
                            entry("fine", "a4", ["alpha", "beta"])]
    notes: list[str] = []
    picked = H.pick_library_entry(bank, [Keyword("alpha", 2, True), Keyword("beta", 2, True)], notes)
    assert picked["entry"]["id"] == "fine"
    assert any("gone" in n and "not in the fact bank" in n for n in notes) and any("denied" in n and "deny" in n for n in notes), notes


# ---------------------------------------------------------------- the strings are the library's, word for word
def test_every_hook_string_equals_the_library_text_for_the_anaplan_ad(bank):
    hk = H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist")
    lib = next(e for e in bank["hook_library"] if e["id"] == hk.hook_id)
    assert hk.hook_id == "h_prod_tests" and hk.source == "library"
    assert (hk.headline, hk.profile_line, hk.letter_sentence) == (lib["headline"], lib["profile_line"], lib["letter_line"])
    assert hk.proof_bullet_id == "a4" and hk.number == "~65" and hk.entry_id == "ex1"
    assert " · " not in hk.headline and hk.headline.startswith("LLM agent")                  # no role title is put in front of it


def test_the_hook_is_not_typeset_or_reworded(bank):
    bank["hook_library"][0]["profile_line"] = "Data scientist who ships: a three-tier framework of ~65 tests (all hyphens, quotes: it's)."
    hk = H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist")
    assert hk.profile_line == bank["hook_library"][0]["profile_line"]


def test_there_is_no_hook_model_call(bank):
    class Boom(MockLLM):
        def json(self, *a, **k):
            raise AssertionError("the hook must not call a model")

    assert H.build_hook(anaplan_job(), golden_keywords(), bank, Boom(), role="Data Scientist").source == "library"
    assert not hasattr(H, "_model_strings")
    from careerops.tailoring import prompts as P
    assert not hasattr(P, "HOOK_SYSTEM") and not hasattr(P, "HOOK_USER")


def test_the_problem_phrase_is_a_lower_case_noun_phrase_of_the_entry_terms_the_ad_asked_for(bank):
    hk = H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist")
    words = hk.problem.replace(",", " ").split()
    assert words and all(w.islower() or w in {"AWS", "CI/CD", "MLOps", "Python", "SQL", "PyTorch"} for w in words), hk.problem        # never "Production, Testing"
    assert "," in hk.problem or " and " in hk.problem
    assert {w.lower() for w in words} - {"and"} <= {t.lower() for t in bank["hook_library"][0]["terms"]}


def test_a_bank_without_a_library_or_with_no_matching_entry_gets_no_hook_and_no_template(fb_fixture):
    bank = copy.deepcopy(fb_fixture)
    bank.pop("hook_library")
    assert H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist") is None
    bank["hook_library"] = [entry("x", "a4", ["kubernetes-only-term"])]
    notes: list[str] = []
    assert H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist", notes=notes) is None
    assert any("no hook_library entry hits at least 2" in n for n in notes), notes


def test_the_hook_id_survives_the_report_round_trip(bank):
    hk = H.build_hook(anaplan_job(), golden_keywords(), bank, role="Data Scientist")
    assert H.Hook.from_dict(hk.to_dict()).hook_id == "h_prod_tests"


# ---------------------------------------------------------------- on the CV
@pytest.mark.slow
def test_the_cv_carries_the_library_text_and_the_proof_bullet_first_with_its_situation(settings, bank):
    bank["experience"][0]["situation"] = "Needed an LLM agent a research team could trust before it shipped"
    llm = MockLLM()
    rep = tailor.tailor_job(anaplan_job(), bank, llm, settings, settings.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    lib = next(e for e in bank["hook_library"] if e["id"] == hk.hook_id)
    assert "hook" not in llm.calls and hk.source == "library"
    assert (hk.headline, hk.profile_line, hk.letter_sentence) == (lib["headline"], lib["profile_line"], lib["letter_line"])
    ctx = rep["context"]
    assert ctx["hook_headline"] == lib["headline"] and ctx["profile"].startswith(lib["profile_line"])
    flat = " ".join(pdf_text(Path(rep["cv_pdf"])).split())
    assert lib["headline"] in flat and lib["profile_line"] in flat
    owner = next(e for e in with_openers(ctx)["experience"] if e["id"] == hk.entry_id)
    assert ctx["experience"][0]["bullet_ids"][0] == "a4" and owner["bullets"][0].startswith(bank["experience"][0]["situation"] + ": ")
    assert rep["hook_placed"] == {"headline": True, "profile": True, "bullet": True, "number_in_pdf": True}
