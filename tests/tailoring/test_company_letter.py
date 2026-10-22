"""letter.mode: company (tailoring/company_letter.py): P1 the hook's letter line (else the role family's profile line), P2 why this company (the
job ad is its only source), P3 and P4 two fact-bank bullets each tied to a named ad requirement, P5 closing_hook; 360-420 words; one redraft;
no letter (and review says why) after a second rejection. MockLLM only: no network, no model. The guards are called directly (fast); the
tests that make a PDF are marked slow (xelatex)."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_settings, load_yaml
from careerops.llm import MockLLM
from careerops.review import workflow
from careerops.tailoring import company_letter as CL
from careerops.tailoring import letters as L
from careerops.tailoring import tailor
from careerops.tailoring.grounding import sentences
from careerops.tailoring.letter_rules import i_clause, noun_phrase
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

# every claim below is the ad's own (see JD): the platform, the customers' aim, the team's mission, the 2,400+ companies
P2 = ("Anaplan builds an AI-infused scenario planning and analysis platform so its customers can outpace their competition and the market. "
      "The Functional Analytics team is hiring a Data Scientist to design and develop functional analytic capabilities that integrate directly "
      "into Anaplan Applications to solve complex enterprise business problems.")
PAD = ("beyond this example I enjoy talking through decisions with colleagues and explaining each step in plain language so others can follow "
       "the reasoning").split()


def pad(n: int) -> str:
    """Exactly n neutral words (no name, number, tool or claim) ending in a full stop."""
    return " ".join((PAD * (n // len(PAD) + 1))[:n]) + "." if n > 0 else ""


def keywords():
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


@pytest.fixture
def world(settings, fb_fixture):
    settings.raw["letter"]["mode"] = "company"
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = copy.deepcopy(LIBRARY)
    kws = keywords()
    hk = H.build_hook(JOB, kws, fb, role="Data Scientist")
    assert hk is not None
    chosen = {b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    evidence = tailor.letter_evidence(fb, kws, chosen, JD)
    claimable = [k.term for k in kws]
    plan = CL.prepare(JOB, fb, kws, hk, "", evidence, settings, claimable, chosen)
    assert not plan.error
    own = L.assemble_body(fb, plan.evidence, hk, kws, JD, "Anaplan")        # P3 and P4 first sentences, from their own bullets
    return SimpleNamespace(settings=settings, fb=fb, kws=kws, hk=hk, chosen=chosen, evidence=evidence, claimable=claimable, plan=plan, own=own)


def body(w, total: int = 400, p2: str = P2, p3: str | None = None, p4: str | None = None) -> list[str]:
    """[P2, P3, P4] that make the whole letter exactly `total` words, the padding in P3 and P4."""
    p3 = w.own[0] if p3 is None else p3
    p4 = w.own[1] if p4 is None else p4
    need = total - CL.words([w.plan.p1, p2, p3, p4, w.plan.p5])
    assert need >= 0, f"the base letter is already {total - need} words"
    return [p2, f"{p3} {pad(need // 2)}".strip(), f"{p4} {pad(need - need // 2)}".strip()]


def check(w, paragraphs: list[str]) -> list[str]:
    problems, _ = CL.check(w.plan.p1, paragraphs, w.plan.p5, JOB, w.fb, w.kws, w.plan.evidence, w.plan.reqs, TEAM, w.settings)
    return problems


# ------------------------------------------------------------------ the setting
def test_the_mode_is_known_and_the_shipped_settings_hold_the_new_keys(settings):
    settings.raw["letter"]["mode"] = "company"
    assert tailor.letter_mode(settings) == "company" and "company" in tailor.LETTER_MODES
    s = load_settings()
    assert s["letter"]["company_words"] == 400 and s["scoring"]["letter_max_words_company"] == 420
    assert s["scoring"]["letter_max_words"] == 250                          # the other modes keep theirs


@pytest.mark.parametrize("value", [360, 400, 420, "380"])
def test_company_words_between_360_and_420_are_accepted(settings, value):
    settings.raw["letter"]["company_words"] = value
    assert CL.target_words(settings) == int(value)


@pytest.mark.parametrize("value", [250, 359, 421, 500, "many", None, True])
def test_company_words_outside_the_range_are_refused(settings, value):
    settings.raw["letter"]["company_words"] = value
    with pytest.raises(ValueError, match="letter.company_words must be a whole number between 360 and 420"):
        CL.target_words(settings)


def test_the_word_cap_is_the_company_one_and_never_over_420(settings):
    assert CL.word_cap(settings) == 420
    settings.raw["scoring"]["letter_max_words_company"] = 500
    assert CL.word_cap(settings) == 420
    settings.raw["scoring"]["letter_max_words_company"] = 400
    assert CL.word_cap(settings) == 400 and settings["scoring"]["letter_max_words"] == 250


# ------------------------------------------------------------------ what P1 and P5 are
def test_p1_is_the_hooks_letter_line_word_for_word_and_p5_is_closing_hook_with_the_hooks_problem(world):
    assert world.plan.p1 == world.hk.letter_sentence
    assert world.plan.p5 == f"Could we take 20 minutes to talk about {world.hk.problem}?"


def test_with_no_hook_p1_is_the_family_profile_line_and_p5_names_the_ads_own_keywords(world):
    plan = CL.prepare(JOB, world.fb, world.kws, None, FAMILY_LINE, world.evidence, world.settings, world.claimable, world.chosen)
    assert not plan.error and plan.p1 == FAMILY_LINE
    assert plan.p5.startswith("Could we take 20 minutes to talk about ") and plan.p5.endswith("?") and "Python" in plan.p5


def test_with_neither_hook_nor_family_line_there_is_nothing_to_open_with(world):
    plan = CL.prepare(JOB, world.fb, world.kws, None, "", world.evidence, world.settings, world.claimable, world.chosen)
    assert "nothing to open with" in plan.error


def test_p3_and_p4_come_from_the_best_other_bullets_and_each_has_a_requirement_it_evidences(world):
    assert world.hk.proof_bullet_id not in {e["id"] for e in world.plan.evidence}
    assert len(world.plan.evidence) == 2 and all(r for r in world.plan.reqs)
    assert world.plan.reqs[0] == ["Python", "machine learning", "XGBoost"]


# ------------------------------------------------------------------ a good letter passes
def test_a_letter_of_the_right_shape_and_length_passes_both_guards(world):
    paragraphs = body(world, 400)
    assert CL.words([world.plan.p1, *paragraphs, world.plan.p5]) == 400
    assert check(world, paragraphs) == []


def test_p2_claims_that_the_ad_makes_pass_including_its_own_number(world):
    p2 = "Anaplan names Coca-Cola, LinkedIn, Adobe, LVMH and Bayer among the 2,400+ global companies who rely on its platform."
    assert CL.company_problems(p2, JD, CL.stems(f"Anaplan {JOB['title']} {TEAM}")) == []


# ------------------------------------------------------------------ the company guard
@pytest.mark.parametrize("claim", [
    "Anaplan sells robotic lawnmowers to regional hospitals across Scandinavia.",
    "Anaplan serves insurance and pharmaceutical customers in Europe.",
    "The Functional Analytics team builds a data warehouse for logistics partners.",
])
def test_a_p2_claim_the_ad_does_not_make_is_rejected(world, claim):
    problems = check(world, body(world, 400, p2=claim))
    assert any(p.startswith("P2: claim about the company is not supported by the ad") for p in problems), problems


@pytest.mark.parametrize("claim, number", [
    ("Anaplan has more than 9,000 customers and its platform supports scenario planning and analysis.", "9000"),
    ("Anaplan raised 250m last year to build its scenario planning and analysis platform.", "250m"),
    ("The Functional Analytics team of 12 designs functional analytic capabilities for customers.", "12"),
])
def test_a_company_number_the_ad_does_not_have_is_rejected(world, claim, number):
    problems = check(world, body(world, 400, p2=claim))
    assert any("number(s) about the company not in the ad" in p and number in p for p in problems), problems


@pytest.mark.parametrize("p2, name", [
    ("Anaplan builds an AI-infused scenario planning and analysis platform, led by Sarah Jones, so customers can outpace their competition.", "Sarah"),
    ("Anaplan builds a scenario planning and analysis platform, and the Insights Guild is hiring a Data Scientist to design analytic capabilities.", "Guild"),
])
def test_an_invented_person_or_team_name_is_rejected(world, p2, name):
    problems = check(world, body(world, 400, p2=p2))
    assert any(name in p and "(a name) is not in the ad" in p for p in problems), problems


def test_we_and_our_about_the_employer_and_keyword_lists_are_rejected(world):
    problems = check(world, body(world, 400, p2="At Anaplan we build an AI-infused scenario planning and analysis platform for our customers."))
    assert any("'we'" in p or "'our'" in p for p in problems), problems
    dump = "Python, SQL, XGBoost, PyTorch, MLflow, Optuna, Prefect, Snowflake, dbt."
    problems = check(world, body(world, 400, p3=world.own[0] + " " + dump))
    assert any("list of terms" in p or "repeated" in p or "lower case" in p for p in problems), problems


def test_the_review_sources_of_p2_are_the_ad_sentences_it_was_written_from(world):
    skip = CL.stems(f"Anaplan {JOB['title']} {TEAM}")
    rows = CL.p2_sources(P2, JD, skip)
    assert [r["claim"] for r in rows] == sentences(P2)
    assert "AI-infused scenario planning and analysis platform" in rows[0]["ad"][0] and "integrate directly into Anaplan Applications" in rows[1]["ad"][0]


# ------------------------------------------------------------------ the user guard
def test_a_user_number_that_is_not_in_the_chosen_bullets_is_rejected(world):
    p3 = world.own[0] + " That work cut the error rate by 83% for the team."
    problems = check(world, body(world, 400, p3=p3))
    assert any(p.startswith("P3: number(s) about you not in the chosen bullets") and "83%" in p for p in problems), problems


def test_a_number_that_is_in_the_chosen_bullet_is_fine(world):
    assert "7,043" in world.own[0]                        # the bullet's own number, in the first body sentence
    assert not [p for p in check(world, body(world, 400)) if "number(s)" in p]


def test_a_tool_or_organisation_that_is_not_the_bullets_is_rejected(world):
    p3 = world.own[0] + " I also ran the whole pipeline on Docker for the Harbour Pay Ltd team."
    problems = check(world, body(world, 400, p3=p3))
    assert any("names 'docker'" in p or "names 'Docker'" in p for p in problems), problems
    assert any("Harbour Pay Ltd" in p for p in problems), problems


def test_a_paragraph_must_name_an_ad_requirement_its_bullet_evidences(world):
    problems = check(world, body(world, 400, p3="Here is my closest example: I built a classifier on customer records and compared models carefully."))
    assert any(p.startswith("P3: does not name an ad requirement its bullet evidences") for p in problems), problems


def test_a_deny_listed_tool_is_rejected(world):
    problems = check(world, body(world, 400, p3=world.own[0] + " I would also bring Kubernetes to the work."))
    assert any("deny-listed 'kubernetes'" in p for p in problems), problems


def test_a_pasted_bullet_is_rejected(world):
    bullet = world.plan.evidence[0]["text"]
    problems = check(world, body(world, 400, p3=f"On machine learning, {bullet}"))
    assert any("pastes a fact-bank bullet" in p for p in problems), problems


# ------------------------------------------------------------------ the length
@pytest.mark.parametrize("total", [360, 361, 400, 419, 420])
def test_360_to_420_words_pass(world, total):
    paragraphs = body(world, total)
    assert CL.words([world.plan.p1, *paragraphs, world.plan.p5]) == total
    assert check(world, paragraphs) == []


@pytest.mark.parametrize("total", [250, 359, 421, 500])
def test_250_and_500_words_and_the_edges_around_the_range_are_rejected(world, total):
    paragraphs = body(world, total) if total >= 200 else None
    problems = check(world, paragraphs)
    assert f"{total} words: the letter must be between 360 and 420" in problems, problems


def test_a_lower_cap_in_settings_narrows_the_range(world):
    world.settings.raw["scoring"]["letter_max_words_company"] = 400
    assert check(world, body(world, 400)) == []
    assert any("401 words" in p for p in check(world, body(world, 401)))


def test_a_draft_without_three_paragraphs_is_rejected(world):
    problems, sources = CL.check(world.plan.p1, ["only one"], world.plan.p5, JOB, world.fb, world.kws, world.plan.evidence, world.plan.reqs, TEAM,
                                 world.settings)
    assert problems == ["expected 3 paragraphs (P2, P3, P4), got 1"] and sources == []


# ------------------------------------------------------------------ the model call: draft, redraft once, then no letter
def writer(world, responses: list):
    """A cover_letter handler that answers with the next entry of `responses` (a list of [P2, P3, P4]) and records the contexts it was given."""
    seen = []

    def handler(ctx):
        seen.append(ctx)
        return {"paragraphs": responses[min(len(seen), len(responses)) - 1]}
    return handler, seen


def run(world, llm, hk="hook", family_line=""):
    return CL.make(JOB, world.fb, world.kws, world.hk if hk == "hook" else None, family_line, world.evidence, llm, world.settings,
                   world.settings.path("outputs") / "company", "Ada Lovelace Cover Letter", TEAM, world.claimable, world.chosen)


@pytest.mark.slow
def test_the_model_is_asked_through_the_cover_letter_task_with_the_ad_and_the_two_evidence_bullets(world):
    handler, seen = writer(world, [body(world)])
    llm = MockLLM({"cover_letter": handler})
    pdf, letter = run(world, llm)
    assert llm.calls == ["cover_letter"] and seen[0]["mode"] == "company" and seen[0]["ad"] == JD
    assert seen[0]["opening"] == world.plan.p1 and seen[0]["closing"] == world.plan.p5 and len(seen[0]["evidence"]) == 2
    assert 360 <= CL.words([world.plan.p1, world.plan.p5]) + seen[0]["body_words"] <= 420


@pytest.mark.slow
def test_a_good_first_draft_makes_the_pdf_with_the_five_paragraphs_in_order(world):
    handler, seen = writer(world, [body(world)])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}))
    assert letter["status"] == "ok" and letter["mode"] == "company" and 360 <= letter["words"] <= 420 and letter["retry"]["outcome"] == "first draft"
    p = letter["paragraphs"]
    assert len(p) == 5 and p[0] == world.hk.letter_sentence and p[1] == P2 and p[4] == world.plan.p5
    assert pdf.is_file() and pdf_text(pdf).count("Could we take 20 minutes") == 1
    assert [r["claim"] for r in letter["p2_sources"]] == sentences(P2) and letter["requirements"] == world.plan.reqs


@pytest.mark.slow
def test_a_rejected_draft_is_redrafted_once_with_the_rejections_named_and_the_second_is_used(world):
    bad = body(world, 400, p2="Anaplan serves insurance customers across Europe with 9,000 employees.")
    handler, seen = writer(world, [bad, body(world)])
    llm = MockLLM({"cover_letter": handler})
    pdf, letter = run(world, llm)
    assert llm.calls == ["cover_letter", "cover_letter"] and letter["status"] == "ok" and letter["retry"]["outcome"] == "ok after redraft"
    assert letter["retry"]["count"] == 1 and len(letter["retry"]["failures"]) == 1 and pdf.is_file()


def test_two_rejected_drafts_are_no_letter_and_the_reason_is_recorded(world):
    bad = body(world, 400, p2="Anaplan serves insurance customers across Europe with 9,000 employees.")
    handler, seen = writer(world, [bad, bad])
    llm = MockLLM({"cover_letter": handler})
    pdf, letter = run(world, llm)
    assert pdf is None and letter["status"] == "none" and letter["paragraphs"] == [] and llm.calls == ["cover_letter"] * 2     # asked twice, never a third time
    assert "2 draft(s) rejected by the company guards" in letter["reason"] and "9000" in letter["reason"] + " ".join(letter["problems"])
    assert [f["attempt"] for f in letter["retry"]["failures"]] == [1, 2] and letter["retry"]["outcome"] == "no letter"
    assert any("redrafting once" in n for n in letter["notes"])


@pytest.mark.slow
def test_the_second_draft_is_told_every_rejection(world):
    sent = []

    class Spy(MockLLM):
        def json(self, task, system, user, context=None):
            sent.append(user)
            return super().json(task, system, user, context)
    bad = body(world, 400, p2="Anaplan has more than 9,000 customers and its platform supports scenario planning and analysis.")
    handler, seen = writer(world, [bad, body(world)])
    run(world, Spy({"cover_letter": handler}))
    assert "YOUR LAST DRAFT BROKE THESE RULES" not in sent[0] and "number(s) about the company not in the ad" in sent[1]


def test_a_model_that_answers_with_the_old_shape_is_rejected_not_trusted(world):
    llm = MockLLM()                                                  # the default cover_letter answer has no company mode
    pdf, letter = run(world, llm)
    assert pdf is None and letter["status"] == "none"


def test_no_provider_for_the_task_leaves_the_letter_pending(world):
    from careerops.llm.router import TaskUnavailable

    def down(ctx):
        raise TaskUnavailable("cover_letter: no provider may take it now")
    pdf, letter = run(world, MockLLM({"cover_letter": down}))
    assert pdf is None and letter["status"] == "pending" and "no provider" in letter["reason"]


# ------------------------------------------------------------------ a job with no hook still gets a letter
@pytest.mark.slow
def test_a_no_hook_job_still_gets_a_letter_that_opens_with_the_family_profile_line(world):
    plan = CL.prepare(JOB, world.fb, world.kws, None, FAMILY_LINE, world.evidence, world.settings, world.claimable, world.chosen)
    handler, seen = writer(world, [[P2, *_no_hook_body(world, plan)]])
    pdf, letter = run(world, MockLLM({"cover_letter": handler}), hk=None, family_line=FAMILY_LINE)
    assert letter["status"] == "ok", letter["problems"] or letter["reason"]
    assert letter["paragraphs"][0] == FAMILY_LINE and letter["paragraphs"][4] == plan.p5 and letter["hook_source"] == "family profile line"
    assert pdf.is_file() and 360 <= letter["words"] <= 420


def _no_hook_body(world, plan) -> list[str]:
    own = L.assemble_body(world.fb, plan.evidence, world.hk, world.kws, JD, "Anaplan")
    need = 400 - CL.words([plan.p1, P2, *own, plan.p5])
    return [f"{own[0]} {pad(need // 2)}", f"{own[1]} {pad(need - need // 2)}"]


def test_a_no_hook_job_with_no_family_line_has_no_letter_and_says_why(world):
    llm = MockLLM({"cover_letter": lambda ctx: pytest.fail("the model must not be asked when there is nothing to open the letter with")})
    pdf, letter = run(world, llm, hk=None, family_line="")
    assert pdf is None and letter["status"] == "none" and "nothing to open with" in letter["reason"]


# ------------------------------------------------------------------ the review screen
def test_review_shows_the_word_count_and_the_ad_sentences_p2_drew_from(world):
    skip = CL.stems(f"Anaplan {JOB['title']} {TEAM}")
    letter = {"mode": "company", "status": "ok", "words": 398, "target_words": 400, "hook_source": "library", "requirements": [["Python", "XGBoost"], ["PyTorch"]],
              "p2_sources": CL.p2_sources(P2, JD, skip)}
    lines = workflow.company_letter_lines(letter)
    text = "\n".join(lines)
    assert lines[0].startswith("Letter (company mode): 398 words (asked for 400; accepted 360-420)") and "Python, XGBoost; PyTorch" in lines[0]
    assert "P2 (why this company) drew from the job ad:" in text and "- Anaplan builds an AI-infused scenario planning" in text
    assert "ad: At Anaplan, we are a team of innovators focused on optimizing" in text
    assert workflow.company_letter_lines({"mode": "company", "status": "none"}) == [] and workflow.company_letter_lines({"status": "ok"}) == []


# ------------------------------------------------------------------ the whole pipeline
@pytest.mark.slow
def test_tailor_job_in_company_mode_without_a_hook_writes_the_letter_from_the_family_line(tmp_path):
    from conftest import fixture_profile_settings

    settings = fixture_profile_settings()
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge", "robots", "secrets",
              "discover", "aiapply_applied", "calibration", "frozen_keywords"):
        settings.raw["paths"][k] = str(tmp_path / Path(settings.raw["paths"][k]).name)
    settings.raw["paths"]["applications"] = str(tmp_path / "Companies")
    settings.raw["paths"]["tmp"] = str(tmp_path / "stage")
    settings.raw["letter"]["mode"] = "company"
    bank = load_yaml(HERE / "fixtures" / "golden" / "fact_bank_anaplan.yaml")
    bank.pop("hook_library", None)                                 # no hook for this ad: P1 is the role family's profile line
    bank["families"] = [{"id": "data_scientist", "match": ["data scientist", "machine learning", "forecasting"], "title": "Data Scientist",
                         "headline_stack": ["Python"], "profile_body": FAMILY_LINE,
                         "skill_lines": [{"label": "Python & Machine Learning", "items": "Python, pandas, scikit-learn, XGBoost, SQL"}]}]
    seen = []

    def handler(ctx):                                              # P3 and P4 from the evidence the pipeline chose, P2 from the ad
        seen.append(ctx)
        sentences_ = [f"On {noun_phrase([r[0]], ctx['ad'])}, here is my closest example: " + _first_person(t) for t, r in zip(ctx["evidence"], ctx["requirements"])]
        need = 400 - CL.words([ctx["opening"], P2, *sentences_, ctx["closing"]])
        return {"paragraphs": [P2, f"{sentences_[0]} {pad(need // 2)}", f"{sentences_[1]} {pad(need - need // 2)}"]}
    rep = tailor.tailor_job(JOB, bank, MockLLM({"cover_letter": handler}), settings, settings.path("outputs"))
    letter = rep["letter"]
    assert letter["status"] == "ok", letter.get("problems") or letter.get("reason")
    assert rep["letters"] is None and Path(rep["letter_pdf"]).is_file() and letter["mode"] == "company" and 360 <= letter["words"] <= 420
    assert rep["hook"] is None and rep["family_line"] == FAMILY_LINE and letter["paragraphs"][0] == FAMILY_LINE
    assert len(seen) == 1 and letter["hook_source"] == "family profile line"
    assert letter["pages"] == 1                                    # 400 words fit one page


def _first_person(bullet: str) -> str:
    return i_clause(L._fragments(bullet)[-1]) + "."          # the shortest cut of the bullet: the sentence stays well under the paste limit
