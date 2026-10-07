"""Context terms (team / product-area / org names) with the Monzo JD of the first live run: they are not scored, not in the
ceiling, and shown separately; the same `analyse` makes the keywords for live tailoring and for calibration."""
import shutil
from pathlib import Path

import pytest
import yaml

from careerops.config import load_yaml
from careerops.reporting import calibrate as cal
from careerops.review import workflow as review
from careerops.tailoring import tailor
from careerops.tailoring.context_terms import listed_names, model_terms, split_context
from careerops.tailoring.scorer import Keyword, score

MONZO = (Path(__file__).resolve().parents[1] / "fixtures" / "tailoring" / "jd_monzo.txt").read_text(encoding="utf-8")

# the keyword list the first live run stored for this JD (data/careerops.db, manual:de99f0a98229): term, weight
LIVE_RUN = [("A/B experiments", 2.0), ("data mining", 2.0), ("lifetime value", 2.0), ("Looker", 2.0), ("Python", 2.0),
            ("quantitative analysis", 2.0), ("real-time analytics", 2.0), ("SQL", 2.0), ("Growth", 1.0), ("Borrowing", 1.0),
            ("Core", 1.0), ("cross-functional squads", 1.0), ("data science teams", 1.0), ("Fincrime", 1.0),
            ("Payments", 1.0), ("product squad", 1.0), ("product strategy", 1.0), ("self-serve analytics", 1.0),
            ("Wealth", 1.0)]
CONTEXT = {"Growth", "Borrowing", "Core", "cross-functional squads", "data science teams", "Fincrime", "Payments",
           "product squad", "Wealth"}


def kws(rows=LIVE_RUN):
    return [Keyword(t, w, w >= 2.0) for t, w in rows]


def test_the_capitalised_list_in_how_we_hire_is_read_as_names():
    assert {"borrowing", "growth", "payments", "fincrime", "wealth", "core"} <= listed_names(MONZO)
    assert "python" not in listed_names(MONZO)


def test_the_nine_team_and_org_terms_of_the_first_live_run_are_context_and_the_skills_stay():
    kept, context = split_context(kws(), MONZO)
    assert {c["term"] for c in context} == CONTEXT
    assert [k.term for k in kept] == ["A/B experiments", "data mining", "lifetime value", "Looker", "Python",
                                      "quantitative analysis", "real-time analytics", "SQL", "product strategy",
                                      "self-serve analytics"]
    why = {c["term"]: c["why"] for c in context}
    assert "organisational noun" in why["product squad"] and "organisational noun" in why["data science teams"]
    assert "organisational noun" in why["cross-functional squads"] and "listed in the JD" in why["Fincrime"]
    assert "listed in the JD" in why["Core"] and all(c["weight"] in (1.0, 2.0) for c in context)


def test_a_taxonomy_skill_is_never_context_whatever_the_model_or_a_list_says():
    jd = "Tools: Python, Looker, SQL, Tableau and more. Teams: Growth, Payments, Wealth."
    kept, context = split_context(kws([("Python", 2.0), ("Looker", 2.0), ("SQL", 2.0)]), jd,
                                  model={"python", "looker", "sql"})
    assert [k.term for k in kept] == ["Python", "Looker", "SQL"] and context == []


def test_only_the_last_word_makes_an_org_noun():
    jd = "We value team leadership and engineering teams."
    kept, context = split_context(kws([("team leadership", 1.0), ("engineering teams", 1.0), ("squad", 1.0),
                                       ("data governance", 1.0)]), jd)
    assert [c["term"] for c in context] == ["engineering teams", "squad"]
    assert [k.term for k in kept] == ["team leadership", "data governance"]


def test_a_lone_capitalised_word_in_a_sentence_is_not_a_list():
    kept, context = split_context(kws([("Growth", 1.0), ("Payments", 1.0)]), "Growth and Payments matter to us.")
    assert context == [] and len(kept) == 2


def test_the_models_context_terms_must_be_in_the_jd_and_the_teams_own_name_counts():
    assert model_terms(["Growth", "Invented Squad"], MONZO) == {"growth"}
    assert model_terms("nonsense", "nothing") == set() and model_terms(None, MONZO) == set()
    kept, context = split_context(kws([("Risk", 1.0), ("Fraud", 1.0), ("pricing", 1.0)]),
                                  "Risk and Fraud and pricing", model={"risk"}, team="Fraud")
    assert {c["term"]: c["why"] for c in context} == {"Risk": "named as context by the model",
                                                      "Fraud": "the team's own name"}
    assert [k.term for k in kept] == ["pricing"]


# ---------------------------------------------------------------- generic head words: "data discipline" is not a skill
def test_data_discipline_from_the_monzo_jd_is_a_context_term_not_a_skill_or_a_gap():
    assert "central data discipline" in MONZO                      # the JD says it (the same text as data/jds/inbox/live-001.txt)
    kept, context = split_context(kws([("data discipline", 1.0), ("Python", 2.0), ("product strategy", 1.0)]), MONZO)
    assert [k.term for k in kept] == ["Python", "product strategy"]
    assert [c["term"] for c in context] == ["data discipline"] and "generic head word 'discipline'" in context[0]["why"]


@pytest.mark.parametrize("term", ["data discipline", "engineering culture", "growth mindset", "Data Culture", "discipline",
                                  "central data function", "business function", "analytics functions", "ways of working ethos"])
def test_generic_head_words_make_a_term_context(term):
    kept, context = split_context(kws([(term, 1.0)]), f"We value {term} here.")
    assert kept == [] and [c["term"] for c in context] == [term]


@pytest.mark.parametrize("term", ["loss function", "window functions", "activation function", "cost functions",
                                  "disciplined approach", "culture fit analysis", "functional programming"])
def test_a_skill_that_merely_contains_such_a_word_stays_a_keyword(term):
    kept, context = split_context(kws([(term, 1.0)]), f"You know {term}.")
    assert [k.term for k in kept] == [term] and context == []


def test_a_taxonomy_term_is_never_demoted_by_a_generic_head_word():
    assert split_context(kws([("Python", 2.0)]), "Python discipline matters.")[1] == []


def test_analyse_takes_data_discipline_out_of_the_keywords_even_when_the_model_lists_it_as_required(settings, spy_llm):
    job = {"company": "Monzo", "title": "Data Scientist", "description": MONZO}
    llm = spy_llm({"extract_keywords": _answer(["data discipline", "A/B experiments"], ["data mining"])})
    notes, context = [], []
    got, _, _ = tailor.analyse(job, llm, settings, notes, None, context)
    assert "data discipline" not in [k.term.lower() for k in got] and "A/B experiments" in [k.term for k in got]
    assert [c["term"] for c in context] == ["data discipline"]
    assert any("'data discipline' is context (generic head word 'discipline'" in n for n in notes)


@pytest.mark.slow
def test_a_tailored_job_never_lists_data_discipline_as_a_genuine_gap(settings, fb_fixture, spy_llm):
    job = {"id": "manual:monzo2", "source": "manual", "company": "Monzo", "title": "Data Scientist", "location": "London",
           "url": "u", "apply_url": "u", "description": MONZO}
    llm = spy_llm({"extract_keywords": _answer(["data discipline", "A/B experiments", "data mining"], [])})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert "data discipline" not in [k["term"].lower() for k in rep["keywords"]]
    assert "data discipline" not in [g.lower() for g in rep["genuine_gaps"]]
    assert "data discipline" in [c["term"] for c in rep["context_terms"]]


# ---------------------------------------------------------------- scoring: out of the numerator AND the denominator
def test_context_terms_leave_both_the_numerator_and_the_denominator_of_the_score():
    cv = "Python SQL and a Core library"                         # 'Core' is only the word, not a skill of the CV
    before = score(cv, kws())
    kept, context = split_context(kws(), MONZO)
    after = score(cv, kept)
    assert round(before.literal, 4) == round(5 / 27, 4)          # the 0.185 of the first live run
    assert round(after.literal, 4) == round(4 / 18, 4) and after.hits == ["Python", "SQL"]
    assert after.literal > before.literal and not set(after.misses) & CONTEXT


# ---------------------------------------------------------------- analyse: the one place keywords are made
def _answer(terms_required, terms_preferred, context=()):
    def handler(ctx):
        return {"required": terms_required, "preferred": terms_preferred, "context_terms": list(context),
                "company_hook": "Monzo builds a bank.", "team": ""}
    return handler


JUNK = ["Growth", "Borrowing", "Fincrime", "Payments", "Wealth", "Core", "product squad", "cross-functional squads",
        "data science teams"]


def test_analyse_puts_the_teams_aside_and_they_do_not_use_up_a_place_under_the_cap(settings, spy_llm):
    job = {"company": "Monzo", "title": "Data Scientist", "description": MONZO}
    llm = spy_llm({"extract_keywords": _answer(["A/B experiments", "data mining", "lifetime value", "quantitative analysis",
                                                 "real-time analytics", "Looker"],
                                                ["product strategy", "self-serve analytics", *JUNK])})
    notes, context = [], []
    got, hook, team = tailor.analyse(job, llm, settings, notes, None, context)
    terms = [k.term for k in got]
    assert {c["term"] for c in context} == CONTEXT
    low = {t.lower() for t in terms}
    assert not set(terms) & CONTEXT and {"product strategy", "self-serve analytics", "python", "sql", "looker"} <= low
    assert any("'product squad' is context (organisational noun 'squad'), not scored" in n for n in notes)
    # with a cap exactly the size of the scored list nothing real is cut: the team names did not take places
    settings.raw["scoring"]["max_keywords"] = len(terms)
    again, _, _ = tailor.analyse(job, spy_llm({"extract_keywords": _answer(
        ["A/B experiments", "data mining", "lifetime value", "quantitative analysis", "real-time analytics", "Looker"],
        ["product strategy", "self-serve analytics", *JUNK])}), settings, [], None, [])
    assert {k.term for k in again} == set(terms)


def test_a_term_the_model_names_as_context_is_taken_out_too(settings, spy_llm):
    job = {"company": "Monzo", "title": "Data Scientist", "description": MONZO}
    llm = spy_llm({"extract_keywords": _answer(["Python"], ["self-serve analytics", "Looker"], context=["Looker", "How we hire"])})
    context = []
    got, _, _ = tailor.analyse(job, llm, settings, [], None, context)
    assert "Looker" in [k.term for k in got] and context == []     # Looker is a taxonomy tool: the model cannot demote it


def test_the_extraction_prompt_asks_for_context_terms():
    from careerops.tailoring import prompts as P
    flat = " ".join(P.KEYWORDS_SYSTEM.split())
    assert '"context_terms"' in flat and "that are NOT skills" in flat and "Never put these in required or preferred" in flat


# ---------------------------------------------------------------- live tailoring and calibration: the same hygiene
@pytest.fixture
def monzo_llm(spy_llm):
    return spy_llm({"extract_keywords": _answer(["A/B experiments", "data mining", "lifetime value", "quantitative analysis"],
                                                 ["product strategy", "self-serve analytics", *JUNK, "tasks", "data",
                                                  "retrieval pipelines", "Bachelor's degree"])})


@pytest.fixture
def monzo_job():
    return {"id": "manual:monzo1", "source": "manual", "company": "Monzo", "title": "Data Scientist",
            "location": "London", "url": "u", "apply_url": "u", "description": MONZO}


@pytest.mark.slow
def test_live_tailoring_drops_generic_phrases_and_teams_exactly_as_calibration_does(settings, fb_fixture, monzo_job, monzo_llm,
                                                                                      tmp_path):
    rep = tailor.tailor_job(monzo_job, fb_fixture, monzo_llm, settings, settings.path("outputs"))
    live_terms = [k["term"] for k in rep["keywords"]]
    assert not set(live_terms) & CONTEXT and "tasks" not in live_terms and "data" not in live_terms
    assert {c["term"] for c in rep["context_terms"]} == CONTEXT
    assert any("dropped keyword 'tasks'" in n for n in rep["analysis_warnings"])

    # the calibration route: the same JD, the same model answer, frozen with `freeze_keywords`
    jds = tmp_path / "jds"
    jds.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "fixtures" / "tailoring" / "jd_monzo.txt", jds / "monzo.txt")
    manifest = tmp_path / "m.yaml"
    manifest.write_text(yaml.safe_dump([{"id": "cal-monzo", "company": "Monzo", "title": "Data Scientist",
                                         "jd_file": str(jds / "monzo.txt")}]), encoding="utf-8")
    kdir = tmp_path / "kw"
    res = cal.freeze_keywords(settings, monzo_llm, manifest, kdir, log=lambda m: None)
    assert res["errors"] == {}
    frozen = yaml.safe_load((kdir / "cal-monzo.yaml").read_text(encoding="utf-8"))
    assert [k["term"] for k in frozen["keywords"]] == live_terms
    assert {c["term"] for c in frozen["context_terms"]} == CONTEXT
    # and a frozen file hands the context terms back to the tailoring loop, so a calibration report shows them too
    entry = cal.load_manifest(manifest, settings.root)[0]
    loaded = cal.load_frozen(kdir / "cal-monzo.yaml", entry)
    context = []
    kw, _, _ = tailor.analyse(monzo_job, None, settings, [], loaded, context)
    assert [k.term for k in kw] == live_terms and {c["term"] for c in context} == CONTEXT


@pytest.mark.slow
def test_the_ceiling_and_the_score_of_a_tailored_job_do_not_count_the_teams(settings, fb_fixture, monzo_job, monzo_llm):
    rep = tailor.tailor_job(monzo_job, fb_fixture, monzo_llm, settings, settings.path("outputs"))
    scored = {k["term"] for k in rep["keywords"]}
    assert scored and not scored & CONTEXT
    assert not (set(rep["final"]["hits"]) | set(rep["final"]["misses"]) | set(rep["genuine_gaps"])) & CONTEXT


@pytest.mark.slow
def test_the_review_screen_lists_the_context_terms_separately(settings, fb_fixture, monzo_job, monzo_llm, db):
    rep = tailor.tailor_job(monzo_job, fb_fixture, monzo_llm, settings, settings.path("outputs"))
    db.upsert_job(monzo_job)
    db.update_job(monzo_job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="data_scientist")
    text = review.summary(db.job(monzo_job["id"]), rep)
    line = next(l for l in text.splitlines() if l.startswith("Context terms"))
    assert "not scored, not in the ceiling" in line and all(t in line for t in ("Growth", "Fincrime", "product squad"))
    assert "Genuine gaps" not in text or not any(t in text.split("Genuine gaps")[1] for t in ("Growth", "Wealth"))


@pytest.mark.slow
def test_an_old_report_without_context_terms_still_renders(settings, fb_fixture, monzo_job, monzo_llm, db):
    rep = tailor.tailor_job(monzo_job, fb_fixture, monzo_llm, settings, settings.path("outputs"))
    rep.pop("context_terms"); rep.pop("possible_evidence")
    db.upsert_job(monzo_job)
    db.update_job(monzo_job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="data_scientist")
    assert "Context terms" not in review.summary(db.job(monzo_job["id"]), rep)
