"""v3.3: can an ATS parser read the CV? The structural score on the real PDF text layer (ats_structure.py), the content lints, the screener signals
(review/screeners.py), the auto-submit gate, the dry-run report.md and doctor. Small PDFs are built with xelatex here; a missing xelatex or poppler
FAILS these tests (CLAUDE.md: a silent skip hides a broken pipeline)."""
import copy
import hashlib
import json
import struct
import subprocess
import zlib
from datetime import date
from pathlib import Path

import pytest
import yaml

from careerops import ats_structure as AS
from careerops import cli
from careerops.apply import autogate, dryrun, requiredgate
from careerops.config import ROOT, load_yaml
from careerops.discovery import gates
from careerops.review import screeners, workflow
from careerops.setup import ats_structure_checks
from conftest import PROFILE_FIX

pytestmark = pytest.mark.slow              # the PDF tests run xelatex (tests/conftest.py asks for the mark)
TODAY = date(2026, 11, 7)
PROSE = ("Built forecasting tools in Python and SQL for clinical researchers, validated every release with tests, and reported the results "
         "honestly to the team. ")


# ------------------------------------------------------------------ building PDFs
def build(tmp_path: Path, body: str, name: str = "cv", preamble: str = "") -> Path:
    tex = tmp_path / f"{name}.tex"
    tex.write_text("\\documentclass[10pt]{article}\n\\usepackage[margin=0.7in]{geometry}\n\\usepackage{graphicx}\n" + preamble +
                   "\n\\pagestyle{empty}\n\\setlength{\\parindent}{0pt}\n\\begin{document}\n" + body + "\n\\end{document}\n", encoding="utf-8")
    proc = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex.name], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-1500:]
    return tex.with_suffix(".pdf")


def clean_body(email: str = "ada.lovelace@example.org | +44 7700 900123", extra: str = "") -> str:
    return f"""{{\\Large Ada Lovelace}}\\par
Data Scientist\\par
Bristol, UK | {email}\\par
\\medskip
{{\\bfseries PROFILE}}\\par
{PROSE * 2}Costs fell by £3,000 a month – a café's worth.\\par
{{\\bfseries SKILLS}}\\par
Programming\\par
Python, SQL, R, scikit-learn, pandas, PyTorch\\par
{{\\bfseries WORK EXPERIENCE}}\\par
Acme Analytics · Bristol, UK \\hfill Jan 2023 – Sep 2024\\par
Data Scientist\\par
\\begin{{itemize}}\\item {PROSE}\\item Shipped a churn model to production with a monitored rollout.\\end{{itemize}}
Globex Labs · London, UK \\hfill Oct 2024 – Present\\par
Machine Learning Engineer\\par
\\begin{{itemize}}\\item {PROSE}\\end{{itemize}}
{{\\bfseries EDUCATION}}\\par
University of Northfield \\hfill Sep 2019 – Jun 2022\\par
BSc Mathematics\\par
{extra}"""


ROLES = [{"id": "acme", "title": "Data Scientist", "org": "Acme Analytics", "dates": "Jan 2023 – Sep 2024"},
         {"id": "globex", "title": "Machine Learning Engineer", "org": "Globex Labs", "dates": "Oct 2024 – Present"}]


def png(path: Path, w: int = 1200, h: int = 1600) -> Path:
    """A grey PNG, written by hand (no image library)."""
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)      # noqa: E731
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))
    return path


# ------------------------------------------------------------------ A. the structural score
def test_a_clean_single_column_cv_scores_a_with_no_critical(tmp_path):
    st = AS.check_pdf(build(tmp_path, clean_body()), roles=ROLES)
    assert st.critical == [] and st.grade == "A" and st.ok, [i.line() for i in st.issues]
    assert st.parts["utf8"] == (5, 5)                                         # £ – é survived extraction
    assert st.line() == f"ATS structure {st.score}/A (0 critical)"


def test_an_image_only_pdf_is_critical(tmp_path):
    png(tmp_path / "scan.png")
    st = AS.check_pdf(build(tmp_path, "\\includegraphics[width=\\textwidth]{scan.png}"))
    codes = {i.code for i in st.critical}
    assert "text-layer" in codes and not st.ok and st.grade == "F"
    assert any(i.code == "images-text" for i in st.issues)


def test_a_two_column_pdf_is_flagged_for_reading_order(tmp_path):
    left = " ".join(f"Left column sentence number {i} about pipelines and models." for i in range(14))
    right = " ".join(f"Right column sentence number {i} about teams and stakeholders." for i in range(14))
    two = (f"\\begin{{minipage}}[t]{{0.46\\textwidth}}{left}\\end{{minipage}}\\hfill"
           f"\\begin{{minipage}}[t]{{0.46\\textwidth}}{right}\\end{{minipage}}\\par")
    st = AS.check_pdf(build(tmp_path, clean_body(extra=two)), roles=ROLES)
    assert any(i.code == "two-column" and i.severity == AS.CRITICAL for i in st.issues) and not st.ok


def test_a_fi_ligature_in_the_extraction_is_flagged():
    text = clean_text().replace("forecasting tools", "ﬁnancial forecasting tools")
    st = AS.score(AS.Extract(text, text), roles=[])
    assert any(i.code == "ligature-glyphs" and "ﬁ" in i.message for i in st.issues) and st.parts["fonts"][0] < 10


def test_mojibake_in_the_extraction_is_flagged():
    text = clean_text() + "\nCosts fell by Â£3,000 â€“ a cafÃ©.\n"
    assert any(i.code == "mojibake" for i in AS.score(AS.Extract(text, text)).issues)


def test_an_email_only_inside_a_link_annotation_is_critical(tmp_path):
    body = clean_body(email="\\href{mailto:ada.lovelace@example.org}{Write to me} | +44 7700 900123")
    pdf = build(tmp_path, body, preamble="\\usepackage{hyperref}")
    ex = AS.extract(pdf)
    assert "mailto:ada.lovelace@example.org" in ex.uris and "@" not in ex.layout
    st = AS.score(ex, roles=ROLES)
    assert [i.code for i in st.critical] == ["email-in-link-only"] and not st.ok


def test_no_experience_heading_is_critical():
    text = clean_text().replace("WORK EXPERIENCE", "WHAT I DID")
    st = AS.score(AS.Extract(text, text))
    assert any(i.code == "section-headings" and i.severity == AS.CRITICAL for i in st.issues)


def test_a_role_whose_bullets_come_before_its_header_in_raw_order_is_critical():
    text = clean_text()
    head = "Acme Analytics · Bristol, UK      Jan 2023 – Sep 2024\nData Scientist\n"
    assert head in text
    raw = text.replace(head, "").replace("• Shipped a churn model", head + "• Shipped a churn model")       # its first bullet is read before its header
    st = AS.score(AS.Extract(text, raw), roles=ROLES)
    assert any(i.code == "role-order" for i in st.critical)


def test_stuffing_outside_skills_and_white_text_are_flagged():
    text = clean_text() + "\n" + "Kubernetes " * 6 + "\n"
    st = AS.score(AS.Extract(text, text), keywords=["Kubernetes"], source="\\textcolor{white}{python python}")
    hit = next(i for i in st.issues if i.code == "hidden-or-stuffed")
    assert "Kubernetes ×6" in hit.message and "white" in hit.message and st.parts["hidden"] == (0, 5)


def test_grades():
    assert [AS.grade(x) for x in (95, 90, 85, 75, 65, 10)] == ["A", "A", "B", "C", "D", "F"]


def test_a_letter_is_scored_on_the_checks_that_apply_to_it(tmp_path):
    body = "Ada Lovelace | ada.lovelace@example.org | +44 7700 900123\\par\\medskip Hi Acme team,\\par " + PROSE * 4 + "\\par Regards,\\par Ada Lovelace"
    st = AS.check_pdf(build(tmp_path, body, name="letter"), kind="letter")
    assert st.kind == "letter" and set(st.parts) == set(AS.LETTER_CHECKS) and st.score == 100 and st.line() == "letter structure 100/A (0 critical)"


def clean_text() -> str:
    return ("Ada Lovelace\nData Scientist\nBristol, UK | ada.lovelace@example.org | +44 7700 900123\n\nPROFILE\n" + PROSE * 3 + "\nSKILLS\nProgramming\n"
            "Python, SQL, R, scikit-learn\nWORK EXPERIENCE\nAcme Analytics · Bristol, UK      Jan 2023 – Sep 2024\nData Scientist\n• " + PROSE +
            "\n• Shipped a churn model to production.\nGlobex Labs · London, UK      Oct 2024 – Present\nMachine Learning Engineer\n• " + PROSE +
            "\nEDUCATION\nUniversity of Northfield      Sep 2019 – Jun 2022\nBSc Mathematics\n")


# ------------------------------------------------------------------ B. the lints
LINT_CASES = [
    ("multiple-date-ranges", {"id": "avasoft", "org": "Avasoft", "title": "Software Development Engineer (Jul 2023 - Sep 2024); Trainee (Jan 2023 - Jun 2023)",
                              "dates": "Jan 2023 – Sep 2024"}),
    ("parenthetical-in-title", {"id": "az", "org": "Northwind", "title": "Data Scientist (MSc Dissertation)", "dates": "May 2026 – Sep 2026"}),
    ("parenthetical-in-company", {"id": "gx", "org": "Globex (Cloud Platform Division)", "title": "Data Engineer", "dates": "2021 – 2022"}),
    ("employer-in-role", {"id": "c1", "org": "Freelance", "title": "Data Scientist at Initech Ltd", "dates": "2020 – 2021"}),
]


@pytest.mark.parametrize("code,role", LINT_CASES)
def test_each_role_lint_fires_on_its_example_and_quotes_the_line(code, role):
    got = [l for l in AS.role_lints([role]) if l.code == code]
    assert got and role["title" if code != "parenthetical-in-company" else "org"] in got[0].value
    assert "decide:" in got[0].line() and any(w in got[0].decision for w in ("yourself", "your call", "yours to decide"))


def test_employer_in_role_sees_another_roles_employer():
    roles = [{"id": "a", "org": "Initech", "title": "Consultant for Initech", "dates": "2020 – 2021"},
             {"id": "b", "org": "Initech", "title": "Analyst", "dates": "2019 – 2020"},
             {"id": "c", "org": "Hooli", "title": "Data Scientist, Initech project", "dates": "2021 – 2022"}]
    got = [l for l in AS.role_lints(roles) if l.code == "employer-in-role"]
    assert [l.where for l in got] == ["Hooli"] and "Initech" in got[0].decision


def test_date_format_mixed_fires_on_mixed_separators_and_formats():
    seps = "Acme   Jan 2023 - Sep 2024\nGlobex   Oct 2024 -- Present\n"
    assert any("separators" in l.decision for l in AS.date_lints(seps))
    fmts = "Acme   Jan 2023 – Sep 2024\nGlobex   09/2024 – Present\n"
    got = AS.date_lints(fmts)
    assert got and "MM/YYYY" in got[0].value and "Mon YYYY" in got[0].value


def test_skills_undelimited_fires_on_a_run_together_line():
    text = clean_text().replace("Python, SQL, R, scikit-learn", "Python SQL R scikit-learn pandas PyTorch Docker")
    got = AS.skills_lints(text)
    assert [l.code for l in got] == ["skills-undelimited"] and "Python SQL R" in got[0].value


def test_every_lint_is_silent_on_a_clean_cv():
    assert AS.lints(ROLES, clean_text()) == []


def test_the_lints_never_rewrite_anything(tmp_path, settings):
    """The guard: no write call in the two modules, and a full check leaves the fact bank's bytes and its parsed data as they were."""
    for mod in ("careerops/ats_structure.py", "careerops/review/screeners.py"):
        src = (ROOT / mod).read_text(encoding="utf-8")
        assert not any(w in src for w in ("write_text", "write_bytes", "safe_dump", "yaml.dump", 'open(', ".unlink(", ".rename(")), mod
    bank = PROFILE_FIX / "fact_bank.yaml"
    before = hashlib.sha256(bank.read_bytes()).hexdigest()
    fb = load_yaml(bank)
    snapshot = copy.deepcopy(fb)
    roles = [dict(r) for r in fb["experience"]]
    AS.lints(roles, clean_text())
    screeners.check({"keywords": [], "cv_pdf": None}, settings, fb, {"title": "Data Scientist", "description": "x"})
    assert hashlib.sha256(bank.read_bytes()).hexdigest() == before and fb == snapshot and roles == [dict(r) for r in snapshot["experience"]]


# ------------------------------------------------------------------ D. where it shows up
def test_the_user_template_output_scores_well_and_its_lints_show(tailored, settings, db, job, capsys):
    """The mock run: the fixture job tailored through the real templates (xelatex), then checked."""
    rep, fb, _ = tailored
    chk = screeners.check(rep, settings, fb, db.job(job["id"]), today=TODAY)
    st = chk.structure
    with capsys.disabled():
        print(f"\n  mock-run CV: {st.cv.line()} · {st.letter.line() if st.letter else 'no letter'} · {chk.line()}")
    assert st.cv.critical == [] and st.cv.grade in ("A", "B") and st.ok
    assert any(l.code == "parenthetical-in-title" and "(MSc dissertation)" in l.value for l in st.lints)
    assert chk.line().startswith(f"ATS structure {st.cv.score}/{st.cv.grade} (0 critical) | lints: ")
    screen = workflow.summary(db.job(job["id"]), rep, settings, fb)
    assert chk.line() in screen


def test_the_gate_blocks_a_critical_issue_or_a_score_under_80(monkeypatch):
    def with_cv(st):
        monkeypatch.setattr(AS, "for_report", lambda *a, **k: AS.JobStructure(st, None, []))
        return autogate._ats_structure({}, {})
    assert with_cv(AS.Structure("cv", 95)).ok
    g = with_cv(AS.Structure("cv", 79))
    assert not g.ok and "79 < 80" in g.why
    g = with_cv(AS.Structure("cv", 95, [AS.Issue("email-missing", AS.CRITICAL, "x")]))
    assert not g.ok and "critical (email-missing)" in g.why


def test_the_review_line_is_red_when_the_cv_may_not_go_out(monkeypatch, settings):
    monkeypatch.setattr(AS, "for_report", lambda *a, **k: AS.JobStructure(AS.Structure("cv", 72, [AS.Issue("two-column", AS.CRITICAL, "x")]), None, []))
    lines = screeners.check({"keywords": []}, settings, {}, {"title": "Data Scientist", "description": ""}).lines()
    assert lines[0].startswith(screeners.RED) and "auto-submit is blocked; in assist" in lines[0]


class _Page:
    def screenshot(self, **k):
        raise RuntimeError("no page in a test")


def test_the_dry_run_record_writes_report_md_with_the_ats_line(tmp_path):
    rep = {"ats": {"strict": 74.0, "ceiling": 90.0}, "letter": {"status": "off"},
           "ats_check": {"line": "ATS structure 94/A (0 critical) | lints: none | title match: yes", "lines": ["head", "  LINT x (y): z"], "ok": True}}
    job = {"id": "j1", "company": "Acme", "title": "Data Scientist", "url": "https://example.com/j1"}
    rows = [requiredgate.Row("Phone", "phone", "text", True, "EMPTY")]
    folder = dryrun.record(tmp_path / "run", _Page(), job, rep, rows, cv=None, letter=None, letter_text=None, out=lambda *_: None)
    md = (folder / "report.md").read_text(encoding="utf-8")
    assert "ATS strict CV 74.0% | claimable ceiling 90.0%" in md and "ATS structure 94/A (0 critical) | lints: none" in md and "LINT x (y): z" in md
    assert json.loads((folder / "run.json").read_text(encoding="utf-8"))["ats_structure"]["ok"] is True


def test_the_dry_run_record_checks_the_pdf_when_live_attached_nothing(tmp_path):
    pdf = build(tmp_path, clean_body())
    folder = dryrun.record(tmp_path / "run", _Page(), {"company": "Acme", "title": "DS"}, {"cv_pdf": str(pdf)}, [], cv=None, letter=None, letter_text=None,
                           out=lambda *_: None)
    assert "ATS structure" in (folder / "report.md").read_text(encoding="utf-8") and "/A (0 critical)" in (folder / "report.md").read_text(encoding="utf-8")


def test_doctor_checks_the_newest_tailored_cv(tailored, settings):
    rows = ats_structure_checks(settings)
    assert len(rows) == 1 and rows[0][0].startswith("ATS structure of the newest CV (") and "lints: parenthetical-in-title" in rows[0][0]
    assert rows[0][1] in (True, "warn")


def test_doctor_says_when_there_is_no_tailored_cv(settings):
    assert ats_structure_checks(settings)[0][1] == "warn"


def test_the_cli_checks_a_pdf(tmp_path, settings, capsys):
    pdf = build(tmp_path, clean_body())
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "ats-check", str(pdf)])
    assert e.value.code == 0 and capsys.readouterr().out.startswith("ATS structure ")
