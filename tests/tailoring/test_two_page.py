"""Phase 1: two full pages and pinned core skills. cv2.tex.j2, page-2 fill measured from the PDF, greedy selection with the
two-page targets, no near-duplicate bullets, `skills.core` pinned, `result:` as evidence, `--one-page`. No network, no model.
Everything uses fictional fact banks (tests/fixtures/tailoring/), never the real one."""
import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import fixture_profile_settings

from careerops.config import load_yaml
from careerops.db import DB
from careerops.llm import MockLLM
from careerops.setup import core_skill_checks
from careerops.tailoring import tailor
from careerops.tailoring.render import CV2_MARGIN_PT, compile_tex, env, last_line_fills, page2_fill, page_count, pdf_text
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import ONE_PAGE, TWO_PAGE, fill_order, jaccard, near_duplicate, select_cv
from careerops.tailoring.skills import core_skill_problems, cv_skills

FIX = Path(__file__).parent.parent / "fixtures" / "tailoring"


def _kws(*terms, weight=1.0):
    return [Keyword(t, weight, True) for t in terms]


def _b(bid, *words):
    return {"id": bid, "text": " ".join(words) + " work", "terms": []}


# ---------------------------------------------------------------- one full tailoring run, shared by several tests
@pytest.fixture(scope="module")
def run2(tmp_path_factory):
    """The two-page default on the fictional bank that has enough bullets to fill page 2."""
    fb = load_yaml(FIX / "fact_bank_two_page.yaml")
    s = fixture_profile_settings()
    job = {"id": "manual:two", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
           "location": "Bristol, UK", "url": "u", "apply_url": "u",
           "description": (FIX / "jd_northwind.txt").read_text(encoding="utf-8")}
    out = tmp_path_factory.mktemp("two_page")
    rep = tailor.tailor_job(job, fb, MockLLM(), s, out)
    return rep, fb, s


def test_defaults_are_two_pages_and_a_page_two_within_two_lines_of_the_bottom(settings):
    assert settings["scoring"]["max_pages"] == 2 and settings["scoring"]["min_fill_page2"] == 0.96


def test_the_fixture_bank_renders_exactly_two_pages_with_page_two_96_to_100_percent_full(run2):
    rep, _, s = run2
    pdf = Path(rep["cv_pdf"])
    assert rep["cv_pages"] == rep["final"]["pages"] == page_count(pdf) == 2
    fill = page2_fill(pdf)
    assert s["scoring"]["min_fill_page2"] <= fill <= 1.0
    assert rep["page2_fill"] == rep["final"]["page2_fill"] == pytest.approx(fill)
    assert rep["fill"]["reached"] is True       # (the hook's headline and profile line take room, so the fill may have little left to add)
    assert rep["attempts"][-1]["page2_fill"] == pytest.approx(fill)


def test_the_fixture_cv_shows_three_projects_and_reaches_the_fill_target_or_says_why_not(run2):
    rep, fb, s = run2
    projects = rep["context"]["projects"]
    assert s["scoring"]["cv_projects"] == 3 and len(fb["projects"]) == 5 and len(projects) == 3          # the bank has five; the Document rules show three
    assert all(1 <= len(p["bullet_ids"]) <= 3 for p in projects)
    reached = rep["fill"]["reached"] is True and rep["page2_fill"] >= s["scoring"]["min_fill_page2"]
    assert reached or "fact bank has no more experience bullets" in rep["fill_shortfall"]                 # a short page 2 is said, never padded
    first = rep["attempts"][-1]["proposal"]["selection"]
    assert all(2 <= len(first["project_bullets"][p]) <= 3 for p in first["projects"])               # selection: 2-3 each


def test_page_two_fill_comes_from_the_rendered_pdf_not_from_character_counts(run2):
    rep, _, _ = run2
    pdf = Path(rep["cv_pdf"])
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True).stdout
    pages = re.findall(r'<page width="[\d.]+" height="([\d.]+)">(.*?)</page>', xml, re.S)
    assert len(pages) == 2
    height = float(pages[0][0])
    ys = [float(y) for y in re.findall(r'yMax="([\d.]+)"', pages[1][1])]
    area = height - 2 * CV2_MARGIN_PT
    assert page2_fill(pdf) == pytest.approx((max(ys) - CV2_MARGIN_PT) / area)
    # the constant matches the template's geometry: text starts at the top margin and page 1 runs to the bottom one
    top = min(float(y) for y in re.findall(r'yMin="([\d.]+)"', pages[0][1]))
    assert CV2_MARGIN_PT - 3 <= top <= CV2_MARGIN_PT + 12      # (the 22pt name's box reaches about 1pt above the margin)
    first, second = last_line_fills(pdf)
    assert 0.85 <= first <= 1.0 and second == pytest.approx(page2_fill(pdf))


@pytest.mark.slow
def test_a_one_page_cv_has_no_page_two(tmp_path, fb_fixture):
    tex = env().get_template("cv.tex.j2").render(
        id={"name": "A B", "location": "X", "email": "a@b.c"}, profile="p", skills={"Languages": ["Python"]},
        experience=[{"org": "O", "dates": "d", "title": "t", "bullets": ["Did a thing."]}], projects=[],
        education=[], certifications=[])
    assert page2_fill(compile_tex(tex, tmp_path, "cv")) == 0.0


def test_the_cv_starts_with_profile_then_education_experience_projects_skills_certifications(run2):
    rep, _, _ = run2                                                                     # 8 Nov 2026: Education first (tailoring/sections.py)
    text = pdf_text(Path(rep["cv_pdf"]))
    heads = [h for h in ("PROFILE", "EDUCATION", "EXPERIENCE", "PROJECTS", "SKILLS", "CERTIFICATIONS") if h in text]
    assert heads == ["PROFILE", "EDUCATION", "EXPERIENCE", "PROJECTS", "SKILLS", "CERTIFICATIONS"]
    assert [text.index(h) for h in heads] == sorted(text.index(h) for h in heads)


@pytest.mark.slow
def test_cv2_template_compiles_with_only_the_required_fields(tmp_path):
    ctx = {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"}, "profile": "Data scientist.",
           "skills": {"Core": ["Python"]}, "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst",
                                                          "bullets": ["Built a report."]}],
           "projects": [{"name": "Thing", "bullets": ["Built a thing."]}],
           "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": []}
    pdf = compile_tex(env().get_template("cv2.tex.j2").render(**ctx), tmp_path, "cv")
    assert page_count(pdf) == 1


# ---------------------------------------------------------------- selection: targets, greedy, no near-duplicates
def _bank(n_first=7, n_other=5, n_proj=3):
    def entry(eid, n):
        return {"id": eid, "bullets": [_b(f"{eid}{i}", f"k{eid}{i}", f"w{eid}{i}") for i in range(n)]}

    return {"skills": {}, "experience": [entry("e1", n_first), entry("e2", n_other), entry("e3", n_other)],
            "projects": [{"id": f"p{j}", "bullets": [_b(f"p{j}b{i}", f"kp{j}{i}", f"wp{j}{i}") for i in range(n_proj)]}
                         for j in range(5)]}


def test_two_page_targets_are_four_to_six_for_the_newest_entry_three_to_five_for_the_rest():
    fb = _bank()
    kws = _kws(*[f"k{e}{i}" for e in ("e1", "e2", "e3") for i in range(7)], *[f"kp{j}{i}" for j in range(5) for i in range(3)])
    sel = select_cv(fb, kws, layout=TWO_PAGE)
    assert [len(sel["experience"][e]) for e in ("e1", "e2", "e3")] == [6, 5, 5]       # plenty to gain: the maximum
    assert len(sel["projects"]) == 3 and all(len(b) == 3 for b in sel["project_bullets"].values())
    none = select_cv(fb, _kws("zzz-nothing"), layout=TWO_PAGE)
    assert [len(none["experience"][e]) for e in ("e1", "e2", "e3")] == [4, 3, 3]      # nothing to gain: the minimum
    assert len(none["projects"]) == 3 and all(len(b) == 2 for b in none["project_bullets"].values())


def test_an_entry_with_fewer_bullets_than_its_target_shows_all_of_them():
    fb = _bank(n_first=3, n_other=2)
    sel = select_cv(fb, _kws("zzz-nothing"), layout=TWO_PAGE)
    assert [len(sel["experience"][e]) for e in ("e1", "e2", "e3")] == [3, 2, 2]


def test_the_one_page_layout_is_unchanged():
    sel = select_cv(_bank(), _kws("zzz-nothing"), layout=ONE_PAGE)
    assert [len(sel["experience"][e]) for e in ("e1", "e2", "e3")] == [2, 1, 1] and len(sel["projects"]) == 2


def test_greedy_gain_prefers_new_keywords_and_ties_go_to_the_more_recent_entry():
    fb = {"skills": {}, "projects": [], "experience": [
        {"id": "new", "bullets": [_b("n1", "alpha"), _b("n2", "dull")]},
        {"id": "old", "bullets": [_b("o1", "alpha"), _b("o2", "beta"), _b("o3", "gamma")]}]}
    sel = select_cv(fb, _kws("alpha", "beta", "gamma"), layout=TWO_PAGE)
    assert sel["experience"]["new"][0] == "n1"                         # the tie on "alpha" goes to the newer entry
    assert "o1" not in sel["experience"]["old"][:2]                    # and the older one adds only what is still missing
    assert sel["experience"]["old"][:2] == ["o2", "o3"]


def test_a_bullet_that_nearly_repeats_a_chosen_one_is_never_selected():
    twin_a = "Built a churn model with XGBoost and tuned the threshold for recall"
    twin_b = "Built a churn model with XGBoost and tuned the threshold for precision"     # 8/10 words shared
    assert jaccard(twin_a, twin_b) >= 0.6 and near_duplicate(twin_b, [twin_a])
    assert not near_duplicate("Wrote the data preparation module with unit tests", [twin_a])
    fb = {"skills": {}, "projects": [], "experience": [
        {"id": "e1", "bullets": [{"id": "t1", "text": twin_a, "terms": []}, {"id": "t2", "text": twin_b, "terms": []},
                                 _b("u1", "unique", "alpha"), _b("u2", "other", "beta")]}]}
    kws = _kws("churn", "xgboost", "recall", "precision", "alpha", "beta")
    sel = select_cv(fb, kws, layout=TWO_PAGE)
    assert not {"t1", "t2"} <= set(sel["experience"]["e1"])
    assert "t2" not in fill_order(fb, kws, sel)                          # nor added later to fill a page
    assert "t1" not in fill_order(fb, kws, {"experience": {"e1": ["t2", "u1", "u2"]}, "projects": []})


def test_no_two_bullets_of_the_rendered_cv_are_near_duplicates(run2):
    rep, _, s = run2
    ctx = rep["context"]
    bullets = [b for sec in ("experience", "projects") for e in ctx[sec] for b in e["bullets"]]
    assert len(bullets) > 15
    limit = s["scoring"]["bullet_dedupe_jaccard"]
    assert all(jaccard(a, b) < limit for i, a in enumerate(bullets) for b in bullets[i + 1:])


def test_the_shrink_floors_follow_the_layout():
    ctx = {"experience": [{"bullet_ids": [f"x{i}" for i in range(4)], "bullets": [f"b{i} python" for i in range(4)]},
                          {"bullet_ids": ["y0", "y1", "y2"], "bullets": ["c0", "c1", "c2"]}], "projects": []}
    kws = _kws("python")
    assert tailor._shrink(ctx, kws, TWO_PAGE) is True and len(ctx["experience"][1]["bullets"]) == 2   # newest is at its 4
    assert len(ctx["experience"][0]["bullets"]) == 4
    assert tailor._shrink(ctx, kws, TWO_PAGE)                                                         # floors relax: last bullets
    assert len(ctx["experience"][1]["bullets"]) == 1


# ---------------------------------------------------------------- core skills
LABELS = ["Machine Learning, Deep Learning & Statistics", "Python, Data Quality & Cloud", "NLP, LLMs & Evaluation"]
CLIENT = "Client Delivery & Communication"


def test_the_two_page_cv_shows_the_fixed_groups_with_every_core_skill_pinned_in_one_of_them(run2):
    rep, fb, s = run2
    groups = rep["context"]["skills"]
    assert list(groups) == LABELS + [CLIENT] == rep["skills"]["rows"]   # the fixture bank has no row for the fourth group: it shows the user's own terms
    assert groups[CLIENT] and not any("Client Delivery" in w for w in rep["analysis_warnings"])
    assert rep["skills"]["core"] == ["Python", "pandas", "NumPy", "Docker"]
    shown = {x.casefold() for items in groups.values() for x in items}
    assert {c.casefold() for c in rep["skills"]["core"]} <= shown        # core is pinned
    text = pdf_text(Path(rep["cv_pdf"]))
    for label, items in groups.items():                                 # the label on its own line, the items below it
        assert re.search(r"^" + re.escape(label) + r"$", text, re.M)
        written = {t.casefold() for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] for t in b.get("terms", [])}
        assert set(items) <= {i for g in fb["skills"].values() for i in g} | {"Python"} or (          # a bank skill, or (the client group) a term the user wrote
            label == CLIENT and {i.casefold() for i in items} <= written)
    assert set(rep["skills"]["dropped_groups"]) == {"Mobile & Web", "Visualisation"}     # bank groups no configured group claims (Matplotlib, always shown, is placed by name)
    assert "Matplotlib" in groups["Python, Data Quality & Cloud"] and "Tableau" not in {x for g in groups.values() for x in g}


def test_a_skill_appears_once_whatever_its_case_and_the_first_occurrence_wins(run2):
    rep, fb, _ = run2
    names = [s.casefold() for items in rep["context"]["skills"].values() for s in items]
    assert len(names) == len(set(names))
    assert "Python" in rep["context"]["skills"]["Python, Data Quality & Cloud"]
    assert "python" in [s for s in fb["skills"]["Visualisation"]]       # the bank repeats it in lower case; the CV does not
    skills_block = re.search(r"\nSKILLS\n(.*?)CERTIFICATIONS", pdf_text(Path(rep["cv_pdf"])), re.S).group(1)       # Skills after Projects since 8 Nov 2026
    items_only = "".join(ln + chr(10) for ln in skills_block.splitlines() if ln.strip() not in rep["context"]["skills"])   # not the group labels
    assert len(re.findall(r"\bpython\b", items_only, re.I)) == 1


def test_the_one_page_rows_stay_core_plus_at_most_five_groups_the_least_jd_relevant_goes(run2):
    rep, fb, s = run2
    kws = [Keyword("swift", 2.0, True)]
    rows, dropped = cv_skills(fb, kws, s["scoring"]["max_skill_groups"])      # the older rows, still the one-page CV's
    assert len(rows) == 1 + s["scoring"]["max_skill_groups"] == 6
    assert "Mobile & Web" in rows and "Mobile & Web" not in dropped      # relevant to this JD: it stays
    assert len(dropped) == 2 and "Core" in rows


@pytest.mark.slow
def test_core_skills_survive_the_shrink_step(settings, broad_job, broad_llm):
    fb = load_yaml(FIX / "fact_bank_two_page.yaml")
    rep = tailor.tailor_job(broad_job, fb, broad_llm, settings, settings.path("outputs"), one_page=True)
    assert max(a["shrinks"] for a in rep["attempts"]) >= 1, "the shrink step never ran"
    assert rep["cv_pages"] == 1
    assert list(rep["context"]["skills"])[0] == "Core"
    text = pdf_text(Path(rep["cv_pdf"]))
    assert re.search(r"Core:\s+Python, pandas, NumPy, Docker", text)


def test_core_skill_that_is_denied_or_evidenced_nowhere_fails_doctor_and_names_the_skill(tmp_path):
    fb = load_yaml(FIX / "fact_bank_two_page.yaml")
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump(fb), encoding="utf-8")
    assert core_skill_checks(ok) == [("core skills (4) all evidenced and none denied", True, "")]

    denied = copy.deepcopy(fb)
    denied["skills"]["core"].append("Terraform")
    denied["skills"]["Languages"].append("Terraform")                   # evidenced elsewhere, but on deny_terms
    p = tmp_path / "denied.yaml"
    p.write_text(yaml.safe_dump(denied), encoding="utf-8")
    bad = core_skill_checks(p)
    assert len(bad) == 1 and not bad[0][1] and "Terraform" in bad[0][0] and "deny" in bad[0][0]

    ghost = copy.deepcopy(fb)
    ghost["skills"]["core"].append("Plotly")                            # appears nowhere else in the bank
    p = tmp_path / "ghost.yaml"
    p.write_text(yaml.safe_dump(ghost), encoding="utf-8")
    bad = core_skill_checks(p)
    assert len(bad) == 1 and not bad[0][1] and "Plotly" in bad[0][0] and "nowhere" in bad[0][0]
    assert core_skill_problems(ghost) == [bad[0][0]]


def _bank_with_core(core, evidence):
    """A one-bullet bank whose only mention of the skills is `evidence` (terms of that bullet); core is the pinned row."""
    return {"skills": {"core": core}, "deny_terms": [],
            "experience": [{"id": "e", "title": "T", "org": "O", "bullets": [
                {"id": "e1", "text": "Built things.", "terms": evidence}]}], "projects": []}


def test_core_skill_check_takes_plural_and_singular_as_one_term_in_both_directions():
    """The doctor false alarm: core 'LLMs' / 'APIs' were reported as evidenced nowhere though the bank says llm / api."""
    plural_core = _bank_with_core(["LLMs", "APIs", "Prompts"], ["llm", "api", "prompt"])
    assert core_skill_problems(plural_core) == []
    singular_core = _bank_with_core(["LLM", "API", "Prompt"], ["llms", "apis", "prompts"])
    assert core_skill_problems(singular_core) == []


def test_core_skill_check_is_still_a_true_negative_for_a_skill_the_bank_never_says(tmp_path):
    fb = _bank_with_core(["LLMs", "Plotly", "Kafka"], ["llm", "plots", "kafkaesque"])
    problems = core_skill_problems(fb)
    assert len(problems) == 2 and "'Plotly'" in problems[0] and "'Kafka'" in problems[1]     # plots != Plotly, a longer word != Kafka
    assert all("nowhere" in p for p in problems)
    p = tmp_path / "bank.yaml"
    p.write_text(yaml.safe_dump(fb), encoding="utf-8")
    assert [c[0] for c in core_skill_checks(p)] == problems and not any(ok for _, ok, _ in core_skill_checks(p))


def test_the_cv_refuses_to_render_a_denied_core_skill(settings, job):
    fb = load_yaml(FIX / "fact_bank_two_page.yaml")
    fb["skills"]["core"].append("Terraform")
    with pytest.raises(ValueError, match="Terraform"):
        tailor.tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))


# ---------------------------------------------------------------- --one-page
@pytest.mark.slow
def test_one_page_flag_renders_one_page_with_the_old_template_and_layout(settings, fb_fixture, job):
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"), one_page=True)
    assert rep["one_page"] is True and rep["cv_pages"] == rep["final"]["pages"] == 1
    assert rep["page2_fill"] == 0.0 and rep["fill"]["added"] == []
    assert len(rep["context"]["projects"]) == 2
    assert "\\fontsize{10.5}" in Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")


def test_the_default_run_uses_the_two_page_template(run2):
    rep, _, _ = run2
    tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    assert rep["one_page"] is False and "\\documentclass[a4paper,11pt]{article}" in tex       # the reference CV's preamble
    assert "margin=0.6in, top=0.5in, bottom=0.5in" in tex and ("\\linespread{1.02}" in tex or "\\linespread{1.0" in tex)


# ---------------------------------------------------------------- result: as evidence
def _result_bank():
    return {"deny_terms": [], "experience": [{"id": "e", "org": "Acme", "bullets": [
        {"id": "r1", "text": "Built a forecasting service in Python.", "terms": ["python"],
         "result": "Cut forecast error by 18%."},
        {"id": "r2", "text": "Wrote the reporting pipeline in Python.", "terms": ["python"], "result": "Saved 12 hours a week."},
        {"id": "r3", "text": "Built a dashboard in Python.", "terms": ["python"]}]}], "projects": [],
        "skills": {}, "profile_facts": []}


def test_a_number_from_the_bullets_result_passes_the_guard_and_an_invented_one_fails():
    fb = _result_bank()
    kws = _kws("python")
    good = "Built a forecasting service in Python, cutting forecast error by 18%."
    acc, viol, _ = tailor.guard_rephrases(fb, {"r1": good}, kws)
    assert acc == {"r1": good} and viol == []
    acc, viol, _ = tailor.guard_rephrases(fb, {"r1": "Built a forecasting service in Python, cutting forecast error by 25%."}, kws)
    assert acc == {} and any("new numbers" in v and "25%" in v for v in viol)
    acc, viol, _ = tailor.guard_rephrases(fb, {"r1": "Built a forecasting service in Python, saving 12 hours a week."}, kws)
    assert acc == {} and any("12" in v for v in viol)                  # another bullet's result is not evidence for this one
    acc, viol, _ = tailor.guard_rephrases(fb, {"r3": "Built a dashboard in Python that cut errors by 18%."}, kws)
    assert acc == {} and any("18%" in v for v in viol)                 # a bullet without result: no number at all


def test_a_result_reaches_the_rephrase_prompt_even_when_no_keyword_is_missing(spy_llm):
    fb = _result_bank()
    llm = spy_llm()
    proposed, stats = tailor.propose_rephrases({"title": "Data Scientist"}, fb, {"experience": {"e": ["r1", "r3"]},
                                                                                "projects": []}, _kws("python"), llm, {}, [])
    assert stats["eligible"] == 1 and stats["calls"] == 1              # r3 has no result and no missing keyword: not sent
    sent = llm.sent[0]
    assert "RESULT: Cut forecast error by 18%." in sent["user"]
    assert sent["context"]["bullets"][0]["result"] == "Cut forecast error by 18%."
    assert "RESULT" in sent["system"]


def test_a_result_the_text_already_states_is_not_sent_again(spy_llm):
    fb = _result_bank()
    fb["experience"][0]["bullets"][0]["text"] = "Built a forecasting service in Python. Cut forecast error by 18%."
    llm = spy_llm()
    tailor.propose_rephrases({"title": "x"}, fb, {"experience": {"e": ["r1"]}, "projects": []}, _kws("python"), llm, {}, [])
    assert llm.sent == []


def test_bullets_without_a_result_are_listed_for_the_user_to_fill_in(run2):
    rep, fb, _ = run2
    assert rep["needs_result"], "the fixture bank has no result: fields at all"
    ids = {n["id"] for n in rep["needs_result"]}
    on_cv = {i for sec in ("experience", "projects") for e in rep["context"][sec] for i in e["bullet_ids"]}
    assert ids == on_cv                                                  # every bullet on the CV, none invented
    assert all(n["text"] for n in rep["needs_result"])
    ctx = {"experience": [{"org": "Acme", "bullet_ids": ["r1", "r2", "r3"]}], "projects": []}
    assert [n["id"] for n in tailor.needs_result(_result_bank(), ctx)] == ["r3"]


# ---------------------------------------------------------------- the report and the jobs table
def test_the_jobs_table_has_the_page_columns(settings):
    db = DB(settings.path("db"))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert {"cv_pages", "page2_fill"} <= cols
    db.upsert_job({"id": "m:1", "source": "m", "company": "C", "title": "t"})
    db.update_job("m:1", cv_pages=2, page2_fill=0.87)
    row = db.job("m:1")
    assert (row["cv_pages"], row["page2_fill"]) == (2, 0.87)
