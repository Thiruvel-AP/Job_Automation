"""ATS scorer, stemming, keyword extraction, honest ceiling."""
from pathlib import Path

import pytest

from careerops.config import ROOT, load_yaml
from careerops.tailoring.scorer import Keyword, ceiling, norm_stem, score, taxonomy_keywords
from careerops.tailoring.tailor import fact_text


def test_literal_vs_stemmed_and_linewrap():
    kws = [Keyword("communicate", 1, True), Keyword("end-to-end", 1, True), Keyword("scalable", 2, True),
           Keyword("Master's degree", 1, True)]
    text = "Communicating clearly. Built end to end systems that are scal-\nable. Master’s degree."
    s = score(text, kws)
    assert "scalable" in s.hits                     # line-wrap hyphen is an artifact, not a miss
    assert "Master's degree" in s.hits              # curly apostrophe normalised
    assert set(s.wording_misses) == {"communicate", "end-to-end"}
    assert s.literal == pytest.approx(3 / 5) and s.stemmed == 1.0


def test_british_american():
    assert norm_stem("containerised") == norm_stem("containerized")
    assert norm_stem("modelling behaviour") == norm_stem("modeling behavior")


def test_taxonomy_weights_by_section(jd):
    kws = {k.term.lower(): k for k in taxonomy_keywords(jd)}
    assert kws["python"].weight == 2.0 and kws["python"].required
    assert kws["kubernetes"].weight == 1.0 and not kws["kubernetes"].required


def test_ceiling_reports_genuine_gaps(jd):
    fb = load_yaml(ROOT / "config/profile/fact_bank.yaml")
    c, gaps = ceiling(fact_text(fb), taxonomy_keywords(jd))
    assert "kubernetes" in [g.lower() for g in gaps]     # confirmed-absent → honest gap
    assert 0.7 < c < 1.0


def test_classifier_classification_and_visualisation_share_a_stem_family_in_the_stemmed_view_only():
    assert norm_stem("classifiers") == norm_stem("classification")
    assert len({norm_stem(w) for w in ("visualisation", "visualization", "visualise", "visualize", "visualising")}) == 1
    kws = [Keyword("classifier", 1, True), Keyword("visualization", 1, True)]
    s = score("Built classification models and visualisation dashboards.", kws)
    assert s.literal == 0.0 and s.stemmed == 1.0                  # the literal score is untouched
    assert set(s.wording_misses) == {"classifier", "visualization"}
