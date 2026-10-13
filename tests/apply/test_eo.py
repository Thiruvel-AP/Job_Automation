"""Equal-opportunities answers, the parts that need no browser (apply/eo.py): matching ignoring case and dashes, the order of match_any, the
structure of the applicant.yaml block and what doctor says about it. The form tests are in test_assist_eo.py (Chromium)."""
import copy

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops.apply import eo
from careerops.setup import applicant_checks

MATCH = ["Asian or Asian British - Indian", "Asian, Asian British or Asian Welsh: Indian", "Indian", "Asian"]
ENTRY = eo.Entry("ethnicity", "Asian - Indian", tuple(MATCH), tuple(eo.re.compile(p, eo.re.I) for p in eo.BUILTIN_QUESTIONS["ethnicity"]))

UK_STYLE = ["Please select", "White - English, Welsh, Scottish, Northern Irish or British", "White - Irish", "Mixed - White and Asian",
            "Asian or Asian British - Indian", "Asian or Asian British - Pakistani", "Black or Black British - African", "Prefer not to say"]
US_STYLE = ["Hispanic or Latino", "White", "Black or African American", "American Indian or Alaska Native", "Asian",
            "Native Hawaiian or Other Pacific Islander", "Two or More Races"]


@pytest.fixture
def applicant():
    return yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ choosing the option
def test_a_uk_style_list_picks_the_asian_indian_option():
    assert eo.choose(UK_STYLE, ENTRY) == "Asian or Asian British - Indian"


def test_a_us_style_list_picks_asian_and_never_american_indian():
    assert eo.choose(US_STYLE, ENTRY) == "Asian"                      # "Indian" must equal an option; "American Indian or Alaska Native" does not


def test_a_list_with_no_matching_option_picks_nothing():
    assert eo.choose(["White", "Black", "Mixed", "Other", "Prefer not to say"], ENTRY) is None
    assert eo.choose([], ENTRY) is None and eo.choose(["", "  "], ENTRY) is None


@pytest.mark.parametrize("option", ["asian or asian british - indian", "ASIAN OR ASIAN BRITISH – INDIAN", "Asian or Asian British — Indian",
                                    "Asian or Asian British-Indian", "Asian or Asian British  -  Indian ", "Asian or Asian British – Indian",
                                    "Asian or Asian British − Indian"])
def test_matching_ignores_case_dashes_and_spacing(option):
    assert eo.choose(["White", option], ENTRY) == option


def test_the_first_match_any_entry_wins_over_form_order():
    options = ["Asian", "Indian", "Asian or Asian British - Indian"]
    assert eo.choose(options, ENTRY) == "Asian or Asian British - Indian"          # the best entry, though it is last on the form
    assert eo.choose(["Asian", "Indian"], ENTRY) == "Indian"


def test_the_first_option_in_form_order_wins_among_equal_ones():
    assert eo.choose(["Asian - Indian x", "Asian", "ASIAN"], ENTRY) == "Asian"


def test_only_a_whole_option_matches_never_part_of_one():
    assert eo.choose(["Asian British", "Indian Ocean Islander", "Asian or Asian British - Indian or other"], ENTRY) is None


def test_the_answer_line_is_for_you_and_is_not_matched_unless_it_is_in_match_any():
    assert eo.choose(["Asian - Indian"], ENTRY) is None                               # `answer` is not a match_any entry


# ------------------------------------------------------------------ which questions are about equal opportunities
@pytest.mark.parametrize("text", ["What is your ethnicity?", "Ethnic origin", "Race", "Gender", "Please tell us your sex", "Do you have a disability?",
                                  "Veteran status", "Preferred pronouns", "Sexual orientation", "Religion or belief", "Are you transgender?"])
def test_these_are_equal_opportunities_questions(text):
    assert eo.is_eo_field(text)


@pytest.mark.parametrize("text", ["First name", "Salary expectations", "Nationality", "Do you require sponsorship?", "Why do you want to work here?",
                                  "Brace yourself", "Have you ever been convicted?", "Essex office"])
def test_these_are_not(text):
    assert not eo.is_eo_field(text)


def test_only_a_listed_question_has_an_entry(applicant):
    listed = eo.entries(applicant)
    assert [e.key for e in listed] == ["ethnicity"]
    assert eo.entry_for("What is your ethnicity?", listed).key == "ethnicity" and eo.entry_for("Race", listed).key == "ethnicity"
    assert eo.entry_for("Gender", listed) is None and eo.entry_for("Disability", listed) is None
    assert eo.entries({}) == [] and eo.entries(None) == [] and eo.entries({"equal_opportunities": None}) == []


def test_another_question_is_answered_only_when_you_list_it_with_its_label_regex(applicant):
    applicant["equal_opportunities"]["gender"] = {"answer": "x", "match_any": ["Prefer not to say"], "question": ["gender"]}
    listed = eo.entries(applicant)
    assert [e.key for e in listed] == ["ethnicity", "gender"]
    assert eo.entry_for("Gender identity", listed).key == "gender"
    assert eo.choose(["Man", "Woman", "Prefer not to say"], listed[1]) == "Prefer not to say"


# ------------------------------------------------------------------ the structure of the block
def test_the_shipped_shape_is_valid(applicant):
    assert eo.problems(applicant["equal_opportunities"]) == []


@pytest.mark.parametrize("block, fragment", [
    (None, "is empty"),
    (["ethnicity"], "must be a mapping"),
    ({"ethnicity": "Asian"}, "equal_opportunities.ethnicity must be a mapping"),
    ({"ethnicity": {"answer": "Asian"}}, "match_any must be a non-empty list"),
    ({"ethnicity": {"match_any": []}}, "match_any must be a non-empty list"),
    ({"ethnicity": {"match_any": "Asian"}}, "match_any must be a non-empty list"),
    ({"ethnicity": {"match_any": ["Asian", ""]}}, "match_any must be a non-empty list"),
    ({"ethnicity": {"match_any": ["Asian", 3]}}, "match_any must be a non-empty list"),
    ({"ethnicity": {"match_any": ["Asian"], "answer": ""}}, "answer must be a non-empty text"),
    ({"ethnicity": {"match_any": ["Asian"], "answr": "x"}}, "unknown key 'answr'"),
    ({"ethnicity": {"match_any": ["Asian"], "question": "ethnic"}}, "question must be a list of regular expressions"),
    ({"ethnicity": {"match_any": ["Asian"], "question": ["(unclosed"]}}, "'(unclosed'"),
    ({"gender": {"match_any": ["Man"]}}, "no question to recognise it by"),
])
def test_a_block_with_a_mistake_says_which(block, fragment):
    found = eo.problems(block)
    assert found and any(fragment in p for p in found), found


def test_every_problem_is_listed_and_an_invalid_entry_is_never_used_to_fill(applicant):
    applicant["equal_opportunities"]["gender"] = {"answer": "x"}
    applicant["equal_opportunities"]["ethnicity"]["answr"] = "typo"
    assert len(eo.problems(applicant["equal_opportunities"])) == 3                    # gender: no match_any, no question; ethnicity: typo
    assert eo.entries(applicant) == []                                                # neither is used


# ------------------------------------------------------------------ doctor
def checks(tmp_path, applicant):
    p = tmp_path / "applicant.yaml"
    p.write_text(yaml.safe_dump(applicant), encoding="utf-8")
    return [c for c in applicant_checks(p) if "equal_opportunities" in c[0]]


def test_doctor_passes_a_valid_block_and_says_how_many_questions(tmp_path, applicant):
    ((name, ok, _),) = checks(tmp_path, applicant)
    assert ok is True and name == "applicant equal_opportunities is well formed (1 question(s))"


def test_doctor_has_nothing_to_say_when_there_is_no_block(tmp_path, applicant):
    del applicant["equal_opportunities"]
    assert checks(tmp_path, applicant) == []


def test_doctor_fails_a_malformed_block_and_lists_every_problem(tmp_path, applicant):
    bad = copy.deepcopy(applicant)
    bad["equal_opportunities"] = {"ethnicity": {"answer": "Asian"}, "gender": {"match_any": ["Man"]}}
    ((name, ok, hint),) = checks(tmp_path, bad)
    assert ok is False and name == "applicant equal_opportunities is well formed"
    assert "ethnicity.match_any" in hint and "gender: no question" in hint
    bad["equal_opportunities"] = "yes please"
    ((_, ok, hint),) = checks(tmp_path, bad)
    assert ok is False and "must be a mapping" in hint
    bad["equal_opportunities"] = None
    assert checks(tmp_path, bad)[0][1] is False                                      # a key with nothing under it
