"""The 18 Oct round: five changes to the two-page CV, the layout itself untouched. Each test was written before its change.

  1. project links    the right-hand tag of a project with a `github:` value is `\\url{<github>}` (default \\url font), never `\\href{..}{GitHub}`;
                       when the name and the URL do not fit one line the URL moves to the stack line, right-aligned. Checked on the rendered
                       PDF with `pdftotext -bbox`: the URL's box never touches another word's box.
  2. verbatim         a bullet with `verbatim: true` is never reworded, shortened, split or given an opener: the output is the fact-bank text.
  3. replaces         a selected bullet with `replaces:` drops what it replaces (preferred when it scores >= 0.8 x the best of them); one situation
                       opener per role: the `situation:` text or a `situation_included: true` bullet, never both.
  4. skills           the family whose `match` terms carry the most keyword weight gives its `skill_lines` word for word.
  5. recent roles     a role dated within the last two years keeps at least two bullets; the shrink loop trims projects first.

The fictional bank is tests/fixtures/golden/fact_bank_verbatim.yaml (the real bank's shape); the ad is the golden Anaplan one. No network, no real
model, the real fact bank is never read. The pipeline tests run xelatex and are marked slow.
"""
import copy
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import layout as pdflayout
from careerops.tailoring import selection, skills, tailor, typography
from careerops.tailoring.render import compile_tex, env, pdf_text, render_cv
from careerops.tailoring.scorer import Keyword

HERE = Path(__file__).parent.parent
GOLDEN = HERE / "golden" / "anaplan"
BANK = HERE / "fixtures" / "golden" / "fact_bank_verbatim.yaml"
TODAY = date(2026, 10, 18)
RIGHT_EDGE = 595.276 - 0.6 * 72                 # A4 width minus the 0.6in side margin of cv2.tex.j2


def bank() -> dict:
    return load_yaml(BANK)


def anaplan_kws() -> list[Keyword]:
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


def anaplan_jd() -> str:
    return (GOLDEN / "jd.txt").read_text(encoding="utf-8")


def verbatim(fb: dict) -> dict[str, str]:
    return {b["id"]: b["text"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if b.get("verbatim")}


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(selection, "today", lambda: TODAY)


# ------------------------------------------------------------------------------------------------ one pipeline run on the Anaplan ad
@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """tailor_job on the fictional bank and the golden ad; the fake model answers every rephrase_bullets item with the bullet minus its final
    period (a change the guard accepts), so a bullet that reaches the model and comes back changed shows in the output."""
    from conftest import SpyLLM, fixture_profile_settings

    settings = fixture_profile_settings()
    tmp = tmp_path_factory.mktemp("oct18")
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge", "robots", "secrets",
              "discover", "aiapply_applied", "calibration", "frozen_keywords"):
        settings.raw["paths"][k] = str(tmp / Path(settings.raw["paths"][k]).name)
    settings.raw["paths"]["applications"] = str(tmp / "Companies")
    settings.raw["paths"]["tmp"] = str(tmp / "stage")
    settings.raw["letter"]["mode"] = "off"
    fb = bank()
    jd = anaplan_jd()
    low = jd.lower()
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    required = [r["term"] for r in rows if r["weight"] >= 2 and r["term"].lower() in low]
    preferred = [r["term"] for r in rows if r["weight"] < 2 and r["term"].lower() in low]
    llm = SpyLLM({"extract_keywords": lambda ctx: {"required": required, "preferred": preferred, "team": "Functional Analytics",
                                                  "company_hook": "Anaplan builds scenario planning software.", "context_terms": []},
                  "rephrase_bullets": lambda ctx: {b["id"]: b["text"].rstrip(".") for b in ctx["bullets"]}})
    job = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan",
           "title": "Data Scientist, Functional Analytics (REQ 26490)", "location": "London, UK",
           "url": "https://example.org/anaplan", "apply_url": "https://example.org/anaplan", "description": jd}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selection, "today", lambda: TODAY)
        rep = tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    return rep, fb, llm


def shown(ctx: dict) -> dict:
    """The context as the PDF gets it: situation openers and splits, then the typography step (what render_cv hands the template)."""
    return typography.typeset(pdflayout.with_openers(pdflayout.with_splits(ctx)))


# ================================================================================================================ 1. project links
def _ctx(projects: list[dict]) -> dict:
    return dict(id={"name": "A B", "location": "X", "email": "a@b.c"}, profile="p", skills={"Core": ["Python"]},
                experience=[{"org": "O", "dates": "Jan 2025 - Feb 2025", "title": "t", "bullets": ["Did a thing."]}],
                projects=projects, education=[], certifications=[])


def _body(tex: str) -> str:
    return tex.split(r"\begin{document}")[1]


def test_a_project_with_a_github_value_prints_its_url_in_the_default_url_font_not_the_word_github():
    gh = "https://github.com/Ada-Test/churn"
    body = _body(env().get_template("cv2.tex.j2").render(**_ctx([{"name": "Churn", "stack": "R", "github": gh, "bullets": ["Built it."]}])))
    assert rf"\url{{{gh}}}" in body
    assert "{GitHub}" not in body and r"\gh{" not in body and r"\href{https://github.com/Ada-Test" not in body
    assert r"\urlstyle" not in body and "texttt" not in body               # the default \url font: nothing sets another


@pytest.mark.slow
def test_a_url_with_a_fragment_or_a_percent_sign_compiles_and_prints_whole(tmp_path):
    r"""The URL sits inside \project's argument, where a bare # or % is a TeX error: the template escapes them for \url, which prints them as
    they are. Checked on the PDF text, not on the .tex."""
    gh = "https://github.com/Ada-Test/churn?q=a%20b#readme"
    body = _body(env().get_template("cv2.tex.j2").render(**_ctx([{"name": "Churn", "stack": "R", "github": gh, "bullets": ["Built it."]}])))
    assert r"\url{https://github.com/Ada-Test/churn?q=a\%20b\#readme}" in body
    pdf = render_cv(_ctx([{"name": "Churn", "stack": "R", "github": gh, "bullets": ["Built it."]}]), tmp_path)
    assert pdflayout.link_problems(pdf, [gh]) == [] and gh in flat(pdf_text(pdf))


def test_a_project_without_a_link_keeps_its_award_or_badge():
    body = _body(env().get_template("cv2.tex.j2").render(**_ctx([
        {"name": "Prize", "stack": "R", "award": "3rd place", "github": None, "bullets": ["Built it."]},
        {"name": "Course", "stack": "Torch", "badge": "MSc Coursework", "github": None, "bullets": ["Built it."]},
        {"name": "Plain", "stack": "Go", "bullets": ["Built it."]}])))
    assert r"\badge{3rd place}" in body and r"\badge{MSc Coursework}" in body
    assert r"\url" not in body.split("Projects")[1] and "GitHub" not in body


def test_the_one_page_template_prints_the_url_too():
    gh = "https://github.com/Ada-Test/churn"
    body = _body(env().get_template("cv.tex.j2").render(**_ctx([{"name": "Churn", "stack": "R", "github": gh, "bullets": ["Built it."]}])))
    assert rf"\url{{{gh}}}" in body and "{GitHub}" not in body


@pytest.mark.slow
def test_the_generated_tex_has_a_url_for_every_project_with_a_link_and_no_github_link_text(run):
    rep, fb, _ = run
    tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    links = {p["name"]: p["github"] for p in rep["context"]["projects"] if p.get("github")}
    assert links, "the run printed no project with a link"
    for name, gh in links.items():
        assert rf"\url{{{gh}}}" in tex, name
    assert tex.count(r"\url{https://github.com/Ada-Test/") >= len(links) and "{GitHub}" not in tex


def _words(pdf):
    return pdflayout.pdf_words(pdf)


def _box(words, text):
    hit = [w for w in words if w.text == text]
    assert len(hit) == 1, (text, [w.text for w in words if text[:12] in w.text])
    return hit[0]


def _touch(a, b, pad=0.3):
    return a.page == b.page and a.x0 < b.x1 - pad and b.x0 < a.x1 - pad and a.y0 < b.y1 - pad and b.y0 < a.y1 - pad


@pytest.mark.slow
def test_a_link_that_does_not_fit_beside_the_name_moves_to_the_stack_line_and_nothing_overlaps(tmp_path):
    short = "https://github.com/Ada-Test/churn"
    long_ = "https://github.com/Ada-Test/a-repository-with-a-fairly-long-name"
    huge = "https://github.com/Ada-Test/another-repository-with-an-even-longer-name-than-the-last"
    long_name = "Alpha " + " ".join(f"Word{i}" for i in range(1, 11)) + " Omega"
    ctx = _ctx([
        {"name": "ShortOne", "stack": "STACKA, python", "github": short, "bullets": ["Built it."]},
        {"name": long_name + " Beta", "stack": "STACKB, python", "github": long_, "bullets": ["Built it."]},
        {"name": long_name + " Gamma", "stack": "STACKC " + ", ".join(f"tool{i}" for i in range(1, 15)), "github": huge,
         "bullets": ["Built it."]}])
    pdf = render_cv(ctx, tmp_path)
    words = _words(pdf)
    # (a) fits: the URL is on the name's line, right-aligned, after the name
    url, name = _box(words, short), _box(words, "ShortOne")
    assert abs(url.y0 - name.y0) < 2 and url.x0 > name.x1 and abs(url.x1 - RIGHT_EDGE) < 1.5
    # (b) the name and the URL do not fit one line: the URL is on the stack line, right-aligned, clear of the stack text
    url, stack, beta = _box(words, long_), _box(words, "STACKB,"), _box(words, "Beta")
    assert url.y0 > beta.y1 - 1 and abs(url.y0 - stack.y0) < 3 and abs(url.x1 - RIGHT_EDGE) < 1.5
    # (c) the stack is long too: the URL gets a line of its own under the stack, right-aligned
    url, stack = _box(words, huge), _box(words, "STACKC")
    assert url.y0 > stack.y1 - 1 and abs(url.x1 - RIGHT_EDGE) < 1.5
    # never overlapping: no URL box touches any other word's box
    for u in (short, long_, huge):
        box = _box(words, u)
        assert not [w.text for w in words if w is not box and _touch(box, w)], u
    assert pdflayout.link_problems(pdf, [short, long_, huge]) == []


@pytest.mark.slow
def test_the_pdf_check_names_a_link_that_is_missing_from_the_page(tmp_path):
    pdf = render_cv(_ctx([{"name": "One", "stack": "R", "github": "https://github.com/Ada-Test/churn", "bullets": ["Built it."]}]), tmp_path)
    assert pdflayout.link_problems(pdf, ["https://github.com/Ada-Test/churn"]) == []
    assert pdflayout.link_problems(pdf, ["https://github.com/Ada-Test/not-there"])[0].endswith("is not on the page")


@pytest.mark.slow
def test_every_link_of_the_anaplan_cv_is_clear_of_the_other_words(run):
    rep, fb, _ = run
    urls = [p["github"] for p in rep["context"]["projects"] if p.get("github")]
    assert urls and pdflayout.link_problems(Path(rep["cv_pdf"]), urls) == [] and rep["lengths"]["links"] == []


# ===================================================================================================================== 2. verbatim
def _sel(fb: dict, ids: list[str]) -> dict:
    owner = {b["id"]: e["id"] for e in fb["experience"] for b in e["bullets"]}
    out = {e["id"]: [] for e in fb["experience"]}
    for i in ids:
        out[owner[i]].append(i)
    return {"experience": out, "projects": []}


def test_a_verbatim_bullet_is_never_sent_to_the_rephrase_call(spy_llm):
    fb = bank()
    for b in (x for e in fb["experience"] for x in e["bullets"]):          # every bullet has a result its text lacks: all would be asked
        b["result"] = "an outcome the text lacks"
    llm = spy_llm({"rephrase_bullets": lambda ctx: {b["id"]: b["text"].rstrip(".") for b in ctx["bullets"]}})
    notes: list[str] = []
    proposed, stats = tailor.propose_rephrases({"title": "DS"}, fb, _sel(fb, ["az5", "az11", "az12", "az9"]), anaplan_kws(), llm, {}, notes)
    sent = [b["id"] for call in llm.sent for b in call["context"]["bullets"]]
    assert sorted(sent) == ["az5", "az9"] and set(proposed) == {"az5", "az9"}
    assert stats["eligible"] == 2


def test_a_verbatim_bullet_is_not_a_wording_target_and_is_not_shortened(spy_llm):
    fb = bank()
    ctx = {"experience": [{"id": "az", "bullet_ids": ["az11", "az9"],
                           "bullets": [fb["experience"][0]["bullets"][10]["text"], fb["experience"][0]["bullets"][8]["text"]]}], "projects": []}
    kws = [Keyword("evaluations", 2.0, True), Keyword("documented", 2.0, True)]
    plan, unplaced = tailor.wording_targets(ctx, kws, ["evaluations", "documented"], skip=frozenset(verbatim(fb)))
    assert "az11" not in plan
    llm = spy_llm()
    out = tailor.shorten_pass({"title": "DS"}, fb, {"az11": (ctx["experience"][0]["bullets"][0], 20)}, kws, llm, {}, [])
    assert out == {} and llm.sent == []


@pytest.mark.slow
def test_every_verbatim_bullet_in_the_output_is_the_fact_bank_text_byte_for_byte(run):
    rep, fb, llm = run
    texts = verbatim(fb)
    ctx = rep["context"]
    on_cv = {i: t for sec in ("experience", "projects") for e in ctx[sec] for i, t in zip(e["bullet_ids"], e["bullets"]) if i in texts}
    assert len(on_cv) >= 3, f"too few verbatim bullets chosen for the test to prove anything: {list(on_cv)}"
    for bid, text in on_cv.items():
        assert text == texts[bid], bid
    # and after the openers, the splits and the typography step the PDF gets (the existing step: the same on both sides)
    got = {i: t for sec in ("experience", "projects") for e in shown(ctx)[sec] for i, t in zip(e["bullet_ids"], e["bullets"]) if i in texts}
    want = typography.typeset({"experience": [{"bullet_ids": list(texts), "bullets": list(texts.values())}], "projects": []})["experience"][0]
    want = dict(zip(want["bullet_ids"], want["bullets"]))
    assert got == {i: want[i] for i in got} and set(got) == set(on_cv)


@pytest.mark.slow
def test_no_verbatim_bullet_reached_the_model_in_the_run_and_none_was_split(run):
    rep, fb, llm = run
    sent = {b["id"] for call in llm.sent if call["task"] == "rephrase_bullets" for b in call["context"]["bullets"]}
    assert not sent & set(verbatim(fb)), sent & set(verbatim(fb))
    assert not set(rep["context"].get("splits") or {}) & set(verbatim(fb))
    assert any("fl7" in n and "left as the fact bank has it" in n for a in rep["attempts"] for n in a["warnings"]), \
        "a verbatim bullet that prints over 3 lines is reported, not trimmed"


# ===================================================================================================================== 3. replaces
def _micro(x_text: str, r_text: str, kws_terms: list[str]):
    fb = {"identity": {"name": "A"}, "profile_facts": [], "skills": {}, "projects": [], "education": [], "certifications": [],
          "experience": [{"id": "e", "title": "t", "org": "o", "dates": "Jan 2026 – Feb 2026", "bullets": [
              {"id": "x", "text": x_text, "terms": []}, {"id": "r", "text": r_text, "terms": [], "replaces": ["x"], "verbatim": True}]}]}
    return fb, [Keyword(t, 2.0, True) for t in kws_terms]


X_TEXT = "Built forecasting pipelines in python with pandas and mlflow on sql data."
KW5 = ["forecasting", "python", "pandas", "mlflow", "sql"]


def _pick(fb, kws, layout=selection.Layout((1, 2), (1, 2), 0, None)):
    return selection.select_cv(fb, kws, None, skills={}, layout=layout)["experience"]["e"]


def test_the_replacing_bullet_is_preferred_when_it_scores_at_least_80_percent_of_the_best_it_replaces():
    fb, kws = _micro(X_TEXT, "Built forecasting pipelines in python with pandas and mlflow.", KW5)
    sc = selection.relevance_scores(fb, kws, "")
    assert sc["r"] >= 0.8 * sc["x"] and sc["r"] < sc["x"]                      # the replacer is slightly weaker, but within 80 %
    assert _pick(fb, kws) == ["r"]


def test_a_replacing_bullet_below_80_percent_leaves_the_originals():
    fb, kws = _micro(X_TEXT, "Built forecasting pipelines in python with pandas.", KW5)
    sc = selection.relevance_scores(fb, kws, "")
    assert sc["r"] < 0.8 * sc["x"]
    assert _pick(fb, kws) == ["x"]


def test_selecting_a_replacing_bullet_drops_the_ones_it_replaces_even_when_the_entry_is_below_its_minimum():
    fb, kws = _micro("Built forecasting pipelines with pandas and mlflow.", "Built forecasting pipelines with pandas and mlflow.", ["forecasting"])
    assert _pick(fb, kws, selection.Layout((2, 3), (2, 3), 0, None)) == ["r"]


def test_no_selected_bullet_replaces_another_selected_one_on_the_bank_and_the_ad_of_the_run():
    fb, kws = bank(), anaplan_kws()
    replaced = {r: b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] for r in b.get("replaces") or []}
    for layout in (selection.TWO_PAGE, selection.ONE_PAGE):
        sel = selection.select_cv(fb, kws, None, layout=layout, jd=anaplan_jd())
        ids = set(selection.selected_ids(fb, sel))
        assert not [(replaced[i], i) for i in ids if i in replaced and replaced[i] in ids], (layout, sorted(ids))


def test_the_fill_never_adds_a_bullet_next_to_the_one_it_replaces_or_is_replaced_by():
    fb, kws = bank(), anaplan_kws()
    sel = selection.select_cv(fb, kws, None, layout=selection.TWO_PAGE, jd=anaplan_jd())
    used = set(selection.selected_ids(fb, sel))
    order = selection.fill_order(fb, kws, sel, None, selection.JACCARD, 6, anaplan_jd())
    every = used | set(order)
    bidx = tailor.bullet_index(fb)
    for i in every:
        assert not (set(bidx[i].get("replaces") or []) & every), (i, bidx[i]["replaces"])


def test_at_most_one_bullet_of_a_role_has_its_own_situation_opener():
    fb = bank()
    second = {"id": "az13", "verbatim": True, "situation_included": True, "text": "Needed a second thing: did it.", "terms": ["python", "machine learning", "mlops"]}
    fb["experience"][0]["bullets"].append(second)
    sel = selection.select_cv(fb, anaplan_kws(), None, layout=selection.TWO_PAGE, jd=anaplan_jd())
    situated = {b["id"] for e in fb["experience"] for b in e["bullets"] if b.get("situation_included")}
    for e in fb["experience"]:
        assert len(set(sel["experience"][e["id"]]) & situated) <= 1, e["id"]


def _entry(bullets, ids, situation="Needed a thing"):
    return {"id": "az", "situation": situation, "bullet_ids": ids, "bullets": bullets}


def test_the_situation_text_is_not_added_to_a_role_that_has_a_bullet_with_its_own_opener():
    ctx = {"experience": [_entry(["Took it to production.", "Needed an agent: built one."], ["az4", "az10"])], "projects": [],
           "situated": ["az10"], "verbatim": ["az10"]}
    out = pdflayout.with_openers(ctx)["experience"][0]["bullets"]
    assert out == ["Took it to production.", "Needed an agent: built one."]
    plain = {**ctx, "situated": []}
    assert pdflayout.with_openers(plain)["experience"][0]["bullets"][0] == "Needed a thing: took it to production."


def test_the_situation_text_is_never_put_on_a_verbatim_bullet():
    ctx = {"experience": [_entry(["Diagnosed where it struggled.", "Took it to production."], ["az11", "az4"])], "projects": [],
           "situated": [], "verbatim": ["az11"]}
    assert pdflayout.with_openers(ctx)["experience"][0]["bullets"] == ["Diagnosed where it struggled.", "Took it to production."]


def test_a_hook_proof_that_a_selected_bullet_replaces_hands_the_proof_over_when_the_number_survives():
    fb = bank()
    from careerops import hook as H

    hk = H.build_hook({"description": anaplan_jd(), "company": "Anaplan"}, anaplan_kws(), fb, notes=[])
    assert hk.proof_bullet_id == "az4" and hk.number == "~65"
    sel = {"experience": {"az": ["az11", "az6"], "dc180": [], "freelance": [], "avasoft": []}, "projects": []}
    sel2, hk2, notes = tailor.reconcile_proof(fb, sel, hk)
    assert hk2.proof_bullet_id == "az11" and sel2["experience"]["az"] == ["az11", "az6"] and any("az4" in n and "az11" in n for n in notes)
    sel3 = {"experience": {"az": ["az12", "az6"], "dc180": [], "freelance": [], "avasoft": []}, "projects": []}
    az = {b["id"]: b for b in fb["experience"][0]["bullets"]}
    az["az11"]["replaces"] = []
    az["az12"]["replaces"] = ["az3", "az4"]                                    # az12 replaces the proof but does not carry its number
    sel4, hk4, notes = tailor.reconcile_proof(fb, sel3, hk)
    assert hk4.proof_bullet_id == "az4" and "az12" not in sel4["experience"]["az"] and "az4" in sel4["experience"]["az"]


@pytest.mark.slow
def test_the_astrazeneca_role_has_no_replacing_pair_and_exactly_one_opener(run):
    rep, fb, _ = run
    ctx = rep["context"]
    az = next(e for e in ctx["experience"] if e["id"] == "az")
    replaces = {b["id"]: set(b.get("replaces") or []) for b in fb["experience"][0]["bullets"]}
    for i in az["bullet_ids"]:
        assert not replaces[i] & set(az["bullet_ids"]), (i, az["bullet_ids"])
    out = next(e for e in shown(ctx)["experience"] if e["id"] == "az")
    assert [b for b in out["bullets"] if b.startswith("Needed")] and len([b for b in out["bullets"] if b.startswith("Needed")]) == 1, out["bullets"]
    for e in ctx["experience"] + ctx["projects"]:                       # and in every other role and project
        for i in e["bullet_ids"]:
            assert not (set(tailor.bullet_index(fb)[i].get("replaces") or []) & set(e["bullet_ids"])), (e["id"], i)


# ======================================================================================================================== 4. skills
def test_the_family_with_the_most_match_weight_for_the_ad_is_picked():
    fb = bank()
    assert skills.family_scores(fb, anaplan_kws())["data_scientist"] > skills.family_scores(fb, anaplan_kws())["ai_engineer"]
    agent = [Keyword("LLM", 3.0, True), Keyword("agent", 3.0, True), Keyword("RAG", 2.0, True), Keyword("Python", 1.0, True)]
    assert skills.pick_family(fb, agent)["id"] == "ai_engineer"
    assert skills.pick_family(fb, anaplan_kws())["id"] == "data_scientist"


def test_a_tie_goes_to_the_first_family_and_no_match_picks_none_unless_the_job_family_names_one():
    fb = bank()
    assert skills.pick_family(fb, [Keyword("zzz-nothing", 3.0, True)]) is None
    assert skills.pick_family(fb, [Keyword("zzz-nothing", 3.0, True)], "data_scientist")["id"] == "data_scientist"
    assert skills.pick_family({**fb, "families": []}, anaplan_kws()) is None


def test_family_skill_lines_are_the_labels_and_items_as_written():
    fb = bank()
    lines = skills.family_skill_lines(fb, anaplan_kws())
    ds = next(f for f in fb["families"] if f["id"] == "data_scientist")["skill_lines"]
    assert lines == {l["label"]: l["items"] for l in ds} and len(lines) == 4
    assert list(lines)[0] == "Machine Learning, Deep Learning & Statistics" and all(isinstance(v, str) for v in lines.values())


def test_a_family_that_omits_a_standing_skill_the_bank_lists_is_warned_about_and_never_amended():
    fb = bank()
    for f in fb["families"]:
        for l in f["skill_lines"]:
            l["items"] = l["items"].replace("Seaborn, ", "")
    lines = skills.family_skill_lines(fb, anaplan_kws())
    missing = skills.family_basics_missing(lines, fb, ["Python", "pandas", "Seaborn", "Docker"])
    assert missing == ["Seaborn"] and "Seaborn" not in " ".join(lines.values())


@pytest.mark.slow
def test_the_anaplan_cv_prints_the_four_data_scientist_lines_as_written(run):
    rep, fb, _ = run
    ds = next(f for f in fb["families"] if f["id"] == "data_scientist")["skill_lines"]
    assert rep["context"]["skills"] == {l["label"]: l["items"] for l in ds}
    want = typography.typeset({"skills": {l["label"]: l["items"] for l in ds}})["skills"]
    assert typography.typeset(rep["context"])["skills"] == want
    text = flat(pdf_text(Path(rep["cv_pdf"])))
    for label, items in want.items():
        assert flat(label) in text and flat(items) in text, label
    assert "Stakeholder, Consulting" not in text and "Swift" not in text.split("SKILLS")[1].split("WORK EXPERIENCE")[0]
    basics = ["Python", "pandas", "NumPy", "SciPy", "Matplotlib", "Seaborn", "Plotly", "TensorFlow", "Docker", "Kubernetes"]
    skills_part = text.split("SKILLS")[1].split("WORK EXPERIENCE")[0]
    assert [b for b in basics if b not in skills_part] == []
    assert rep["skills"]["family"] == "data_scientist" and rep["skills"]["rows"] == list(want)


# ========================================================================================================== 5. recent roles, two bullets
def test_a_role_end_date_is_read_from_its_dates():
    assert selection.role_end("May 2026 – Sep 2026", TODAY) == date(2026, 9, 30)
    assert selection.role_end("Jan 2025 - Aug 2025", TODAY) == date(2025, 8, 31)
    assert selection.role_end("2025 – 2026", TODAY) == date(2026, 12, 31)
    assert selection.role_end("Jan 2023 – Present", TODAY) == TODAY
    assert selection.role_end("whenever", TODAY) is None
    assert selection.is_recent({"dates": "Jan 2023 – Sep 2024"}, 2, TODAY) is False
    assert selection.is_recent({"dates": "Jan 2023 – Nov 2024"}, 2, TODAY) is True
    assert selection.is_recent({"dates": "nonsense"}, 2, TODAY) is False


def test_a_recent_role_gets_two_bullets_even_when_its_other_bullets_are_near_duplicates():
    fb = bank()
    dc = next(e for e in fb["experience"] if e["id"] == "dc180")
    base = "Led the architecture for a five person team and built the Python prototype with FastMCP and PostgreSQL for the client"
    dc["bullets"] = [{"id": "d1", "text": base + ".", "terms": ["mcp", "python"]},
                     {"id": "d2", "text": base + " and ran a demo.", "terms": ["mcp"]},
                     {"id": "d3", "text": base + " and a live demo.", "terms": ["mcp"]}]
    layout = selection.Layout((4, 5), (1, 2), 5, (2, 3), recent_min=2)
    sel = selection.select_cv(fb, anaplan_kws(), None, layout=layout, jd=anaplan_jd())
    assert len(sel["experience"]["dc180"]) >= 2
    old = selection.select_cv(fb, anaplan_kws(), None, layout=selection.Layout((4, 5), (1, 2), 5, (2, 3)), jd=anaplan_jd())
    assert len(old["experience"]["dc180"]) == 1                                 # what the near-duplicate filter did before the rule


def _ctx_of(fb: dict, per_role: dict[str, int], per_project: int = 2, n_projects: int = 5) -> dict:
    exp = [{**{k: v for k, v in e.items() if k != "bullets"}, "bullet_ids": [b["id"] for b in e["bullets"][:per_role.get(e["id"], 1)]],
            "bullets": [b["text"] for b in e["bullets"][:per_role.get(e["id"], 1)]]} for e in fb["experience"]]
    pro = [{**{k: v for k, v in p.items() if k != "bullets"}, "bullet_ids": [b["id"] for b in p["bullets"][:per_project]],
            "bullets": [b["text"] for b in p["bullets"][:per_project]]} for p in fb["projects"][:n_projects]]
    return {"experience": exp, "projects": pro}


def test_the_shrink_loop_trims_projects_before_a_recent_role_goes_below_two_bullets():
    fb, kws = bank(), anaplan_kws()
    layout = selection.Layout((4, 5), (3, 4), 5, (2, 3), recent_min=2)
    ctx = _ctx_of(fb, {"az": 4, "dc180": 3, "freelance": 3, "avasoft": 3}, per_project=2)
    counts = lambda: {e["id"]: len(e["bullets"]) for e in ctx["experience"]}      # noqa: E731
    while len(ctx["projects"]) > 1:
        assert tailor._shrink(ctx, kws, layout)
        now = counts()
        assert min(now["dc180"], now["freelance"]) >= 2, (now, [p["id"] for p in ctx["projects"]])
        if now["az"] < 4 or now["dc180"] < 3 or now["freelance"] < 3:           # a role below the layout's floor: the projects are already down
            assert len(ctx["projects"]) <= tailor.MIN_PROJECTS_KEPT and all(len(p["bullets"]) == 1 for p in ctx["projects"]), now
    while tailor._shrink(ctx, kws, layout):                                     # and only then may a recent role fall to its last bullet
        pass
    assert min(counts().values()) == 1 and len(ctx["projects"]) == 1


@pytest.mark.slow
def test_the_anaplan_cv_gives_180_degrees_consulting_and_the_freelance_role_two_bullets_or_more(run):
    rep, fb, _ = run
    counts = {e["id"]: len(e["bullets"]) for e in rep["context"]["experience"]}
    assert counts["dc180"] >= 2 and counts["freelance"] >= 2 and counts["az"] >= 2, counts
    assert rep["cv_pages"] == 2
