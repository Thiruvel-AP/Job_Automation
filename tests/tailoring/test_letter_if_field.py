"""v2.4.1: letter.mode `if_field` (the code default) writes no letter at tailoring time; the apply step writes it when the form has a cover-letter field. And no
place says a bare "no letter" any more: the guards' own words (tailoring/letter_reason.py). No network, no model."""
import pytest

from careerops.tailoring import tailor
from careerops.tailoring.letter_reason import letter_reason

REASON = "3 draft(s) rejected by the company guards; last: P4: number(s) about you not in the chosen bullets: ['20%']"


def test_if_field_is_a_known_mode_and_a_missing_key_is_if_field(settings):
    assert "if_field" in tailor.LETTER_MODES
    del settings.raw["letter"]["mode"]
    assert tailor.letter_raw_mode(settings) == "if_field" and tailor.letter_deferred(settings)
    assert tailor.letter_mode(settings) == "company"                       # the writers use the company letter


@pytest.mark.parametrize("raw,raw_mode,deferred", [("company", "company", False), ("off", "off", False), (False, "off", False), (None, "off", False),
                                                    ("if_field", "if_field", True), ("IF_FIELD", "if_field", True), ("hook", "hook", False)])
def test_the_modes_are_read_as_written(settings, raw, raw_mode, deferred):
    settings.raw["letter"]["mode"] = raw
    assert tailor.letter_raw_mode(settings) == raw_mode and tailor.letter_deferred(settings) is deferred


def test_the_users_company_mode_is_unchanged(settings):
    settings.raw["letter"]["mode"] = "company"
    assert not tailor.letter_deferred(settings) and tailor.letter_mode(settings) == "company"


def test_at_tailoring_time_if_field_writes_nothing_and_says_why(settings, fb_fixture):
    settings.raw["letter"]["mode"] = "if_field"
    pdf, letter, letters = tailor._letters({"company": "X", "title": "Y", "description": ""}, fb_fixture, [], "", None, [{"id": "az1", "text": "t"}],
                                           llm=None, settings=settings, out_dir=None, letter_name="n", team="", gaps=[], chosen_ids=set(), claimable=[],
                                           possible=None)
    assert pdf is None and letters is None
    assert letter["status"] == "off" and letter["mode"] == "if_field" and "when an application form asks" in letter["reason"]


# ------------------------------------------------------------------ never a bare "no letter"
def test_the_reason_is_the_guards_own_words():
    letter = {"status": "none", "reason": REASON, "problems": ["the hook and paragraph 2 use the same evidence bullet: 'Needed an agent that rese'"],
              "retry": {"failures": [{"attempt": 1, "problems": ["P5: claim about the company is not supported by the ad"]},
                                     {"attempt": 3, "problems": ["P3 and P4 use the same evidence bullet"]}]}}
    why = letter_reason(letter)
    assert REASON in why and "draft 1: P5: claim about the company" in why and "draft 3: P3 and P4 use the same evidence bullet" in why
    assert why.strip().lower() != "no letter"


def test_a_letter_with_no_recorded_reason_says_so_instead_of_no_letter():
    why = letter_reason({"status": "none"})
    assert "no reason" in why and why != "no letter"


def test_a_letter_that_exists_has_no_reason():
    assert letter_reason({"status": "ok"}) == "" and letter_reason({"status": "fallback"}) == ""


def test_the_summary_line_carries_the_reason_and_the_first_problem():
    s = tailor.letter_summary({"status": "none", "reason": REASON, "problems": ["P4: x"], "retry": {}})
    assert s.startswith("NO LETTER (3 draft(s) rejected by the company guards") and "first: P4: x" in s
    assert "NO LETTER (no reason" in tailor.letter_summary({"status": "none"}) or "no reason" in tailor.letter_summary({"status": "none"})


def test_the_note_names_the_reason_where_it_said_no_letter(fb_fixture):
    from careerops import note

    rep = {"ats": {"strict": 63.4, "ceiling": 75.3, "combined": None, "letter": None, "gate": "retry"}, "keywords": [], "hook": {},
           "letter": {"status": "none", "reason": REASON, "problems": [], "retry": {}}, "context": {}}
    text = note.build({"company": "RightShip", "title": "AI Engineer", "description": "x"}, rep, fb_fixture)
    assert "no letter (" + REASON[:30] in text.replace("\n", " ")
