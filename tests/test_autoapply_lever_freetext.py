"""A Lever form's custom question is a textarea with its label ABOVE it (the box itself only says "Type your response"). The free-text drafter runs on it
like on any other required question; a label that cannot be read is said so, never skipped in silence. Headless Chromium on a local fixture
(tests/fixtures/apply/lever_form.html): no network, no model (a stub stands in for apply/freetext.py)."""
import functools

import pytest
from test_autoapply import APPLICANT, FIX, chromium, world  # noqa: F401  (the fixtures and the runner of the auto-apply tests)

from careerops import autoapply
from careerops.apply import autolog, filler
from careerops.apply.freetext import Limit, Plan, Proposal

pytestmark = pytest.mark.slow          # these tests drive Chromium


class Stub:
    """What apply/freetext.FreeText offers the form loop: `propose(question, label, maxlength, ...)` (a draft), `review(proposal, strict)` (the person's y),
    `last`, and the `ask` / `out` the runner sets."""

    def __init__(self, text="I like what Valarian builds and I would like to help run its models in production."):
        self.questions, self.labels, self.text, self.last = [], [], text, "approved"
        self.ask = self.out = None

    def propose(self, question, label="", maxlength=None, *, problems=None, tries=2):
        self.questions.append(question)
        self.labels.append(label)
        return Proposal(question, "why_company", Limit(words=120), Plan("why_company", [], "stub"), self.text, "draft")

    def review(self, proposal, strict=True, on_text=None):
        self.last = "approved"
        return proposal.text


def run(world, monkeypatch, stub, page="lever_form.html", replies=None):
    monkeypatch.setattr(autoapply, "run", functools.partial(autoapply.run, freetext=stub))
    return world.run(page, replies or {}, submit=False)


def test_a_lever_custom_question_with_its_label_above_gets_a_draft_offered_and_filled(world, monkeypatch):
    stub = Stub()
    r = run(world, monkeypatch, stub)
    assert stub.questions == ["Describe a project you are proud of"]                    # the real question, not "Type your response"
    assert world.state["values"]["cards[1][field0]"] == stub.text and r.status == autolog.NOT_SUBMITTED
    assert any("Describe a project you are proud of" in f and "free text (approved)" in f for f in r.filled)


def test_the_standard_lever_fields_are_still_filled_from_applicant_yaml(world, monkeypatch):
    run(world, monkeypatch, Stub())
    v = world.state["values"]
    assert v["name"] == "Ada Lovelace" and v["email"] == APPLICANT["email"]


def test_a_box_whose_question_cannot_be_read_is_reported_not_skipped_and_gets_no_draft(world, monkeypatch):
    stub = Stub()
    r = run(world, monkeypatch, stub, page="lever_form.html?nolabel")
    assert stub.questions == []
    assert r.status == autolog.NEEDS_USER and "cannot read the question of a required text box" in r.reason
    assert any("cannot be read" in line and "Type your response" in line for line in world.out)
    assert world.state["values"].get("cards[1][field0]", "") == ""


@pytest.mark.parametrize("question, ok", [
    ("Why do you want to work at Valarian?", True), ("Describe a project you are proud of", True), ("Anything else?", True),
    ("Type your response", False), ("type your response", False), ("Enter your answer here", False), ("", False), ("*", False),
    ("cards[abc123][field0]", False), ("field_7", False)])
def test_what_counts_as_a_readable_question(question, ok):
    assert filler.readable_question(question) is ok
