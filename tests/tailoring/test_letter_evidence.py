"""The company letter's P3 (the ad's skills through a second bullet or project) and P4 (the roll-up, one bullet per role) are written from bullets the
code chose; the length is a range of BODY words (letter.company_words: [200, 320]); and the user guard lets a capability or skill word through when
the fact bank says it anywhere. MockLLM only; the guards are called directly."""
import copy

import pytest

from careerops.tailoring import company_letter as CL
from careerops.tailoring import grounding
from careerops.tailoring.scorer import keyword_hits
from test_company_letter import JD, JOB, P3, TEAM, body, check, letter_parts, pad, world  # noqa: F401  (world is a fixture)


def hits(world, bullet: dict) -> set[str]:
    return {world.kws[i].term for i in keyword_hits(" ".join([bullet["text"], *bullet.get("terms", [])]), world.kws)}


# ------------------------------------------------------------------ what P3 and P4 are written from
def test_p3_is_written_from_one_or_two_bullets_that_carry_the_skills_it_must_name(world):
    plan = world.plan
    skills, group = plan.reqs[0], plan.groups[0]
    assert 1 <= len(group) <= 2 and len(skills) >= 2
    carried = set().union(*(hits(world, e) for e in group))
    assert set(skills) <= carried                                                  # every skill P3 must name is evidenced by its bullets
    assert world.hk.proof_bullet_id not in {e["id"] for e in group}               # never the hook's own bullet (P1 already tells that story)
    assert [e for g in plan.groups for e in g] == plan.evidence


def test_the_skills_are_the_heaviest_the_group_evidences_required_first(world):
    skills = world.plan.reqs[0]
    weight = {k.term: k.weight for k in world.kws}
    assert [weight[s] for s in skills] == sorted((weight[s] for s in skills), reverse=True)


def test_p4_has_one_bullet_per_role_and_no_bullet_p3_uses(world):
    p3_ids = {e["id"] for e in world.plan.groups[0]}
    assert len(world.plan.groups[1]) == 2 and not p3_ids & {e["id"] for e in world.plan.groups[1]}
    owners = {b["id"]: e["id"] for e in world.fb["experience"] for b in e["bullets"]}
    assert len({owners[e["id"]] for e in world.plan.groups[1]}) == 2


def test_the_prompt_shows_the_evidence_blocks_grouped_by_paragraph_and_the_context_carries_them(world):
    from careerops.llm import MockLLM
    from test_company_letter import run, writer

    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append((system, user, context))
            return super().json(task, system, user, context)
    handler, _ = writer([body(world, P5="Anaplan sells boats.")])                 # rejected: the prompt is what is looked at
    run(world, Spy({"cover_letter": handler}))
    system, user, ctx = sent[0]
    assert len(ctx["evidence"]) == len(world.plan.evidence) == 3 and ctx["requirements"] == world.plan.reqs
    for tag in ("P3 EVIDENCE", "P4 EVIDENCE"):
        assert tag in user
    assert user.count("method:") == 3 and "JD-MATCHED SKILLS" in system and "EXPERIENCE ROLL-UP" in system


def test_a_letter_whose_p3_uses_a_bullet_of_the_other_group_still_passes_the_number_check(world):
    other = world.plan.groups[1][0]["text"]
    assert CL.words([other]) > 0                                                   # numbers in any of the chosen bullets are allowed
    assert not [p for p in check(world, letter_parts(world)) if "number(s) about you" in p]


def test_a_job_with_a_single_usable_bullet_has_no_letter_and_says_why(world):
    one = {next(iter(world.chosen - {world.hk.proof_bullet_id}))}
    plan = CL.prepare(JOB, world.fb, world.kws, world.hk, "", world.evidence, world.settings, world.claimable, one)
    assert plan.error


def test_a_cv_without_a_work_experience_bullet_besides_the_hooks_still_rolls_up_what_it_has(world):
    exp = {b["id"] for e in world.fb["experience"] for b in e["bullets"]}
    plan = CL.prepare(JOB, world.fb, world.kws, world.hk, "", world.evidence, world.settings, world.claimable, world.chosen - exp | {world.hk.proof_bullet_id})
    assert plan.error == "" or "P4" in plan.error or "skill" in plan.error


# ------------------------------------------------------------------ the length is a range
def test_the_shipped_range_is_200_to_320(settings):
    assert settings["letter"]["company_words"] == [200, 320]
    assert CL.word_range(settings) == (200, 320)


@pytest.mark.parametrize("value, expected", [([200, 320], (200, 320)), ([250, 300], (250, 300)), (["220", 310], (220, 310)), ([275, 275], (275, 275))])
def test_a_range_of_two_whole_numbers_is_accepted(settings, value, expected):
    settings.raw["letter"]["company_words"] = value
    assert CL.word_range(settings) == expected


@pytest.mark.parametrize("value", [300, "300", [230], [230, 300, 320], [300, 230], [150, 300], [230, 321], ["a", "b"], [True, 300], None, "230-300"])
def test_anything_else_is_refused_with_the_setting_named(settings, value):
    settings.raw["letter"]["company_words"] = value
    with pytest.raises(ValueError, match=r"letter.company_words must be \[low, high\]"):
        CL.word_range(settings)


def test_the_model_is_asked_for_the_middle_of_the_range(settings):
    assert CL.target_words(settings) == 260
    settings.raw["letter"]["company_words"] = [250, 300]
    assert CL.target_words(settings) == 275
    settings.raw["letter"]["company_words"] = [230, 311]
    assert CL.target_words(settings) == 270


@pytest.mark.parametrize("total", [200, 201, 260, 320])
def test_200_to_320_words_pass(world, total):
    parts = letter_parts(world, total)
    assert CL.words(CL.assemble(parts)[0]) == total
    assert check(world, parts) == []


@pytest.mark.parametrize("total", [199, 150, 321])
def test_outside_the_range_is_rejected_with_the_real_bounds(world, total):
    problems = check(world, letter_parts(world, total))
    assert f"{total} words: the letter must be between 200 and 320" in problems, problems


def test_a_narrower_range_in_settings_is_the_one_checked(world):
    world.settings.raw["letter"]["company_words"] = [230, 280]
    assert check(world, letter_parts(world, 230)) == [] and check(world, letter_parts(world, 280)) == []
    assert "229 words: the letter must be between 230 and 280" in check(world, letter_parts(world, 229))
    world.settings.raw["scoring"]["letter_max_words_company"] = 270                      # the company cap still narrows the top
    assert "280 words: the letter must be between 230 and 270" in check(world, letter_parts(world, 280))


def test_review_prints_the_accepted_range_from_the_letter(world):
    from careerops.review import workflow

    letter = {"mode": "company", "status": "ok", "words": 271, "target_words": 275, "accepted": [230, 320], "hook_source": "library",
              "requirements": [["Python"], []], "p5_sources": [], "names": ["P1", "P3", "P4", "P5", "P7"]}
    assert "accepted 230-320" in workflow.company_letter_lines(letter)[0]


# ------------------------------------------------------------------ the user guard: a capability or skill word the bank says anywhere passes
def bank_has(world, word: str) -> bool:
    return grounding.has_in_bank(grounding.bank_text(world.fb), word)


def capability(problems: list[str]) -> list[str]:
    return [p for p in problems if "(capability)" in p]


def test_deployment_in_p3_passes_when_the_fact_bank_says_it_anywhere(world):
    assert bank_has(world, "deployment") and "deployment" not in " ".join(e["text"].lower() for e in world.plan.evidence)
    p3 = P3 + " That grounded my approach to deployment."
    assert not capability(check(world, letter_parts(world, P3=p3))), check(world, letter_parts(world, P3=p3))


@pytest.mark.parametrize("word", ["production", "pipelines", "end-to-end", "monitoring"])
def test_the_other_capability_words_pass_when_the_bank_has_them(world, word):
    fb = copy.deepcopy(world.fb)
    fb["profile_facts"] = list(fb.get("profile_facts") or []) + [f"Comfortable with {word} work."]
    assert grounding.has_in_bank(grounding.bank_text(fb), word)
    problems = grounding.grounding_problems([f"I care about {word} work."], world.plan.evidence[:1], "", "", "", bank=grounding.bank_text(fb))
    assert not capability(problems), problems


def test_a_capability_word_the_bank_does_not_say_still_fails(world):
    evidence = world.plan.evidence[:1]
    assert "end-to-end" not in evidence[0]["text"].lower()
    problems = grounding.grounding_problems(["I bring end-to-end ownership."], evidence, "", "", "", bank="the bank says nothing like that")
    assert capability(problems), problems
    assert not capability(grounding.grounding_problems(["I bring end-to-end ownership."], evidence, "", "", "", bank="end-to-end work in the bank"))


def test_a_hyphenated_name_nothing_allowed_says_still_fails(world):
    assert "zorbix" not in grounding.bank_text(world.fb).lower() and "zorbix" not in world.job["description"].lower()
    p3 = P3 + " I like Zorbix-driven releases."
    problems = check(world, letter_parts(world, P3=p3, total=260))
    assert any("Zorbix-driven" in p for p in problems), problems


def test_ci_driven_passes_by_its_name_part_when_the_ad_says_ci(world):
    """v2.5: the per-paragraph guard reads the ad (as name_problems does), and a hyphenated compound is checked by its name part (2 Nov RightShip)."""
    assert "ci-driven" not in grounding.bank_text(world.fb)
    assert grounding._has(grounding.norm_literal(world.job["description"]), "CI")
    problems = check(world, letter_parts(world, P3=P3 + " I like CI-driven releases.", total=260))
    assert not any("CI-driven" in p and "(name)" in p for p in problems), problems


def test_names_sectors_and_clients_are_not_relaxed_by_the_bank(world):
    fb_text = grounding.bank_text(world.fb)
    assert "fintech" in fb_text                                                            # the bank says it, the evidence of this paragraph does not
    problems = grounding.grounding_problems(["I know fintech customers well."], world.plan.evidence[:1], "", "", "", bank=fb_text)
    assert any("(sector)" in p or "(client)" in p for p in problems), problems


def test_the_cross_bullet_postgresql_link_still_fails(world):
    p3 = P3 + " I ran the churn classifier on PostgreSQL."
    assert "postgresql" in grounding.bank_text(world.fb)
    problems = check(world, letter_parts(world, P3=p3))
    assert any("postgresql" in p.lower() for p in problems), problems


def test_a_number_not_in_a_chosen_bullet_still_fails_even_if_the_bank_has_it(world):
    assert "14 heterogeneous source datasets" in grounding.bank_text(world.fb)               # bullet a5, which is not one of the chosen ones
    assert "a5" not in {e["id"] for e in world.plan.evidence}
    p3 = P3 + " I once handled 14 source datasets."
    problems = check(world, letter_parts(world, P3=p3))
    assert any("number(s) about you not in the chosen bullets" in p for p in problems), problems


@pytest.mark.parametrize("word", ["robust", "seamless", "cutting-edge", "passionate", "leverage", "synergy"])
def test_filler_is_still_rejected_even_if_the_bank_says_it(world, word):
    fb = copy.deepcopy(world.fb)
    fb["profile_facts"] = list(fb.get("profile_facts") or []) + [f"A {word} approach."]
    world.fb = fb
    problems = check(world, letter_parts(world, P3=P3 + f" The result was {word} for the team."))
    assert problems, word


def test_the_company_guard_on_p5_is_unchanged_by_the_bank(world):
    claim = "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia."
    problems = check(world, letter_parts(world, P5=claim))
    assert any(p.startswith("P5: claim about the company is not supported by the ad") for p in problems), problems
