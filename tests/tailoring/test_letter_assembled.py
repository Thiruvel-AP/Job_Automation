"""The letter is off by default; when on, it is assembled and checked (round of 17 Oct, item 4).

letter.mode: off in the shipped settings, so a live run uploads the CV only. With letter.mode: hook the letter is the hook entry's
`letter_line`, then two sentences each built from ONE selected bullet's own text (the paste-overlap check still applies), then the
call to action, whose problem phrase comes from the hook entry's `terms`, lower case, never title case. No model writes any of it.
A guard rejects "our", "we", "I help <Company>" and any noun phrase that links one bullet's object to a different bullet's tool.
Evidence (17 Oct): the letter said Docker and AWS Lambda ran "the test suite" (the agent was deployed there; the tests are another
bullet), "our LLM migration", and "I help Anaplan solve forecasting". No network, no model.
"""
import copy
from pathlib import Path

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_settings, load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import letters as L
from careerops.tailoring import tailor
from careerops.tailoring.grounding import sentences
from careerops.tailoring.letter_rules import PASTE_OVERLAP, paste_overlap
from careerops.tailoring.scorer import Keyword

GOLDEN = Path(__file__).parent.parent / "golden" / "anaplan"
LIBRARY = load_yaml(Path(__file__).parent.parent / "fixtures" / "tailoring" / "hook_library_fixture.yaml")
JD = (GOLDEN / "jd.txt").read_text(encoding="utf-8")
JOB = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan", "title": "Data Scientist, Functional Analytics (REQ 26490)",
       "location": "London, UK", "url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002", "description": JD}


def keywords():
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


@pytest.fixture
def bank(fb_fixture) -> dict:
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = copy.deepcopy(LIBRARY)
    return fb


@pytest.fixture
def hk(bank) -> H.Hook:
    return H.build_hook(JOB, keywords(), bank, role="Data Scientist")


def all_bullets(fb):
    return [b["text"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]]


# ---------------------------------------------------------------- off by default
def test_the_shipped_default_is_no_letter():
    s = load_settings()
    assert tailor.letter_mode(s) == "off"
    assert s["letter"]["closing_hook"] == "Could we take 20 minutes to talk about {problem}?"


@pytest.mark.slow
def test_a_live_run_with_the_shipped_default_makes_only_a_cv(settings, bank):
    settings.raw["letter"]["mode"] = "off"
    llm = MockLLM()
    rep = tailor.tailor_job(JOB, bank, llm, settings, settings.path("outputs"))
    assert rep["letter"]["status"] == "off" and rep["letter_pdf"] is None and "cover_letter" not in llm.calls
    assert tailor.letter_summary(rep["letter"]).startswith("off")
    assert not list(Path(rep["out_dir"]).glob("*Cover Letter*")) and not (Path(rep["out_dir"]) / "letters").exists()


# ---------------------------------------------------------------- the call to action
def test_the_problem_phrase_is_lower_case_nouns_from_the_entry_terms_never_title_case():
    assert L.noun_phrase(["Production", "Deployment", "Testing"], JD) == "production, deployment and testing"
    assert L.noun_phrase(["aws", "mlops", "ci/cd"], JD) == "AWS, MLOps and CI/CD"            # a name keeps its spelling
    assert L.noun_phrase(["testing"], JD) == "testing" and L.noun_phrase(["a", "b"], "") == "a and b"
    known = ["AWS Lambda", "Statistics", "scikit-learn", "Docker"]                               # the bank's own spellings
    assert L.noun_phrase(["aws lambda", "mlops"], JD, known) == "AWS Lambda and MLOps"
    assert L.noun_phrase(["statistics", "docker"], JD, known) == "statistics and Docker"          # a bare first capital is not a spelling
    assert L.noun_phrase([], JD) == ""


def test_the_call_to_action_names_the_problem_from_the_hook_entry_terms(hk, settings):
    cta = L.closing(hk, settings)
    assert cta == f"Could we take 20 minutes to talk about {hk.problem}?"
    terms = {t.lower() for t in LIBRARY[0]["terms"]}
    assert {w.lower() for w in hk.problem.replace(",", " ").split()} - {"and"} <= terms
    assert not any(w[0].isupper() and w not in ("AWS", "MLOps", "CI/CD", "Python", "SQL", "PyTorch") for w in hk.problem.replace(",", " ").split())


# ---------------------------------------------------------------- the two sentences
def test_each_body_sentence_is_built_from_one_selected_bullet_and_is_not_a_paste(bank, hk):
    kws = keywords()
    chosen = {b["id"] for sec in ("experience", "projects") for e in bank[sec] for b in e["bullets"]}
    evidence = L.body_evidence(bank, kws, chosen, JD, hk)
    sents = L.assemble_body(bank, evidence, hk, kws, JD, JOB["company"])
    assert len(sents) == 2 and len(evidence) == 2
    for s, e in zip(sents, evidence):
        assert s.endswith(".") and e["text"].split()[0].lower() in s.lower(), (s, e["text"])
        assert max(paste_overlap(s, b) for b in all_bullets(bank)) < PASTE_OVERLAP, s
        words = [w.lower() for w in e["text"].replace("(", " ").replace(")", " ").split()]
        assert " ".join(words[1:4]) in s.lower(), (s, words[:5])                # the bullet's own words, in order
    assert L.claim_problems(sents, JOB["company"], bank, L.exempt_words(hk, JOB, evidence)) == []


@pytest.mark.slow
def test_the_letter_is_the_letter_line_two_sentences_and_the_call_to_action_with_no_model(bank, hk, settings, tmp_path):
    settings.raw["letter"]["mode"] = "hook"

    class Boom(MockLLM):
        def json(self, *a, **k):
            raise AssertionError("no letter text may come from a model")

    kws = keywords()
    chosen = {b["id"] for sec in ("experience", "projects") for e in bank[sec] for b in e["bullets"]}
    out = L.make_letters(JOB, bank, kws, "", hk, Boom(), settings, tmp_path, "Ada Lovelace Cover Letter", "", chosen, [], None, {})
    a, b = out["A"], out["B"]
    assert a["status"] == "ok" and a["paragraphs"][0] == hk.letter_sentence and a["paragraphs"][-1].endswith(L.closing(hk, settings))
    assert len(sentences(" ".join(a["paragraphs"][1:-1]))) == 2 and a["words"] <= settings["scoring"]["letter_max_words"]
    assert b["status"] == "none" and "model" in b["reason"] and out["choice"] == "A"
    assert Path(a["pdf"]).is_file()


@pytest.mark.slow
def test_a_number_in_an_employers_name_is_not_a_new_number(bank, hk, settings, tmp_path):
    """Found by the stand-in run on the real bank: "at 180 Degrees Consulting Bristol, I ..." was refused for the new number 180."""
    settings.raw["letter"]["mode"] = "hook"
    bank["experience"][1]["org"] = "180 Degrees Consulting Bristol"
    chosen = {"b1", "b2", "b3"}                                                  # the evidence is that employer's bullets
    out = L.make_letters(JOB, bank, keywords(), "", hk, MockLLM(), settings, tmp_path, "Ada Lovelace Cover Letter", "", chosen, [], None, {})
    assert out["A"]["status"] == "ok", (out["A"]["reason"], out["A"]["problems"])
    assert "180 Degrees Consulting Bristol" in " ".join(out["A"]["paragraphs"])


# ---------------------------------------------------------------- the guard
@pytest.mark.parametrize("text, why", [
    ("At Northwind our LLM migration was validated by ~65 tests.", "'our'"),
    ("We validated the LLM migration with ~65 tests.", "'we'"),
    ("I help Anaplan solve forecasting at scale.", "I help Anaplan"),
    ("I can help Anaplan with forecasting.", "I help Anaplan"),
    ("I helped Anaplan Functional Analytics.", "I help Anaplan"),
])
def test_the_guard_rejects_our_we_and_i_help_company(bank, text, why):
    out = L.claim_problems([text], "Anaplan", bank)
    assert any(why in p for p in out), out


def test_the_guard_rejects_a_noun_phrase_that_links_one_bullets_object_to_another_bullets_tool(bank):
    """a3 deployed the agent on Docker and AWS Lambda; a4 wrote the tests. Neither says the tests ran there."""
    for text in ("I ran the ~65 tests on Docker and AWS Lambda.", "Docker and AWS Lambda ran the test framework."):
        out = L.claim_problems([text], "Anaplan", bank)
        assert any("links" in p for p in out), (text, out)


def test_the_guard_passes_a_sentence_whose_object_and_tool_are_one_bullets(bank):
    assert L.claim_problems(["I deployed the agent as a Docker container on AWS Lambda."], "Anaplan", bank) == []
    assert L.claim_problems(["I wrote a three-tier test framework of ~65 tests."], "Anaplan", bank) == []


def test_the_draft_mode_letter_is_under_the_same_guard(settings, bank, spy_llm):
    bad = "At Northwind we built the LLM migration tests, and I help Anaplan solve forecasting."
    llm = spy_llm({"cover_letter": lambda ctx: {"paragraphs": [bad, bad]}})
    evidence = tailor.letter_evidence(bank, [], None, "")
    paras, problems = tailor._letter(JOB, bank, [], "", evidence, llm, settings, [], [], "", {}, False)
    assert paras == [] and any("'we'" in p for p in problems) and any("I help Anaplan" in p for p in problems), problems


# ---------------------------------------------------------------- the whole run
@pytest.mark.slow
def test_a_tailored_job_with_letters_on_gets_the_assembled_letter(settings, bank):
    settings.raw["letter"]["mode"] = "hook"
    llm = MockLLM()
    rep = tailor.tailor_job(JOB, bank, llm, settings, settings.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    a = rep["letters"]["A"]
    assert "cover_letter" not in llm.calls and a["status"] == "ok", (a["reason"], a["problems"], a["evidence"])
    assert a["paragraphs"][0] == hk.letter_sentence
    assert a["words"] <= 250 and rep["letters"]["B"]["status"] == "none"
    # the guard is for the built sentences; the call to action is the user's own template ("Could we take 20 minutes ...")
    assert L.claim_problems(a["paragraphs"][1:-1], JOB["company"], bank, L.exempt_words(hk, JOB, [])) == []
