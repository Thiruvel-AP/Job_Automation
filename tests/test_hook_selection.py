"""Hook selection needs evidence in the ad, and the library headlines are linted (never rewritten).

A library hook is eligible only when its `terms` hit at least 2 of the ad's top 10 weighted keywords; with no eligible hook the CV keeps
the role family's headline with no number and nothing is written (no model, no code template). The review screen prints why the hook
won and any lint warning; `careerops doctor` lints the whole library. No network, no model; the fictional bank is tests/fixtures/tailoring/.
"""
import copy

import pytest

from careerops import hook as H
from careerops import setup
from careerops.llm import MockLLM
from careerops.review import workflow as review
from careerops.tailoring import render, tailor
from careerops.tailoring.scorer import Keyword

JOB = {"id": "manual:sel1", "company": "Northwind Health AI", "title": "Data Scientist", "location": "Bristol, UK",
       "description": "We need a data scientist for forecasting and demand planning with Python and SQL."}
OLD_HEADLINE = "Per gene models cutting RAM from 8.2 to 5.5 GB"        # the shape of the old library headline that wasted the line


def entry(eid, proof, terms, headline="LLM agent behind automated tests"):      # v3.5.2: no number its proof might not hold (hook_claims)
    return {"id": eid, "proof": proof, "terms": terms, "headline": headline, "profile_line": "Profile line.", "letter_line": "Letter line."}


def ranked(*terms_and_weights) -> list[Keyword]:
    return [Keyword(t, w, True) for t, w in terms_and_weights]


@pytest.fixture
def bank(fb_fixture) -> dict:
    return copy.deepcopy(fb_fixture)


# ---------------------------------------------------------------- eligibility
def test_a_hook_needs_two_of_the_top_ten_keywords(bank):
    bank["hook_library"] = [entry("one", "a4", ["alpha"]), entry("two", "p2a", ["alpha", "beta"])]
    kws = ranked(("alpha", 3), ("beta", 2), ("gamma", 1))
    assert H.pick_library_entry(bank, kws)["entry"]["id"] == "two"             # "one" hits only alpha
    bank["hook_library"] = [entry("one", "a4", ["alpha"])]
    notes: list[str] = []
    assert H.pick_library_entry(bank, kws, notes) is None
    assert any("at least 2" in n and "top 10" in n and "'one' with 1" in n and "'alpha' (3)" in n for n in notes), notes


def test_keywords_below_the_top_ten_do_not_count(bank):
    kws = ranked(*[(f"k{i}", 5) for i in range(10)], ("low1", 1), ("low2", 1))      # low1 and low2 are keywords 11 and 12
    bank["hook_library"] = [entry("late", "a4", ["low1", "low2"]), entry("early", "p2a", ["k0", "k9"])]
    assert H.top_keyword_overlap(bank["hook_library"][0], kws) == []
    assert H.pick_library_entry(bank, kws)["entry"]["id"] == "early"
    bank["hook_library"] = [entry("late", "a4", ["low1", "low2"])]
    assert H.pick_library_entry(bank, kws) is None


def test_the_top_ten_is_by_weight_not_by_list_position(bank):
    kws = ranked(*[(f"w{i}", 1) for i in range(10)], ("heavy1", 3), ("heavy2", 3))      # the heavy ones are last in the list
    assert [t for t, _ in H.top_keyword_overlap(entry("x", "a4", ["heavy1", "heavy2", "w9"]), kws)] == ["heavy1", "heavy2"]


def test_a_job_ad_with_no_overlapping_hook_gets_no_hook_and_nothing_is_written(bank):
    bank["hook_library"] = [entry("x", "a4", ["kubernetes", "rust"])]
    notes: list[str] = []
    assert H.build_hook(JOB, ranked(("Python", 3), ("SQL", 3), ("forecasting", 2)), bank, role="Data Scientist", notes=notes) is None
    assert any("no hook_library entry hits at least 2" in n for n in notes), notes
    bank.pop("hook_library")
    notes = []
    assert H.build_hook(JOB, ranked(("Python", 3)), bank, role="Data Scientist", notes=notes) is None and "the fact bank has no hook_library" in notes


@pytest.mark.slow
def test_the_cv_of_an_ad_with_no_overlapping_hook_carries_the_family_headline_and_no_number(settings, bank):
    bank["hook_library"] = [entry("x", "a4", ["no-such-term-in-any-ad", "another-unmatched-term"])]
    rep = tailor.tailor_job({**JOB, "family": "data_scientist"}, bank, MockLLM(), settings, settings.path("outputs"))
    assert rep["hook"] is None and rep["hook_family_headline"] == rep["headline"] and rep["headline"]
    assert "hook_headline" not in rep["context"] and rep["context"]["id"]["headline"] == rep["headline"]
    assert any(w.startswith("hook: no library hook qualifies") and rep["headline"] in w for w in rep["analysis_warnings"])
    lines = review.hook_lines(rep, color=False)
    assert lines[0].startswith("!! no hook: the headline has no number")                     # v3.5.3: the warning comes first
    assert lines[1].startswith("Hook: none qualifies") and f"'{rep['headline']}' with no number" in lines[1]
    assert any("at least 2" in ln for ln in lines)          # v3.6: "no hook_library entry has proof bullets that cover at least 2 existing requirements"


FAMILY_HEADLINE = "Data Scientist, forecasting and demand planning"


@pytest.mark.parametrize("template", ["cv.tex.j2", "cv2.tex.j2"])
def test_the_tex_of_a_cv_with_no_hook_carries_the_family_headline(bank, template):
    ctx = tailor._context(bank, {"experience": {}, "projects": []}, {}, "Profile.", [], FAMILY_HEADLINE, {})
    assert not [k for k in ctx if k.startswith("hook_")]                      # no hook: no hook_* key at all
    ctx["linespread"] = 1.0
    tex = render.env().get_template(template).render(**ctx)
    assert FAMILY_HEADLINE in tex
    assert tex.index(FAMILY_HEADLINE) < tex.index(bank["identity"]["email"])      # under the name, above the contact line
    ctx["hook_headline"], ctx["hook_number"] = "LLM agent behind ~65 tests", "65"
    hooked = render.env().get_template(template).render(**ctx)
    assert "LLM agent behind" in hooked and FAMILY_HEADLINE not in hooked        # a hook's headline replaces the family's, never both


# ---------------------------------------------------------------- the reason it won
def test_the_reason_names_the_overlapping_terms_and_their_weights(bank):
    bank["hook_library"] = [entry("h_fc", "p2a", ["forecasting", "demand", "python"])]
    kws = ranked(("forecasting", 3), ("demand", 2), ("SQL", 3))
    hk = H.build_hook(JOB, kws, bank, role="Data Scientist")
    assert hk.hook_id == "h_fc" and hk.overlap == [["forecasting", 3.0], ["demand", 2.0]]
    assert hk.reason.startswith("h_fc won: its terms hit 2 of the ad's top 10 keywords, 'forecasting' (3), 'demand' (2)")
    assert H.Hook.from_dict(hk.to_dict()).reason == hk.reason                           # survives report.json


def test_the_review_screen_prints_the_selection_reason(bank):
    bank["hook_library"] = [entry("h_fc", "p2a", ["forecasting", "demand"])]
    hk = H.build_hook(JOB, ranked(("forecasting", 3), ("demand", 2)), bank, role="Data Scientist")
    lines = review.hook_lines({"hook": hk.to_dict(), "hook_placed": {}})
    assert f"  why it won: {hk.reason}" in lines
    assert "'forecasting' (3), 'demand' (2)" in "\n".join(lines)


# ---------------------------------------------------------------- the lint
def test_the_old_ram_headline_triggers_the_lint():
    warnings = H.headline_lint(OLD_HEADLINE)
    assert any("more than one number (8.2, 5.5)" in w for w in warnings), warnings
    assert any("hardware or resource unit" in w and "RAM" in w and "GB" in w for w in warnings), warnings


@pytest.mark.parametrize("headline, fragment", [
    ("A" * 61, "61 characters > 60"),
    ("Models cut from 12 to 3 steps", "more than one number (12, 3)"),
    ("LLM agent behind ~65 tests", None),
    ("Pipeline cut latency to 80 ms", "unit (ms)"),
    ("Trained on a GPU", "unit (GPU)"),
    ("Cut CPU use by 5GB", "unit (CPU, GB)"),
    ("Runs on T4 and S3", None),                       # a digit inside a name is not a number
    ("Warmsley programme", None),                      # "ms" inside a word is not a unit
    ("Fine-tuned Llama 3.1 8B to 92% accuracy", None),  # v3.5.4: a model name, version and size are not result numbers
    ("GPT-4o agent cut triage time 40%", None),
    ("Qwen2.5 7B and Python 3.12 served 11,459 users", None),
    ("A 70B model saved 30%", None),
    ("Llama 3.1 8B cut errors from 12 to 3", "more than one number (12, 3)"),   # two result numbers still count
    # v3.5.5: "A -> B" is one result (a before-to-after pair), whatever the arrow
    ("QLoRA on Llama 3.1 8B: held-out accuracy 16.2% → 27.3%", None),            # h_finetune, 11 Nov 2026
    ("Held-out accuracy 16.2% -> 27.3%", None),
    ("Completion 32% => 52%", None),
    ("Accuracy 16.2%→27.3%", None),
    ("Accuracy 16.2% → 27.3% and 5x faster", "more than one number (16.2, 5)"),    # a second result next to the pair still counts
    ("Accuracy 16.2% → 27.3%, recall 40% → 55%", "more than one number (16.2, 40)"),   # two pairs are two results
])
def test_the_lint_rules(headline, fragment):
    got = H.headline_lint(headline)
    assert (not got) if fragment is None else any(fragment in w for w in got), got


def test_a_sixty_character_headline_passes_and_the_lint_never_rewrites(bank):
    assert H.headline_lint("x" * 60) == []
    bank["hook_library"] = [entry("old", "a4", ["alpha", "beta"], headline=OLD_HEADLINE)]
    a4 = next(b for e in bank["experience"] for b in e["bullets"] if b["id"] == "a4")
    a4["text"] += " Per-gene models cut RAM from 8.2 to 5.5 GB."                    # v3.5.2: a hook's numbers must be its proof's (hook_claims)
    hk = H.build_hook(JOB, ranked(("alpha", 3), ("beta", 3)), bank, role="Data Scientist")
    assert hk.headline == OLD_HEADLINE                                                 # used word for word, warned about elsewhere
    lines = review.hook_lines({"hook": hk.to_dict(), "hook_placed": {}})
    assert any(ln.startswith("  LINT WARNING (headline):") and "RAM" in ln for ln in lines), lines


def test_the_library_lint_names_the_entry(bank):
    bank["hook_library"] = [entry("fine", "a4", ["a"]), entry("old", "a4", ["a"], headline=OLD_HEADLINE)]
    assert {eid for eid, _ in H.library_lint(bank)} == {"old"}


# ---------------------------------------------------------------- careerops doctor
def test_doctor_warns_about_a_library_headline_and_does_not_fail(bank, tmp_path):
    bank["hook_library"] = [entry("fine", "a4", ["a"]), entry("old", "a4", ["a"], headline=OLD_HEADLINE)]
    path = tmp_path / "fact_bank.yaml"
    path.write_text(__import__("yaml").safe_dump(bank), encoding="utf-8")
    rows = setup.hook_lint_checks(path)
    assert rows and all(ok == setup.WARN for _, ok, _ in rows) and all(name.startswith("hook 'old' headline:") for name, _, _ in rows)
    assert all(ok for _, ok, _ in rows)                                                # a warning is truthy: it never fails the run
    bank["hook_library"] = [entry("fine", "a4", ["a"])]
    path.write_text(__import__("yaml").safe_dump(bank), encoding="utf-8")
    assert setup.hook_lint_checks(path) == [("hook library headlines (1) pass the lint", True, "")]


@pytest.mark.slow                                    # doctor checks playwright
def test_doctor_runs_the_hook_lint(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(setup, "hook_lint_checks", lambda p: seen.append(p) or [("hook lint ran", True, "")])
    monkeypatch.setattr(setup, "model_checks", lambda *a, **k: [])
    monkeypatch.setattr(setup, "judge_checks", lambda *a, **k: [])
    monkeypatch.setattr(setup, "core_skill_checks", lambda *a, **k: [])
    monkeypatch.setattr(setup, "applicant_checks", lambda *a, **k: [])
    monkeypatch.setattr(setup, "reminder_checks", lambda *a, **k: [])
    assert ("hook lint ran", True, "") in setup.doctor(settings, ping=False) and seen == [settings.path("fact_bank")]
