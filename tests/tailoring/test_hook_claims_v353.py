"""v3.5.3 (10 Nov 2026, fix 1): a hook_library entry's `proof:` may be a LIST of bullet ids. A claim passes when ANY listed proof bullet (its text,
`result:` or `limits:`) supports it; a list is exactly those bullets (a single id keeps the whole-role scope). The CV uses the first listed proof
that is in the bank; `careerops hook-check` shows the supporting quote of every claim of a hook that passes. A fictional bank built here."""
import copy

from careerops import hook as hook_mod
from careerops.tailoring import hook_claims as H
from careerops.tailoring.scorer import Keyword

BANK = {
    "experience": [
        {"id": "az", "org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project",
         "bullets": [{"id": "az3", "text": "Deployed the agent as a Docker container on AWS Lambda.", "terms": ["docker", "aws lambda"]},
                     {"id": "az4", "text": "Wrote a three-tier test framework of ~65 tests across 8 files.", "result": "~65 tests across 8 files",
                      "terms": ["testing", "pytest"]},
                     {"id": "az10", "text": "Built a tool-calling agent with deterministic hallucination guards and circuit breakers.",
                      "result": "19,213-gene parquet fallback", "terms": ["guardrails", "hallucination"]}]}],
    "projects": [],
    "hook_library": [
        {"id": "h_guard", "proof": ["az10", "az4"], "terms": ["hallucination", "guardrails", "testing"],
         "headline": "LLM agent with hallucination guards, ~65 tests",
         "profile_line": "For my AstraZeneca-affiliated MSc dissertation I built an agent with deterministic hallucination guards and a 19,213-gene "
                         "fallback, checked by ~65 tests.",
         "letter_line": "For my AstraZeneca-affiliated MSc dissertation I built an agent with hallucination guards, checked by ~65 tests."}]}


def entry(bank):
    return bank["hook_library"][0]


def test_a_claim_passes_when_any_listed_proof_bullet_says_it():
    """~65 is only in az4 (the second listed proof), 19,213 only in az10's result: both pass."""
    assert H.check_all(BANK) == {"h_guard": []}


def test_a_list_is_exactly_the_listed_bullets_not_the_whole_role():
    bank = copy.deepcopy(BANK)
    entry(bank)["proof"] = ["az10"]                                     # az4 is in the same role but no longer listed
    found = H.conflicts(bank, entry(bank))
    assert {c.kind for c in found} == {"number"} and all("65" in c.what for c in found)
    assert {c.bullet_id for c in found} == {"az10"}


def test_a_single_id_keeps_the_whole_role_scope():
    bank = copy.deepcopy(BANK)
    entry(bank)["proof"] = "az10"                                       # the older form: the role of az10 is the proof
    assert H.conflicts(bank, entry(bank)) == []


def test_a_compound_with_the_employers_name_is_the_employers_and_another_name_is_still_blocked():
    """"AstraZeneca-affiliated" is the employer's own name (it blocked three real hooks on 10 Nov); "Rust-driven" names Rust, which no proof says."""
    assert not [c for c in H.conflicts(BANK, entry(BANK)) if c.kind == "name"]
    bank = copy.deepcopy(BANK)
    entry(bank)["letter_line"] = "For my AstraZeneca-affiliated MSc dissertation I built a Rust-driven agent, checked by ~65 tests."
    assert [(c.field, c.kind) for c in H.conflicts(bank, entry(bank))] == [("letter_line", "name")]


def test_proof_ids_and_the_first_listed_proof_in_the_bank():
    assert hook_mod.proof_ids({"proof": "az4"}) == ["az4"]
    assert hook_mod.proof_ids({"proof": ["az4", " ", "az3", "az4", None]}) == ["az4", "az3"]
    assert hook_mod.proof_ids({}) == []
    kind, owner, bullet = hook_mod.find_proof(BANK, {"proof": ["gone", "az4", "az3"]})
    assert (kind, owner["id"], bullet["id"]) == ("experience", "az", "az4")
    assert hook_mod.find_proof(BANK, {"proof": ["gone"]}) is None


def test_the_cv_uses_the_first_listed_proof_and_a_missing_listed_id_is_noted_not_fatal():
    bank = copy.deepcopy(BANK)
    entry(bank)["proof"] = ["gone", "az10", "az4"]
    kws = [Keyword("hallucination", 3.0, True), Keyword("guardrails", 3.0, True), Keyword("testing", 2.0, True)]
    notes: list[str] = []
    hk = hook_mod.build_hook({"description": "LLM guardrails, hallucination, testing"}, kws, bank, notes=notes)
    assert hk is not None and hk.hook_id == "h_guard" and hk.proof_bullet_id == "az10"
    assert any("gone" in n and "not in the fact bank" in n for n in notes)


def test_the_report_quotes_the_bullet_behind_each_claim_of_a_passing_hook():
    report = H.report(BANK)
    assert "h_guard: ok (proof az10, az4)" in report
    lines = report.splitlines()
    sixty_five = next(l for l in lines if l.strip().startswith("65"))
    assert "az4" in sixty_five and "~65 tests" in sixty_five
    assert any(l.strip().startswith("19,213") and "az10" in l for l in lines)
    assert "1 ok, 0 blocked of 1 hooks" in report


def test_a_blocked_hook_names_its_listed_proofs():
    bank = copy.deepcopy(BANK)
    entry(bank)["headline"] = "LLM agent with hallucination guards, ~90 tests"
    assert "h_guard: BLOCKED (proof az10, az4; 1 claim" in H.report(bank)
