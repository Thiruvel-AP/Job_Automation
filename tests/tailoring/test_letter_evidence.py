"""Round of 22 Oct (2): the company letter's P3 and P4 each use the two best fact-bank bullets for ONE named requirement of the ad (four bullets
in all), the length is a range (letter.company_words: [330, 420]), and the user guard lets a capability or skill word through when the fact bank
says it anywhere. MockLLM only; the guards are called directly."""
import copy

import pytest

from careerops.tailoring import company_letter as CL
from careerops.tailoring import grounding
from careerops.tailoring import letters as L
from careerops.tailoring.scorer import keyword_hits
from test_company_letter import JD, JOB, P2, TEAM, body, check, pad, world  # noqa: F401  (world is a fixture)


def hits(world, bullet: dict) -> set[str]:
    return {world.kws[i].term for i in keyword_hits(" ".join([bullet["text"], *bullet.get("terms", [])]), world.kws)}


# ------------------------------------------------------------------ four bullets, two requirements
def test_p3_and_p4_each_get_two_bullets_for_one_named_requirement(world):
    plan = world.plan
    assert len(plan.evidence) == 4 and len({e["id"] for e in plan.evidence}) == 4          # four different bullets in all
    assert len(plan.reqs) == 2 and all(len(r) == 1 for r in plan.reqs) and plan.reqs[0] != plan.reqs[1]   # one named requirement each
    assert [len(g) for g in plan.groups] == [2, 2] and [e for g in plan.groups for e in g] == plan.evidence
    for group, req in zip(plan.groups, plan.reqs):
        assert all(req[0] in hits(world, e) for e in group), (req, [e["id"] for e in group])      # both bullets evidence that requirement


def test_each_group_is_the_two_best_bullets_for_its_requirement_and_never_the_hooks_proof_bullet(world):
    from careerops.tailoring.budget import letter_scores

    assert world.hk.proof_bullet_id not in {e["id"] for e in world.plan.evidence}
    score = letter_scores(world.fb, world.kws, JD)
    every = {b["id"]: b for sec in ("experience", "projects") for e in world.fb[sec] for b in e["bullets"]}
    pool = [b for b in sorted(score, key=lambda b: -score[b]) if b in world.chosen and b != world.hk.proof_bullet_id]
    taken: set[str] = set()
    for group, req in zip(world.plan.groups, world.plan.reqs):
        best = [b for b in pool if b not in taken and req[0] in hits(world, every[b])][:2]
        assert [e["id"] for e in group] == best
        taken |= set(best)


def test_the_prompt_shows_four_evidence_blocks_grouped_by_paragraph_and_the_context_carries_them(world):
    from careerops.llm import MockLLM
    from test_company_letter import run, writer

    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append((system, user, context))
            return super().json(task, system, user, context)
    handler, _ = writer(world, [body(world, 400, p2="Anaplan sells boats.")])                 # rejected: the prompt is what is looked at
    run(world, Spy({"cover_letter": handler}))
    system, user, ctx = sent[0]
    assert len(ctx["evidence"]) == 4 and len(ctx["requirements"]) == 2
    for tag in ("P3 EVIDENCE", "P4 EVIDENCE"):
        assert tag in user
    assert user.count("method:") == 4 and "the two best bullets" in system.lower().replace("\n", " ")


def test_a_letter_whose_p3_names_the_requirement_but_uses_a_bullet_of_the_other_group_still_passes_the_number_check(world):
    other = world.plan.groups[1][0]["text"]
    assert CL.words([other]) > 0                                                           # numbers in any of the four bullets are allowed
    problems = check(world, body(world, 400))
    assert not [p for p in problems if "number(s) about you" in p], problems


def test_fewer_than_two_bullets_for_a_requirement_falls_back_to_what_there_is(world):
    kws = [k for k in world.kws if k.term in ("PyTorch", "XGBoost")]                      # one bullet each in the fixture bank
    plan = CL.prepare(JOB, world.fb, kws, world.hk, "", world.evidence, world.settings, world.claimable, world.chosen)
    assert not plan.error and 2 <= len(plan.evidence) <= 4 and len(plan.groups) == 2


def test_a_job_with_a_single_usable_bullet_has_no_letter_and_says_why(world):
    one = {next(iter(world.chosen - {world.hk.proof_bullet_id}))}
    plan = CL.prepare(JOB, world.fb, world.kws, world.hk, "", world.evidence, world.settings, world.claimable, one)
    assert plan.error


# ------------------------------------------------------------------ the length is a range
def test_the_shipped_range_is_330_to_420(settings):
    assert settings["letter"]["company_words"] == [330, 420]
    assert CL.word_range(settings) == (330, 420)


@pytest.mark.parametrize("value, expected", [([330, 420], (330, 420)), ([300, 400], (300, 400)), (["350", 410], (350, 410)), ([375, 375], (375, 375))])
def test_a_range_of_two_whole_numbers_is_accepted(settings, value, expected):
    settings.raw["letter"]["company_words"] = value
    assert CL.word_range(settings) == expected


@pytest.mark.parametrize("value", [400, "400", [330], [330, 420, 500], [420, 330], [250, 400], [330, 421], ["a", "b"], [True, 400], None, "330-420"])
def test_anything_else_is_refused_with_the_setting_named(settings, value):
    settings.raw["letter"]["company_words"] = value
    with pytest.raises(ValueError, match=r"letter.company_words must be \[low, high\]"):
        CL.word_range(settings)


def test_the_model_is_asked_for_the_middle_of_the_range(settings):
    assert CL.target_words(settings) == 375
    settings.raw["letter"]["company_words"] = [350, 400]
    assert CL.target_words(settings) == 375
    settings.raw["letter"]["company_words"] = [330, 411]
    assert CL.target_words(settings) == 370


@pytest.mark.parametrize("total", [330, 331, 375, 420])
def test_330_to_420_words_pass(world, total):
    paragraphs = body(world, total)
    assert CL.words([world.plan.p1, *paragraphs, world.plan.p5]) == total
    assert check(world, paragraphs) == []


@pytest.mark.parametrize("total", [329, 250, 421])
def test_outside_the_range_is_rejected_with_the_real_bounds(world, total):
    problems = check(world, body(world, total))
    assert f"{total} words: the letter must be between 330 and 420" in problems, problems


def test_a_narrower_range_in_settings_is_the_one_checked(world):
    world.settings.raw["letter"]["company_words"] = [360, 400]
    assert check(world, body(world, 360)) == [] and check(world, body(world, 400)) == []
    assert "359 words: the letter must be between 360 and 400" in check(world, body(world, 359))
    world.settings.raw["scoring"]["letter_max_words_company"] = 390                      # the company cap still narrows the top
    assert "400 words: the letter must be between 360 and 390" in check(world, body(world, 400))


def test_review_prints_the_accepted_range_from_the_letter(world):
    from careerops.review import workflow

    letter = {"mode": "company", "status": "ok", "words": 371, "target_words": 375, "accepted": [330, 420], "hook_source": "library",
              "requirements": [["Python"], ["SQL"]], "p2_sources": []}
    assert "accepted 330-420" in workflow.company_letter_lines(letter)[0]


# ------------------------------------------------------------------ the user guard: a capability or skill word the bank says anywhere passes
def bank_has(world, word: str) -> bool:
    return grounding.has_in_bank(grounding.bank_text(world.fb), word)


def capability(problems: list[str]) -> list[str]:
    return [p for p in problems if "(capability)" in p]


def test_deployment_in_p3_passes_when_the_fact_bank_says_it_anywhere(world):
    assert bank_has(world, "deployment") and "deployment" not in " ".join(e["text"].lower() for e in world.plan.evidence)
    p3 = world.own[0] + " That grounded my approach to deployment."
    assert not capability(check(world, body(world, 400, p3=p3))), check(world, body(world, 400, p3=p3))


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


def test_ci_driven_is_not_in_the_bank_and_still_fails(world):
    assert "ci-driven" not in grounding.bank_text(world.fb)
    p3 = world.own[0] + " I like CI-driven releases."
    problems = check(world, body(world, 400, p3=p3))
    assert any("CI-driven" in p for p in problems), problems


def test_names_sectors_and_clients_are_not_relaxed_by_the_bank(world):
    fb_text = grounding.bank_text(world.fb)
    assert "fintech" in fb_text                                                            # the bank says it, the evidence of this paragraph does not
    problems = grounding.grounding_problems(["I know fintech customers well."], world.plan.evidence[:1], "", "", "", bank=fb_text)
    assert any("(sector)" in p or "(client)" in p for p in problems), problems


def test_the_cross_bullet_postgresql_link_still_fails(world):
    p3 = world.own[0] + " I ran the churn classifier on PostgreSQL."
    assert "postgresql" in grounding.bank_text(world.fb)
    problems = check(world, body(world, 400, p3=p3))
    assert any("postgresql" in p.lower() for p in problems), problems


def test_a_number_not_in_a_chosen_bullet_still_fails_even_if_the_bank_has_it(world):
    assert "14 heterogeneous source datasets" in grounding.bank_text(world.fb)               # bullet a5, which is not one of the four
    assert "a5" not in {e["id"] for e in world.plan.evidence}
    p3 = world.own[0] + " I once handled 14 source datasets."
    problems = check(world, body(world, 400, p3=p3))
    assert any("number(s) about you not in the chosen bullets" in p for p in problems), problems


@pytest.mark.parametrize("word", ["robust", "seamless", "cutting-edge", "passionate", "leverage", "synergy"])
def test_filler_is_still_rejected_even_if_the_bank_says_it(world, word):
    fb = copy.deepcopy(world.fb)
    fb["profile_facts"] = list(fb.get("profile_facts") or []) + [f"A {word} approach."]
    world.fb = fb
    problems = check(world, body(world, 400, p3=world.own[0] + f" The result was {word} for the team."))
    assert problems, word


def test_the_company_guard_on_p2_is_unchanged_by_the_bank(world):
    claim = "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia."
    problems = check(world, body(world, 400, p2=claim))
    assert any(p.startswith("P2: claim about the company is not supported by the ad") for p in problems), problems
