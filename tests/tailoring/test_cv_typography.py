"""CV typography snapshot tests: the fictional fixture profile (tests/fixtures/profile/fact_bank.yaml) with a few em dashes,
hyphenated compounds, results, an award and a Databases row added, rendered with xelatex and read back with pdftotext / pdffonts.
They fail (never skip) when a tool is missing: a silent skip would hide a broken pipeline. No network, no model (MockLLM), never
the user's real profile."""
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from conftest import fixture_profile_settings

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import render, tailor, typography
from careerops.tailoring.render import pdf_text, render_cv
from careerops.tailoring.typography import audit

pytestmark = pytest.mark.slow
FIX = Path(__file__).parent.parent / "fixtures"
SCALE = {"contact and stack lines": 10 / 10.95, "body": 1.0, "hook headline": 10 / 10.95, "section headings": 1.0, "name": 17.28 / 10.95}     # the user's current CV (v2.3): \LARGE name, 11pt bold headings and body, \small (10pt) headline, contact and stack lines


def _bank() -> dict:
    fb = load_yaml(FIX / "profile" / "fact_bank.yaml")
    fb["education"][0]["dates"] = "Sep 2025 \u2013 Sep 2026"
    fb["education"][0]["note"] = "Distinction (Expected)"
    fb["skills"]["Databases"] = ["PostgreSQL", "MongoDB", "DynamoDB"]
    fb["skills"]["Languages"] = ["Python", "SQL", "R", "typescript", "Swift"]          # a row whose first item is lower case
    fb["skills"]["Engineering"] = ["FastAPI", "Docker", "GitHub Actions", "pytest", "Databricks/PySpark (coursework)"]
    exp = {e["id"]: e for e in fb["experience"]}
    proj = {p["id"]: p for p in fb["projects"]}
    az = {b["id"]: b for b in exp["az"]["bullets"]}
    az["az1"]["text"] = ("Built a tool-calling LLM agent (LangChain, AWS Bedrock) for a multi-omics cancer research platform \u2014 "
                         "with three tools for gene alias resolution and dataset lookup, used by the whole research team.")
    az["az2"]["result"] = "19,213-gene parquet fallback"
    az["az4"]["result"] = "~65 tests across 8 files"
    az["az5"]["result"] = "14 source datasets"
    exp["dc"]["bullets"][0]["text"] = ("Prototyped an MCP-based agentic tool in Python (FastMCP, PostgreSQL, GitHub Actions CI) for a "
                                       "start-up client -- presented in a live stakeholder demo.")
    proj["bitcoin"]["name"] = "Price forecasting \u2014 LSTM time-series service"
    proj["churn"]["name"] = "Price forecasting — LSTM time-series service"     # the project the three-project CV shows for this ad (Document rules)
    proj["lloyds"]["award"] = "3rd place"
    proj["lloyds"]["bullets"][0]["result"] = "3rd place; 200:1 class imbalance"
    fb["certifications"] = ["Introduction to Model Context Protocol \u2014 Anthropic, Jun 2026",
                            "AWS Academy Cloud Foundations, Nov 2025"]
    fb["publications"] = [{"title": "Congestion management with firefly algorithms", "venue": "ICEES 2023", "date": "Mar 2023"}]
    return fb


def _job() -> dict:
    return {"id": "manual:typo", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "u", "apply_url": "u",
            "description": (FIX / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")}


def _tailor(tmp_path, **cv) -> dict:
    s = fixture_profile_settings()
    s.raw["cv"] = {**s.raw.get("cv", {}), **cv}
    return tailor.tailor_job(_job(), _bank(), MockLLM(), s, tmp_path)


def _tex(rep: dict) -> str:
    return next(Path(rep["cv_pdf"]).parent.glob("*CV.tex")).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    rep = _tailor(tmp_path_factory.mktemp("typo"))
    return rep, pdf_text(Path(rep["cv_pdf"])), _tex(rep)


# ------------------------------------------------------------------------------------------------------------------ dashes
def test_no_em_dash_survives_into_the_pdf(run):
    rep, text, tex = run
    flat = " ".join(text.split())
    body = re.sub(r"(?m)^%.*$", "", tex.split(r"\begin{document}")[1])                    # the template's own comment rules
    assert "\u2014" not in text and "\u2014" not in tex and "--" not in body
    assert rep["typography"]["em_dashes"] == 0 and audit(text)["em_dashes"] == 0
    assert "multiomics cancer research platform, with three tools" in flat or "multi-omics cancer research platform, with three tools" in flat
    assert "Introduction to Model Context Protocol, Anthropic, Jun 2026" in flat


def _render_chosen(tmp_path, projects: dict[str, list[str]], **bank_edits):
    """Render exactly these projects (project id -> bullet ids) after a role each, bypassing the selection: for the tests that need
    a particular project on the page."""
    fb = _bank()
    for key, edit in bank_edits.items():
        edit(fb)
    sel = {"experience": {e["id"]: [e["bullets"][0]["id"]] for e in fb["experience"]}, "projects": list(projects),
           "project_bullets": projects}
    ctx = tailor._context(fb, sel, {}, "Data Scientist with an MSc.", [], "Data Scientist", {"Core": ["Python"]})
    pdf = render_cv(ctx, tmp_path, "cv")
    return ctx, pdf_text(pdf), " ".join(pdf_text(pdf).split())


def test_a_project_name_with_a_dash_prints_a_colon_and_keeps_its_hyphen(tmp_path):
    ctx, text, flat = _render_chosen(tmp_path, {"bitcoin": ["bitcoin1"]})
    assert "Price forecasting: LSTM time-series service" in flat and "—" not in text


def test_the_checks_saw_the_original_wording_and_the_report_keeps_it(run):
    rep, _, _ = run
    assert any("\u2014" in b for e in rep["context"]["experience"] for b in e["bullets"]), \
        "report.json keeps the text the faithfulness checks ran on"
    assert any("\u2014" in p["name"] for p in rep["context"]["projects"])


def test_hyphens_are_kept_and_protected_terms_and_urls_stay(run):
    rep, text, _ = run
    flat = " ".join(text.split()).casefold()
    assert "scikit-learn" in flat
    urls = re.findall(r"https://\S+", text)                                              # the project URLs (18 Oct) and the header's full URLs (v2.3)
    assert "https://github.com/ada-lovelace-test/churn#readme" in urls
    assert all(u.startswith(("https://github.com/ada-lovelace-test", "https://www.linkedin.com/", "https://ada-lovelace-test.example.org")) for u in urls)
    assert "linkedin.com/in/ada-lovelace-test" in flat                              # a URL is never touched
    assert rep["typography"]["hyphens"] == {}                                        # nothing is reported: a hyphen is no defect (12 Nov 2026)


def test_the_date_separator_is_a_setting(tmp_path):
    rep = _tailor(tmp_path, date_separator=" to ")
    text = pdf_text(Path(rep["cv_pdf"]))
    assert re.search(r"May 2026 to Sep 2026", text) and re.search(r"Jan 2025 to Aug 2025", text)
    assert "May 2026 \u2013 Sep 2026" not in text and rep["typography"]["date_separator"] == " to "


# ---------------------------------------------------------------------------------------------------------------- type scale
def _word_heights(pdf) -> list[tuple[str, float]]:
    """(word, height in points) of every word on page 1, from pdftotext -bbox. The box is the font's ascent to descent, so within
    one family it is proportional to the font size."""
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    page1 = xml.split("</page>")[0]
    return [(m.group(3), float(m.group(2)) - float(m.group(1))) for m in
            re.finditer(r'<word xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</word>', page1)]


def test_the_type_scale_is_the_users_current_cv(run):
    rep, _, tex = run
    words = _word_heights(rep["cv_pdf"])
    body = Counter(round(h, 1) for _, h in words).most_common(1)[0][0]                    # the bullets: by far the most words
    ratio = lambda w: next(h for t, h in words if t == w) / body                           # noqa: E731
    assert ratio("Ada") == pytest.approx(SCALE["name"], rel=0.03) and ratio("Lovelace") == pytest.approx(SCALE["name"], rel=0.03)
    assert ratio("PROFILE") == pytest.approx(SCALE["section headings"], rel=0.03)             # headings are the body's size (bold, with a rule)
    assert ratio("SKILLS") == pytest.approx(SCALE["section headings"], rel=0.03)                 # page 1 (Skills second since 10 Nov 2026)
    assert ratio("Bristol,") == pytest.approx(SCALE["contact and stack lines"], rel=0.03)  # the contact line
    ratios = {round(h / body, 2) for _, h in words}
    assert all(any(abs(r - s) <= 0.03 for s in SCALE.values()) for r in ratios), ratios   # nothing at any other size
    assert min(ratios) >= 0.90                                                            # the smallest text is 10pt
    assert r"\documentclass[a4paper,11pt]{article}" in tex
    assert "\\fontsize" not in tex and r"\LARGE\bfseries" in tex and r"\small\textbf{" in tex     # \LARGE name, \small bold headline; the 10pt lines are \small


def _line_heights(pdf) -> dict[str, float]:
    """{text of a line: its height} on every page, from pdftotext -bbox-layout."""
    xml = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    out = {}
    for m in re.finditer(r'<line xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</line>', xml, re.S):
        out[" ".join(re.findall(r">([^<]+)</word>", m.group(3)))] = float(m.group(2)) - float(m.group(1))
    return out


def test_the_stack_line_is_10pt_and_the_bullets_11pt(run):
    rep, _, _ = run
    words = _word_heights(rep["cv_pdf"])
    body = Counter(round(h, 1) for _, h in words).most_common(1)[0][0]
    lines = _line_heights(rep["cv_pdf"])
    stacks = [p["stack"] for p in rep["context"]["projects"] if p.get("stack") and p["stack"] in lines]
    assert stacks, "no project stack line found on any page"
    assert all(abs(lines[s] / body - 10 / 11) < 0.04 for s in stacks)
    bullet = next(h for t, h in words if t == "Built")
    assert abs(bullet / body - 1.0) < 0.03


# --------------------------------------------------------------------------------------------------- bold and italic
def _group(tex: str, command: str) -> list[str]:
    """The braced argument of every \\<command>{...} in `tex`, nested braces included."""
    out = []
    for m in re.finditer(re.escape(command) + r"\{", tex):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(tex[i], 0)
            i += 1
        out.append(tex[m.end():i - 1])
    return out


def _plain(tex: str) -> str:
    """LaTeX text back to the words it prints."""
    t = re.sub(r"\\textit\{([^{}]*)\}", r"\1", tex)
    return t.replace(r"\textasciitilde{}", "~").replace(r"\&", "&").replace(r"\%", "%").replace(r"\textquotesingle{}", "'")


def _macro(tex: str, name: str, n: int) -> list[tuple[str, ...]]:
    """The n braced arguments of every \\<name>{..}{..} in `tex` (the reference CV's macros), nested braces included."""
    out = []
    for m in re.finditer(r"\\" + name + r"(?=\{)", tex):
        i, args = m.end(), []
        for _ in range(n):
            assert tex[i] == "{", (name, tex[m.start():i + 20])
            depth, j = 1, i + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(tex[j], 0)
                j += 1
            args.append(tex[i + 1:j - 1])
            i = j
        out.append(tuple(args))
    return out


def test_bold_is_only_the_employer_school_project_name_labels_badges_and_results(run):
    """The body has no \\textbf of its own: bold comes only through the reference's macros (role / roleNoLoc / project bold their
    first argument, skillgroup its label, badge its text, num its result), so it is enough to check what goes into those. The one
    bold of the body's own is the lead of an Achievements line (the award project's name and award)."""
    rep, _, tex = run
    body = tex.split(r"\begin{document}")[1]
    t = typography.typeset(rep["context"])                                                  # what the template was given
    lead = re.compile(r"\\item \\textbf\{[^{}]*}")
    edu = [ln for ln in body.splitlines() if ln.startswith(r"\noindent\textit{") and r"\textbf{" in ln]                # the compact Education lines (8 Nov 2026)
    plain = "\n".join(ln for ln in body.splitlines() if r"\url{" not in ln and r"\small\textbf{" not in ln and ln not in edu)    # the project-link layout (18 Oct) and the headline set their own bold
    assert len(lead.findall(body)) == len(rep["achievements"]) == 1 and r"\textbf" not in lead.sub("", plain)
    employers = {render.org_parts(e)[0] for e in t["experience"]}
    schools = {render.school_line(e) for e in t["education"]}
    roles, no_loc = _macro(body, "role", 4), _macro(body, "roleNoLoc", 3)
    assert len(roles) + len(no_loc) == len(t["experience"])                                # Education is compact lines, not role macros
    assert {_plain(o) for o, *_ in roles} <= employers and {_plain(o) for o, *_ in no_loc} <= employers
    assert {_plain(o) for o, *_ in roles} | {_plain(o) for o, *_ in no_loc} >= employers   # every one is there
    assert len(edu) == len(t["education"]) and all(ln.count(r"\textbf{") == 1 for ln in edu)            # one bold per line: the school
    assert all(r"\num" not in x and r"\textbf" not in x for a in roles + no_loc for x in a)           # dates, title, location: plain
    names = [_plain(n) for n, *_ in _macro(body, "project", 3)]
    assert names == [render.project_parts(p)[0] for p in t["projects"]]
    assert [_plain(label) for label, _ in _macro(body, "skillgroup", 2)] == list(t["skills"])         # group labels
    awards = {p["award"].strip() for p in t["projects"] if p.get("award")}
    assert {_plain(b) for b, in _macro(body, "badge", 1)} <= awards | {"MSc Coursework", "Coursework"}
    results = [r for r in t["results"].values() if r.split()]
    for (b,) in _macro(body, "num", 1):
        plain = _plain(b)
        if plain == (rep.get("hook") or {}).get("number"):
            continue                                                                        # the hook headline's number, in bold like a result
        assert any(plain.split()[0] == r.split()[0] and plain.split()[-1].strip(".,;") == r.split()[-1] for r in results), \
            f"\\num text that is not a bullet's result: {plain!r}"
    assert not re.search(r"\\num\{[^{}]*\\num", body)                                        # nothing inside a result


def test_the_bullets_results_are_in_num_and_nothing_else_in_a_bullet_is(run):
    rep, _, tex = run
    items = re.findall(r"\\item (.*)", tex.split(r"\begin{document}")[1])
    nums = [[b for b, in _macro(i, "num", 1)] for i in items]                               # (a result may hold \textasciitilde{})
    got = {b for bs in nums for b in bs}
    assert "19,213-gene parquet fallback" in got                                           # the az2 result, in its bullet (hyphens are kept)
    assert "14 heterogeneous source datasets" in got                                        # az5: found through one extra word
    assert all(len(bs) <= 1 for bs in nums)                                                 # at most one result run per bullet
    assert rep["typography"]["results_bold"] == sum(len(bs) for bs in nums) >= 2
    assert isinstance(rep["typography"]["results_unmatched"], list)
    assert not any(re.search(r"\\num\{[^{}]*\d+\\%", i) for i in items)                     # a number in a bullet is not wrapped
    assert r"\textbf" not in "".join(re.sub(r"^\\textbf\{[^{}]*\}", "", i) for i in items)      # (an Achievements line leads with the bold name)


def test_the_italic_list(run):
    rep, _, tex = run
    body = tex.split(r"\begin{document}")[1]
    it = _group(body, r"\textit")
    assert "(coursework)" in it                                                             # the marker (an expected grade is not printed since 8 Nov 2026)
    assert "Jun 2026" in it and "Nov 2025" in it                                            # a certification's date
    assert "Anthropic" not in it                                                            # its issuer is not italic
    roles, no_loc = _macro(body, "role", 4), _macro(body, "roleNoLoc", 3)
    assert "Data Scientist (MSc dissertation)" in [_plain(r[3]) for r in roles]              # job title: the macro's last argument
    assert "Bristol, UK" in [_plain(r[1]) for r in roles]                                    # location
    assert "MSc Data Science" in it and "B.E. Electrical and Electronics Engineering" in it    # degree names (the compact Education lines)
    assert any(_plain(s).startswith("Python, scikit-learn") for _, s, _ in _macro(body, "project", 3))   # a stack line
    fonts = subprocess.run(["pdffonts", str(rep["cv_pdf"])], capture_output=True, text=True, check=True).stdout
    names = [ln.split()[0].split("+")[-1] for ln in fonts.splitlines()[2:] if ln.strip()]
    assert any("Italic" in n for n in names) and any("Bold" in n for n in names) and all(n.startswith(("Carlito", "LMMono10")) for n in names)   # LMMono10: the default \url font, project URLs only


# ---------------------------------------------------------------------------------------------------------------- selection
def test_the_chosen_bullets_of_a_role_print_proof_first_then_numbers_then_the_rest(run):
    """v3.9.1 (replaces the fact-bank order): the hook's proof bullet, then bullets with a number or a result, then the others."""
    rep, _, _ = run
    ctx = rep["context"]
    proof = (rep["hook"] or {}).get("proof_bullet_id")
    for e in ctx["experience"] + ctx["projects"]:
        assert proof not in e["bullet_ids"] or e["bullet_ids"][0] == proof
        rest = [(i, t) for i, t in zip(e["bullet_ids"], e["bullets"]) if i != proof]
        has = [any(c.isdigit() for c in t) or i in (ctx.get("results") or {}) for i, t in rest]
        assert has == sorted(has, reverse=True), (e.get("org") or e.get("name"), e["bullet_ids"])


def test_the_report_has_the_top_ten_ranking_with_scores(run):
    rep, _, _ = run
    r = rep["ranking"]
    assert len(r["bullets"]) == 10 and r["bullets"][0]["score"] >= r["bullets"][-1]["score"]
    chosen = {i for e in rep["context"]["experience"] + rep["context"]["projects"] for i in e["bullet_ids"]}
    assert {b["id"] for b in r["bullets"] if b["chosen"]} <= chosen and r["projects"]


def test_a_projects_award_is_in_its_heading_and_not_in_its_bullets(tmp_path):
    def repeat_it(fb):
        p = next(p for p in fb["projects"] if p["id"] == "lloyds")
        p["bullets"][0]["text"] = "Built an anomaly detector on 200:1 class imbalance, 3rd place, tracked in MLflow."
    ctx, text, flat = _render_chosen(tmp_path, {"lloyds": ["lloyds1"]}, repeat=repeat_it)
    assert flat.count("3rd place") == 1, "the award is printed once, in the heading"
    assert "Built an anomaly detector on 200:1 class imbalance, tracked in MLflow." in flat
    assert tailor.typography_report(ctx, text, " – ", [])["award_repeats"] == []
    assert ctx["projects"][0]["bullets"][0].count("3rd place") == 1             # the context the checks saw is untouched


def test_the_report_shows_an_award_repeated_in_a_bullet_when_it_cannot_be_taken_out():
    ctx = {"experience": [], "results": {}, "projects": [{"id": "x", "name": "N", "award": "3rd place", "bullet_ids": ["b"],
                                                           "bullets": ["Finished third overall, 3rd"]}]}
    assert tailor.typography_report(ctx, "", " – ", [])["award_repeats"] == ["b"]


def test_the_first_item_of_every_skills_row_is_capitalised(run):
    _, text, tex = run
    body = tex.split(r"\begin{document}")[1]
    firsts = [items[0] for _, items in _macro(body, "skillgroup", 2)]
    assert len(firsts) == 4 and all(c.isupper() for c in firsts), firsts


def test_the_databases_are_in_the_cloud_group_when_the_jd_mentions_sql(tmp_path):
    s = fixture_profile_settings()
    rep = tailor.tailor_job(_job(), _bank(), MockLLM(), s, tmp_path)                        # the JD says "Strong Python and SQL"
    cloud = rep["context"]["skills"]["Python, Data Quality & Cloud"]
    assert {"PostgreSQL", "SQL"} <= set(cloud) and "Databases" not in rep["skills"]["rows"]   # the Databases bank group is claimed by that group
    assert "Databases" not in rep["skills"]["dropped_groups"]


def test_the_one_page_template_has_no_em_dash_either(run, tmp_path):
    rep, _, _ = run
    text = pdf_text(render_cv(rep["context"], tmp_path, "cv", one_page=True))
    assert "\u2014" not in text and "--" not in text and any(q["name"] in text for q in rep["context"]["projects"] if "—" not in q["name"])
