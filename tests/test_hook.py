"""The hook (careerops/hook.py): problem + one proof bullet + its number, in the headline, profile line 1, the first bullet of its
role and sentence 1 of the letter.

This file covers the CODE side: the number and the proof bullet; tests/test_hook_library.py covers the library and
tests/test_hook_selection.py the eligibility rule, the lint and the family-headline fallback. No model and no code template writes a
hook any more: a bank without a `hook_library` (or without an eligible entry) gets no hook.

Pure-logic tests use the fictional fact bank (tests/fixtures/tailoring/fact_bank_fixture.yaml) and a hand-made keyword list; the
tailoring tests at the bottom render with xelatex (marked slow). No network, no model.
"""
from pathlib import Path

import pytest

from careerops import hook as H
from careerops.llm import MockLLM
from careerops.tailoring import tailor
from careerops.tailoring.render import page_count, pdf_text
from careerops.tailoring.scorer import Keyword

JD = ("We are hiring an AI Engineer to build and evaluate LLM agents on our research data platform, from prototyping to production "
      "deployment on AWS. You will write evaluation suites with pytest and keep quality high in production.\n"
      "Requirements\n- Python\n- AWS (Lambda, IAM)\n- Docker\n- pytest\n")
JOB = {"id": "manual:hook1", "company": "Northwind Health AI", "title": "AI Engineer", "location": "Bristol, UK", "description": JD}
KWS = [Keyword("Python", 2, True), Keyword("AWS", 2, True), Keyword("Docker", 2, True), Keyword("pytest", 2, True),
       Keyword("LLM agents", 1, False)]


def bank(*entries, deny=()):
    return {"identity": {"name": "Ada Lovelace", "headline": "AI Engineer"}, "deny_terms": list(deny), "experience": list(entries),
            "projects": [], "profile_facts": ["a"], "skills": {"core": ["Python"]}, "education": [], "certifications": []}


def entry(eid, org, *bullets):
    return {"id": eid, "title": "Engineer", "org": org, "dates": "2025", "bullets": list(bullets)}


def b(bid, text, terms, **kw):
    return {"id": bid, "text": text, "terms": terms, **kw}


# ---------------------------------------------------------------- the number
@pytest.mark.parametrize("bullet, want", [
    (b("x", "Wrote a three-tier test framework of ~65 tests across 8 files.", []), "~65"),
    (b("x", "Built an agent on Claude Haiku 4.5 and 12 tools.", []), "12"),               # a model version is not a result
    (b("x", "Built a service.", [], result="cut latency from 8 s to 2 s"), "8"),             # the user's own result wins
    (b("x", "Loaded 19,213-gene tables and 14 views.", []), "19,213"),
    (b("x", "Cut costs 40% with a cache.", []), "40%"),
    (b("x", "Built a service on Python 3.12.", []), ""),                                      # only a version: no number
    (b("x", "Built a service.", []), ""),
])
def test_find_number_is_verbatim_and_skips_versions(bullet, want):
    n = H.find_number(bullet)
    assert n == want
    assert not n or n in bullet["text"] or n in str(bullet.get("result"))


# ---------------------------------------------------------------- the proof bullet
def test_proof_is_the_highest_score_among_bullets_with_a_number():
    fb = bank(entry("e1", "Acme",
                    b("a1", "Built Python services for 40 clients.", ["python"]),
                    b("a2", "Deployed on AWS with Docker and pytest, 65 tests.", ["aws", "docker", "pytest"]),
                    b("a3", "Wrote Python, AWS, Docker and pytest tools.", ["python", "aws", "docker", "pytest"])))   # best score, no number
    proof = H.choose_proof(fb, KWS)
    assert proof["bullet"]["id"] == "a2" and proof["number"] == "65" and proof["score"] == 6.0


def test_a_tie_goes_to_the_more_recent_bullet():
    fb = bank(entry("new", "Newer", b("n1", "Ran 10 jobs on AWS.", ["aws"])), entry("old", "Older", b("o1", "Ran 20 jobs on AWS.", ["aws"])))
    assert H.choose_proof(fb, KWS)["bullet"]["id"] == "n1"


def test_a_result_counts_as_a_number():
    fb = bank(entry("e1", "Acme", b("a1", "Deployed on AWS.", ["aws"], result="five star commendation"), b("a2", "Used Docker.", ["docker"])))
    assert H.choose_proof(fb, KWS)["bullet"]["id"] == "a1"


def test_with_no_number_the_best_bullet_is_used_and_flagged():
    fb = bank(entry("e1", "Acme", b("a1", "Deployed on AWS with Docker.", ["aws", "docker"]), b("a2", "Used Python.", ["python"])))
    proof = H.choose_proof(fb, KWS)
    assert proof["bullet"]["id"] == "a1" and proof["number"] == ""
    fb["hook_library"] = [{"id": "x", "proof": "a1", "terms": ["aws", "docker"], "headline": "h", "profile_line": "p", "letter_line": "l"}]
    hk = H.build_hook(JOB, KWS, fb)
    assert hk is not None and H.NO_NUMBER_FLAG in hk.flags and hk.number == ""


def test_a_bullet_with_a_deny_listed_term_is_never_the_proof():
    fb = bank(entry("e1", "Acme", b("a1", "Ran 90 dbt models on AWS.", ["aws", "python", "docker"]), b("a2", "Ran 12 jobs on AWS.", ["aws"])),
              deny=["dbt"])
    assert H.choose_proof(fb, KWS)["bullet"]["id"] == "a2"


def test_the_most_relevant_role_is_the_one_with_the_highest_summed_score():
    fb = bank(entry("e1", "Acme", b("a1", "Ran 5 jobs.", ["aws"])),
              entry("e2", "Beta", b("b1", "Ran 6 jobs.", ["python"]), b("b2", "Ran 7 jobs.", ["docker", "pytest"])))
    assert H.choose_proof(fb, KWS)["relevant_role"] == "e2"


def test_a_project_bullet_can_be_the_proof():
    fb = bank(entry("e1", "Acme", b("a1", "Used Python.", ["python"])))
    fb["projects"] = [{"id": "p1", "name": "Forecaster", "bullets": [b("p1a", "Served a model on AWS for 30 users.", ["aws", "docker"])]}]
    proof = H.choose_proof(fb, KWS)
    assert proof["bullet"]["id"] == "p1a" and proof["kind"] == "project"


def hook_bank():
    return bank(entry("e1", "Acme", b("a1", "Wrote a test framework of ~65 tests across 8 files for an LLM agent deployed on AWS.",
                                      ["llm agents", "aws", "pytest", "testing"], result="~65 tests across 8 files")))


def test_no_hook_rather_than_a_fake_one_when_there_is_no_bullet():
    fb = bank(entry("e1", "Acme", b("a1", "Ran 12 jobs.", ["aws"])))
    assert H.build_hook(JOB, KWS, {**fb, "experience": [], "projects": []}) is None


@pytest.mark.parametrize("word", ["passionate", "proven", "dynamic", "results-driven", "extensive", "strong"])
def test_every_vague_adjective_is_listed(word):
    assert H.vague_words(f"A {word} engineer") == [word]


# ---------------------------------------------------------------- putting it on the CV
def test_ensure_selected_adds_the_proof_bullet_to_its_role():
    fb = bank(entry("e1", "Acme", b("a1", "x", []), b("a2", "y", [])))
    hk = H.Hook("p", "a2", "1", "h", "pl", "ls", entry_id="e1")
    sel = {"experience": {"e1": ["a1"]}, "projects": [], "project_bullets": {}}
    assert H.ensure_selected(sel, fb, hk)["experience"]["e1"] == ["a1", "a2"] and sel["experience"]["e1"] == ["a1"]


def test_ensure_selected_puts_a_project_first():
    fb = bank(entry("e1", "Acme", b("a1", "x", [])))
    hk = H.Hook("p", "p2", "1", "h", "pl", "ls", entry_id="pj", entry_kind="project")
    sel = {"experience": {"e1": ["a1"]}, "projects": ["other", "pj"], "project_bullets": {"pj": ["p1"]}}
    out = H.ensure_selected(sel, fb, hk)
    assert out["projects"] == ["pj", "other"] and out["project_bullets"]["pj"] == ["p1", "p2"]


def test_proof_first_moves_the_bullet_to_the_top_of_its_role_and_keeps_its_text():
    ctx = {"experience": [{"id": "e1", "bullet_ids": ["a1", "a2", "a3"], "bullets": ["one", "two", "three"]}], "projects": []}
    hk = H.Hook("p", "a3", "1", "h", "pl", "ls", entry_id="e1")
    assert H.proof_first(ctx, hk) and ctx["experience"][0]["bullet_ids"] == ["a3", "a1", "a2"]
    assert ctx["experience"][0]["bullets"] == ["three", "one", "two"]


def test_keeps_number():
    hk = H.Hook("p", "a1", "~65", "h", "pl", "ls")
    assert H.keeps_number("wrote ~65 tests", hk) and not H.keeps_number("wrote 65 tests", hk)
    assert H.keeps_number("anything", H.Hook("p", "a1", "", "h", "pl", "ls"))


# ---------------------------------------------------------------- the whole tailoring run
@pytest.mark.slow
def test_a_tailored_cv_carries_the_hook_in_all_three_places(settings, fb_fixture, job, tmp_path):
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    assert hk.source == "library" and rep["hook_placed"] == {"headline": True, "profile": True, "bullet": True, "number_in_pdf": True}
    flat = " ".join(pdf_text(Path(rep["cv_pdf"])).split())
    assert " ".join(hk.headline.split()) in flat and " ".join(hk.profile_line.split()) in flat
    assert rep["context"]["profile"].startswith(hk.profile_line) and rep["context"]["hook_headline"] == hk.headline
    owner = next(e for sec in ("experience", "projects") for e in rep["context"][sec] if e.get("id") == hk.entry_id)
    assert owner["bullet_ids"][0] == hk.proof_bullet_id
    assert page_count(Path(rep["cv_pdf"])) == 2
    assert len(hk.headline) <= H.HEADLINE_MAX


@pytest.mark.slow
def test_the_headline_is_one_line_under_the_name(settings, fb_fixture, job):
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    flat = [" ".join(ln.split()) for ln in pdf_text(Path(rep["cv_pdf"])).splitlines() if ln.strip()]
    assert any(ln == " ".join(hk.headline.split()) for ln in flat[:4]), flat[:4]            # the whole headline sits on one line


@pytest.mark.slow
def test_a_rewording_that_loses_the_number_is_not_used(settings, fb_fixture, job):
    """The model rewords the proof bullet without its number: the fact-bank wording (with the number) stays on the CV."""
    def rephrase(ctx):
        return {x["id"]: "Built something that sounds nice and has no figure in it" for x in ctx["bullets"]}

    llm = MockLLM({"rephrase_bullets": rephrase})
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    hk = H.Hook.from_dict(rep["hook"])
    owner = next(e for sec in ("experience", "projects") for e in rep["context"][sec] if e.get("id") == hk.entry_id)
    first = owner["bullets"][0]
    assert owner["bullet_ids"][0] == hk.proof_bullet_id and (not hk.number or hk.number in first), (hk, first)


@pytest.mark.slow
def test_hook_disabled_leaves_the_old_cv(settings, fb_fixture, job):
    settings.raw["hook"] = {"enabled": False}
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
    assert rep["hook"] is None and rep["hook_placed"] == {} and "hook_headline" not in rep["context"]


# ---------------------------------------------------------------- fix round 2: the hook was silently lost on the real bank
# Root cause (found by running build_hook on the real fact bank, read-only): choose_proof took "10" from "new 10-class head" (the
# plural-s test read "class" as a plural) and "3" from "3rd place"; the template then named the project with its em dash, the
# problem clause "deep learning" tripped the qualifier check, and build_hook returned None without a word. With no hook the CV had no
# headline, the profile had no hook line, and letter.mode hook quietly drafted the old single letter.
@pytest.mark.parametrize("bullet, want", [
    (b("x", "Compared it with ResNet50 transfer learning (frozen backbone, new 10-class head).", []), ""),   # a modifier, not a count
    (b("x", "Placed well.", [], result="3rd place; 200:1 class imbalance"), "200:1"),                         # an ordinal is not a result
    (b("x", "Spotted outliers.", [], result="3rd place"), ""),
    (b("x", "Wrote a 10-class head and 3 loss functions.", []), "3"),                                        # "class" is not a plural; "10-class" is a modifier
    (b("x", "Wrote a three-tier test framework of ~65 tests across 8 files.", []), "~65"),
    (b("x", "Ran 20 jobs on AWS.", []), "20"),                                                               # a first-word verb is not a product name
])
def test_find_number_skips_ordinals_and_hyphenated_modifiers(bullet, want):
    assert H.find_number(bullet) == want


def test_skilled_is_a_vague_adjective():
    assert H.vague_words("Data Scientist skilled in building deployable ML models") == ["skilled"]
    assert "skilled" in H.VAGUE_ADJECTIVES


def test_a_hook_that_cannot_be_built_says_why():
    fb = bank(entry("e1", "Acme", b("a1", "Ran 90 dbt models on AWS.", ["aws"])), deny=["dbt"])
    notes: list[str] = []
    assert H.build_hook(JOB, KWS, fb, None, "AI Engineer", notes=notes) is None
    assert notes == ["the fact bank has no hook_library"], notes


# ---------------------------------------------------------------- the proof comes from the most relevant role
def test_the_proof_is_a_bullet_of_the_most_relevant_role_when_that_role_has_a_number():
    """e1 has the highest summed score (8) but e2 has the single best numbered bullet (6): the proof is still in e1, so the first bullet
    of the most relevant role is the proof bullet."""
    fb = bank(entry("e1", "Acme", b("a1", "Ran 20 jobs on AWS.", ["aws"]), b("a2", "Wrote Docker, Python and pytest tools.", ["docker", "python"]),
                    b("a3", "Wrote Python and pytest tools.", ["python", "pytest"])),
              entry("e2", "Beta", b("b1", "Ran 6 jobs on AWS with Docker, Python and pytest.", ["aws", "docker", "python"])))
    proof = H.choose_proof(fb, KWS)
    assert proof["relevant_role"] == "e1" and proof["entry"]["id"] == "e1" and proof["bullet"]["id"] == "a1"


def test_without_a_number_in_the_most_relevant_role_the_best_bullet_anywhere_is_used():
    fb = bank(entry("e1", "Acme", b("a1", "Wrote Docker, Python and pytest tools.", ["docker", "python", "pytest"])),
              entry("e2", "Beta", b("b1", "Ran 6 jobs on AWS.", ["aws"])))
    proof = H.choose_proof(fb, KWS)
    assert proof["relevant_role"] == "e1" and proof["bullet"]["id"] == "b1" and proof["number"] == "6"


# ---------------------------------------------------------------- found by running the real bank offline (read-only, MockLLM)
def test_work_for_an_employer_outranks_a_project_when_the_best_role_has_no_number():
    """The real bank's best role by summed score has no numbered bullet; a project scored higher than the ~65-tests bullet of the
    AstraZeneca-like role and became the proof. A job's numbered bullet now comes before any project's."""
    fb = bank(entry("e1", "Acme", b("a1", "Wrote Docker, Python and pytest tools.", ["docker", "python", "pytest"])),
              entry("e2", "Beta", b("b1", "Wrote ~65 tests for an AWS service.", ["aws"], result="~65 tests")))
    fb["projects"] = [{"id": "p1", "name": "Forecaster", "bullets": [b("p1a", "Served a model on AWS for 30 users with Docker and Python.",
                                                                       ["aws", "docker", "python"])]}]
    proof = H.choose_proof(fb, KWS)
    assert proof["relevant_role"] == "e1" and proof["bullet"]["id"] == "b1" and proof["kind"] == "experience"
    assert [c["bullet"]["id"] for c in H.proof_candidates(fb, KWS)] == ["b1", "p1a", "a1"]


def test_a_number_that_describes_the_next_word_is_not_a_result_even_without_a_hyphen():
    """The real bullet says "new 10 class head" (no hyphen): 10 is a size of the head, not a figure the CV can lead with."""
    assert H.find_number(b("x", "Compared it with ResNet50 transfer learning (frozen backbone, new 10 class head), judged by loss.", [])) == ""
    assert H.find_number(b("x", "Wrote 12 unit tests for the service.", [])) == "12"
