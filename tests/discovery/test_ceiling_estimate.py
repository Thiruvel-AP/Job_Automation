"""The zero-token ceiling estimate (discovery/ceiling_estimate.py, v3.5.4): the ad's stated and structural terms (taxonomy terms plus the labels of its
requirement bullets) against the fact bank, weighted as the honest ceiling weighs them. It is triage's E and live's estimate F, and each tailoring logs it
next to the final ceiling so `calibrate --scores` reports the mean absolute error. No model, no network."""
import json

import pytest

from careerops.config import load_yaml
from careerops.discovery import ceiling_estimate as ce
from careerops.discovery import gates, priority, triage
from conftest import PROFILE_FIX

# The shape of the Iwoca ad (10 Nov 2026): its own phrases as bullet labels, two generic terms the bank has. The old taxonomy-only scan read 100%.
PHRASE_AD = """About the company
We lend to small businesses. Our team uses statistics and Python every day.

The requirements
Essential:
 - Applied statistics. You have a background in statistics from a quantitative field.
 - Probabilistic reasoning. You quantify the uncertainty in each estimate.
 - Analytical ownership. You take ambiguous problems to a decision.
 - Research mindset. You have tested new modelling ideas of your own.
 - Communication. You write and speak clearly.

Bonus:
 - Bayesian methods. You have used hierarchical models in real work.
 - Coding. The team works in Python.

The salary
We pay well. Communication matters at every level.
"""


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb):
    gates.load_roles(settings, fb)


def old_taxonomy_only(jd, fb, settings):
    from careerops.tailoring.scorer import ceiling, taxonomy_keywords
    from careerops.tailoring.tailor import fact_text
    sc = settings["scoring"]
    return ceiling(fact_text(fb), taxonomy_keywords(jd, sc["required_weight"], sc["preferred_weight"]))[0]


def test_the_requirement_bullet_labels_become_terms_and_a_section_word_does_not(settings):
    terms = {k.term: (k.weight, k.required) for k in ce.label_keywords(PHRASE_AD)}
    assert terms["Applied statistics"] == (2.0, True) and terms["Analytical ownership"] == (2.0, True)
    assert terms["Bayesian methods"] == (1.0, False)                       # under Bonus: a nice-to-have weight
    assert "Essential" not in terms and "The salary" not in terms


def test_a_label_outside_a_requirements_section_is_not_a_term():
    ad = "Live examples of the work:\n - Causal estimation. Estimating the causal effect of price.\n"
    assert ce.label_keywords(ad) == []


def test_the_phrase_ad_no_longer_reads_as_a_full_match(settings, fb):
    old = old_taxonomy_only(PHRASE_AD, fb, settings)
    est = ce.estimate(PHRASE_AD, fb, settings)
    assert est.ceiling < old and est.ceiling < 0.7, (old, est.ceiling)
    assert "Probabilistic reasoning" in est.lack


def test_an_inferred_term_is_dropped_while_a_stated_or_structural_one_is_left(settings):
    terms = [k.term for k in ce.keywords(PHRASE_AD, settings)]
    assert "Bayesian methods" not in terms                                 # a single mention under Bonus: inferred
    assert "Applied statistics" in terms


def test_e_and_the_estimate_f_are_the_same_number(settings, fb):
    est = ce.estimate(PHRASE_AD, fb, settings).ceiling
    e, have, lack = triage.evidence(PHRASE_AD, fb, settings)
    assert e == pytest.approx(1 + 4 * est) and gates.pre_ceiling(PHRASE_AD, fb, settings) == pytest.approx(est)
    assert "Probabilistic reasoning" in lack


def test_an_ad_with_no_term_keeps_the_neutral_e(settings, fb):
    assert triage.evidence("We are a friendly team.", fb, settings) == (triage.NO_REQUIRED, [], [])


def test_the_log_keeps_estimate_vs_final_and_calibrate_reports_the_error(settings, fb):
    jobs = [{"id": "a", "company": "Iwoca", "title": "DS", "description": PHRASE_AD}, {"id": "b", "company": "B", "title": "DS", "description": PHRASE_AD}]
    est = ce.estimate(PHRASE_AD, fb, settings).ceiling
    ce.log(settings, jobs[0], fb, 0.387, 0.387)
    ce.log(settings, jobs[1], fb, est, est - 0.1)
    rows = [json.loads(x) for x in ce.log_path(settings).read_text(encoding="utf-8").splitlines()]
    assert [r["job_id"] for r in rows] == ["a", "b"] and rows[0]["final"] == 0.387
    text = "\n".join(ce.report(settings))
    assert f"mean absolute error {100 * abs(est - 0.387) / 2:.1f} points" in text
    assert "Iwoca | DS: estimate" in text


def test_the_log_never_raises(settings, fb):
    assert ce.log(settings, {"id": "x"}, fb, 0.5, 0.5) is None              # no description key: nothing written, no error
    assert ce.report(settings)[0].startswith("Ceiling estimate: no tailored job logged yet")


def test_recording_a_tailoring_logs_the_estimate(settings, db):
    from careerops import cli

    db.upsert_job({"id": "manual:iw", "source": "manual", "company": "Iwoca", "title": "Data Scientist", "description": PHRASE_AD})
    rep = {"final": {"score": 0.387, "score_stemmed": 0.4, "band": "ok"}, "letter": {"status": "ok", "score": 0.5}, "ceiling": 0.387, "keywords": [],
           "attempts": [], "out_dir": "x", "cv_pages": 1, "page2_fill": None}
    cli.record_tailoring(db, settings, db.job("manual:iw"), rep)
    row = ce.read(settings)[-1]
    assert row["job_id"] == "manual:iw" and row["final"] == 0.387 and row["strict"] == 0.387 and 0 <= row["estimate"] <= 1


def test_a_paragraph_pasted_twice_does_not_make_its_terms_structural(settings):
    blurb = "We value careful testing and honest research. "
    ad = f"About us\n{blurb * 2}\n\nRequirements:\n- Python\n"
    assert [k.term for k in ce.keywords(ad, settings)] == ["python"]
