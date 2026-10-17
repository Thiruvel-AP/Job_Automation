"""The CV profile's facts: code picks the `profile_facts` that matter most for the job (keyword overlap plus the JD's company domain),
the fact bank's order breaks a tie, and the profile is assembled from them (tests/tailoring/test_profile_assembled.py). The model
no longer picks or words anything here: the old picking/retry tests went with the `tailor_profile` call."""
from careerops.tailoring import tailor
from careerops.tailoring.profile import assemble
from careerops.tailoring.scorer import Keyword

HEADLINE = "Data Scientist"
FACTS = ["MSc Data Science, University of Northfield (completed 2026)",
         "Production LLM agent work on a clinical research platform",
         "Background in mobile FinTech engineering before data science"]
JD_KWS = [Keyword("fintech", 2, True), Keyword("llm", 2, True)]          # facts 2 and 3 of FACTS say these; fact 1 says neither


def test_the_two_facts_are_ranked_by_keywords_and_by_the_jds_company_domain(fb_fixture):
    """With more than two profile_facts the same ranking as the letter's evidence chooses (keyword overlap plus the JD's domain: a
    bank JD keeps the fact that says FinTech although no keyword names it)."""
    jd = "Help build the bank of the future for our customers."
    p = assemble({"profile_facts": FACTS, "deny_terms": []}, [], jd)
    assert FACTS[2] in p["facts"] and len(p["facts"]) == 2                    # FinTech is finance: it outranks the LLM fact
    assert tailor.pick_facts(FACTS, [], [], 2, jd) == [0, 2]                  # MSc (bank order on the tie) + FinTech
    assert tailor.pick_facts(FACTS, [], [], 2, "") == [0, 1]                  # nothing to rank by: the bank's first two
    assert tailor.pick_facts(FACTS[:2], [], [], 2, jd) == [0, 1]              # two facts or fewer: all of them
    assert tailor.pick_facts(FACTS, JD_KWS, [], 2) == [1, 2]                  # returned in fact-bank order, not rank order


def test_keywords_decide_which_facts_the_profile_uses(fb_fixture):
    p = assemble({"profile_facts": FACTS, "deny_terms": []}, JD_KWS, "")
    assert p["facts"] == [FACTS[1], FACTS[2]]
    assert p["text"] == "Production LLM agent work on a clinical research platform. Background in mobile FinTech engineering before data science."


def test_the_profile_of_the_fixture_bank_is_the_users_own_sentences(fb_fixture):
    p = assemble(fb_fixture, JD_KWS, "", "Data scientist who ships.")
    assert p["text"].startswith("Data scientist who ships. ") and p["violations"] == [] and not p["retried"] and not p["fallback"]
    assert all(f in fb_fixture["profile_facts"] for f in p["facts"])
