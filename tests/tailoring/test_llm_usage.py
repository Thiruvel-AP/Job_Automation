"""report.json carries the actual LLM usage per call (M5 7)."""
import json
from pathlib import Path

from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.tailor import regenerate_letter


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
                              "total_tokens": 0}


def test_regenerating_the_letter_adds_its_call_to_the_jobs_usage(settings, fb_fixture, job):
    llm = UsageLLM()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    n = len(rep["llm_usage"]["calls"])
    rep = regenerate_letter(job, fb_fixture, llm, settings, rep)
    assert len(rep["llm_usage"]["calls"]) == n + 1 and rep["llm_usage"]["calls"][-1]["task"] == "cover_letter"
    assert rep["llm_usage"]["total_tokens"] == 70 * (n + 1)
