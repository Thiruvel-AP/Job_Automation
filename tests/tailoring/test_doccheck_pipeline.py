"""The document check on a real tailoring (xelatex, MockLLM, a fictional bank): a bank written to satisfy the Document rules gets `DOC CHECK: pass`
from the files the pipeline wrote, and a bank that cannot fill a 100-word profile gets the shortfall named. No network."""
import copy
from pathlib import Path

import pytest
from conftest import fixture_profile_settings

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import doccheck, tailor

pytestmark = pytest.mark.slow
FIX = Path(__file__).parent.parent / "fixtures"
REAL_FOR_REPORT = doccheck.for_report                       # the suite stubs doccheck.for_report per test; this module wants the real one

SENTENCES = [
    "Builds machine learning models for small teams, from the first notebook to a service that other people can call and rely on every day.",
    "Writes down the assumptions behind each model so that a reviewer can challenge them before anything is released to users.",
    "Works comfortably with people who are not data specialists, explaining results in plain language and agreeing next steps in the room.",
    "Keeps tests next to the code and runs them on every change, because a model nobody can rerun is a model nobody can trust.",
]


def bank(sentences=SENTENCES):
    fb = load_yaml(FIX / "profile" / "fact_bank.yaml")
    fb["profile"] = {"sentences": list(sentences)}
    spare = {"az": 1, "dc": 2, "fl": 3, "hp": 3, "ra": 3}
    for e in fb["experience"]:
        for i in range(spare.get(e["id"], 0)):
            e["bullets"].append({"id": f"{e['id']}x{i}", "terms": [], "text": f"Prepared analysis {i} of survey returns for {e['id']} and wrote up the caveats."})
    return fb


def job():
    return {"id": "manual:doc", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer", "location": "Bristol, UK", "url": "u",
            "apply_url": "u", "description": (FIX / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")}


def run(tmp_path, fb):
    s = fixture_profile_settings()
    rep = tailor.tailor_job(job(), fb, MockLLM(), s, tmp_path)
    return rep, s


def test_a_bank_written_for_the_rules_passes_the_check_from_its_own_files(tmp_path):
    fb = bank()
    rep, s = run(tmp_path, fb)
    found = REAL_FOR_REPORT(rep, s, fb, job())
    assert found.ok, found.text()
    assert found.text() == "DOC CHECK: pass"
    assert 100 <= rep["profile"]["words"] <= 150 and rep["profile"]["shortfall"] == "" and len(rep["context"]["projects"]) <= 3


def test_a_bank_with_too_few_profile_sentences_gets_the_shortfall_named(tmp_path):
    fb = bank(sentences=[])
    fb["profile_facts"] = fb["profile_facts"][:1]
    rep, s = run(tmp_path, fb)
    found = REAL_FOR_REPORT(rep, s, fb, job())
    assert not found.ok
    assert any(f.startswith("PROFILE ") and "/100 words: add profile sentences to fact_bank.yaml → profile.sentences" in f for f in found.failures)
    assert rep["profile"]["shortfall"] in found.failures
    assert any("profile:" in w and "/100 words" in w for w in rep["analysis_warnings"])


def test_the_files_the_pipeline_wrote_hold_every_rule(tmp_path):
    fb = bank()
    rep, s = run(tmp_path, fb)
    tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    assert tex.count("\item", tex.index("Certifications")) >= len(fb["certifications"])
    assert "textperiodcentered" not in tex[tex.index("Certifications"):]
    assert not doccheck.check(rep, s, tex=tex, fb=fb).failures


def test_a_private_project_is_left_off_the_page_but_stays_in_the_report(tmp_path):
    fb = bank()
    for p in fb["projects"]:
        p["public"] = False
    rep, s = run(tmp_path, fb)
    tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    assert "github.com/ada-lovelace-test/" not in tex.split("\section{Projects}")[1].split("\section{Education}")[0]
    assert not [f for f in doccheck.check(rep, s, tex=tex, fb=fb).failures if f.startswith("PROJECTS")]
