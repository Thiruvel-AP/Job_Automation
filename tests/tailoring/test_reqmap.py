"""v3.6: JD-first assembly. The ad's requirements are mapped to the fact bank's evidence (tailoring/reqmap.py) and that map, not the role family,
picks the hook, the profile fragments, the Skills rows, the bullets and the letter template. A fictional bank built here; no network, no model."""
import copy
import json

from careerops import hook as hook_mod
from careerops.tailoring import reqmap as RM
from careerops.tailoring import skills as skills_mod
from careerops.tailoring import tailor as T
from careerops.tailoring import template_letter as TL
from careerops.tailoring.scorer import Keyword

DS_LETTER = """Hi {company} team,

{company_problem_sentence} That is the kind of problem I have been working on. I build the model a brief needs and report its limits plainly, with XGBoost and hypothesis testing.

Your ad asks for {ad_requirement}, which is the work I want to do next.

Could we spend 20 minutes on {question}?

Regards,
Test Person
"""
AI_LETTER = DS_LETTER.replace("I build the model a brief needs and report its limits plainly, with XGBoost and hypothesis testing.", "I take LLM agents from prototype to deployment.")

BANK = {
    "identity": {"name": "Test Person", "headline": "Data Scientist"},
    "deny_terms": ["terraform", "dbt"],
    "profile_facts": ["Turns statistical outputs into clear recommendations for stakeholders"],
    "skills": {"ML & Statistics": ["scikit-learn", "XGBoost", "hypothesis testing", "ANOVA", "descriptive statistics (mean, variance)"],
               "Languages": ["Python", "SQL"]},
    "experience": [
        {"id": "az", "org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project", "bullets": [
            {"id": "az5", "text": "Surveyed and explored 14 heterogeneous source datasets with descriptive statistics and correlation analysis.",
             "result": "14 source datasets", "terms": ["eda", "exploratory data analysis"]},
            {"id": "az6", "text": "Integrated the agent into a React front end with real-time enrichment.", "terms": ["react", "frontend"]}]}],
    "projects": [
        {"id": "ql", "name": "QLoRA", "bullets": [
            {"id": "p_ql2", "text": "Audited all 859,494 training rows for completeness, structure and duplicates (44,369 excess duplicate rows found).",
             "result": "859,494 rows audited", "terms": ["data quality", "data audit", "deduplication"]}]},
        {"id": "sc", "name": "SCEM", "bullets": [
            {"id": "p_sc1", "text": "Sized an experiment with a power analysis and chose the best of 6 algorithm configurations with ANOVA (Cohen's d = 3.78).",
             "terms": ["anova", "power analysis", "hypothesis testing"]}]},
        {"id": "ch", "name": "Churn", "bullets": [{"id": "p_ch1", "text": "Built an XGBoost churn classifier with Bayesian tuning.", "terms": ["xgboost"]}]},
        {"id": "ll", "name": "Lloyds", "bullets": [{"id": "p_ll1", "text": "Placed 3rd at a datathon on anomaly detection.", "terms": ["anomaly detection"]}]}],
    "education": [{"degree": "MSc Data Science", "modules": ["Statistical Computing & Empirical Methods"]}],
    "certifications": ["AWS Academy Cloud Foundations, Nov 2025"],
    "hook_library": [
        {"id": "h_stats", "proof": ["p_sc1"], "terms": ["anova", "power analysis"], "headline": "Chose the best of 6 configurations by ANOVA, d = 3.78",
         "profile_line": "Data scientist who tests before concluding: used ANOVA to choose the best of 6 algorithm configurations (Cohen's d = 3.78).",
         "letter_line": "I sized an experiment with a power analysis and used ANOVA to choose the best of 6 algorithm configurations."},
        {"id": "h_data_quality", "proof": ["p_ql2", "az5"], "terms": ["data quality", "eda"],
         "headline": "Audited 859,494 training rows for quality and duplicates",
         "profile_line": "Data scientist who checks the data before the model: audited 859,494 training rows for completeness and duplicates.",
         "letter_line": "I audited 859,494 training rows for completeness and duplicates, finding 44,369 excess duplicate rows."}],
    "jd_aliases": {"data analysis": ["eda", "exploratory data analysis", "explored"], "exploration": ["explored", "eda"],
                   "statistical analyses": ["hypothesis testing", "anova", "descriptive statistics", "correlation analysis"],
                   "data science models": ["xgboost", "classification", "regression"]},
    "profile_fragments": [
        {"id": "pf_models", "terms": ["data science models", "xgboost"], "text": "Builds the modelling a brief needs: an XGBoost churn classifier with Bayesian tuning."},
        {"id": "pf_dq", "terms": ["data analysis", "data quality", "exploration"], "text": "Checks the data before the model: audited 859,494 training rows for completeness and duplicates."},
        {"id": "pf_dq_again", "terms": ["data quality"], "text": "Checks the data before the model: audited 859,494 training rows for completeness and more."},
        {"id": "pf_unrelated", "terms": ["react"], "text": "Builds React front ends."}],
    "families": [
        {"id": "ai_engineer", "title": "AI Engineer", "match": ["llm", "agent"], "letter_template": AI_LETTER,
         "skill_lines": [{"label": "Agents", "items": "LLM agents, RAG, Python"}]},
        {"id": "data_scientist", "title": "Data Scientist", "match": ["machine learning"], "letter_template": DS_LETTER,
         "skill_lines": [{"label": "ML", "items": "scikit-learn, XGBoost, SQL, AWS"}, {"label": "Cloud", "items": "AWS, Docker (Kubernetes)"}]}],
}

# an EDITED-like ad: data analysis, exploration and data quality, with React as a minor ask and a terraform gap
EDITED = [Keyword("data analysis", 3.0, True), Keyword("exploration", 2.0, False), Keyword("data quality", 2.0, True),
          Keyword("react", 0.5, False), Keyword("terraform", 1.0, False), Keyword("structured outputs", 1.0, False)]
STATS = [Keyword("statistical analyses", 3.0, True), Keyword("data science models", 2.0, True), Keyword("python", 1.0, False)]


def bank(**drop):
    fb = copy.deepcopy(BANK)
    for k in drop:
        fb.pop(k, None)
    return fb


def by_term(rm):
    return {r.term: r for r in rm.reqs}


# ---------------------------------------------------------------------------------------------------------- the map
def test_edited_like_ad_maps_data_analysis_exploration_quality_to_bullets():
    rm = by_term(RM.build(EDITED, bank()))
    assert rm["data analysis"].bucket == rm["exploration"].bucket == rm["data quality"].bucket == "existing"
    assert "az5" in rm["data analysis"].bullet_ids() and "az5" in rm["exploration"].bullet_ids()
    assert "p_ql2" in rm["data quality"].bullet_ids()
    assert rm["exploration"].evidence[0].quote.lower() == "explored"          # the exact words of the bullet, not the alias as typed


def test_gap_is_a_gap_and_a_deny_listed_term_is_always_one():
    fb = bank()
    fb["skills"]["Cloud"] = ["Terraform"]                                      # the bank even lists it: the deny list wins
    rm = by_term(RM.build(EDITED, fb))
    assert rm["structured outputs"].bucket == "gap" and rm["structured outputs"].evidence == []
    assert rm["terraform"].bucket == "gap" and rm["terraform"].evidence == []


def test_coverage_is_existing_plus_half_of_supported_over_all_weight():
    rm = RM.build(EDITED, bank())
    total = sum(k.weight for k in EDITED)
    expect = (sum(r.weight for r in rm.reqs if r.bucket == "existing") + 0.5 * sum(r.weight for r in rm.reqs if r.bucket == "supported")) / total
    assert abs(rm.coverage - expect) < 1e-9 and 0 < rm.coverage < 1


def test_statistical_analyses_maps_through_jd_aliases_without_them_it_is_a_gap_and_nothing_is_written():
    fb = bank()
    fb["experience"][0]["bullets"] = [fb["experience"][0]["bullets"][1]]       # no bullet says it: only skills / facts do
    fb["projects"] = fb["projects"][2:]
    r = by_term(RM.build(STATS, fb))["statistical analyses"]
    assert r.bucket == "supported" and {e.kind for e in r.evidence} == {"skills"}
    assert {"hypothesis testing", "ANOVA"} <= {e.quote for e in r.evidence}
    no_alias = copy.deepcopy(fb)
    del no_alias["jd_aliases"]
    rm = RM.build(STATS, no_alias)
    assert by_term(rm)["statistical analyses"].bucket == "gap"
    rows = RM.skill_rows(rm, no_alias, {})
    assert rows is None or not any("statistical analyses" in k.lower() for k in rows)       # nothing is written for a gap


def test_skills_label_uses_the_jd_phrase_and_lists_only_evidence_words():
    fb = bank()
    rm = RM.build(STATS, fb)
    rows = RM.skill_rows(rm, fb, {"ML": "scikit-learn, XGBoost, SQL, AWS", "Cloud": "AWS, Docker (Kubernetes)"})
    assert rows and list(rows)[0] == "Statistical analyses"
    assert rows["Statistical analyses"].split(", ")[:2] == ["hypothesis testing", "ANOVA"]
    assert "XGBoost" in " ".join(rows.values())                                # a family fallback row, the user's own line
    assert 2 <= len(rows) <= 4 and not skills_mod.row_repeats(rows)             # a skill appears once on the whole section
    assert "AWS" in " ".join(rows.values())                                    # the family's own lines are the fallback rows


def test_gap_term_never_reaches_cv_letter_or_profile_text():
    fb = bank()
    rm = RM.build(EDITED, fb)
    used = " ".join([*RM.skill_rows(rm, fb, {}).values(), *(f["text"] for f in RM.pick_fragments(rm, fb))]).lower() if RM.skill_rows(rm, fb, {}) else \
        " ".join(f["text"] for f in RM.pick_fragments(rm, fb)).lower()
    assert "terraform" not in used and "structured outputs" not in used
    assert "terraform" in " ".join(RM.gap_lines(rm)) and "structured outputs" in " ".join(RM.gap_lines(rm))


# ---------------------------------------------------------------------------------------------------------- the hook
def test_hook_whose_proof_bullets_cover_the_most_existing_weight_wins():
    fb = bank()
    rm = RM.build(EDITED, fb)
    hk = hook_mod.build_hook({"description": ""}, EDITED, fb, rm=rm)
    assert hk is not None and hk.hook_id == "h_data_quality"
    assert {t for t, _ in hk.overlap} == {"data analysis", "exploration", "data quality"}


def test_a_hook_must_cover_at_least_two_existing_requirements():
    fb = bank()
    one = [Keyword("data quality", 3.0, True), Keyword("terraform", 1.0, False)]
    notes: list[str] = []
    assert hook_mod.build_hook({"description": ""}, one, fb, notes=notes, rm=RM.build(one, fb)) is None
    assert any("at least 2" in n for n in notes)
    best = RM.best_proof_bullet(RM.build(one, fb), fb)                         # the no-hook profile line 1
    assert best["id"] == "p_ql2"


# ---------------------------------------------------------------------------------------------------------- profile, bullets, letter
def test_profile_uses_the_fragments_that_cover_the_ad_none_twice_none_unrelated():
    fb = bank()
    rm = RM.build(EDITED, fb)
    ids = [f["id"] for f in RM.pick_fragments(rm, fb)]
    assert ids[0] == "pf_dq" and "pf_unrelated" not in ids or "pf_unrelated" not in ids[:1]
    assert "pf_dq_again" not in ids                                            # it repeats pf_dq: no fragment repeated
    hook_line = fb["hook_library"][1]["profile_line"]
    assert "pf_dq" not in [f["id"] for f in RM.pick_fragments(rm, fb, avoid=hook_line)]     # nor the hook's own line again
    assert RM.pick_fragments(RM.build([Keyword("terraform", 1.0, False)], fb), fb) == []     # nothing covered: the caller uses profile_body


def test_a_zero_coverage_bullet_is_never_first_and_is_dropped_before_a_project():
    fb = bank()
    rm = RM.build(EDITED[:3], fb)
    scores = RM.blend({"az5": 0.0, "az6": 0.0, "p_ql2": 0.0, "p_sc1": 0.0, "p_ch1": 0.0, "p_ll1": 0.0}, rm)
    assert scores["az6"] == 0 and scores["az5"] > 0
    ctx = {"experience": [{"id": "az", "bullet_ids": ["az6", "az5"], "bullets": ["react", "explored"], "dates": "May 2026 – Sep 2026"}],
           "projects": [{"id": "ql", "bullet_ids": ["p_ql2"], "bullets": ["audited"]}, {"id": "sc", "bullet_ids": ["p_sc1"], "bullets": ["anova"]},
                        {"id": "ch", "bullet_ids": ["p_ch1"], "bullets": ["xgboost"]}]}
    T.lead_best(ctx, scores)
    assert ctx["experience"][0]["bullet_ids"][0] == "az5"                      # the react bullet (0 coverage) is not first
    assert T._shrink(ctx, EDITED[:3], T.ONE_PAGE, None, scores)
    assert len(ctx["projects"]) == 3 and "az6" not in ctx["experience"][0]["bullet_ids"]      # it went before any project


def test_selection_credits_a_bullet_for_the_requirement_it_is_evidence_of():
    from careerops.tailoring import selection as S

    fb = bank()
    rm = RM.build(STATS, fb)
    hits = RM.bullet_coverage(rm)
    pk = S._Picker(fb, STATS, {}, 0.8, "", hits)
    assert 0 in pk.hits["p_sc1"]                                               # "statistical analyses" through the alias "anova"
    assert 0 not in S._Picker(fb, STATS, {}, 0.8, "").hits["p_sc1"]            # not without the map


def test_the_same_ad_always_gives_the_same_map_and_choices():
    fb = bank()
    a, b = RM.build(EDITED, fb), RM.build(EDITED, copy.deepcopy(fb))
    assert a.to_dict() == b.to_dict()
    assert RM.bullet_scores(a) == RM.bullet_scores(b)
    assert [f["id"] for f in RM.pick_fragments(a, fb)] == [f["id"] for f in RM.pick_fragments(b, fb)]
    assert RM.skill_rows(RM.build(STATS, fb), fb, {}) == RM.skill_rows(RM.build(STATS, fb), fb, {})


def test_the_map_is_stored_as_reqmap_json(tmp_path):
    rm = RM.build(EDITED, bank())
    path = RM.write(rm, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "reqmap.json" and data["coverage"] == round(rm.coverage, 4)
    row = next(r for r in data["requirements"] if r["term"] == "data analysis")
    assert row["bucket"] == "existing" and row["evidence"][0]["id"] == "az5" and row["evidence"][0]["quote"]
    assert RM.ReqMap.from_dict(data).to_dict() == data


def test_review_block_lines():
    rm = RM.build(EDITED, bank())
    text = "\n".join(RM.review_block(rm, RM.risk_map(rm, "Data Scientist", sections={"profile": "audited data quality"}), RM.alias_suggestions(rm, bank())))
    assert text.startswith(f"REQUIREMENTS (coverage {rm.coverage:.2f})")
    assert 'existing az5 "Surveyed and explored 14…"' in text and "GAP structured outputs" in text
    assert "recruiter risk map" in text and "unanswered" in text


# ---------------------------------------------------------------------------------------------------------- title and letter template
def test_the_ad_title_names_the_family_not_the_body_words():
    fb = bank()
    kws = [Keyword("llm", 5.0, True), Keyword("agent", 5.0, True)]            # an ad body that screams ai_engineer
    assert skills_mod.pick_family(fb, kws, None, None, "Junior Data Scientist")["id"] == "data_scientist"
    assert skills_mod.pick_family(fb, kws, None, None, "Senior AI Engineer")["id"] == "ai_engineer"
    assert skills_mod.pick_family(fb, kws, None, None, "Analyst")["id"] == "ai_engineer"        # no title match: the old rule stands
    assert RM.title_family(fb, "AI Engineer / Data Scientist") is None                    # two families named: no decision from the title


def test_data_scientist_template_renders_the_data_scientist_text_regression_14_nov():
    """14 Nov: the log said template 'data_scientist' while the letter body was the ai_engineer text."""
    fb = bank()
    ds_ad = [Keyword("data science models", 3.0, True), Keyword("statistical analyses", 2.0, True)]
    rm = RM.build(ds_ad, fb)
    order = RM.template_order(rm, fb, ["ai_engineer", "data_scientist"])        # the role family said ai_engineer, the ad is a DS ad
    assert order[0] == "data_scientist"
    t = TL.find(fb, order)
    assert t.family == "data_scientist" and "report its limits plainly" in " ".join(t.paragraphs) and "LLM agents" not in " ".join(t.paragraphs)
    body = TL.fill(t, "Acme", {"company_problem_sentence": "You need models.", "ad_requirement": "statistical analyses", "question": "your models"})
    line = T.letter_template_line({"compose": "template", "template_family": t.family, "paragraphs": body})
    assert line.startswith("letter template: data_scientist") and 'body starts "You need models. That is the kind of problem' in line
    ai = RM.build([Keyword("llm", 3.0, True), Keyword("agents", 2.0, True)], fb)


def test_template_order_prefers_the_template_claiming_the_most_requirement_weight():
    fb = bank()
    fb["families"][0]["letter_template"] = AI_LETTER.replace("take LLM agents from prototype to deployment", "take an LLM agent with RAG to production")
    ai = RM.build([Keyword("rag", 3.0, True), Keyword("llm", 2.0, True), Keyword("python", 1.0, False)], fb)
    # nothing in the bank backs "rag" or "llm" by evidence, so both are gaps and the title's family (first in the list) decides
    assert RM.template_order(ai, fb, ["data_scientist", "ai_engineer"])[0] == "data_scientist"


# ---------------------------------------------------------------------------------------------------------- doctor
def test_doctor_flags_dead_aliases_and_repeating_fragments():
    fb = bank()
    fb["jd_aliases"]["structured outputs"] = ["json schema"]                   # no such words anywhere in the bank
    dead = RM.dead_aliases(fb)
    assert len(dead) == 1 and "structured outputs" in dead[0]
    rep = RM.fragment_repeats(fb)
    assert any("pf_dq" in r and "pf_dq_again" in r for r in rep)
    assert RM.dead_aliases(bank()) == []


def test_alias_suggestions_are_marked_and_never_for_deny_listed_terms():
    fb = bank()
    kws = [Keyword("structured explorations", 1.0, False), Keyword("terraform", 1.0, False)]
    out = RM.alias_suggestions(RM.build(kws, fb), fb)
    assert all("suggestion" in s for s in out) and not any("terraform" in s for s in out)
