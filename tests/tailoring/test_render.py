"""CV-2: the user's one-page CV and cover-letter templates, the URL filters, and what the PDF text layer shows.
Everything here uses the fixture fact bank (tests/fixtures/tailoring/fact_bank_fixture.yaml)."""
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring.render import compile_tex, env, page_count, pdf_text
from careerops.tailoring.tailor import tailor_job


# ---------------------------------------------------------------- filters
@pytest.mark.parametrize("raw,out", [
    ("https://example.org/a#top", r"https://example.org/a\#top"),
    ("https://example.org/a%20b", r"https://example.org/a\%20b"),
    ("https://example.org/plain", "https://example.org/plain"),
])
def test_urlesc_escapes_percent_and_hash_for_href(raw, out):
    assert env().filters["urlesc"](raw) == out
    assert env().from_string(r"\VAR{u|urlesc}").render(u=raw) == out


@pytest.mark.parametrize("raw,out", [
    ("https://www.linkedin.com/in/ada-lovelace-test/", "linkedin.com/in/ada-lovelace-test"),
    ("http://github.com/ada", "github.com/ada"),
    ("https://ada.example.org/portfolio#top", "ada.example.org/portfolio#top"),
    ("https://wwwexample.org/x", "wwwexample.org/x"),               # only a real "www." prefix goes
])
def test_urlshow_drops_scheme_www_and_trailing_slash(raw, out):
    assert env().filters["urlshow"](raw) == out


# ---------------------------------------------------------------- templates compile
def _cv_ctx(fb, minimal=False):
    if minimal:
        return {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"},
                "profile": "Data scientist with Python.",
                "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst", "bullets": ["Built a report."]}],
                "projects": [{"name": "Thing", "bullets": ["Built a thing."]}],
                "skills": {"Languages": ["Python"]},
                "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": []}
    return {"id": {**fb["identity"], "status_line": ["Available from Nov 2026", "Open to relocation"]},
            "profile": "Data Scientist with LLM agent and forecasting work.",
            "experience": [{**e, "bullets": [b["text"] for b in e["bullets"][:2]]} for e in fb["experience"]],
            "projects": [{**p, "bullets": [b["text"] for b in p["bullets"][:1]]} for p in fb["projects"][:2]],
            "skills": fb["skills"], "education": fb["education"], "certifications": fb["certifications"]}


def _letter_ctx(fb, minimal=False):
    paras = ["First paragraph about the team.", "Second paragraph with an example.", "Could we talk for twenty minutes?"]
    if minimal:
        return {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"},
                "company": "Acme", "paragraphs": paras}
    return {"id": fb["identity"], "company": "Acme", "paragraphs": paras, "date": "03 October 2026",
            "greeting": "Hi Acme AI Accelerator team", "signoff": "With regards"}


@pytest.mark.parametrize("minimal", [False, True], ids=["full", "minimal"])
def test_cv_template_compiles_to_one_page(fb_fixture, tmp_path, minimal):
    tex = env().get_template("cv.tex.j2").render(**_cv_ctx(fb_fixture, minimal))
    pdf = compile_tex(tex, tmp_path, "cv")
    assert page_count(pdf) == 1


@pytest.mark.parametrize("minimal", [False, True], ids=["full", "minimal"])
def test_letter_template_compiles_to_one_page(fb_fixture, tmp_path, minimal):
    tex = env().get_template("cover_letter.tex.j2").render(**_letter_ctx(fb_fixture, minimal))
    pdf = compile_tex(tex, tmp_path, "letter")
    assert page_count(pdf) == 1


def test_full_cv_header_shows_all_three_links_and_the_status_line(fb_fixture, tmp_path):
    full = pdf_text(compile_tex(env().get_template("cv.tex.j2").render(**_cv_ctx(fb_fixture)), tmp_path, "cv"))
    assert "linkedin.com/in/ada-lovelace-test" in full
    assert "github.com/ada-lovelace-test" in full
    assert "ada-lovelace-test.example.org/portfolio#top" in full      # '#' reached the PDF through \href
    assert "Available from Nov 2026" in full


def test_letter_header_shows_address_and_falls_back_to_location(fb_fixture, tmp_path):
    with_addr = pdf_text(compile_tex(env().get_template("cover_letter.tex.j2").render(**_letter_ctx(fb_fixture)),
                                     tmp_path / "a", "letter"))
    assert "12 Analytical Row" in with_addr and "LinkedIn" in with_addr and "+44 7700 900123" in with_addr
    assert "Hi Acme AI Accelerator team," in with_addr and "With regards," in with_addr
    minimal = pdf_text(compile_tex(env().get_template("cover_letter.tex.j2").render(**_letter_ctx(fb_fixture, True)),
                                   tmp_path / "b", "letter"))
    assert "Bristol, UK" in minimal
    assert "Dear Acme hiring team," in minimal and "Kind regards," in minimal


# ---------------------------------------------------------------- the real loop: shrink to one page
def test_scoring_max_pages_is_one(settings):
    assert settings["scoring"]["max_pages"] == 1


def test_fixture_cv_overflows_then_the_shrink_loop_brings_it_to_one_page(one_page_run):
    rep, _ = one_page_run
    assert max(a["shrinks"] for a in rep["attempts"]) >= 1, "fixture is too short to exercise the shrink loop"
    assert rep["final"]["pages"] == 1 and page_count(Path(rep["cv_pdf"])) == 1
    assert rep["letter"]["pages"] == 1 and page_count(Path(rep["letter_pdf"])) == 1


def test_pdf_text_shows_linkedin_straight_apostrophes_and_headings(one_page_run):
    _, text = one_page_run
    assert "linkedin.com/in/" in text
    assert "St. Joseph's College" in text and "Master's Prize" in text
    assert "’" not in text and "‘" not in text                   # no curly quotes for an ATS to trip on
    for heading in ("PROFILE", "EXPERIENCE", "PROJECTS", "SKILLS", "EDUCATION", "CERTIFICATIONS & AWARDS"):
        assert heading in text


def test_one_page_cv_keeps_exactly_two_projects(one_page_run):
    rep, _ = one_page_run
    assert len(rep["context"]["projects"]) == 2
