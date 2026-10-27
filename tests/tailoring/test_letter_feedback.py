"""Review's `l` on a company letter asks "What should change?"; the next draft is handed the previous draft, every guard rejection rewritten as an
instruction, and the reader's feedback word for word; at most three drafts per `l`; the feedback is kept in paths.letter_feedback (jsonl).
MockLLM only. The drafts used here are rejected on purpose, so no PDF is made (fast)."""
import json
from datetime import date

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letter_feedback as LF
from test_company_letter import JD, JOB, P2, TEAM, body, check, world  # noqa: F401  (world is a fixture)

FEEDBACK = "Too formal. Lead P3 with the churn model and say what the customers did with it."


class Spy(MockLLM):
    def __init__(self, handlers):
        super().__init__(handlers)
        self.sent: list[str] = []

    def json(self, task, system, user, context=None):
        self.sent.append(user)
        return super().json(task, system, user, context)


def make(world, llm, revise=None):
    return CL.make(JOB, world.fb, world.kws, world.hk, "", world.evidence, llm, world.settings, world.settings.path("outputs") / "company",
                   "Ada Lovelace Cover Letter", TEAM, world.claimable, world.chosen, revise=revise)


def answers(world, drafts: list[list[str]]):
    """A cover_letter handler that answers with the next draft of `drafts` (the last one again when they run out)."""
    n = {"i": 0}

    def handler(ctx):
        d = drafts[min(n["i"], len(drafts) - 1)]
        n["i"] += 1
        return {"paragraphs": d}
    return handler


# ------------------------------------------------------------------ a rejection becomes an instruction
def test_a_filler_word_becomes_remove_it(world):
    problems = check(world, body(world, 400, p3=world.own[0] + " The result was robust for the team."))
    assert "vague filler 'robust' is not in the evidence" in problems
    assert "remove 'robust'" in LF.instructions(problems, [world.plan.p1, *body(world, 400), world.plan.p5], world.plan)


def test_too_short_names_the_count_and_the_bullet_whose_detail_to_use(world):
    paragraphs = body(world, 288)
    problems = check(world, paragraphs)
    assert "288 words: the letter must be between 300 and 500" in problems
    lines = LF.instructions(problems, [world.plan.p1, *paragraphs, world.plan.p5], world.plan)
    line = next(x for x in lines if x.startswith("the letter is 288 words"))
    assert "12 short of the 300 minimum" in line and "use more of the detail of bullet " in line
    named = line.split("use more of the detail of bullet ")[1].split()[0].strip(".,:;")
    assert named in {e["id"] for e in world.plan.evidence}


def test_too_long_names_how_many_words_to_cut(world):
    paragraphs = body(world, 530)
    lines = LF.instructions(check(world, paragraphs), [world.plan.p1, *paragraphs, world.plan.p5], world.plan)
    assert any(x.startswith("the letter is 530 words, 30 over the 500 maximum: cut 30 words") for x in lines), lines


def test_a_number_a_tool_and_a_missing_requirement_become_instructions(world):
    p3 = world.own[0] + " That cut the error rate by 83% and ran on Docker."
    problems = check(world, body(world, 400, p3=p3))
    lines = LF.instructions(problems, [world.plan.p1, *body(world, 400, p3=p3), world.plan.p5], world.plan)
    assert any(x.startswith("P3: remove the number(s) ['83%']") for x in lines), lines
    assert any(x.startswith("P3: remove 'docker'") for x in lines), lines
    problems = check(world, body(world, 400, p4="Here is another example of my work with customer records."))
    lines = LF.instructions(problems, [world.plan.p1, *body(world, 400), world.plan.p5], world.plan)
    assert any(x.startswith("P4: name ") and world.plan.reqs[1][0] in x for x in lines), lines


def test_a_company_claim_the_ad_does_not_make_becomes_rewrite_p2(world):
    claim = "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia."
    problems = check(world, body(world, 400, p2=claim))
    lines = LF.instructions(problems, [world.plan.p1, *body(world, 400, p2=claim), world.plan.p5], world.plan)
    assert any(x.startswith("P2: remove or rewrite") and "the ad does not support it" in x for x in lines), lines


def test_a_reason_nobody_taught_the_rewriter_is_still_an_instruction(world):
    lines = LF.instructions(["something new the guard says"], [], world.plan)
    assert lines == ["fix this: something new the guard says"]


def test_no_instruction_is_repeated():
    plan = CL.Plan()
    assert LF.instructions(["vague filler 'robust' is not in the evidence", "vague filler 'robust' is not in the evidence"], [], plan) == ["remove 'robust'"]


# ------------------------------------------------------------------ the next draft is told all three
def test_the_next_draft_gets_the_previous_draft_the_instructions_and_the_feedback_word_for_word(world):
    bad = body(world, 400, p3=world.own[0] + " The result was robust for the team.")
    llm = Spy({"cover_letter": answers(world, [bad])})
    make(world, llm, revise={"previous": body(world, 400), "problems": ["vague filler 'seamless' is not in the evidence"], "feedback": FEEDBACK})
    first, second = llm.sent[0], llm.sent[1]
    for prompt in (first, second):
        assert FEEDBACK in prompt                                          # word for word, every draft of this `l`
    assert "remove 'seamless'" in first and body(world, 400)[2][:60] in first          # the earlier rejection and the earlier draft
    assert "remove 'robust'" in second and bad[1][:60] in second              # the draft just rejected and why
    assert "YOUR PREVIOUS DRAFT" in first and "THE READER'S FEEDBACK" in first and "CHANGE THESE" in first


def test_a_plain_first_draft_has_no_revision_block(world):
    llm = Spy({"cover_letter": answers(world, [body(world, 400, p2="Anaplan sells boats.")])})
    make(world, llm)
    assert "YOUR PREVIOUS DRAFT" not in llm.sent[0] and "READER'S FEEDBACK" not in llm.sent[0]
    assert "YOUR PREVIOUS DRAFT" in llm.sent[1]                            # a redraft after a rejection always shows the draft it replaces


def test_an_enter_with_no_feedback_still_hands_over_the_previous_draft_and_its_reasons(world):
    llm = Spy({"cover_letter": answers(world, [body(world, 400, p2="Anaplan sells boats.")])})
    make(world, llm, revise={"previous": body(world, 400), "problems": ["P3: names 'docker', which is not in the chosen bullets"], "feedback": ""})
    assert "THE READER'S FEEDBACK" not in llm.sent[0] and "P3: remove 'docker'" in llm.sent[0] and "YOUR PREVIOUS DRAFT" in llm.sent[0]


def test_at_most_three_drafts_per_l(world):
    llm = Spy({"cover_letter": answers(world, [body(world, 400, p2="Anaplan sells boats.")])})
    pdf, letter = make(world, llm, revise={"previous": [], "problems": [], "feedback": FEEDBACK})
    assert llm.calls == ["cover_letter"] * 3 and CL.ATTEMPTS == 3 and letter["status"] == "none"
    assert letter["draft_body"] == body(world, 400, p2="Anaplan sells boats.")           # the last draft is kept for the next `l`
    assert letter["opening"] == world.plan.p1 and letter["closing"] == world.plan.p5


# ------------------------------------------------------------------ the feedback file
def test_feedback_is_appended_with_the_job_id_the_text_and_the_date(settings):
    path = LF.save(settings, "manual:abc", FEEDBACK)
    LF.save(settings, "manual:def", "Shorter.")
    assert path == settings.path("letter_feedback") and path.name == "feedback.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"job_id": "manual:abc", "text": FEEDBACK, "date": date.today().isoformat()}
    assert [r["job_id"] for r in rows] == ["manual:abc", "manual:def"]


def test_the_feedback_path_is_in_the_shipped_settings(settings):
    assert str(settings.raw["paths"]["letter_feedback"]).endswith("letters/feedback.jsonl") or settings.path("letter_feedback").name == "feedback.jsonl"


def test_the_most_recent_feedback_of_a_job_is_found(settings):
    LF.save(settings, "manual:abc", "first")
    LF.save(settings, "manual:abc", "second")
    LF.save(settings, "manual:other", "elsewhere")
    assert LF.last(settings, "manual:abc") == "second" and LF.last(settings, "manual:none") == ""
