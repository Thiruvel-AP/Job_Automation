"""The CV profile is assembled, not merged (round of 17 Oct, item 2).

Profile = the hook's `profile_line` + one or two `profile_facts` sentences chosen by keyword score, each ending with a full stop. No
model writes it. Evidence: the output read "...validating LLM migration Data Scientist with experience...": two sentences fused with
no full stop, and "Data Scientist" appeared twice. No network, no model.
"""
import copy
import re
from pathlib import Path

import pytest

from careerops import hook as H
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.profile import assemble, problems, role_phrases
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

LINE = "Data scientist who takes models from prototype to production: at Northwind I shipped an LLM agent to AWS Lambda, validated by ~65 tests."
FACTS = ["Data Scientist with experience building production LLM agents on a research platform",       # the title again: skipped
         "MSc Data Science, University of Northfield (completed 2026)",
         "Owns data workflows end-to-end in Python and SQL, from merging structured data through deployment with Docker and MLflow",
         "Turns statistical outputs into clear recommendations for technical and non-technical stakeholders",
         "3rd place, national datathon"]
KWS = [Keyword("Python", 3, True), Keyword("SQL", 3, True), Keyword("Docker", 2, True), Keyword("MLflow", 2, True),
       Keyword("LLM", 3, True), Keyword("stakeholders", 1, False)]
JD = "We need Python, SQL, Docker, MLflow and LLM experience and good communication with stakeholders. " * 2


def bank(facts=FACTS):
    return {"profile_facts": list(facts), "deny_terms": ["kubernetes"], "identity": {"headline": "Data Scientist"}}


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def test_the_profile_is_the_hook_line_plus_one_or_two_facts_each_with_a_full_stop():
    out = assemble(bank(), KWS, JD, LINE)
    assert out["text"].startswith(LINE) and 2 <= len(sentences(out["text"])) <= 3
    for s in sentences(out["text"]):
        assert s.endswith((".", "!", "?")), s
    for fact in out["facts"]:
        assert fact in FACTS and fact.rstrip(".") + "." in out["text"]
    assert out["text"] == " ".join([LINE, *(f.rstrip(".") + "." for f in out["facts"])])


def test_facts_are_chosen_by_keyword_score_and_bank_order_breaks_a_tie():
    out = assemble(bank(), KWS, JD, LINE)
    assert out["facts"][0] == FACTS[2]                                   # Python, SQL, Docker and MLflow: the heaviest
    flat = assemble(bank(["Plays chess puzzles weekly.", "Climbs mountains in winter.", "Bakes sourdough bread."]), [Keyword("zzz", 1, False)], "zzz", LINE)
    assert flat["facts"] == ["Plays chess puzzles weekly.", "Climbs mountains in winter."]


def test_no_sentence_repeats_the_title():
    """The 17 Oct output said 'Data Scientist' twice: the opener and a fact that begins with the title."""
    out = assemble(bank(), KWS, JD, LINE)
    assert FACTS[0] not in out["facts"]
    assert sum(len(role_phrases(s)) for s in sentences(out["text"])) == 1, out["text"]
    assert problems(out["text"]) == []
    assert problems("Data scientist who ships. Data Scientist with experience in testing.") == ["the title 'data scientist' is repeated"]


def test_a_fact_that_only_restates_the_opener_is_left_out():
    facts = ["Shipped an LLM agent to AWS Lambda validated by ~65 tests at Northwind", "MSc Data Science, University of Northfield (completed 2026)"]
    out = assemble(bank(facts), KWS, JD, LINE)
    assert out["facts"] == [facts[1]]


def test_two_sentences_are_never_fused():
    out = assemble(bank(), KWS, JD, LINE)
    assert problems(out["text"]) == []
    assert problems("Data scientist who ships models validating LLM migration Data Scientist with experience in testing") == [
        "the title 'data scientist' is repeated", "sentence does not end in punctuation: 'Data scientist who ships models validating LLM migration Data Scientist with experience in testing'"]


def test_a_deny_listed_fact_is_never_used():
    out = assemble(bank(["Runs Kubernetes clusters for fun", "MSc Data Science, University of Northfield (completed 2026)"]), KWS, JD, LINE)
    assert out["facts"] == ["MSc Data Science, University of Northfield (completed 2026)"]


def test_without_a_hook_line_the_profile_is_the_facts_alone():
    out = assemble(bank(), KWS, JD, "")
    assert out["text"] == " ".join(f.rstrip(".") + "." for f in out["facts"]) and 1 <= len(out["facts"]) <= 2


def test_make_profile_and_its_model_call_are_gone():
    assert not hasattr(tailor, "make_profile")
    from careerops.tailoring import prompts as P
    assert not hasattr(P, "PROFILE_SYSTEM")


@pytest.mark.slow
def test_a_tailored_cv_has_an_assembled_profile_and_makes_no_profile_call(settings, fb_fixture, job):
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = load_yaml(Path(__file__).parent.parent / "fixtures" / "tailoring" / "hook_library_fixture.yaml")
    fb["profile_facts"] = ["Data Scientist with experience in production LLM agents", *fb["profile_facts"]]      # the 17 Oct failure
    llm = MockLLM()
    rep = tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    assert "tailor_profile" not in llm.calls and "hook" not in llm.calls
    text = rep["context"]["profile"]
    hk = H.Hook.from_dict(rep["hook"])
    assert text.startswith(hk.profile_line) and problems(text) == [] and "Data Scientist with experience" not in text
    assert all(s.endswith((".", "!", "?")) for s in sentences(text))
    flat = " ".join(pdf_text(Path(rep["cv_pdf"])).split())
    assert " ".join(hk.profile_line.split()) in flat
