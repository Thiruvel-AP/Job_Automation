"""letter.mode: off | draft | hook. Off asks for no letter: no letter call, no PDF, no fallback. Draft hands the model the evidence as
fields (method, data, outcome), never as the bullet sentence, under the same letter checks. Hook assembles the letter in code
(tests/tailoring/test_letter_assembled.py, test_hook_letters.py). The shipped default is off (round of 17 Oct)."""
from pathlib import Path

import pytest

from careerops import cli
from careerops.config import load_settings
from careerops.review import workflow as review
from careerops.tailoring import evidence_fields as ef
from careerops.tailoring import tailor
from careerops.tailoring.scorer import Keyword


# ------------------------------------------------------------------ the setting
def test_the_shipped_default_is_off():
    s = load_settings()
    assert s["letter"]["mode"] == "off" and tailor.letter_mode(s) == "off"


def test_the_suites_fixture_turns_the_letter_on(settings):
    assert tailor.letter_mode(settings) == "draft"


@pytest.mark.parametrize("value, want", [("off", "off"), ("draft", "draft"), (" Draft ", "draft"), ("OFF", "off"), (None, "off"),
                                         (False, "off")])          # YAML reads an unquoted `mode: off` as False
def test_letter_mode_reads_the_setting(settings, value, want):
    settings.raw["letter"]["mode"] = value
    assert tailor.letter_mode(settings) == want


def test_an_unknown_mode_is_refused_with_the_choices(settings):
    settings.raw["letter"]["mode"] = "auto"
    with pytest.raises(ValueError, match="letter.mode must be one of off, draft, hook, got 'auto'"):
        tailor.letter_mode(settings)


# ------------------------------------------------------------------ the evidence as fields
def test_fields_come_from_the_fact_bank_keys_or_say_not_stated():
    ev = {"id": "a1", "text": "Built a thing with Python and SQL.", "terms": ["python", "sql", "etl"], "where": "Acme"}
    # fix round 2: the `terms` the bullet's own text names, not the whole list (its synonyms and index words are not in the work)
    assert ef.fields(ev) == {"method": "Python; SQL", "data": "(not stated)", "outcome": "(not stated)"}
    assert ef.fields({**ev, "text": "Built a thing."})["method"] == "(not stated)"
    ev = {**ev, "result": "Cut review time by a third.", "data": "donor exports", "method": "pandas and SQL joins"}
    assert ef.fields(ev) == {"method": "pandas and SQL joins", "data": "donor exports", "outcome": "Cut review time by a third."}
    assert ef.fields({**ev, "outcome": "Used by the whole team."})["outcome"] == "Used by the whole team."     # outcome beats result
    assert ef.fields({"id": "x", "text": "t"}) == {"method": "(not stated)", "data": "(not stated)", "outcome": "(not stated)"}


def test_the_block_numbers_the_items_and_never_carries_the_sentence():
    ev = [{"id": "a1", "text": "Built a tool-calling LLM agent for a research platform.", "terms": ["llm", "agent"]},
          {"id": "b1", "text": "Built a churn classifier.", "terms": ["scikit-learn"], "result": "AUC 0.84"}]
    out = ef.block(ev)
    assert out == ("EVIDENCE 1\n  method: LLM; agent\n  data: (not stated)\n  outcome: (not stated)\n"
                   "EVIDENCE 2\n  method: (not stated)\n  data: (not stated)\n  outcome: AUC 0.84")
    assert all(e["text"] not in out for e in ev)


def test_letter_evidence_carries_the_optional_bank_keys_only_when_the_bank_has_them(fb_fixture):
    kws = [Keyword("langchain", 2.0, True)]
    plain = tailor.letter_evidence(fb_fixture, kws)
    assert all(set(e) == {"id", "text", "terms", "where"} for e in plain)
    bank = {**fb_fixture, "projects": [{**p, "bullets": [{**b, "method": "stratified CV", "result": "AUC 0.84"} for b in p["bullets"]]}
                                       for p in fb_fixture["projects"]],
            "experience": [{**e, "bullets": [{**b, "method": "stratified CV", "result": "AUC 0.84"} for b in e["bullets"]]}
                           for e in fb_fixture["experience"]]}
    rich = tailor.letter_evidence(bank, kws)
    assert rich and all(e.get("method") == "stratified CV" and e.get("result") == "AUC 0.84" for e in rich)


# ------------------------------------------------------------------ draft: what the model is sent
@pytest.mark.slow
def test_draft_sends_fields_not_sentences_and_says_so_in_the_system_prompt(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    call = [s for s in llm.sent if s["task"] == "cover_letter"][0]
    assert call["user"].count("EVIDENCE ") == 2 and "  method: " in call["user"] and "  outcome: (not stated)" in call["user"]
    assert "EVIDENCE:\n- " not in call["user"] and tailor.P.LETTER_FIELDS_NOTE in call["system"]
    for e in rep["letter"]["evidence"]:
        assert e["text"] not in call["user"] and e["text"] not in call["system"]                     # the sentence is not sent
    assert rep["letter"]["status"] == "ok"                                                            # MockLLM's draft passed every check


@pytest.mark.slow
def test_draft_hands_the_model_a_result_the_bank_gives(settings, fb_fixture, job, spy_llm):
    def with_results(entries):
        return [{**e, "bullets": [{**b, "result": f"RESULT-{b['id']}"} for b in e["bullets"]]} for e in entries]
    bank = {**fb_fixture, "experience": with_results(fb_fixture["experience"]), "projects": with_results(fb_fixture["projects"])}
    llm = spy_llm()
    tailor.tailor_job(job, bank, llm, settings, settings.path("outputs"))
    user = [s for s in llm.sent if s["task"] == "cover_letter"][0]["user"]
    assert user.count("outcome: RESULT-") == 2


# ------------------------------------------------------------------ off: nothing is asked for
@pytest.mark.slow
def test_off_makes_no_letter_call_writes_no_letter_and_says_off(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["mode"] = "off"
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    assert "cover_letter" not in [s["task"] for s in llm.sent] and "cover_letter" not in llm.calls
    assert rep["letter"]["status"] == "off" and rep["letter"]["reason"] == "letter.mode: off" and rep["letter"]["score"] is None
    assert rep["letter_pdf"] is None and rep["letter"]["paragraphs"] == [] and len(rep["letter"]["evidence"]) == 2
    out = Path(rep["out_dir"])
    assert not [p for p in out.rglob("*") if "Cover Letter" in p.name]                                # no PDF, no .tex
    assert Path(rep["cv_pdf"]).exists() and (out / "report.json").exists()
    assert tailor.letter_summary(rep["letter"]) == "off (letter.mode: off)"


@pytest.mark.slow
def test_off_still_produces_the_same_cv(settings, fb_fixture, job, spy_llm):
    on = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs") / "on")
    settings.raw["letter"]["mode"] = "off"
    off = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs") / "off")
    assert on["final"]["score"] == off["final"]["score"] and on["context"]["experience"] == off["context"]["experience"]


@pytest.mark.slow
def test_the_tailor_command_prints_letter_off_and_no_page_warning(settings, db, fb_fixture, job, spy_llm, capsys):
    settings.raw["letter"]["mode"] = "off"
    db.upsert_job(job)
    db.update_job(job["id"], status="triaged", ceiling_pre=0.9, tier=1, family="ai_engineer")
    cli.tailor_jobs(db, settings, fb_fixture, spy_llm(), [db.job(job["id"])], mock=True)
    out = capsys.readouterr().out
    assert "letter off (letter.mode: off)" in out and "!!" not in out and "ERROR" not in out
    assert db.job(job["id"])["status"] == "needs_review"                       # not letter_pending: nothing waits for a budget


@pytest.mark.slow
def test_review_shows_letter_off_and_approves_without_confirmation(settings, db, fb_fixture, job, spy_llm):
    settings.raw["letter"]["mode"] = "off"
    rep = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, settings.path("outputs"))
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=rep["out_dir"], tier=1, family="ai_engineer")
    row = db.job(job["id"])
    assert review.letter_status(rep) == "off"
    text = review.summary(row, rep)
    assert "Letter: off (letter.mode: off" in text and "NO LETTER" not in text and "FALLBACK" not in text
    review.approve(db, row, rep, settings.path("preferences"))                 # no confirm needed
    assert db.job(job["id"])["status"] == "approved"


@pytest.mark.slow
def test_regenerating_a_letter_that_is_off_makes_no_call(settings, fb_fixture, job, spy_llm):
    settings.raw["letter"]["mode"] = "off"
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    rep2 = tailor.regenerate_letter(job, fb_fixture, llm, settings, rep)
    assert rep2["letter"]["status"] == "off" and "cover_letter" not in llm.calls and rep2["letter_pdf"] is None
