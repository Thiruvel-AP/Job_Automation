"""apply/option_match.py, a port of career-ops-hq's pickComboboxOption (option-match.mjs @ 20b9fd3) with its own tests, plus the addendum's cases.
An exact option wins; otherwise a whole-word match only when exactly one option has it; anything else is left for the person."""
from careerops.apply.option_match import choose, pick


def test_an_exact_option_wins_ignoring_case_and_surrounding_space():
    assert pick(["Female", " male "], "Male") == 1


def test_male_never_picks_female():
    assert pick(["Male", "Female"], "Male") == 0
    assert pick(["Female"], "Male") == -1


def test_us_never_picks_australia():
    assert pick(["Australia", "United States"], "US") == -1
    assert pick(["Australia", "Belarus", "Cyprus"], "US") == -1


def test_a_whole_word_partial_match_is_taken_when_it_is_the_only_one():
    assert pick(["Canada", "United States (US)"], "US") == 1
    assert pick(["São Paulo, Brasil", "Lisboa"], "são paulo") == 0


def test_an_ambiguous_whole_word_match_is_left_for_the_person():
    assert pick(["Yes, I need sponsorship", "Yes, I do not need sponsorship"], "Yes") == -1


def test_regex_characters_in_the_answer_are_literal():
    assert pick(["C++ (expert)", "C (expert)"], "C++") == 0
    assert pick(["a.b"], ".") == -1


def test_an_empty_menu_or_an_empty_answer_picks_nothing():
    assert pick([], "Yes") == -1
    assert pick(["Yes"], "  ") == -1
    assert choose(["Work Visa", "Citizen"], "work visa") == "Work Visa"
