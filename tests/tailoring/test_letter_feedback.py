"""Review's `l` on a company letter asks "What should change?"; the next draft is handed the previous draft (P3, P4 and P5, named as in the paragraph
table), every guard rejection rewritten as an instruction, and the reader's feedback word for word; at most three drafts per `l`; the feedback is kept in paths.letter_feedback (jsonl).
MockLLM only. The drafts used here are rejected on purpose, so no PDF is made (fast)."""
import json
from datetime import date

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letter_feedback as LF
from test_company_letter import JD, JOB, P3, P4, P5, TEAM, body, check, letter_parts, world  # noqa: F401  (world is a fixture)

FEEDBACK = "Too formal. Lead P3 with the churn model and say what the customers did with it. Shorten P4."


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
def lines_for(world, parts, problems):
    return LF.instructions(problems, [parts["P3"], parts["P4"], parts["P5"]], world.plan)


def test_a_filler_word_becomes_remove_it(world):
    parts = letter_parts(world, P3=P3 + " The result was robust for the team.")
    problems = check(world, parts)
    assert "vague filler 'robust' is not in the evidence" in problems
    assert "remove 'robust'" in lines_for(world, parts, problems)


def test_too_short_names_the_count_and_the_bullet_whose_detail_to_use(world):
    parts = letter_parts(world, 188)
    problems = check(world, parts)
    assert "188 words: the letter must be between 200 and 320" in problems
    line = next(x for x in lines_for(world, parts, problems) if x.startswith("the letter is 188 words"))
    assert "12 short of the 200 minimum" in line and "use more of the detail of bullet " in line
    named = line.split("use more of the detail of bullet ")[1].split()[0].strip(".,:;")
    assert named in {e["id"] for e in world.plan.evidence}


def test_too_long_names_how_many_words_to_cut(world):
    parts = letter_parts(world, 350)
    lines = lines_for(world, parts, check(world, parts))
    assert any(x.startswith("the letter is 350 words, 30 over the 320 maximum: cut 30 words") for x in lines), lines


def test_a_number_a_tool_and_a_missing_skill_become_instructions(world):
    parts = letter_parts(world, P3=P3 + " That cut the error rate by 83% and ran on Docker.")
    lines = lines_for(world, parts, check(world, parts))
    assert any(x.startswith("P3: remove the number(s) ['83%']") for x in lines), lines
    assert any(x.startswith("P3: remove 'docker'") for x in lines), lines
    none = "My closest match is a churn model built on 7,043 customer records. Cross validation chose the winner, and I set its threshold to favour recall."
    parts = letter_parts(world, P3=none)
    lines = lines_for(world, parts, check(world, parts))
    assert any(x.startswith("P3: name at least 2 of these ad skills: ") and world.plan.reqs[0][0] in x for x in lines), lines


def test_p4_and_p5_rejections_become_instructions(world):
    parts = letter_parts(world, P4=P4.replace("Bristol Pro Bono Consulting", "a charity").replace("19,213-gene ", ""))
    lines = lines_for(world, parts, check(world, parts))
    assert "P4: name the employer Bristol Pro Bono Consulting in its own sentence" in lines
    assert any(x.startswith("P4: give one number of the Northwind Research Lab bullet") for x in lines), lines
    claim = "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia."
    parts = letter_parts(world, P5=claim)
    lines = lines_for(world, parts, check(world, parts))
    assert any(x.startswith("P5: remove or rewrite") and "the ad does not support it" in x for x in lines), lines
    assert "P5: quote or closely follow one of the numbered About / role sentences, in its own words" in lines_for(world, parts, ["P5 follows 0 of the ad's About / role sentences; it must quote or closely follow 1 of them (for example: 'x')"])
    assert "P5: write ONE short sentence" in lines_for(world, parts, ["P5 is 2 sentences; it must be one"])


def test_a_memory_figure_and_a_wrong_paragraph_count_become_instructions(world):
    parts = letter_parts(world, P3=P3 + " I cut memory use from 8.2 to 5.5 GB.")
    assert any(x.startswith("P3: remove '5.5 GB'") or x.startswith("P3: remove '8.2 to 5.5 GB'") or "the ad is not about memory" in x for x in lines_for(world, parts, check(world, parts)))
    assert 'return JSON {"paragraphs": ["P3", "P4", "P5"]}: exactly three paragraphs' in LF.instructions(["expected 3 paragraphs (P3, P4, P5), got 5"], [], world.plan)


def test_a_reason_nobody_taught_the_rewriter_is_still_an_instruction(world):
    lines = LF.instructions(["something new the guard says"], [], world.plan)
    assert lines == ["fix this: something new the guard says"]


def test_no_instruction_is_repeated():
    plan = CL.Plan()
    assert LF.instructions(["vague filler 'robust' is not in the evidence", "vague filler 'robust' is not in the evidence"], [], plan) == ["remove 'robust'"]


# ------------------------------------------------------------------ the next draft is told all three
def test_the_next_draft_gets_the_previous_draft_the_instructions_and_the_feedback_word_for_word(world):
    bad = body(world, P3=P3 + " The result was robust for the team.")
    llm = Spy({"cover_letter": answers(world, [bad])})
    make(world, llm, revise={"previous": body(world), "problems": ["vague filler 'seamless' is not in the evidence"], "feedback": FEEDBACK})
    first, second = llm.sent[0], llm.sent[1]
    for prompt in (first, second):
        assert FEEDBACK in prompt                                          # word for word, every draft of this `l`
    assert "remove 'seamless'" in first and body(world)[2][:60] in first          # the earlier rejection and the earlier draft
    assert "remove 'robust'" in second and bad[1][:60] in second              # the draft just rejected and why
    assert "YOUR PREVIOUS DRAFT" in first and "THE READER'S FEEDBACK" in first and "CHANGE THESE" in first
    assert f"P4: {body(world)[1]}" in first and f"P5: {body(world)[2]}" in first      # named as in the paragraph table


def test_a_plain_first_draft_has_no_revision_block(world):
    llm = Spy({"cover_letter": answers(world, [body(world, P5="Anaplan sells boats.")])})
    make(world, llm)
    assert "YOUR PREVIOUS DRAFT" not in llm.sent[0] and "READER'S FEEDBACK" not in llm.sent[0]
    assert "YOUR PREVIOUS DRAFT" in llm.sent[1]                            # a redraft after a rejection always shows the draft it replaces


def test_an_enter_with_no_feedback_still_hands_over_the_previous_draft_and_its_reasons(world):
    llm = Spy({"cover_letter": answers(world, [body(world, P5="Anaplan sells boats.")])})
    make(world, llm, revise={"previous": body(world), "problems": ["P3: names 'docker', which is not in the chosen bullets"], "feedback": ""})
    assert "THE READER'S FEEDBACK" not in llm.sent[0] and "P3: remove 'docker'" in llm.sent[0] and "YOUR PREVIOUS DRAFT" in llm.sent[0]


def test_at_most_three_drafts_per_l(world):
    llm = Spy({"cover_letter": answers(world, [body(world, P5="Anaplan sells boats.")])})
    pdf, letter = make(world, llm, revise={"previous": [], "problems": [], "feedback": FEEDBACK})
    assert llm.calls == ["cover_letter"] * 3 and CL.ATTEMPTS == 3 and letter["status"] == "none"
    assert letter["draft_body"] == body(world, P5="Anaplan sells boats.")           # the last draft is kept for the next `l`
    assert letter["opening"] == world.plan.p1 and letter["closing"] == world.plan.p7


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
