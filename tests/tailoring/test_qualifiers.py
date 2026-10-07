"""Deleting unsupported qualifier words from a letter draft: which problems count, and that the edit is removal only."""
import pytest

from careerops.tailoring.qualifiers import (QUALIFIERS, is_qualifier, only_removed, qualifier_only, remove_qualifiers,
                                            repair)

CLAIM = "'{w}' (claim) is not in the two evidence bullets: 'Acme mirrors my {w} work'"
FILLER = "vague filler '{w}' is not in the evidence"


def test_the_list_has_the_words_from_the_monzo_run_and_the_ones_the_brief_names():
    for w in ("specialised", "integrated", "extensive", "deep", "proven", "seamless", "robust", "strong", "solid"):
        assert w in QUALIFIERS and is_qualifier(w.upper())


# ---------------------------------------------------------------- which problems are qualifier-only
def test_claim_and_filler_problems_naming_qualifiers_are_repairable_and_deduped():
    assert qualifier_only([CLAIM.format(w="specialised")]) == ["specialised"]
    assert qualifier_only([CLAIM.format(w="robust"), FILLER.format(w="robust"), CLAIM.format(w="integrated")]) == [
        "robust", "integrated"]


@pytest.mark.parametrize("other", [
    "'Spark' (name) is in neither the evidence nor the job description: 'I used Spark'",       # a tool
    "'production' (capability) is in neither the evidence nor the job description: 'x'",       # a capability claim
    "'fintech' (sector) is in neither the evidence nor the job description: 'x'",
    "'client' (client) is in neither the evidence nor the job description: 'x'",
    "'pipelines' (claim) is not in the two evidence bullets: 'x'",                              # a noun
    "'wonderful' (claim) is not in the two evidence bullets: 'x'",                              # not on the list: unknown word
    "deny-listed 'kubernetes'", "new numbers ['40']", "300 words > 250", "banned opener",
    "claims a gap not in GAPS: 'I have not used Go'", "last paragraph is not a call-to-action question",
])
def test_any_other_kind_of_violation_makes_the_draft_unrepairable(other):
    assert qualifier_only([CLAIM.format(w="specialised"), other]) is None
    assert qualifier_only([other]) is None


def test_a_name_or_capability_problem_is_never_a_qualifier_even_when_the_word_is_on_the_list():
    assert qualifier_only(["'robust' (capability) is in neither the evidence nor the job description: 'x'"]) is None
    assert qualifier_only(["'Robust' (name) is in neither the evidence nor the job description: 'x'"]) is None


def test_no_problems_is_nothing_to_repair():
    assert qualifier_only([]) is None


# ---------------------------------------------------------------- removal tidies what is around the word
@pytest.mark.parametrize("text, words, want", [
    ("My specialised work building NLP workflows.", ["specialised"], "My work building NLP workflows."),
    ("It mirrors my integrated work.", ["integrated"], "It mirrors my work."),
    ("I built a robust and scalable pipeline.", ["robust"], "I built a scalable pipeline."),
    ("I built a robust, scalable pipeline.", ["robust"], "I built a scalable pipeline."),
    ("I built a scalable and robust pipeline.", ["robust"], "I built a scalable pipeline."),
    ("I built a scalable, robust pipeline.", ["robust"], "I built a scalable pipeline."),
    ("I built a fast, robust, tested pipeline.", ["robust"], "I built a fast, tested pipeline."),
    ("I built an extensive test suite.", ["extensive"], "I built a test suite."),          # an -> a
    ("I wrote a strong SQL layer.", ["strong"], "I wrote an SQL layer."),                   # a -> an before an acronym
    ("I wrote a solid R package.", ["solid"], "I wrote an R package."),
    ("I wrote a proven approach.", ["proven"], "I wrote an approach."),
    ("An integrated tool I wrote.", ["integrated"], "A tool I wrote."),                    # capital kept
    ("Seamlessly, I shipped the agent.", ["seamlessly"], "I shipped the agent."),           # sentence-initial adverb
    ("Robust pipelines were my focus.", ["robust"], "Pipelines were my focus."),            # next word capitalised
    ("It ran. Proven methods helped.", ["proven"], "It ran. Methods helped."),
    ("I deployed it seamlessly.", ["seamlessly"], "I deployed it."),                        # no space before the full stop
    ("The result was very solid and fast.", ["solid"], "The result was very fast."),
])
def test_the_word_goes_and_the_text_around_it_is_tidied(text, words, want):
    assert remove_qualifiers([text], words) == [want]


def test_every_occurrence_in_every_paragraph_goes_and_two_words_can_go_together():
    body = ["My specialised work and an integrated tool.", "Specialised again, with integrated parts."]
    assert remove_qualifiers(body, ["specialised", "integrated"]) == ["My work and a tool.", "Again, with parts."]


# ---------------------------------------------------------------- when it must not touch the draft
@pytest.mark.parametrize("text, word", [
    ("I built deep learning models.", "deep"),                  # the word is part of a skill name
    ("The pipeline is robust.", "robust"),                      # after a copula: deleting it breaks the sentence
    ("I built a pipeline.", "robust"),                          # the word is not in the body (it is in the closing)
    ("I built a robust.", "robust"),                            # leaves a dangling article
    ("Robust.", "robust"),                                      # leaves nothing
])
def test_unsafe_removals_return_none(text, word):
    assert remove_qualifiers([text], [word]) is None


def test_only_removed_accepts_deletions_and_a_an_swaps_and_nothing_else():
    assert only_removed(["I built a robust and scalable pipeline."], ["I built a scalable pipeline."])
    assert only_removed(["I wrote an extensive test suite."], ["I wrote a test suite."])
    assert not only_removed(["I wrote a test suite."], ["I wrote a test suite and docs."])          # an insertion
    assert not only_removed(["I wrote tests first."], ["First I wrote tests."])                       # a reorder
    assert not only_removed(["I wrote tests."], ["I wrote tests.", "More."])                          # a paragraph more


# ---------------------------------------------------------------- repair: the recheck decides
def test_repair_accepts_only_what_the_full_recheck_passes():
    body = ["My specialised work."]
    problems = [CLAIM.format(w="specialised")]
    got = repair(body, problems, lambda b: [] if "specialised" not in " ".join(b) else ["still bad"])
    assert got == (["My work."], ["specialised"])
    assert repair(body, problems, lambda b: ["a second problem appears"]) is None
    assert repair(body, [*problems, "'Spark' (name) is in neither the evidence nor the job description: 'x'"],
                  lambda b: []) is None
