"""Keywords frozen per job: the first extraction is stored in the job record and every later tailoring of that job
(`--retailor`, a retry after an error) reuses it, so two runs on the same JD give the same keyword list, ceiling and ATS.
`--re-extract` redoes it. No network, no model: a scripted LLM whose keyword answer changes on every call stands in, so
any second extraction would show."""
import json

import pytest
from conftest import FlipLLM

from careerops import cli
from careerops.tailoring import freeze, tailor
from careerops.tailoring.scorer import Keyword


def _terms(row) -> list[str]:
    return [k["term"] for k in json.loads(row["keywords"])]


# ---------------------------------------------------------------- the stored record
def _kws():
    return [Keyword("Python", 2.0, True), Keyword("PyTorch", 1.0, False, ["TensorFlow"])]


def test_a_record_round_trips_into_the_frozen_analysis_tailor_job_takes(job):
    ctx = [{"term": "Growth", "weight": 1.0, "required": False, "why": "a team name"}]
    rec = freeze.record(_kws(), "a hook", "Payments", ctx, ["note"], job["description"])
    row = {"analysis": json.dumps(rec), "description": job["description"]}
    frozen, why = freeze.frozen_for(row)
    assert why == "" and frozen["keywords"] == _kws()
    assert (frozen["company_hook"], frozen["team"], frozen["notes"], frozen["context_terms"]) == ("a hook", "Payments",
                                                                                                     ["note"], ctx)
    assert frozen["date"] == rec["frozen"]


def test_a_job_without_an_analysis_has_nothing_frozen(job):
    assert freeze.frozen_for({"analysis": None, "description": "x"}) == (None, "")
    assert freeze.frozen_for({"description": "x"}) == (None, "")            # a row from before the column existed


def test_a_changed_jd_or_an_unreadable_record_is_not_reused_and_says_why(job):
    rec = json.dumps(freeze.record(_kws(), "", "", [], [], job["description"]))
    frozen, why = freeze.frozen_for({"analysis": rec, "description": job["description"] + " (edited)"})
    assert frozen is None and why == "the JD changed since its keywords were frozen"
    for bad in ("{not json", json.dumps({"keywords": [{"weight": 1}]}), json.dumps(["list"])):
        frozen, why = freeze.frozen_for({"analysis": bad, "description": "x"})
        assert frozen is None and why.startswith("the stored keywords are unreadable")


def test_analyse_with_a_stored_analysis_calls_no_model_and_returns_the_same_everything(settings, job):
    llm = FlipLLM()
    notes, ctx = [], []
    kws, hook, team = tailor.analyse(job, llm, settings, notes, None, ctx)
    rec = freeze.record(kws, hook, team, ctx, notes, job["description"])
    frozen, _ = freeze.frozen_for({"analysis": json.dumps(rec), "description": job["description"]})
    notes2, ctx2 = [], []
    again = tailor.analyse(job, llm, settings, notes2, frozen, ctx2)
    assert llm.extractions == 1 and again == (kws, hook, team) and ctx2 == ctx
    assert notes2[0].startswith("keywords: frozen (") and "extract_keywords not called" in notes2[0]


def test_the_analysis_column_is_added_to_a_database_made_before_it_existed(tmp_path):
    import sqlite3

    from careerops.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,"
                " first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
    con.commit()
    con.close()
    db = DB(path)
    assert "analysis" in {r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")}


# ---------------------------------------------------------------- tailor --id / live --retailor
@pytest.fixture
def queue(settings, db, job, monkeypatch):
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    return job["id"]


def _tailor(queue, db, llm, monkeypatch, capsys, *extra):
    """What `live --retailor` does to a job and then tailors it: back to triaged, tailor exactly this job."""
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: llm)
    db.update_job(queue, status="triaged")
    cli.main(["tailor", "--id", queue, *extra])
    return capsys.readouterr().out, db.job(queue)


@pytest.mark.slow
def test_two_retailor_runs_on_the_same_jd_give_identical_keyword_lists_and_ats(queue, db, monkeypatch, capsys):
    llm = FlipLLM()
    out1, first = _tailor(queue, db, llm, monkeypatch, capsys)
    out2, second = _tailor(queue, db, llm, monkeypatch, capsys)
    out3, third = _tailor(queue, db, llm, monkeypatch, capsys)
    assert llm.extractions == 1                                            # the model was asked for keywords once
    assert _terms(first) == _terms(second) == _terms(third)
    assert (first["score"], first["score_stemmed"], first["ceiling"]) == (second["score"], second["score_stemmed"],
                                                                         second["ceiling"])
    assert (second["score"], second["ceiling"]) == (third["score"], third["ceiling"])
    assert "reusing the extraction frozen on" not in out1
    assert "reusing the extraction frozen on" in out2 and "--re-extract to redo it" in out3
    assert {"LangChain", "scikit-learn", "GitHub Actions"} <= set(_terms(first))
    assert json.loads(first["analysis"])["keywords"] and json.loads(first["analysis"])["jd_sha1"]


@pytest.mark.slow
def test_re_extract_redoes_the_extraction_and_the_new_one_is_what_is_frozen_afterwards(queue, db, monkeypatch, capsys):
    llm = FlipLLM()
    _, first = _tailor(queue, db, llm, monkeypatch, capsys)
    out, redone = _tailor(queue, db, llm, monkeypatch, capsys, "--re-extract")
    assert llm.extractions == 2 and "keywords: extracting again (--re-extract)" in out
    assert _terms(redone) != _terms(first)
    assert {"lambda", "iam", "tool calling"} <= {t.lower() for t in _terms(redone)}      # the taxonomy may change the casing
    _, later = _tailor(queue, db, llm, monkeypatch, capsys)
    assert llm.extractions == 2 and _terms(later) == _terms(redone)


@pytest.mark.slow
def test_a_changed_jd_is_extracted_again(queue, db, job, monkeypatch, capsys):
    llm = FlipLLM()
    _tailor(queue, db, llm, monkeypatch, capsys)
    db.update_job(queue, description=job["description"] + "\nWe also use LangChain every day.\n")
    out, _ = _tailor(queue, db, llm, monkeypatch, capsys)
    assert llm.extractions == 2 and "keywords: extracting again (the JD changed since its keywords were frozen)" in out


@pytest.mark.slow
def test_a_failed_extraction_is_never_frozen(queue, db, monkeypatch, capsys):
    llm = FlipLLM(fail=True)
    out, row = _tailor(queue, db, llm, monkeypatch, capsys)
    assert "keyword LLM failed, using taxonomy only" in out and row["analysis"] is None
    llm.fail = False
    _tailor(queue, db, llm, monkeypatch, capsys)
    assert llm.extractions == 2 and db.job(queue)["analysis"] is not None      # the next run asked again, and froze it


@pytest.mark.slow
def test_an_extraction_survives_a_tailoring_that_fails_after_it(queue, db, monkeypatch, capsys):
    llm = FlipLLM()

    def boom(*a, **k):
        raise RuntimeError("xelatex exploded")
    with monkeypatch.context() as m:
        m.setattr(tailor, "make_profile", boom)
        out, row = _tailor(queue, db, llm, monkeypatch, capsys)
    assert row["status"] == "error" and "xelatex exploded" in out
    assert row["analysis"] is not None and llm.extractions == 1             # frozen before anything could fail
    _, ok = _tailor(queue, db, llm, monkeypatch, capsys)
    assert ok["status"] == "needs_review" and llm.extractions == 1           # the retry did not extract again
    assert {"LangChain", "scikit-learn", "GitHub Actions"} <= set(_terms(ok))
