"""Fix round 2, item 6: the Bitcoin LSTM forecasting project was absent from a forecasting-heavy job while an irrelevant Swift/SwiftUI
bullet was on the CV.

Two causes, both reproduced on the real bank (read-only) with the golden Anaplan keywords:
  1. matching: the ad says "time-series forecasting", the bullet's `terms` say "forecasting" and "time series" as two separate items.
     Hyphen / space / case differences were handled by the stemmer, but a multiword keyword had to appear as ONE phrase, so the
     project scored 0 (no hit at all) and ranked with the filler.
  2. selection: even when it hit, the skills line already said "Time series forecasting", and a keyword the skills cover adds no gain,
     so a project that proves it lost to projects that add anything at all.
Fix: both sides are normalised (lower case; - _ / are spaces; spaces collapsed) and a multiword keyword matches as a phrase OR when every
one of its words is in the bullet's text and terms; a keyword the skills line covers but no chosen bullet shows still earns a project
a part of its weight (selection.SKILL_ONLY_CREDIT).
"""
import pytest

from careerops.tailoring import budget
from careerops.tailoring import selection as S
from careerops.tailoring.scorer import Keyword

KWS = [Keyword("time-series forecasting", 3, True), Keyword("Python", 3, True), Keyword("XGBoost", 3, True), Keyword("hyperparameter tuning", 2, True),
       Keyword("MLflow", 2, True), Keyword("PyTorch", 2, True), Keyword("scikit-learn", 1, False)]
JD = "We need time-series forecasting with Python, XGBoost and hyperparameter tuning; experiment tracking in MLflow."


def b(bid, text, terms):
    return {"id": bid, "text": text, "terms": terms}


def bank():
    return {
        "experience": [
            {"id": "e1", "org": "Acme", "title": "Data Scientist", "bullets": [
                b("a1", "Built an XGBoost propensity model in Python.", ["xgboost", "python"]),
                b("a2", "Tracked experiments in MLflow.", ["mlflow"])]},
            {"id": "e2", "org": "Avasoft", "title": "Software Engineer", "bullets": [
                b("av1", "Integrated payment APIs into a Flutter app.", ["api", "flutter"]),
                b("av7", "Built e-commerce and ticketing UIs in Swift/SwiftUI with product search.", ["swift", "swiftui", "ios", "mobile", "ui"])]}],
        "projects": [
            {"id": "swift", "name": "Ticketing app", "bullets": [
                b("p_sw1", "Built a ticketing UI in SwiftUI.", ["swift", "swiftui"])]},
            {"id": "churn", "name": "Banking Customer Churn", "bullets": [
                b("p_ch1", "Predicted churn with XGBoost and Bayesian hyperparameter tuning.", ["xgboost", "hyperparameter tuning", "python"])]},
            {"id": "torch", "name": "Image classifier", "bullets": [
                b("p_to1", "Trained a PyTorch CNN on CIFAR-10.", ["pytorch", "cnn"])]},
            {"id": "nlp", "name": "Ticket clustering", "bullets": [
                b("p_nl1", "Clustered support tickets with scikit-learn.", ["scikit-learn", "clustering"])]},
            {"id": "bitcoin", "name": "Bitcoin Forecasting: LSTM Time Series Service", "bullets": [
                b("p_bt1", "Trained a forecasting LSTM with cyclic date encoding and TimeSeriesSplit grid search, served over a REST API.",
                  ["forecasting", "time series", "lstm", "tensorflow", "model serving", "rest api"])]}],
        "skills": {"Forecasting": ["Time series forecasting (LSTM, TimeSeriesSplit validation)", "XGBoost"]},
        "deny_terms": [], "identity": {"name": "A"}, "profile_facts": ["x"], "education": [], "certifications": []}


def test_a_hyphenated_multiword_keyword_matches_a_bullet_whose_terms_split_it():
    hit = S._hits("Trained a forecasting LSTM. forecasting time series lstm", KWS)
    assert 0 in hit
    assert 0 not in S._hits("Built e-commerce UIs in SwiftUI. swift swiftui ios", KWS)


@pytest.mark.parametrize("keyword", ["time-series forecasting", "Time Series Forecasting", "time_series/forecasting", "time series  forecasting"])
def test_the_keyword_is_normalised_on_both_sides(keyword):
    assert S._hits("forecasting, time-series, LSTM", [Keyword(keyword, 3, True)]) == frozenset({0})


def test_single_words_still_need_the_word():
    assert S._hits("a series of forecasts", [Keyword("time series", 1, True)]) == frozenset()
    assert S._hits("Python and SQL", [Keyword("pytorch", 1, True)]) == frozenset()


def test_the_bitcoin_bullet_scores_above_the_swiftui_bullet():
    sc = budget.relevance_scores(bank(), KWS, JD)
    assert sc["p_bt1"] > sc["av7"], sc
    assert budget.bullet_scores(bank(), KWS)["p_bt1"] >= 3.0


def test_the_bitcoin_project_is_selected_even_though_the_skills_line_already_says_forecasting():
    """The keyword matches (alts), the skills line covers it, and three other projects add something: without a credit for a
    skill-only keyword the project that proves it ties with the filler at zero gain and loses to a one-point project."""
    fb = bank()
    kws = [Keyword("time series forecasting", 3, True, ["time series", "forecasting"]), *KWS[1:]]
    layout = S.Layout((2, 2), (1, 2), 3, None)
    sel = S.select_cv(fb, kws, None, skills=fb["skills"], layout=layout, jd=JD)
    assert sel["projects"][:2] == ["churn", "torch"] and "bitcoin" in sel["projects"], sel["projects"]
    assert "swift" not in sel["projects"]


def test_with_the_ads_hyphenated_keyword_and_no_alts_the_project_is_still_found():
    fb = bank()
    layout = S.Layout((2, 2), (1, 2), 3, None)
    sel = S.select_cv(fb, KWS, None, skills=fb["skills"], layout=layout, jd=JD)
    assert "bitcoin" in sel["projects"] and "swift" not in sel["projects"], sel["projects"]


def test_the_hook_scoring_uses_the_same_matching():
    from careerops import hook as H

    scores = H.bullet_hook_scores(bank(), KWS)
    assert scores["p_bt1"] == 3.0 and scores["av7"] == 0.0
