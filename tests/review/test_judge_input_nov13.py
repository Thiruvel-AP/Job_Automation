"""13 Nov RightShip run: the judge sees the fact bank's skills and labelled claim-guard warnings; a tied shrink keeps a project's first bullet."""
from __future__ import annotations

from careerops.review import decision_queue as dq
from careerops.review import judge as review_judge
from careerops.tailoring import tailor as tailor_module
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import Layout

FB = {"skills": {"Cloud & MLOps": ["AWS Lambda", "SQS", "Kubernetes (AWS EKS)"], "Databases": ["MongoDB", "DynamoDB"]},
      "experience": [], "projects": [], "families": [], "profile_facts": []}
REP = {"context": {"experience": [], "projects": []}}


def test_source_lines_carry_every_skills_row():
    lines = review_judge.source_lines(FB, REP, [])
    assert "[skills Cloud & MLOps] AWS Lambda, SQS, Kubernetes (AWS EKS)" in lines
    assert "[skills Databases] MongoDB, DynamoDB" in lines


def test_claim_guard_warnings_are_labelled_by_kind():
    rep = {"final": {"chosen_attempt": 1}, "attempts": [{"attempt": 1, "warnings": [
        "length: az10 prints 5 lines (limit 3); it is a verbatim bullet, so it is never split or reworded and is left as the fact bank has it",
        "profile: left out a fact that only restates the sentences before it: 'Deployed an LLM'",
        "p_x: no-regression: kept the earlier wording, the rewrite would lose llm"]}, {"attempt": 2, "warnings": ["other attempt"]}]}
    got = dq.chosen_warnings(rep)
    assert [w["kind"].split(" ")[0] for w in got] == ["verbatim-length", "profile", "no-regression"]
    assert got[0]["text"].startswith("length: az10")


def test_shrink_on_a_tie_drops_the_later_bullet_not_the_projects_first():
    ctx = {"experience": [], "projects": [{"id": "iata", "bullet_ids": ["p_ia1", "p_ia2"], "bullets": ["NLP clustering", "Git and Trello"]}]}
    assert tailor_module._shrink(ctx, [Keyword("Python", 2, True)], Layout((1, 3), (1, 3), 0, None))
    assert ctx["projects"][0]["bullet_ids"] == ["p_ia1"]


def test_a_blocked_rewrite_keeps_the_original_bullet(settings):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    bid = fb["experience"][0]["bullets"][0]["id"]
    accepted, viol, _ = tailor_module.guard_rephrases(fb, {bid: "Invented 99% claim with Terraform."}, [Keyword("Python", 2, True)])
    assert bid not in accepted and viol                       # blocked: no accepted text, so the context prints the fact-bank wording
    ctx = tailor_module._context(fb, {"experience": {fb["experience"][0]["id"]: [bid]}, "projects": [], "project_bullets": {}}, accepted,
                                 "p", [Keyword("Python", 2, True)])
    assert ctx["experience"][0]["bullets"][0] == fb["experience"][0]["bullets"][0]["text"]
