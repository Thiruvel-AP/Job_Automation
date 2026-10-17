"""Full tailoring loop with a model that tries to fabricate."""
from pathlib import Path

import pytest

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import tailor_job


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


def test_tailor_loop_end_to_end(settings, job):
    from conftest import _fabricating_llm, with_results

    fb = with_results(load_yaml(settings.path("fact_bank")))
    llm = _fabricating_llm()
    rep = tailor_job(job, fb, llm, settings, settings.path("outputs"))
    assert Path(rep["cv_pdf"]).exists() and Path(rep["letter_pdf"]).exists()
    assert rep["final"]["pages"] <= 2
    terms = [k["term"] for k in rep["keywords"]]
    assert "made-up keyword not in JD" not in terms            # verbatim filter
    viol = " ".join(v for a in rep["attempts"] for v in a["violations"])
    assert "kubernetes" in viol.lower() and "40%" in viol      # guard fired
    # the lie never reached the PDF
    text = pdf_text(Path(rep["cv_pdf"])).lower()
    assert "kubernetes" not in text and "40%" not in text
    assert rep["ceiling"] < 1.0 and any(g.lower() == "kubernetes" for g in rep["genuine_gaps"])
    assert "tailor_cv" not in llm.calls                        # code selects the bullets; the model never does
    assert "tailor_profile" not in llm.calls and "hook" not in llm.calls      # the profile and the hook are assembled in code
    hk = rep["hook"]
    assert rep["context"]["profile"].startswith(hk["profile_line"]) and all(f.rstrip(".") in rep["context"]["profile"] for f in rep["profile"]["facts"])
    assert 1 <= llm.calls.count("rephrase_bullets") <= 5 * len(rep["attempts"])


def test_letter_rules(tailored):
    rep, _, _ = tailored
    text = " ".join(rep["letter"]["paragraphs"])
    assert len(text.split()) <= 250
    assert not text.lower().startswith("i am writing")
    assert "look forward to hearing" not in text.lower()


# ---------------------------------------------------------------- A1: every job gets a cover letter
def _failing_letter_llm():
    def boom(ctx):
        raise RuntimeError("provider quota exhausted")
    return MockLLM({"cover_letter": boom})


def test_good_letter_has_status_ok(tailored):
    rep, _, _ = tailored
    assert rep["letter"]["status"] == "ok" and rep["letter"]["problems"] == []


def test_llm_failure_still_writes_a_letter_marked_fallback(settings, db, job):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    fb = load_yaml(settings.path("fact_bank"))
    rep = tailor_job(job, fb, _failing_letter_llm(), settings, settings.path("outputs"))
    assert Path(rep["letter_pdf"]).exists() and Path(rep["letter_pdf"]).with_suffix(".tex").exists()
    assert rep["letter"]["status"] == "fallback"
    assert any("letter LLM failed" in p for p in rep["letter"]["problems"])
    assert job["company"].lower() in pdf_text(Path(rep["letter_pdf"])).lower()


def test_letter_that_breaks_the_rules_three_times_is_a_fallback(settings, db, job):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    fb = load_yaml(settings.path("fact_bank"))
    rambling = MockLLM({"cover_letter": lambda ctx: {"paragraphs": ["word " * 400]}})
    rep = tailor_job(job, fb, rambling, settings, settings.path("outputs"))
    assert rep["letter"]["status"] == "fallback" and Path(rep["letter_pdf"]).exists()
    assert rambling.calls.count("cover_letter") == 3


def test_regenerate_letter_replaces_a_fallback_in_place(settings, db, job):
    settings.raw["letter"]["on_failure"] = "template"          # this test is about the fallback letter
    from careerops.tailoring.tailor import regenerate_letter
    fb = load_yaml(settings.path("fact_bank"))
    rep = tailor_job(job, fb, _failing_letter_llm(), settings, settings.path("outputs"))
    name = Path(rep["letter_pdf"]).name
    rep = regenerate_letter(job, fb, MockLLM(), settings, rep)
    assert rep["letter"]["status"] == "ok" and rep["letter"]["problems"] == []
    assert Path(rep["letter_pdf"]).name == name and Path(rep["letter_pdf"]).exists()
    import json
    assert json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))["letter"]["status"] == "ok"
