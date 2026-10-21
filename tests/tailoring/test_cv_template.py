"""The CV template round: cv2.tex.j2 renders with the preamble and macros of the user's hand-made style reference
(config/reference/cv_style_reference.tex, v2: role, roleNoLoc, project, gh, badge, skillgroup, num, gap) and the layout of its body
(employer bold left and dates right, title on the next line; projects with a right-aligned GitHub link or badge and an italic stack
line; skill groups with the navy label on its own line above the items; one certification bullet joined with a middle dot, years in
italics), "Mon YYYY \u2013 Mon YYYY" dates, a bullet's result text in \num, a freelance entry as "Freelance \u00b7 Location", and the
fill order up to a page 2 that is filled to within two lines of the bottom margin (96%).

Snapshot tests render the fictional fixture profile (tests/fixtures/profile/fact_bank.yaml) with xelatex and read the PDF back
with pdftotext / pdffonts. A missing tool makes them fail, not skip: a silent skip would hide a broken pipeline. No network,
no model (MockLLM), never the user's real profile or reference files."""
import html
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from conftest import fixture_profile_settings

from careerops.config import ROOT, load_settings, load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import tailor, typography
from careerops.tailoring.render import (badge_shown, bullet_latex, cert_tex, dates_display, degree_line, education_lines, env, language_line,
                                         org_line, page2_fill, page_count, pdf_text, project_parts, publication_line, role_line)
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import fill_order, select_cv, selected_ids, TWO_PAGE

FIX = Path(__file__).parent.parent / "fixtures"
REFERENCE = ROOT / "config" / "reference" / "cv_style_reference.tex"     # the user's file, git-ignored: compared when it is there
SEPARATORS = ("|", "\u00b7", ",", ";")

# The reference's preamble and macros, from \documentclass to \begin{document}, copied here so the test needs no git-ignored file.
# cv2.tex.j2 renders exactly these lines plus one: \usepackage{needspace} (no heading or entry alone at the foot of a page).
REFERENCE_PREAMBLE = r"""
\documentclass[a4paper,11pt]{article}

\usepackage{fontspec}
\setmainfont{Carlito}
\usepackage[margin=0.6in, top=0.5in, bottom=0.5in]{geometry}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage[colorlinks=true]{hyperref}
\usepackage{microtype}
\usepackage{ragged2e}

% ---- palette: one accent, one ink, one muted grey ----
\definecolor{navy}{RGB}{12, 83, 166}
\definecolor{ink}{RGB}{33, 37, 41}
\definecolor{muted}{RGB}{96, 102, 110}
\hypersetup{urlcolor=navy, linkcolor=navy}
\color{ink}

% ---- ragged right, no hyphenation: even word spacing, no split words ----
\hyphenpenalty=10000 \exhyphenpenalty=10000 \emergencystretch=3em
\setlength{\parindent}{0pt}
\pagestyle{empty}
\linespread{1.03}
\AtBeginDocument{\RaggedRight}

% ---- type scale (ratio ~1.2): body 11 -> headline 12 -> section 13 -> name 22 ----
\titleformat{\section}
  {\color{navy}\fontsize{13}{15}\selectfont\bfseries\addfontfeature{LetterSpace=6}}
  {}{0em}{\MakeUppercase}[{\color{navy}\vspace{1pt}\titlerule[0.8pt]}]
\titlespacing*{\section}{0pt}{9pt}{5pt}

\setlist[itemize]{label={\color{navy}\small\textbullet}, leftmargin=1.1em, labelsep=0.45em,
  itemsep=1.5pt, parsep=0pt, topsep=2pt, partopsep=0pt}

% ---- building blocks: bold = organisation/project, italic = role/stack, grey = dates/places ----
\newcommand{\role}[4]{% org, location, dates, title
  \textbf{#1}{\color{muted}\,\textperiodcentered\,\textit{#2}}\hfill{\color{muted}#3}\\
  \textit{#4}\par}
\newcommand{\roleNoLoc}[3]{\textbf{#1}\hfill{\color{muted}#2}\\ \textit{#3}\par}
\newcommand{\project}[3]{% name, stack, right-hand tag
  \textbf{#1}\hfill#3\\ {\color{muted}\small\textit{#2}}\par}
\newcommand{\gh}[1]{\href{#1}{\small GitHub}}
\newcommand{\badge}[1]{{\color{navy}\small\textbf{#1}}}
\newcommand{\skillgroup}[2]{{\color{navy}\textbf{#1}}\\ #2\par\vspace{3pt}}
\newcommand{\gap}{\vspace{5pt}}
\newcommand{\num}[1]{\textbf{#1}}   % result numbers only
"""


def _lines(tex: str) -> list[str]:
    return [ln.rstrip() for ln in tex.splitlines() if ln.strip()]


def _preamble(tex: str) -> list[str]:
    return _lines(tex.split(r"\begin{document}")[0])


def _bank() -> dict:
    return load_yaml(FIX / "profile" / "fact_bank.yaml")


def _job() -> dict:
    return {"id": "manual:tpl", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "u", "apply_url": "u",
            "description": (FIX / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")}


def _tailor(tmp_path, fb: dict, **scoring) -> dict:
    s = fixture_profile_settings()
    s.raw["scoring"].update(scoring)
    return tailor.tailor_job(_job(), fb, MockLLM(), s, tmp_path)


def _tex(rep: dict) -> str:
    return next(Path(rep["cv_pdf"]).parent.glob("*CV.tex")).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    fb = _bank()
    fb["education"][0]["dates"] = "Sep 2025 \u2013 Sep 2026"           # the fixture has a year-only date: tested apart below
    rep = _tailor(tmp_path_factory.mktemp("tpl"), fb)
    return rep, fb


# ---------------------------------------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("raw,shown", [
    ("May 2026 - Sep 2026", "May 2026 \u2013 Sep 2026"), ("Jan 2025 -- Aug 2025", "Jan 2025 \u2013 Aug 2025"),
    ("Jun 2022 \u2014 Sep 2022", "Jun 2022 \u2013 Sep 2022"), ("September 2025 to Present", "Sep 2025 \u2013 Present"),
    ("Sept 2025 \u2013 now", "Sep 2025 \u2013 Present"), ("Jul 2023 \u2013 Sep 2024", "Jul 2023 \u2013 Sep 2024"),
    ("2025 \u2013 2026", "2025 \u2013 2026")])                          # a missing month is never invented
def test_dates_are_mon_yyyy_en_dash_mon_yyyy(raw, shown):
    assert dates_display(raw) == shown


def test_a_freelance_entry_is_freelance_with_its_location_and_never_self_employed():
    e = {"org": "Chennai Studio Ltd", "employment_type": "freelance", "location": "Chennai, India",
         "title": "Freelance AI/ML Engineer"}
    assert org_line(e) == "Freelance, Chennai, India" and role_line(e) == "AI/ML Engineer"
    assert org_line({**e, "employment_type": "Freelance"}) == "Freelance, Chennai, India"
    assert org_line({"org": "Self-employed, Chennai", "title": "ML Engineer"}) == "Freelance, Chennai"    # a bank without the field
    assert org_line({**e, "location": ""}) == "Freelance"
    assert org_line({"org": "Acme", "location": "Bristol, UK", "employment_type": "full-time"}) == "Acme, Bristol, UK"
    assert org_line({"org": "Acme, Bristol", "location": "Bristol"}) == "Acme, Bristol"                  # not said twice
    assert role_line({"org": "Acme", "title": "Freelance Analyst"}) == "Freelance Analyst"                 # only under "Freelance"


def test_only_the_results_text_is_bold_in_a_bullet():
    s = bullet_latex("Merged 14 heterogeneous datasets into 1,399 cell lines and ~2.8M edges, cutting RAM from 8.2 to 5.5 GB.",
                     "1,399 cell lines and ~2.8M edges")
    assert r"\textbf{1,399 cell lines and \textasciitilde{}2.8M edges}" in s and s.count(r"\textbf") == 1
    for plain in ("14 heterogeneous", "8.2 to 5.5 GB"):                         # numbers that are not the result stay plain
        assert plain in s
    assert bullet_latex("Cut 50% of 100 & more_x") == r"Cut 50\% of 100 \& more\_x"      # no result: no bold, still escaped


def test_education_line_adds_modules_grade_note_and_details():
    assert degree_line({"degree": "MSc Data Science", "modules": ["A", "B"], "details": "dissertation"}) == \
        "MSc Data Science (A, B); dissertation"
    assert degree_line({"degree": "B.E. EEE", "note": "First Class"}) == "B.E. EEE, First Class"
    assert publication_line({"title": "A study.", "venue": "ICEES", "date": "Mar 2023"}) == "A study. ICEES, Mar 2023"
    assert publication_line("Plain string") == "Plain string"
    assert language_line({"name": "Tamil", "level": "native"}) == "Tamil (native)" and language_line("English") == "English"


MINIMAL = dict(id={"name": "A B", "location": "X", "email": "a@b.c"}, profile="p", skills={"Core": ["Python"]},
               experience=[{"org": "O", "dates": "Jan 2025 - Feb 2025", "title": "t", "bullets": ["Did a thing."]}], projects=[],
               education=[], certifications=[])


def test_the_preamble_and_macros_are_the_reference_ones_plus_needspace():
    tex = env().get_template("cv2.tex.j2").render(**MINIMAL)
    want = _lines(REFERENCE_PREAMBLE)
    got = _preamble(tex)
    assert r"\usepackage{needspace}" in got
    assert [ln for ln in got if ln != r"\usepackage{needspace}"] == want          # every line, in order; nothing else added
    if REFERENCE.exists():                                                         # and the pinned copy is still the user's file
        ref = REFERENCE.read_text(encoding="utf-8")
        assert _lines(r"\documentclass" + ref.split(r"\documentclass", 1)[1].split(r"\begin{document}")[0]) == want, \
            "config/reference/cv_style_reference.tex changed: update REFERENCE_PREAMBLE and templates/cv2.tex.j2 together"
    # the type scale: 22pt name, 13pt headings, 12pt hook headline under the name (only when the job has a hook), 10pt (\small)
    # contact / stack / modules lines, 11pt body; nothing else is sized
    sizes = lambda t: set(re.findall(r"\\fontsize\{([\d.]+)\}", t))
    assert sizes(tex) == {"22", "13"} and "\\fontsize{10" not in tex and "10.5pt" not in tex           # no hook: no 12pt line
    hooked = env().get_template("cv2.tex.j2").render(**{**MINIMAL, "hook_headline": "AI Engineer for clinical data"})
    assert sizes(hooked) == {"22", "13", "12"} and "\\fontsize{10" not in hooked and "10.5pt" not in hooked
    assert r"\linespread{1.06}" in env().get_template("cv2.tex.j2").render(**{**MINIMAL, "linespread": 1.06})


def test_the_body_uses_the_reference_macros():
    ctx = dict(id={"name": "Ada Lovelace", "location": "Bristol, UK", "email": "a@b.c", "phone": "+44 7700 900123",
                   "links": {"github": "https://github.com/ada"}, "status_line": ["Right to work in the UK", "Available now"]},
               profile="p", skills={"Core": ["Python"], "Stats": ["R"]}, results={"x1": "3 seconds"},
               experience=[{"org": "Acme", "location": "Bristol, UK", "dates": "Jan 2025 - Feb 2025", "title": "Analyst",
                            "bullets": ["Replied in 3 seconds."], "bullet_ids": ["x1"]},
                           {"org": "Solo", "dates": "2024", "title": "Dev", "bullets": ["Did it."]}],
               projects=[{"name": "Thing", "stack": "Python", "github": "https://github.com/a/b#c", "bullets": ["Built it."]},
                         {"name": "Prize", "stack": "R", "award": "3rd place", "bullets": ["Built it."]},
                         {"name": "Course work (MSc coursework)", "stack": "Torch", "bullets": ["Built it."]},
                         {"name": "No stack", "bullets": ["Built it."]}],
               education=[{"org": "Uni", "location": "Bristol, UK", "dates": "Sep 2025 - Sep 2026", "degree": "MSc Data Science",
                           "grade": "Distinction", "modules": ["Deep Learning", "NLP"], "details": "dissertation"}],
               certifications=["Course, Anthropic, Jun 2026", "AWS Cloud Foundations, Nov 2025"])
    tex = env().get_template("cv2.tex.j2").render(**ctx)
    body = tex.split(r"\begin{document}")[1]
    assert "\\role{Acme}{Bristol, UK}{Jan 2025 \u2013 Feb 2025}{Analyst}" in body
    assert r"\roleNoLoc{Solo}{2024}{Dev}" in body                                   # no location: the macro without one
    plain = "\n".join(ln for ln in body.splitlines() if r"\url{" not in ln)           # the project-link layout (18 Oct) sets its own bold
    assert r"\item Replied in \num{3 seconds}." in body and r"\textbf" not in plain.replace(r"\textbf{No stack}", "")
    assert r"\project{Thing}{Python}{\url{https://github.com/a/b\#c}}" in body and "{GitHub}" not in body    # the URL itself (18 Oct), not \gh
    assert r"\project{Prize}{R}{\badge{3rd place}}" in body
    assert r"\project{Course work}{Torch}{\badge{MSc Coursework}}" in body           # the coursework label is the badge
    assert r"\textbf{No stack}\hfill \par" in body                                  # no stack: no empty stack line
    assert r"\skillgroup{Core}{Python}" in body and r"\skillgroup{Stats}{R}" in body
    assert "\\roleNoLoc{Uni, Bristol, UK}{Sep 2025 \u2013 Sep 2026}{MSc Data Science, Distinction}" in body
    assert r"{\color{muted}\small Deep Learning, NLP; dissertation}\par" in body
    assert r"Course, Anthropic, \textit{Jun 2026}" in body and r"AWS Cloud Foundations, \textit{Nov 2025}" in body
    assert body.count(r"\item", body.index("Certifications")) == 1
    assert r"\fontsize{22}{26}\selectfont\bfseries\color{navy} Ada Lovelace}" in body
    assert r"\textperiodcentered" in body and "|" not in body                       # middle-dot separators, as in the reference
    assert r"\color{muted} Right to work in the UK" in body and "Available now" in body
    sections = re.findall(r"\\section\{([^}]*)\}", body)
    assert sections == ["Profile", "Skills", "Work Experience", "Projects", "Education", "Certifications \\& Achievements"]


def test_a_project_label_is_its_award_or_its_coursework_tag():
    assert project_parts({"name": "Lloyd's Datathon", "award": "3rd place"}) == ("Lloyd's Datathon", "3rd place")
    assert project_parts({"name": "Lloyd's Datathon, 3rd place", "award": "3rd place"}) == ("Lloyd's Datathon, 3rd place", "")
    assert project_parts({"name": "Pipeline on AWS (MSc coursework)"}) == ("Pipeline on AWS", "MSc Coursework")
    assert project_parts({"name": "Image Classification (coursework)"}) == ("Image Classification", "Coursework")
    # a link is shown instead of a label: the name keeps its tag; an award wins over the coursework tag
    assert project_parts({"name": "X (MSc coursework)", "github": "https://github.com/a/b"}) == ("X (MSc coursework)", "")
    assert project_parts({"name": "X (MSc coursework)", "award": "Winner"}) == ("X (MSc coursework)", "Winner")
    assert project_parts({"name": "Plain"}) == ("Plain", "")


def test_a_project_badge_is_printed_in_the_right_hand_tag_the_way_an_award_is():
    fb = load_yaml(FIX / "profile" / "fact_bank.yaml")                                 # the fictional bank; edited in memory only
    proj = {p["id"]: p for p in fb["projects"]}
    proj["bitcoin"]["badge"] = "Open source"                                           # has a github link: the badge wins over the link
    proj["lloyds"]["badge"] = "Competition"                                            # has an award too: the award wins
    proj["churn"]["badge"] = "Churn (Competition)"                                      # its text is already in the name: printed once
    proj["churn"]["name"] = "Churn (Competition) model"
    shown = {p["id"]: project_parts(p) for p in fb["projects"]}
    assert shown["bitcoin"] == ("Price forecasting", "Open source")
    assert shown["lloyds"] == ("Banking datathon", "3rd place, national datathon")
    assert shown["churn"] == ("Churn (Competition) model", "")
    assert badge_shown({"name": "X", "badge": "  Client   work "}) == "Client work"          # spaces closed up; blank / missing = none
    assert badge_shown({"name": "X", "badge": " "}) == "" and badge_shown({"name": "X"}) == ""
    assert project_parts({"name": "X (MSc coursework)", "badge": "Client work"}) == ("X (MSc coursework)", "Client work")   # beats the coursework tag
    ctx = {**MINIMAL, "projects": [{"name": p["name"], "stack": p["stack"], "github": p.get("github"), "award": p.get("award"),
                                    "badge": p.get("badge"), "bullets": ["Built it."]} for p in (proj["bitcoin"], proj["lloyds"])]}
    body = env().get_template("cv2.tex.j2").render(**ctx).split(r"\begin{document}")[1]
    assert r"\project{Price forecasting}{Python, PyTorch, LSTM}{\badge{Open source}}" in body and r"\gh{" not in body
    assert r"\badge{3rd place, national datathon}" in body and "Competition" not in body
    one_page = env().get_template("cv.tex.j2").render(**ctx).split(r"\begin{document}")[1]       # the --one-page template too
    assert r"\textit{Open source}" in one_page and r"\textit{3rd place, national datathon}" in one_page
    assert "Competition" not in one_page


def test_education_lines_are_the_degree_with_its_grade_and_the_muted_modules_line():
    assert education_lines({"degree": "B.E. EEE", "grade": "First Class", "note": "Distinction (Expected)"}) == \
        ("B.E. EEE, First Class, Distinction (Expected)", "")
    assert education_lines({"degree": "MSc", "modules": ["A", "B"], "details": "dissertation"}) == ("MSc", "A, B; dissertation")
    assert education_lines({"degree": "MSc", "details": "dissertation"}) == ("MSc", "dissertation")


def test_certification_years_are_italic_and_joined_with_a_middle_dot():
    assert cert_tex("Course \u2014 Anthropic, Jun 2026") == r"Course, Anthropic, \textit{Jun 2026}"
    assert cert_tex("AWS Academy Cloud Foundations, Nov 2025") == r"AWS Academy Cloud Foundations, \textit{Nov 2025}"
    assert cert_tex("Bristol PLUS Award 2026") == r"Bristol PLUS Award, \textit{2026}"
    assert cert_tex({"name": "Cloud Practitioner", "issuer": "AWS", "date": "May 2025"}) == \
        r"Cloud Practitioner, AWS, \textit{May 2025}"
    assert cert_tex("ISO 27001") == "ISO 27001"                                          # not a year
    assert cert_tex("Plain") == "Plain"


def test_settings_for_the_fill():
    sc = load_settings()["scoring"]
    assert (sc["max_pages"], sc["min_fill_page2"], sc["cv_projects"], sc["cv_projects_max"]) == (2, 0.96, 5, 6)   # 0.96 = within 2 lines
    assert (sc["cv_linespread"], sc["cv_linespread_max"]) == (1.03, 1.09)               # the reference's, then three steps of 0.02


# ----------------------------------------------------------------------------------------------- fill order (no xelatex)
def _kw(*terms):
    return [Keyword(t, 1.0, True) for t in terms]


def _fill_bank(n_projects=8):
    def bullets(prefix, n):
        return [{"id": f"{prefix}{i}", "text": f"Did {prefix}{i} thing{i} with tool{prefix}{i}.", "terms": []} for i in range(n)]
    return {"skills": {}, "experience": [{"id": f"e{j}", "bullets": bullets(f"e{j}b", 7)} for j in range(2)],
            "projects": [{"id": f"p{j}", "bullets": bullets(f"p{j}b", 3)} for j in range(n_projects)]}


def test_fill_adds_further_projects_first_then_project_bullets_then_experience_bullets():
    fb = _fill_bank()
    kws = _kw(*[f"toolp{j}b0" for j in range(8)])
    sel = select_cv(fb, kws, layout=TWO_PAGE)
    assert len(sel["projects"]) == 5
    order = fill_order(fb, kws, sel, max_projects=6)
    owner = {b["id"]: (sec, e["id"]) for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    stages = [("new" if owner[b][0] == "projects" and owner[b][1] not in sel["projects"]
               else "shown" if owner[b][0] == "projects" else "experience") for b in order]
    assert stages == sorted(stages, key=["new", "shown", "experience"].index)           # never back to an earlier stage
    new = {owner[b][1] for b, st in zip(order, stages) if st == "new"}
    assert len(new) == 1 and not new & set(sel["projects"])                              # 5 shown + 1 new = the maximum of 6
    assert stages.count("shown") > 0 and stages.count("experience") > 0


def test_a_project_is_added_by_jd_relevance_and_none_without_a_maximum():
    fb = _fill_bank()
    kws = _kw("toolp7b0", "toolp7b1")                                                    # only the last project matters
    sel = select_cv(fb, kws, layout=TWO_PAGE)
    assert "p7" in sel["projects"]
    sel["projects"].remove("p7")                                                         # as if it had not been picked
    sel["project_bullets"].pop("p7", None)
    assert fill_order(fb, kws, sel, max_projects=5 + 1)[0].startswith("p7b")             # the best of the remaining first
    none = fill_order(fb, kws, sel)                                                      # no maximum: no further project
    assert not any(b.startswith("p7") for b in none)


# --------------------------------------------------------------------------------------------- snapshots (xelatex)
@pytest.mark.slow
def test_snapshot_two_pages_and_page_two_at_least_90_percent_full(fixture_run):
    rep, _ = fixture_run
    pdf = Path(rep["cv_pdf"])
    assert page_count(pdf) == rep["cv_pages"] == 2
    assert page2_fill(pdf) >= 0.96 and rep["page2_fill"] == pytest.approx(page2_fill(pdf)) and rep["fill"]["reached"] is True
    assert len(rep["context"]["projects"]) in (5, 6)


@pytest.mark.slow
def test_snapshot_the_only_font_is_carlito(fixture_run):
    rep, _ = fixture_run
    out = subprocess.run(["pdffonts", str(rep["cv_pdf"])], capture_output=True, text=True, check=True).stdout
    names = [ln.split()[0] for ln in out.splitlines()[2:] if ln.strip()]
    assert names and all(re.sub(r"^[A-Z]{6}\+", "", n).startswith(("Carlito", URL_FONT)) for n in names), names     # \url's own font: the URLs


URL_FONT = "LMMono10"                                                               # the default \url font: used for the project URLs and nothing else
SECTIONS = ["PROFILE", "SKILLS", "WORK EXPERIENCE", "PROJECTS", "EDUCATION", "CERTIFICATIONS & ACHIEVEMENTS"]
SIZES = (22, 13, 12, 11, 10)      # the reference's type scale: name, headings, the hook headline under the name (hook.py), body, contact / stack / modules lines
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</word>')


def _bbox_words(pdf) -> list[tuple[int, float, float, str]]:
    """(page, y, height, text) of every word of the PDF in reading order, from pdftotext -bbox. The box is the font's ascent to
    descent, so within one family its height is proportional to the font size."""
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    out = []
    for page, body in enumerate(xml.split("</page>")[:-1], 1):
        out += [(page, float(m.group(2)), float(m.group(3)) - float(m.group(2)), html.unescape(m.group(4))) for m in _WORD.finditer(body)]
    return out


@pytest.mark.slow
def test_snapshot_section_order_fonts_and_sizes_from_the_bounding_boxes(fixture_run):
    rep, _ = fixture_run
    pdf = Path(rep["cv_pdf"])
    words = _bbox_words(pdf)
    body = Counter(round(h, 1) for _, _, h, _ in words).most_common(1)[0][0]          # the bullets: by far the most words
    size = lambda h: 11 * h / body                                                    # noqa: E731  (the body is 11pt)
    # sizes: nothing at any size but 22 / 13 / 12 / 11 / 10, and all five are used
    got = {min(SIZES, key=lambda s: abs(s - size(h))) for _, _, h, _ in words}
    assert got == set(SIZES)
    assert all(abs(size(h) - min(SIZES, key=lambda s: abs(s - size(h)))) < 0.35 for _, _, h, _ in words), \
        sorted({round(size(h), 1) for _, _, h, _ in words})
    by_size = lambda s: [(p, y, t) for p, y, h, t in words if abs(size(h) - s) < 0.35]  # noqa: E731
    assert [t for _, _, t in by_size(22)] == ["Ada", "Lovelace"]                      # the name, and only the name
    headline = typography.typeset({"hook_headline": rep["hook"]["headline"]})["hook_headline"]
    assert " ".join(t for _, _, t in by_size(12)) == " ".join(headline.split())       # the 12pt line under the name is the hook headline
    assert by_size(12)[0][1] > by_size(22)[-1][1] and by_size(12)[-1][1] < by_size(10)[0][1]        # between the name and the contact line
    assert len({p for p, _, _ in by_size(12)}) == 1 and len({round(y) for _, y, _ in by_size(12)}) == 1      # one line
    # section order: the 13pt words, line by line, are the six headings in the reference's order
    heads, last = [], None
    for p, y, t in by_size(13):
        if last and last[0] == p and abs(last[1] - y) < 3:
            heads[-1] += " " + t
        else:
            heads.append(t)
        last = (p, y)
    assert heads == SECTIONS
    assert [t for _, _, t in by_size(10)][:2] == ["Bristol,", "UK"]                    # the contact line is the first 10pt text
    # fonts: Carlito, regular / bold / italic, and nothing else
    fonts = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True, check=True).stdout
    names = [re.sub(r"^[A-Z]{6}\+", "", ln.split()[0]) for ln in fonts.splitlines()[2:] if ln.strip()]
    assert names and all(n.startswith(("Carlito", URL_FONT)) for n in names), names         # \url's default font (18 Oct) prints the project URLs
    assert {"Carlito-Regular", "Carlito-Bold", "Carlito-Italic"} <= set(names)


@pytest.mark.slow
def test_snapshot_no_line_starts_with_a_separator(fixture_run):
    rep, _ = fixture_run
    text = pdf_text(Path(rep["cv_pdf"]))
    starts = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(SEPARATORS)]
    assert starts == [] and "·" in text                                              # the header does use them between its items


@pytest.mark.slow
def test_snapshot_layout_follows_the_reference(fixture_run):
    rep, _ = fixture_run
    text = pdf_text(Path(rep["cv_pdf"]))
    tex = _tex(rep)
    heads = [h for h in ("PROFILE", "SKILLS", "WORK EXPERIENCE", "PROJECTS", "EDUCATION", "CERTIFICATIONS & ACHIEVEMENTS") if h in text]
    assert heads == ["PROFILE", "SKILLS", "WORK EXPERIENCE", "PROJECTS", "EDUCATION", "CERTIFICATIONS & ACHIEVEMENTS"]
    # employer bold left, its location after a middle dot, the dates right-aligned on the same line, the title on the next line
    lines = text.splitlines()
    i = next(n for n, ln in enumerate(lines)
             if re.match(r"^Northwind Research Lab, University of Northfield \u00b7 Bristol, UK\s+May 2026 \u2013 Sep 2026$", ln.strip()))
    assert lines[i + 1].strip() == "Data Scientist (MSc dissertation)"
    assert "\\role{Northwind Research Lab, University of Northfield}{Bristol, UK}{May 2026 \u2013 Sep 2026}{Data Scientist (MSc dissertation)}" in tex
    # a project: bold name with its GitHub URL at the right (\url, 18 Oct), an italic stack line, then bullets
    assert r"\project{Customer churn prediction}{Python, scikit-learn, XGBoost}{\url{https://github.com/ada-lovelace-test/churn\#readme}}" in tex
    assert "{GitHub}" not in tex
    assert re.search(r"^Customer churn prediction\s+https://github\.com/ada-lovelace-test/churn#readme$", text, re.M)
    assert re.search(r"^Python, scikit-learn, XGBoost$", text, re.M)
    # a skill group: the navy label on a line of its own, the items on the lines below it
    assert r"\skillgroup{Machine Learning, Deep Learning \& Statistics}{" in tex and r"\skillgroup{Python, Data Quality \& Cloud}{" in tex
    assert re.search(r"^Machine Learning, Deep Learning & Statistics$", text, re.M)
    # the name in navy; short header links
    assert r"{\fontsize{22}{26}\selectfont\bfseries\color{navy} Ada Lovelace}" in tex and "linkedin.com/in/ada-lovelace-test" in text
    urls = re.findall(r"https://\S+", text)                                         # only the project URLs: the header links are short
    assert "https://github.com/ada-lovelace-test/churn#readme" in urls
    assert all(u.startswith("https://github.com/ada-lovelace-test/") for u in urls)
    # certifications: one bullet joined with a middle dot, the years in italics
    cert = text.split("CERTIFICATIONS & ACHIEVEMENTS")[1]
    assert cert.count("\u2022") == 2 and " \u00b7 " in cert            # the award project's line, then the one certifications bullet
    assert tex.count(r"\item", tex.index("Certifications")) == 2
    assert len(re.findall(r"\\textit\{(?:[A-Z][a-z]{2} )?\d{4}\}", tex[tex.index("Certifications"):])) == 2
    # every date range reads "Mon YYYY – Mon YYYY"
    ranges = re.findall(r"(?:[A-Za-z]+ )?\d{4} – (?:[A-Za-z]+ \d{4}|Present)", text)
    assert len(ranges) >= 6 and all(re.fullmatch(r"[A-Z][a-z]{2} \d{4} – (?:[A-Z][a-z]{2} \d{4}|Present)", r) for r in ranges)
    assert not re.search(r"\d{4} - [A-Z]", text)


@pytest.mark.slow
def test_snapshot_freelance_entry(fixture_run):
    rep, fb = fixture_run
    text, tex = pdf_text(Path(rep["cv_pdf"])), _tex(rep)
    assert re.search(r"^Freelance \u00b7 Chennai, India\s+Jan 2025 \u2013 Aug 2025$", text, re.M)
    assert "Self-employed" not in text and "Self-employed" not in tex
    assert "\\role{Freelance}{Chennai, India}{Jan 2025 \u2013 Aug 2025}{ML Engineer}" in tex       # Freelance bold, its location italic


@pytest.mark.slow
def test_an_explicit_employment_type_freelance_renders_freelance(tmp_path):
    fb = _bank()
    for e in fb["experience"]:
        if e["id"] == "fl":
            e.update(org="Chennai Data Studio", employment_type="freelance", title="Freelance ML Engineer")
    rep = _tailor(tmp_path, fb)
    text = pdf_text(Path(rep["cv_pdf"]))
    assert re.search(r"^Freelance \u00b7 Chennai, India\s+Jan 2025 \u2013 Aug 2025$", text, re.M) and "Chennai Data Studio" not in text
    assert re.search(r"^ML Engineer$", text, re.M)


@pytest.mark.slow
def test_a_year_only_date_is_printed_as_given_not_given_a_month(fixture_run, tmp_path):
    fb = _bank()                                                                          # the MSc in the fixture is "2025 – 2026"
    rep = _tailor(tmp_path, fb)
    assert re.search(r"^University of Northfield\s+2025 \u2013 2026$", pdf_text(Path(rep["cv_pdf"])), re.M)


def _bank_with(per_job: int, per_project: int, projects: int = 5) -> dict:
    """The fixture bank cut to `per_job` bullets per job and `per_project` per project (`projects` of them): how much content is
    left to fill page 2 with. Measured with the v2 template: (2, 1) with 5 projects reaches about 69% at the highest
    linespread; (5, 2) with 5 projects reaches 96% at 1.07, between the reference's 1.03 and the ceiling 1.09 (the fill moves in
    lumps: a step of 0.02 can push an entry onto a third page, and the loop then keeps the last value that fits)."""
    fb = _bank()
    fb["experience"] = [{**e, "bullets": e["bullets"][:per_job]} for e in fb["experience"]]
    fb["projects"] = [{**p, "bullets": p["bullets"][:per_project]} for p in fb["projects"][:projects]]
    from careerops.hook import find_number
    kept = [b["id"] for e in fb["experience"] for b in e["bullets"] if find_number(b)]
    for entry in fb["hook_library"]:                                  # the library's proof bullet must still be in the cut bank
        entry["proof"] = kept[0] if entry["proof"] not in [b["id"] for e in fb["experience"] for b in e["bullets"]] else entry["proof"]
    return fb


@pytest.mark.slow
def test_optional_fact_bank_sections_are_added_in_order_when_the_bullets_run_out(tmp_path):
    fb = _bank_with(2, 1)
    fb["publications"] = [{"title": "Congestion management with firefly algorithms.", "venue": "ICEES 2023", "date": "Mar 2023"}]
    fb["languages"] = [{"name": "English", "level": "professional working"}, "Tamil"]
    fb["research_focus"] = "Graph neural networks for multi-omics integration with learnable adjacency matrices."
    rep = _tailor(tmp_path, fb)
    fill, text = rep["fill"], pdf_text(Path(rep["cv_pdf"]))
    assert fill["sections"] == ["publications", "languages", "research_focus"]
    assert all(h in text for h in ("PUBLICATIONS", "LANGUAGES", "RESEARCH FOCUS"))
    assert "Congestion management with firefly algorithms. ICEES 2023, Mar 2023" in " ".join(text.split())
    assert "English (professional working) · Tamil" in text
    assert page_count(Path(rep["cv_pdf"])) == 2
    bare = _tailor(tmp_path / "bare", _bank_with(2, 1))                      # the same bank without the sections
    assert bare["fill"]["sections"] == [] and "PUBLICATIONS" not in pdf_text(Path(bare["cv_pdf"]))
    assert rep["page2_fill"] > bare["page2_fill"] or fill["linespread"] < bare["fill"]["linespread"]   # they did fill space


@pytest.mark.slow
def test_when_content_runs_out_the_linespread_stops_at_1_09_and_the_shortfall_is_reported(tmp_path):
    rep = _tailor(tmp_path, _bank_with(2, 1))
    fill = rep["fill"]
    assert fill["linespread"] == 1.09 and fill["reached"] is False and fill["sections"] == []
    assert 0.5 < rep["page2_fill"] < 0.96 and page_count(Path(rep["cv_pdf"])) == 2        # nothing invented to make up the rest
    tex = _tex(rep)
    assert r"\linespread{1.09}" in tex and "11pt" in tex and set(re.findall(r"\\fontsize\{([\d.]+)\}", tex)) == {"22", "13", "12"}      # the 11pt body is never resized
    capped = _tailor(tmp_path / "capped", _bank_with(2, 1), cv_linespread_max=1.03)
    assert capped["fill"]["linespread"] == 1.03 and r"\linespread{1.03}" in _tex(capped)    # the ceiling is a setting
    assert capped["page2_fill"] < rep["page2_fill"]


@pytest.mark.slow
def test_the_linespread_is_raised_only_as_far_as_the_fill_needs(tmp_path):
    rep = _tailor(tmp_path, _bank_with(5, 2, 5))
    assert 1.03 < rep["fill"]["linespread"] < 1.09 and rep["fill"]["reached"] is True
    assert rep["page2_fill"] >= 0.96 and page_count(Path(rep["cv_pdf"])) == 2
    assert "\\linespread{" + str(rep["fill"]["linespread"]) + "}" in _tex(rep)


@pytest.mark.slow
def test_the_review_screen_reports_the_fill(fixture_run, settings):
    from careerops.review.workflow import fill_lines

    rep, _ = fixture_run
    line = fill_lines(rep, settings)[0]
    assert line.startswith("Fill: page 2 ") and "(target 96%)" in line and f"{len(rep['context']['projects'])} projects" in line
    assert "!!" not in line
    short = {**rep, "final": {**rep["final"], "page2_fill": 0.5}}
    assert "below target" in fill_lines(short, settings)[0]
    assert fill_lines({**rep, "one_page": True}, settings) == []
