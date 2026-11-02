"""RightShip, 31 Oct 2026 (v2.4.1): three drafts of the company letter were rejected, the last one only for "the hook and paragraph 2 use the same
evidence bullet". The check ran on P3 and P4 (the two paragraphs the model writes), and `role_group` had handed both the same bullet: the AstraZeneca
role had one numbered bullet left (az10), P3 already used it, and the fallback took it again instead of one of the role's other bullets.
Both are fixed: the role group prefers a bullet nobody has used, and the check says P3 / P4 and lets through a bullet the plan itself gave to both.
The guards that keep the letter faithful are untouched; the last tests here show they still reject an invented claim. No network, no model."""
import copy
import re

from careerops.tailoring import company_letter as CL
from careerops.tailoring.grounding import repeated_evidence_problems
from test_company_letter import JOB, P3, P4, P5, TEAM, letter_parts, make_world, world  # noqa: F401  (world is a fixture)

SAME = "use the same evidence bullet"


def _numbered(text: str) -> bool:
    return bool(re.search(r"\d", text))


def role_bullets(fb: dict, entry_id: str) -> list[dict]:
    return next(e for e in fb["experience"] if e["id"] == entry_id)["bullets"]


def test_a_role_group_keeps_a_number_in_p4_even_when_p3_already_used_the_roles_numbered_bullets(world):
    """The RightShip shape: every numbered bullet of the first role is already used (by the hook or P3); an unnumbered one is free. P4 gives one number
    of each role's bullet, so the numbered one is taken again (v2.4.2: the plan gave it to both paragraphs, so `shared` lets it through, never a rejection);
    only a role with no numbered bullet gets an unnumbered one."""
    fb = world.fb
    role = world.plan.groups[1][0]
    entry = next(e for e in fb["experience"] if any(b["id"] == role["id"] for b in e["bullets"]))
    numbered = {b["id"] for b in entry["bullets"] if _numbered(b["text"])}
    free = [b["id"] for b in entry["bullets"] if b["id"] not in numbered]
    assert numbered and free, "the fixture role needs numbered and unnumbered bullets"
    groups = CL.role_group(fb, world.kws, world.chosen, JOB["description"], world.hk, set(numbered))
    mine = [g for g in groups if g["id"] in {b["id"] for b in entry["bullets"]}]
    assert mine and mine[0]["id"] in numbered, f"took {mine[0]['id']}, which has no number, with the numbered {sorted(numbered)} available"
    unused_numbered = CL.role_group(fb, world.kws, world.chosen, JOB["description"], world.hk, set())
    assert any(g["id"] in numbered for g in unused_numbered if g["id"] in {b["id"] for b in entry["bullets"]})


def test_with_no_unused_bullet_the_role_group_still_returns_one(world):
    entry = next(e for e in world.fb["experience"] if any(b["id"] == world.plan.groups[1][0]["id"] for b in e["bullets"]))
    every = {b["id"] for b in entry["bullets"]}
    groups = CL.role_group(world.fb, world.kws, world.chosen, JOB["description"], world.hk, every)
    assert [g for g in groups if g["id"] in every]                    # a role is never dropped: the last resort is a used bullet


def test_the_shipped_fixture_plan_gives_p3_and_p4_different_bullets(world):
    ids3, ids4 = {e["id"] for e in world.plan.groups[0]}, {e["id"] for e in world.plan.groups[1]}
    assert not ids3 & ids4


def test_the_same_bullet_in_p3_and_p4_is_still_rejected_and_the_message_names_them(world):
    evidence = [world.plan.groups[0][0], world.plan.groups[1][0]]
    same = [evidence[0]["text"], evidence[0]["text"]]
    found = repeated_evidence_problems(same, evidence, ("P3", "P4"))
    assert found and found[0].startswith("P3 and P4 use the same evidence bullet")
    assert repeated_evidence_problems(same, evidence)[0].startswith("the hook and paragraph 2 use")          # hook mode keeps its wording


def test_a_bullet_the_plan_gave_to_both_paragraphs_is_not_a_reason_to_reject(world):
    evidence = [world.plan.groups[0][0], world.plan.groups[1][0]]
    same = [evidence[0]["text"], evidence[0]["text"]]
    assert repeated_evidence_problems(same, evidence, ("P3", "P4"), {evidence[0]["id"]}) == []


def test_user_problems_judges_each_paragraph_against_its_own_bullets(world):
    """The RightShip shape: P3 shows the AstraZeneca agent as a skill, P4 retells the same project as the role roll-up. The groups the plan gave them are
    different bullets, so the draft is not rejected for "the same bullet"; and a bullet the plan itself gave to both is never a reason either."""
    plan = copy.copy(world.plan)
    g3, g4 = world.plan.groups
    text = g3[0]["text"]
    kw = dict(opening=plan.opening.paragraph if plan.opening else "", fb=world.fb, kws=world.kws, reqs=plan.reqs, company=JOB["company"], title=JOB["title"])
    assert not [p for p in CL.user_problems([text, text], [g3, g4], **kw) if SAME in p]            # P4 resembles P3's bullet, its own is another one
    assert not [p for p in CL.user_problems([text, text], [g3, g3], **kw) if SAME in p]            # the plan offered the same bullet to both


def test_the_whole_letter_comparison_still_exists_for_the_hook_letters_and_for_a_bullet_given_to_both_by_choice():
    ev = [{"id": "a", "text": "Built a tool-calling agent for a cancer research platform with circuit breakers and parquet fallback data"},
          {"id": "b", "text": "Wrote a dissertation chapter about evaluation of agents with gene alias resolution"}]
    same = [ev[0]["text"], ev[0]["text"]]
    assert repeated_evidence_problems(same, ev)                                                    # hook mode: no groups, the old comparison
    assert repeated_evidence_problems(same, ev, ("P3", "P4"), set(), [[ev[0]], [ev[0]]])          # company mode, one bullet given to both and told twice


def test_the_letter_passes_the_guards_when_p3_and_p4_use_different_bullets(world):
    problems, _ = CL.check(letter_parts(world), world.job, world.fb, world.kws, world.plan, TEAM, world.settings, claimable=world.claimable)
    assert problems == []


# ------------------------------------------------------------------ nothing was loosened: invented claims are still rejected
def test_an_invented_user_claim_is_still_rejected(world):
    bad = P3.replace("7,043 customer records", "7,043 customer records and a 92% accuracy at Contoso Labs using Kubeflow")
    problems, _ = CL.check(letter_parts(world, P3=bad), world.job, world.fb, world.kws, world.plan, TEAM, world.settings, claimable=world.claimable)
    assert any("92" in p or "Contoso" in p or "kubeflow" in p.lower() for p in problems), problems


def test_a_company_claim_that_is_not_in_the_ad_is_still_rejected(world):
    bad = "Anaplan's mission is to make every planner at a Fortune 500 company ten times faster with quantum forecasting."
    problems, _ = CL.check(letter_parts(world, P5=bad), world.job, world.fb, world.kws, world.plan, TEAM, world.settings, claimable=world.claimable)
    assert any(p.startswith("P5") for p in problems), problems


def test_the_company_letter_failure_text_names_the_guard_reason(world):
    """Three rejected drafts: the summary line and the letter's reason carry the guards' words, never a bare "no letter"."""
    from careerops.tailoring import tailor

    bad = P3 + " It also cut costs by 92% at Contoso Labs."
    parts = letter_parts(world, P3=bad)
    problems, _ = CL.check(parts, world.job, world.fb, world.kws, world.plan, TEAM, world.settings, claimable=world.claimable)
    assert problems
    letter = {"status": "none", "reason": f"3 draft(s) rejected by the company guards; last: {'; '.join(problems)[:300]}", "problems": problems, "retry": {}}
    text = tailor.letter_summary(letter)
    assert text.startswith("NO LETTER (3 draft(s) rejected by the company guards") and problems[0][:30] in text and text.strip().lower() != "no letter"
