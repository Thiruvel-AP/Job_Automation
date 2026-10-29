"""The letter's opening, P1 (Document rules, B4 and v2.3 B3): the company's problem first, quoted from the job ad, else from the company's own careers
page (never news, LinkedIn or a third party), a semicolon, then the hook's `letter_line` fact (it must carry a number from the fact bank). A hook
whose line has no number cannot open a letter: the next hook is used, else the strongest numbered bullet of the CV with the role family's profile
line. A banned opening is rejected. No network, no model."""
import copy
import dataclasses
import re

import pytest

from careerops import hook as H
from careerops.tailoring.faithfulness import numbers
from careerops.tailoring import opening as O
from test_company_letter import FAMILY_LINE, JD, JOB, LIBRARY, world  # noqa: F401  (world is a fixture)


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.casefold()).split())


def quoted(sentence: str) -> str:
    return re.findall(r'"([^"]+)"', sentence)[-1]


def numberless(world):
    """The CV's hook as h_forecast, whose letter line has no number."""
    entry = next(e for e in LIBRARY if e["id"] == "h_forecast")
    return dataclasses.replace(world.hk, hook_id="h_forecast", letter_sentence=entry["letter_line"], proof_bullet_id="p2a", entry_id="p2",
                               entry_kind="project", number="")


# ------------------------------------------------------------------ the fact and the problem
def test_the_opening_is_the_hooks_fact_plus_a_phrase_of_the_ad(world):
    op = O.build(JOB, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE)
    assert op.ok and op.source == "ad" and op.hook_id == world.hk.hook_id and op.fact_kind == "hook"
    assert op.sentence.startswith(f'Anaplan is hiring for "{op.problem}"; ') and op.sentence.endswith(O.lower_first(world.hk.letter_sentence.rstrip(".")) + ".")
    assert O.bank_numbers(op.sentence, world.fb) == ["65"] and op.number == "65"
    assert norm(quoted(op.sentence)) in norm(JD) and op.problem == quoted(op.sentence)
    assert 3 <= len(op.problem.split()) <= O.PHRASE_MAX_WORDS
    assert op.paragraph == op.sentence and O.problems(op.paragraph, world.fb, [JD]) == []


def test_the_phrase_is_a_clause_of_an_ad_sentence_that_says_what_the_team_is_for(world):
    op = O.build(JOB, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE)
    assert O.CUES.search(next(s for s in O.ad_sentences(JD) if norm(op.problem) in norm(s)))


def test_the_hooks_line_continues_after_the_semicolon_with_a_lower_case_start_only_for_a_sentence_opening_word():
    assert O.lower_first("At Northwind I shipped it.") == "at Northwind I shipped it."
    assert O.lower_first("I trained per-gene models.") == "I trained per-gene models."
    assert O.lower_first("Built a model with 65 tests.") == "Built a model with 65 tests."
    assert O.lower_first("Across 14 datasets I did it.") == "across 14 datasets I did it."


def test_the_company_page_lead_names_the_page_not_the_ad(world):
    page = "Acme Health builds software that helps hospitals schedule operating theatres without double booking."
    op = O.build({**JOB, "description": "Apply now."}, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE, page_text=page)
    assert op.sentence.startswith(f'Anaplan describes its work as "{op.problem}"; ')


def test_a_phrase_with_a_deny_listed_word_is_never_used(world):
    jd = "We are hiring a Data Scientist to run production pipelines on Snowflake. The team ships Python models to AWS Lambda behind tests."
    fb = copy.deepcopy(world.fb)
    fb["deny_terms"] = [*fb.get("deny_terms", []), "snowflake"]
    op = O.build({**JOB, "description": jd}, fb, world.kws, world.hk, world.chosen, FAMILY_LINE)
    assert op.ok and "snowflake" not in op.sentence.lower()


def test_the_ads_own_phrase_is_preferred_to_the_company_page(world):
    op = O.build(JOB, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE, page_text="Anaplan helps planners decide faster every quarter.")
    assert op.source == "ad"


def test_the_company_page_is_used_when_the_ad_has_nothing_to_quote(world):
    page = "Acme Health builds software that helps hospitals schedule operating theatres without double booking."
    op = O.build({**JOB, "description": "Apply now."}, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE, page_text=page)
    assert op.ok and op.source == "company_page" and norm(op.problem) in norm(page)
    assert O.problems(op.paragraph, world.fb, ["Apply now.", page]) == []


def test_no_phrase_in_the_ad_or_the_page_is_an_error_not_an_invented_phrase(world):
    op = O.build({**JOB, "description": "Apply now."}, world.fb, world.kws, world.hk, world.chosen, FAMILY_LINE, page_text="")
    assert not op.ok and "no problem phrase" in op.error and "company page" in op.error


# ------------------------------------------------------------------ a hook without a number cannot open a letter
def test_a_numberless_hook_is_skipped_for_the_next_hook_with_a_number(world):
    op = O.build(JOB, world.fb, world.kws, numberless(world), world.chosen, FAMILY_LINE)
    assert op.ok and op.hook_id == "h_prod_tests" and O.bank_numbers(op.sentence, world.fb) == ["65"]


def test_a_next_hook_whose_proof_bullet_is_not_on_the_cv_is_not_used(world):
    off_cv = world.chosen - {"a4"}
    op = O.build(JOB, world.fb, world.kws, numberless(world), off_cv, FAMILY_LINE)
    assert op.fact_kind == "bullet" and op.hook_id == ""


def test_with_no_numbered_hook_the_strongest_numbered_cv_bullet_then_the_family_line(world):
    fb = copy.deepcopy(world.fb)
    fb["hook_library"] = [e for e in fb["hook_library"] if e["id"] != "h_prod_tests"]
    op = O.build(JOB, fb, world.kws, numberless(world), world.chosen, FAMILY_LINE)
    assert op.ok and op.fact_kind == "bullet" and op.bullet_id in world.chosen and op.bullet_id not in ("",)
    bullet = next(b for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if b["id"] == op.bullet_id)
    assert op.sentence.startswith('Anaplan is hiring for "') and "; I " in op.sentence and O.bank_numbers(op.sentence, fb)
    assert op.paragraph == f"{op.sentence} {FAMILY_LINE}"
    assert op.number in numbers(bullet["text"])


def test_with_no_numbered_bullet_on_the_cv_the_opening_has_no_number_and_says_so(world):
    fb = copy.deepcopy(world.fb)
    fb["hook_library"] = []
    plain = {b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if not re.search(r"\d", b["text"])}
    assert plain                                                                  # the fixture bank has bullets without a number
    op = O.build(JOB, fb, world.kws, None, plain, FAMILY_LINE)
    assert not op.ok and "number" in op.error


# ------------------------------------------------------------------ banned openings and the guard on sentence 1
@pytest.mark.parametrize("start", ["I am writing to apply for the Data Scientist role.", "I am writing to express my interest in this post.",
                                   "I would like to apply for this position.", "Please accept my application for the role.",
                                   "  i AM WRITING to apply", "Please accept this letter."])
def test_a_banned_opening_is_rejected(world, start):
    problems = O.problems(f"{start} At Northwind I shipped it behind ~65 tests: \"{O.ad_sentences(JD)[1][:40]}\".", world.fb, [JD])
    assert any(p.startswith("opening: starts with a banned phrase") for p in problems), problems


def test_a_hook_whose_letter_line_is_a_banned_opening_is_skipped(world):
    fb = copy.deepcopy(world.fb)
    fb["hook_library"][0]["letter_line"] = "I am writing to apply because at Northwind I shipped an LLM agent behind ~65 tests."
    op = O.build(JOB, fb, world.kws, H.Hook(**{**dataclasses.asdict(world.hk), "letter_sentence": fb["hook_library"][0]["letter_line"]}),
                 world.chosen, FAMILY_LINE)
    assert op.ok and not O.BANNED.search(op.sentence) and op.fact_kind == "bullet"


def test_a_sentence_with_no_number_from_the_fact_bank_is_rejected(world):
    phrase = O.ad_sentences(JD)[2][:50]
    assert "opening: no number from the fact bank" in O.problems(f"At Northwind I built things people liked: \"{phrase}\".", world.fb, [JD])
    assert "opening: no number from the fact bank" in O.problems(f"At Northwind I shipped 4242 things: \"{phrase}\".", world.fb, [JD])     # 4242 is not the user's
    assert "opening: no number from the fact bank" not in O.problems(f"At Northwind I shipped ~65 tests: \"{phrase}\".", world.fb, [JD])


def test_a_problem_phrase_that_neither_the_ad_nor_the_page_says_is_rejected(world):
    s = 'At Northwind I shipped ~65 tests, the kind of work behind "building quantum toasters for the moon".'
    found = O.problems(s, world.fb, [JD])
    assert 'opening: the problem phrase "building quantum toasters for the moon" is not in the ad or the company\'s own page' in found
    assert O.problems(s, world.fb, [JD, "Acme is busy building quantum toasters for the moon every day."]) == []


def test_an_opening_with_no_quoted_phrase_is_rejected(world):
    assert "opening: no problem phrase from the ad or the company's page" in O.problems("At Northwind I shipped ~65 tests.", world.fb, [JD])
