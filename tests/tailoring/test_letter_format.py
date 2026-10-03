"""CV-2: letter greeting/sign-off from settings, the team name, and the optional gap sentence."""
from pathlib import Path

from careerops.tailoring import prompts as P
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import letter_greeting, regenerate_letter, tailor_job

JD_NO_GAP = "About the role\nWe build data tools.\n\nRequirements\n- Python\n- LangChain\n"
JD_GAP = "About the role\nWe build data tools.\n\nRequirements\n- Python\n- Kubernetes\n"
APOLOGY = ("sorry", "apolog", "unfortunately", "regret")


def _run(settings, fb, job, llm, jd=None):
    job = {**job, "description": jd if jd is not None else job["description"]}
    return tailor_job(job, fb, llm, settings, settings.path("outputs")), job


def _letter_ctx(llm):
    return [s["context"] for s in llm.sent if s["task"] == "cover_letter"][-1]


def _team(name):
    return {"extract_keywords": lambda ctx: {"required": [], "preferred": [], "company_hook": "", "team": name}}


# ---------------------------------------------------------------- greeting
def test_settings_carry_the_letter_block(settings):
    assert settings["letter"] == {"greeting": "Hi {company}{team} team",
                                  "greeting_fallback": "Dear {company} hiring team", "signoff": "With regards"}


def test_greeting_uses_the_team_when_there_is_one_and_the_fallback_when_not(settings):
    assert letter_greeting(settings, "Lendable", " AI Accelerator") == "Hi Lendable AI Accelerator team"
    assert letter_greeting(settings, "Lendable", "AI Accelerator ") == "Hi Lendable AI Accelerator team"
    assert letter_greeting(settings, "Lendable", "") == "Dear Lendable hiring team"


def test_letter_without_a_team_says_dear_hiring_team_and_signs_off_from_settings(settings, fb_fixture, job, spy_llm):
    rep, _ = _run(settings, fb_fixture, job, spy_llm())
    text = pdf_text(Path(rep["letter_pdf"]))
    assert "Dear Northwind Health AI hiring team," in text
    assert "With regards," in text and "Kind regards" not in text


def test_letter_with_a_team_named_in_the_jd_says_hi_team(settings, fb_fixture, job, spy_llm):
    jd = job["description"] + "\nYou will join the AI Accelerator group.\n"
    rep, _ = _run(settings, fb_fixture, job, spy_llm(_team(" AI Accelerator")), jd)
    assert rep["team"] == "AI Accelerator"
    assert "Hi Northwind Health AI AI Accelerator team," in pdf_text(Path(rep["letter_pdf"]))


def test_a_team_the_jd_never_mentions_is_dropped(settings, fb_fixture, job, spy_llm):
    rep, _ = _run(settings, fb_fixture, job, spy_llm(_team("Phantom Squad")))
    assert rep["team"] == ""
    assert "Dear Northwind Health AI hiring team," in pdf_text(Path(rep["letter_pdf"]))


def test_regenerated_letter_keeps_the_team_greeting(settings, fb_fixture, job, spy_llm):
    jd = job["description"] + "\nYou will join the AI Accelerator group.\n"
    llm = spy_llm(_team("AI Accelerator"))
    rep, job2 = _run(settings, fb_fixture, job, llm, jd)
    rep = regenerate_letter(job2, fb_fixture, llm, settings, rep)
    assert "Hi Northwind Health AI AI Accelerator team," in pdf_text(Path(rep["letter_pdf"]))


# ---------------------------------------------------------------- gaps
def test_no_gap_sentence_when_gaps_is_empty(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, JD_NO_GAP)
    assert rep["genuine_gaps"] == [] and _letter_ctx(llm)["gaps"] == []
    assert "GAPS" in llm.sent[-1]["user"] and "(none)" in llm.sent[-1]["user"]
    text = " ".join(rep["letter"]["paragraphs"]).lower()
    assert "kubernetes" not in text and "not yet" not in text
    assert len(rep["letter"]["paragraphs"]) == 3 and rep["letter"]["status"] == "ok"


def test_a_required_gap_reaches_the_prompt_and_gets_one_short_plain_sentence(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, JD_GAP)
    assert _letter_ctx(llm)["gaps"] == ["kubernetes"]                 # the taxonomy's spelling
    assert "kubernetes" in llm.sent[-1]["user"].lower()
    gap = [s for p in rep["letter"]["paragraphs"] for s in p.split(". ") if "kubernetes" in s.lower()]
    assert len(gap) == 1 and len(gap[0].split()) <= 25
    assert not any(a in gap[0].lower() for a in APOLOGY)
    # 'kubernetes' is on the deny list; naming it as a gap is allowed, so the model's letter stands
    assert rep["letter"]["status"] == "ok" and rep["letter"]["problems"] == []


def test_only_required_gaps_are_passed(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep, _ = _run(settings, fb_fixture, job, llm, "Requirements\n- Python\n\nNice to have\n- Kubernetes\n")
    assert "kubernetes" in rep["genuine_gaps"] and _letter_ctx(llm)["gaps"] == []


def test_deny_listed_gap_term_is_still_blocked_when_the_letter_claims_it(settings, fb_fixture, job, spy_llm):
    lying = {"cover_letter": lambda ctx: {"paragraphs": [
        "Northwind builds tools I already work on.", "I have run Kubernetes clusters in production for two years.",
        "Could we talk for twenty minutes?"]}}
    rep, _ = _run(settings, fb_fixture, job, spy_llm(lying), JD_GAP)
    assert rep["letter"]["status"] == "fallback"
    assert any("deny-listed 'kubernetes'" in p for p in rep["letter"]["problems"])


def test_deny_listed_term_that_is_not_a_gap_is_still_blocked(settings, fb_fixture, job, spy_llm):
    sneaky = {"cover_letter": lambda ctx: {"paragraphs": [
        "Northwind builds tools I already work on.", "I have not yet used Terraform, but I learn quickly.",
        "Could we talk for twenty minutes?"]}}
    rep, _ = _run(settings, fb_fixture, job, spy_llm(sneaky), JD_GAP)
    assert rep["letter"]["status"] == "fallback"
    assert any("deny-listed 'terraform'" in p for p in rep["letter"]["problems"])


# ---------------------------------------------------------------- prompt text
def test_letter_prompt_follows_the_user_format():
    s = P.LETTER_SYSTEM.lower()
    for must in ("45 words", "25 words", "gaps", "question", "never describe a metric as measured"):
        assert must in s, must
    assert "{gaps}" in P.LETTER_USER


def test_keywords_prompt_asks_for_the_team():
    assert '"team"' in P.KEYWORDS_SYSTEM and "AI Accelerator" in P.KEYWORDS_SYSTEM


def test_tailor_prompt_follows_the_user_format():
    s = P.TAILOR_SYSTEM
    for must in ("45 words", "exactly 2", "2-4", "1-2", "past-tense", "STAR"):
        assert must in s, must
    assert "{headline}" in P.TAILOR_USER
