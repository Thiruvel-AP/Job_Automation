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
from careerops.tailoring.tailor import clean_proposal, regenerate_letter

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


def test_assemble_gives_the_same_cv_for_object_shaped_and_id_shaped_proposals(fb_fixture):
    ids = {"profile": "", "experience": {"ex1": ["a1", "a3"], "ex2": ["b1"]}, "projects": ["p1", "p3"], "rephrase": {}}
    canon = tailor.bullet_index(fb_fixture)["a1"]["text"]            # an object that just echoes the bullet text
    objs = {"profile": "", "experience": {"ex1": [{"id": "a1", "text": canon}, {"id": "a3"}], "ex2": [{"id": "b1"}]},
            "projects": [{"id": "p1"}, {"id": "p3"}], "rephrase": {}}
    a, _, wa = tailor._assemble(fb_fixture, ids, [], "Data Scientist")
    b, _, wb = tailor._assemble(fb_fixture, objs, [], "Data Scientist")
    assert a["experience"] == b["experience"] and a["projects"] == b["projects"]
    assert not any("proposal" in w for w in wa) and any("proposal: experience ex1" in w for w in wb)


# ---------------------------------------------------------------- the whole pipeline with an any-of group
def _object_shaped(ctx):
    out = MockLLM().json("tailor_cv", "", "", ctx)
    out["experience"] = {e: [{"id": i, "text": "ignored"} for i in ids] for e, ids in out["experience"].items()}
    out["projects"] = [{"id": p, "name": "ignored"} for p in out["projects"]]
    out["rephrase"] = [{"id": i, "text": t} for i, t in out["rephrase"].items()]
    return out


def _anyof_keywords(ctx):
    return {"required": ["Python", "SQL", "PyTorch or TensorFlow", "causal inference"],
            "preferred": ["LangChain", "parameter-efficient or LoRA or PEFT"],
            "company_hook": "Chattermill turns customer feedback into insight.", "team": ""}


@pytest.fixture
def chattermill_run(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"tailor_cv": _object_shaped, "extract_keywords": _anyof_keywords})
    j = {**job, "company": "Chattermill", "title": "Data Scientist", "description": CHATTERMILL}
    return tailor.tailor_job(j, fb_fixture, llm, settings, settings.path("outputs")), llm, j


def test_chattermill_with_any_of_groups_and_object_shaped_answers_tailors_without_crashing(chattermill_run):
    rep, llm, _ = chattermill_run
    assert any(set(["PyTorch", "TensorFlow"]) <= set(k["alts"] + [k["term"]]) or
               set(["pytorch", "tensorflow"]) <= {w.lower() for w in k["alts"] + [k["term"]]}
               for k in rep["keywords"])                                    # the any-of group survived merging
    assert rep["final"]["pages"] == 1 and rep["letter"]["pages"] == 1
    assert any("proposal: experience" in w for w in rep["attempts"][0]["warnings"])        # the shape was noted


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
