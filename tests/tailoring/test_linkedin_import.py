"""`careerops import-linkedin`: the LinkedIn profile PDF's text -> a proposal in fact-bank schema and a list of every
mismatch with the fact bank. Runs on a fictional profile saved as text (tests/fixtures/tailoring/linkedin_fixture.txt: the same
layout as pdftotext gives for a browser-printed profile, page headers and footers and debris included), and on the
fictional fact bank. Never the user's real LinkedIn export or fact bank; the real bank is never opened."""
import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from careerops import cli
from careerops.tailoring import linkedin as L

FIX = Path(__file__).parent.parent / "fixtures"
TEXT = (FIX / "tailoring" / "linkedin_fixture.txt").read_text(encoding="utf-8")
BANK = FIX / "profile" / "fact_bank.yaml"


@pytest.fixture(scope="module")
def parsed():
    return L.parse_linkedin(TEXT)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------------------- parsing
def test_every_section_is_found(parsed):
    assert [len(parsed[k]) for k in ("experience", "education", "projects", "certifications", "publications", "languages")] \
        == [5, 2, 2, 2, 1, 2]


def test_a_stand_alone_role_has_title_org_type_dates_and_location(parsed):
    first = parsed["experience"][0]
    assert (first["title"], first["org"], first["employment_type"], first["dates"]) == (
        "Data Scientist", "Northwind Research Lab", "indirect contract", "May 2026 – Sep 2026")
    assert first["location"] == "Bristol, England, United Kingdom"
    freelance = parsed["experience"][2]
    assert (freelance["org"], freelance["employment_type"], freelance["title"]) == ("Self Employed", "freelance", "AI/ML Engineer")


def test_the_roles_of_one_company_share_its_name_and_location_and_keep_their_own_dates(parsed):
    sde, trainee = parsed["experience"][3:]
    assert (sde["org"], trainee["org"]) == ("HARBOUR PAY", "HARBOUR PAY")
    assert (sde["title"], sde["dates"]) == ("Software Development Engineer", "Jun 2023 – Sep 2024")
    assert (trainee["title"], trainee["dates"]) == ("Software Development Engineer Trainee", "Jan 2023 – May 2023")
    assert sde["location"] == trainee["location"] == "Chennai, Tamil Nadu, India"
    assert sde["employment_type"] == "full-time"


def test_bullets_are_rejoined_across_wrapped_lines_blank_lines_and_page_breaks(parsed):
    first = parsed["experience"][0]["bullets"]
    assert len(first) == 3 and first[0].startswith("Surveyed 14 heterogeneous source datasets")
    assert "cross-validation of every release" in first[2]                    # "cross-" + "validation" rejoined
    assert [len(e["bullets"]) for e in parsed["experience"]] == [3, 2, 1, 3, 1]    # the third Harbour Pay bullet is past a page break
    assert parsed["experience"][3]["bullets"][2].startswith("Delivered 10 reusable component templates")


def test_page_headers_footers_and_debris_never_reach_the_data(parsed):
    flat = repr(parsed)
    for junk in ("LinkedIn", "linkedin.com", "ata Sc e ce", "1:15 PM", "skills", "Show all"):
        assert junk not in flat


def test_education_has_dates_grade_modules_and_the_research_focus(parsed):
    msc, be = parsed["education"]
    assert (msc["org"], msc["degree"], msc["dates"]) == ("University of Northfield", "Master of Science - MSc, Data Science",
                                                         "Sep 2025 – Sep 2026")
    assert msc["modules"] == ["Large-Scale Data Engineering", "Statistical Computing & Empirical Methods", "AI & Text Analytics"]
    assert parsed["research_focus"].startswith("graph neural networks for multi-omics integration")
    assert (be["org"], be["dates"], be["grade"]) == ("St. Joseph's College Of Engineering", "Aug 2019 – May 2023", "91 %")


def test_projects_certifications_publications_and_languages(parsed):
    assert [p["name"] for p in parsed["projects"]] == [
        "Omics Graph: Multi-Omics Graph for Cancer Cell Line Selection (PoC)",
        "Mining Insights from Customer Feedback NLP & Unsupervised Analytics Pipeline"]
    assert parsed["projects"][0]["associated_with"] == "Northwind Research Lab"
    assert [(c["name"], c["issuer"], c["date"]) for c in parsed["certifications"]] == [
        ("Model Context Protocol: Advanced Topics", "Anthropic", "Jun 2026"), ("Introduction to MCP", "Anthropic", "Jun 2026")]
    pub = parsed["publications"][0]
    assert pub["title"].startswith("Modern Power System Operations") and pub["title"].endswith("Firefly Algorithms")
    assert (pub["venue"], pub["date"]) == ("icees2023 EEE SSN", "Mar 23, 2023")
    assert parsed["languages"] == [{"name": "English", "level": "Professional working proficiency"},
                                   {"name": "Tamil", "level": "Native or bilingual proficiency"}]       # Tamil once, not twice


def test_a_section_with_more_items_than_the_pdf_holds_is_a_warning_and_a_cut_description_too(parsed):
    warnings = L.parse_warnings(parsed)
    assert any("10 items under 'Projects'" in w and "holds 2" in w for w in warnings)
    assert any("5 items under 'Licenses & certifications'" in w for w in warnings)
    assert any("Omics Graph" in w and "'...'" in w for w in warnings)


# ------------------------------------------------------------------------------------------- the diff and the proposal
@pytest.fixture
def imported(tmp_path):
    bank = tmp_path / "fact_bank.yaml"
    shutil.copy(BANK, bank)
    before = _digest(bank)
    res = L.import_linkedin(tmp_path / "linkedin.pdf", bank, tmp_path / "proposed.yaml", tmp_path / "diff.md", text=TEXT)
    return res, bank, before, tmp_path


def test_the_fact_bank_is_only_read(imported):
    _, bank, before, _ = imported
    assert _digest(bank) == before


def test_the_diff_lists_every_kind_of_mismatch(imported):
    diff = (imported[3] / "diff.md").read_text(encoding="utf-8")
    exp = diff.split("## Experience")[1].split("## Education")[0]
    assert "- **title** — fact bank: Data Scientist (MSc dissertation) | LinkedIn: Data Scientist" in exp
    assert "- **org** — fact bank: Northwind Research Lab, University of Northfield | LinkedIn: Northwind Research Lab" in exp
    assert "- **dates** — fact bank: Oct 2025 – Dec 2025 | LinkedIn: Sep 2025 – Dec 2025" in exp            # an org's start month
    assert "- **employment_type** — fact bank: (none) | LinkedIn: internship" in exp
    assert "- **employment_type** — fact bank: (none) | LinkedIn: freelance" in exp
    harbour = exp.split("### Harbour Pay Ltd")[1]
    assert "LinkedIn: Jan 2023 – Sep 2024 (Software Development Engineer: Jun 2023 – Sep 2024; " in harbour   # two roles, one span
    assert "Software Development Engineer / Software Development Engineer Trainee" in harbour
    assert "### University of Northfield, Department of Statistics" in exp and "**not on LinkedIn**" in exp


def test_ltd_and_uk_are_not_mismatches(imported):
    exp = (imported[3] / "diff.md").read_text(encoding="utf-8").split("## Experience")[1].split("## Education")[0]
    harbour = exp.split("### Harbour Pay Ltd")[1].split("###")[0]
    assert "**org**" not in harbour                                  # "Harbour Pay Ltd" vs "HARBOUR PAY"
    northwind = exp.split("### Northwind")[1].split("###")[0]
    assert "**location**" not in northwind                           # "Bristol, UK" vs "Bristol, England, United Kingdom"


def test_the_diff_lists_education_modules_dates_and_grade(imported):
    edu = (imported[3] / "diff.md").read_text(encoding="utf-8").split("## Education")[1].split("## Projects")[0]
    assert "- **dates** — fact bank: 2025 – 2026 | LinkedIn: Sep 2025 – Sep 2026" in edu
    assert "- **modules** — fact bank: (none) | LinkedIn: Large-Scale Data Engineering, Statistical Computing" in edu
    assert "- **dates** — fact bank: Sep 2019 – Apr 2023 | LinkedIn: Aug 2019 – May 2023" in edu
    assert "- **grade** — fact bank: First Class with Distinction | LinkedIn: 91 %" in edu


def test_what_the_pdf_cannot_show_is_unverified_not_a_mismatch(imported):
    res, _, _, tmp = imported
    diff = (tmp / "diff.md").read_text(encoding="utf-8")
    assert res["unverified"] > 0 and "(unverified)" in diff and "## Warnings" in diff
    assert "- **the fact bank has no `publications`**" in diff and "- **the fact bank has no `languages`**" in diff
    assert "- **the fact bank has no `research_focus`**" in diff


def test_the_proposal_has_the_fact_bank_schema_and_reuses_matching_ids(imported):
    _, _, _, tmp = imported
    prop = yaml.safe_load((tmp / "proposed.yaml").read_text(encoding="utf-8"))
    assert list(prop) == ["experience", "education", "projects", "certifications", "publications", "languages", "research_focus"]
    assert [e["id"] for e in prop["experience"]][:3] == ["az", "dc", "fl"]           # matched entries keep the bank's ids
    first = prop["experience"][0]
    assert {"id", "title", "org", "dates", "location", "employment_type", "bullets"} <= set(first)
    assert first["employment_type"] == "indirect contract"
    assert all(set(b) == {"id", "text", "terms"} and b["terms"] == [] for e in prop["experience"] for b in e["bullets"])
    assert len({e["id"] for e in prop["experience"]}) == len(prop["experience"])      # ids stay unique (two Harbour Pay roles)
    assert prop["education"][0]["modules"][0] == "Large-Scale Data Engineering"
    assert prop["publications"][0]["venue"] == "icees2023 EEE SSN"
    assert prop["languages"][0] == {"name": "English", "level": "Professional working proficiency"}
    assert prop["certifications"][0] == "Model Context Protocol: Advanced Topics (Anthropic), Jun 2026"
    assert (tmp / "proposed.yaml").read_text(encoding="utf-8").startswith("# PROPOSED")


# ------------------------------------------------------------------------------------------------------------ the CLI
def test_the_command_writes_both_files_and_never_touches_the_fact_bank(tmp_path, monkeypatch, capsys):
    bank = tmp_path / "fact_bank.yaml"
    shutil.copy(BANK, bank)
    before = _digest(bank)
    pdf = tmp_path / "linkedin.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")                         # the text layer is patched in: no pdftotext needed
    monkeypatch.setattr(L, "extract_text", lambda p: TEXT)
    cli.main(["import-linkedin", str(pdf), "--fact-bank", str(bank), "--out", str(tmp_path / "p.yaml"),
              "--diff", str(tmp_path / "d.md")])
    out = capsys.readouterr().out
    assert "5 experience, 2 education, 2 projects, 2 certifications, 1 publications, 2 languages" in out
    assert "was not modified: merge by hand." in out and "! LinkedIn lists 10 items under 'Projects'" in out
    assert (tmp_path / "p.yaml").is_file() and (tmp_path / "d.md").is_file()
    assert _digest(bank) == before


def test_a_missing_pdf_is_an_error_not_a_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["import-linkedin", str(tmp_path / "nope.pdf"), "--out", str(tmp_path / "p.yaml"), "--diff", str(tmp_path / "d.md")])
    assert e.value.code == 1 and "not found" in capsys.readouterr().err


def test_the_two_new_paths_are_settings_and_the_reference_folder_is_git_ignored(settings):
    assert settings["paths"]["linkedin_diff"] == "data/linkedin_diff.md"
    proposed = settings["paths"]["fact_bank_proposed"]                                # the shipped value, not a path a test opens
    assert proposed.endswith("/fact_bank.proposed.yaml")
    assert settings.path("fact_bank_proposed") not in (settings.path("fact_bank"), settings.path("applicant"))   # never the real bank
    ignored = (Path(__file__).parent.parent.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config/reference/" in ignored and proposed in ignored
