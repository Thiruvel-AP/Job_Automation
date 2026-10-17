"""Fix round 2, item 5: at render time (after every claim check) a hyphenated word-word becomes word word, except the protected forms.

The failing dry run's CV still had 15 hyphenated words: class-imbalance, cross-validation, scoring-methodology, 19,213-gene, MCP-based,
5-star, production-ready, bank-account, sign-on, e-commerce, bank-customer, financial-services, comparable-company, cell-line, per-gene.
Root cause: typography.clean() closed up only the forms listed one by one in config/typography.yaml, so every new compound stayed
hyphenated. It is now the general rule, with the exceptions below (typography.yaml `protected`, the JD's own hyphenated keywords, URLs,
handles, number ranges and names like CIFAR-10).
"""
from pathlib import Path

import pytest

from careerops.tailoring import typography as T

FOUND = ["class-imbalance", "cross-validation", "scoring-methodology", "19,213-gene", "MCP-based", "5-star", "production-ready",
         "bank-account", "sign-on", "e-commerce", "bank-customer", "financial-services", "comparable-company", "cell-line", "per-gene"]
WANT = {"19,213-gene": "19,213 gene", "5-star": "five star", "sign-on": "sign on", "e-commerce": "ecommerce"}


@pytest.mark.parametrize("word", FOUND)
def test_each_hyphenated_word_of_the_dry_run_is_closed_up(word):
    want = WANT.get(word, word.replace("-", " "))
    assert T.clean(f"Built a {word} pipeline.") == f"Built a {want} pipeline."


@pytest.mark.parametrize("kept", ["scikit-learn", "k-means", "gpt-oss", "faster-whisper", "CIFAR-10", "TF-IDF", "Skip-gram", "GPT-4o",
                                  "3.9–3.11", "3.9-3.11", "https://github.com/Thiruvel-AP/Repo-Name", "thiruvel-a-p", "Thiruvel-AP",
                                  "github.com/Thiruvel-AP", "linkedin.com/in/thiruvel-a-p"])
def test_the_exceptions_stay_as_they_are(kept):
    assert T.clean(f"See {kept} for details.") == f"See {kept} for details."


def test_a_jd_keyword_spelt_with_a_hyphen_is_left_alone():
    assert T.clean("Handles the cold-start problem.", keep=["cold-start"]) == "Handles the cold-start problem."
    assert T.clean("Handles the cold-start problem.") == "Handles the cold start problem."


def test_a_hyphen_between_numbers_is_a_range_not_a_word():
    assert T.clean("From 2025-2026 and 10-12 weeks.") == "From 2025-2026 and 10-12 weeks."


def test_several_hyphens_in_one_word_are_all_closed():
    assert T.clean("A state-of-the-art, hands-on, end-to-end review.") == "A state of the art, hands on, end to end review."


def test_the_audit_has_nothing_left_to_report_after_typeset():
    ctx = {"profile": "A cross-validation and class-imbalance specialist with scikit-learn.",
           "experience": [{"id": "e", "org": "Acme", "title": "Data Scientist", "dates": "Jan 2025 – Sep 2025",
                           "bullets": [f"Used {w} here." for w in FOUND], "bullet_ids": [f"b{i}" for i in range(len(FOUND))]}],
           "projects": [], "skills": {"ML": ["scikit-learn", "k-means", "CIFAR-10"]}, "id": {"name": "Ada", "email": "a@b.org"}}
    out = T.typeset(ctx)
    text = " ".join([out["profile"], *out["experience"][0]["bullets"], *out["skills"]["ML"]])
    assert T.audit(text)["hyphens"] == {}
    assert "scikit-learn" in text and "k-means" in text and "CIFAR-10" in text and "five star" in text and "ecommerce" in text


@pytest.mark.slow
def test_a_rendered_fixture_cv_has_no_hyphen_outside_the_exception_list(settings, fb_fixture, job, tmp_path):
    from careerops.llm import MockLLM
    from careerops.tailoring import tailor
    from careerops.tailoring.render import pdf_text

    fb = {**fb_fixture, "experience": [dict(e) for e in fb_fixture["experience"]]}
    first = dict(fb["experience"][0])
    first["bullets"] = [dict(first["bullets"][0], text="Resolved a class-imbalance and e-commerce sign-on issue across a 5-star, cross-validation "
                                                      "heavy, production-ready pipeline for the bank-customer team."),
                        *first["bullets"][1:]]
    fb["experience"][0] = first
    rep = tailor.tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    text = pdf_text(Path(rep["cv_pdf"]))
    assert "five star" in " ".join(text.split()) and "ecommerce" in text            # the bullet is on the CV, and it was typeset
    left =T.audit(text, keep=tailor.hyphen_keywords([tailor.Keyword(**k) for k in rep["keywords"]]))["hyphens"]
    assert left == {}, left
    assert rep["typography"]["hyphens"] == {}


def test_the_handles_of_the_cvs_own_links_are_never_closed_up_but_a_repository_name_is():
    ctx = {"id": {"name": "A", "links": {"linkedin": "https://www.linkedin.com/in/thiruvel-a-p/", "github": "https://github.com/Thiruvel-AP",
                                         "portfolio": "https://thiruvel-ap.github.io/Personal-Portfolio/"}},
           "profile": "See linkedin and Thiruvel-AP or thiruvel-a-p; a cross-validation expert.", "skills": {}, "experience": [], "projects": []}
    assert T.handles(ctx) == ("thiruvel-a-p", "Thiruvel-AP")
    assert T.typeset(ctx)["profile"] == "See linkedin and Thiruvel-AP or thiruvel-a-p; a cross validation expert."


def test_a_hyphenated_word_inside_a_longer_jd_keyword_is_kept_so_the_ats_still_finds_it():
    """The ad says "time-series forecasting": the literal ATS view looks for that spelling, so the hyphen of its token stays on the CV."""
    from careerops.tailoring import tailor

    keep = tailor.hyphen_keywords([tailor.Keyword("time-series forecasting", 2, True, ["time series"]), tailor.Keyword("Python", 2, True)])
    assert "time-series forecasting" in keep and "time-series" in keep and "Python" not in keep
    assert T.clean("Built time-series forecasting and class-imbalance fixes.", keep=keep) == "Built time-series forecasting and class imbalance fixes."
