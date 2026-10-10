"""The CV profile: the model picks two of `profile_facts` and writes <= 45 words from them; the guard sees those two facts
(plus the headline, the user's own role title) and nothing else; one retry, then the two facts themselves."""
import pytest

from careerops.tailoring import prompts as P
from careerops.tailoring import tailor
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.shapes import normalise_profile

HEADLINE = "Data Scientist"
FACTS = ["MSc Data Science, University of Northfield (completed 2026)",
         "Production LLM agent work on a clinical research platform",
         "Background in mobile FinTech engineering before data science"]


def _profile(fb, llm, kws=(), retry=None, headline=HEADLINE):
    return tailor.make_profile({"title": "Data Scientist", "company": "Acme"}, fb, list(kws), headline, llm, retry)


def _llm(spy_llm, picks, profile):
    return spy_llm({"tailor_profile": lambda ctx: {"facts": picks, "profile": profile}})


# ---------------------------------------------------------------- code picks the two facts, the model words them
JD_KWS = [Keyword("fintech", 2, True), Keyword("llm", 2, True)]          # facts 2 and 3 of FACTS say these; fact 1 says neither


def test_the_prompt_lists_only_the_two_facts_code_picked_for_the_job(fb_fixture, spy_llm):
    llm = _llm(spy_llm, ["F1", "F2"], "Data Scientist with production LLM agent work and FinTech engineering roots.")
    _profile(fb_fixture, llm, JD_KWS)
    call = llm.sent[0]
    assert call["task"] == "tailor_profile" and "F1: " + FACTS[1] in call["user"] and "F2: " + FACTS[2] in call["user"]
    assert FACTS[0] not in call["user"] and "F3" not in call["user"]            # the MSc fact is the least relevant: not offered
    assert "HEADLINE: Data Scientist" in call["user"] and "exactly two facts" in call["system"]
    assert "at most 45 words" in call["system"] and "{deny}" not in call["system"]
    assert call["context"]["facts"] == [{"id": "F1", "text": FACTS[1]}, {"id": "F2", "text": FACTS[2]}]


def test_the_two_facts_are_ranked_by_keywords_and_by_the_jds_company_domain(fb_fixture, spy_llm):
    """Relevance round: with more than two profile_facts the same ranking as the letter's evidence chooses (keyword overlap
    plus the JD's domain: a bank JD keeps the fact that says FinTech although no keyword names it)."""
    jd = {"title": "Data Scientist", "company": "Monzo", "description": "Help build the bank of the future for our customers."}
    llm = _llm(spy_llm, ["F1", "F2"], "Data Scientist with mobile FinTech engineering roots and an MSc.")
    p = tailor.make_profile(jd, fb_fixture, [], HEADLINE, llm)
    assert FACTS[2] in p["facts"] and len(p["facts"]) == 2                    # FinTech is finance: it outranks the LLM fact
    assert tailor.pick_facts(FACTS, [], [], 2, jd["description"]) == [0, 2]   # MSc (bank order on the tie) + FinTech
    assert tailor.pick_facts(FACTS, [], [], 2, "") == [0, 1]                  # nothing to rank by: the bank's first two
    assert tailor.pick_facts(FACTS[:2], [], [], 2, jd["description"]) == [0, 1]    # two facts or fewer: all of them
    assert tailor.pick_facts(FACTS, JD_KWS, [], 2) == [1, 2]                   # returned in fact-bank order, not rank order


def test_a_good_profile_is_kept_and_only_the_picked_facts_are_reported(fb_fixture, spy_llm):
    text = "Data Scientist with production LLM agent work on a clinical research platform and a mobile FinTech past."
    llm = _llm(spy_llm, ["F1", "F2"], text)
    p = _profile(fb_fixture, llm, JD_KWS)
    assert p["text"] == text and p["facts"] == [FACTS[1], FACTS[2]] and not p["retried"] and not p["fallback"]
    assert p["violations"] == [] and len(llm.sent) == 1


@pytest.mark.parametrize("picks", [["F2", "F3"], [2, 3], ["2", "3"], [FACTS[1], FACTS[2]], "F2, F3",
                                   [{"id": "F2"}, {"id": "F3"}], ["f2", "Fact 3"]])
def test_facts_may_be_named_by_id_number_or_text(fb_fixture, picks):
    idx, _, _ = normalise_profile({"facts": picks, "profile": "x"}, FACTS)
    assert idx == [1, 2]


def test_bad_or_missing_picks_are_topped_up_by_jd_overlap_in_code(fb_fixture, spy_llm):
    p = _profile(fb_fixture, _llm(spy_llm, ["F9", "nonsense"], "Data Scientist with FinTech and LLM work."), JD_KWS)
    assert p["facts"] == [FACTS[1], FACTS[2]]                      # the two facts that mention the JD keywords
    assert any("picked by JD overlap" in w for w in p["warnings"])
    assert any("dropped" in w for w in p["warnings"])
    one = _profile(fb_fixture, _llm(spy_llm, ["F2"], "Data Scientist with FinTech work."), JD_KWS)
    assert one["facts"][0] == FACTS[2] and len(one["facts"]) == 2   # the model's pick stays, code adds the other
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2", "F3"], "Data Scientist with LLM work."), JD_KWS)
    assert p["facts"] == [FACTS[1], FACTS[2]] and any("dropped 'F3'" in w for w in p["warnings"])    # only two were offered
    _, _, notes = normalise_profile({"facts": ["F1", "F2", "F3"], "profile": "x"}, FACTS)
    assert any("more than two" in n for n in notes)


def test_a_model_that_fails_still_gets_a_profile_from_two_facts(fb_fixture, spy_llm):
    def boom(ctx):
        raise RuntimeError("provider down")

    p = _profile(fb_fixture, spy_llm({"tailor_profile": boom}), [Keyword("llm", 2, True)])
    assert p["fallback"] and p["text"] == ". ".join(f for f in p["facts"]) + "." and len(p["facts"]) == 2
    assert any("LLM failed (provider down)" in w for w in p["warnings"])


# ---------------------------------------------------------------- the guard sees the picked facts only
def test_a_number_from_a_bullet_is_not_allowed_in_the_profile(fb_fixture, spy_llm):
    text = "Data Scientist with a three-tier framework of ~65 tests."          # 65 is in bullet a4, not in a fact
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], text))
    assert any("new numbers ['65']" in v for v in p["violations"]) and p["text"] != text


def test_a_skill_or_tool_that_is_not_in_the_picked_facts_is_not_allowed(fb_fixture, spy_llm):
    kws = [Keyword("FastAPI", 2, True), Keyword("Python", 2, True)]            # skills, not profile facts
    text = "Data Scientist with Python and FastAPI services."
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], text), kws)
    assert any("keyword 'FastAPI' not evidenced" in v or "keyword 'Python' not evidenced" in v
               for v in p["violations"])


def test_a_fact_that_was_not_picked_is_not_allowed_either(fb_fixture, spy_llm):
    kws = [Keyword("FinTech", 2, True), Keyword("llm", 2, True), Keyword("Northfield", 0.5, False)]
    text = "Data Scientist with mobile FinTech and LLM work from Northfield."
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], text), kws)          # code picked the FinTech and LLM facts
    assert any("keyword 'Northfield' not evidenced" in v for v in p["violations"])    # the MSc fact says it, but was not picked
    kws = [Keyword("FinTech", 2, True), Keyword("Northfield", 1.5, False)]    # now the MSc fact outranks the LLM fact
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], "Data Scientist with mobile FinTech work from Northfield."), kws)
    assert p["facts"] == [FACTS[0], FACTS[2]] and p["violations"] == []


def test_the_headline_is_allowed_because_it_is_the_users_own_role_title(fb_fixture, spy_llm):
    kws = [Keyword("data scientist", 2, True)]
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], "Data Scientist with production LLM agent work."), kws)
    assert p["violations"] == []


def test_a_deny_listed_tool_is_rejected(fb_fixture, spy_llm):
    fb = dict(fb_fixture, deny_terms=["kubernetes"])
    p = _profile(fb, _llm(spy_llm, ["F1", "F2"], "Data Scientist with Kubernetes and LLM agent work."))
    assert any("deny-listed term 'kubernetes'" in v for v in p["violations"])


@pytest.mark.parametrize("bad, problem", [("Data Scientist with 5 years of LLM agent work.", "years-of-experience"),
                                         ("x", "1 words < 6"),
                                         ("Data Scientist with " + "production LLM agent work " * 12,
                                          "words > 45")])
def test_years_claims_and_long_profiles_are_rejected(fb_fixture, spy_llm, bad, problem):
    p = _profile(fb_fixture, _llm(spy_llm, ["F1", "F2"], bad))
    assert any(problem in v for v in p["violations"]) and p["text"] != bad


# ---------------------------------------------------------------- one retry, then the facts
def test_a_profile_that_fails_the_guard_is_retried_once_with_the_violations_and_the_source_facts(fb_fixture, spy_llm):
    bad = "AI engineer with 5 years of production experience in Kubernetes."
    good = "Data Scientist with production LLM agent work on a clinical research platform."
    llm = spy_llm({"tailor_profile": lambda ctx: ({"profile": good} if "previous" in ctx
                                                  else {"facts": ["F1", "F2"], "profile": bad})})
    p = _profile(fb_fixture, llm)
    retry = llm.sent[1]
    assert p["text"] == good and p["retried"] and not p["fallback"] and len(llm.sent) == 2
    assert "PROBLEMS FOUND:" in retry["user"] and "years-of-experience" in retry["user"] and bad in retry["user"]
    assert "SOURCE FACTS:\n" + FACTS[0] + "\n" + FACTS[1] in retry["user"]
    assert any(v.startswith("profile:") for v in p["violations"])
    assert any("rewritten once" in w for w in p["warnings"])


@pytest.mark.parametrize("retry", [lambda p, pr, s: "AI engineer with 5 years in Kubernetes.", lambda p, pr, s: "",
                                   lambda p, pr, s: (_ for _ in ()).throw(RuntimeError("llm down"))])
def test_the_two_facts_are_used_only_after_the_retry_also_fails(fb_fixture, spy_llm, retry):
    llm = _llm(spy_llm, ["F1", "F2"], "AI engineer with 5 years of production experience in Kubernetes.")
    p = _profile(fb_fixture, llm, retry=retry)
    assert p["fallback"] and p["text"] == FACTS[0].rstrip(".") + ". " + FACTS[1] + "."
    assert any("fell back to the picked profile_facts" in w for w in p["warnings"])


def test_a_good_profile_is_never_retried(fb_fixture, spy_llm):
    calls = []
    llm = _llm(spy_llm, ["F1", "F2"], "Data Scientist with production LLM agent work on a clinical research platform.")
    p = _profile(fb_fixture, llm, retry=lambda *a: calls.append(a) or "x")
    assert calls == [] and not p["retried"]


# ---------------------------------------------------------------- the prompt
def test_the_prompt_asks_for_the_headline_first():
    assert "starting with the HEADLINE" in P.PROFILE_SYSTEM and "{headline}" in P.PROFILE_USER
