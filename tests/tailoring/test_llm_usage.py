"""report.json carries the actual LLM usage per call (M5 7)."""
import pytest
import json
from pathlib import Path

from conftest import with_results
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.tailor import regenerate_letter


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


class UsageLLM(MockLLM):
    """A MockLLM that records usage like the Router does."""

    def json(self, task, system, user, context=None):
        out = super().json(task, system, user, context)
        self.usage.append({"task": task, "provider": "p", "model": "m", "max_tokens": 100, "est_input_tokens": 10,
                           "prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70, "finish_reason": "stop"})
        return out


def test_report_lists_every_call_with_its_usage_and_the_totals(settings, fb_fixture, job):
    llm = UsageLLM()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    u = rep["llm_usage"]
    assert [c["task"] for c in u["calls"]] == llm.calls
    assert u["total_tokens"] == 70 * len(llm.calls) and u["prompt_tokens"] == 50 * len(llm.calls)
    assert json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))["llm_usage"] == u


def test_only_this_jobs_calls_are_counted_when_the_llm_is_shared(settings, fb_fixture, job):
    llm = UsageLLM()
    tailor.tailor_job({**job, "id": "manual:other"}, fb_fixture, llm, settings, settings.path("outputs"))
    before = len(llm.usage)
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert len(rep["llm_usage"]["calls"]) == len(llm.usage) - before > 0


def test_a_model_that_records_no_usage_gives_empty_totals(settings, fb_fixture, job, spy_llm):
    rep = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))
    assert rep["llm_usage"] == {"calls": [], "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                              "total_tokens": 0, "by_task": {}, "by_model": {}}


def test_regenerating_the_letter_adds_its_call_to_the_jobs_usage(settings, fb_fixture, job):
    llm = UsageLLM()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    n = len(rep["llm_usage"]["calls"])
    rep = regenerate_letter(job, fb_fixture, llm, settings, rep)
    assert len(rep["llm_usage"]["calls"]) == n + 1 and rep["llm_usage"]["calls"][-1]["task"] == "cover_letter"
    assert rep["llm_usage"]["total_tokens"] == 70 * (n + 1)


def test_usage_is_summed_per_task_with_each_calls_prompt_tokens_in_order(settings, fb_fixture, job):
    llm = UsageLLM()
    rep = tailor.tailor_job(job, with_results(fb_fixture), llm, settings, settings.path("outputs"))
    by = rep["llm_usage"]["by_task"]
    assert list(by) == list(dict.fromkeys(llm.calls))                            # one entry per task, first-call order
    for task, t in by.items():
        n = llm.calls.count(task)
        assert t["calls"] == n and t["prompt_tokens"] == 50 * n and t["total_tokens"] == 70 * n
        assert t["prompt_tokens_per_call"] == [50] * n
    assert by["rephrase_bullets"]["calls"] >= 1


def test_the_estimate_stands_in_when_the_provider_reports_no_prompt_tokens():
    calls = [{"task": "a", "est_input_tokens": 900, "prompt_tokens": None, "total_tokens": None},
             {"task": "a", "est_input_tokens": 100, "prompt_tokens": 80, "total_tokens": 120}]
    t = tailor.usage_by_task(calls)["a"]
    assert t["prompt_tokens_per_call"] == [900, 80] and t["calls"] == 2 and t["total_tokens"] == 120


def test_regenerating_the_letter_updates_the_per_task_usage(settings, fb_fixture, job):
    llm = UsageLLM()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    n = rep["llm_usage"]["by_task"]["cover_letter"]["calls"]
    rep = regenerate_letter(job, fb_fixture, llm, settings, rep)
    assert rep["llm_usage"]["by_task"]["cover_letter"]["calls"] == n + 1


def test_the_tailor_command_prints_the_prompt_tokens_of_every_call_by_task(settings, db, job, monkeypatch, capsys):
    from careerops import cli
    db.upsert_job(job)
    db.update_job(job["id"], status="triaged", tier=1, ceiling_pre=0.9)
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "_llm", lambda args, s, d: UsageLLM())
    cli.main(["tailor", "--limit", "1"])
    out = capsys.readouterr().out
    assert "    extract_keywords: 1 call(s), prompt tokens 50, total 70" in out
    assert "tailor_profile" not in out and "hook:" not in out                     # the profile and the hook are assembled in code
    assert "    cover_letter: 1 call(s), prompt tokens 50, total 70" in out
