"""The letter has the CV's header (Document rules, B2): the same name, the same contact line and the same hook headline, the headline's strings
identical. The templates are rendered to strings; no xelatex, no network, no model."""
import pytest

from careerops.tailoring import render

IDENT = {"name": "Ada Lovelace", "location": "Bristol, UK", "email": "ada@example.org", "phone": "+44 7700 900123",
         "links": {"linkedin": "https://www.linkedin.com/in/ada/", "github": "https://github.com/ada", "portfolio": "https://ada.example.org/p#top"},
         "status_line": ["Right to work in UK"]}
HEADLINE = "LLM agent that recovers from tool failure, ~65-test evals"


def cv_tex(identity=IDENT, headline=HEADLINE, number="65") -> str:
    ctx = {"id": identity, "hook_headline": headline, "hook_number": number, "profile": "A profile.", "skills": {"Core": ["Python"]},
           "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst", "bullets": ["Built it."]}], "projects": [],
           "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": []}
    return render.env().get_template("cv2.tex.j2").render(**ctx)


def letter_tex(identity=IDENT, **extra) -> str:
    ctx = {"id": identity, "company": "Acme", "paragraphs": ["One.", "Two."], "date": "27 October 2026", "greeting": "Dear Acme team", "signoff": "With regards"}
    return render.env().get_template("cover_letter.tex.j2").render(**{**ctx, **extra})


def header_of(tex: str) -> str:
    return tex.split("\\begin{center}", 1)[1].split("\\end{center}", 1)[0]


def test_the_contact_line_is_one_function_and_both_templates_print_it():
    line = render.contact_line(IDENT)
    assert line in cv_tex() and line in letter_tex()
    assert line.startswith("Bristol, UK") and "mailto:ada@example.org" in line and "tel:+447700900123" in line
    assert line.count("textbar") == 5 and "textperiodcentered" not in line                # location | email | phone | linkedin | github | portfolio (A2)
    assert r"\href{https://github.com/ada}{https://github.com/ada}" in line and "Portfolio" not in line          # the full URLs as the link text (A2, B2)


def test_the_contact_line_leaves_out_what_the_bank_does_not_have():
    bare = {"name": "Min", "location": "Leeds", "email": "m@example.org"}
    assert render.contact_line(bare) == "Leeds" + render.DOT + "\\href{mailto:m@example.org}{m@example.org}"


def test_the_headline_strings_are_identical_in_the_cv_and_the_letter():
    shown = render.hook_tex(HEADLINE, "65", "num")
    assert shown in cv_tex()
    assert shown in letter_tex(headline=HEADLINE, headline_number="65")
    assert shown in header_of(letter_tex(headline=HEADLINE, headline_number="65"))


def test_the_name_comes_first_then_the_headline_then_the_contact_line_in_both():
    for tex in (cv_tex(), letter_tex(headline=HEADLINE, headline_number="65")):
        head = header_of(tex)
        assert head.index("Ada Lovelace") < head.index("tool failure") < head.index("Bristol, UK")


def test_a_letter_with_no_headline_still_renders_with_the_name_and_contact_line():
    head = header_of(letter_tex())
    assert "Ada Lovelace" in head and render.contact_line(IDENT) in head and "tool failure" not in head


def test_a_special_character_in_the_headline_is_escaped_in_both():
    h = "Forecasts at 95% recall & 5x speed_up"
    assert render.hook_tex(h, "95%", "num") in cv_tex(headline=h, number="95%")
    assert render.hook_tex(h, "95%", "num") in letter_tex(headline=h, headline_number="95%")
