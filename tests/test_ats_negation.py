"""Negation-aware ATS scoring (careerops/ats.py): a mention within six tokens after "not", "never", "without", "lack", "gap",
"learning" and the like, in the same sentence, is not a hit; the three numbers; the gate. No network, no model, no PDFs."""
import pytest

from careerops import ats
from careerops.tailoring.scorer import Keyword, score as plain_score

PY, LGBM, DBT = Keyword("Python", 2, True), Keyword("LightGBM", 2, True), Keyword("dbt", 1, False)


def hits(text, *kws):
    return ats.score(text, list(kws)).hits


@pytest.mark.parametrize("text", [
    "I have not used LightGBM.",
    "I haven't used LightGBM.",
    "I have not yet used LightGBM.",
    "I am yet to use LightGBM.",
    "No experience with LightGBM.",
    "I have never touched LightGBM.",
    "Worked without LightGBM.",
    "A lack of LightGBM on my CV.",
    "Gap: LightGBM.",
    "Currently learning LightGBM.",
    "My forecasting has been on price series, not retail demand or LightGBM.",
])
def test_a_negated_mention_is_not_a_hit(text):
    s = ats.score(text, [LGBM])
    assert s.hits == [] and s.literal == 0.0 and s.negated == ["LightGBM"], text


@pytest.mark.parametrize("text", ["I used LightGBM.", "Built LightGBM models for demand.", "LightGBM, XGBoost and Python."])
def test_a_plain_mention_is_a_hit(text):
    assert hits(text, LGBM) == ["LightGBM"]


def test_the_window_is_six_tokens_before_the_term():
    six = "not a b c d e LightGBM"          # 'not' is the 6th token before the term: negated
    seven = "not a b c d e f LightGBM"       # the 7th: out of range
    assert ats.score(six, [LGBM]).hits == [] and ats.score(seven, [LGBM]).hits == ["LightGBM"]


def test_a_negator_after_the_term_does_not_matter():
    assert hits("LightGBM, which I do not use at all", LGBM) == ["LightGBM"]


def test_the_window_stops_at_the_sentence():
    assert hits("I have not used dbt. I used LightGBM.", LGBM, DBT) == ["LightGBM"]
    assert hits("I have not used dbt\n\nLightGBM models", LGBM, DBT) == ["LightGBM"]            # a blank line ends it
    assert hits("Skills\n• not dbt\n• LightGBM", LGBM, DBT) == ["LightGBM"]            # so does a bullet


def test_a_wrapped_line_stays_in_its_sentence():
    assert ats.score("I have not used\nLightGBM", [LGBM]).hits == []


@pytest.mark.parametrize("text", [
    "Machine learning with Python.", "Deep learning in Python.", "Transfer learning, Python.", "A learning rate schedule in Python.",
    "Reinforcement learning and Python.", "Statistical learning with Python.",
])
def test_learning_is_a_negator_only_as_a_verb(text):
    assert hits(text, PY) == ["Python"], text


def test_learning_as_a_verb_is_a_negator():
    assert hits("Currently learning Python.", PY) == []
    assert hits("Learning Python in my spare time.", PY) == []


def test_any_negated_mention_makes_the_text_not_claim_the_term():
    """A letter that says it has not used LightGBM and then bridges from it ("XGBoost shares LightGBM's ideas") is not claiming it."""
    text = "I have not yet used LightGBM. XGBoost shares LightGBM's gradient boosting ideas."
    s = ats.score(text, [LGBM])
    assert s.hits == [] and s.misses == ["LightGBM"] and s.negated == ["LightGBM"]


def test_a_term_with_a_clean_mention_in_another_text_is_still_a_hit_in_that_text():
    cv, letter = "Built LightGBM models.", "I have not used dbt."
    assert hits(cv, LGBM) == ["LightGBM"] and hits(letter, LGBM) == []


def test_the_stemmed_view_skips_negated_sentences_too():
    """'prototyping' is the keyword; 'prototyped' says it in another form (a wording miss), unless the sentence is negated."""
    kw = Keyword("prototyping", 2, True)
    assert ats.score("I prototyped the service.", [kw]).wording_misses == ["prototyping"]
    s = ats.score("I have not prototyped the service.", [kw])
    assert s.wording_misses == [] and s.misses == ["prototyping"]


def test_without_negators_the_score_is_the_plain_score():
    text = "Python and SQL at work. Built dbt models with LightGBM in Python."
    kws = [PY, LGBM, DBT, Keyword("SQL", 2, True)]
    a, b = ats.score(text, kws), plain_score(text, kws)
    assert (a.literal, a.stemmed, a.hits, a.misses, a.wording_misses) == (b.literal, b.stemmed, b.hits, b.misses, b.wording_misses)


def test_score_is_the_weight_share_of_the_hits():
    s = ats.score("Python and dbt. I have not used LightGBM.", [PY, LGBM, DBT])
    assert s.literal == pytest.approx((2 + 1) / 5)


# ---------------------------------------------------------------- the three numbers and the gate
KWS = [Keyword("Python", 3, True), Keyword("SQL", 3, True), Keyword("LightGBM", 2, True), Keyword("dbt", 2, True)]


def test_the_three_numbers():
    cv = "Python and SQL at work."
    letter = "I have not yet used LightGBM, but I know dbt well."
    s = ats.scores(cv, KWS, ["Python", "SQL", "LightGBM"], letter)
    assert s.strict == pytest.approx(60.0)                       # (3+3)/10
    assert s.ceiling == pytest.approx(80.0)                      # (3+3+2)/10: dbt is not claimable
    assert s.combined == pytest.approx(80.0)                     # + dbt from the letter; LightGBM is negated
    assert s.letter == pytest.approx(20.0) and s.letter_negated == ["LightGBM"]
    assert s.negated == [] and "ATS strict CV 60.0%" in s.line() and "letter alone 20.0% (not gated)" in s.line()


def test_without_a_letter_there_is_no_combined_number():
    s = ats.scores("Python", KWS, ["Python"])
    assert s.combined is None and s.letter is None


def test_claimable_leaves_out_gaps_and_deny_listed_terms():
    kws = [Keyword("Python", 2, True), Keyword("dbt", 2, True), Keyword("Snowflake", 2, True), Keyword("Optuna", 1, False)]
    got = ats.claimable_terms(kws, gaps=["snowflake"], deny=["dbt"])
    assert got == ["Python", "Optuna"]
    assert ats.claimable_ceiling(kws, got) == pytest.approx(100 * 3 / 7)


@pytest.mark.parametrize("strict, ceiling, want", [
    (72.5, 72.5, "ok"), (70.0, 70.0, "ok"), (85.0, 90, "ok"), (85.1, 90, "above"),
    (65.0, 72.5, "retry"), (65.0, 70.0, "retry"), (65.0, 69.9, "stretch"), (40.0, 50.0, "stretch"),
])
def test_the_gate(strict, ceiling, want):
    assert ats.gate(strict, ceiling) == want


def test_a_stretch_job_is_labelled_not_dropped():
    s = ats.scores("Python", KWS, ["Python"])                    # strict 30, ceiling 30
    assert s.gate == "stretch" and "STRETCH" in s.line()


def test_the_bridge_after_a_but_is_not_negated():
    """Fix round 2: "I have not used dbt yet, but SQL is the closest tool I know" denies dbt, not SQL."""
    from careerops import ats
    from careerops.tailoring.scorer import Keyword

    kws = [Keyword("dbt", 2, True), Keyword("SQL", 2, True)]
    s = ats.score("I have not used dbt yet, but SQL is the closest tool I know.", kws)
    assert s.negated == ["dbt"] and s.hits == ["SQL"]
    assert ats.score("I have not used dbt or SQL.", kws).negated == ["dbt", "SQL"]            # no "but": both are denied
