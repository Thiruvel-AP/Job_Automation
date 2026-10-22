"""The ATS bands in the real tailoring pipeline (xelatex, MockLLM): the letter is drafted only AFTER the band check, and only for a job in review or
auto. The band thresholds are set in the test's own settings to force each outcome whatever the fixture CV happens to score."""
import json
from pathlib import Path

import pytest

from careerops import bands, cli
from careerops.llm import MockLLM
from careerops.tailoring.tailor import letter_summary, tailor_job

pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex


def force(settings, auto: float, review: float, rescue: float) -> None:
    settings.raw.setdefault("apply", {})["bands"] = {"auto": auto, "review": review, "rescue_ceiling": rescue}


def letter_calls(llm) -> int:
    return len([s for s in llm.sent if s["task"] == "cover_letter"])


def test_a_job_under_the_band_makes_no_letter_call_and_no_letter_file(settings, fb_fixture, job, spy_llm):
    force(settings, 100, 100, 100)                                          # nothing under 100 reaches review, no ceiling under 100 reaches the rescue line
    llm = spy_llm({"cover_letter": lambda ctx: pytest.fail("a letter was drafted for a skipped job")})
    rep = tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert rep["band"]["outcome"] == "skip" and rep["letter"]["status"] == "off" and rep["letter_pdf"] is None
    assert letter_calls(llm) == 0 and not list(Path(rep["out_dir"]).glob("*Cover Letter*"))
    assert "ATS S < 100" in rep["letter"]["reason"] and Path(rep["cv_pdf"]).is_file()           # the CV itself is still made and scored
    assert rep["ats"]["strict"] < 100 and rep["ats"]["ceiling"] < 100 and rep["band"]["strict"] == rep["ats"]["strict"]
    assert letter_summary(rep["letter"]).startswith("off (ATS S < 100")


@pytest.mark.parametrize("auto, review, rescue, outcome", [(0, 0, 0, "auto"), (100, 0, 0, "review"), (100, 100, 0, "rescue")])
def test_a_job_in_auto_review_or_rescue_gets_its_letter(settings, fb_fixture, job, spy_llm, auto, review, rescue, outcome):
    force(settings, auto, review, rescue)
    llm = spy_llm()
    rep = tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert rep["band"]["outcome"] == outcome and letter_calls(llm) >= 1 and rep["letter"]["status"] == "ok"
    assert Path(rep["letter_pdf"]).is_file()
    assert (outcome == "rescue") == bool(rep["band"]["flag"])


def test_tailor_parks_a_job_under_the_band_as_skipped_ats_and_files_nothing_else(settings, db, job, monkeypatch, capsys, spy_llm):
    force(settings, 100, 100, 100)
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    llm = spy_llm({"cover_letter": lambda ctx: pytest.fail("a letter was drafted for a skipped job")})
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: llm)
    cli.main(["tailor", "--limit", "1"])
    out = capsys.readouterr().out
    row = db.job(job["id"])
    assert row["status"] == bands.ATS_SKIP and row["reason"] == "ATS S < 100" and db.jobs("needs_review") == []
    assert "ATS band: strict" in out and "skipped (under 100, ceiling under 100): no letter, no form" in out


def test_tailor_sends_a_rescued_job_to_review_with_the_red_flag(settings, db, job, monkeypatch, capsys, spy_llm):
    force(settings, 100, 100, 0)
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: spy_llm())
    cli.main(["tailor", "--limit", "1"])
    row = db.job(job["id"])
    flags = json.loads(row["flags"])
    assert row["status"] == "needs_review" and any(f.startswith("CV under-uses the fact bank (strict ") for f in flags)
