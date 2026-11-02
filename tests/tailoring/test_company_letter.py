"""letter.mode: company (tailoring/company_letter.py), v2.3: P1 the hook (the ad's problem phrase, a semicolon, the hook's letter line), P2 what it
shows (the hook fact's `limits` / `result`, verbatim), P3 the ad's skills through a second bullet, P4 the experience roll-up, P5 why this team (one
sentence quoting the ad), P6 one honest gap (optional), P7 the 20-minute ask; 200-320 body words; up to three drafts; no letter (and review says why)
after the third rejection. MockLLM only: no network, no model. The guards are called directly (fast); the tests that make a PDF are marked slow
(xelatex)."""
import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_settings, load_yaml
from careerops.llm import MockLLM
from careerops.review import workflow
from careerops.tailoring import company_letter as CL
from careerops.tailoring import tailor
from careerops.tailoring.grounding import sentences
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

HERE = Path(__file__).parent.parent
GOLDEN = HERE / "golden" / "anaplan"
LIBRARY = load_yaml(HERE / "fixtures" / "tailoring" / "hook_library_fixture.yaml")
JD = (GOLDEN / "jd.txt").read_text(encoding="utf-8")
JOB = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan", "title": "Data Scientist, Functional Analytics (REQ 26490)",
       "location": "London, UK", "url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002", "description": JD}
TEAM = "Functional Analytics"
FAMILY_LINE = "Builds the modelling a brief needs and reports its limits plainly, from churn models to forecasts."
LIMITS = "The gain came from finishing answers inside the budget, not from stronger reasoning; I reported it that way."

# P3, P4 and P5 as a model would write them for this ad and these bullets (every number, tool and employer is the bullets'; P5 follows an About sentence)
P3 = ("For Python and XGBoost, my closest match is a churn model built on 7,043 customer records. Cross validation chose the winner, "
      "and I set its threshold to favour recall.")
P4 = ("At Northwind Research Lab I designed the guard architecture behind a 19,213-gene fallback, and at Bristol Pro Bono Consulting I cleaned "
      "and joined donor, event and survey exports with pandas and SQL for the charity's trustees.")
P5 = ("The Functional Analytics team's core mission is to design and develop functional analytic capabilities that integrate directly into "
      "Anaplan Applications.")
PAD = ("beyond this example I enjoy talking through decisions with colleagues and explaining each step in plain language so others can follow "
       "the reasoning").split()


def pad(n: int) -> str:
    """Exactly n neutral words (no name, number, tool or claim) ending in a full stop."""
    return " ".join((PAD * (n // len(PAD) + 1))[:n]) + "." if n > 0 else ""


def keywords():
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


def make_world(settings, fb_fixture, limits: str = "", result: str = "", claimable: list[str] | None = None, possible: dict | None = None,
               job: dict = JOB, jd: str = JD):
    settings.raw["letter"]["mode"] = "company"
    settings.raw["letter"]["compose"] = "free"            # these tests drive the model-written body; the assembled default: test_letter_assembled.py
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = copy.deepcopy(LIBRARY)
    if limits:
        fb["hook_library"][0]["limits"] = limits
    if result:
        for sec in ("experience", "projects"):
            for e in fb[sec]:
                for b in e["bullets"]:
                    if b["id"] == fb["hook_library"][0]["proof"]:
                        b["result"] = result
    kws = keywords()
    hk = H.build_hook(job, kws, fb, role="Data Scientist")
    assert hk is not None
    chosen = {b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    evidence = tailor.letter_evidence(fb, kws, chosen, jd)
    claim = [k.term for k in kws] if claimable is None else claimable
    plan = CL.prepare(job, fb, kws, hk, "", evidence, settings, claim, chosen, possible=possible)
    assert not plan.error, plan.error
    return SimpleNamespace(settings=settings, fb=fb, kws=kws, hk=hk, chosen=chosen, evidence=evidence, claimable=claim, plan=plan, possible=possible,
                           job=job)


@pytest.fixture
def world(settings, fb_fixture):
    return make_world(settings, fb_fixture)


def letter_parts(w, total: int = 240, p2: bool = True, gap: bool = False, **over) -> dict[str, str]:
    """{"P1".."P7"} of a letter of exactly `total` body words: the code-written paragraphs as the plan has them, P3, P4 and P5 as above with neutral
    padding in P3 and P4. `over` replaces a paragraph (and then it is not padded)."""
    parts = {"P1": w.plan.p1, "P3": P3, "P4": P4, "P5": P5, "P7": w.plan.p7}
    if p2 and w.plan.p2:
        parts["P2"] = w.plan.p2
    if gap and w.plan.gap:
        parts["P6"] = w.plan.gap["paragraph"]
    padded = {"P3", "P4"} - set(over)
    parts.update(over)
    need = total - CL.words(CL.assemble(parts)[0])
    assert need >= 0, f"the base letter is already {total - need} words"
    for n, share in zip(sorted(padded), (need // 2, need - need // 2)):
        parts[n] = f"{parts[n]} {pad(share)}".strip()
    return parts


def check(w, parts: dict[str, str]) -> list[str]:
    problems, _ = CL.check(parts, w.job, w.fb, w.kws, w.plan, TEAM, w.settings, claimable=w.claimable)
    return problems


# ------------------------------------------------------------------ the setting
def test_the_mode_is_known_and_the_shipped_settings_hold_the_new_keys(settings):
    settings.raw["letter"]["mode"] = "company"
    assert tailor.letter_mode(settings) == "company" and "company" in tailor.LETTER_MODES
    s = load_settings()
    assert s["letter"]["company_words"] == [200, 320] and s["letter"]["honest_gap"] is False        # tests use false (B5)
    assert s["scoring"]["letter_max_words"] == 250                                                   # the other modes keep theirs


def test_the_word_cap_is_the_company_one_and_never_over_320(settings):
    assert CL.word_cap(settings) == 320 and CL.word_range(settings) == (200, 320)
    settings.raw["scoring"]["letter_max_words_company"] = 600
    assert CL.word_cap(settings) == 320
    settings.raw["scoring"]["letter_max_words_company"] = 300
    assert CL.word_cap(settings) == 300 and settings["scoring"]["letter_max_words"] == 250


@pytest.mark.parametrize("raw", [[199, 320], [200, 321], [320, 200], [250], 260, "200-320", ["a", 300], [True, 300]])
def test_a_range_outside_200_to_320_is_refused(settings, raw):
    settings.raw["letter"]["company_words"] = raw
    with pytest.raises(ValueError, match="letter.company_words must be"):
        CL.word_range(settings)


# ------------------------------------------------------------------ the paragraphs: what each one is
def test_p1_is_the_ads_problem_phrase_a_semicolon_and_the_hooks_letter_line(world):
    p1, op = world.plan.p1, world.plan.opening
    assert p1.startswith(f'Anaplan is hiring for "{op.problem}"; ') and op.source == "ad" and op.problem in JD          # the phrase is the ad's own words
    assert p1.endswith(world.hk.letter_sentence[0].lower() + world.hk.letter_sentence[1:].rstrip(".") + ".")       # the hook's line, "At" lower-cased, nothing else
    assert op.hook_id == world.hk.hook_id and "~65" in p1 and not p1.lower().startswith(("i am writing", "i would like to apply", "please accept"))


def test_p1_quotes_the_companys_own_page_when_the_ad_has_no_clause_about_the_work(settings, fb_fixture):
    w = make_world(settings, fb_fixture)
    page = "Anaplan builds forecasting software. We are hiring analysts to design reliable planning models for large retailers."
    plan = CL.prepare({**JOB, "description": "Apply now. Equal opportunity employer."}, w.fb, w.kws, w.hk, "", w.evidence, settings, w.claimable, w.chosen, page)
    assert not plan.error and plan.opening.source == "company_page" and plan.p1.startswith('Anaplan describes its work as "')
    assert plan.opening.problem in page


def test_p2_is_the_hook_facts_limits_word_for_word(settings, fb_fixture):
    w = make_world(settings, fb_fixture, limits=LIMITS)
    assert w.plan.p2 == LIMITS and w.plan.p2_source == "hook h_prod_tests limits"


def test_p2_falls_back_to_the_proof_bullets_result_and_leaves_out_arrows(settings, fb_fixture):
    w = make_world(settings, fb_fixture, result="answer completion 32% → 52%; exact-answer accuracy 16.2% → 27.3%")
    assert w.plan.p2 == "The result, as measured: answer completion 32% to 52%; exact-answer accuracy 16.2% to 27.3%."
    assert w.plan.p2_source == "bullet a4 result"


def test_p2_is_left_out_when_the_fact_bank_has_neither_and_the_letter_still_passes(world):
    assert world.plan.p2 == "" and "P2" not in letter_parts(world)
    assert check(world, letter_parts(world)) == []


def test_limits_win_over_a_result(settings, fb_fixture):
    w = make_world(settings, fb_fixture, limits=LIMITS, result="14 source datasets")
    assert w.plan.p2 == LIMITS


def test_p2_may_only_hold_numbers_the_fact_bank_holds(settings, fb_fixture):
    w = make_world(settings, fb_fixture, limits=LIMITS)
    assert check(w, letter_parts(w)) == []
    assert any("P2: number(s) the fact bank does not hold" in p for p in check(w, letter_parts(w, p2=False, P2="It improved accuracy by 83% overall.")))


def test_p3_shows_two_or_three_of_the_ads_skills_through_a_bullet_that_is_not_the_hooks(world):
    skills = world.plan.reqs[0]
    assert 2 <= len(skills) <= 3 and all(s in {k.term for k in world.kws} for s in skills)
    assert world.hk.proof_bullet_id not in {e["id"] for e in world.plan.groups[0]} and len(world.plan.groups[0]) in (1, 2)
    assert world.plan.reqs[1] == []                                                                  # P4 has no skills to name


def test_p4_has_one_bullet_for_each_of_the_two_most_relevant_roles(world):
    roles = world.plan.groups[1]
    assert len(roles) == 2 and [CL.employer(e) for e in roles] == ["Northwind Research Lab", "Bristol Pro Bono Consulting"]
    assert not {e["id"] for e in roles} & {e["id"] for e in world.plan.groups[0]}


def test_p7_is_the_20_minute_ask_about_the_ads_first_problem_and_the_role(world):
    assert world.plan.p7 == f"Could we spend 20 minutes on the first problem the Data Scientist would own, around {world.hk.problem}?"
    assert world.plan.p7.startswith("Could we spend 20 minutes on ") and world.plan.p7.endswith("?")


def test_p7_also_asks_what_the_assessment_covers_when_the_ad_names_one(world):
    job = {**JOB, "description": JD + "\nThe process includes a technical assessment and a final interview.\n"}
    assert CL.ask_text(job, "forecasting models", world.settings) == (
        "Could we spend 20 minutes on the first problem the Data Scientist would own, around forecasting models, and what the technical assessment covers?")
    assert CL.assessment_in(JD) == "" and CL.assessment_in("a case study round") == "case study"


def test_p7_with_no_problem_phrase_still_names_the_role(world):
    assert CL.ask_text(JOB, "the Data Scientist role", world.settings) == "Could we spend 20 minutes on the first problem the Data Scientist would own?"


def test_with_no_hook_p1_is_a_numbered_bullet_then_the_family_profile_line(world):
    plan = CL.prepare(JOB, world.fb, world.kws, None, FAMILY_LINE, world.evidence, world.settings, world.claimable, world.chosen)
    assert not plan.error and plan.p1.endswith(FAMILY_LINE) and plan.opening.fact_kind == "bullet" and plan.p1.startswith('Anaplan is hiring for "')
    assert "; I " in plan.p1 and plan.p7.startswith("Could we spend 20 minutes on ") and plan.p2 == ""


def test_with_no_numbered_fact_on_the_cv_there_is_nothing_to_open_with(world):
    plain = {b["id"] for sec in ("experience", "projects") for e in world.fb[sec] for b in e["bullets"] if not any(c.isdigit() for c in b["text"])}
    plan = CL.prepare(JOB, world.fb, world.kws, None, "", world.evidence, world.settings, world.claimable, plain)
    assert "number" in plan.error and "nothing numbered to open the letter with" in plan.error


# ------------------------------------------------------------------ a good letter passes; the length is the BODY
def test_a_letter_of_the_right_shape_and_length_passes_the_guards(world):
    parts = letter_parts(world, 240)
    assert CL.words(CL.assemble(parts)[0]) == 240 and check(world, parts) == []
    assert CL.assemble(parts)[1] == ["P1", "P3", "P4", "P5", "P7"]


@pytest.mark.parametrize("total", [200, 201, 260, 319, 320])
def test_200_to_320_body_words_pass(world, total):
    assert check(world, letter_parts(world, total)) == []


@pytest.mark.parametrize("total", [150, 199, 321, 400])
def test_199_and_321_words_and_the_rest_outside_the_range_fail(world, total):
    problems = check(world, letter_parts(world, total))
    assert f"{total} words: the letter must be between 200 and 320" in problems, problems


def test_the_greeting_sign_off_and_name_are_not_counted(world, tmp_path):
    parts = letter_parts(world, 200)
    assert check(world, parts) == []                                      # exactly 200 body words: "Hi Anaplan team," and "Regards," add nothing
    assert len(" ".join(CL.assemble(parts)[0]).split()) == 200


def test_a_lower_cap_in_settings_narrows_the_range(world):
    world.settings.raw["scoring"]["letter_max_words_company"] = 300
    assert check(world, letter_parts(world, 300)) == []
    assert any("301 words" in p for p in check(world, letter_parts(world, 301)))


def test_a_draft_that_misses_a_paragraph_is_rejected(world):
    parts = letter_parts(world)
    del parts["P4"]
    assert check(world, parts) == ["missing paragraph(s): P4"]


# ------------------------------------------------------------------ P5: one sentence, quoting the ad
@pytest.mark.parametrize("claim", [
    "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia.",
    "The Functional Analytics team builds a data warehouse for logistics partners.",
])
def test_a_p5_claim_the_ad_does_not_make_is_rejected(world, claim):
    problems = check(world, letter_parts(world, P5=claim))
    assert any(p.startswith("P5: claim about the company is not supported by the ad") for p in problems), problems


def test_a_company_number_the_ad_does_not_have_is_rejected(world):
    problems = check(world, letter_parts(world, P5="Anaplan has more than 9,000 customers and its platform supports scenario planning and analysis."))
    assert any("number(s) about the company not in the ad" in p and "9000" in p for p in problems), problems


def test_an_invented_person_or_team_name_in_p5_is_rejected(world):
    problems = check(world, letter_parts(world, P5="The Insights Guild is hiring a Data Scientist to design functional analytic capabilities for Anaplan Applications."))
    assert any("Guild" in p and "(a name) is not in the ad" in p for p in problems), problems


def test_p5_is_one_sentence(world):
    problems = check(world, letter_parts(world, P5=P5 + " It also solves complex enterprise business problems for customers."))
    assert any(p.startswith("P5 is 2 sentences") for p in problems), problems


def test_p5_that_follows_none_of_the_about_sentences_is_rejected(world):
    problems = check(world, letter_parts(world, P5="Anaplan is a company."))
    assert any(p.startswith("P5 follows 0 of the ad's About / role sentences") for p in problems), problems


def test_the_review_sources_of_p5_are_the_ad_sentences_it_was_written_from(world):
    skip = CL.stems(f"Anaplan {JOB['title']} {TEAM}")
    rows = CL.p5_sources(P5, JD, skip)
    assert [r["claim"] for r in rows] == sentences(P5) and "integrate directly into Anaplan Applications" in rows[0]["ad"][0]
    assert CL.used_sentences(P5, CL.about_sentences(JD, "Anaplan", world.kws), skip)


def test_we_and_our_about_the_employer_and_keyword_lists_are_rejected(world):
    problems = check(world, letter_parts(world, P5="At Anaplan we build an AI-infused scenario planning and analysis platform for our customers."))
    assert any("'we'" in p or "'our'" in p for p in problems), problems
    dump = "Python, SQL, XGBoost, PyTorch, MLflow, Optuna, Prefect, Snowflake, dbt."
    problems = check(world, letter_parts(world, P3=P3 + " " + dump))
    assert any("list of terms" in p or "repeated" in p or "lower case" in p for p in problems), problems


# ------------------------------------------------------------------ P3 and P4: the user guard
def test_a_user_number_that_is_not_in_the_chosen_bullets_is_rejected(world):
    problems = check(world, letter_parts(world, P3=P3 + " That work cut the error rate by 83% for the team."))
    assert any(p.startswith("P3: number(s) about you not in the chosen bullets") and "83%" in p for p in problems), problems


def test_a_tool_or_organisation_that_is_not_the_bullets_is_rejected(world):
    problems = check(world, letter_parts(world, P3=P3 + " I also ran the whole pipeline on Docker for the Harbour Pay Ltd team."))
    assert any("names 'docker'" in p or "names 'Docker'" in p for p in problems), problems


def test_p3_must_name_two_of_its_skills(world):
    one = "For Python, my closest match is a churn model built on 7,043 customer records. Cross validation chose the winner, and I set its threshold to favour recall."
    problems = check(world, letter_parts(world, P3=one))
    assert any(p.startswith("P3: names 1 of the ad skills") for p in problems), problems
    none = "My closest match is a churn model built on 7,043 customer records. Cross validation chose the winner, and I set its threshold to favour recall."
    assert any(p.startswith("P3: names 0 of the ad skills") for p in check(world, letter_parts(world, P3=none)))


def test_p4_names_every_roles_employer_and_a_number_of_its_bullet(world):
    no_employer = P4.replace("Bristol Pro Bono Consulting", "a charity")
    assert any(p == "P4: does not name the employer Bristol Pro Bono Consulting" for p in check(world, letter_parts(world, P4=no_employer)))
    no_number = P4.replace("19,213-gene ", "")
    assert any(p.startswith("P4: gives no number from the Northwind Research Lab bullet") for p in check(world, letter_parts(world, P4=no_number)))


def test_a_deny_listed_tool_is_rejected(world):
    deny = world.fb["deny_terms"][0]
    problems = check(world, letter_parts(world, P3=P3 + f" I have also used {deny} for years."))
    assert any(deny in p.lower() for p in problems), problems


def test_a_pasted_bullet_is_rejected(world):
    bullet = world.plan.groups[0][0]["text"]
    assert any("pastes a fact-bank bullet" in p for p in check(world, letter_parts(world, P3="For Python and XGBoost: " + bullet)))


def test_filler_in_the_body_is_rejected(world):
    assert any("vague filler 'seamless'" in p for p in check(world, letter_parts(world, P3=P3 + " I deliver seamless releases.")))


def test_at_least_60_percent_of_the_sentences_of_p1_to_p4_cite_a_bullet(world):
    tags = CL.citation_tags(letter_parts(world), world.plan.cite_ids())
    assert {t["para"] for t in tags} == {"P1", "P3", "P4"} and all(t["ids"] for t in tags) and CL.balance_problems(tags) == []
    uncited = [{**t, "ids": []} if i % 2 == 0 else t for i, t in enumerate(tags)] + [{"para": "P4", "sentence": "x", "ids": []}] * 4
    assert CL.balance_problems(uncited)[0].startswith("body: ") and "at least 60% must" in CL.balance_problems(uncited)[0]


def test_p5_to_p7_are_not_counted_in_the_citation_rule(world):
    tags = CL.citation_tags(letter_parts(world, gap=True), world.plan.cite_ids())
    assert not {t["para"] for t in tags} & {"P5", "P6", "P7"}


# ------------------------------------------------------------------ B4: memory, RAM and GB figures
def test_a_gb_figure_is_rejected_when_the_ad_is_not_about_hardware(world):
    problems = check(world, letter_parts(world, P3=P3 + " I cut memory use from 8.2 to 5.5 GB."))
    assert any(p.startswith("P3: mentions memory / RAM / GB") for p in problems), problems


def test_a_gb_figure_is_allowed_when_the_ad_talks_about_gpus_memory_or_edge(world):
    parts = letter_parts(world, P3=P3 + " I cut memory use from 8.2 to 5.5 GB.")
    assert CL.hardware_problems(parts, JD) != [] and CL.hardware_problems(parts, JD + "\nYou will optimise memory use on edge devices.") == []
    assert CL.hardware_problems(parts, "We deploy models on a single GPU.") == []


def test_ram_in_the_hook_headline_is_not_this_checks_business():
    assert CL.hardware_problems({"P1": "At Acme I fitted the model in 8 GB.", "P7": "x?"}, "A data role.") != []


# ------------------------------------------------------------------ B5: the honest gap (P6)
GAP_KEYS = {"possible": {"Snowflake": [{"via": "PostgreSQL", "where": "dc"}]}}


def gap_world(settings, fb_fixture, **kw):
    claim = [k.term for k in keywords() if k.term not in ("Snowflake", "Databricks")]
    return make_world(settings, fb_fixture, claimable=claim, **kw)


def test_the_gap_is_the_heaviest_required_keyword_the_fact_bank_cannot_claim(settings, fb_fixture):
    w = gap_world(settings, fb_fixture)
    assert w.plan.gap["term"] == "Snowflake" and w.plan.gap["have"] == ""
    assert w.plan.gap["paragraph"] == "One honest gap: I have never used Snowflake, so it is where I will learn."


def test_the_gap_with_a_neighbour_skill_the_bank_backs_has_the_agreed_form(settings, fb_fixture):
    w = gap_world(settings, fb_fixture, possible=GAP_KEYS["possible"])
    assert w.plan.gap["paragraph"] == "One honest gap: I have used PostgreSQL only, never Snowflake, so Snowflake is where I will learn."
    assert check(w, letter_parts(w, gap=True)) == []


def test_an_everything_claimable_ad_has_no_gap(world):
    assert world.plan.gap == {}


@pytest.mark.parametrize("term", ["visa sponsorship", "MSc degree", "5 years of experience"])
def test_a_hard_filter_is_never_a_gap(settings, fb_fixture, term):
    w = gap_world(settings, fb_fixture)
    kws = [*w.kws, Keyword(term, 9.0, True, [])]
    assert CL.gap_paragraph(JOB, kws, w.fb, w.claimable, None)["term"] == "Snowflake"


def test_with_only_a_hard_filter_left_there_is_no_p6(settings, fb_fixture):
    w = make_world(settings, fb_fixture)
    kws = [Keyword("visa sponsorship", 9.0, True, [])]
    assert CL.gap_paragraph(JOB, kws, w.fb, [], None) == {}


def test_a_deny_listed_gap_is_never_written_up(settings, fb_fixture):
    w = gap_world(settings, fb_fixture)
    fb = copy.deepcopy(w.fb)
    fb["deny_terms"] = [*fb["deny_terms"], "snowflake"]
    assert CL.gap_paragraph(JOB, w.kws, fb, w.claimable, None)["term"] != "Snowflake"


def test_p6_is_only_in_the_letter_when_honest_gap_is_on(settings, fb_fixture):
    w = gap_world(settings, fb_fixture)
    off = CL.included(w.plan, settings, set())
    assert "P6" not in off
    settings.raw["letter"]["honest_gap"] = True
    on = CL.included(w.plan, settings, set())
    assert on["P6"] == w.plan.gap["paragraph"] and "P6" not in CL.included(w.plan, settings, {"P6"})
    assert "P6" in CL.included(w.plan, load_settings(), set(), kept_gap=True)          # the person kept it in review


def test_p6_must_be_one_sentence_a_real_gap_and_a_claim_the_bank_backs(settings, fb_fixture):
    w = gap_world(settings, fb_fixture, possible=GAP_KEYS["possible"])
    ok = letter_parts(w, gap=True)
    assert check(w, ok) == []
    assert any("exactly one sentence" in p for p in check(w, {**ok, "P6": "I lack Snowflake. I will learn it."}))
    w.plan.gap = {**w.plan.gap, "have": "Kubernetes"}
    assert any("'Kubernetes' is not in the fact bank" in p for p in check(w, ok))
    w.plan.gap = {**w.plan.gap, "term": "Python", "have": ""}
    assert any("is not a gap: the fact bank claims it" in p for p in check(w, ok))


# ------------------------------------------------------------------ the model call: draft, redraft (up to three drafts), then no letter
def writer(responses: list):
    """A cover_letter handler that answers with the next entry of `responses` (a list of [P3, P4, P5]) and records the contexts it was given."""
    seen = []

    def handler(ctx):
        seen.append(ctx)
        return {"paragraphs": responses[min(len(seen), len(responses)) - 1]}
    return handler, seen


def body(w, total: int = 240, **over) -> list[str]:
    parts = letter_parts(w, total, **over)
    return [parts["P3"], parts["P4"], parts["P5"]]


def run(w, llm, hk="hook", family_line="", revise=None, header=None, folder="company"):
    return CL.make(w.job, w.fb, w.kws, w.hk if hk == "hook" else None, family_line, w.evidence, llm, w.settings, w.settings.path("outputs") / folder,
                   "Ada Lovelace Cover Letter", TEAM, w.claimable, w.chosen, revise=revise, header=header, possible=w.possible)


@pytest.mark.slow
def test_the_model_is_asked_through_the_cover_letter_task_with_the_ad_the_table_and_the_evidence(world):
    handler, seen = writer([body(world)])
    llm = MockLLM({"cover_letter": handler})
    pdf, letter = run(world, llm)
    assert llm.calls == ["cover_letter"] and seen[0]["mode"] == "company" and seen[0]["ad"] == JD
    assert seen[0]["opening"] == world.plan.p1 and seen[0]["closing"] == world.plan.p7 and len(seen[0]["evidence"]) == 3
    assert seen[0]["roles"] == ["Northwind Research Lab", "Bristol Pro Bono Consulting"] and seen[0]["requirements"] == world.plan.reqs
    assert 200 <= CL.words([world.plan.p1, world.plan.p7]) + seen[0]["body_words"] <= 320


@pytest.mark.slow
def test_the_prompt_carries_the_paragraph_table_so_feedback_works_by_name(world):
    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append((system, user))
            return super().json(task, system, user, context)
    handler, _ = writer([body(world)])
    run(world, Spy({"cover_letter": handler}), revise={"previous": body(world), "problems": [], "feedback": "shorten P4"})
    system, user = sent[0]
    for name in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert re.search(rf"^{name} \(", user, re.M), name
    assert world.plan.p1 in user and world.plan.p7 in user and "P2 (what it shows, written in code): (none)" in user
    assert "THE READER'S FEEDBACK" in user and "shorten P4" in user and "P4: " + P4[:30] in user
    assert '{"paragraphs": ["P3", "P4", "P5"]}' in system and "ONE sentence per role" in system


@pytest.mark.slow
def test_a_good_first_draft_makes_the_pdf_and_letter_txt_with_the_paragraphs_in_order(world):
    handler, seen = writer([body(world)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert letter["status"] == "ok" and letter["mode"] == "company" and 200 <= letter["words"] <= 320 and letter["retry"]["outcome"] == "first draft"
    assert letter["names"] == ["P1", "P3", "P4", "P5", "P7"] and letter["paragraphs"][0] == world.plan.p1 and letter["paragraphs"][-1] == world.plan.p7
    assert pdf.is_file() and pdf_text(pdf).count("Could we spend 20 minutes") == 1
    assert [r["claim"] for r in letter["p5_sources"]] == sentences(P5) and letter["requirements"] == world.plan.reqs
    txt = (pdf.parent / "letter.txt").read_text(encoding="utf-8")
    assert txt.startswith("Hi Anaplan Functional Analytics team,\n\n") and txt.rstrip().endswith("Regards,\n\nAda Lovelace")
    assert all(p in txt for p in letter["paragraphs"])


SPECIAL = {"P3": "I cut the loss by ~5 pp at 100% recall for $5 & #1 of 5.5 GB_total {x} in file_name.", "P5": "The role is in 12_months of 3 ~ 4 work."}


@pytest.mark.slow
def test_a_letter_with_percent_and_tilde_compiles_and_letter_txt_matches_the_pdf_word_for_word(world, tmp_path):
    parts = {**letter_parts(world), **SPECIAL}
    pdf, letter = CL.write_letter(JOB, world.kws, parts, TEAM, world.fb, world.settings, tmp_path, "Ada Lovelace Cover Letter", {"headline": "", "number": ""})
    tex = pdf.with_suffix(".tex").read_text(encoding="utf-8")
    assert r"$\sim$5\,pp at 100\% recall for \$5 \& \#1 of 5.5\,GB\_total \{x\} in file\_name." in tex      # B6: the escapes, ~ as $\sim$, a number kept with its unit
    assert r"12\_months of 3 $\sim$ 4 work." in tex and letter["status"] == "ok" and pdf.is_file()
    norm = lambda s: " ".join(s.replace("\u223c", "~").replace("\u2009", " ").replace("\u00a0", " ").split())     # noqa: E731  (the math tilde, thin spaces)
    txt = (tmp_path / "letter.txt").read_text(encoding="utf-8")
    assert "~5 pp at 100% recall for $5 & #1 of 5.5 GB_total {x} in file_name." in txt and "$\\sim$" not in txt          # plain text: the words as written
    mine, theirs = norm(txt).split(), norm(pdf_text(pdf)).split()
    at = theirs.index(mine[0], theirs.index("Lovelace") + 1) if mine[0] in theirs else -1
    assert at >= 0 and theirs[at:at + len(mine)] == mine, [(a, c) for a, c in zip(mine, theirs[at:]) if a != c][:5]    # the same words, one by one


@pytest.mark.slow
def test_a_rejected_draft_is_redrafted_and_the_second_is_used(world):
    bad = body(world, P5="Anaplan serves insurance customers across Europe with 9,000 employees.")
    handler, seen = writer([bad, body(world)])
    llm = MockLLM({"cover_letter": handler})
    pdf, letter = run(world, llm)
    assert llm.calls == ["cover_letter", "cover_letter"] and letter["status"] == "ok" and letter["retry"]["outcome"] == "ok after redraft"
    assert letter["retry"]["count"] == 1 and len(letter["retry"]["failures"]) == 1 and pdf.is_file()


@pytest.mark.slow
def test_the_second_draft_is_told_every_rejection(world):
    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append(user)
            return super().json(task, system, user, context)
    bad = body(world, P5="Anaplan has more than 9,000 customers and its platform supports scenario planning and analysis.")
    handler, _ = writer([bad, body(world)])
    run(world, Spy({"cover_letter": handler}))
    assert "YOUR LAST DRAFT BROKE THESE RULES" not in sent[0] and "P5: remove the number(s) ['9000']: the ad does not state them" in sent[1]
    assert "YOUR PREVIOUS DRAFT" in sent[1] and "P5: Anaplan has more than 9,000" in sent[1]


def test_three_rejected_drafts_are_no_letter_and_the_reasons_are_shown(world):
    # three DIFFERENT rejected drafts (an identical one stops the tries after the second: tests/tailoring/test_letter_retries.py)
    bads = [body(world, 300, P4=f"{P4} " + " ".join(f"{c}{k}" for k in range(40)), P5="Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia.")
            for c in "xyz"]
    handler, seen = writer(bads)
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and letter["status"] == "none" and len(seen) == 3 == CL.ATTEMPTS and letter["retry"]["outcome"] == "no letter"
    assert "3 draft(s) rejected" in letter["reason"] and letter["draft_body"] == bads[2] and letter["opening"] == world.plan.p1 and letter["closing"] == world.plan.p7


def test_a_model_that_answers_with_the_old_shape_is_rejected_not_trusted(world):
    pdf, letter = run(world, MockLLM())                          # the default cover_letter answer has no company mode
    assert pdf is None and letter["status"] == "none"


def test_a_model_that_returns_five_paragraphs_is_rejected(world):
    handler, _ = writer([[*body(world), "a fourth", "a fifth"]])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert pdf is None and any("expected 3 paragraphs (P3, P4, P5), got 5" in p for p in letter["problems"])


def test_no_provider_for_the_task_leaves_the_letter_pending(world):
    from careerops.llm.router import TaskUnavailable

    def down(ctx):
        raise TaskUnavailable("cover_letter: no provider may take it now")
    pdf, letter = run(world, MockLLM({"cover_letter": down}))
    assert pdf is None and letter["status"] == "pending" and "no provider" in letter["reason"]


def test_a_no_hook_job_still_gets_a_letter_that_opens_with_the_family_profile_line(world):
    plan = CL.prepare(JOB, world.fb, world.kws, None, FAMILY_LINE, world.evidence, world.settings, world.claimable, world.chosen)
    handler, seen = writer([body(world)])                                    # (P3 and P4 are written from the bullets this plan leaves: not these)
    pdf, letter = run(world, MockLLM({"cover_letter": handler}), hk=None, family_line=FAMILY_LINE)
    assert seen[0]["opening"] == plan.p1 and plan.p1.endswith(FAMILY_LINE) and seen[0]["closing"] == plan.p7 and letter["opening"] == plan.p1


def test_a_job_with_no_numbered_fact_on_its_cv_has_no_letter_and_says_why(world):
    world.chosen = {b["id"] for sec in ("experience", "projects") for e in world.fb[sec] for b in e["bullets"] if not any(c.isdigit() for c in b["text"])}
    pdf, letter = run(world, MockLLM(), hk=None)
    assert pdf is None and letter["status"] == "none" and "nothing numbered to open the letter with" in letter["reason"]


# ------------------------------------------------------------------ P2 and P6 by name in the reader's feedback
def test_feedback_names_the_optional_paragraphs_in_code():
    assert CL.feedback_flags("drop P6, and shorten P4") == {"drop": {"P6"}, "keep": set()}
    assert CL.feedback_flags("Please remove the P2 and keep P6") == {"drop": {"P2"}, "keep": {"P6"}}
    assert CL.feedback_flags("shorten P4") == {"drop": set(), "keep": set()} and CL.feedback_flags("drop P4") == {"drop": set(), "keep": set()}


@pytest.mark.slow
def test_drop_p6_in_the_feedback_leaves_the_gap_out_and_it_stays_out_on_the_next_l(settings, fb_fixture):
    settings.raw["letter"]["honest_gap"] = True
    w = gap_world(settings, fb_fixture, possible=GAP_KEYS["possible"])
    parts = letter_parts(w, 260, gap=True)
    handler, _ = writer([[parts["P3"], parts["P4"], parts["P5"]]])
    pdf, letter = run(w, MockLLM({"cover_letter": handler}))
    assert letter["status"] == "ok" and "P6" in letter["names"] and letter["gap"]["included"] is True
    dropped = letter_parts(w, 240)
    handler, _ = writer([[dropped["P3"], dropped["P4"], dropped["P5"]]])
    pdf2, letter2 = run(w, MockLLM({"cover_letter": handler}), revise={"previous": body(w), "problems": [], "feedback": "drop P6", "dropped": []})
    assert letter2["status"] == "ok" and "P6" not in letter2["names"] and letter2["dropped"] == ["P6"] and letter2["gap"]["included"] is False
    rev = tailor.revision_for(settings, {"letter": letter2}, "")
    assert rev["dropped"] == ["P6"] and rev["gap_kept"] is False


# ------------------------------------------------------------------ review: keep or drop P6 without asking the model
@pytest.mark.slow
def test_review_keeps_and_drops_p6_by_rerendering_the_letter_without_the_model(settings, fb_fixture, tmp_path):
    w = gap_world(settings, fb_fixture, possible=GAP_KEYS["possible"])
    w.settings.raw["letter"]["honest_gap"] = False
    parts = letter_parts(w, 250)
    handler, _ = writer([[parts["P3"], parts["P4"], parts["P5"]]])
    pdf, letter = run(w, MockLLM({"cover_letter": handler}))
    assert "P6" not in letter["names"] and letter["gap"]["paragraph"] and letter["gap"]["included"] is False
    rep = {"letter": letter, "letter_pdf": str(pdf), "out_dir": str(pdf.parent), "team": TEAM, "context": {"id": w.fb["identity"]},
           "keywords": [k.__dict__ for k in w.kws]}
    shown = workflow.honest_gap_lines(letter)
    assert "NOT in the letter" in shown[0] and "[k]eep" in shown[1] and "[d]rop" in shown[1]
    new_pdf, kept, problems = CL.set_gap(w.job, w.fb, rep, w.settings, keep=True)
    assert problems == [] and "P6" in kept["names"] and kept["paragraphs"][kept["names"].index("P6")] == letter["gap"]["paragraph"]
    assert kept["words"] == letter["words"] + CL.words([letter["gap"]["paragraph"]]) and "IN the letter" in workflow.honest_gap_lines(kept)[0]
    assert "One honest gap" in pdf_text(new_pdf) and "One honest gap" in (pdf.parent / "letter.txt").read_text(encoding="utf-8")
    rep["letter"] = kept
    back_pdf, back, problems = CL.set_gap(w.job, w.fb, rep, w.settings, keep=False)
    assert problems == [] and "P6" not in back["names"] and back["words"] == letter["words"] and "One honest gap" not in pdf_text(back_pdf)
    assert "One honest gap" not in (pdf.parent / "letter.txt").read_text(encoding="utf-8")


def test_review_cannot_drop_a_gap_that_would_take_the_letter_under_200_words(settings, fb_fixture, tmp_path):
    w = gap_world(settings, fb_fixture, possible=GAP_KEYS["possible"])
    parts = letter_parts(w, 210, gap=True)
    letter = CL._letter_dict("ok", paragraphs=CL.assemble(parts)[0], names=CL.assemble(parts)[1], parts=parts, gap={**w.plan.gap, "included": True})
    rep = {"letter": letter, "keywords": [k.__dict__ for k in w.kws], "out_dir": str(tmp_path), "letter_pdf": str(tmp_path / "x.pdf"), "context": {}}
    pdf, same, problems = CL.set_gap(w.job, w.fb, rep, w.settings, keep=False)
    assert pdf is None and same is letter and problems[0].endswith("the letter must be between 200 and 320")


# ------------------------------------------------------------------ edits by hand
def test_an_edited_letter_is_checked_by_the_same_guards(world):
    bullets = [b for sec in ("experience", "projects") for e in world.fb[sec] for b in e["bullets"]]
    cv = {"experience": [{"bullet_ids": [b["id"] for b in bullets], "bullets": [b["text"] for b in bullets]}], "projects": [], "profile": "",
          "skills": {"All": [s for items in world.fb["skills"].values() for s in items]}}
    rep = {"keywords": [k.__dict__ for k in world.kws], "context": cv, "claimable": world.claimable, "team": TEAM, "hook": world.hk.to_dict(),
           "family_line": ""}
    assert CL.edit_problems(JOB, world.fb, rep, world.settings, letter_parts(world)) == []
    bad = {**letter_parts(world), "P9": "x"}
    assert CL.edit_problems(JOB, world.fb, rep, world.settings, bad) == ["unknown paragraph name(s): P9 (use P1 to P7)"]
    assert any("P5: claim about the company" in p for p in CL.edit_problems(JOB, world.fb, rep, world.settings, letter_parts(world, P5="Anaplan sells robotic lawnmowers.")))


# ------------------------------------------------------------------ the review screen
@pytest.mark.slow
def test_review_shows_the_word_count_the_paragraphs_and_the_ad_sentences_p5_drew_from(world):
    handler, _ = writer([body(world)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    lines = workflow.company_letter_lines(letter)
    text = "\n".join(lines)
    assert lines[0].startswith(f"Letter (company mode): {letter['words']} words (asked for 260; accepted 200-320)")
    assert "paragraphs: P1 P3 P4 P5 P7  (P2 left out: the fact bank has no limits or result for the hook)" in text
    assert "P5 (why this team) drew from the job ad:" in text and "integrate directly into Anaplan Applications" in text
    assert "P5 quotes or closely follows these About / role sentences of the ad:" in text
    assert workflow.company_letter_lines({**letter, "mode": "hook"}) == [] and "quotes or closely follows" not in "\n".join(
        workflow.company_letter_lines({**letter, "p5_used": []}))


def test_the_allowed_vocabulary_is_the_chosen_bullets_tools_plus_the_ads_skill_terms(world):
    text = CL.allowed_vocabulary(world.plan.evidence, world.kws)
    assert text.startswith("from the chosen bullets: ") and "the ad's skill terms: " in text and "Python" in text
