"""ATS scorer, stemming, keyword extraction, honest ceiling."""
from pathlib import Path

import pytest

from careerops.config import load_yaml
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


def test_ceiling_reports_genuine_gaps(jd, settings):
    fb = load_yaml(settings.path("fact_bank"))             # the fictional profile: it has no Kubernetes, whatever the real bank gains
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


def test_literal_score_takes_singular_and_plural_as_one_term_in_both_directions():
    for kw, text in (("prompts", "Wrote prompt templates."), ("prompt", "Wrote prompts."),
                     ("evaluations", "Ran an LLM evaluation after the migration."),
                     ("evaluation", "Ran evaluations."), ("business processes", "Mapped a business process."),
                     ("box", "Used boxes."), ("boxes", "Used a box.")):
        s = score(text, [Keyword(kw, 1, True)])
        assert s.literal == 1.0 and s.hits == [kw] and s.wording_misses == [], (kw, text)


def test_plural_tolerance_does_not_match_a_longer_word_or_a_different_word():
    for kw, text in (("prompt", "Wrote a promptly reply."), ("evaluations", "Evaluated the model."),
                     ("class", "Used clas methods."), ("model", "Modelling things.")):
        assert score(text, [Keyword(kw, 1, True)]).literal == 0.0, (kw, text)


def test_the_stemmed_view_is_unchanged_by_the_plural_rule():
    assert norm_stem("prompts") == norm_stem("prompt") and norm_stem("evaluations") == norm_stem("evaluation")
    s = score("Wrote prompting guides.", [Keyword("prompts", 1, True)])        # "prompting" is not a plural of "prompts"
    assert s.literal == 0.0 and s.stemmed == 1.0 and s.wording_misses == ["prompts"]
