"""The zero-token ceiling estimate (discovery/ceiling_estimate.py, v3.5.4, calibrated v3.5.5): the terms the ad states (taxonomy terms plus the labels of its
requirement bullets) against the CV-able fact bank, claimable as ats.py counts them, plus the terms the model is expected to add to the final keyword list. It is
triage's E and live's estimate F, and each tailoring logs it next to the final ceiling so `calibrate --scores` reports the error. No model, no network."""
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


def test_every_term_is_kept_as_the_final_keyword_list_keeps_it(settings):
    terms = [k.term for k in ce.keywords(PHRASE_AD, settings)]
    assert "Bayesian methods" in terms and "Applied statistics" in terms     # v3.5.5: the final ceiling counts a single "Bonus" mention too; the old tier filter is gone


def test_triage_still_reads_only_the_stated_and_structural_terms(settings, fb):
    est = ce.estimate(PHRASE_AD, fb, settings)
    assert "Bayesian methods" not in est.stated and "Applied statistics" in est.stated          # a single mention under Bonus: inferred, so left out of E and the lists
    assert "Bayesian methods" in [k.term for k in est.keywords]                                   # but F counts it, as the final keyword list does
    assert 0 < est.stated_share <= 1


def test_e_reads_the_stated_terms_and_the_estimate_f_adds_the_expected_ones(settings, fb):
    est = ce.estimate(PHRASE_AD, fb, settings)
    e, have, lack = triage.evidence(PHRASE_AD, fb, settings)
    assert e == pytest.approx(1 + 4 * est.stated_share) and gates.pre_ceiling(PHRASE_AD, fb, settings) == pytest.approx(est.ceiling)
    assert est.ceiling < est.stated_share                      # v3.5.5: F carries the terms the model adds (the bank backs few): E stays on the scale T was set on
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


# ------------------------------------------------------------------ v3.5.5: the calibrated estimate
THREE = "Requirements:\n- Python\n- SQL\n- scikit-learn\n"
# the shape of Trainline's Senior Data Scientist (11 Nov 2026): "About us" never closed by an all-caps / emoji heading, so its terms were read as company blurb
TRAINLINE_SHAPE = """About us

We help millions of travellers find the best value tickets across carriers and fares.

INTRODUCING DATA SCIENCE & ANALYTICS AT OUR COMPANY \U0001F44B

Data scientists are embedded alongside analysts in cross-functional teams.

WE'D LOVE TO HEAR FROM YOU IF YOU HAVE... \U0001F50D

 - Experience running A/B tests to evaluate product effectiveness.

 - Tech Stack: SQL, Python, R, Tableau, AWS Athena + More!

More information:

Enjoy fantastic perks like private healthcare & dental insurance.
"""


def test_three_backed_terms_no_longer_read_as_a_full_match(settings, fb):
    e = ce.estimate(THREE, fb, settings, terms=34, coverage=0.3)
    assert e.known == 3 and e.known_weight == 6.0 and e.other_weight == (34 - 3) * 2.0
    assert e.ceiling == pytest.approx((e.backed_weight + 0.3 * e.other_weight) / (e.known_weight + e.other_weight))
    assert e.ceiling < 0.5                                                  # the old reading said 1.0 (Scale AI: 100% vs a final 42%)
    # nothing expected beyond the terms read = the old reading: the share of the read terms the bank backs
    assert ce.estimate(THREE, fb, settings, terms=3).ceiling == pytest.approx(e.backed_weight / e.known_weight)


def test_the_calibration_is_in_the_settings_with_the_agreed_defaults(settings):
    assert ce.params(settings) == (34, 0.30) == (ce.DEFAULT_EXPECTED_TERMS, ce.DEFAULT_OTHER_COVERAGE)
    settings.raw["priority"]["estimate"] = {"expected_terms": 10, "other_coverage": 0.0}
    assert ce.params(settings) == (10, 0.0)
    before = ce.estimate(THREE, fb_for(settings), settings).ceiling
    del settings.raw["priority"]["estimate"]                                  # a key that is missing: the code default
    assert ce.params(settings) == (34, 0.30) and ce.estimate(THREE, fb_for(settings), settings).ceiling > before


def fb_for(settings):
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


def test_a_deny_listed_term_is_not_backed_even_when_the_bank_text_names_it(settings, fb):
    plain = ce.estimate(THREE, fb, settings)
    denied = ce.estimate(THREE, {**fb, "deny_terms": [*fb.get("deny_terms", []), "scikit-learn"]}, settings)
    assert "scikit-learn" in denied.lack and "scikit-learn" not in plain.lack          # ats.claimable_terms: a tool the user confirmed they lack is no evidence
    assert denied.backed_weight < plain.backed_weight and denied.ceiling < plain.ceiling


def test_only_the_evidence_the_cv_may_use_is_counted(settings, fb):
    hidden = {"id": "hidden", "name": "Zephyr Pipeline", "stack": "Zephyr", "public": False, "github": "https://github.com/x/zephyr",
              "bullets": [{"id": "hz", "text": "Built a Zephyr scheduler.", "terms": ["zephyr"]}]}
    bank = {**fb, "projects": [*fb["projects"], hidden]}
    ad = "Requirements:\n - Zephyr. You have built Zephyr schedulers.\n - Python. You write Python daily.\n - SQL. You query data.\n"
    settings.raw["scoring"]["cv_projects_need_url"] = False
    shown = ce.estimate(ad, bank, settings)
    settings.raw["scoring"]["cv_projects_need_url"] = True                       # the CV leaves a project with no public URL off (tailor.cv_bank), so does F
    left_off = ce.estimate(ad, bank, settings)
    assert "zephyr" in {t.lower() for t in left_off.lack} and left_off.backed_weight < shown.backed_weight


def test_an_ad_whose_headings_are_all_caps_with_emoji_is_still_read(settings):
    """scorer.taxonomy_keywords alone read nothing of this ad (everything sat in "About us"): the estimate was 0.00."""
    from careerops.tailoring.scorer import taxonomy_keywords

    sc = settings["scoring"]
    assert not {"sql", "python", "tableau"} & {k.term.lower() for k in taxonomy_keywords(TRAINLINE_SHAPE, sc["required_weight"], sc["preferred_weight"])}
    terms = {k.term.lower() for k in ce.keywords(TRAINLINE_SHAPE, settings)}
    assert {"sql", "python", "tableau"} <= terms
    assert ce.close_sections(TRAINLINE_SHAPE).count("Requirements:") == 1


def test_a_single_capitalised_word_is_not_turned_into_a_heading(settings):
    ad = "Requirements:\n- Python\nSQL\n- Docker\n"
    assert {k.term.lower() for k in ce.keywords(ad, settings)} == {"python", "sql", "docker"}


def test_the_header_lines_of_an_inbox_file_are_not_the_ads_words(settings):
    text = "Company: Acme\nTitle: Python Engineer\nURL: https://e.com/1\nLocation: London\n\nWe are a friendly team.\n"
    assert ce.body(text).strip() == "We are a friendly team."
    assert ce.keywords(text, settings) == []


def test_an_ad_with_too_few_terms_has_no_reliable_estimate(settings, fb):
    thin = ce.estimate("Requirements:\n- Python\n- SQL\n", fb, settings)
    assert thin.known == 2 and not thin.reliable and ce.estimate(THREE, fb, settings).reliable
    none = ce.estimate("We are a friendly team.", fb, settings)
    assert none.known == 0 and not none.reliable and 0 < none.ceiling < 1            # the prior (the expected terms at the expected coverage), not zero


def row(final, estimate, known=6, company="Co"):
    return ce.Row(f"id{final}{estimate}", company, "DS", final, estimate, known, 12.0, 8.0, 2.0)


def test_the_error_is_the_mean_absolute_difference_and_the_bias_its_signed_mean():
    rows = [row(0.40, 0.50), row(0.60, 0.50), row(0.30, 0.60)]
    mae, bias = ce.errors(rows)
    assert mae == pytest.approx((0.10 + 0.10 + 0.30) / 3) and bias == pytest.approx((0.10 - 0.10 + 0.30) / 3)


def test_the_table_estimates_every_tailored_job_from_its_stored_ad(settings, fb):
    jobs = [{"id": "a", "company": "A", "title": "DS", "description": PHRASE_AD, "ceiling": 0.387},
            {"id": "b", "company": "B", "title": "DS", "description": PHRASE_AD, "ceiling": None},               # never tailored: no row
            {"id": "c", "company": "C", "title": "DS", "description": "tiny", "ceiling": 0.9}]                   # a stub, not an ad: no row
    rows = ce.table(settings, jobs, fb)
    assert [r.job_id for r in rows] == ["a"] and rows[0].estimate == pytest.approx(ce.estimate(PHRASE_AD, fb, settings).ceiling)
    assert rows[0].error == pytest.approx(rows[0].estimate - 0.387)


def test_fit_finds_the_pair_that_matches_the_rows_best():
    truth = (30, 0.25)
    rows = [ce.Row(str(i), "Co", "DS", ce.combine(kw, kw * 0.8, n, *truth, 2.0), 0.0, n, kw, kw * 0.8, 2.0)
            for i, (n, kw) in enumerate([(3, 6.0), (8, 14.0), (12, 22.0), (20, 36.0), (5, 9.0)])]
    terms, coverage, mae = ce.fit(rows)
    assert (terms, round(coverage, 2)) == truth and mae == pytest.approx(0.0, abs=1e-9)


def test_calibrate_scores_prints_n_the_error_the_bias_the_worst_five_and_what_the_skip_line_does(settings, fb):
    jobs = [{"id": str(i), "company": f"Co{i}", "title": "DS", "description": THREE + "x" * 600, "ceiling": c} for i, c in enumerate([0.9, 0.3, 0.8, 0.2, 0.5, 0.4, 0.7])]
    out = "\n".join(ce.calibration(settings, jobs, fb, threshold=0.45))
    assert "(7 tailored jobs" in out and "mean absolute error" in out and "bias" in out and "calibration in use: expected_terms 34, other_coverage 0.30" in out
    assert len([ln for ln in out.splitlines() if " vs final " in ln]) == 5                     # the worst five
    assert "at F < 0.45:" in out and "tailored for nothing" in out and "wrongly skipped" in out
    assert ce.calibration(settings, [], fb)[0].startswith("Ceiling estimate: no tailored job")


def test_the_skip_check_counts_the_saved_the_wrong_and_the_wasted():
    rows = [row(0.30, 0.40, company="Saved"), row(0.70, 0.40, company="Wrong"), row(0.40, 0.60, company="Wasted"), row(0.80, 0.60, company="Fine"),
            row(0.20, 0.30, known=1, company="Thin")]                                        # too few terms: never held, however low
    out = "\n".join(ce.skip_check(rows, 0.45))
    assert "2 of 5 tailored jobs would have been skipped" in out and "1 of them ended under a 50%" in out
    assert "1 reached 60% or more (wrongly skipped: Wrong 70%)" in out and "2 jobs that passed F still ended under 50%" in out
