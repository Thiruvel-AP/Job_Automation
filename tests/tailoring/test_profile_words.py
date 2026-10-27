"""The CV profile is 100 to 150 words (Document rules, A2). It is assembled in code from the user's own text, in this order: the hook's
`profile_line`, the family's `profile_body`, then further fact-bank profile sentences (`profile.sentences`; the older `profile_facts` are read
too), best keyword overlap first, stopping before 150 words. Under 100 words the check fails and says how many sentences are missing; no model
writes profile text. No network, no model."""
import re

import pytest

from careerops.tailoring import profile as P
from careerops.tailoring.scorer import Keyword

KWS = [Keyword("Python", 3, True), Keyword("SQL", 3, True), Keyword("Docker", 2, True)]
JD = "We need Python, SQL and Docker."


def words(n: int, tag: str = "w") -> str:
    """One sentence of exactly n words (the first is a capitalised word, the last carries the full stop)."""
    body = [f"{tag}{i}" for i in range(1, n + 1)]
    body[0] = body[0].capitalize()
    return " ".join(body) + "."


def count(text: str) -> int:
    return len(text.split())


def bank(sentences, key="sentences"):
    fb = {"deny_terms": [], "identity": {"headline": "Data Scientist"}}
    if key == "sentences":
        fb["profile"] = {"sentences": list(sentences)}
    else:
        fb["profile_facts"] = list(sentences)
    return fb


def test_the_order_is_the_hook_line_then_the_family_body_then_further_sentences():
    hook, body = words(30, "h"), words(40, "b")
    extra = [words(20, "x"), words(20, "y")]
    out = P.assemble(bank(extra), KWS, JD, hook, family_body=body, words=(100, 150))
    assert out["text"].startswith(hook + " " + body + " ")
    assert out["text"] == " ".join([hook, body, *out["facts"]])
    assert count(out["text"]) == 30 + 40 + 20 + 20 and out["words"] == 110


def test_further_sentences_are_chosen_by_keyword_overlap_best_first():
    hook, body = words(30, "h"), words(40, "b")
    plain = "Enjoys long walks and quiet weekends in the hills near the city of Bristol every year without fail."
    strong = "Builds Python and SQL pipelines in Docker for every project that the team ships to production each quarter."
    out = P.assemble(bank([plain, strong]), KWS, JD, hook, family_body=body, words=(100, 150))
    assert out["facts"][0] == strong


def test_it_stops_adding_once_the_profile_has_100_words():
    hook, body = words(50, "h"), words(55, "b")                      # 105 words already
    out = P.assemble(bank([words(10, "x")]), KWS, JD, hook, family_body=body, words=(100, 150))
    assert out["facts"] == [] and out["words"] == 105


def test_it_never_goes_over_150_words_a_sentence_that_would_is_skipped_for_a_shorter_one():
    hook, body = words(40, "h"), words(50, "b")                      # 90 words
    out = P.assemble(bank([words(70, "long"), words(15, "short")]), KWS, JD, hook, family_body=body, words=(100, 150))
    assert out["facts"] == [words(15, "short")] and out["words"] == 105


def test_100_words_passes_and_150_passes():
    for n in (100, 150):
        out = P.assemble(bank([]), KWS, JD, words(n), words=(100, 150))
        assert out["words"] == n and out["shortfall"] == "" and P.word_problems(out["text"], (100, 150)) == []


def test_99_words_fails_with_the_shortfall_and_where_to_fix_it():
    out = P.assemble(bank([]), KWS, JD, words(99), words=(100, 150))
    assert out["words"] == 99
    assert out["shortfall"] == "PROFILE 99/100 words: add profile sentences to fact_bank.yaml → profile.sentences"
    assert P.word_problems(out["text"], (100, 150)) == [out["shortfall"]]


def test_the_84_word_profile_of_21_oct_names_its_shortfall():
    assert P.word_problems(words(84), (100, 150)) == ["PROFILE 84/100 words: add profile sentences to fact_bank.yaml → profile.sentences"]


def test_151_words_are_trimmed_at_a_sentence_boundary():
    hook, body = words(40, "h"), words(60, "b")
    tail = [words(30, "t"), words(21, "u")]                           # 40 + 60 + 30 + 21 = 151
    text = " ".join([hook, body, *tail])
    assert count(text) == 151
    trimmed = P.trim(text, 150)
    assert trimmed == " ".join([hook, body, tail[0]]) and count(trimmed) == 130 and trimmed.endswith(".")
    out = P.assemble(bank([]), KWS, JD, hook, family_body=" ".join([body, *tail]), words=(100, 150))
    assert out["words"] <= 150 and out["text"].endswith(".") and re.search(r"u\d+\.$", out["text"]) is None


def test_trim_leaves_a_profile_within_the_limit_alone_and_never_drops_the_first_sentence():
    ok = " ".join([words(30), words(30)])
    assert P.trim(ok, 150) == ok
    only = words(160)
    assert P.trim(only, 150) == only                                  # one sentence: nothing to cut at; the check will report it
    assert P.word_problems(only, (100, 150)) == ["PROFILE 160/150 words: the profile is over the limit and has no sentence to cut at"]


def test_the_older_profile_facts_key_is_read_when_there_is_no_profile_sentences():
    out = P.assemble(bank([words(20, "x")], key="facts"), KWS, JD, words(90, "h"), words=(100, 150))
    assert out["words"] == 110 and out["facts"] == [words(20, "x")]


def test_profile_sentences_come_before_profile_facts_and_both_are_listed():
    fb = {"deny_terms": [], "profile": {"sentences": [words(12, "a")]}, "profile_facts": [words(12, "b")]}
    assert P.bank_sentences(fb) == [words(12, "a"), words(12, "b")]
    assert P.bank_sentences({"deny_terms": []}) == []


def test_a_deny_listed_sentence_is_never_used_and_never_invented_to_fill_the_gap():
    fb = bank(["Runs kubernetes clusters for fun and has done for many years now across every team they joined."])
    fb["deny_terms"] = ["kubernetes"]
    out = P.assemble(fb, KWS, JD, words(60, "h"), words=(100, 150))
    assert out["facts"] == [] and out["words"] == 60 and out["shortfall"].startswith("PROFILE 60/100 words")


def test_without_a_range_the_older_one_or_two_facts_rule_stands():
    out = P.assemble(bank([words(10, "x"), words(10, "y"), words(10, "z")]), KWS, JD, words(30, "h"))
    assert len(out["facts"]) == 2 and "shortfall" in out


@pytest.mark.parametrize("raw,ok", [([100, 150], True), ([120, 120], True), ([90, 150], False), ([100, 151], False), ([150, 100], False), ("x", False)])
def test_the_range_setting_is_checked(raw, ok):
    if ok:
        assert P.word_range({"scoring": {"profile_words": raw}}) == tuple(raw)
    else:
        with pytest.raises(ValueError, match="profile_words"):
            P.word_range({"scoring": {"profile_words": raw}})
    assert P.word_range({"scoring": {}}) == (100, 150)
