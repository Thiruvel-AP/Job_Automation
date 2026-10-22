"""`careerops retriage --ats`: re-score the `skipped (ats)` jobs after the fact bank changes. A dry run by default (the tailoring goes to a scratch
folder, nothing in the database or the inbox changes); `--apply` keeps the new tailoring and brings back every job that now reaches the review
band. Fakes only: tailor_job is replaced, no model, no xelatex."""
import json
from pathlib import Path

import pytest

from careerops import bands, cli
from careerops.apply import decided
from careerops.llm import MockLLM


def fake_rep(out_dir: Path, strict: float, ceiling: float, settings) -> dict:
    """Just the keys `cli` reads from tailor_job's report."""
    v = bands.classify(strict, ceiling, bands.config(settings))
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"keywords": [], "ceiling": ceiling / 100, "attempts": [1], "out_dir": str(out_dir), "cv_pages": 2, "page2_fill": 0.8, "one_page": False,
            "final": {"score": strict / 100, "score_stemmed": strict / 100, "band": "below", "pages": 2},
            "letter": {"status": "off" if not v.letter else "ok", "score": None if not v.letter else 0.5, "pages": 0 if not v.letter else 1},
            "ats": {"strict": strict, "ceiling": ceiling}, "band": v.to_dict(), "stretch": False,
            "llm_usage": {"calls": [], "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "by_task": {}}}


@pytest.fixture
def skipped(settings, db, job, jd):
    """One job that was skipped on the ATS band, with its JD filed in jds_done/skipped/ beside its reason."""
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status=bands.ATS_SKIP, reason="ATS S < 50", score=0.32, out_dir=str(settings.path("outputs") / "old"), ceiling=0.41)
    folder = settings.path("jds_done") / "skipped"
    folder.mkdir(parents=True)
    f = folder / "northwind.txt"
    f.write_text(f"Company: {job['company']}\nTitle: {job['title']}\nURL: {job['url']}\n\n{jd}\n", encoding="utf-8")
    (folder / "northwind.reason.txt").write_text("ATS S < 50\n", encoding="utf-8")
    return job["id"]


@pytest.fixture
def wired(settings, db, monkeypatch):
    seen = {"roots": [], "calls": 0}
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: MockLLM())

    def run_with(strict: float, ceiling: float):
        def fake_tailor(job, fb, llm, s, out_root, frozen=None, **kw):
            seen["calls"] += 1
            seen["roots"].append(Path(out_root))
            return fake_rep(Path(out_root) / "Northwind_AI_Engineer", strict, ceiling, s)
        monkeypatch.setattr("careerops.tailoring.tailor.tailor_job", fake_tailor)
    seen["run_with"] = run_with
    return seen


def test_a_dry_run_reports_the_new_score_and_changes_nothing(skipped, settings, db, wired, capsys):
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats"])
    out = capsys.readouterr().out
    assert "Northwind Health AI | AI Engineer" in out and "ATS 32 -> 55" in out and "would come back to review" in out
    assert "Dry run: nothing was changed. Run again with --apply" in out
    job = db.job(skipped)
    assert job["status"] == bands.ATS_SKIP and job["reason"] == "ATS S < 50" and abs(job["score"] - 0.32) < 1e-9
    assert (settings.path("jds_done") / "skipped" / "northwind.txt").exists() and not list((settings.path("jds") / "inbox").glob("*.txt"))
    assert wired["roots"] and all(r != settings.path("outputs") for r in wired["roots"])        # the scratch folder, never the outputs folder


def test_a_dry_run_leaves_no_scratch_folder_behind(skipped, settings, wired):
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats"])
    assert not wired["roots"][0].exists()


def test_apply_brings_back_a_job_that_now_reaches_50(skipped, settings, db, wired, capsys):
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats", "--apply"])
    out = capsys.readouterr().out
    job = db.job(skipped)
    assert job["status"] == "needs_review" and abs(job["score"] - 0.55) < 1e-9 and job["reason"] is None
    inbox = settings.path("jds") / "inbox"
    assert [p.name for p in inbox.glob("*.txt")] == ["northwind.txt"] and not (settings.path("jds_done") / "skipped" / "northwind.txt").exists()
    assert "came back to review" in out and wired["roots"] == [settings.path("outputs")]


def test_a_job_that_still_scores_under_50_stays_skipped_even_with_apply(skipped, settings, db, wired, capsys):
    wired["run_with"](41, 52)
    cli.main(["retriage", "--ats", "--apply"])
    out = capsys.readouterr().out
    job = db.job(skipped)
    assert job["status"] == bands.ATS_SKIP and "stays skipped" in out
    assert (settings.path("jds_done") / "skipped" / "northwind.txt").exists() and not list((settings.path("jds") / "inbox").glob("*.txt"))


def test_a_job_with_a_high_ceiling_comes_back_flagged(skipped, settings, db, wired):
    wired["run_with"](41, 74)
    cli.main(["retriage", "--ats", "--apply"])
    job = db.job(skipped)
    assert job["status"] == "needs_review"
    assert "CV under-uses the fact bank (strict 41, ceiling 74)" in json.loads(job["flags"])


def test_only_skipped_ats_jobs_are_touched(skipped, settings, db, wired, job, capsys):
    db.upsert_job({**{k: job[k] for k in ("source", "company", "title", "location", "url", "apply_url", "description")}, "id": "manual:other"})
    db.update_job("manual:other", status="needs_review", score=0.6)
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats", "--apply"])
    assert wired["calls"] == 1 and db.job("manual:other")["status"] == "needs_review" and abs(db.job("manual:other")["score"] - 0.6) < 1e-9


def test_nothing_to_do_is_said(settings, db, wired, capsys):
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats"])
    assert "No job is skipped (ats)" in capsys.readouterr().out and wired["calls"] == 0


def test_a_brought_back_job_is_worked_on_by_live_again(skipped, settings, db, wired):
    state = settings.path("live")
    state.mkdir(parents=True, exist_ok=True)
    (state / "state.json").write_text(json.dumps({"version": 1, "jobs": {"abc": {"job_id": skipped, "done": True, "status": "skipped (ats)", "file": "northwind.txt"}}}))
    wired["run_with"](55, 70)
    cli.main(["retriage", "--ats", "--apply"])
    entry = json.loads((state / "state.json").read_text())["jobs"]["abc"]
    assert entry["done"] is False


def test_apply_belongs_to_decided_or_ats(settings, db, wired, capsys):
    with pytest.raises(SystemExit):
        cli.main(["retriage", "--apply"])
    with pytest.raises(SystemExit):
        cli.main(["retriage", "--ats", "--decided"])
