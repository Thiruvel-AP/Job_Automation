"""Tuning round 2: (1) a keyword the CV lacks is offered to a bullet whose own `terms` evidence it (max 2 per bullet, once
overall) and its fate is reported, (2) a rewording is accepted only if its literal keyword hits are a superset of the text
it replaces (the Lendable dc1/az4 case), (3) singular/plural in the literal score. No network, no model: spy MockLLM."""
from careerops.tailoring import tailor
from careerops.tailoring.faithfulness import check
from careerops.tailoring.scorer import Keyword, score

# the two Lendable bullets of calibration runs 20261006-162044 / 190956, as the fact bank words them (text and terms)
DC1 = {"id": "dc1", "text": "Proposed and prototyped an MCP-based agentic tool in Python (FastMCP, Ollama gpt-oss:20b, "
                            "PostgreSQL, GitHub Actions CI) for a Madrid-based product start-up client, presented in a live "
                            "stakeholder demo.",
       "terms": ["mcp", "agentic", "agent", "python", "prototype", "stakeholder", "client", "consulting", "ci", "postgresql",
                 "open-source llm", "open-weight", "communication", "presentation", "stakeholder communication"]}
AZ4 = {"id": "az4", "text": "Wrote a three-tier test framework (tool-level, API/integration, live agent) of ~65 tests across 8 "
                            "files, validated across the LLM provider migration from Groq to AWS Bedrock after a rate-limit "
                            "evaluation.",
       "terms": ["testing", "evaluation", "pytest", "integration testing", "llm evaluation", "quality"]}
LENDABLE = [Keyword("llms", 2, True, ["llm"]), Keyword("communication", 1, False), Keyword("evaluations", 1, False),
            Keyword("stakeholders", 1, False), Keyword("prototyping", 1, False), Keyword("retrieval", 2, True)]


def _fb(*bullets):
    return {"deny_terms": [], "experience": [{"id": "e", "org": "Acme", "bullets": list(bullets)}], "projects": [],
            "skills": {}, "profile_facts": []}


def _sel(*ids):
    return {"experience": {"e": list(ids)}, "projects": []}


def _offers(fb, sel, kws, other_text=""):
    return tailor.placement_offers(fb, sel, kws, other_text)


# ---------------------------------------------------------------- 1. placement from `terms`
def test_the_guard_already_accepts_a_bullets_own_terms_as_evidence_so_faithfulness_is_not_touched():
    ok = check("Presented the MCP tool to stakeholders with clear communication.", DC1["text"], DC1["terms"], [], LENDABLE)
    assert ok.ok                                                    # 'communication' is only in dc1's terms: evidenced
    bad = check("Presented the MCP tool, retrieval included.", DC1["text"], DC1["terms"], [], LENDABLE)
    assert not bad.ok and any("retrieval" in v for v in bad.violations)      # in neither text nor terms: still a violation


def test_a_missed_keyword_is_offered_to_the_bullet_whose_terms_evidence_it():
    offers, unoffered = _offers(_fb(DC1, AZ4), _sel("dc1", "az4"), LENDABLE)
    # communication: dc1's terms; prototyping: dc1's term 'prototype' (stem); evaluations/stakeholders: the CV already says
    # them (singular), llms: AZ4 says LLM; retrieval: no selected bullet evidences it
    assert offers == {"dc1": ["communication", "prototyping"]} and unoffered == ["retrieval"]


def test_a_keyword_the_profile_or_skills_already_have_is_not_offered():
    offers, _ = _offers(_fb(DC1, AZ4), _sel("dc1", "az4"), LENDABLE, other_text="Skills: prompts, communication")
    assert offers == {"dc1": ["prototyping"]}


def test_a_bullet_is_offered_at_most_two_keywords_and_a_keyword_goes_to_one_bullet_only():
    one = {"id": "x1", "text": "Built a thing.", "terms": ["alpha", "beta", "gamma"]}
    two = {"id": "x2", "text": "Built another thing.", "terms": ["alpha", "beta", "gamma"]}
    kws = [Keyword(t, 2, True) for t in ("alpha", "beta", "gamma")]
    assert _offers(_fb(one, two), _sel("x1", "x2"), kws) == ({"x1": ["alpha", "beta"], "x2": ["gamma"]}, [])
    assert _offers(_fb(one), _sel("x1"), kws) == ({"x1": ["alpha", "beta"]}, ["gamma"])   # no room: reported, not forced


def test_the_heavier_keyword_is_offered_first_when_room_is_short():
    one = {"id": "x1", "text": "Built a thing.", "terms": ["alpha", "beta", "gamma"]}
    kws = [Keyword("alpha", 1, False), Keyword("beta", 1, False), Keyword("gamma", 2, True)]
    assert _offers(_fb(one), _sel("x1"), kws) == ({"x1": ["gamma", "alpha"]}, ["beta"])


def test_the_rephrase_prompt_lists_only_the_offered_keywords_and_a_bullet_with_none_is_not_sent(spy_llm):
    llm = spy_llm()
    _, stats = tailor.propose_rephrases({"title": "AI Engineer"}, _fb(DC1, AZ4), _sel("dc1", "az4"), LENDABLE, llm, {}, [])
    sent = {i["id"]: i for s in llm.sent for i in s["context"]["bullets"]}
    assert list(sent) == ["dc1"] and sent["dc1"]["introduce"] == ["communication", "prototyping"]
    assert "MAY INTRODUCE: communication, prototyping" in llm.sent[0]["user"]
    assert "include each one only if it reads naturally" in llm.sent[0]["system"]
    assert stats["offered"] == {"dc1": ["communication", "prototyping"]} and stats["unoffered"] == ["retrieval"]


def test_placement_report_says_placed_not_placed_or_blocked():
    ctx = {"experience": [{"bullet_ids": ["a", "b", "c"],
                           "bullets": ["Built it with communication skills.", "Built it.", "Built it too."]}], "projects": []}
    offered = {"a": ["communication"], "b": ["evaluations"], "c": ["stakeholders"]}
    proposed = {"a": "Built it with communication skills.", "c": "Built it for stakeholders and a lie."}
    accepted = {"a": "Built it with communication skills."}
    assert tailor.placement_report(offered, ["retrieval"], proposed, accepted, ctx) == {
        "placed": ["communication"], "not_placed": ["evaluations"], "blocked": ["stakeholders"], "unoffered": ["retrieval"]}


# ---------------------------------------------------------------- 2. no regression (Lendable dc1 / az4)
DC1_162044 = ("Proposed and prototyped an MCP-based agentic tool in Python using Ollama gpt-oss:20b and PostgreSQL, "
              "presenting the llm solution to stakeholders during a live communication demo.")
DC1_190956 = ("Prototyping an MCP-based agentic tool in Python for a Madrid-based product start-up client, presenting to "
              "stakeholders.")
AZ4_162044 = ("Wrote a three-tier test framework of ~65 tests across 8 files, conducting evaluations during LLM provider "
              "migration from Groq to AWS Bedrock after rate-limit assessment.")
AZ4_190956 = ("Wrote a three-tier test framework of ~65 tests across 8 files, validating LLM provider migration from Groq to "
              "AWS Bedrock after a rate-limit evaluation.")
COMM_EVAL = [Keyword("communication", 1, False), Keyword("evaluations", 1, False)]


def test_lendable_dc1_lost_communication_and_az4_only_the_plural_between_runs_162044_and_190956():
    assert score(DC1_162044, COMM_EVAL).hits == ["communication"] and score(DC1_190956, COMM_EVAL).hits == []
    assert score(AZ4_162044, COMM_EVAL).hits == ["evaluations"]
    assert score(AZ4_190956, COMM_EVAL).hits == ["evaluations"]        # singular/plural rule: 'evaluation' is the same term
    assert score(AZ4["text"], COMM_EVAL).hits == ["evaluations"]


def test_a_rewording_that_loses_a_keyword_the_replaced_text_had_is_refused_and_logged():
    fb = _fb(DC1, AZ4)
    # the wording pass replaces what is on the CV (the 162044 wording), not the canonical bullet
    acc, viol, warn = tailor.guard_rephrases(fb, {"dc1": DC1_190956}, COMM_EVAL, base={"dc1": DC1_162044})
    assert acc == {} and viol == []
    assert any("dc1" in w and tailor.NO_REGRESSION in w and "communication" in w for w in warn)
    # the same text over the canonical bullet loses nothing: the canonical dc1 never said 'communication'
    acc, _, warn = tailor.guard_rephrases(fb, {"dc1": DC1_190956}, COMM_EVAL)
    assert acc == {"dc1": DC1_190956} and not [w for w in warn if tailor.NO_REGRESSION in w]


def test_a_rewording_that_keeps_every_hit_and_adds_one_is_accepted():
    new = AZ4["text"].replace("rate-limit evaluation", "rate-limit evaluations")
    acc, _, warn = tailor.guard_rephrases(_fb(DC1, AZ4), {"az4": new}, COMM_EVAL)
    assert acc == {"az4": new} and not [w for w in warn if tailor.NO_REGRESSION in w]


def test_a_rewording_that_drops_a_hit_the_canonical_bullet_had_is_refused():
    b = {"id": "k1", "text": "Built an LLM pipeline in Python with PostgreSQL.", "terms": ["llm", "python", "postgresql"]}
    kws = [Keyword("llm", 2, True), Keyword("python", 2, True), Keyword("postgresql", 1, False)]
    acc, viol, warn = tailor.guard_rephrases(_fb(b), {"k1": "Built a language-model pipeline in Python with PostgreSQL."}, kws)
    assert acc == {} and viol == [] and any(tailor.NO_REGRESSION in w and "llm" in w for w in warn)


def test_the_wording_pass_keeps_the_current_text_and_logs_when_the_rewrite_would_lose_a_hit(spy_llm):
    kws = [Keyword("communication", 1, False), Keyword("prototyping", 1, False)]
    current = DC1_162044.replace("Proposed and prototyped", "Prototyped")
    ctx = {"experience": [{"bullet_ids": ["dc1"], "bullets": [current]}], "projects": []}
    llm = spy_llm({"rephrase_bullets": lambda c: {"dc1": DC1_190956}})
    notes: list[str] = []
    stats = tailor.wording_pass({"title": "AI Engineer"}, _fb(DC1), ctx, kws, ["prototyping"], llm, {}, notes)
    assert stats["accepted"] == [] and stats["blocked"] == ["dc1"]
    assert ctx["experience"][0]["bullets"][0] == current                           # the CV keeps the earlier wording
    assert any(tailor.NO_REGRESSION in n and "communication" in n for n in notes)
