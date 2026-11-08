"""F2 (v2.4.1): "What motivated you to apply for this position?" and its relatives are always drafted (RightShip left it empty: class `other`, "it is yours").
60-150 words, first person, no greeting; from the letter's P1 and P5 when the job has a letter, else the ad, the user's `motivations` and one CV bullet with
a number; the ad is required (under 150 words it is fetched once more, still short = hand-off, nothing drafted); the claim guard and the 5-word-shingle reuse
check apply; `[l]` regenerates with the reader's feedback. MockLLM only, no network."""
import pytest
from test_freetext import AD_PHRASE, BOOK, FB, JOB, JD, REP, TODAY, WHY, Script, llm_with, make  # noqa: F401  (make is a fixture)

from careerops.apply import answers as answers_mod
from careerops.apply import freetext as ft

QUESTIONS = [
    "What motivated you to apply for this position?",
    "What motivated you to apply?",
    "Why do you want to work here?",
    "Why are you interested in this role?",
    "Why this role?",
    "Why this position?",
    "Why Northwind Health AI?",
    "What excites you about this opportunity?",
    "Why are you applying?",
]


@pytest.mark.parametrize("q", QUESTIONS)
def test_every_motivation_wording_is_a_why_question(q):
    assert ft.classify(q, "Northwind Health AI") in ft.WHY


def test_what_motivated_you_to_apply_is_drafted_not_left_to_the_person(make):
    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    f = make(llm, "y")
    out = f.answer("What motivated you to apply for this position?", "", None)
    assert out == WHY and llm.seen and llm.seen[0]["class"] == "why_role"


def test_the_draft_names_the_company_quotes_the_ad_and_is_60_to_150_words(make):
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    p = f.propose("What motivated you to apply for this position?")
    assert p.text and p.origin == "draft"
    assert 60 <= len(p.text.split()) <= 150 and "Northwind Health AI" in p.text and AD_PHRASE in p.text


@pytest.mark.parametrize("draft,why", [
    ("Northwind Health AI " + AD_PHRASE + " and I want to build AI systems that are trustworthy in production.", "at least 60 words"),
    ("Dear Northwind Health AI team, " + WHY, "greeting"),
    (WHY.replace("Northwind Health AI", "The company"), "does not name the company"),
    (WHY + " " + WHY, "at most 150 words"),
])
def test_a_draft_outside_the_motivation_rules_is_rejected(make, draft, why):
    f = make(llm_with(draft, ad_phrases=[AD_PHRASE]), "")
    p = f.propose("What motivated you to apply for this position?")
    assert p.text == "" and any(why in x for x in p.problems), p.problems


def test_a_smaller_limit_on_the_field_wins(make):
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "")
    p = f.propose("What motivated you to apply for this position? Max 40 words")
    assert p.text == "" and any("at most 40 words" in x for x in p.problems)


def test_with_a_letter_the_sources_are_its_p1_and_p5(make):
    rep = {**REP, "letter": {"status": "ok", "parts": {"P1": "Northwind Health AI is hiring for \"tools that help oncology researchers\"; I shipped an LLM agent.",
                                                       "P5": "The team builds clinical decision-support tools that help oncology researchers.", "P3": "NOT USED"}}}
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y", rep=rep)
    p = f.propose("What motivated you to apply for this position?")
    assert p.plan.ids[:2] == ["ad", "letter"] and "NOT USED" not in p.plan.sources[1].text and "hiring for" in p.plan.sources[1].text
    assert "az2" in p.plan.ids and "m1" in p.plan.ids                  # v3.4: the proof bullet and the motivations a code-built answer may tell


def test_without_a_letter_the_sources_are_the_ad_the_motivations_and_one_numbered_bullet(make):
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    p = f.propose("What motivated you to apply for this position?")
    ids = p.plan.ids
    assert ids[0] == "ad" and any(i.startswith("m") for i in ids)
    bullet = [s for s in p.plan.sources if s.id not in ("ad", "hook") and not s.id.startswith("m")]
    assert len(bullet) == 1 and any(ch.isdigit() for ch in bullet[0].text)


def test_a_claim_about_the_user_that_is_in_no_source_is_still_rejected(make):
    draft = WHY + " I also led a team of 40 engineers at Contoso building Kubeflow pipelines."
    f = make(llm_with(draft, ad_phrases=[AD_PHRASE]), "")
    p = f.propose("What motivated you to apply for this position?")
    assert p.text == "" and p.problems


def test_without_a_jd_the_job_is_handed_off_and_nothing_is_drafted(make):
    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    f = make(llm, "", job={"description": "Short ad. " * 10})
    p = f.propose("What motivated you to apply for this position?")
    assert p.text == "" and p.handoff.startswith("no JD") and llm.seen == []


def test_a_short_jd_is_fetched_once_more_and_then_used(make):
    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    calls = []
    f = make(llm, "y", job={"description": "Short ad. " * 10})
    f.refetch_jd = lambda: calls.append(1) or JD
    p = f.propose("What motivated you to apply for this position?")
    assert calls == [1] and p.text and not p.handoff
    f.propose("Why are you applying?")
    assert calls == [1]                                                                      # once


def test_a_refetch_that_fails_or_stays_short_is_a_hand_off(make):
    f = make(llm_with(WHY), "", job={"description": "Short ad. " * 10})

    def boom():
        raise RuntimeError("blocked")
    f.refetch_jd = boom
    assert f.propose("Why are you applying?").handoff.startswith("no JD")
    g = make(llm_with(WHY), "", job={"description": "Short ad. " * 10})
    g.refetch_jd = lambda: "still short"
    assert g.propose("Why are you applying?").handoff.startswith("no JD")


def test_an_answer_that_reuses_an_earlier_motivation_answer_is_redrafted_then_refused(make, settings):
    first = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y", job={"id": "manual:other", "company": "Other Co"})
    first.lib.add(ft.Record.new("Why Other Co?", WHY, "why_company", "Other Co", "manual:other", [], TODAY))        # an approved answer for another job
    again = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "")
    p = again.propose("What motivated you to apply for this position?")
    assert p.text == "" and any("reuses" in x for x in p.problems)
    assert again.motivation_log and "FAILED" in again.motivation_log[-1]["guard"]


def test_a_reworded_answer_passes_the_reuse_check(make):
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    f.lib.add(ft.Record.new("Why Other Co?", "Totally different words about a different employer and a different reason entirely, "
                                              "with nothing in common at all.", "why_company", "Other Co", "manual:other", [], TODAY))
    p = f.propose("What motivated you to apply for this position?")
    assert p.text and f.motivation_log[-1]["guard"] == "passed" and f.motivation_log[-1]["words"] == len(p.text.split())


def test_l_regenerates_with_the_readers_feedback(make):
    other = WHY.replace("trustworthy in production", "trustworthy in production, which is what I care about most")
    llm = llm_with(WHY, other, ad_phrases=[AD_PHRASE])
    f = make(llm, "l", "shorter and about the data platform", "y")
    out = f.answer("What motivated you to apply for this position?", "", None)
    assert out == other and any("What should change" in p for p in f.script.prompts)
    assert any("the reader asks: shorter and about the data platform" in " ".join(c.get("problems") or []) for c in llm.seen)
    assert "[l]" in ft.MENU


def test_the_motivation_answer_is_stored_after_y_and_reused_for_the_same_job(make):
    f = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    f.answer("What motivated you to apply for this position?", "", None)
    g = make(llm_with("never used"))
    assert g.answer("What motivated you to apply for this position?", "", None) == WHY and g.last == "reused"
