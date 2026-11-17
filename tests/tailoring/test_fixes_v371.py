"""v3.7.1 (16 Nov 2026): fixes from the Sirius Analysis dry run (job manual:2f46779b6f54). One section per bug, each reproduced here first.
Fictional banks only; no network, no real model (MockLLM). The two end-to-end tests render PDFs with xelatex."""
import copy

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import layout, plain as plain_mod, reqmap as RM, selection, tailor
from careerops.tailoring import template_letter as TL
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.skills import line_items


def kws(*terms, weight=2.0):
    return [Keyword(t, weight, True) for t in terms]


# ============================================================================================ 1. retired bullets never print
OLD = "Built forecasting pipelines in python with pandas and mlflow on sql data."
NEW = "Built forecasting pipelines in python."


def retired_bank(replacer_entry: str = "e") -> dict:
    """Entry e: `old` (scores high for the ad) and `new`, which replaces it (scores lower). `replacer_entry` e2 puts the replacer in another entry."""
    new = {"id": "new", "text": NEW, "terms": [], "replaces": ["old"]}
    e = {"id": "e", "title": "t", "org": "o", "dates": "Jan 2026 – Feb 2026", "bullets": [{"id": "old", "text": OLD, "terms": []}]}
    e2 = {"id": "e2", "title": "t2", "org": "o2", "dates": "Jan 2025 – Feb 2025", "bullets": [{"id": "other", "text": "Cleaned data.", "terms": []}]}
    (e if replacer_entry == "e" else e2)["bullets"].append(new)
    return {"identity": {"name": "A"}, "profile_facts": [], "skills": {}, "projects": [], "education": [], "certifications": [],
            "experience": [e, e2]}


KW5 = kws("forecasting", "python", "pandas", "mlflow", "sql")


def test_a_retired_bullet_that_scores_higher_than_its_replacement_still_prints_the_replacement():
    fb = retired_bank()
    sc = selection.relevance_scores(fb, KW5, "")
    assert sc["old"] > 1.25 * sc["new"]                                        # far below the old 80 % rule: the replacement was "passed over"
    for layout_ in (selection.Layout((1, 2), (1, 2), 0, None), selection.TWO_PAGE, selection.ONE_PAGE):
        sel = selection.select_cv(fb, KW5, None, skills={}, layout=layout_)
        assert sel["experience"]["e"] == ["new"], layout_
        assert "old" not in selection.selected_ids(fb, sel)


def test_the_fill_and_the_settle_step_never_bring_a_retired_bullet_back():
    fb = retired_bank()
    sel = selection.select_cv(fb, KW5, None, skills={}, layout=selection.Layout((1, 2), (1, 2), 0, None))
    assert "old" not in selection.fill_order(fb, KW5, sel, {}, selection.JACCARD, 0, "", selection.TWO_PAGE)
    both = {"experience": {"e": ["old", "new"], "e2": ["other"]}, "projects": []}
    assert selection.settle_selection(fb, both)[0]["experience"]["e"] == ["new"]


def test_a_bullet_another_entry_replaces_is_not_retired():
    fb = retired_bank(replacer_entry="e2")                                     # the replacer sits in e2: `old` has no replacer in its own entry
    assert selection.retired_ids(fb) == frozenset()
    sel = selection.select_cv(fb, KW5, None, skills={}, layout=selection.Layout((1, 2), (1, 2), 0, None))
    assert sel["experience"]["e"] == ["old"]


def test_a_retired_bullet_still_counts_as_evidence_in_the_requirement_map():
    fb = retired_bank()
    fb["experience"][0]["bullets"][0]["terms"] = ["mlflow"]
    rm = RM.build(kws("mlflow"), fb)
    req = rm.reqs[0]
    assert req.bucket == "existing" and "old" in req.bullet_ids()              # the REQUIREMENTS block still names it
    sel = selection.select_cv(fb, kws("mlflow"), None, skills={}, layout=selection.Layout((1, 2), (1, 2), 0, None), extra_hits=RM.bullet_coverage(rm))
    assert sel["experience"]["e"] == ["new"]                                   # but the printed bullet is the one that replaces it


def test_a_hook_proof_keeps_its_bullet_only_when_the_replacement_lacks_the_hooks_number():
    """The narrow conflict: the hook's proof bullet is retired and its replacement does not say the hook's number. The hook is the first bullet by
    rule, so the proof stays (and a note says so); with the number in the replacement it takes over, whatever it scores."""
    from careerops import hook as H

    fb = retired_bank()
    fb["experience"][0]["bullets"][0]["text"] = "Built 7 forecasting pipelines in python."
    hk = H.Hook(problem="", proof_bullet_id="old", number="7", headline="Built 7 pipelines", profile_line="Built 7 forecasting pipelines.",
                letter_sentence="I built 7 forecasting pipelines.", entry_id="e")
    sel = {"experience": {"e": ["old"], "e2": ["other"]}, "projects": []}
    out, got, notes = tailor.reconcile_proof(fb, sel, hk, kws("forecasting"), "")
    assert got.proof_bullet_id == "old" and out["experience"]["e"] == ["old"]
    fb["experience"][0]["bullets"][1]["text"] = "Built 7 forecasting pipelines."
    out, got, notes = tailor.reconcile_proof(fb, sel, hk, kws("forecasting"), "")
    assert got.proof_bullet_id == "new" and out["experience"]["e"] == ["new"] and any("new" in n for n in notes)


# ============================================================================================ 2. no situation prefix on a plain line
SIT = "Needed structure in unstructured airline support tickets"
PLAIN_LINE = "Built the text-cleaning and grouping steps for airline support tickets and traced weak groupings to repeated wording."


def entry_with_plain() -> dict:
    return {"id": "iata", "name": "Mining Insights", "situation": SIT,
            "bullets": [{"id": "p_ia1", "text": "Designed the preprocessing pipeline and the clustering pipeline (K-Means, HAC, DBSCAN).",
                         "plain": PLAIN_LINE, "terms": []}]}


def shown(entry: dict, ids: list[str], texts: dict[str, str]) -> dict:
    return {**entry, "situation_owner": layout.situation_owner(entry), "bullet_ids": list(ids), "bullets": [texts[i] for i in ids]}


def test_a_bullet_with_both_a_situation_and_plain_prints_the_plain_text_unchanged():
    e = entry_with_plain()
    ctx = {"experience": [], "projects": [shown(e, ["p_ia1"], {"p_ia1": PLAIN_LINE})], "plain": ["p_ia1"]}
    assert layout.with_openers(ctx)["projects"][0]["bullets"] == [PLAIN_LINE]
    assert layout.opener_words(ctx["projects"][0], 0, (), set(ctx["plain"])) == 0
    ctx = {"experience": [], "projects": [shown(e, ["p_ia1"], {"p_ia1": PLAIN_LINE})]}          # the same text, not shown as plain: the opener still goes on
    assert layout.with_openers(ctx)["projects"][0]["bullets"][0].startswith(SIT + ": ")


def test_the_render_context_marks_the_bullets_that_print_their_plain_line():
    fb = {"identity": {"name": "A"}, "profile_facts": [], "skills": {}, "education": [], "certifications": [], "experience": [],
          "projects": [entry_with_plain()]}
    pb, info = plain_mod.plain_bank(fb, [], cap=0)
    assert info["plain"] == ["p_ia1"]
    sel = {"experience": {}, "projects": ["iata"], "project_bullets": {"iata": ["p_ia1"]}}
    ctx = tailor._context(pb, sel, {}, "profile", [], "Data Scientist", {})
    assert ctx["plain"] == ["p_ia1"]
    assert layout.with_openers(ctx)["projects"][0]["bullets"] == [PLAIN_LINE]
    ctx = tailor._context(fb, sel, {}, "profile", [], "Data Scientist", {})                   # technical style: no plain list, the opener is added
    assert ctx["plain"] == [] and layout.with_openers(ctx)["projects"][0]["bullets"][0].startswith(SIT + ": ")


# ============================================================================================ 3. a refused plain line is never silent
OWN = "Contributed to a team LoRA fine-tune of a Llama model and served the models as REST APIs."


def refused_bank() -> dict:
    return {"id": "fl7", "text": OWN, "plain": "Built the team's fine-tuned model and served it as REST APIs.", "terms": []}


def style_ctx(bullets: list[dict], texts: dict[str, str]) -> dict:
    ids = [b["id"] for b in bullets]
    return {"id": {"headline": "DS"}, "profile": "", "skills": {}, "certifications": [], "projects": [],
            "experience": [{"id": "e", "title": "t", "org": "o", "bullet_ids": ids, "bullets": [texts[i] for i in ids]}]}


def test_a_plain_line_rejected_by_a_rule_shows_the_rule_in_the_style_report():
    fb = {"identity": {"name": "A"}, "experience": [{"id": "e", "title": "t", "org": "o", "bullets": [refused_bank()]}], "projects": []}
    pb, info = plain_mod.plain_bank(fb, [], cap=0)
    assert "fl7" in info["refused"] and info["plain"] == []
    rep = tailor.plain_style_report(style_ctx([refused_bank()], {"fl7": OWN}), info, [], [])
    why = rep["plain_skipped"]["fl7"]
    assert "contributed" in why and "ownership" in why                          # the rule's own message
    assert rep["plain_refused"]["fl7"] and "fl7" not in rep["plain_on_cv"]


def test_a_missing_plain_line_and_an_ats_fallback_are_named_too():
    fb = {"identity": {"name": "A"}, "experience": [{"id": "e", "title": "t", "org": "o", "bullets": [
        {"id": "a", "text": "Built forecasting pipelines in python.", "terms": []},
        {"id": "b", "text": "Cleaned the sales data with pandas and mlflow in 4 weeks.", "plain": "Cleaned the sales data in 4 weeks.", "terms": []}]}],
          "projects": []}
    pb, info = plain_mod.plain_bank(fb, kws("pandas", "mlflow"), cap=0)
    ctx = style_ctx([{"id": "a"}, {"id": "b"}], {"a": "Built forecasting pipelines in python.", "b": "Cleaned the sales data in 4 weeks."})
    rep = tailor.plain_style_report(ctx, info, kws("pandas", "mlflow", "forecasting", "python"), ["pandas", "mlflow", "forecasting", "python"])
    assert "no `plain:` line" in rep["plain_skipped"]["a"]
    assert set(rep["plain_skipped"]) >= {"a"} and all(v for v in rep["plain_skipped"].values())
    for b in rep["style_fallback"]:
        assert "ATS" in rep["plain_skipped"][b]


def test_the_review_screen_prints_the_rule_and_where_to_fix_it():
    from careerops.review import workflow

    rep = {"style": {"plain_skipped": {"fl7": "the saved plain line breaks a rule: drops the ownership word 'contributed'"}}}
    lines = workflow.plain_skipped_lines(rep)
    text = "\n".join(lines)
    assert "fl7" in text and "ownership word 'contributed'" in text and "fact_bank.yaml" in text
    assert workflow.plain_skipped_lines({"style": {"style": "technical"}}) == [] and workflow.plain_skipped_lines({}) == []


# ============================================================================================ 4. plain bullets are never sent to the rephraser
def rephrase_bank() -> dict:
    return {"identity": {"name": "A"}, "projects": [], "experience": [{"id": "e", "title": "t", "org": "o", "bullets": [
        {"id": "a", "text": "Built forecasting pipelines in python.", "result": "3 clients", "plain": "Built forecasting tools for 3 clients.",
         "terms": ["forecasting"]},
        {"id": "b", "text": "Cleaned the sales data with pandas.", "result": "4 weeks", "plain": "Cleaned the sales data in 4 weeks.",
         "terms": ["pandas"]}]}]}


def test_a_cv_where_every_bullet_has_plain_makes_no_rephrase_call():
    fb = rephrase_bank()
    sel = {"experience": {"e": ["a", "b"]}, "projects": []}
    llm = MockLLM()
    _, stats = tailor.propose_rephrases({"title": "DS"}, fb, sel, kws("forecasting", "pandas"), MockLLM(), {}, [])
    assert stats["eligible"] == 2 and stats["calls"] == 1                      # without `skip` both go to the model (each has a result the text lacks)
    proposed, stats = tailor.propose_rephrases({"title": "DS"}, fb, sel, kws("forecasting", "pandas"), llm, {}, [], skip={"a", "b"})
    assert proposed == {} and stats["calls"] == 0 and stats["eligible"] == 0 and llm.calls.count("rephrase_bullets") == 0
    _, stats = tailor.propose_rephrases({"title": "DS"}, fb, sel, kws("forecasting", "pandas"), llm, {}, [], skip={"a"})
    assert stats["eligible"] == 1 and stats["calls"] == 1                      # a bullet outside `skip` still goes to the model


def test_plain_ids_of_the_bank_are_every_bullet_with_a_plain_line_used_or_refused():
    fb = rephrase_bank()
    fb["experience"][0]["bullets"].append({"id": "c", "text": "Led 5 analysts.", "plain": "Helped 5 analysts.", "terms": []})   # refused: upgrades nothing, drops "Led"?
    assert tailor.plain_ids(fb) == {"a", "b", "c"}
    assert tailor.plain_ids({"experience": [{"bullets": [{"id": "z", "text": "x"}]}], "projects": []}) == set()


# ============================================================================================ 5. alias words are never printed as skills
ALIAS_BANK = {
    "identity": {"name": "T"}, "skills": {"ML": ["scikit-learn", "XGBoost", "hypothesis testing", "ANOVA", "descriptive statistics (mean, variance)"],
                                          "Languages": ["Python", "SQL"]},
    "experience": [{"id": "az", "org": "AZ", "title": "DS", "bullets": [
        {"id": "az5", "text": "Surveyed and explored 14 source datasets, with an exploratory data analysis and a data audit.", "result": "14 source datasets",
         "terms": ["eda"]}]}],
    "projects": [{"id": "ql", "name": "QL", "bullets": [
        {"id": "p_ql2", "text": "Audited all 859,494 training rows for data quality and duplicates.", "result": "859,494", "terms": ["data quality"]}]}],
    "education": [], "certifications": [],
    "jd_aliases": {"data analysis": ["eda", "exploratory data analysis", "data audit", "data quality", "explored"],
                   "statistical analyses": ["hypothesis testing", "anova", "descriptive statistics"]},
    "families": [{"id": "ds", "title": "DS", "match": ["python"], "skill_lines": [
        {"label": "ML", "items": "scikit-learn, XGBoost, SQL"}, {"label": "Cloud", "items": "AWS, Docker (Kubernetes)"},
        {"label": "Languages", "items": "Python, R"}]}],
}
FAMILY_LINES = {"ML": "scikit-learn, XGBoost, SQL", "Cloud": "AWS, Docker (Kubernetes)", "Languages": "Python, R"}
ALIAS_WORDS = ("eda", "exploratory data analysis", "data audit", "data quality", "explored")


def test_an_alias_value_that_is_not_a_skill_never_appears_in_the_rendered_skills_section():
    fb = copy.deepcopy(ALIAS_BANK)
    rm = RM.build([Keyword("data analysis", 3.0, True), Keyword("statistical analyses", 2.0, True), Keyword("python", 1.0, True)], fb)
    assert next(r for r in rm.reqs if r.term == "data analysis").bucket == "existing"        # the alias words ARE evidence (REQUIREMENTS block)
    rows = RM.skill_rows(rm, fb, FAMILY_LINES)
    text = " ".join(f"{k}: {v}" for k, v in (rows or {}).items()).lower()
    for word in ALIAS_WORDS:
        assert word not in text, (word, rows)
    assert "data analysis" not in [k.lower() for k in (rows or {})] and "explored" not in text


def test_a_row_named_with_the_ads_words_takes_its_items_only_from_skills_and_the_family_lines():
    fb = copy.deepcopy(ALIAS_BANK)
    rm = RM.build([Keyword("statistical analyses", 3.0, True), Keyword("python", 2.0, True), Keyword("docker", 1.0, True)], fb)
    rows = RM.skill_rows(rm, fb, FAMILY_LINES)
    assert rows and list(rows)[0] == "Statistical analyses"
    allowed = {s.lower() for items in fb["skills"].values() for s in items}
    allowed |= {i.lower() for line in FAMILY_LINES.values() for i in line_items(line)} | {p.lower() for line in FAMILY_LINES.values() for p in line.split(", ")}
    for it in line_items(rows["Statistical analyses"]):
        assert it.lower() in allowed or any(it.lower() in a for a in allowed), it
    assert rows["Statistical analyses"].split(", ")[:2] == ["hypothesis testing", "ANOVA"]


def test_a_requirement_row_with_no_skill_items_is_dropped_and_the_existing_rows_stay():
    fb = copy.deepcopy(ALIAS_BANK)
    rm = RM.build([Keyword("data analysis", 3.0, True), Keyword("python", 1.0, True)], fb)
    rows = RM.skill_rows(rm, fb, FAMILY_LINES)
    assert rows is None or "Data analysis" not in rows                          # None: the caller keeps the family's lines whole


# ============================================================================================ 6. acronyms in the letter's model-filled slots
@pytest.mark.parametrize("raw, want", [
    ("ai / ml solutions", "AI/ML solutions"), ("AI / ML solutions", "AI/ML solutions"), ("ai/ml solutions", "AI/ML solutions"),
    ("llm and nlp pipelines", "LLM and NLP pipelines"), ("llms and apis on aws and gcp", "LLMs and APIs on AWS and GCP"),
    ("sql and api work", "SQL and API work"), ("ml-based ai agents", "ML-based AI agents"),
    ("email, mail and laws about sqlite", "email, mail and laws about sqlite"),    # whole words only
])
def test_acronyms_in_a_slot_are_upper_case_whole_words(raw, want):
    assert TL.normalise_acronyms(raw) == want


def test_ai_ml_solutions_renders_as_upper_case_and_passes_the_requirement_check():
    ad = "We deliver AI/ML solutions for clinical teams. You will build AI/ML solutions with Python and SQL."
    slot = TL._clean_slot("ad_requirement", "ai / ml solutions")
    assert slot == "AI/ML solutions"
    assert TL.requirement_problems(slot, ad, "Acme", "Data Scientist") == []
    assert TL._clean_slot("question", "how the team runs llm evaluation on aws") == "how the team runs LLM evaluation on AWS"
    assert TL._clean_slot("company_problem_sentence", "your team ships nlp apis on gcp") == "Your team ships NLP APIs on GCP."


@pytest.mark.slow
def test_a_lower_case_ai_ml_slot_renders_upper_case_and_the_letter_passes_its_fact_check(fb_fixture, settings, tmp_path):
    """The Sirius letter failed on "Your ad asks for ai / ml solutions". The model's slot is normalised before the fact check, which is unchanged."""
    from test_template_letter import AD, QUESTION, SENT, bank, KWS

    ad = AD + "\nWe deliver AI/ML solutions for oncology research teams.\n"
    jb = {"id": "manual:tpl371", "company": "Northwind Health AI", "title": "AI Engineer", "description": ad}
    llm = MockLLM({"cover_letter": lambda ctx: {"company_problem_sentence": SENT, "ad_requirement": "ai / ml solutions", "question": QUESTION}})
    fb = bank(fb_fixture)
    t = TL.find(fb, ["ai_engineer"])
    _, letter = TL.make(t, jb, fb, KWS, llm, settings, tmp_path, "Ada Lovelace Cover Letter", "", {"headline": "AI Engineer", "number": ""}, "LLM agents and AWS")
    assert letter["slots"]["ad_requirement"] == "AI/ML solutions" and letter["slot_source"]["ad_requirement"] == "model"
    assert "Your ad asks for AI/ML solutions, which is the work I want to do next." in letter["paragraphs"]
    assert not any("ad_requirement" in p for p in letter["template_problems"]), letter["template_problems"]


# ============================================================================================ 7. the profile never repeats an organisation
PF_BANK = {
    "experience": [{"id": "az", "org": "AstraZeneca", "display_org": "AstraZeneca, United Kingdom", "bullets": [{"id": "az1", "text": "Built an agent."}]},
                   {"id": "dc180", "org": "180 Degrees Consulting Bristol", "display_org": "180 Degrees Consulting", "bullets": [{"id": "dc1", "text": "Led a demo."}]},
                   {"id": "freelance", "org": "Freelance", "bullets": [{"id": "fl1", "text": "Built RAG."}]},
                   {"id": "avasoft", "org": "Avasoft", "display_org": "Avasoft LTD., Chennai, India", "bullets": [{"id": "av1", "text": "Built an app."}]}],
    "projects": [{"id": "lloyds", "name": "Lloyd's Banking Group Datathon", "bullets": [{"id": "p_ll1", "text": "Placed 3rd."}]},
                 {"id": "churn", "name": "Banking Customer Churn — propensity model", "bullets": [{"id": "p_ch1", "text": "Built churn."}]}],
    "profile_fragments": [
        {"id": "pf_data", "terms": ["data analysis"], "text": "Checks the data before the model: combined 14 research datasets for an AstraZeneca-affiliated MSc dissertation."},
        {"id": "pf_agents", "terms": ["agent"], "text": "Built an AI assistant on AWS for an MSc dissertation with an AstraZeneca affiliation; it is tested by about 65 checks."},
        {"id": "pf_client", "terms": ["client"], "text": "At 180 Degrees Consulting, led the design in a five-person team that turned a vague idea into a working prototype."},
        {"id": "pf_client2", "terms": ["demo"], "text": "Scoped a loose brief for 180 Degrees Consulting into a stakeholder demo rated 5 stars."},
        {"id": "pf_lloyds", "terms": ["anomaly"], "text": "Placed 3rd at Lloyd's Banking Group's datathon by spotting rare suspicious cases."},
        {"id": "pf_lloyds2", "terms": ["leaderboard"], "text": "Won a place at the Lloyd’s datathon with a model."},
        {"id": "pf_av", "terms": ["app"], "text": "Shipped a money tool at Avasoft."},
        {"id": "pf_av2", "terms": ["release"], "text": "Put the first version of a fintech product live at Avasoft."}],
}


def pf_rm(*terms):
    return RM.ReqMap([RM.Req(i, t, 2.0, "stated", RM.EXISTING) for i, t in enumerate(terms)], 1.0)


def test_a_hook_sentence_naming_astrazeneca_excludes_a_fragment_that_also_names_it():
    rm = pf_rm("data analysis", "agent")
    skipped: list[str] = []
    got = RM.pick_fragments(rm, PF_BANK, avoid="Data scientist whose MSc dissertation was done for AstraZeneca.", skipped=skipped)
    assert [f["id"] for f in got] == []                                         # pf_data and pf_agents both name AstraZeneca
    assert len(skipped) == 2 and all("AstraZeneca" in s for s in skipped)
    assert {s.split(" ")[0] for s in skipped} == {"pf_data", "pf_agents"}


def test_two_fragments_naming_the_same_organisation_are_never_both_picked():
    rm = pf_rm("data analysis", "agent")
    skipped: list[str] = []
    got = RM.pick_fragments(rm, PF_BANK, skipped=skipped)
    assert len(got) == 1 and got[0]["id"] in ("pf_data", "pf_agents")
    assert len(skipped) == 1 and "AstraZeneca" in skipped[0]


@pytest.mark.parametrize("terms, first, second", [(("client", "demo"), "pf_client", "pf_client2"), (("anomaly", "leaderboard"), "pf_lloyds", "pf_lloyds2"),
                                                  (("app", "release"), "pf_av", "pf_av2")])
def test_180_degrees_lloyds_and_avasoft_are_checked_too(terms, first, second):
    skipped: list[str] = []
    got = [f["id"] for f in RM.pick_fragments(pf_rm(*terms), PF_BANK, skipped=skipped)]
    assert len(got) == 1 and (first in got or second in got) and len(skipped) == 1


def test_the_organisation_names_come_from_the_fact_bank_and_a_generic_project_is_not_one():
    names = RM.organisation_names(PF_BANK)
    assert {"AstraZeneca", "180 Degrees", "Lloyd's", "Avasoft"} <= set(names)
    assert "Freelance" not in names and "Banking" not in names


# ============================================================================================ end to end (xelatex)
@pytest.mark.slow
def test_a_plain_cv_makes_no_rephrase_call_and_the_report_names_a_skipped_plain_line(settings, fb_fixture, job):
    fb = copy.deepcopy(fb_fixture)
    for e in fb["experience"] + fb["projects"]:
        for b in e["bullets"]:
            b["plain"] = b["text"]                                              # every bullet has a plain line (same words: nothing to refuse)
    fb["experience"][0]["bullets"][0]["text"] = "Contributed to " + fb["experience"][0]["bullets"][0]["text"][0].lower() + fb["experience"][0]["bullets"][0]["text"][1:]
    fb["experience"][0]["bullets"][0]["plain"] = "Built " + fb["experience"][0]["bullets"][0]["text"][len("Contributed to "):]    # upgrades "contributed" to "built"
    settings.raw["cv"]["style"] = "plain"
    llm = MockLLM()
    rep = tailor.tailor_job(dict(job), fb, llm, settings, settings.path("outputs") / "v371")
    assert llm.calls.count("rephrase_bullets") == 0 and rep["rephrase"]["calls"] == 0 and rep["rephrase"]["eligible"] == 0
    skipped = rep["style"]["plain_skipped"]
    first = fb["experience"][0]["bullets"][0]["id"]                            # its plain line upgrades "contributed": refused, the rule is on record
    assert "contributed" in skipped[first] and rep["style"]["plain_refused"][first]
    assert all(isinstance(v, str) and v for v in skipped.values())
