"""12 Nov 2026 (RightShip dry run): an intra-word hyphen is never removed from the rendered CV or letter.

Before: typography.clean() closed every word-word up at render time ("tool calling", "five person", "end to end", "real time") while the report
text kept the hyphens, so the PDFs and the report disagreed and the ATS keywords spelt with a hyphen could not be matched. The step was
typography.close_hyphens plus the `rewrite` table of config/typography.yaml, called by typography.typeset (the CV) and hook.py (the hook sentences
the letter quotes). Both are out of the render path now; em dashes are still replaced. "~65" prints the same in the CV and the letter.
"""
import re
from pathlib import Path

import pytest

from careerops.tailoring import typography as T

WORDS = ["tool-calling", "five-person", "end-to-end", "real-time", "class-imbalance", "cross-validation", "19,213-gene", "MCP-based", "5-star",
         "production-ready", "e-commerce", "sign-on", "start-up", "multi-agent", "scikit-learn", "CIFAR-10", "t-SNE", "2025-2026"]


@pytest.mark.parametrize("word", WORDS)
def test_clean_keeps_every_hyphenated_word(word):
    assert T.clean(f"Built a {word} pipeline.") == f"Built a {word} pipeline."


def test_the_em_dash_is_still_replaced_but_the_hyphens_around_it_stay():
    assert T.clean("A real-time agent — built end-to-end") == "A real-time agent, built end-to-end"


def test_typeset_keeps_the_hyphens_of_every_piece_of_prose():
    ctx = {"profile": "A cross-validation and class-imbalance specialist.", "skills": {"ML": ["scikit-learn", "multi-agent systems"]},
           "experience": [{"id": "e", "org": "Acme", "title": "Data Scientist", "dates": "Jan 2025 – Sep 2025",
                           "bullets": [f"Used {w} here." for w in WORDS], "bullet_ids": [f"b{i}" for i in range(len(WORDS))]}],
           "projects": [], "id": {"name": "Ada", "email": "a@b.org"}}
    out = T.typeset(ctx)
    assert out["profile"] == ctx["profile"] and out["skills"] == ctx["skills"] and out["experience"][0]["bullets"] == ctx["experience"][0]["bullets"]


def test_the_audit_no_longer_calls_a_hyphen_a_defect_but_still_counts_em_dashes():
    assert T.audit("tool-calling and end-to-end")["hyphens"] == {} and T.audit("a -- b")["em_dashes"] == 1


def _flat(text: str) -> str:
    return re.sub(r"-\s+", "-", " ".join(text.split()))          # a wrapped line may end at a hyphen: "tool-" / "calling"


@pytest.mark.slow
def test_every_hyphenated_word_of_the_source_is_hyphenated_in_the_pdftotext_of_the_cv_and_the_letter(settings, fb_fixture, job, tmp_path):
    from careerops.llm import MockLLM
    from careerops.tailoring import tailor
    from careerops.tailoring.render import pdf_text, render_letter

    words = ["tool-calling", "five-person", "end-to-end", "real-time"]
    sentence = "Built a tool-calling, five-person, end-to-end real-time pipeline"
    fb = {**fb_fixture, "experience": [dict(e) for e in fb_fixture["experience"]]}
    first = dict(fb["experience"][0])
    first["bullets"] = [dict(first["bullets"][0], text=f"{sentence} with ~65 tests."), *first["bullets"][1:]]
    fb["experience"][0] = first
    rep = tailor.tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    cv = _flat(pdf_text(Path(rep["cv_pdf"])))
    assert all(w in cv for w in words), [w for w in words if w not in cv]
    letter_pdf = render_letter({"id": fb["identity"], "company": "Acme", "paragraphs": [f"{sentence} with ~65 tests."], "date": "12 November 2026",
                                "greeting": "Hi Acme team", "signoff": "Regards"}, tmp_path, "letter")
    letter = _flat(pdf_text(letter_pdf))
    assert all(w in letter for w in words), [w for w in words if w not in letter]
    assert "~65" in cv and "~65" in letter and "∼" not in cv + letter          # the same tilde in both documents
