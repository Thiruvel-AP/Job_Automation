"""A tailor run that fails part-way must leave an earlier successful output folder untouched."""
from pathlib import Path

import pytest

from careerops.tailoring import tailor
from careerops.tailoring.render import RenderError


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


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


def test_a_failure_in_a_later_render_also_leaves_it_untouched(settings, fb_fixture, job, spy_llm, monkeypatch):
    rep = _run(settings, fb_fixture, job, spy_llm())
    out = Path(rep["out_dir"])
    (out / "report.json").write_text("OLD REPORT", encoding="utf-8")
    real, calls = tailor.render_cv, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise RenderError("xelatex died on the second render")
        return real(*a, **k)

    monkeypatch.setattr(tailor, "render_cv", flaky)             # the first render works, a later one (shrink / final) dies
    with pytest.raises(RenderError, match="second render"):
        _run(settings, fb_fixture, job, spy_llm())
    assert (out / "report.json").read_text(encoding="utf-8") == "OLD REPORT"
    assert sorted(p.name for p in out.parent.iterdir()) == [out.name, out.name + ".failed"]


def test_a_successful_rerun_is_a_new_attempt_beside_the_first_and_reports_its_final_paths(settings, fb_fixture, job, spy_llm):
    rep = _run(settings, fb_fixture, job, spy_llm())
    out = Path(rep["out_dir"])
    (out / "stale.txt").write_text("from the earlier run", encoding="utf-8")
    rep2 = _run(settings, fb_fixture, job, spy_llm())
    new = Path(rep2["out_dir"])
    assert new == out / "attempts" / "1" and (out / "stale.txt").exists() and Path(rep["cv_pdf"]).exists()     # the first output is untouched
    assert Path(rep2["cv_pdf"]).exists() and Path(rep2["letter_pdf"]).exists() and (new / "report.json").exists()
    assert Path(rep2["cv_pdf"]).parent == new and ".partial" not in rep2["cv_pdf"]
