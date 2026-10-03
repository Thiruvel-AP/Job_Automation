"""The model's JSON is read, not trusted: tailor_cv / extract_keywords / cover_letter answers in unexpected shapes are
normalised (drops reported in report.json), an unusable tailor_cv answer is retried once and then fails the job with a
clear error, and every raw reply is kept under llm_raw/ for inspection."""
import json
from pathlib import Path

import pytest

from careerops import cli
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.rawlog import RawRecorder
from careerops.tailoring.shapes import (clean_proposal, normalise_keywords, normalise_letter, proposal_usable)

A3_NEW = ("Packaged and deployed the agent as a Docker container on AWS Lambda with ECR image pipelines, IAM execution "
          "roles and least-privilege policies, replacing a manual copy-and-paste release process.")
A5_BAD = "Surveyed 14 source datasets and cut review time by 40%."          # an invented number: must be rejected


def _final(rep):
    return Path(rep["out_dir"])


def _raw(rep, name):
    return (_final(rep) / "llm_raw" / name).read_text(encoding="utf-8")


def _run(settings, fb, job, llm):
    return tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))


# ================================================================== (a) id strings
def test_a_id_strings_work_as_before_and_every_raw_reply_is_saved(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = _run(settings, fb_fixture, job, llm)
    names = sorted(p.name for p in (_final(rep) / "llm_raw").iterdir())
    assert "extract_keywords-1.json" in names and "cover_letter-1.json" in names
    assert [n for n in names if n.startswith("tailor_cv-")] == [f"tailor_cv-{i}.json"
                                                               for i in range(1, len(rep["attempts"]) + 1)]
    assert not any("proposal" in w for a in rep["attempts"] for w in a["warnings"])
    raw = json.loads(_raw(rep, "tailor_cv-1.json"))                     # exactly what the model sent
    assert raw == rep["attempts"][0]["proposal"] and set(raw) >= {"profile", "experience", "projects"}
    assert json.loads(_raw(rep, "cover_letter-1.json"))["paragraphs"] == rep["letter"]["paragraphs"]


# ================================================================== (b) objects with id + rephrase
def _objects(ctx):
    out = MockLLM().json("tailor_cv", "", "", ctx)
    out["experience"] = {"ex1": [{"id": "a1", "text": ctx["experience"][0]["bullets"][0]["text"]},
                                 {"bullet_id": "a3", "rephrase": A3_NEW},
                                 {"id": "a5", "rephrase": A5_BAD},
                                 {"id": "zz9", "rephrase": "a bullet that does not exist"},
                                 {"note": "no id at all"}],
                         "ex99": [{"id": "a2"}],                                  # an entry that does not exist
                         **{e: [{"id": b} for b in ids] for e, ids in out["experience"].items() if e != "ex1"}}
    out["projects"] = [{"id": p} for p in out["projects"]] + [{"id": "nope"}]
    return out


def test_b_objects_with_id_and_rephrase_are_converted_and_the_rephrase_is_still_checked(settings, fb_fixture, job,
                                                                                           spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm({"tailor_cv": _objects}))
    ctx = rep["context"]
    ex1 = ctx["experience"][0]
    assert ex1["bullet_ids"][:3] == ["a1", "a3", "a5"]
    texts = dict(zip(ex1["bullet_ids"], ex1["bullets"]))
    bidx = tailor.bullet_index(fb_fixture)
    assert texts["a3"] == A3_NEW                                      # the object's rephrase was applied ...
    assert texts["a5"] == bidx["a5"]["text"] and "40%" not in texts["a5"]    # ... unless the guard rejects it
    rec = rep["attempts"][0]
    assert any(v.startswith("a5:") and "new numbers" in v for v in rec["violations"])
    assert all(len(p["bullet_ids"]) >= 1 for p in ctx["projects"]) and len(ctx["projects"]) == 2


def test_b_every_drop_is_a_warning_in_report_json(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm({"tailor_cv": _objects}))
    saved = json.loads((_final(rep) / "report.json").read_text(encoding="utf-8"))
    warn = saved["attempts"][0]["warnings"]
    for expected in ("proposal: experience ex1: entries were objects; read their ids",
                     "proposal: experience ex1: dropped {'note': 'no id at all'} (no id)",
                     "proposal: dropped unknown bullet id 'zz9' for experience ex1",
                     "proposal: dropped unknown experience id 'ex99'",
                     "proposal: dropped unknown project id 'nope'",
                     "proposal: dropped rephrase for unknown bullet id 'zz9'"):
        assert expected in warn, expected


def test_b_clean_proposal_moves_object_text_into_rephrase_but_an_explicit_rephrase_wins(fb_fixture):
    prop = {"experience": {"ex1": [{"id": "a1", "rephrase": "from object"}, {"id": "a3", "text": "also object"}]},
            "projects": [], "rephrase": {"a1": "explicit"}}
    out, notes = clean_proposal(prop, fb_fixture)
    assert out["rephrase"] == {"a1": "explicit", "a3": "also object"}
    assert out["experience"] == {"ex1": ["a1", "a3"]}


def test_a_bullet_listed_under_the_wrong_entry_is_dropped(fb_fixture):
    out, notes = clean_proposal({"experience": {"ex1": ["a1", "b1"]}, "projects": ["p1"]}, fb_fixture)
    assert out["experience"] == {"ex1": ["a1"]} and "dropped unknown bullet id 'b1' for experience ex1" in notes


# ================================================================== (c) garbage -> retry -> clear error
GARBAGE = {"profile": "x", "experience": "a1", "projects": 3, "rephrase": 7}


def test_c_garbage_is_retried_once_with_the_schema_then_the_job_fails_clearly(settings, fb_fixture, job, spy_llm):
    llm = spy_llm({"tailor_cv": lambda ctx: GARBAGE})
    with pytest.raises(tailor.ProposalError) as e:
        _run(settings, fb_fixture, job, llm)
    msg = str(e.value)
    assert "no usable bullet or project ids" in msg and "even after being told the exact schema" in msg
    assert "raw model answers" in msg
    calls = [s for s in llm.sent if s["task"] == "tailor_cv"]
    assert len(calls) == 2                                             # the original call and exactly one retry
    assert "YOUR LAST ANSWER COULD NOT BE USED" not in calls[0]["user"]
    retry = calls[1]["user"]
    assert "YOUR LAST ANSWER COULD NOT BE USED" in retry and "Return EXACTLY this JSON" in retry
    assert "Valid experience ids: ex1" in retry and "Valid project ids:" in retry and retry.startswith(calls[0]["user"])


def test_c_the_raw_answers_of_a_failed_job_are_kept_next_to_it_and_nothing_half_done_is_left(settings, fb_fixture,
                                                                                                 job, spy_llm):
    out_root = settings.path("outputs")
    with pytest.raises(tailor.ProposalError) as e:
        _run(settings, fb_fixture, job, spy_llm({"tailor_cv": lambda ctx: GARBAGE}))
    failed = next(p for p in out_root.iterdir() if p.name.endswith(".failed"))
    assert sorted(p.name for p in out_root.iterdir()) == [failed.name]          # no job folder, no .partial
    raw = {p.name: p.read_text(encoding="utf-8") for p in (failed / "llm_raw").iterdir()}
    assert set(raw) >= {"tailor_cv-1.json", "tailor_cv-1-2.json", "extract_keywords-1.json"}
    assert json.loads(raw["tailor_cv-1.json"]) == GARBAGE
    assert str(failed / "llm_raw") in str(e.value)


def test_c_a_good_answer_after_the_schema_retry_succeeds_and_both_replies_are_kept(settings, fb_fixture, job, spy_llm):
    calls = []

    def flaky(ctx):
        calls.append(1)
        return GARBAGE if len(calls) == 1 else MockLLM().json("tailor_cv", "", "", ctx)

    rep = _run(settings, fb_fixture, job, spy_llm({"tailor_cv": flaky}))
    assert rep["attempts"][0]["prompt"]["schema_retry"] is True
    assert json.loads(_raw(rep, "tailor_cv-1.json")) == GARBAGE
    assert "experience" in json.loads(_raw(rep, "tailor_cv-1-2.json"))
    assert rep["final"]["pages"] == 1 and not list(settings.path("outputs").glob("*.failed"))


def test_c_a_later_success_clears_the_old_failure_folder(settings, fb_fixture, job, spy_llm):
    with pytest.raises(tailor.ProposalError):
        _run(settings, fb_fixture, job, spy_llm({"tailor_cv": lambda ctx: GARBAGE}))
    _run(settings, fb_fixture, job, spy_llm())
    assert not list(settings.path("outputs").glob("*.failed"))


def test_c_the_cli_marks_the_job_as_an_error_with_the_clear_message(settings, db, job, monkeypatch, capsys):
    db.upsert_job(job)
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    llm = MockLLM({"tailor_cv": lambda ctx: GARBAGE})
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: llm)
    cli.main(["tailor", "--limit", "1"])
    row = db.job(job["id"])
    assert row["status"] == "error" and "no usable bullet or project ids" in row["reason"]
    assert "ERROR: tailor_cv returned no usable" in capsys.readouterr().out


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


def test_a_letter_in_text_objects_is_accepted_and_the_drops_are_reported(settings, fb_fixture, job, spy_llm):
    good = ["Northwind builds oncology tools, which is the work I do.", "I built a tool-calling agent.",
            "Could we talk for twenty minutes?"]
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [{"text": good[0]}, good[1], None, {"text": good[2]}]}})
    rep = _run(settings, fb_fixture, job, llm)
    assert rep["letter"]["paragraphs"] == good and rep["letter"]["status"] == "ok"
    assert rep["letter"]["notes"] == ["cover_letter: paragraphs: dropped None (no text)"]


# ================================================================== raw logging
def test_raw_replies_are_saved_per_attempt_with_a_suffix_for_a_second_reply(settings, fb_fixture, job, spy_llm):
    settings.raw["scoring"]["band"] = [0.0, 0.001]                     # never in band: the loop runs every attempt
    rep = _run(settings, fb_fixture, job, spy_llm())
    n = len(rep["attempts"])
    assert n >= 2
    assert sorted(p.name for p in (_final(rep) / "llm_raw").glob("tailor_cv-*")) == sorted(
        f"tailor_cv-{i}.json" for i in range(1, n + 1))


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
    rec.json("tailor_cv", "s", "u")
    rec.json("tailor_cv", "s", "u")
    rec.json("tailor_cv", "s", "u")
    inner.last_raw = None
    rec.json("tailor_cv", "s", "u")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tailor_cv-2-2.json", "tailor_cv-2-3.json", "tailor_cv-2.json"]


def test_regenerating_a_letter_adds_its_raw_reply_without_touching_the_first(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm())
    first = _raw(rep, "cover_letter-1.json")
    tailor.regenerate_letter(job, fb_fixture, spy_llm(), settings, rep)
    assert _raw(rep, "cover_letter-1.json") == first and (_final(rep) / "llm_raw" / "cover_letter-1-2.json").exists()


def test_llm_raw_is_git_ignored():
    root = Path(__file__).resolve().parents[2]
    ignore = [ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "data/" in ignore                       # outputs (and so outputs/<job>/llm_raw/) live under data/
