"""CV typography, applied at render time (typography.py, config/typography.yaml): no em dash, hyphens closed up only where the
rewrite table says so, protected terms / URLs / e-mails never touched, an award never repeated inside its project's bullet, the
bullet's `result:` as the only bold in a bullet, italics for the markers, and the date separator setting. No xelatex here (the
snapshot tests that read the PDF are in test_cv_typography.py), no network, no model."""
import copy

import pytest

from careerops.tailoring import typography as T
from careerops.tailoring.render import (bullet_latex, cert_tex, dates_display, degree_tex, env, inline, org_tex, publication_tex,
                                         result_span, role_tex, school_tex, skill_row)


# ------------------------------------------------------------------------------------------------------------ em dashes
@pytest.mark.parametrize("raw,clean", [
    # a pair around an aside: commas, or parentheses when the aside has a comma of its own
    ("Built an agent — on AWS — for the team", "Built an agent, on AWS, for the team"),
    ("Built an agent — with tools, caches and guards — for the team", "Built an agent (with tools, caches and guards) for the team"),
    # one dash: a colon after a name or title, a comma after a clause
    ("Bitcoin Forecasting — LSTM time series service", "Bitcoin Forecasting: LSTM time series service"),
    ("Lloyd's Banking Group Datathon — 3rd place", "Lloyd's Banking Group Datathon: 3rd place"),
    ("Built a retrieval agent over 14 datasets with a guard layer — with three tools for lookup",
     "Built a retrieval agent over 14 datasets with a guard layer, with three tools for lookup"),
    ("Cut RAM — saving 40% of memory", "Cut RAM, saving 40% of memory"),
    # "--", "---", a dash without spaces and a spaced hyphen are the same dash
    ("Built the API -- and the docs", "Built the API, and the docs"),
    ("GTAI Finder—multi omics graph", "GTAI Finder: multi omics graph"),
    ("Right to work - Graduate visa", "Right to work, Graduate visa"),
    ("Built an agent \u2014 fast", "Built an agent, fast"), ("Mining Insights from Customer Feedback \u2014 NLP analytics",
                                                           "Mining Insights from Customer Feedback: NLP analytics"),
    # a dash at the edge or after punctuation is just dropped; a number range keeps an en dash
    ("Trained models —", "Trained models"), ("Trained, — tested", "Trained, tested"),
    ("Python 3.9—3.11 and 3.9--3.12", "Python 3.9–3.11 and 3.9–3.12"),
    ("Nothing to change here.", "Nothing to change here."),
])
def test_an_em_dash_becomes_a_comma_a_colon_or_parentheses(raw, clean):
    assert T.clean(raw) == clean
    assert "—" not in T.clean(raw) and "--" not in T.clean(raw)


def test_a_dash_in_a_name_is_always_a_colon():
    assert T.clean("Price forecasting \u2014 LSTM time-series service", colon=True) == "Price forecasting: LSTM time series service"
    assert T.clean("Price forecasting \u2014 LSTM time-series service") == "Price forecasting, LSTM time series service"
    assert T.typeset({"projects": [{"name": "Price forecasting \u2014 LSTM service", "bullets": [], "bullet_ids": []}]}
                     )["projects"][0]["name"] == "Price forecasting: LSTM service"


def test_a_spaced_hyphen_between_numbers_or_months_is_not_a_dash():
    assert T.clean("Jan 2025 - Aug 2025") == "Jan 2025 - Aug 2025"          # dates have their own filter; this text is left alone
    assert T.clean("pages 3 - 5") == "pages 3 - 5"


# ------------------------------------------------------------------------------------------------------- hyphen rewriting
@pytest.mark.parametrize("raw,clean", [
    ("AI-agent", "AI agent"), ("test-case", "test case"), ("rate-limit", "rate limit"), ("tool-calling", "tool calling"),
    ("Madrid-based", "Madrid based"), ("start-up", "startup"), ("day-to-day", "daily"), ("end-to-end", "end to end"),
    ("multi-omics", "multiomics"), ("three-tier", "three tier"), ("hallucination-guard", "hallucination guard"),
    ("A tool-calling AI-agent with day-to-day use", "A tool calling AI agent with daily use"),
    ("Three AI-agents and two test-cases", "Three AI agents and two test cases"),               # a plural s stays
    ("Start-up founders", "Startup founders"), ("TOOL-CALLING", "TOOL CALLING"),                  # the match was capitalised
    ("a non-listed-word goes", "a non listed word goes"),                                         # fix round 2: every compound closes
])
def test_the_rewrite_table_closes_up_ordinary_compounds(raw, clean):
    assert T.clean(raw) == clean


@pytest.mark.parametrize("text", [
    "scikit-learn and k-means with TF-IDF", "gpt-oss and faster-whisper and Skip-gram", "GPT-OSS-20B", "SCIKIT-LEARN", "k-NN",
    "see https://github.com/ada-lovelace-test/tool-calling-agent and www.example.org/end-to-end",
    "write to ada-lovelace@end-to-end.example.org", "tool-calling-agent.example.org"])
def test_protected_terms_urls_and_emails_are_never_touched(text):
    assert T.clean(text) == text


def test_a_protected_term_beats_the_table_even_inside_a_table_phrase(tmp_path):
    rules = tmp_path / "typography.yaml"
    rules.write_text("rewrite: {tool-calling: tool calling, k-means: k means}\nprotected: [k-means, tool-calling]\n", encoding="utf-8")
    assert T.clean("tool-calling and k-means", T.load_rules(rules)) == "tool-calling and k-means"


def test_a_hyphenated_jd_keyword_keeps_its_hyphen_so_the_ats_still_matches_it():
    assert T.clean("an end-to-end tool-calling pipeline", keep=["end-to-end"]) == "an end-to-end tool calling pipeline"
    assert T.clean("an end-to-end pipeline", keep=["End-To-End"]) == "an end-to-end pipeline"       # case does not matter


def test_the_rules_come_from_config_typography_yaml():
    rules = T.load_rules()
    table = dict(rules.rewrite)
    assert table["AI-agent"] == "AI agent" and table["day-to-day"] == "daily" and table["multi-omics"] == "multiomics"
    assert {"scikit-learn", "k-means", "TF-IDF", "gpt-oss", "faster-whisper", "Skip-gram"} <= set(rules.protected)


# -------------------------------------------------------------------------------------------------------------- awards
@pytest.mark.parametrize("text,award,out", [
    ("3rd place; 200:1 class imbalance", "3rd place", "200:1 class imbalance"),
    ("200:1 class imbalance; 3rd place", "3rd place", "200:1 class imbalance"),
    ("Built it, 3rd place, and shipped it", "3rd place", "Built it, and shipped it"),
    ("Built it and took 3rd place.", "3rd place", "Built it and took."),
    ("Built it.", "3rd place", "Built it."), ("Built it.", "", "Built it."),
])
def test_an_award_is_taken_out_of_the_bullet(text, award, out):
    assert T.strip_award(text, award) == out


def test_award_repeats_sees_the_phrase_and_the_placing():
    assert T.award_repeats("Won 3rd place in the final", "3rd place") and T.award_repeats("finished 3rd overall", "3rd place")
    assert not T.award_repeats("Detected outliers at 200:1", "3rd place") and not T.award_repeats("Anything", "")


# -------------------------------------------------------------------------------------------------------- certifications
def test_a_certification_splits_into_name_issuer_and_date():
    assert T.parse_cert("Introduction to Model Context Protocol — Anthropic, Jun 2026") == {
        "name": "Introduction to Model Context Protocol", "issuer": "Anthropic", "rest": ", Jun 2026"}
    assert T.parse_cert("AWS Academy Cloud Foundations, Nov 2025") == {
        "name": "AWS Academy Cloud Foundations, Nov 2025", "issuer": "", "rest": ""}      # no issuer to italicise
    assert T.parse_cert({"name": "Cloud Practitioner", "issuer": "AWS", "date": "May 2025"}) == {
        "name": "Cloud Practitioner", "issuer": "AWS", "rest": ", May 2025"}
    assert T.cert_text("Course — Anthropic, Jun 2026") == "Course, Anthropic, Jun 2026"
    assert cert_tex("Course — Anthropic, Jun 2026") == r"Course, Anthropic, \textit{Jun 2026}"      # the date is italic, the issuer plain


# ------------------------------------------------------------------------------------------------------------- typeset
def _ctx():
    return {"id": {"name": "A B", "email": "a@b-c.example.org", "location": "Bristol — UK",
                   "links": {"github": "https://github.com/a/tool-calling"}, "status_line": ["Start-up friendly"]},
            "profile": "Data Scientist — AI-agent work.",
            "skills": {"Core": ["Python", "tool-calling"]},
            "experience": [{"id": "az", "org": "Acme", "title": "Day-to-day engineer", "dates": "May 2026 — Sep 2026",
                            "bullet_ids": ["az1"], "bullets": ["Built an AI-agent — fast, with scikit-learn."]}],
            "projects": [{"id": "ll", "name": "Datathon — 3rd place", "award": "3rd place", "stack": "scikit-learn, multi-omics",
                          "github": "https://github.com/a/b-c", "bullet_ids": ["ll1"],
                          "bullets": ["Detected outliers under 200:1 imbalance; 3rd place"]}],
            "education": [{"degree": "MSc", "org": "Uni", "dates": "2025 – 2026"}],
            "certifications": ["Course — Anthropic, Jun 2026"],
            "results": {"az1": "AI-agent", "ll1": "3rd place; 200:1 imbalance"}, "linespread": 1.02}


def test_typeset_cleans_a_copy_and_leaves_the_original_for_the_checks():
    ctx = _ctx()
    before = copy.deepcopy(ctx)
    out = T.typeset(ctx)
    assert ctx == before                                                       # the context kept in report.json is untouched
    assert out["profile"] == "Data Scientist: AI agent work."
    assert out["experience"][0]["bullets"] == ["Built an AI agent, fast, with scikit-learn."]
    assert out["experience"][0]["title"] == "Daily engineer" and out["id"]["status_line"] == ["Startup friendly"]
    assert out["id"]["location"] == "Bristol: UK"
    assert out["skills"]["Core"] == ["Python", "tool calling"]
    assert out["projects"][0]["stack"] == "scikit-learn, multiomics" and out["projects"][0]["name"] == "Datathon: 3rd place"
    assert out["certifications"] == [{"name": "Course", "issuer": "Anthropic", "rest": ", Jun 2026"}]


def test_typeset_leaves_links_email_dates_ids_and_numbers_alone():
    out = T.typeset(_ctx())
    assert out["id"]["email"] == "a@b-c.example.org" and out["id"]["links"]["github"] == "https://github.com/a/tool-calling"
    assert out["projects"][0]["github"] == "https://github.com/a/b-c" and out["linespread"] == 1.02
    assert out["experience"][0]["dates"] == "May 2026 — Sep 2026"         # the dates filter prints these
    assert out["experience"][0]["id"] == "az" and out["experience"][0]["bullet_ids"] == ["az1"]


def test_typeset_takes_the_award_out_of_a_project_bullet_and_its_result():
    out = T.typeset(_ctx())
    assert out["projects"][0]["bullets"] == ["Detected outliers under 200:1 imbalance"]
    assert out["results"]["ll1"] == "200:1 imbalance" and out["results"]["az1"] == "AI agent"


def test_the_audit_reports_what_the_pdf_text_still_has():
    text = ("Built the AI agent — fast. A prose – dash here. May 2026 – Sep 2026. Python 3.9–3.11.\n"
            "scikit-learn, pre-trained models, pre-trained again, end-to-end, https://x.org/a-b, me@a-b.org, 2025 – Present")
    a = T.audit(text, keep=["end-to-end"])
    assert a["em_dashes"] == 1
    assert len(a["prose_en_dashes"]) == 1 and "prose – dash" in a["prose_en_dashes"][0]       # a date range is not one
    assert a["hyphens"] == {"pre-trained": 2} and a["kept"] == ["end-to-end"]                    # protected / URL / e-mail not listed
    assert T.audit("Nothing here.")["hyphens"] == {} and T.audit("a -- b")["em_dashes"] == 1


# ------------------------------------------------------------------------------------------------------ date separator
@pytest.mark.parametrize("raw,sep,shown", [
    ("May 2026 - Sep 2026", " – ", "May 2026 – Sep 2026"), ("May 2026 - Sep 2026", " to ", "May 2026 to Sep 2026"),
    ("September 2025 to Present", " to ", "Sep 2025 to Present"), ("Jun 2022 — Sep 2022", " – ", "Jun 2022 – Sep 2022"),
    ("2025 – 2026", " to ", "2025 to 2026")])
def test_the_date_separator_is_a_setting(raw, sep, shown):
    assert dates_display(raw, sep) == shown
    assert env(sep).filters["dates"](raw) == shown


def test_the_default_date_separator_is_the_spaced_en_dash():
    from careerops.config import load_settings
    from careerops.tailoring.tailor import date_separator

    assert load_settings()["cv"]["date_separator"] == " – " and date_separator(load_settings()) == " – "
    assert dates_display("May 2026 - Sep 2026") == "May 2026 – Sep 2026"

    class S:
        raw = {"cv": {"date_separator": "to"}}
    assert date_separator(S()) == " to " and date_separator(type("N", (), {"raw": {}})()) == " – "


# --------------------------------------------------------------------------------------- bold, italics and the type scale
def test_only_the_results_text_is_bold_inside_a_bullet():
    text = "Wrote a three-tier test framework of ~65 tests across 8 files, validated across 14 datasets and 200:1 odds."
    s = bullet_latex(text, "~65 tests across 8 files")
    assert s.count(r"\textbf{") == 1 and r"\textbf{\textasciitilde{}65 tests across 8 files}" in s       # not 14, not 200:1
    assert bullet_latex(text, "") == bullet_latex(text) and r"\textbf" not in bullet_latex(text)         # no result: no bold at all
    assert r"\textbf" not in bullet_latex(text, "a result the bullet does not say")
    assert bullet_latex("Cut 50% of 100 & more_x", "50%") == r"Cut \textbf{50\%} of 100 \& more\_x"       # still escaped


def test_a_result_the_bullet_words_with_a_few_extra_words_is_still_found():
    text = "Surveyed 14 heterogeneous source datasets (gene expression, gene properties), owning the identity spine."
    start, end = result_span(text, "14 source datasets")
    assert text[start:end] == "14 heterogeneous source datasets"                           # at most two extra words between
    assert result_span(text, "14 source datasets from three labs") is None
    assert result_span("Reached 0.822 modularity.", "0.822 modularity")  == (8, 25 - 1)
    assert result_span("Reached 0.822 modularity.", "") is None


def test_the_italic_markers():
    assert inline("Distinction (Expected)") == r"Distinction \textit{(Expected)}"
    assert inline("Databricks/PySpark (coursework)") == r"Databricks/PySpark \textit{(coursework)}"
    assert inline("Pipeline on AWS (MSc coursework)") == r"Pipeline on AWS \textit{(MSc coursework)}"
    assert inline("Distinction (Expected Sep 2026)") == r"Distinction \textit{(Expected Sep 2026)}"
    assert inline("Plain (not a marker) text") == "Plain (not a marker) text"


def test_employer_bold_location_title_degree_venue_issuer_italic():
    exp = {"org": "AstraZeneca", "location": "Bristol, UK (Remote)", "title": "Data Scientist"}
    assert org_tex(exp) == r"\textbf{AstraZeneca}, \textit{Bristol, UK (Remote)}" and role_tex(exp) == r"\textit{Data Scientist}"
    assert org_tex({"org": "Acme"}) == r"\textbf{Acme}"
    assert org_tex({"org": "Self-employed, Chennai", "title": "ML Engineer"}) == r"\textbf{Freelance}, \textit{Chennai}"
    assert school_tex({"org": "University of Bristol", "location": "Clifton"}) == r"\textbf{University of Bristol}, \textit{Clifton}"
    assert school_tex({"org": "University of Bristol", "location": "Bristol"}) == r"\textbf{University of Bristol}"       # not said twice
    assert degree_tex({"degree": "MSc Data Science", "note": "Distinction (Expected)", "modules": ["A", "B"]}) == \
        r"\textit{MSc Data Science} (A, B); Distinction \textit{(Expected)}"
    assert degree_tex({"degree": "B.E. EEE", "grade": "91%"}) == r"\textit{B.E. EEE}, 91\%"
    assert publication_tex({"title": "A study.", "venue": "ICEES 2023", "date": "Mar 2023"}) == \
        r"A study. \textit{ICEES 2023}, Mar 2023"
    assert publication_tex("Plain string") == "Plain string"


def test_the_first_skill_of_a_row_is_capitalised_and_the_rest_are_as_written():
    assert skill_row(["pandas", "NumPy", "scikit-learn"]) == "Pandas, NumPy, scikit-learn"
    assert skill_row(["Python", "SQL"]) == "Python, SQL" and skill_row([]) == ""
    assert skill_row(["S3, SQS (MSc coursework)", "x"]) == r"S3/SQS \textit{(MSc coursework)}, x"
