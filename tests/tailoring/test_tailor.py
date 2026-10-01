"""Full tailoring loop with a model that tries to fabricate."""
from pathlib import Path

import pytest

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import tailor_job


def test_tailor_loop_end_to_end(tailored):
    rep, fb, llm = tailored
    assert Path(rep["cv_pdf"]).exists() and Path(rep["letter_pdf"]).exists()
    assert rep["final"]["pages"] <= 2
    terms = [k["term"] for k in rep["keywords"]]
    assert "made-up keyword not in JD" not in terms            # verbatim filter
    viol = " ".join(v for a in rep["attempts"] for v in a["violations"])
    assert "kubernetes" in viol.lower() and "40%" in viol      # guard fired
    # the lie never reached the PDF
    text = pdf_text(Path(rep["cv_pdf"])).lower()
    assert "kubernetes" not in text and "40%" not in text and "5 years" not in text
    assert "agentic" in text                                   # evidenced rephrase (az1 terms) survived
    assert rep["ceiling"] < 1.0 and any(g.lower() == "kubernetes" for g in rep["genuine_gaps"])
    assert llm.calls.count("tailor_cv") == len(rep["attempts"]) <= 3


def test_letter_rules(tailored):
    rep, _, _ = tailored
    text = " ".join(rep["letter"]["paragraphs"])
    assert len(text.split()) <= 250
    assert not text.lower().startswith("i am writing")
    assert "look forward to hearing" not in text.lower()
