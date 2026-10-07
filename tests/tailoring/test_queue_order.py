"""Queue ordering key, tier-3 run quota, triage-time ceiling and the family prior in tailoring (M4b)."""
import pytest
import json
from pathlib import Path

from careerops.cli import triage_new
from careerops.config import load_yaml
from careerops.llm import MockLLM, Router
from careerops.tailoring.queue import order, queue_key, select_for_run
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import tailor_job


def _j(jid, tier, ceiling_pre, seen="2026-09-01T00:00:00+00:00", posted=None, ceiling=None):
    return {"id": jid, "tier": tier, "ceiling_pre": ceiling_pre, "ceiling": ceiling,
            "posted_at": posted, "first_seen": seen}


def test_sort_key_floor_then_tier_then_ceiling(settings):
    t3_good = _j("t3_good", 3, 0.85)
    t1_ok = _j("t1_ok", 1, 0.72)
    t1_low = _j("t1_low", 1, 0.60)
    assert [j["id"] for j in order([t1_low, t3_good, t1_ok], settings)] == ["t1_ok", "t3_good", "t1_low"]
    # within a tier: best honest fit first; equal fit → freshest posting (posted_at, else first_seen)
    a = _j("old", 1, 0.80, posted="2026-09-01T00:00:00Z")
    b = _j("new", 1, 0.80, seen="2026-09-20T00:00:00+00:00")
    c = _j("best", 1, 0.90, seen="2026-08-01T00:00:00+00:00")
    assert [j["id"] for j in order([a, b, c], settings)] == ["best", "new", "old"]
    # rows not yet retriaged sort last
    assert queue_key(_j("x", None, None), 0.70) > queue_key(t1_low, 0.70)


def test_post_tailor_order_prefers_llm_ceiling(settings):
    """After tailoring, COALESCE(ceiling, ceiling_pre): taxonomy-only ceiling_pre overestimates domain-heavy JDs."""
    inflated = _j("inflated", 1, 0.87, ceiling=0.65)
    honest = _j("honest", 1, 0.70, ceiling=0.75)
    untailored = _j("pre_only", 1, 0.80)
    assert [j["id"] for j in order([inflated, honest, untailored], settings, tailored=True)] == \
        ["pre_only", "honest", "inflated"]
    # the tailor queue still uses ceiling_pre only
    assert [j["id"] for j in order([honest, inflated], settings)] == ["inflated", "honest"]


def test_quota_caps_tier3(settings):
    jobs = [_j(f"a{i}", 1 + i % 2, 0.80) for i in range(20)] + [_j(f"c{i}", 3, 0.95) for i in range(10)]
    picked = select_for_run(jobs, 25, settings)
    assert len(picked) == 25
    assert sum(j["tier"] in (1, 2) for j in picked) == 20 and sum(j["tier"] == 3 for j in picked) == 5
    assert picked == order(picked, settings)                  # output stays in key order


def test_quota_tier3_fills_when_t12_run_out(settings):
    jobs = [_j(f"a{i}", 1, 0.80) for i in range(8)] + [_j(f"c{i}", 3, 0.80) for i in range(22)]
    picked = select_for_run(jobs, 25, settings)
    assert sum(j["tier"] in (1, 2) for j in picked) == 8 and sum(j["tier"] == 3 for j in picked) == 17


def test_ceiling_pre_at_triage_without_llm(settings, db, job, monkeypatch):
    calls = []
    monkeypatch.setattr(MockLLM, "json", lambda self, task, *a, **k: calls.append(task))
    monkeypatch.setattr(Router, "json", lambda self, task, *a, **k: calls.append(task))
    db.upsert_job(job)
    triage_new(db, settings)
    row = db.job(job["id"])
    assert row["status"] == "triaged"
    assert (row["family"], row["tier"]) == ("ai_engineer", 1)
    assert row["ceiling_pre"] is not None and 0 < row["ceiling_pre"] <= 1
    assert calls == []                                         # MockLLM / Router call count = 0


class RecordingLLM(MockLLM):
    def __init__(self):
        super().__init__()
        self.prompts: dict[str, list[str]] = {}

    def json(self, task, system, user, context=None):
        self.prompts.setdefault(task, []).append(user)
        return super().json(task, system, user, context)


@pytest.mark.slow
def test_headline_and_prior_reach_prompt_and_report(settings, job):
    fb = load_yaml(settings.path("fact_bank"))
    llm = RecordingLLM()
    rep = tailor_job(dict(job, title="Data Scientist", family="data_scientist", tier=1), fb, llm, settings,
                     settings.path("outputs"))
    assert "HEADLINE: Data Scientist" in llm.prompts["tailor_profile"][0]       # the profile starts with the headline
    saved = json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    assert (saved["family"], saved["tier"], saved["headline"]) == ("data_scientist", 1, "Data Scientist")
    assert rep["context"]["id"]["headline"] == "Data Scientist"
    lines = [ln.strip() for ln in pdf_text(Path(rep["cv_pdf"])).splitlines() if ln.strip()]
    # CV-2: the user's one-page layout has no headline line (header = name, contact line, links); the
    # headline reaches the page as the first words of the model's profile ("<headline> with ...").
    assert lines[0] == fb["identity"]["name"]
