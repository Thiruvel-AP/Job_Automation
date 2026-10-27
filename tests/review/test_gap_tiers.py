"""E2: requirement importance tiers in the gap report. Each missing JD keyword is `stated` (must-have wording), `structural` (in the Requirements section, or
repeated) or `inferred` (our weight guess). The rule: an inferred keyword is never ranked first and never shown as the top priority."""
import itertools

import pytest

from careerops import bands
from careerops.config import load_yaml
from careerops.review import gaps, tiers
from careerops.review import workflow as review
from careerops.tailoring.scorer import Keyword

from conftest import PROFILE_FIX
from test_gap_report import report
from test_gap_review import world  # noqa: F401  (the fixture: one job in the review band)

JD = """About the role
You must have production experience with LangChain.
We run everything as code. We use Terraform across the platform. Terraform modules are reviewed weekly.

Requirements:
- Strong Python
- Docker experience

Nice to have:
- Dagster
"""


def kw(term, alts=()):
    return Keyword(term, 2.0, True, list(alts))


def tier(jd, term, alts=()):
    return tiers.classify(jd, [kw(term, alts)])[term]


# ------------------------------------------------------------------ the three tiers
def test_must_have_wording_makes_a_keyword_stated():
    assert tier(JD, "LangChain") == "stated"


@pytest.mark.parametrize("sentence", [
    "Python is essential for this role.", "Candidates are required to know Python.", "You will need Python.", "Python is mandatory.",
    "A minimum of Python experience is expected.", "You'll need Python from day one.", "We require Python.",
])
def test_each_must_have_phrase_is_stated(sentence):
    assert tier("About us\n" + sentence, "Python") == "stated"


def test_a_keyword_in_the_requirements_section_is_structural():
    assert tier(JD, "Python") == "structural" and tier(JD, "Docker") == "structural"


def test_a_keyword_the_ad_repeats_is_structural_wherever_it_is():
    assert tier(JD, "Terraform") == "structural"


@pytest.mark.parametrize("heading", ["What you'll need", "Qualifications", "About you", "Essential skills", "REQUIREMENTS", "## Requirements",
                                     "What we're looking for:"])
def test_the_usual_requirements_headings_are_recognised(heading):
    assert tier(f"About us\nWe build things.\n\n{heading}\n- Python\n", "Python") == "structural"


def test_a_single_mention_under_nice_to_have_is_inferred():
    assert tier(JD, "Dagster") == "inferred"


def test_a_single_mention_in_the_about_text_is_inferred():
    assert tier("About us\nWe sometimes use Spark for batch jobs.\n", "Spark") == "inferred"


def test_a_keyword_the_ad_never_says_is_inferred():
    assert tier(JD, "Redshift") == "inferred"


def test_nice_to_have_wording_is_never_must_have():
    assert tier("About us\nIdeally you have Dagster; it is not essential.\n", "Dagster") == "inferred"
    assert tier("Nice to have:\nDagster is essential to our roadmap.\n", "Dagster") == "inferred"


def test_a_nice_to_have_keyword_repeated_is_structural():
    assert tier("Nice to have:\nDagster\nExperience with Dagster pipelines.\n", "Dagster") == "structural"


def test_the_ads_alternatives_count_as_the_ads_own_words():
    assert tier("About us\nYou must know PyTorch or TensorFlow.\n", "TensorFlow", alts=["PyTorch"]) == "stated"
    assert tier("About us\nYou must know PyTorch.\n", "TensorFlow") == "inferred"                    # a synonym is not what the ad said


def test_no_ad_makes_everything_inferred():
    assert tiers.classify("", [kw("Python"), kw("SQL")]) == {"Python": "inferred", "SQL": "inferred"}
    assert tiers.classify(None, [kw("Python")]) == {"Python": "inferred"}


def test_a_heading_is_not_a_mention():
    assert tier("Requirements\nWe like cheese.\n", "Requirements") == "inferred"


# ------------------------------------------------------------------ in the gap report
@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


# (term, weight, required, on the CV?, claimable?)
def kws(*rows):
    return [(t, w, True, False, False) for t, w in rows]


def test_every_row_carries_its_tier_and_the_report_explains_the_words(fb):
    g = gaps.build(report(fb, keywords=kws(("LangChain", 2), ("Python", 2), ("Dagster", 2), ("Redshift", 2))), fb, bands.Bands(), jd=JD)
    assert {r.term: r.tier for r in g.rows} == {"LangChain": "stated", "Python": "structural", "Dagster": "inferred", "Redshift": "inferred"}
    text = "\n".join(g.lines())
    assert "Importance: stated" in text and "inferred = our weight guess" in text
    assert "LangChain (required, w=2)  gap  [stated]" in text and "Redshift (required, w=2)  gap  [inferred]" in text


def test_an_inferred_keyword_is_never_ranked_first_however_big_its_gain(fb):
    rows = kws(("Dagster", 9), ("Redshift", 8), ("Python", 1), ("LangChain", 1))
    g = gaps.build(report(fb, keywords=rows), fb, bands.Bands(), jd=JD)
    assert [r.term for r in g.rows][:2] == ["LangChain", "Python"] or [r.term for r in g.rows][:2] == ["Python", "LangChain"]
    assert g.rows[0].tier != "inferred" and g.top is g.rows[0]
    assert [r.tier for r in g.rows] == ["stated", "structural", "inferred", "inferred"]


def test_inferred_never_first_over_every_mix_of_weights_and_tiers(fb):
    pool = {"LangChain": "stated", "Python": "structural", "Dagster": "inferred", "Redshift": "inferred"}
    for weights in itertools.product((1, 2, 7), repeat=4):
        rows = kws(*zip(pool, weights))
        g = gaps.build(report(fb, keywords=rows), fb, bands.Bands(), jd=JD)
        assert g.rows[0].tier != "inferred", weights
        assert [r.tier for r in g.rows] == sorted((r.tier for r in g.rows), key=tiers.TIERS.index)       # ranked stated, structural, inferred


def test_within_a_tier_the_largest_gain_comes_first(fb):
    g = gaps.build(report(fb, keywords=kws(("Dagster", 1), ("Redshift", 5), ("Spark", 3))), fb, bands.Bands(), jd="About us\nnothing here\n")
    assert [r.term for r in g.rows] == ["Redshift", "Spark", "Dagster"]


def test_when_every_missing_keyword_is_inferred_nothing_is_the_top_priority(fb):
    g = gaps.build(report(fb, keywords=kws(("Dagster", 5), ("Redshift", 1))), fb, bands.Bands(), jd=JD)
    assert g.top is None and "<- top priority" not in "\n".join(g.lines())


def test_without_the_ad_every_keyword_is_inferred_and_the_order_is_by_gain(fb):
    g = gaps.build(report(fb, keywords=kws(("Python", 1), ("Dagster", 5))), fb, bands.Bands())
    assert [r.term for r in g.rows] == ["Dagster", "Python"] and {r.tier for r in g.rows} == {"inferred"} and g.top is None


def test_only_the_first_row_is_marked_top_priority_and_only_when_it_is_not_inferred(fb):
    g = gaps.build(report(fb, keywords=kws(("LangChain", 1), ("Python", 1), ("Dagster", 9))), fb, bands.Bands(), jd=JD)
    marked = [l for l in g.lines() if "top priority" in l and "<-" in l]
    assert len(marked) == 1 and "LangChain" in marked[0]


def test_the_keywords_the_user_is_asked_about_come_in_importance_order(fb):
    g = gaps.build(report(fb, keywords=kws(("Dagster", 9), ("Redshift", 8), ("Python", 1), ("LangChain", 1))), fb, bands.Bands(), jd=JD)
    assert [r.term for r in g.askable(4)][:2] == ["LangChain", "Python"] or [r.term for r in g.askable(4)][:2] == ["Python", "LangChain"]


def test_claimable_rows_are_tiered_too(fb):
    rows = [("SQL", 2, True, False, True), ("Dagster", 2, True, False, False)]
    g = gaps.build(report(fb, keywords=rows), fb, bands.Bands(), jd="About us\nYou must have SQL.\n")
    assert {r.term: (r.kind, r.tier) for r in g.rows} == {"SQL": ("claimable", "stated"), "Dagster": ("gap", "inferred")}


# ------------------------------------------------------------------ the review screen passes the ad
def test_the_review_loop_gives_the_gap_report_the_jobs_ad(world, monkeypatch):
    seen = []
    real = review.gaps.build
    monkeypatch.setattr(review.gaps, "build", lambda rep, fb, b, jd=None: seen.append(jd) or real(rep, fb, b, jd=jd))
    world.run("q")
    assert seen and seen[0] == "d"                                         # the World job's description


def test_a_job_without_a_description_is_tiered_inferred_not_a_crash():
    assert review._jd({"id": "x"}) is None and review._jd({"description": "ad"}) == "ad"
