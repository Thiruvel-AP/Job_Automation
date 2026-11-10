"""v3.5.2 (10 Nov 2026, the user's rule): a graduate / junior / scheme ad gets Profile, Education, Skills, Work Experience, Projects,
Certifications; every other ad Profile, Skills, Work Experience, Projects, Education, Certifications. The compact Education keeps the MSc line's
dissertation clause from its display_line and never drops it to fit. Fixtures only (the slow test renders PDFs with xelatex)."""
import re
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import render
from careerops.tailoring import sections as S

SENIOR = "About the role\nWe need an AI engineer.\n\nRequirements\n- 3+ years of hands-on AI/ML experience\n- Python\n"
GRADUATE_ORDER = ["profile", "education", "skills", "experience", "projects", "certifications"]
STANDARD_ORDER = ["profile", "skills", "experience", "projects", "education", "certifications"]
MSC = {"degree": "MSc Data Science", "display_org": "University of Bristol, UK", "org": "University of Bristol", "dates": "Sep 2025 – Sep 2026",
       "note": "Distinction (Expected)",
       "display_line": "MSc Data Science (Large-Scale Data Engineering, AI & Text Analytics); dissertation with AstraZeneca affiliation (GeneTraceAI)"}
BE = {"degree": "B.E. Electrical and Electronics Engineering", "display_line": "B.E. Electrical & Electronics Engineering, First Class with Distinction",
      "org": "St. Joseph's College of Engineering", "dates": "Aug 2019 – April 2023", "note": "First Class with Distinction"}


@pytest.mark.parametrize("title, text", [("Graduate Data Scientist", SENIOR), ("Junior AI Engineer", SENIOR), ("Data Science Graduate Scheme 2027", ""),
                                         ("Data Scientist", "Join our graduate scheme this September."),
                                         ("AI Engineer", "This is an entry-level role for recent graduates."),
                                         ("Machine Learning Engineer", "Early careers programme, September 2027 start.")])
def test_a_graduate_junior_or_scheme_ad_gets_education_then_skills_before_experience(title, text):
    order, why = S.order_for({"title": title, "description": text})
    assert order[:6] == GRADUATE_ORDER and "graduate order" in why


@pytest.mark.parametrize("title, text", [("AI Engineer", SENIOR), ("Senior Data Scientist", "You will mentor junior engineers."),
                                         ("Data Scientist", "A graduate degree in a quantitative subject. Generous pension scheme."),
                                         ("AI Engineer", "")])
def test_every_other_ad_gets_skills_first_and_education_after_projects(title, text):
    order, why = S.order_for({"title": title, "description": text})
    assert order[:6] == STANDARD_ORDER and "standard order" in why
    assert order[6:] == list(S.TAIL)


def test_the_order_reads_a_database_row_as_well_as_a_dict():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    row = con.execute("select 'Graduate AI Engineer' as title, '' as description").fetchone()
    assert S.order_for(row)[0][:6] == GRADUATE_ORDER


def test_doctor_states_both_orders():
    name, ok, _ = S.doctor_line()
    assert ok is True and "graduate" in name and "Profile, Skills, Work Experience, Projects, Education, Certifications" in name


def test_the_msc_line_keeps_its_dissertation_clause_from_the_display_line():
    rows, notes = S.compact_education([MSC, BE])
    assert [r["kind"] for r in rows] == ["degree", "dissertation", "degree"] and not notes
    assert rows[1]["text"] == "Dissertation with AstraZeneca affiliation (GeneTraceAI)"
    assert S.dissertation_clause(BE) == ""


def test_the_dissertation_line_is_never_dropped_to_fit_the_line_cap():
    many = [MSC, *(dict(BE, degree=f"B.Sc. Subject number {i} with a long name", org="A college with a long name in a long town") for i in range(5))]
    rows, notes = S.compact_education(many)
    assert rows[0]["head"] == "MSc Data Science" and rows[1]["text"].startswith("Dissertation with AstraZeneca affiliation")
    assert notes and all("dissertation" not in n.split(" was left out")[0] for n in notes if "left out" in n)


def test_the_template_prints_the_dissertation_clause_under_the_msc():
    from test_cv_rules import CERTS, OPEN, ctx_with
    rows, _ = S.compact_education([MSC, BE])
    ctx = render.with_public_links({**ctx_with([OPEN], CERTS), "education_compact": rows, "section_order": [*S.STANDARD, *S.TAIL]})
    tex = render.env().get_template("cv2.tex.j2").render(**ctx)
    block = tex[tex.index(r"\section{Education}"):tex.index(r"\section{Certifications")]
    assert "Dissertation with AstraZeneca affiliation (GeneTraceAI)" in block
    order = [tex.index("\\section{" + h) for h in ("Profile", "Skills", "Work Experience", "Projects", "Education", "Certifications")]
    assert order == sorted(order)


@pytest.mark.slow
def test_tailoring_prints_the_standard_order_for_an_ai_engineer_ad_and_the_graduate_order_for_a_graduate_one(settings, fb_fixture, job):
    """The RightShip CV of 9 Nov 2026 came out with Skills after Education: both orders, end to end, on the rendered .tex."""
    from careerops.tailoring.tailor import tailor_job

    heads = {"Profile": "profile", "Skills": "skills", "Work Experience": "experience", "Projects": "projects", "Education": "education",
             "Certifications": "certifications"}
    for title, want in (("AI Engineer", STANDARD_ORDER), ("Graduate AI Engineer", GRADUATE_ORDER)):
        rep = tailor_job({**job, "title": title}, fb_fixture, MockLLM(), settings, settings.path("outputs") / title.replace(" ", "_"))
        assert rep["context"]["section_order"][:6] == want
        tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
        found = [(tex.index("\\section{" + h), key) for h, key in heads.items() if "\\section{" + h in tex]
        printed = [key for _, key in sorted(found)]
        assert printed == [k for k in want if k in printed], printed
        assert re.search(r"\\section\{Skills\}", tex)
