"""`careerops doctor` before the first live run (Document rules, C): profile sentences enough for 100 words in each family, no skill repeats in the
fact bank (tests/tailoring/test_skill_repeats.py), and every project with `github:` has `public:` set. Read only: it never edits the fact bank.
Fact banks are tmp files; no network."""
import inspect

import yaml

from careerops import setup


def write(tmp_path, fb) -> "Path":
    path = tmp_path / "fact_bank.yaml"
    path.write_text(yaml.safe_dump(fb, allow_unicode=True), encoding="utf-8")
    return path


def words(n: int, tag: str = "w") -> str:
    return " ".join([tag.capitalize(), *(f"{tag}{i}" for i in range(2, n + 1))]) + "."


def bank(body_words=60, sentences=(), facts=(), deny=()):
    return {"deny_terms": list(deny), "profile": {"sentences": list(sentences)}, "profile_facts": list(facts),
            "families": [{"id": "ai_engineer", "match": ["ai"], "profile_body": words(body_words, "b"), "skill_lines": [{"label": "A", "items": "Python"}]},
                         {"id": "data_scientist", "match": ["ds"], "profile_body": words(body_words + 30, "d"), "skill_lines": [{"label": "A", "items": "R"}]}]}


def test_each_family_needs_enough_words_for_a_100_word_profile(tmp_path):
    rows = setup.profile_word_checks(write(tmp_path, bank(60, sentences=[words(20, "s")])))
    assert [(ok, name) for name, ok, _ in rows] == [
        (False, "profile words for family ai_engineer: 80 of 100 available (add profile sentences to fact_bank.yaml → profile.sentences)"),
        (True, "profile words for family data_scientist: 110 of 100 available")]
    assert "profile.sentences" in rows[0][2]


def test_both_profile_sentences_and_the_older_profile_facts_count(tmp_path):
    rows = setup.profile_word_checks(write(tmp_path, bank(60, sentences=[words(20, "s")], facts=[words(25, "f")])))
    assert all(ok for _, ok, _ in rows) and "105 of 100" in rows[0][0]


def test_a_deny_listed_sentence_does_not_count(tmp_path):
    rows = setup.profile_word_checks(write(tmp_path, bank(60, sentences=["Runs kubernetes clusters every day " + words(40, "x")], deny=["kubernetes"])))
    assert rows[0][1] is False and "60 of 100" in rows[0][0]


def test_a_bank_without_families_is_judged_on_its_sentences_alone(tmp_path):
    ok = setup.profile_word_checks(write(tmp_path, {"deny_terms": [], "profile": {"sentences": [words(100, "s")]}}))
    assert ok == [("profile sentences: 100 of 100 words available", True, "")]
    short = setup.profile_word_checks(write(tmp_path, {"deny_terms": [], "profile_facts": [words(40, "s")]}))
    assert short[0][1] is False and "40 of 100" in short[0][0]


def test_a_missing_fact_bank_is_a_failure_not_a_crash(tmp_path):
    assert setup.profile_word_checks(tmp_path / "nope.yaml")[0][1] is False


# ------------------------------------------------------------------ projects: public or not
def projects_bank(*projects):
    return {"deny_terms": [], "projects": [{"id": p[0], "name": p[1], **p[2], "bullets": [{"id": p[0] + "b", "text": "x"}]} for p in projects]}


def test_every_project_with_a_github_url_needs_public_set(tmp_path):
    path = write(tmp_path, projects_bank(("a", "Alpha", {"github": "https://github.com/u/a"}),
                                         ("b", "Beta", {"github": "https://github.com/u/b", "public": True}),
                                         ("c", "Gamma", {"github": "https://github.com/u/c", "public": False}),
                                         ("d", "Delta", {})))
    rows = setup.project_public_checks(path)
    assert rows == [("project 'Alpha' has github: but no public: true/false (treated as public, its URL is printed)", setup.WARN,
                     "add `public: true` or `public: false` to the project in fact_bank.yaml (the pipeline never edits it)")]


def test_a_public_value_that_is_not_true_or_false_is_reported(tmp_path):
    rows = setup.project_public_checks(write(tmp_path, projects_bank(("a", "Alpha", {"github": "https://github.com/u/a", "public": "maybe"}))))
    assert rows[0][1] == setup.WARN and "public: 'maybe' is neither true nor false" in rows[0][0]


def test_all_set_is_one_passing_line(tmp_path):
    rows = setup.project_public_checks(write(tmp_path, projects_bank(("b", "Beta", {"github": "https://github.com/u/b", "public": True}))))
    assert rows == [("every project with a github link says whether it is public (1)", True, "")]


def test_the_checks_are_part_of_doctor_and_never_write_the_fact_bank(tmp_path):
    src = inspect.getsource(setup.doctor)
    assert "profile_word_checks" in src and "project_public_checks" in src and "skill_repeat_checks" in src
    path = write(tmp_path, bank())
    before = path.read_bytes()
    setup.profile_word_checks(path)
    setup.project_public_checks(path)
    setup.skill_repeat_checks(path)
    assert path.read_bytes() == before
