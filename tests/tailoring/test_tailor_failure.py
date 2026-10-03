"""A tailor run that fails part-way must leave an earlier successful output folder untouched."""
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.render import RenderError


def _files(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _run(settings, fb, job, llm):
    return tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))


def test_failed_rerun_leaves_the_earlier_successful_folder_untouched(settings, fb_fixture, job, spy_llm, monkeypatch):
    rep = _run(settings, fb_fixture, job, spy_llm())
    out = Path(rep["out_dir"])
    cv, report = Path(rep["cv_pdf"]), out / "report.json"
    cv.write_bytes(b"OLD CV")                                  # sentinels: a re-render would replace them
    report.write_text("OLD REPORT", encoding="utf-8")
    before = _files(out)

    def boom(*a, **k):
        raise RenderError("xelatex exploded after the CV was re-rendered")

    monkeypatch.setattr(tailor, "render_letter", boom)         # the CV is already rendered by this point
    with pytest.raises(RenderError):
        _run(settings, fb_fixture, job, spy_llm())

    assert _files(out) == before
    assert sorted(p.name for p in out.parent.iterdir()) == [out.name, out.name + ".failed"]   # no .partial / .old


def test_llm_failure_on_a_later_attempt_also_leaves_it_untouched(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm())
    out = Path(rep["out_dir"])
    (out / "report.json").write_text("OLD REPORT", encoding="utf-8")
    calls = []

    def flaky(ctx):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("provider down")
        return MockLLM().json("tailor_cv", "", "", ctx)

    # attempt 1 renders; a failing attempt 2 (or the first call raising) must not touch the finished folder
    settings.raw["scoring"]["band"] = [0.0, 0.001]              # always above the band, so the loop asks for attempt 2
    with pytest.raises(RuntimeError, match="provider down"):
        _run(settings, fb_fixture, job, spy_llm({"tailor_cv": flaky}))
    assert (out / "report.json").read_text(encoding="utf-8") == "OLD REPORT"
    assert sorted(p.name for p in out.parent.iterdir()) == [out.name, out.name + ".failed"]


def test_a_successful_rerun_replaces_the_folder_and_reports_its_final_paths(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm())
    out = Path(rep["out_dir"])
    (out / "stale.txt").write_text("from the earlier run", encoding="utf-8")
    rep2 = _run(settings, fb_fixture, job, spy_llm())
    assert rep2["out_dir"] == rep["out_dir"] and not (out / "stale.txt").exists()
    assert Path(rep2["cv_pdf"]).exists() and Path(rep2["letter_pdf"]).exists() and (out / "report.json").exists()
    assert Path(rep2["cv_pdf"]).parent == out and ".partial" not in rep2["cv_pdf"]
