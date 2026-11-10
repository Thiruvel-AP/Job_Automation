"""The compact Education of at most 4 printed lines, the first Work Experience bullet's position, the standard headings unchanged. The section
ORDER rule of 8 Nov 2026 (Education first until a date) was replaced on 10 Nov 2026: tests/tailoring/test_sections_v352.py. Fixtures only (the slow
tests render PDFs with xelatex)."""
from datetime import date
from pathlib import Path

import pytest

from careerops.tailoring import render
from careerops.tailoring import sections as S
from careerops.tailoring.layout import Line

EDU = [{"degree": "MSc Data Science", "display_org": "University of Bristol, UK", "org": "University of Bristol", "dates": "Sep 2025 – Sep 2026",
        "note": "Distinction (Expected)", "modules": ["Large-Scale Data Engineering", "AI & Text Analytics"],
        "display_line": "MSc Data Science (Large-Scale Data Engineering); dissertation with AstraZeneca affiliation (GeneTraceAI)",
        "dissertation": "Finding cancer cell lines faster with patient data"},
       {"degree": "B.E. Electrical and Electronics Engineering", "display_line": "B.E. Electrical & Electronics Engineering, First Class with Distinction",
        "org": "St. Joseph's College of Engineering", "dates": "Aug 2019 – April 2023", "note": "First Class with Distinction"}]
SENIOR = "About the role\nWe need a data scientist.\n\nRequirements\n- 3+ years of commercial experience in data science\n- Python\n"
JUNIOR = "About the role\nWe need a data scientist.\n\nRequirements\n- 1+ years of experience in a data role\n- Python\n"


def test_compact_education_is_on_by_default_and_a_setting(settings):
    assert S.compact_on(settings) and S.compact_on({}) and not S.compact_on({"cv": {"education_compact": False}})


# ------------------------------------------------------------------ the compact Education
def _printed(rows):
    return sum(S._lines(f"{r['head']}, {r['tail']}  {r['dates']}") if r["kind"] == "degree" else S._lines(r["text"], S.SMALL_LINE_CHARS)
               for r in rows)


def test_education_is_one_line_per_degree_plus_the_dissertation_line_no_modules():
    rows, notes = S.compact_education(EDU)
    assert [r["kind"] for r in rows] == ["degree", "dissertation", "degree"] and not notes
    assert rows[0] == {"kind": "degree", "head": "MSc Data Science", "school": "University of Bristol, UK", "cls": "", "tail": "University of Bristol, UK",
                       "dates": "Sep 2025 – Sep 2026"}
    assert rows[1]["text"] == "Dissertation with AstraZeneca affiliation: Finding cancer cell lines faster with patient data"
    assert rows[2]["head"] == "B.E. Electrical & Electronics Engineering" and rows[2]["tail"].endswith("First Class with Distinction")
    text = " ".join(str(v) for r in rows for v in r.values())
    assert "Large-Scale" not in text and "Expected" not in text                      # no modules; an expected grade is not a classification
    assert _printed(rows) <= S.EDUCATION_MAX_LINES


def test_the_line_cap_takes_out_old_degrees_never_the_dissertation_line():
    long = [dict(EDU[0], dissertation="A very long dissertation title " * 4), EDU[1]]
    rows, notes = S.compact_education(long)
    assert [r["kind"] for r in rows] == ["degree", "dissertation"] and any("B.E." in n and "left out" in n for n in notes)
    many = [dict(EDU[1], degree=f"B.Sc. Subject number {i} with a long name", org="A college with a long name in a long town") for i in range(5)]
    rows, notes = S.compact_education(many)
    assert _printed(rows) <= S.EDUCATION_MAX_LINES and len(rows) < 5 and notes


def test_only_a_short_degree_class_is_printed_as_the_classification():
    assert S.classification({"note": "First Class with Distinction"}) == "First Class with Distinction"
    assert S.classification({"note": "Distinction (Expected)"}) == ""
    assert S.classification({"note": "Distinction-level dissertation on LLM agents for research data"}) == ""
    assert S.classification({"classification": "2:1", "note": "anything"}) == "2:1"


def test_no_dissertation_field_prints_the_display_line_dissertation_clause_word_for_word():
    rows, notes = S.compact_education([{k: v for k, v in EDU[0].items() if k != "dissertation"}])
    assert [r["kind"] for r in rows] == ["degree", "dissertation"] and not notes
    assert rows[1]["text"] == "Dissertation with AstraZeneca affiliation (GeneTraceAI)"


def test_the_template_prints_the_compact_education_under_the_standard_headings():
    from test_cv_rules import CERTS, OPEN, ctx_with
    rows, _ = S.compact_education(EDU)
    ctx = render.with_public_links({**ctx_with([OPEN], CERTS), "education_compact": rows,
                                    "section_order": [*S.GRADUATE, *S.TAIL]})
    tex = render.env().get_template("cv2.tex.j2").render(**ctx)
    block = tex[tex.index(r"\section{Education}"):tex.index(r"\section{Skills}")]
    assert r"\textit{MSc Data Science}, \textbf{University of Bristol, UK}\unskip" in block and r"\mbox{Sep 2025 – Sep 2026}" in block
    assert r"\textbf{St. Joseph\textquotesingle{}s College of Engineering}, First Class with Distinction\unskip" in block
    assert "Dissertation with AstraZeneca affiliation: Finding cancer cell lines faster with patient data" in block
    assert "Large-Scale" not in block and block.count(r"\par") == 3
    for heading in ("Profile", "Education", "Work Experience", "Projects", "Skills", r"Certifications \& Achievements"):
        assert f"\\section{{{heading}}}" in tex
    order = [tex.index("\\section{" + h) for h in ("Profile", "Education", "Skills", "Work Experience", "Projects", "Certifications")]
    assert order == sorted(order)


# ------------------------------------------------------------------ the first bullet's position
def L(page, x, y, text):
    return Line(page, x, y, 11.0, text.split())


def test_the_first_bullet_position_is_read_under_the_work_experience_heading():
    lines = [L(1, 43, 40, "Ada Lovelace"), L(1, 43, 300, "WORK EXPERIENCE"), L(1, 43, 318, "Northwind Research Lab May 2026 – Sep 2026"),
             L(1, 43, 331, "Data Scientist"), L(1, 50, 345, "• Built an AI assistant ...")]
    assert S.first_bullet_fraction(lines) == pytest.approx(345 / S.A4_HEIGHT)
    assert S.first_bullet_fraction([L(1, 43, 40, "Ada"), L(2, 43, 60, "WORK EXPERIENCE"), L(2, 50, 90, "• x")]) is None


@pytest.mark.slow
def test_a_rendered_cv_puts_the_first_bullet_high_enough_and_the_ats_structure_is_the_same_in_both_orders(tmp_path):
    """Order does not change the v3.3 structure score: the same CV rendered Education first and Experience first."""
    from test_cv_rules import CERTS, OPEN, ctx_with
    from careerops import ats_structure
    from careerops.tailoring.tailor import check_first_bullet

    rows, _ = S.compact_education(EDU)
    base = {**ctx_with([OPEN], CERTS), "education_compact": rows}
    grades = {}
    for name, order in (("edu", S.EDUCATION_FIRST), ("exp", S.EXPERIENCE_FIRST)):
        pdf = render.render_cv({**base, "section_order": [*order, *S.TAIL]}, tmp_path / name, "cv")
        st = ats_structure.check_pdf(pdf)
        grades[name] = (st.score, st.grade)
        if name == "edu":
            at = check_first_bullet(pdf)
            assert at is not None and at <= S.FIRST_BULLET_MAX, at
    assert grades["edu"] == grades["exp"], grades
