"""The letter's name guard sees this job's CV (Document rules B3), and its retries: draft 1 at temperature 0, drafts 2 and 3 at 0.4 and 0.7; every redraft
is told every earlier rejection word for word; two drafts that are 80% the same stop the tries; each paragraph's evidence is named before the model writes,
and P4's bullets carry a number. MockLLM only; no PDF is made (every draft here is rejected)."""
from test_company_letter import JOB, P3, P4, letter_parts, run, world, writer  # noqa: F401  (the fixtures and runners of the letter tests)

from careerops.llm import MockLLM
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letter_feedback as LF
from careerops.tailoring.faithfulness import numbers

BAD_P5 = ["Anaplan serves insurance customers across Europe with 9,000 employees.",
          "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia.",
          "Anaplan runs deep sea mining platforms for forty nations."]
NAME = "(a name) is not in the ad or the fact bank"
LETTERS = "abcdefghij"


def draft(w, n: int) -> list[str]:
    """A draft that is rejected (its P5) and shares well under 80% of its 5-word windows with the other drafts (a long tail of its own words in P4)."""
    tail = " ".join(f"{LETTERS[n]}{k}" for k in range(40))
    parts = letter_parts(w, 300, P4=f"{P4} {tail}", P5=BAD_P5[n])
    return [parts["P3"], parts["P4"], parts["P5"]]


def cv_with(w, skills: list[str]) -> dict:
    ids = [e["id"] for e in w.plan.evidence]
    return CL.cv_from_ctx({"experience": [{"bullet_ids": ids, "bullets": [], "stack": ""}], "skills": {"Tools": skills}, "profile": ""})


def names_flagged(w, add: str, cv: dict | None) -> list[str]:
    problems, _ = CL.check(letter_parts(w, P3=P3 + add), w.job, w.fb, w.kws, w.plan, "Functional Analytics", w.settings, cv=cv, claimable=w.claimable)
    return [p for p in problems if NAME in p]


class Spy(MockLLM):
    """A MockLLM that keeps the user message of every call."""

    def __init__(self, handlers):
        super().__init__(handlers)
        self.sent: list[str] = []

    def json(self, task, system, user, context=None):
        self.sent.append(user)
        return super().json(task, system, user, context)


# ------------------------------------------------------------------ item 3: the name guard
def test_a_skill_the_cv_prints_is_not_an_invented_name(world):
    add = " I also used Terraform for it."
    assert any("'Terraform'" in p for p in names_flagged(world, add, None))                         # no CV known: the old rule
    assert any("'Terraform'" in p for p in names_flagged(world, add, cv_with(world, ["Python"])))   # a skill on neither this CV nor the ad: still rejected
    assert not any("'Terraform'" in p for p in names_flagged(world, add, cv_with(world, ["Python", "Terraform"])))     # on this CV: allowed


def test_a_skill_on_the_cv_does_not_make_another_skill_allowed(world):
    flagged = names_flagged(world, " I also used Terraform and Pulumi for it.", cv_with(world, ["Terraform"]))
    assert any("'Pulumi'" in p for p in flagged) and not any("'Terraform'" in p for p in flagged)


def test_the_cv_names_come_from_its_skills_stacks_and_bullets_not_its_profile():
    cv = CL.cv_from_ctx({"skills": {"Languages": ["Python", "SQL"]}, "experience": [{"stack": "dbt", "bullets": ["Built a Looker model"], "bullet_ids": ["a"]}],
                         "profile": "Fond of Haskell."})
    assert all(x in cv["names"] for x in ("Python", "SQL", "dbt", "Looker")) and "Haskell" not in cv["names"] and "Haskell" in cv["text"]


def test_python_on_the_cv_passes_the_name_check_and_one_on_neither_cv_nor_ad_does_not():
    allowed = "the ad and the bullets say nothing about it"
    assert CL.name_problems(["I also used Python for it."], allowed)
    assert not CL.name_problems(["I also used Python for it."], allowed + " Python SQL")


# ------------------------------------------------------------------ item 4: the retries
def test_draft_1_is_at_temperature_0_and_the_redrafts_at_0_4_and_0_7(world):
    handler, seen = writer([draft(world, 0), draft(world, 1), draft(world, 2)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and [c["temperature"] for c in seen] == [0.0, 0.4, 0.7] and letter["retry"]["temperatures"] == [0.0, 0.4, 0.7]
    assert CL.TEMPERATURES == (0.0, 0.4, 0.7)


def test_every_redraft_is_told_every_earlier_rejection_word_for_word(world):
    handler, _ = writer([draft(world, 0), draft(world, 1), draft(world, 2)])
    llm = Spy({"cover_letter": handler})
    _, letter = run(world, llm)
    sent = llm.sent
    f = [x["problems"] for x in letter["retry"]["failures"]]
    assert len(sent) == 3 and "EVERY REJECTION SO FAR" not in sent[0]
    assert all(f"- {p}" in sent[1] for p in f[0]) and "Draft 2:" not in sent[1]
    assert all(f"- {p}" in sent[2] for p in f[0]) and all(f"- {p}" in sent[2] for p in f[1]) and "Draft 1:" in sent[2] and "Draft 2:" in sent[2]


def test_the_history_block_is_exact_and_empty_without_rejections():
    assert LF.history_block([]) == "" and LF.history_block([[]]) == ""
    assert LF.history_block([["P5: a"], ["P5: a", "P4: b"]]) == (
        "EVERY REJECTION SO FAR, word for word (none of these may come back):\nDraft 1:\n- P5: a\nDraft 2:\n- P5: a\n- P4: b")
    assert LF.revision_block([], [], "") == ""


def test_a_draft_80_percent_like_the_one_before_stops_the_tries(world):
    handler, seen = writer([draft(world, 0)])                                    # the model answers with the same draft again
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and len(seen) == 2 and letter["status"] == "none"
    assert letter["retry"]["stopped_early"].startswith("draft 2 is 100% the same as draft 1")
    assert len(letter["retry"]["failures"]) == 2


def test_drafts_that_differ_get_all_three_tries(world):
    handler, seen = writer([draft(world, 0), draft(world, 1), draft(world, 2)])
    _, letter = run(world, MockLLM({"cover_letter": handler}))
    assert len(seen) == 3 and "stopped_early" not in letter["retry"]


def test_each_paragraph_is_given_its_evidence_in_advance_and_p4_must_hold_a_number(world):
    handler, _ = writer([draft(world, 0)])
    llm = Spy({"cover_letter": handler})
    run(world, llm)
    assert "EVIDENCE ASSIGNMENT" in llm.sent[0]
    block = llm.sent[0].split("EVIDENCE ASSIGNMENT", 1)[1]
    p3, p4, p5 = (next(x for x in block.splitlines() if x.startswith(f"- {n}:")) for n in ("P3", "P4", "P5"))
    assert all(f"(bullet {e['id']})" in p3 for e in world.plan.groups[0])
    for e in world.plan.groups[1]:
        assert CL.employer(e) in p4
        assert (sorted(numbers(e["text"]))[0] in p4 and "MUST contain" in p4) if numbers(e["text"]) else "this bullet has none" in p4
    assert "no bullet" in p5


def test_p4s_bullets_carry_a_number_whenever_their_role_has_a_numbered_bullet_even_if_p3_used_it(world):
    entry_of = {b["id"]: e["id"] for e in world.fb["experience"] for b in e["bullets"]}
    has_number = {r: any(numbers(b["text"]) for e in world.fb["experience"] if e["id"] == r for b in e["bullets"]) for r in set(entry_of.values())}
    avoid = {b["id"] for e in world.fb["experience"] for b in e["bullets"] if numbers(b["text"])}                 # every numbered bullet is "used"
    again = CL.role_group(world.fb, world.kws, world.chosen, JOB["description"], world.hk, avoid)
    assert again and all(numbers(e["text"]) for e in again if has_number[entry_of[e["id"]]])
    roles = [entry_of[e["id"]] for e in again]
    assert all(has_number[r] for r in roles[:sum(has_number.values())])                                          # the roles that have a number come first
