"""answers.yaml (the user's own file): their answers to application-form questions, matched by normalised question text.
Pure functions and files under tmp_path; no browser, no network."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops.apply import answers

JOB = {"id": "greenhouse:1", "company": "Acme Ltd", "title": "AI Engineer", "source": "greenhouse"}


@pytest.fixture
def book():
    return answers.load(PROFILE_FIX / "answers.yaml")


def ask(book, question, job=JOB):
    return book.resolve(question, job)


# ------------------------------------------------------------------ matching
def test_a_pattern_matches_whole_words_only_so_sex_never_matches_essex(book):
    assert ask(book, "Sex").entry_id == "eo_gender"
    assert ask(book, "What is your sex?").entry_id == "eo_gender"
    assert ask(book, "Which county? e.g. Essex") is None
    assert ask(book, "Sussex or Essex office") is None


def test_matching_ignores_case_and_punctuation(book):
    assert ask(book, "NOTICE   PERIOD (weeks)?").entry_id == "notice_period"
    assert ask(book, "Where are you based, currently?").entry_id == "current_location"     # the pattern's words, in a row
    assert ask(book, "Where do you base yourself?") is None                       # a paraphrase is not a match: no guessing
    assert ask(book, "Current-location").entry_id == "current_location"


def test_the_longest_matching_pattern_wins(book):
    # "current salary" (14 chars) beats nothing else here; "salary expectation" beats a shorter pattern in the same question
    assert ask(book, "Current salary").entry_id == "current_salary"
    assert ask(book, "Your salary expectation and current salary").entry_id == "salary_expectation"
    assert ask(book, "Your website or portfolio").entry_id == "portfolio"


def test_a_question_with_no_pattern_has_no_answer(book):
    assert ask(book, "Describe a project you are proud of") is None
    assert ask(book, "") is None


# ------------------------------------------------------------------ blank, numeric, overrides, placeholders
def test_a_blank_answer_is_unknown_not_an_empty_string_to_type(book):
    r = ask(book, "What is your current salary?")
    assert r.entry_id == "current_salary" and r.text == "" and r.known is False


def test_placeholders_are_filled_per_job(book):
    assert ask(book, "How did you hear about us?").text == "Greenhouse"
    assert ask(book, "Why do you want to work here?").text == "I like Acme Ltd and the AI Engineer role."
    other = {**JOB, "company": "Zeta", "title": "Data Scientist", "source": "reed"}
    assert ask(book, "Why do you want to work here?", other).text == "I like Zeta and the Data Scientist role."
    assert ask(book, "How did you hear about us?", other).text == "Reed"


def test_source_label_names_a_manual_job_by_its_apply_page_not_by_manual():
    assert answers.source_label({"source": "manual", "apply_kind": "ashby"}) == "Ashby"
    assert answers.source_label({"source": "manual"}) == "Company website"
    assert answers.source_label({"source": "gmail_alert"}) == "Job alert email"
    assert answers.source_label({"source": "adzuna"}) == "Adzuna"


def test_a_company_override_beats_the_default_answer_for_that_company_only(book):
    assert ask(book, "Have you previously worked for us?").text == "No"
    northwind = {**JOB, "company": "NORTHWIND  health AI."}
    assert ask(book, "Have you previously worked for us?", northwind).text == "Northwind Health AI (MSc dissertation, indirect contact)"


def test_answer_numeric_is_kept_apart_from_the_prose_answer(book):
    r = ask(book, "Salary expectations")
    assert r.entry_id == "salary_expectation" and r.numeric == "50000" and r.text.startswith("I'd welcome")
    assert ask(book, "Notice period").numeric is None


def test_options_are_the_texts_a_dropdown_choice_may_contain_in_order(book):
    assert ask(book, "Gender").options == ("Female", "Woman")
    assert answers.pick_option(["Prefer not to say", "Woman", "Man"], ask(book, "Gender")) == "Woman"
    assert answers.pick_option(["Prefer not to say", "Man"], ask(book, "Gender")) is None
    assert answers.pick_option(["No", "Yes"], ask(book, "Disability")) == "No"


def test_an_option_is_matched_by_whole_words_so_male_is_never_female_and_man_is_never_woman():
    male = answers.Resolved("g", "gender", "Male", None, ("Male", "Man"))
    assert answers.pick_option(["Female", "Male", "Prefer not to say"], male) == "Male"
    assert answers.pick_option(["Female", "Woman", "Non-binary"], male) is None
    assert answers.pick_option(["Female", "Man (including trans man)"], male) == "Man (including trans man)"
    assert answers.pick_option(["Woman (including trans woman)", "Man"], answers.Resolved("g", "g", "Man", None, ("Man",))) == "Man"


def test_without_options_only_an_option_equal_to_the_answer_is_picked(book):
    r = ask(book, "Are you willing to relocate?")
    assert answers.pick_option(["Yes", "No"], r) == "Yes"
    assert answers.pick_option(["Yes, definitely", "No"], r) is None              # "contains" is only for the user's own `options`
    assert answers.pick_option(["yes"], r) == "yes"
    assert answers.pick_option(["Yes", "No"], ask(book, "Notice period")) is None


# ------------------------------------------------------------------ the file
def test_a_missing_file_is_an_empty_book_so_every_question_is_unknown(tmp_path):
    b = answers.load(tmp_path / "nope.yaml")
    assert b.entries == [] and b.resolve("Notice period", JOB) is None


def test_a_broken_file_is_refused_with_its_name(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("answers: [unclosed", encoding="utf-8")
    with pytest.raises(answers.AnswersError, match="answers.yaml"):
        answers.load(p)
    p.write_text("answers:\n  - id: x\n    match: 'not a list'\n    answer: a\n", encoding="utf-8")
    with pytest.raises(answers.AnswersError, match="match"):
        answers.load(p)


def test_the_singular_file_name_is_found_when_the_plural_does_not_exist(tmp_path, settings):
    settings.raw["paths"]["answers"] = str(tmp_path / "answers.yaml")
    (tmp_path / "answer.yaml").write_text("answers: []\n", encoding="utf-8")
    assert answers.path_for(settings) == tmp_path / "answer.yaml"
    (tmp_path / "answers.yaml").write_text("answers: []\n", encoding="utf-8")
    assert answers.path_for(settings) == tmp_path / "answers.yaml"


# ------------------------------------------------------------------ save
def test_save_appends_one_entry_and_leaves_the_rest_of_the_file_byte_for_byte(tmp_path):
    p = tmp_path / "answers.yaml"
    original = (PROFILE_FIX / "answers.yaml").read_text(encoding="utf-8") + "\n# NEVER put here: free text\n"
    p.write_text(original, encoding="utf-8", newline="\n")
    answers.save(p, "Do you hold a full UK driving licence?", "Yes")
    text = p.read_text(encoding="utf-8")
    rest = iter(text.splitlines())
    assert all(line in rest for line in original.splitlines())          # every line of the user's file, unchanged and in order (the entry joins the list
    assert len(text.splitlines()) - len(original.splitlines()) <= 6      # before `motivations:`, where other top-level keys follow `answers:`)
    b = answers.load(p)
    r = b.resolve("Do you hold a full UK driving licence?", JOB)
    assert r.text == "Yes" and r.entry_id.startswith("saved_")
    assert yaml.safe_load(text)["answers"][-1]["match"] == ["do you hold a full uk driving licence"]


def test_save_follows_the_indent_of_the_list_the_file_already_has(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text(yaml.safe_dump(yaml.safe_load((PROFILE_FIX / "answers.yaml").read_text(encoding="utf-8"))), encoding="utf-8", newline="\n")
    assert "\n- answer:" in p.read_text(encoding="utf-8") or "\n- id:" in p.read_text(encoding="utf-8")           # PyYAML does not indent the items
    n = len(answers.load(p).entries)
    answers.save(p, "Do you have a clean driving licence?", "Yes")
    b = answers.load(p)
    assert len(b.entries) == n + 1 and b.resolve("Do you have a clean driving licence?", JOB).text == "Yes"
    assert b.entries[-1].patterns == ("do you have a clean driving licence",)


def test_a_saved_answer_beats_a_blank_entry_that_matched_a_shorter_pattern(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text((PROFILE_FIX / "answers.yaml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    q = "I agree to the privacy notice and consent to being contacted"
    assert answers.load(p).resolve(q, JOB).known is False
    answers.save(p, q, "Yes")
    assert answers.load(p).resolve(q, JOB).text == "Yes"


def test_save_quotes_anything_the_user_typed(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("answers: []\n", encoding="utf-8", newline="\n")
    answers.save(p, 'Why "us": really?', 'He said: "yes" # not a comment\nsecond line')
    got = answers.load(p).entries[0]
    assert got.answer == 'He said: "yes" # not a comment\nsecond line'


def test_save_refuses_a_blank_answer_and_a_blank_question(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text("answers: []\n", encoding="utf-8", newline="\n")
    with pytest.raises(answers.AnswersError):
        answers.save(p, "Question?", "   ")
    with pytest.raises(answers.AnswersError):
        answers.save(p, "  ?! ", "Yes")
    assert p.read_text(encoding="utf-8") == "answers: []\n"


def test_save_puts_the_entry_at_the_end_of_the_answers_list_when_other_top_level_keys_follow_it(tmp_path):
    """The user's file has `motivations:` and `style:` after `answers:`: a new entry must join the list, not land inside `style`."""
    f = tmp_path / "answers.yaml"
    f.write_text("answers:\n  - id: a\n    match: [\"alpha\"]\n    answer: \"one\"\n\n# Reasons in your own words.\nmotivations:\n  - \"I like it.\"\n\n"
                 "style:\n  avoid: [passionate]\n  person: first\n", encoding="utf-8")
    before = f.read_text(encoding="utf-8")
    entry = answers.save(f, "Do you hold a full UK driving licence?", "Yes")
    after = f.read_text(encoding="utf-8")
    raw = yaml.safe_load(after)
    assert [e["id"] for e in raw["answers"]] == ["a", entry] and raw["answers"][1]["answer"] == "Yes"
    assert raw["motivations"] == ["I like it."] and raw["style"] == {"avoid": ["passionate"], "person": "first"}
    assert after.index(entry) < after.index("# Reasons in your own words.")           # before the comment that belongs to `motivations`
    assert after.replace(after[after.index("\n  # saved"):after.index("# Reasons")], "\n") .startswith(before[:before.index("# Reasons")].rstrip("\n"))
    assert answers.load(f).motivations == ["I like it."] and answers.load(f).style["person"] == "first"
