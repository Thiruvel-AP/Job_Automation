"""CV selection round: bullets and projects ranked by JD keywords AND the domain / topics the JD names (customer, consumer,
behaviour, segmentation, validation, statistics ...), the top-10 ranking for the review screen, fact-bank order inside a role, an
award never repeated in a bullet, the unsupported-qualifier check on the profile, and the skills rows (first item capitalised,
Databases row when the JD mentions SQL or databases). Light tests: no xelatex, no network, no model."""
from pathlib import Path

import pytest

from careerops.config import load_yaml
from careerops.review.workflow import ranking_lines, typography_lines
from careerops.tailoring import tailor
from careerops.tailoring.budget import rank_projects, ranking_table, relevance_parts, relevance_scores
from careerops.tailoring.domain import jd_rank_weights, rank_groups
from careerops.tailoring.qualifiers import QUALIFIERS, QUALIFIER_LIST, prompt_list, unsupported_qualifiers
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import TWO_PAGE, fill_order, select_cv
from careerops.tailoring.skills import cv_skills, is_database_row, wants_databases

FIX = Path(__file__).parent.parent / "fixtures"
JD = ("We use customer behaviour data to build consumer segmentation models. You will own model validation and "
      "statistics for our customers.")


def _bank() -> dict:
    return load_yaml(FIX / "profile" / "fact_bank.yaml")


def _tiny(n_projects=4):
    """Projects whose bullets say nothing the keywords ask for: only the domain words tell them apart."""
    def bullets(prefix, texts):
        return [{"id": f"{prefix}{i}", "text": t, "terms": []} for i, t in enumerate(texts)]
    return {"skills": {},
            "experience": [{"id": "e0", "org": "Acme", "title": "Analyst",
                            "bullets": bullets("e0b", ["Wrote nightly jobs.", "Reviewed pull requests.", "Cleaned tables."])}],
            "projects": [{"id": "plain", "name": "Plain tool", "bullets": bullets("plainb", ["Wrote a command line tool."])},
                         {"id": "seg", "name": "Segmentation study",
                          "bullets": bullets("segb", ["Grouped customers by behaviour into segments."])},
                         {"id": "val", "name": "Validation harness",
                          "bullets": bullets("valb", ["Checked statistical validation of the pipeline."])},
                         {"id": "other", "name": "Other tool", "bullets": bullets("otherb", ["Wrote another tool."])}][:n_projects]}


# ------------------------------------------------------------------------------------------------ domain and topics count
def test_customer_consumer_behaviour_segmentation_validation_and_statistics_all_count():
    w = jd_rank_weights(JD)
    assert {"customer", "behaviour", "validation", "statistics"} <= set(w)               # consumer is in the customer group
    for text, group in [("Grouped consumers", "customer"), ("customer churn", "customer"), ("user behaviour", "behaviour"),
                        ("behavioural analytics", "behaviour"), ("customer segmentation", "behaviour"),
                        ("model validation", "validation"), ("validated the output", "validation"),
                        ("statistics", "statistics"), ("statistical tests", "statistics")]:
        assert group in rank_groups(text), (text, group)
    assert rank_groups("Wrote nightly jobs.") == set()


def test_domain_overlap_adds_to_a_bullets_score_only_when_the_jd_names_it():
    fb = _tiny()
    kws = [Keyword("nightly", 2, True)]
    base = relevance_scores(fb, kws)                                                       # no JD: keywords alone
    assert base["segb0"] == 0.0 and base["e0b0"] > 0
    with_jd = relevance_scores(fb, kws, JD)
    assert with_jd["segb0"] > base["segb0"] and with_jd["valb0"] > 0 and with_jd["plainb0"] == 0.0
    kw, dom = relevance_parts(fb, kws, JD)["segb0"]
    assert kw == 0.0 and dom > 0
    assert with_jd["e0b0"] == base["e0b0"]                                                 # a bullet naming no domain: unchanged


def test_projects_are_ranked_by_keywords_plus_domain():
    fb = _tiny()
    assert rank_projects(fb, [], None, "")[:1] == ["plain"]                                # nothing to rank by: fact-bank order
    ranked = rank_projects(fb, [], None, JD)
    assert ranked[:2] == ["seg", "val"] and ranked[2:] == ["plain", "other"]               # the two that name the JD's topics first


def test_the_cv_picks_the_projects_that_name_the_jds_domain_when_keywords_cannot_tell_them_apart():
    fb = _tiny()
    kws = [Keyword("kubernetes", 2, True)]                                                 # nothing in the bank says it
    layout = TWO_PAGE.__class__(TWO_PAGE.first, TWO_PAGE.other, 2, TWO_PAGE.project_bullets)
    assert select_cv(fb, kws, layout=layout)["projects"] == ["plain", "seg"]               # no JD: fact-bank order
    assert set(select_cv(fb, kws, layout=layout, jd=JD)["projects"]) == {"seg", "val"}


def test_the_fill_adds_the_project_that_covers_a_domain_group_nobody_covers_yet():
    fb = _tiny()
    layout = TWO_PAGE.__class__(TWO_PAGE.first, TWO_PAGE.other, 1, TWO_PAGE.project_bullets)
    sel = select_cv(fb, [], layout=layout, jd=JD)
    shown = sel["projects"][0]
    first_extra = fill_order(fb, [], sel, max_projects=2, jd=JD)[0]
    assert first_extra.startswith({"seg": "val", "val": "seg"}[shown])                     # the other topic, not an empty project


def test_the_review_screen_ranking_is_the_top_ten_with_scores():
    fb = _bank()
    kws = [Keyword("pytorch", 2, True), Keyword("docker", 1, False)]
    chosen = {"az1", "churn1"}
    table = ranking_table(fb, kws, JD, chosen)
    assert len(table["bullets"]) == 10 and len(table["projects"]) <= 10
    scores = [b["score"] for b in table["bullets"]]
    assert scores == sorted(scores, reverse=True) and scores[0] > 0
    assert all(b["score"] == pytest.approx(b["keywords"] + b["domain"], abs=0.011) for b in table["bullets"])
    assert all(b["chosen"] == (b["id"] in chosen) for b in table["bullets"])
    ps = [p["score"] for p in table["projects"]]
    assert ps == sorted(ps, reverse=True)
    rep = {"ranking": table}
    lines = ranking_lines(rep)
    assert lines[0].startswith("Ranking, top bullets") and len([ln for ln in lines if ln.startswith("  ")]) == 10 + len(table["projects"])
    assert any("*" in ln for ln in lines) or not any(b["chosen"] for b in table["bullets"])
    assert ranking_lines({}) == []


# ------------------------------------------------------------------------------------------ fact-bank order inside a role
def test_chosen_bullets_are_printed_in_fact_bank_order_not_in_the_order_they_were_picked():
    fb = _bank()
    az = [b["id"] for b in fb["experience"][0]["bullets"]]
    sel = {"experience": {e["id"]: [] for e in fb["experience"]}, "projects": ["churn"],
           "project_bullets": {"churn": ["churn3", "churn1"]}}
    sel["experience"]["az"] = [az[3], az[0], az[2]]                                        # picked best-first, out of order
    ctx = tailor._context(fb, sel, {}, "profile", [], "Data Scientist", {"Core": ["Python"]})
    entry = next(e for e in ctx["experience"] if e["id"] == "az")
    assert entry["bullet_ids"] == [az[0], az[2], az[3]]
    assert [b for b in entry["bullets"]] == [next(b["text"] for b in fb["experience"][0]["bullets"] if b["id"] == i)
                                              for i in entry["bullet_ids"]]
    assert ctx["projects"][0]["bullet_ids"] == ["churn1", "churn3"]


def test_a_bullet_added_by_the_fill_goes_to_its_place_in_the_fact_bank_order():
    pos = {"a1": 0, "a2": 1, "a3": 2, "a4": 3}
    e = {"bullet_ids": ["a1", "a4"], "bullets": ["one", "four"]}
    tailor._insert(e, "a3", "three", pos)
    tailor._insert(e, "a2", "two", pos)
    assert e["bullet_ids"] == ["a1", "a2", "a3", "a4"] and e["bullets"] == ["one", "two", "three", "four"]
    tailor._remove(e, "a2")                                            # the take-back removes by id, not "the last one"
    assert e["bullet_ids"] == ["a1", "a3", "a4"] and e["bullets"] == ["one", "three", "four"]


def test_the_context_carries_the_results_the_render_prints_in_bold():
    fb = _bank()
    sel = select_cv(fb, [], layout=TWO_PAGE)
    ctx = tailor._context(fb, sel, {}, "p", [], "x", {"Core": ["Python"]})
    expected = {b["id"]: " ".join(str(b["result"]).split()) for sec in ("experience", "projects") for e in fb[sec]
                for b in e["bullets"] if b.get("result")}
    assert ctx["results"] == expected


# --------------------------------------------------------------------------------------------- an award is not in a bullet
def _award_bank():
    fb = _bank()
    ll = next(p for p in fb["projects"] if p["id"] == "lloyds")
    ll["award"] = "3rd place"
    ll["bullets"][0]["result"] = "3rd place; 200:1 class imbalance"
    return fb, ll


def test_a_result_that_contains_the_award_is_offered_without_it():
    fb, ll = _award_bank()
    b = ll["bullets"][0]
    b["text"] = "Built an anomaly detector on transactions with Isolation Forest, tracked in MLflow."
    assert tailor.unused_result(b) == "3rd place; 200:1 class imbalance"
    assert tailor.unused_result(b, "3rd place") == "200:1 class imbalance"                 # the heading shows the award
    assert tailor.award_of(fb)[b["id"]] == "3rd place" and "az1" not in tailor.award_of(fb)


def test_a_rewording_that_repeats_the_award_in_the_bullet_is_refused():
    fb, ll = _award_bank()
    b = ll["bullets"][0]
    bad = "Built an anomaly detector on 200:1 class-imbalanced transactions with Isolation Forest and MLflow, placing 3rd place."
    ok = "Built an anomaly detector on 200:1 class-imbalanced transactions with Isolation Forest and ensemble classifiers in MLflow."
    accepted, violations, _ = tailor.guard_rephrases(fb, {b["id"]: bad}, [])
    assert b["id"] not in accepted and any("award" in v for v in violations)
    accepted, violations, _ = tailor.guard_rephrases(fb, {b["id"]: ok}, [])
    assert b["id"] in accepted and not violations


# --------------------------------------------------------------------------- unsupported qualifiers in the profile
def test_proven_extensive_expert_and_passionate_are_unsupported_qualifiers():
    for w in ("proven", "extensive", "expert", "passionate"):
        assert w in QUALIFIERS and w in QUALIFIER_LIST and w in prompt_list("")
    assert unsupported_qualifiers("A passionate expert with proven, extensive skills", "Data Scientist with an MSc") == \
        ["extensive", "proven", "expert", "passionate"]
    assert unsupported_qualifiers("A passionate expert", "Passionate about data; expert in none") == []
    assert unsupported_qualifiers("Expertise in pandas", "pandas") == []                  # only the exact words
    assert unsupported_qualifiers("A hands-on engineer", "") == ["hands-on"]


HEADLINE = "Data Scientist"
FACTS = ["MSc Data Science, University of Northfield (completed 2026)",
         "Production LLM agent work on a clinical research platform",
         "Background in mobile FinTech engineering before data science"]


def test_the_profile_is_the_users_own_sentences_so_no_model_adjective_can_reach_it(fb_fixture):
    """A model once called the candidate "expert" and "passionate"; the profile is assembled from the hook line and the bank's own
    profile_facts now, and a fact that says "Expert in ..." is the user's own word."""
    from careerops.tailoring.profile import assemble

    fb = dict(fb_fixture)
    fb["profile_facts"] = [FACTS[0], "Expert in production LLM agent work on a clinical research platform", FACTS[2]]
    p = assemble(fb, [Keyword("llm", 2, True)], "", "Data scientist who ships models.")
    assert p["text"].startswith("Data scientist who ships models. ") and "Expert in production LLM agent work" in p["text"]
    assert "passionate" not in p["text"].lower() and p["violations"] == []


# ------------------------------------------------------------------------------------------------------------ skills rows
def test_the_databases_row_is_kept_whenever_the_jd_mentions_sql_or_databases():
    fb = _bank()
    fb["skills"]["Databases"] = ["PostgreSQL", "MongoDB"]
    kws = [Keyword("pytorch", 2, True)]                                                    # ranks the ML rows above Databases
    rows, dropped = cv_skills(fb, kws, 2, "Build models with PyTorch.")
    assert "Databases" in dropped and "Databases" not in rows
    for jd in ("You will write SQL daily.", "Experience with databases.", "PostgreSQL and NoSQL stores", "MySQL"):
        rows, dropped = cv_skills(fb, kws, 2, jd)
        assert "Databases" in rows and "Databases" not in dropped and len([g for g in rows if g != "Core"]) == 2, jd
    rows, _ = cv_skills(fb, kws, None, "Build models with PyTorch.")
    assert "Databases" in rows                                                              # no limit: every row is shown


@pytest.mark.parametrize("jd,wants", [("SQL", True), ("sql skills", True), ("PostgreSQL", True), ("NoSQL", True), ("databases", True),
                                      ("a database", True), ("RDBMS", True), ("Build with PyTorch", False), ("sequel", False)])
def test_what_counts_as_mentioning_sql_or_databases(jd, wants):
    assert wants_databases(jd) is wants
    assert is_database_row("Databases") and is_database_row("Databases & Storage") and not is_database_row("Cloud")


def test_the_review_screen_lines_for_typography():
    rep = {"typography": {"em_dashes": 0, "prose_en_dashes": [], "hyphens": {"pre-trained": 3, "x-y": 1}, "kept": ["end-to-end"],
                          "results_bold": 4, "results_unmatched": ["az5"], "award_repeats": []}}
    lines = typography_lines(rep)
    assert lines[0] == "Typography: no em dashes | 4 result(s) in bold"
    assert any("pre-trained x3, x-y" in ln for ln in lines) and any("end-to-end" in ln for ln in lines)
    assert any("az5" in ln for ln in lines)
    bad = typography_lines({"typography": {"em_dashes": 2, "hyphens": {}, "award_repeats": ["ll1"], "prose_en_dashes": ["a – b"]}})
    assert "!! 2 EM DASH(ES) LEFT" in bad[0] and any("ll1" in ln for ln in bad) and any("en dash" in ln for ln in bad)
    assert typography_lines({}) == []
