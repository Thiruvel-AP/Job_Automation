"""v3.9.4 (LSEG run): the fill never prints a 4th project; the problem sentence states a problem, not a list of teams; the question may use neutral
verbs; the Skills label is cased like the letter slots."""
from careerops.tailoring import reqmap, tailor
from careerops.tailoring import template_letter as TL
from careerops.tailoring.selection import MAX_PROJECTS, settle_selection

LSEG = "Your team will work closely with software engineers, SREs, product managers, platform teams and business stakeholders."
AD = ("Your team is bringing generative AI tools into production for financial data users. We care about model quality before release. "
      "You will work closely with software engineers, SREs, product managers, platform teams and business stakeholders.")


def _bank(n=5):
    return {"experience": [], "projects": [{"id": f"p{j}", "bullets": [{"id": f"p{j}b{i}", "text": "x"} for i in range(2)]} for j in range(n)]}


# ------------------------------------------------------------------ 1. project cap
def test_a_selection_with_the_hook_project_and_three_chosen_ones_is_cut_to_three_and_keeps_the_hook_project():
    sel = {"experience": {}, "projects": ["p4", "p0", "p1", "p2"], "project_bullets": {}}
    out, _ = settle_selection(_bank(), sel, protect="p4b0")
    assert out["projects"] == ["p4", "p0", "p1"] and len(out["projects"]) <= MAX_PROJECTS


def test_a_typed_fact_project_is_not_the_one_dropped():
    fb = _bank()
    fb["projects"][3]["bullets"][0]["provisional"] = True
    out, _ = settle_selection(fb, {"experience": {}, "projects": ["p0", "p1", "p2", "p3"], "project_bullets": {}})
    assert out["projects"] == ["p0", "p1", "p3"]


def test_short_page_with_the_cap_reached_is_reported_as_such(monkeypatch, tmp_path):
    fb = _bank()
    ctx = {"experience": [], "projects": [{**p, "bullet_ids": [p["bullets"][0]["id"]], "bullets": ["x"]} for p in fb["projects"][:3]]}
    monkeypatch.setattr(tailor, "_used", lambda pdf, pages: 1.2)
    monkeypatch.setattr(tailor, "page2_fill", lambda pdf, pages=None: 0.2)
    stats = tailor.fill_page2(ctx, fb, ["p3b0"], {}, lambda: (tmp_path / "cv.pdf", 2), 0.96, max_projects=3)
    assert len(ctx["projects"]) == 3 and stats["project_cap_hit"] is True
    assert tailor.fill_shortfall({**stats, "reached": False}, 0.2, 0.96) == "Fill: page 2 20% (project cap 3 reached)"


# ------------------------------------------------------------------ 2. problem sentence
def test_the_lseg_list_of_teams_is_rejected_and_the_fallback_renders():
    ad = [AD.split(". ")[2] + "."]
    chk = TL.problem_sentence_problems(LSEG, ad, "Lseg", AD, "Data Scientist")
    assert any("lists job titles" in p for p in chk) and any("names no goal" in p for p in chk)
    assert TL.fallback("company_problem_sentence", {"company": "Lseg", "title": "Data Scientist"}, {"ad_requirement": "generative AI tools"}) \
        == "Your team is working on generative AI tools."


def test_a_problem_sentence_with_a_goal_and_no_list_passes():
    s = "Your team is bringing generative AI tools into production for financial data users."
    assert TL.problem_sentence_problems(s, [s], "Lseg", AD, "Data Scientist") == []


# ------------------------------------------------------------------ 3. question verbs
def test_a_question_with_a_neutral_verb_passes_when_the_noun_phrase_is_in_the_ad():
    ad = "We care about model quality before release."
    assert TL.question_problems("how the team evaluates model quality before release", ad, "Lseg", "Data Scientist") == []
    assert any("words the ad does not" in p for p in TL.question_problems("how the team orchestrates quantum pipelines", ad, "Lseg", "Data Scientist"))


# ------------------------------------------------------------------ 4. label casing
def test_the_skills_label_renders_mlops():
    assert reqmap._phrase_label("mlops") == "MLOps"
    assert reqmap._phrase_label("python and mlops", TL.bank_casing({"skills": {"core": ["Python"]}})) == "Python and MLOps"
