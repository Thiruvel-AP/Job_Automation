"""13 Nov live run: dissertation wording, [r] never lowers the strict ATS, the judge sees identity / certifications / dates / project names,
verbatim-length warnings are style, Playwright shuts down quietly."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from careerops import pathstore
from careerops.apply import browser as browser_mod
from careerops.apply import freetext as ft
from careerops.review import decision_queue as dq
from careerops.review import judge as review_judge

FB = {"experience": [{"id": "az", "org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project", "employment_type": "MSc dissertation (industry-affiliated)",
                      "dates": "May 2026 – Sep 2026", "bullets": []},
                     {"id": "dc", "org": "180 Degrees Consulting Bristol", "title": "Consultant", "employment_type": "volunteer", "dates": "Oct 2025 – Dec 2025", "bullets": []}],
      "projects": [{"id": "p_cf", "name": "CoverFit — insurance benchmarking API (Bristol × UWE AI Hackathon)", "bullets": []}],
      "education": [{"degree": "MSc Data Science", "org": "University of Bristol", "dates": "Sep 2025 – Sep 2026"}],
      "identity": {"name": "A B", "status_line": ["Right to Work in UK", "Available to join Immediately"]},
      "certifications": ["AWS Academy Cloud Foundations, Nov 2025", "Bristol PLUS Award, Mar 2026"], "skills": {}, "families": [], "profile_facts": []}


# ---- 4. dissertation wording
def test_at_astrazeneca_is_a_problem_for_a_dissertation_but_not_the_affiliated_wording():
    bad = ft.dissertation_problems("At AstraZeneca, I built a tool-calling LLM agent.", FB)
    assert bad and "dissertation" in bad[0] and "for my AstraZeneca-affiliated MSc dissertation" in bad[0]
    assert ft.dissertation_problems("For my AstraZeneca-affiliated MSc dissertation, I built a tool-calling LLM agent.", FB) == []
    assert ft.dissertation_problems("At 180 Degrees Consulting, I built a prototype.", FB) == []          # volunteer role: a normal "At"


def test_the_plain_told_lead_for_a_dissertation_names_it():
    assert ft.dissertation_lead(FB["experience"][0]) == "For my AstraZeneca-affiliated MSc dissertation, "
    assert ft.is_dissertation(FB["experience"][0]) and not ft.is_dissertation(FB["experience"][1])


# ---- 5. [r] keeps the better CV: see tests/tailoring/test_attempts.py


CODE = """
import json, sys
from careerops.tailoring.selection import select_cv
from careerops.tailoring.scorer import Keyword
fb = {"experience": [{"id": "e1", "org": "A", "title": "T", "dates": "2024 - 2025", "bullets": [
          {"id": f"b{i}", "text": f"Built service {i} with python and docker on aws for customer churn banking", "terms": ["python"]} for i in range(6)]}],
      "projects": [{"id": "p1", "name": "P", "bullets": [{"id": "pb1", "text": "customer churn model with xgboost", "terms": []}]}],
      "skills": {"Core": ["Python"]}}
kws = [Keyword("python", 2.0, True), Keyword("docker", 1.0, True), Keyword("aws", 1.0, True), Keyword("churn", 1.5, True), Keyword("banking", 1.0, True)]
print(json.dumps(select_cv(fb, kws, jd="bank customer payments fraud behaviour segmentation statistics validation")))
"""


def test_bullet_selection_is_the_same_under_different_hash_seeds():
    outs = set()
    for seed in ("1", "2", "3", "4"):
        r = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, env={**__import__("os").environ, "PYTHONHASHSEED": seed},
                           cwd=Path(__file__).resolve().parents[1])
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout)
    assert len(outs) == 1


# ---- 6. the judge's payload
def test_judge_sees_identity_certifications_dates_and_project_names():
    lines = "\n".join(review_judge.source_lines(FB, {"context": {"experience": [], "projects": []}}, []))
    assert "[identity status_line] Right to Work in UK; Available to join Immediately" in lines
    assert "[certification 1] AWS Academy Cloud Foundations, Nov 2025" in lines and "Bristol PLUS Award" in lines
    assert "May 2026 – Sep 2026" in lines and "Oct 2025 – Dec 2025" in lines and "Sep 2025 – Sep 2026" in lines
    assert "Bristol × UWE AI Hackathon" in lines


def test_verbatim_length_warnings_are_style_not_claim_guard():
    rep = {"final": {"chosen_attempt": 1}, "attempts": [{"attempt": 1, "warnings": [
        "length: az10 prints 5 lines (limit 3); it is a verbatim bullet, so it is never split or reworded",
        "p_x: no-regression: kept the earlier wording, the rewrite would lose llm"]}]}
    got = dq.chosen_warnings(rep)
    assert [w["category"] for w in got] == ["style", "claim-guard"]
    assert got[0]["kind"].startswith("verbatim-length (style only")


# ---- 7. Playwright shuts down quietly
def test_the_never_retrieved_driver_noise_is_filtered_and_other_asyncio_logs_are_not():
    browser_mod.quiet_driver_noise()
    log = logging.getLogger("asyncio")
    mk = lambda msg: log.makeRecord("asyncio", logging.ERROR, "f", 1, msg, (), None)         # noqa: E731
    assert not log.filter(mk("Future exception was never retrieved\nfuture: <Future> Error: Connection closed while reading from the driver"))
    assert log.filter(mk("Task was destroyed but it is pending"))


def test_shutdown_never_raises_and_stops_every_started_playwright(monkeypatch):
    stopped = []

    class PW:
        def stop(self):
            stopped.append(1)
            raise RuntimeError("driver already gone")
    browser_mod._started[:] = [PW(), PW()]
    browser_mod.shutdown()
    assert stopped == [1, 1] and browser_mod._started == []
