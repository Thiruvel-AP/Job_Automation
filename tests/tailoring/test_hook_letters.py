"""letter.mode: hook (tailoring/letters.py): letter A, ASSEMBLED in code (the hook's sentence, two sentences from two bullets, a
20-minute ask that names a problem from the ad), at most 250 words; there is no letter B (it was the model's gap sentence); a
relocation line from applicant.yaml; the greeting for a team or an agency; review's `l` cycling A -> none; the note. No network, no
model (tests/tailoring/test_letter_assembled.py has the assembly and the guard)."""
import json
import re
from pathlib import Path

import pytest
import yaml

from careerops import ats
from careerops.llm import MockLLM, TaskUnavailable
from careerops.review import workflow as review
from careerops.tailoring import letters as L
from careerops.tailoring import tailor
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

KEYWORDS = {"required": ["LangChain", "Python", "Kubernetes"], "preferred": ["MLflow"], "company_hook": "Northwind builds oncology tools.",
            "team": "", "context_terms": []}


@pytest.fixture
def hook_settings(settings):
    settings.raw["letter"]["mode"] = "hook"
    return settings


def llm_(**handlers):
    return MockLLM({"extract_keywords": lambda ctx: dict(KEYWORDS), **handlers})


def run(settings, fb, job, llm=None):
    return tailor.tailor_job(job, fb, llm or llm_(), settings, settings.path("outputs"))


def words(paragraphs):
    return len(" ".join(paragraphs).split())


# ------------------------------------------------------------------ the pure pieces
def test_letter_mode_hook_is_a_mode(settings):
    settings.raw["letter"]["mode"] = "hook"
    assert tailor.letter_mode(settings) == "hook"
    settings.raw["letter"]["mode"] = "auto"
    with pytest.raises(ValueError, match="one of off, draft, hook"):
        tailor.letter_mode(settings)


def test_the_shipped_default_is_off():
    from careerops.config import load_settings
    s = load_settings()
    assert s["letter"]["mode"] == "off" and tailor.letter_mode(s) == "off"
    assert s["letter"]["closing_hook"] == "Could we take 20 minutes to talk about {problem}?"
    assert s["letter"]["greeting_hook"] == "Dear {company}{team} team" and s["letter"]["greeting_agency"] == "Hi {company} team"


def test_the_greeting_names_the_team_when_the_ad_does_and_an_agency_gets_hi(settings):
    job = {"company": "Anaplan", "agency": 0}
    assert L.greeting(job, "Functional Analytics", settings) == "Dear Anaplan Functional Analytics team"
    assert L.greeting(job, "Functional Analytics team", settings) == "Dear Anaplan Functional Analytics team"
    assert L.greeting(job, "", settings) == "Dear Anaplan team"
    assert L.greeting({"company": "Hays", "agency": 1}, "", settings) == "Hi Hays team"
    assert L.greeting({"company": "Hays", "agency": 1}, "Data", settings) == "Hi Hays team"


@pytest.mark.parametrize("applicant, location, want", [
    ({"relocation": {"willing": True, "scope": "anywhere", "letter_line": "I am ready to relocate to {city}."}}, "London, UK",
     "I am ready to relocate to London."),
    ({"relocation": {"willing": True, "scope": "anywhere", "letter_line": "I am ready to relocate to {city}."}}, "Bristol, UK", ""),   # home
    ({"relocation": {"willing": True, "scope": "anywhere", "letter_line": "I am ready to relocate to {city}."}}, "Remote, UK", ""),
    ({"relocation": {"willing": True, "scope": "anywhere", "letter_line": "I am ready to relocate to {city}."}}, "United Kingdom", ""),
    ({"relocation": {"willing": False, "scope": "anywhere", "letter_line": "I am ready to relocate to {city}."}}, "London, UK", ""),
    ({"relocation": {"willing": True, "scope": "uk", "letter_line": "I am ready to relocate to {city}."}}, "Berlin, Germany", ""),
    ({"relocation": {"willing": True, "scope": "uk", "letter_line": "I am ready to relocate to {city}."}}, "Leeds, England", "I am ready to relocate to Leeds."),
    ({"relocation": {"willing": True, "scope": "europe", "letter_line": "I am ready to relocate to {city}."}}, "London, UK", ""),
    ({"relocation": {"willing": True, "letter_line": ""}}, "London, UK", ""),
    ({}, "London, UK", ""), (None, "London, UK", "")])
def test_the_relocation_line(applicant, location, want):
    fb = {"identity": {"location": "Bristol, UK"}}
    assert L.relocation_line({"location": location}, applicant, fb) == want


def test_the_closing_names_the_problem_and_asks_for_twenty_minutes(settings):
    from careerops.hook import Hook
    hk = Hook("feature engineering and MLflow", "a1", "5", "h", "p", "l")
    assert L.closing(hk, settings) == "Could we take 20 minutes to talk about feature engineering and MLflow?"


def test_learn_list_is_what_the_bank_cannot_claim_with_deny_listed_terms_marked():
    kws = [Keyword("Python", 2, True), Keyword("dbt", 2, True), Keyword("Snowflake", 1, False), Keyword("retail", 1, False)]
    rows = L.learn_list(kws, ["Python"], ["dbt"])
    assert [(r["term"], r["confirmed_absent"], r["required"]) for r in rows] == [("dbt", True, True), ("Snowflake", False, False),
                                                                                  ("retail", False, False)]


def test_the_gap_is_the_heaviest_required_tool_of_the_learn_list_in_the_ads_casing():
    kws = [Keyword("LightGBM", 3, True), Keyword("dbt", 2, True), Keyword("retail", 2, True), Keyword("Optuna", 1, False)]
    learn = L.learn_list(kws, [], ["dbt"])
    assert L.pick_gap(kws, learn, "We use lightgbm daily and DBT.") == "lightgbm" or L.pick_gap(kws, learn, "x") == "LightGBM"
    assert L.pick_gap([Keyword("retail", 2, True)], L.learn_list([Keyword("retail", 2, True)], [], []), "retail") is None   # not a tool
    assert L.pick_gap([Keyword("Optuna", 1, False)], L.learn_list([Keyword("Optuna", 1, False)], [], []), "Optuna") is None   # not required


def test_the_bridge_is_a_neighbour_term_of_the_bank_else_its_heaviest_claimable_tool():
    kws = [Keyword("LightGBM", 3, True), Keyword("XGBoost", 3, True), Keyword("PyTorch", 2, True)]
    possible = {"LightGBM": [{"via": "XGBoost", "where": ["bullet b1"]}]}
    assert L.pick_bridge("LightGBM", {}, kws, ["XGBoost", "PyTorch"], possible) == "XGBoost"
    assert L.pick_bridge("LightGBM", {}, kws, ["XGBoost", "PyTorch"], None) == "XGBoost"           # heaviest claimable framework
    assert L.pick_bridge("LightGBM", {}, kws, [], None) is None


def test_paste_problems_catch_any_fact_bank_bullet_not_only_the_evidence():
    bullets = ["Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines and IAM roles."]
    assert L.paste_problems(["I deployed the agent as a Docker container on AWS Lambda with ECR image pipelines and IAM roles."], bullets)
    assert not L.paste_problems(["Docker and AWS Lambda are where most of my deployment work happened."], bullets)


def test_next_choice_goes_a_b_none_and_skips_a_missing_variant():
    ok = {"status": "ok"}
    both = {"A": ok, "B": ok}
    assert [L.next_choice(both, c) for c in ("A", "B", "none")] == ["B", "none", "A"]
    only_a = {"A": ok, "B": {"status": "none"}}
    assert [L.next_choice(only_a, c) for c in ("A", "none")] == ["none", "A"]


# ------------------------------------------------------------------ the letter of a tailoring run (assembled in code)
@pytest.mark.slow
def test_a_run_makes_letter_a_that_follows_the_rules_and_there_is_no_letter_b(hook_settings, fb_fixture, job):
    llm = llm_()
    rep = run(hook_settings, fb_fixture, job, llm)
    letters, hk = rep["letters"], rep["hook"]
    assert "cover_letter" not in llm.calls                                           # no model writes any of it
    assert letters["choice"] == "A" and rep["letter_choice"] == "A" and rep["letter"]["variant"] == "A"
    a, b = letters["A"], letters["B"]
    assert a["status"] == "ok" and b["status"] == "none" and "no model" in b["reason"]
    paras = a["paragraphs"]
    assert paras[0] == hk["letter_sentence"]                                          # sentence 1 is the hook's
    assert words(paras) <= 250 == hook_settings["scoring"]["letter_max_words"]
    assert paras[-1].endswith("?") and "20 minutes" in paras[-1] and hk["problem"] in paras[-1]
    assert Path(a["pdf"]).is_file() and Path(a["tex"]).is_file() and a["pages"] == 1
    assert Path(a["pdf"]).name == "Ada Lovelace Cover Letter.pdf"                    # the name that goes to the employer
    assert "Dear Northwind Health AI team," in pdf_text(Path(a["pdf"]))
    assert Path(a["pdf"]).parent.name == "A" and b["pdf"] is None
    assert rep["letter_pdf"] == a["pdf"] and Path(rep["letter_pdf"]).parent.parent.parent == Path(rep["out_dir"])


@pytest.mark.slow
def test_the_letter_a_ats_numbers_are_reported_and_only_the_cv_is_gated(hook_settings, fb_fixture, job):
    rep = run(hook_settings, fb_fixture, job)
    a = rep["ats"]
    assert a["letter"] is not None and a["combined"] is not None and a["gate"] in ("ok", "above", "retry", "stretch")
    assert a["combined"] >= a["strict"] - 1e-9                                           # a letter never lowers the CV's own hits
    assert set(a["variants"]) == {"A"}                                                   # B was not made: no numbers for it


@pytest.mark.slow
def test_an_agency_gets_hi_and_a_named_team_is_in_the_salutation(hook_settings, fb_fixture, job):
    rep = run(hook_settings, fb_fixture, {**job, "agency": 1})
    assert "Hi Northwind Health AI team," in pdf_text(Path(rep["letters"]["A"]["pdf"]))
    llm = llm_(extract_keywords=lambda ctx: {**KEYWORDS, "team": "Clinical Data"})
    jd = job["description"] + "\nYou will join the Clinical Data team.\n"
    rep = run(hook_settings, fb_fixture, {**job, "description": jd}, llm)
    assert "Dear Northwind Health AI Clinical Data team," in pdf_text(Path(rep["letters"]["A"]["pdf"]))


@pytest.mark.slow
def test_the_relocation_line_comes_before_the_call_to_action(hook_settings, fb_fixture, job, tmp_path):
    applicant = tmp_path / "applicant.yaml"
    applicant.write_text(yaml.safe_dump({"relocation": {"willing": True, "scope": "anywhere", "remote_ok": True,
                                                         "letter_line": "I am ready to relocate to {city}."}}), encoding="utf-8")
    hook_settings.raw["paths"]["applicant"] = str(applicant)
    rep = run(hook_settings, fb_fixture, {**job, "location": "London, UK"})
    last = rep["letters"]["A"]["paragraphs"][-1]
    assert last.startswith("I am ready to relocate to London. Could we take 20 minutes") and last.endswith("?")
    rep = run(hook_settings, fb_fixture, {**job, "id": "manual:b", "location": "Bristol, UK"})
    assert "relocate" not in rep["letters"]["A"]["paragraphs"][-1]                       # the home town: nothing to say


# ------------------------------------------------------------------ when a letter cannot be made
@pytest.mark.slow
def test_when_no_sentence_can_be_built_there_is_no_letter_and_no_template(hook_settings, fb_fixture, job, monkeypatch):
    monkeypatch.setattr(L, "assemble_body", lambda *a, **k: [])
    rep = run(hook_settings, fb_fixture, job)
    assert rep["letters"]["choice"] == "none" and rep["letter"]["status"] == "none" and rep["letter_pdf"] is None
    assert rep["letters"]["A"]["status"] == rep["letters"]["B"]["status"] == "none" and "paste" in rep["letters"]["A"]["reason"]
    assert not list(Path(rep["out_dir"]).rglob("*Cover Letter*"))                         # no template letter in hook mode


@pytest.mark.slow
def test_a_built_letter_that_fails_its_own_guard_is_not_written(hook_settings, fb_fixture, job, monkeypatch):
    monkeypatch.setattr(L, "assemble_body", lambda *a, **k: ["At Northwind our LLM migration was validated.", "We built it."])
    rep = run(hook_settings, fb_fixture, job)
    assert rep["letters"]["A"]["status"] == "none" and rep["letter_pdf"] is None
    assert any("'our'" in p for p in rep["letters"]["A"]["problems"]) and any("'we'" in p for p in rep["letters"]["A"]["problems"])


@pytest.mark.slow
def test_hook_mode_without_a_hook_writes_the_one_draft_letter(hook_settings, fb_fixture, job):
    hook_settings.raw["hook"] = {"enabled": False}
    rep = run(hook_settings, fb_fixture, job)
    assert rep["hook"] is None and rep["letters"] is None and rep["letter"]["status"] == "ok" and Path(rep["letter_pdf"]).is_file()


# ------------------------------------------------------------------ review: l cycles, the chosen letter is the one that goes
@pytest.mark.slow
def test_review_l_cycles_a_none_a_because_b_was_not_made(hook_settings, db, fb_fixture, job):
    rep = run(hook_settings, fb_fixture, job)
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], letter_score=rep["letter"]["score"])
    seen = []
    for _ in range(3):
        rep = review.cycle_letter(db, db.job(job["id"]), rep)
        seen.append((rep["letter_choice"], rep["letter"]["status"], Path(rep["letter_pdf"]).parent.name if rep["letter_pdf"] else None))
    assert seen == [("none", "declined", None), ("A", "ok", "A"), ("none", "declined", None)]
    saved = json.loads((Path(rep["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    assert saved["letter_choice"] == "none" and saved["letter_pdf"] is None


@pytest.mark.slow
def test_choosing_no_letter_needs_no_extra_words_to_approve_and_the_archive_goes_without(hook_settings, db, fb_fixture, job):
    from careerops.apply import archive
    rep = run(hook_settings, fb_fixture, job)
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"])
    rep = review.cycle_letter(db, db.job(job["id"]), rep)
    assert rep["letter"]["status"] == "declined" and review.letter_status(rep) == "declined"
    review.approve(db, db.job(job["id"]), rep, hook_settings.path("preferences"))              # no 'no letter' confirmation
    assert db.job(job["id"])["status"] == "approved"
    sources, letter_missing = archive._sources(dict(db.job(job["id"])), rep, False)
    assert letter_missing and not any("Cover Letter" in str(p) for p in sources)


@pytest.mark.slow
def test_the_review_screen_lists_the_letter_and_says_why_there_is_no_b_and_the_menu_has_l_and_n(hook_settings, db, fb_fixture, job):
    rep = run(hook_settings, fb_fixture, job)
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], letter_score=rep["letter"]["score"])
    text = review.summary(db.job(job["id"]), rep, hook_settings)
    assert "Letters (hook mode): chosen A" in text and "  A: ok |" in text and "  B: none (" in text
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return "s"

    review.review_job(db, db.job(job["id"]), hook_settings, fb_fixture, ask=ask)
    assert prompts[0] == "[a]pprove  [e]dit  [r]eject  [l]etter A/B/none  [g]enerate letters again  [n]ote  [o]pen PDFs  [s]kip  [q]uit > "


# ------------------------------------------------------------------ the note
@pytest.mark.slow
def test_the_note_has_every_section_and_the_contact_is_a_todo(hook_settings, fb_fixture, job):
    rep = run(hook_settings, fb_fixture, job)
    text = (Path(rep["out_dir"]) / "note.md").read_text(encoding="utf-8")
    for section in ("## Hook", "## ATS", "## Learn list", "## Contact", "## Question", "## Flags", "## Gap"):
        assert section in text, section
    assert "TODO: you find on LinkedIn: hiring manager" in text
    assert f"**Strict CV:** {rep['ats']['strict']:.1f}%" in text and "**Claimable ceiling:**" in text and "**CV + letter:**" in text
    assert "- Kubernetes (confirmed absent)" in text                                         # a deny-listed term of the ad
    assert "## Gap\n\n- None: no gap sentence is written" in text                             # B is not made: no model writes a gap sentence
    assert "Posting age" in text and "Visa / sponsorship" in text and "licensed sponsor" in text
    question = text.split("## Question\n\n", 1)[1].split("\n", 1)[0]
    assert any(k["term"] in question for k in rep["keywords"])                              # it names a term of the ad
