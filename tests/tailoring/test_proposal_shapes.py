"""Regression: `careerops tailor` crashed with "unhashable type: 'dict'" when the model answered tailor_cv with bullet
objects ({"id": "a1", "text": ...}) where bullet ids were expected. Proposals are now read tolerantly, and any-of
keyword groups work through the whole pipeline (scorer, gaps, prompts, report, review, Excel)."""
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from careerops.llm import MockLLM
from careerops.reporting.excel import export
from careerops.tailoring import tailor
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.shapes import clean_proposal, proposal_usable
from careerops.tailoring.tailor import regenerate_letter

CHATTERMILL = (Path(__file__).resolve().parents[1] / "fixtures" / "tailoring" / "jd_chattermill.txt").read_text(
    encoding="utf-8") + "\nProficiency in PyTorch or TensorFlow.\n"


# ---------------------------------------------------------------- clean_proposal
def test_well_formed_proposals_pass_through_unchanged():
    prop = {"profile": "P", "experience": {"ex1": ["a1", "a2"]}, "projects": ["p1", "p2"], "rephrase": {"a1": "new"}}
    assert clean_proposal(prop) == (prop, [])


def test_bullet_objects_are_read_by_their_id():
    prop = {"profile": "P", "projects": [{"id": "p1", "name": "x"}, {"project_id": "p2"}],
            "experience": {"ex1": [{"id": "a1", "text": "t"}, {"bullet_id": "a2"}]}, "rephrase": {}}
    out, notes = clean_proposal(prop)
    assert out["experience"] == {"ex1": ["a1", "a2"]} and out["projects"] == ["p1", "p2"] and notes


def test_lists_of_entries_instead_of_maps():
    prop = {"experience": [{"id": "ex1", "bullets": ["a1", {"id": "a2"}]}, {"id": "ex2", "bullet_ids": "b1"}],
            "rephrase": [{"id": "a1", "text": "new a1"}, {"id": "b1", "rephrased": "new b1"}, {"nope": 1}]}
    out, _ = clean_proposal(prop)
    assert out["experience"] == {"ex1": ["a1", "a2"], "ex2": ["b1"]}
    assert out["rephrase"] == {"a1": "new a1", "b1": "new b1"}


def test_ids_as_object_keys_are_read():
    """The Chattermill run's tailor_cv answer (data/outputs/.../llm_raw/tailor_cv-1.json): the bullet ids were the keys
    of an object, each mapped to a one-element list, and the normaliser dropped every one of them."""
    prop = {"profile": "P", "experience": {"az": {"az1": ["az1"], "az3": ["az3"], "az4": ["az4"]},
                                           "dc180": {"dc1": ["dc1"], "dc2": ["dc2"]}},
            "projects": ["churn", "lloyds"], "rephrase": {}}
    out, notes = clean_proposal(prop)
    assert out["experience"] == {"az": ["az1", "az3", "az4"], "dc180": ["dc1", "dc2"]}
    assert out["projects"] == ["churn", "lloyds"] and any("keys of an object" in n for n in notes)
    assert proposal_usable(out)


def test_id_keyed_objects_work_for_projects_too_and_a_string_value_is_a_rephrase():
    out, _ = clean_proposal({"experience": {"ex1": {"a1": "Built the LangChain agent with tool calling.",
                                                    "a2": "a2", "a3": ""}},
                             "projects": {"p1": [], "p3": None}})
    assert out["experience"] == {"ex1": ["a1", "a2", "a3"]} and out["projects"] == ["p1", "p3"]
    assert out["rephrase"] == {"a1": "Built the LangChain agent with tool calling."}   # still guarded downstream


def test_an_object_describing_one_bullet_is_not_mistaken_for_an_id_keyed_one():
    out, _ = clean_proposal({"experience": {"ex1": {"id": "a1", "text": "t"}}, "projects": [{"id": "p1"}]})
    assert out["experience"] == {"ex1": ["a1"]} and out["rephrase"] == {"a1": "t"}
    out, _ = clean_proposal({"experience": {"ex1": {"id": 5}, "ex2": {"text": 3}}})      # no usable id: nothing invented
    assert out["experience"] == {"ex1": [], "ex2": []}


def test_id_keyed_ids_are_still_validated_against_the_fact_bank(fb_fixture):
    out, notes = clean_proposal({"experience": {"ex1": {"a1": ["a1"], "zzz": ["zzz"]}}}, fb_fixture)
    assert out["experience"] == {"ex1": ["a1"]} and any("unknown bullet id 'zzz'" in n for n in notes)


@pytest.mark.parametrize("prop", [{}, {"experience": None, "projects": None, "rephrase": None, "profile": None},
                                  {"experience": "a1", "projects": 3, "rephrase": 7, "profile": ["x"]},
                                  {"experience": {"ex1": [1, None, ["a1"], {"id": 5}, True]}, "rephrase": {"a1": 5}},
                                  {"experience": [1, "x", None], "rephrase": [None, 3]}, [], "text", None])
def test_garbage_never_raises_and_always_has_the_expected_shape(prop):
    out, _ = clean_proposal(prop)
    assert set(out) == {"profile", "experience", "projects", "rephrase"}
    assert all(isinstance(i, str) for ids in out["experience"].values() for i in ids)
    assert all(isinstance(i, str) for i in out["projects"]) and isinstance(out["profile"], str)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in out["rephrase"].items())


# ---------------------------------------------------------------- the whole pipeline with an any-of group
def _list_shaped(ctx):
    """A rephrase answer in the list-of-objects shape, wrapped in a key, rewording nothing."""
    return {"rephrased": [{"id": b["id"], "text": b["text"]} for b in ctx["bullets"]]}


def _anyof_keywords(ctx):
    return {"required": ["Python", "SQL", "PyTorch or TensorFlow", "causal inference"],
            "preferred": ["LangChain", "parameter-efficient or LoRA or PEFT"],
            "company_hook": "Chattermill turns customer feedback into insight.", "team": ""}


@pytest.fixture
def chattermill_run(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"rephrase_bullets": _list_shaped, "extract_keywords": _anyof_keywords})
    settings.raw["scoring"]["max_keywords"] = 60              # no cap: the cap cuts by weight and frequency, not by arrival
    j = {**job, "company": "Chattermill", "title": "Data Scientist", "description": CHATTERMILL}
    return tailor.tailor_job(j, fb_fixture, llm, settings, settings.path("outputs")), llm, j


def test_chattermill_with_any_of_groups_and_list_shaped_answers_tailors_without_crashing(chattermill_run):
    rep, llm, _ = chattermill_run
    assert any(set(["PyTorch", "TensorFlow"]) <= set(k["alts"] + [k["term"]]) or
               set(["pytorch", "tensorflow"]) <= {w.lower() for w in k["alts"] + [k["term"]]}
               for k in rep["keywords"])                                    # the any-of group survived merging
    assert rep["final"]["pages"] == 1 and rep["letter"]["pages"] == 1
    assert "rephrase_bullets: answer was wrapped in 'rephrased'; read it" in rep["attempts"][0]["warnings"]


def test_groups_and_plain_keywords_are_json_everywhere_and_round_trip(chattermill_run):
    rep, _, _ = chattermill_run
    saved = json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    kws = [Keyword(**k) for k in saved["keywords"]]                    # review / training rebuild keywords this way
    assert [k.members() for k in kws] == [[k["term"], *k["alts"]] for k in saved["keywords"]]
    assert any(k.alts for k in kws) and any(not k.alts for k in kws)
    assert isinstance(saved["genuine_gaps"], list) and all(isinstance(g, str) for g in saved["genuine_gaps"])


def test_prompts_show_group_keywords_as_terms_not_as_objects(chattermill_run):
    _, llm, _ = chattermill_run
    for call in llm.sent:
        assert "Keyword(" not in call["user"] and "{'term'" not in call["user"] and "[{" not in call["user"]


def test_the_letter_can_be_regenerated_and_the_excel_view_exported(chattermill_run, settings, fb_fixture, db, tmp_path):
    rep, llm, job = chattermill_run
    rep = regenerate_letter(job, fb_fixture, llm, settings, rep)       # review's 'l' rebuilds Keyword(**k) with alts
    assert rep["letter"]["pages"] == 1
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", keywords=rep["keywords"], ceiling=rep["ceiling"],
                  score=rep["final"]["score"], score_stemmed=rep["final"]["score_stemmed"],
                  letter_score=rep["letter"]["score"], attempts=len(rep["attempts"]), out_dir=rep["out_dir"])
    out = tmp_path / "x.xlsx"
    assert export(db, out) == 1
    ws = load_workbook(out)["Jobs"]
    assert ws.cell(2, 1).value == "Chattermill" and ws.cell(2, 6).value == "needs_review"


def test_odd_keyword_and_letter_shapes_still_get_a_cv_and_a_letter(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"extract_keywords": lambda ctx: {"required": [{"term": "Python"}, 5, None, ["x"]],
                                                    "preferred": "SQL", "company_hook": 3, "team": ["x"]},
                   "cover_letter": lambda ctx: {"paragraphs": [{"text": "Fine opening."}, None, "Could we talk?"]}})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert rep["final"]["pages"] == 1 and Path(rep["cv_pdf"]).exists() and Path(rep["letter_pdf"]).exists()
    assert rep["analysis_warnings"] and rep["letter"]["notes"]
