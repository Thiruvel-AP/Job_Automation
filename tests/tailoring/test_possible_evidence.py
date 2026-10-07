"""Gap report: a JD term with a neighbour term in the fact bank is `possible evidence — you decide`, never a genuine gap and
never counted or claimed."""
import pytest
from careerops.config import load_yaml
from careerops.review import workflow as review
from careerops.tailoring import tailor
from careerops.tailoring.families import load_families, possible_evidence
from careerops.tailoring.scorer import Keyword, ceiling
from careerops.tailoring.tailor import fact_text

FB = {"profile_facts": ["Data scientist with a statistics background."],
      "skills": {"ML & Stats": ["hypothesis testing", "ANOVA", "experimental design", "statistics"],
                 "Languages": ["Python", "SQL"]},
      "experience": [{"id": "e1", "org": "Acme", "title": "Analyst", "bullets": [
          {"id": "b1", "text": "Ran bootstrap tests on conversion data.", "terms": ["experimental design", "sql"]}]}],
      "projects": []}


def test_the_example_pairs_are_families():
    fams = [set(f) for f in load_families()]
    assert any({"a/b experiments", "hypothesis testing"} <= f for f in fams)
    assert any({"quantitative analysis", "statistics"} <= f for f in fams)


def test_ab_experiments_and_quantitative_analysis_are_possible_evidence_with_where_it_is():
    got = possible_evidence(FB, ["A/B experiments", "quantitative analysis", "Looker"])
    assert set(got) == {"A/B experiments", "quantitative analysis"}                 # Looker has no neighbour: a genuine gap
    ab = {v["via"]: v["where"] for v in got["A/B experiments"]}
    assert "hypothesis testing" in ab and "skills: ML & Stats" in ab["hypothesis testing"]
    assert "bullet b1" in ab["experimental design"]
    assert any(v["via"] == "statistics" for v in got["quantitative analysis"])


def test_a_family_member_the_bank_does_not_have_is_not_evidence():
    assert possible_evidence({"skills": {"x": ["Python"]}, "experience": [], "projects": []}, ["A/B experiments"]) == {}


def test_a_term_is_matched_to_a_family_by_stem_and_through_its_alternatives():
    assert "A/B experiment" in possible_evidence(FB, ["A/B experiment"])           # singular: same stem
    got = possible_evidence(FB, ["experiments"], {"experiments": ["controlled experiments"]})
    assert "experiments" in got


def test_possible_evidence_is_never_counted_in_the_ceiling():
    kws = [Keyword("A/B experiments", 2.0, True), Keyword("Python", 2.0, True)]
    ceil, gaps = ceiling(fact_text(FB), kws)
    assert ceil == 0.5 and gaps == ["A/B experiments"]             # the ceiling still treats it as not evidenced


@pytest.mark.slow
def test_the_report_moves_the_term_out_of_the_genuine_gaps_and_the_review_screen_says_you_decide(settings, spy_llm, job, db):
    fb = load_yaml(settings.path("fact_bank"))
    jd = "You have experience running A/B experiments and quantitative analysis. Looker is a plus.\n\n" + job["description"]
    j = {**job, "description": jd}
    settings.raw["scoring"]["max_keywords"] = 60                  # no cap in this test: Looker must not be cut
    llm = spy_llm({"extract_keywords": lambda ctx: {"required": ["A/B experiments", "quantitative analysis"],
                                                    "preferred": ["Looker"], "company_hook": "", "team": ""}})
    rep = tailor.tailor_job(j, fb, llm, settings, settings.path("outputs"))
    assert {"A/B experiments", "quantitative analysis"} <= set(rep["possible_evidence"])
    assert not {"A/B experiments", "quantitative analysis"} & set(rep["genuine_gaps"]) and "looker" in {g.lower() for g in rep["genuine_gaps"]}
    db.upsert_job(j)
    db.update_job(j["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer")
    text = review.summary(db.job(j["id"]), rep)
    assert "Possible evidence — you decide" in text and "A/B experiments <-" in text
    assert "looker" in text.split("Genuine gaps")[1].lower() and "A/B experiments" not in text.split("Genuine gaps")[1]
    # nothing was claimed: the CV does not contain the words, and the letter's gap list never offers them
    assert "I have not used A/B" not in " ".join(rep["letter"]["paragraphs"])
