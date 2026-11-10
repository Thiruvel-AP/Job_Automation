"""v3.5.2 (10 Nov 2026): a hook may claim nothing its proof bullets do not (tailoring/hook_claims.py). A hook that does is blocked from every CV and
letter, with the two quotes shown; `careerops hook-check` prints the report and doctor warns. A fictional bank built here."""
import copy

import pytest
import yaml

from careerops import hook as hook_mod
from careerops.setup import hook_claim_checks
from careerops.tailoring import hook_claims as H
from careerops.tailoring.scorer import Keyword

BANK = {
    "experience": [
        {"id": "dc180", "org": "180 Degrees Consulting Bristol", "display_org": "180 Degrees Consulting", "title": "Data Consultant",
         "bullets": [{"id": "dc2", "text": "Contributed the MCP implementation to the client's production repository, now in live day-to-day use; "
                                           "engagement received a 5-star commendation from senior leadership."},
                     {"id": "dc8", "replaces": ["dc2"], "text": "Carried it into production with the client's engineering team: contributed to "
                                                                 "their production TypeScript/Node.js MCP server, now in live use."}]},
        {"id": "az", "org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project",
         "bullets": [{"id": "az3", "text": "Deployed the agent as a Docker container on AWS Lambda."},
                     {"id": "az4", "text": "Wrote a three-tier test framework of ~65 tests across 8 files."}]}],
    "projects": [],
    "hook_library": [
        {"id": "h_client", "proof": "dc2", "terms": ["client", "stakeholder", "consulting"],
         "headline": "Client MCP server in live use; engagement rated five stars",
         "profile_line": "At 180 Degrees Consulting I contributed to the client's production MCP server, now in live use; senior leadership rated "
                         "the engagement five stars.",
         "letter_line": "At 180 Degrees Consulting I contributed to the client's production MCP server, now in live use."},
        {"id": "h_tests", "proof": "az4", "terms": ["testing", "tests", "aws", "lambda"],
         "headline": "LLM agent on AWS Lambda behind ~65 automated tests",
         "profile_line": "At AstraZeneca I deployed an LLM agent to AWS Lambda, validated by ~65 tests.",
         "letter_line": "At AstraZeneca I shipped an LLM agent to AWS Lambda, validated by ~65 tests."}]}


def hook(bank, hid):
    return next(h for h in bank["hook_library"] if h["id"] == hid)


def test_consistent_hooks_pass_and_the_org_names_number_is_not_a_claim():
    assert H.check_all(BANK) == {"h_client": [], "h_tests": []}               # "180" is the employer's name; "shipped" is "deployed"


def test_the_users_example_is_blocked_with_both_quotes():
    """"my prototype went into production" vs dc8 "contributed to their production server"."""
    bank = copy.deepcopy(BANK)
    hook(bank, "h_client")["letter_line"] = "At 180 Degrees Consulting my prototype went into production with the client's team."
    found = H.conflicts(bank, hook(bank, "h_client"))
    assert len(found) == 1 and found[0].kind == "ownership" and "drops 'contributed'" in found[0].what
    lines = found[0].lines()
    assert lines[1].strip() == 'hook   : "At 180 Degrees Consulting my prototype went into production with the client\'s team."'
    assert lines[2].strip().startswith("dc2") and "Contributed the MCP implementation" in lines[2]


def test_a_claim_of_production_no_proof_bullet_makes_is_blocked():
    bank = copy.deepcopy(BANK)
    hook(bank, "h_tests")["letter_line"] = "At AstraZeneca I took an LLM agent from prototype to production on AWS Lambda, validated by ~65 tests."
    found = H.conflicts(bank, hook(bank, "h_tests"))
    assert [(c.field, c.kind) for c in found] == [("letter_line", "outcome")] and "'production'" in found[0].what
    assert found[0].bullet_id == "az4"


@pytest.mark.parametrize("field, text, kind", [
    ("headline", "LLM agent on AWS Lambda behind ~90 automated tests", "number"),
    ("headline", "LLM agent on Azure Functions behind ~65 automated tests", "name"),
    ("profile_line", "At 180 Degrees Consulting I built the client's production MCP server, now in live use.", "ownership"),
    ("profile_line", "At 180 Degrees Consulting I contributed to the client's MCP server; my demo was rated five stars by senior leadership.",
     "credit"),
])
def test_each_kind_of_claim_is_caught(field, text, kind):
    bank = copy.deepcopy(BANK)
    hid = "h_client" if "180" in text else "h_tests"
    hook(bank, hid)[field] = text
    assert kind in [c.kind for c in H.conflicts(bank, hook(bank, hid))]


def test_a_blocked_hook_is_never_picked_and_the_note_says_why():
    bank = copy.deepcopy(BANK)
    hook(bank, "h_tests")["letter_line"] = "At AstraZeneca I took an LLM agent from prototype to production, validated by ~65 tests."
    kws = [Keyword("testing", 3.0, True), Keyword("aws", 2.0, True), Keyword("lambda", 2.0, True)]
    notes: list[str] = []
    picked = [p["entry"]["id"] for p in hook_mod.iter_eligible(bank, kws, notes)]
    assert "h_tests" not in picked and any("'h_tests': BLOCKED" in n and "production" in n for n in notes)
    assert hook_mod.build_hook({"description": ""}, kws, bank, notes=[]) is None or \
        hook_mod.build_hook({"description": ""}, kws, bank, notes=[]).hook_id != "h_tests"


def test_doctor_warns_per_blocked_hook_and_the_report_counts(tmp_path):
    bank = copy.deepcopy(BANK)
    hook(bank, "h_tests")["profile_line"] = "At AstraZeneca I shipped an LLM agent to production, validated by ~65 tests."
    p = tmp_path / "fact_bank.yaml"
    p.write_text(yaml.safe_dump(bank, allow_unicode=True), encoding="utf-8")
    rows = hook_claim_checks(p)
    assert len(rows) == 1 and rows[0][1] == "warn" and rows[0][0].startswith("hook 'h_tests' BLOCKED")
    report = H.report(bank)
    assert "h_client: ok (proof dc2)" in report and "h_tests: BLOCKED (proof az4; 1 claim" in report and "1 ok, 1 blocked of 2 hooks" in report
    p.write_text(yaml.safe_dump(BANK, allow_unicode=True), encoding="utf-8")
    assert hook_claim_checks(p)[0][1] is True


def test_the_cli_prints_the_report_and_writes_nothing(tmp_path, settings, capsys):
    from careerops import cli
    bank = tmp_path / "fact_bank.yaml"
    bank.write_text(yaml.safe_dump(BANK, allow_unicode=True), encoding="utf-8")
    raw = copy.deepcopy(settings.raw)
    raw["paths"]["fact_bank"] = str(bank)
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = bank.read_bytes()
    try:
        cli.main(["--config", str(cfg), "hook-check"])
    except SystemExit as e:
        assert not e.code
    out = capsys.readouterr().out
    assert "h_client: ok" in out and "hook-check: 2 ok, 0 blocked of 2 hooks" in out and bank.read_bytes() == before
