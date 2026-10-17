"""What the review screen (and so `careerops live --dry-run`) prints after a tailoring run (round of 17 Oct, "done when"): the hook id, the
headline, profile sentence 1 and letter sentence 1 as the library wrote them, the three ATS numbers, when the CV PDF was made, and
"letter: off" under the shipped default. No network, no model."""
import copy
from datetime import datetime
from pathlib import Path

import pytest

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.review import workflow as review
from careerops.tailoring.tailor import tailor_job

LIBRARY = load_yaml(Path(__file__).parent.parent / "fixtures" / "tailoring" / "hook_library_fixture.yaml")


def test_the_hook_lines_name_the_hook_id_and_the_three_strings():
    rep = {"hook": {"hook_id": "h_prod_tests", "problem": "testing, MLOps and CI/CD", "proof_bullet_id": "az4", "org": "AstraZeneca",
                    "number": "~65", "source": "library", "headline": "LLM agent shipped behind ~65 tests",
                    "profile_line": "Data scientist who ships.", "letter_sentence": "At AstraZeneca I shipped it.", "flags": []},
           "hook_placed": {"headline": True, "profile": True, "bullet": True}}
    lines = review.hook_lines(rep)
    assert lines[0].startswith("Hook h_prod_tests: testing, MLOps and CI/CD | proof az4 (AstraZeneca) | number ~65 [library]")
    assert lines[1:4] == ["  headline: LLM agent shipped behind ~65 tests", "  profile sentence 1: Data scientist who ships.",
                          "  letter sentence 1: At AstraZeneca I shipped it."]


def test_a_template_hook_says_so():
    rep = {"hook": {"hook_id": "", "problem": "p", "proof_bullet_id": "a1", "org": "Acme", "number": "", "source": "template",
                    "headline": "h", "profile_line": "pl", "letter_sentence": "ls", "flags": []}}
    assert review.hook_lines(rep)[0].startswith("Hook (code template): p | proof a1 (Acme) | number NONE [template]")


def test_the_pdf_time_is_the_files_own(tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    assert review.pdf_created(pdf) == " (created " + datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") + ")"
    assert review.pdf_created(tmp_path / "missing.pdf") == "" and review.pdf_created(None) == ""


@pytest.mark.slow
def test_the_review_screen_after_a_run_with_the_shipped_letter_default(settings, db, fb_fixture, job):
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = copy.deepcopy(LIBRARY)
    settings.raw["letter"]["mode"] = "off"                                             # the shipped default
    db.upsert_job(job)
    rep = tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    db.update_job(job["id"], status="needs_review", keywords=rep["keywords"], ceiling=rep["ceiling"], score=rep["final"]["score"],
                  score_stemmed=rep["final"]["score_stemmed"], attempts=len(rep["attempts"]), out_dir=rep["out_dir"])
    text = review.summary(db.job(job["id"]), rep, settings)
    hk = rep["hook"]
    lib = next(e for e in LIBRARY if e["id"] == hk["hook_id"])
    assert f"Hook {lib['id']}:" in text and hk["source"] == "library"
    assert f"  headline: {lib['headline']}" in text and f"  profile sentence 1: {lib['profile_line']}" in text
    assert f"  letter sentence 1: {lib['letter_line']}" in text
    assert "ATS strict CV" in text and "claimable ceiling" in text                     # the ATS numbers
    assert f"CV: {rep['cv_pdf']} (created " in text
    assert "letter: off" in text.splitlines() and "Letter: off (letter.mode: off" in text
