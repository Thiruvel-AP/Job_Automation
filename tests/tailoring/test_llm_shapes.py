"""The model's JSON is read, not trusted: extract_keywords / rephrase_bullets / tailor_profile / cover_letter answers in
unexpected shapes are normalised (drops reported in report.json), a model that fails or answers garbage never fails the
job (canonical bullets, the two profile facts, the fallback letter), and every raw reply is kept under llm_raw/."""
import json
from pathlib import Path

import pytest

from careerops import cli
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.rawlog import RawRecorder
from careerops.tailoring.shapes import (clean_proposal, normalise_keywords, normalise_letter, proposal_usable)


def _final(rep):
    return Path(rep["out_dir"])


def _raw(rep, name):
    return (_final(rep) / "llm_raw" / name).read_text(encoding="utf-8")


def _run(settings, fb, job, llm):
    return tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))


# ================================================================== raw replies of the new tasks
@pytest.mark.slow
def test_a_every_raw_reply_is_saved_as_the_model_sent_it(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = _run(settings, fb_fixture, job, llm)
    names = sorted(p.name for p in (_final(rep) / "llm_raw").iterdir())
    assert {"extract_keywords-1.json", "tailor_profile-1.json", "cover_letter-1.json"} <= set(names)
    assert not any(n.startswith("tailor_cv") for n in names)                    # the model no longer selects
    assert "rephrase_bullets-1.json" in names
    assert json.loads(_raw(rep, "tailor_profile-1.json"))["facts"] == ["F1", "F2"]
    raw = json.loads(_raw(rep, "cover_letter-1.json"))["paragraphs"]            # exactly what the model sent ...
    assert rep["letter"]["paragraphs"][:-1] == raw[:-1]                          # ... its body is kept as sent
    assert rep["letter"]["paragraphs"][-1].endswith("?") and rep["letter"]["paragraphs"][-1] != raw[-1]   # its closing is ours


# ================================================================== (b) rephrase answers in unexpected shapes
def _odd_shape(ctx):
    """Wrapped in a key, a list of objects, one with an unknown id, one without an id, one reworded with an invented
    number: every one of them is read or dropped, and what is read is still guarded."""
    first, *rest = ctx["bullets"]
    items = [{"id": first["id"], "text": first["text"].rstrip(".") + ", covering " + ", ".join(first["introduce"]) + "."}]
    if rest:
        items.append({"id": rest[0]["id"], "rephrased": rest[0]["text"].rstrip(".") + " and cut review time by 40%."})
    return {"rephrased": items + [{"id": "zz9", "text": "a bullet that does not exist"}, {"note": "no id at all"}]}


@pytest.mark.slow
def test_b_odd_shapes_are_read_and_the_reworded_text_is_still_checked(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm({"rephrase_bullets": _odd_shape}))
    rec = rep["attempts"][0]
    assert rec["rephrase"]["accepted"] >= 1 and rec["rephrase"]["blocked"] >= 1
    assert any("new numbers ['40%']" in v for v in rec["violations"])
    texts = " ".join(b for e in rep["context"]["experience"] + rep["context"]["projects"] for b in e["bullets"])
    assert "40%" not in texts and "covering" in texts


@pytest.mark.slow
def test_b_every_drop_is_a_warning_in_report_json(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm({"rephrase_bullets": _odd_shape}))
    saved = json.loads((_final(rep) / "report.json").read_text(encoding="utf-8"))
    warn = saved["attempts"][0]["warnings"]
    assert "rephrase_bullets: answer was wrapped in 'rephrased'; read it" in warn
    assert any(w.startswith("rephrase_bullets: dropped rewrite for 'zz9'") for w in warn)


def test_b_clean_proposal_moves_object_text_into_rephrase_but_an_explicit_rephrase_wins(fb_fixture):
    prop = {"experience": {"ex1": [{"id": "a1", "rephrase": "from object"}, {"id": "a3", "text": "also object"}]},
            "projects": [], "rephrase": {"a1": "explicit"}}
    out, notes = clean_proposal(prop, fb_fixture)
    assert out["rephrase"] == {"a1": "explicit", "a3": "also object"}
    assert out["experience"] == {"ex1": ["a1", "a3"]}


def test_a_bullet_listed_under_the_wrong_entry_is_dropped(fb_fixture):
    out, notes = clean_proposal({"experience": {"ex1": ["a1", "b1"]}, "projects": ["p1"]}, fb_fixture)
    assert out["experience"] == {"ex1": ["a1"]} and "dropped unknown bullet id 'b1' for experience ex1" in notes


# ================================================================== (c) a model that fails does not fail the job
GARBAGE = {"profile": "x", "experience": "a1", "projects": 3, "rephrase": 7}


@pytest.mark.slow
def test_c_garbage_answers_leave_canonical_bullets_a_fact_profile_and_a_fallback_letter(settings, fb_fixture, job,
                                                                                      spy_llm):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    llm = spy_llm({"rephrase_bullets": lambda ctx: GARBAGE, "tailor_profile": lambda ctx: GARBAGE,
                   "cover_letter": lambda ctx: GARBAGE})
    rep = _run(settings, fb_fixture, job, llm)
    bidx = tailor.bullet_index(fb_fixture)
    for e in rep["context"]["experience"] + rep["context"]["projects"]:
        assert e["bullets"] == [bidx[i]["text"] for i in e["bullet_ids"]]
    assert rep["profile"]["fallback"] and len(rep["profile"]["facts"]) == 2
    assert rep["letter"]["status"] == "fallback" and rep["final"]["pages"] <= settings["scoring"]["max_pages"]


@pytest.mark.slow
def test_c_a_model_that_is_down_for_every_task_still_gets_a_cv_and_a_letter(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    def down(ctx):
        raise RuntimeError("all providers failed")

    llm = spy_llm({t: down for t in ("extract_keywords", "rephrase_bullets", "tailor_profile", "cover_letter")})
    rep = _run(settings, fb_fixture, job, llm)
    assert Path(rep["cv_pdf"]).exists() and Path(rep["letter_pdf"]).exists() and rep["letter"]["status"] == "fallback"
    assert rep["profile"]["fallback"] and any("LLM failed" in w for w in rep["attempts"][0]["warnings"])
    assert any("failed (all providers failed); canonical wording kept" in w for w in rep["attempts"][0]["warnings"])


def _render_dies(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("xelatex died")

    monkeypatch.setattr(tailor, "render_cv", boom)


def test_c_the_raw_answers_of_a_failed_job_are_kept_next_to_it_and_nothing_half_done_is_left(settings, fb_fixture,
                                                                                                 job, spy_llm,
                                                                                                 monkeypatch):
    out_root = settings.path("outputs")
    _render_dies(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        _run(settings, fb_fixture, job, spy_llm())
    failed = next(p for p in out_root.iterdir() if p.name.endswith(".failed"))
    assert sorted(p.name for p in out_root.iterdir()) == [failed.name]          # no job folder, no .partial
    raw = {p.name for p in (failed / "llm_raw").iterdir()}
    assert {"extract_keywords-1.json", "tailor_profile-1.json"} <= raw
    assert str(failed / "llm_raw") in str(e.value)


@pytest.mark.slow
def test_c_a_later_success_clears_the_old_failure_folder(settings, fb_fixture, job, spy_llm, monkeypatch):
    with monkeypatch.context() as m:
        _render_dies(m)
        with pytest.raises(RuntimeError):
            _run(settings, fb_fixture, job, spy_llm())
    assert list(settings.path("outputs").glob("*.failed"))
    _run(settings, fb_fixture, job, spy_llm())
    assert not list(settings.path("outputs").glob("*.failed"))


def test_c_the_cli_marks_the_job_as_an_error_with_the_message(settings, db, job, monkeypatch, capsys):
    db.upsert_job(job)
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM())
    _render_dies(monkeypatch)
    cli.main(["tailor", "--limit", "1"])
    row = db.job(job["id"])
    assert row["status"] == "error" and "xelatex died" in row["reason"]
    assert "ERROR: xelatex died" in capsys.readouterr().out


def test_proposal_usable_needs_at_least_one_valid_id(fb_fixture):
    assert not proposal_usable(clean_proposal(GARBAGE, fb_fixture)[0])
    assert not proposal_usable(clean_proposal({"experience": {"ex1": ["nope"]}, "projects": ["nope"]}, fb_fixture)[0])
    assert proposal_usable(clean_proposal({"projects": ["p1"]}, fb_fixture)[0])
    assert proposal_usable(clean_proposal({"experience": {"ex1": ["a1"]}}, fb_fixture)[0])


# ================================================================== extract_keywords
def test_keywords_as_strings_or_objects_with_term_weight_required():
    req, pref, notes = normalise_keywords({
        "required": ["Python", {"term": "SQL", "weight": 9, "required": True}, {"term": "dbt", "required": False},
                     {"keyword": "Docker"}, 5, None, {"weight": 3}],
        "preferred": [{"name": "LangChain"}, {"term": "Spark", "required": True}, "Airflow"]})
    assert req == ["Python", "SQL", "Docker", "Spark"] and pref == ["dbt", "LangChain", "Airflow"]
    assert any("dropped 5 (no term)" in n for n in notes) and any("dropped {'weight': 3}" in n for n in notes)


def test_a_flat_keywords_list_and_odd_containers():
    assert normalise_keywords({"keywords": [{"term": "Python", "required": True}, {"term": "SQL", "required": False},
                                            "Git"]})[:2] == (["Python", "Git"], ["SQL"])
    assert normalise_keywords({"required": "Python", "preferred": 3})[:2] == (["Python"], [])
    assert normalise_keywords(["x"])[:2] == ([], [])


def test_analyse_uses_object_keywords_ignores_their_weights_and_reports_drops(settings, job):
    jd = "Requirements\nPython and SQL\n\nNice to have\nLangChain\n"
    llm = MockLLM({"extract_keywords": lambda ctx: {
        "required": [{"term": "Python", "weight": 99, "required": True}, {"term": "LangChain", "required": False}, 7],
        "preferred": [{"term": "SQL"}], "company_hook": "Hook.", "team": ""}})
    notes = []
    kws, hook, _ = tailor.analyse({**job, "description": jd}, llm, settings, notes)
    by = {k.term.lower(): k for k in kws}
    assert by["python"].weight == settings["scoring"]["required_weight"] and by["python"].required
    assert hook == "Hook." and any("dropped 7 (no term)" in n for n in notes)
    assert all(n.startswith("extract_keywords: ") for n in notes)


def test_keyword_answers_that_are_not_objects_fall_back_to_the_taxonomy(settings, job):
    kws, hook, team = tailor.analyse(job, MockLLM({"extract_keywords": lambda ctx: ["Python", "SQL"]}), settings)
    assert kws and hook == "" and team == ""


# ================================================================== cover_letter
def test_letter_paragraphs_as_strings_text_objects_or_one_string():
    assert normalise_letter({"paragraphs": ["One.", {"text": "Two."}, {"paragraph": "Three."}, None, 4, {"x": 1}]})[0] \
        == ["One.", "Two.", "Three."]
    assert normalise_letter({"paragraphs": "First.\n\nSecond?"})[0] == ["First.", "Second?"]
    assert normalise_letter({"letter": "A.\n\nB?"})[0] == ["A.", "B?"]
    paras, notes = normalise_letter({"paragraphs": ["ok", None, {"x": 1}]})
    assert paras == ["ok"] and len(notes) == 2
    assert normalise_letter({"paragraphs": 5})[0] == [] and normalise_letter("x")[0] == []


@pytest.mark.slow
def test_a_letter_in_text_objects_is_accepted_and_the_drops_are_reported(settings, fb_fixture, job, spy_llm):
    good = ["Northwind builds oncology tools, which is the work I do.", "At the university I built a tool-calling agent for a cancer research platform, and it is the work that I am proudest of.",
            "Could we talk for twenty minutes?"]
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [{"text": good[0]}, good[1], None, {"text": good[2]}]}})
    rep = _run(settings, fb_fixture, job, llm)
    assert rep["letter"]["paragraphs"] == good[:2] + [
        "Could we find twenty minutes to talk about where I could contribute first at Northwind Health AI?"]
    assert rep["letter"]["status"] == "ok"
    assert rep["letter"]["notes"] == ["cover_letter: paragraphs: dropped None (no text)",
                                      "cover_letter: dropped the model's own closing question; the template closing is used"]


# ================================================================== raw logging
def test_raw_replies_of_the_rephrase_calls_are_named_by_attempt_with_a_suffix_for_a_second_batch(tmp_path):
    class Inner:
        last_raw = '{"a": 1}'

        def json(self, task, system, user, context=None):
            return {}

    rec = RawRecorder(Inner(), tmp_path)
    for attempt, calls in ((1, 2), (2, 1)):
        rec.attempt = attempt
        for _ in range(calls):
            rec.json("rephrase_bullets", "s", "u")
    rec.attempt = None
    rec.json("tailor_profile", "s", "u")                                # before the loop: numbered by call
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rephrase_bullets-1-2.json", "rephrase_bullets-1.json",
                                                         "rephrase_bullets-2.json", "tailor_profile-1.json"]


def test_the_recorder_saves_a_reply_that_was_not_valid_json_even_though_the_call_raised(tmp_path):
    class Broken:
        usage = []
        last_raw = None

        def json(self, task, system, user, context=None):
            self.last_raw = "Sure! Here you go: {not json"
            raise ValueError("model did not return valid JSON")

    rec = RawRecorder(Broken(), tmp_path / "llm_raw")
    with pytest.raises(ValueError):
        rec.json("tailor_cv", "s", "u")
    assert (tmp_path / "llm_raw" / "tailor_cv-1.json").read_text() == "Sure! Here you go: {not json"
    assert rec.usage == []                                             # everything else passes through


def test_the_recorder_never_overwrites_and_skips_calls_with_no_reply(tmp_path):
    class Inner:
        last_raw = '{"a": 1}'

        def json(self, task, system, user, context=None):
            return {}

    inner = Inner()
    rec = RawRecorder(inner, tmp_path)
    rec.attempt = 2
    rec.json("rephrase_bullets", "s", "u")
    rec.json("rephrase_bullets", "s", "u")
    rec.json("rephrase_bullets", "s", "u")
    inner.last_raw = None
    rec.json("rephrase_bullets", "s", "u")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rephrase_bullets-2-2.json", "rephrase_bullets-2-3.json",
                                                         "rephrase_bullets-2.json"]


@pytest.mark.slow
def test_regenerating_a_letter_adds_its_raw_reply_without_touching_the_first(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm())
    first = _raw(rep, "cover_letter-1.json")
    tailor.regenerate_letter(job, fb_fixture, spy_llm(), settings, rep)
    assert _raw(rep, "cover_letter-1.json") == first and (_final(rep) / "llm_raw" / "cover_letter-1-2.json").exists()


def test_llm_raw_is_git_ignored():
    root = Path(__file__).resolve().parents[2]
    ignore = [ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "data/" in ignore                       # outputs (and so outputs/<job>/llm_raw/) live under data/
