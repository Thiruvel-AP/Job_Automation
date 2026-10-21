"""Free-text form answers (apply/freetext.py): read the question and its limit, classify it, plan which fact-bank entries it may draw on, draft
with the model (MockLLM here), guard the draft as strictly as the CV, have the person approve it once, and reuse what they approved. No network,
no real model: `MockLLM` handlers stand in for the model and `ask` is a script."""
import copy
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX, SETTINGS_FIX

from careerops.apply import answers as answers_mod
from careerops.apply import freetext as ft
from careerops.config import load_yaml
from careerops.llm import MockLLM

FB = yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))
BOOK = answers_mod.load(PROFILE_FIX / "answers.yaml")
TODAY = date(2026, 10, 21)
JD = Path(PROFILE_FIX).parent.joinpath("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")
JOB = {"id": "manual:northwind1", "company": "Northwind Health AI", "title": "AI Engineer", "description": JD}
KEYWORDS = [{"term": "llm", "weight": 3.0, "required": True, "alts": []}, {"term": "langchain", "weight": 2.0, "required": True, "alts": []},
            {"term": "docker", "weight": 1.0, "required": False, "alts": []}]
REP = {"keywords": KEYWORDS, "hook": {"proof_bullet_id": "az2", "problem": "agents a team can trust", "profile_line": "", "hook_id": "h1"}}

PROJECT_Q = "Describe a forecasting project you are proud of"
GOOD = "On a customer churn project I built a scikit-learn and XGBoost model to find which customers would leave."
OK_LLM = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab for a multi-omics cancer research platform."


class Script:
    """Answers the prompts in order and remembers them; an exhausted script answers '' (nobody types anything)."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def llm_with(*drafts, ad_phrases=()):
    """A model that returns the drafts in order (the last one again when it runs out) and records every context it was given."""
    seen, queue = [], list(drafts)

    def handler(ctx):
        seen.append(ctx)
        return {"answer": queue.pop(0) if len(queue) > 1 else queue[0], "ad_phrases": list(ad_phrases)}
    llm = MockLLM({"free_text_answer": handler})
    llm.seen = seen
    return llm


@pytest.fixture
def make(settings):
    def build(llm, *replies, job=None, rep=None, book=BOOK, fb=FB):
        ask, out = Script(*replies), []
        f = ft.for_job({**JOB, **(job or {})}, rep if rep is not None else REP, fb, llm, settings, book, ask=ask, out=out.append, today=TODAY)
        f.script, f.lines = ask, out
        return f
    return build


def records(settings):
    p = settings.path("answers_library")
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------ the limit of a question
@pytest.mark.parametrize("text,maxlen,want", [
    ("What was the hardest part of building that feature (1-2 sentences)", None, (None, 2, None)),
    ("Tell us about it in 1 to 3 sentences", None, (None, 3, None)),
    ("Why us? Max 200 words", None, (200, None, None)),
    ("Please answer in no more than 150 words.", None, (150, None, None)),
    ("Anything else? (500 characters)", None, (None, None, 500)),
    ("Anything else?", 300, (None, None, 300)),
    ("Say more (max 80 words)", 400, (80, None, 400)),
    ("Describe your best project", None, (120, None, None)),             # nothing stated: 120 words
    ("Write at least 50 words about yourself", None, (120, None, None)),   # a minimum is not a maximum
])
def test_limits_are_read_from_the_label_and_the_field(text, maxlen, want):
    lim = ft.read_limits(text, maxlen)
    assert (lim.words, lim.sentences, lim.chars) == want
    assert lim.stated is (want != (120, None, None))


def test_a_limit_names_the_ways_a_text_breaks_it():
    lim = ft.read_limits("(1-2 sentences)")
    assert lim.problems("One. Two. Three.") and not lim.problems("One. Two.")
    assert ft.read_limits("max 5 words").problems("one two three four five six")
    assert ft.read_limits("", 20).problems("x" * 21)


# ------------------------------------------------------------------ the class of a question
@pytest.mark.parametrize("q,cls", [
    ("Describe an LLM feature you've built, deployed, and maintain", "project"),
    ("Tell us about an LLM system you shipped", "project"),
    ("What was the hardest part of building that feature (1-2 sentences)", "challenge"),
    ("What is the most challenging problem you have solved?", "challenge"),
    ("Why Northwind Health AI?", "why_company"),
    ("Why do you want to work for us?", "why_company"),
    ("Why are you interested in this role?", "why_role"),
    ("Why did you apply for this position?", "why_role"),
    ("What are your hobbies?", "other"),
    ("Anything else you would like us to know?", "other"),
])
def test_questions_are_classified(q, cls):
    assert ft.classify(q, "Northwind Health AI") == cls


# ------------------------------------------------------------------ is a field a free-text field?
@pytest.mark.parametrize("typ,maxlen,yes", [("textarea", None, True), ("textarea", "2000", True), ("text", "255", True), ("text", None, False),
                                            ("text", "60", False), ("email", "500", False), ("select", None, False)])
def test_which_fields_are_free_text(typ, maxlen, yes):
    assert ft.is_free_text(typ, maxlen) is yes


# ------------------------------------------------------------------ a similar question
def test_a_paraphrase_is_similar_and_a_different_question_is_not():
    a = "Describe an LLM feature you've built, deployed, and maintain"
    assert ft.similarity(a, "Tell us about an LLM system you shipped") >= 0.85
    assert ft.similarity(a, a.upper()) == 1.0
    assert ft.similarity(a, "Describe a computer vision system you built") < 0.85
    assert ft.similarity(a, "Why do you want to work here?") < 0.5


# ------------------------------------------------------------------ the library
SEED_Q1 = "Describe an LLM feature you've built, deployed, and maintain"
SEED_A1 = ("At AstraZeneca I built a tool-calling LLM agent (LangChain, Claude Haiku 4.5 on AWS Bedrock) for a cancer research platform and "
           "deployed it as a Docker image on AWS Lambda with least-privilege IAM. I measured it with a three-tier, ~65-test evaluation harness "
           "with hallucination-rate analysis, and reused the same harness to validate the migration from Groq to Bedrock after a rate-limit "
           "evaluation. It fails safe with circuit breakers and a 19,213-gene local parquet fallback when the external APIs are unavailable.")
SEED_Q2 = "What was the hardest part of building that feature (1-2 sentences)"
SEED_A2 = ("Making it trustworthy rather than just functional: failures only became visible once I tested layer by layer (tool, API, then live), "
           "and each one needed a deterministic guard or fallback instead of a prompt tweak. Balancing that against the rate limits and outages "
           "of the external APIs it depends on took the most iteration.")


def test_the_library_starts_with_the_two_approved_limbic_answers_word_for_word(settings):
    lib = ft.Library(settings.path("answers_library"))
    recs = lib.records()
    assert [(r.question, r.answer) for r in recs] == [(SEED_Q1, SEED_A1), (SEED_Q2, SEED_A2)]
    assert all(r.approved and r.job_id == "manual:177a9a1afcc1" and r.company == "Limbic" and r.date == "2026-10-21" for r in recs)
    assert [r.cls for r in recs] == ["project", "challenge"]
    on_disk = records(settings)
    assert on_disk[0]["answer"] == SEED_A1 and on_disk[0]["approved"] is True and on_disk[0]["normalised"]
    assert {"question", "normalised", "answer", "company", "job_id", "source_ids", "date", "approved"} <= set(on_disk[0])
    assert len(ft.Library(settings.path("answers_library")).records()) == 2          # seeded once, not on every open


def test_a_paraphrased_question_finds_the_seeded_answer(settings):
    lib = ft.Library(settings.path("answers_library"))
    hit = lib.find("Tell us about an LLM system you shipped", "project", "Northwind Health AI", ft.read_limits("", None))
    assert hit is not None and hit.answer == SEED_A1
    assert lib.find("Tell us about an LLM system you shipped", "challenge", "Northwind", ft.read_limits("", None)) is None     # same class only
    assert lib.find("Describe a computer vision system you built", "project", "X", ft.read_limits("", None)) is None


def test_a_stored_answer_that_breaks_the_new_limit_is_not_reused(settings):
    lib = ft.Library(settings.path("answers_library"))
    assert lib.find(SEED_Q2, "challenge", "X", ft.read_limits("(1-2 sentences)")) is not None        # two sentences fit "1-2 sentences"
    assert lib.find(SEED_Q2, "challenge", "X", ft.read_limits("(1 sentence)")) is None               # one sentence: the seed has two


def test_a_why_company_answer_is_only_ever_found_for_the_same_company(settings):
    lib = ft.Library(settings.path("answers_library"))
    lib.add(ft.Record.new("Why Northwind Health AI?", "I like the clinical decision-support work.", "why_company", "Northwind Health AI",
                          "manual:northwind1", ["ad"], TODAY))
    q, lim = "Why do you want to work for Northwind Health AI?", ft.read_limits("")
    assert lib.find(q, "why_company", "Northwind Health AI", lim) is not None
    assert lib.find(q, "why_company", "Other Co", lim) is None


# ------------------------------------------------------------------ the plan
def test_the_plan_prefers_the_hook_proof_bullet_of_this_cvs_job(make):
    f = make(llm_with(OK_LLM), "y")
    plan = f.plan("project", "Describe an LLM feature you have built")
    assert plan.sources[0].id == "az2" and 1 <= len(plan.sources) <= 3
    assert "hook" in plan.why
    no_hook = make(llm_with(OK_LLM), rep={"keywords": KEYWORDS})
    assert "az1" in [s.id for s in no_hook.plan("project", "Describe an LLM feature you have built").sources]


def test_a_challenge_is_planned_from_entries_with_a_situation_or_a_result(make):
    ids = [s.id for s in make(llm_with(OK_LLM)).plan("challenge", "What was the hardest part of the project?").sources]
    assert ids and all(i.startswith(("az", "churn")) for i in ids)          # the entries of the fixture bank that carry a `situation`


# ------------------------------------------------------------------ draft, guard, approval
def test_a_draft_that_passes_is_shown_with_its_sources_and_stored_only_when_the_person_says_y(make, settings):
    f = make(llm_with(OK_LLM), "y")
    got = f.answer("Describe an LLM feature you are proud of", "", None)
    assert got == OK_LLM
    shown = "\n".join(f.lines)
    assert "Describe an LLM feature you are proud of" in shown and OK_LLM in shown and "az2" in shown and "Guard: passed" in shown
    assert "120 words" in shown and "[y] use" in f.script.prompts[0] and "[e] edit" in f.script.prompts[0] and "[s]" in f.script.prompts[0]
    (rec,) = [r for r in records(settings) if r["company"] == "Northwind Health AI"]
    assert rec["answer"] == OK_LLM and rec["approved"] is True and rec["job_id"] == "manual:northwind1" and rec["source_ids"][0] == "az2"
    assert rec["date"] == "2026-10-21" and rec["question"] == "Describe an LLM feature you are proud of"


@pytest.mark.parametrize("reply", ["", "n", "no", "yes please", "maybe", "Y!"])
def test_anything_but_y_is_not_approval_and_nothing_is_stored(make, settings, reply):
    f = make(llm_with(OK_LLM), reply, "")
    assert f.answer("Describe an LLM feature you are proud of", "", None) == ""
    assert [r for r in records(settings) if r["company"] == "Northwind Health AI"] == []


def test_a_draft_with_a_number_that_is_not_in_the_sources_is_redrafted_once_with_the_problem(make):
    bad = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab that served 3.1m users."
    llm = llm_with(bad, OK_LLM)
    f = make(llm, "y")
    assert f.answer("Describe an LLM feature you are proud of", "", None) == OK_LLM
    assert len(llm.seen) == 2 and not llm.seen[0].get("problems")
    assert any("3.1m" in p for p in llm.seen[1]["problems"])
    assert "Guard: passed" in "\n".join(f.lines) and len(f.script.prompts) == 1               # the person saw the good draft only


def test_two_failed_drafts_go_to_the_person_with_no_draft(make, settings):
    bad = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab that served 3.1m users."
    llm = llm_with(bad)
    f = make(llm, "I built an agent for a research team.")
    got = f.answer("Describe an LLM feature you are proud of", "", None)
    assert got == "I built an agent for a research team." and len(llm.seen) == 2
    assert not any("[y] use" in p for p in f.script.prompts) and "3.1m" not in got
    (rec,) = [r for r in records(settings) if r["company"] == "Northwind Health AI"]
    assert rec["answer"] == got and rec["approved"] is True                                     # what the person wrote is stored


@pytest.mark.parametrize("draft,why", [
    ("I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab and ran it on Kubernetes.", "kubernetes"),
    ("I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Google for a multi-omics cancer research platform.", "google"),
    ("I built a tool-calling LLM agent (LangChain, AWS Bedrock) with Airflow at Northwind Research Lab.", "airflow"),
    ("I was passionate about building a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab.", "passionate"),
    ("We built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab.", "we"),
    ("The team built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab.", "first person"),
    ("", "empty"),
])
def test_the_guard_rejects(make, draft, why):
    llm = llm_with(draft)
    f = make(llm, "")
    assert f.answer("Describe an LLM feature you are proud of", "", None) == ""                  # both drafts fail, the person writes nothing
    assert len(llm.seen) == 2 and not any("[y] use" in p for p in f.script.prompts)
    assert why in " ".join(llm.seen[1]["problems"]).lower()


def test_the_limit_is_part_of_the_guard(make):
    three = OK_LLM + " It ran for the whole team. It never failed."
    llm = llm_with(three, OK_LLM)
    f = make(llm, "y")
    assert f.answer("Describe an LLM feature you are proud of (1-2 sentences)", "", None) == OK_LLM
    assert any("sentence" in p for p in llm.seen[1]["problems"])


def test_edit_redraft_and_write_myself(make, settings):
    drafts = ["I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab.", OK_LLM]
    f = make(llm_with(*drafts), "r", "e", "I wrote my own text about the agent.")
    assert f.answer("Describe an LLM feature you are proud of", "", None) == "I wrote my own text about the agent."
    assert [r["answer"] for r in records(settings) if r["company"] == "Northwind Health AI"] == ["I wrote my own text about the agent."]
    g = make(llm_with(OK_LLM), "s", "My own answer.")
    assert g.answer("Describe a data project you are proud of", "", None) == "My own answer."


def test_the_assist_never_types_for_the_person_when_they_choose_s(make):
    f = make(llm_with(OK_LLM), "s")
    assert f.answer("Describe an LLM feature you are proud of", "", None, strict=False) == "" and len(f.script.prompts) == 1


def test_a_question_of_class_other_is_asked_not_drafted(make, settings):
    llm = llm_with(OK_LLM)
    f = make(llm, "I enjoy long-distance running.")
    assert f.answer("What are your hobbies?", "", None) == "I enjoy long-distance running." and llm.seen == []
    quiet = make(llm_with(OK_LLM))
    assert quiet.answer("Anything else you want us to know?", "", None, strict=False) == "" and quiet.script.prompts == []


# ------------------------------------------------------------------ reuse
def test_an_approved_answer_is_reused_for_a_similar_question_without_asking_or_drafting(make):
    first = make(llm_with(OK_LLM), "y")
    first.answer("Describe an LLM feature you are proud of", "", None)
    llm = llm_with("never used")
    again = make(llm)
    assert again.answer("Tell us about an LLM feature you are proud of", "", None) == OK_LLM
    assert llm.seen == [] and again.script.prompts == [] and again.last == "reused"


def test_the_seeded_answer_pre_fills_a_paraphrase_of_its_question(make):
    llm = llm_with("never used")
    f = make(llm)
    assert f.answer("Tell us about an LLM system you shipped", "", None) == SEED_A1
    assert llm.seen == [] and f.script.prompts == [] and f.last == "reused"
    assert f.answer("What was the hardest part of building that feature (1-2 sentences)", "", None) == SEED_A2


def test_a_new_draft_is_not_reuse_and_a_reused_answer_is_not_a_new_draft(make):
    f = make(llm_with(OK_LLM), "y")
    f.answer(PROJECT_Q, "", None)
    assert f.last == "approved"
    g = make(llm_with("x"))
    g.answer(PROJECT_Q, "", None)
    assert g.last == "reused"


def test_why_company_without_motivations_asks_and_does_not_draft(make):
    bare = answers_mod.AnswerBook(BOOK.entries, [], BOOK.style)
    llm = llm_with("I like Northwind.")
    f = make(llm, "I like their oncology work.", book=bare)
    assert f.answer("Why Northwind Health AI?", "", None) == "I like their oncology work." and llm.seen == []
    assert any("motivation" in line.lower() for line in f.lines)


AD_PHRASE = "builds clinical decision-support tools that help oncology researchers"
WHY = (f"Northwind Health AI {AD_PHRASE} find the right cancer cell-line models faster, and I want to build AI systems that are "
       "trustworthy in production.")


def test_why_company_draws_on_the_ad_and_the_motivations_and_quotes_the_ad(make):
    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    f = make(llm, "y")
    assert f.answer("Why Northwind Health AI?", "", None) == WHY
    ctx = llm.seen[0]
    assert ctx["class"] == "why_company" and ctx["motivations"] and ctx["ad_snippets"] and AD_PHRASE.split()[0] in " ".join(ctx["ad_snippets"])


@pytest.mark.parametrize("draft,phrases", [
    ("Northwind Health AI is the leading provider of clinical AI in Europe.", ["leading provider of clinical AI in Europe"]),   # not in the ad
    ("I admire Northwind Health AI and want to build AI systems that are trustworthy in production.", [AD_PHRASE]),                # ad phrase not used
    (f"Northwind Health AI {AD_PHRASE} and has 400 staff.", [AD_PHRASE]),                                                           # a number from nowhere
])
def test_a_claim_about_the_company_must_be_a_phrase_of_the_ad(make, draft, phrases):
    llm = llm_with(draft, ad_phrases=phrases)
    f = make(llm, "")
    assert f.answer("Why Northwind Health AI?", "", None) == "" and len(llm.seen) == 2
    assert not any("[y] use" in p for p in f.script.prompts)


def test_a_why_company_answer_is_never_reused_across_companies(make):
    first = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    first.answer("Why Northwind Health AI?", "", None)
    again = make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y")
    assert again.answer("Why Northwind Health AI?", "", None) == WHY and again.last == "reused"          # the same company: yes
    other = make(llm_with("x"), "", job={"company": "Other Co", "id": "manual:o1"})
    assert other.answer("Why Other Co?", "", None) == "" and other.last != "reused"
    unrelated = make(llm_with("x"), "", job={"company": "Other Co", "id": "manual:o1"})
    assert unrelated.answer("Why Northwind Health AI?", "", None) == "" and unrelated.last != "reused"


def test_why_role_draws_on_the_ad_and_the_hook(make):
    why_role = ("The role asks me to build and evaluate LLM agents on top of a research data platform, "
                "which is the problem I solved with an LLM agent at Northwind Research Lab.")
    llm = llm_with(why_role, ad_phrases=["build and evaluate LLM agents"])
    f = make(llm, "y")
    assert f.answer("Why are you interested in this role?", "", None) == why_role
    assert llm.seen[0]["class"] == "why_role" and "agents a team can trust" in json.dumps(llm.seen[0]["sources"])


def test_the_prompt_carries_the_style_and_the_limit(make):
    llm = llm_with(OK_LLM)
    make(llm, "y").answer("Describe an LLM feature you are proud of (1-2 sentences)", "", None)
    ctx = llm.seen[0]
    assert ctx["style"]["avoid"] == ["passionate", "excited", "leverage", "synergy", "cutting-edge"] and ctx["style"]["person"] == "first"
    assert ctx["limit"]["sentences"] == 2 and [s["id"] for s in ctx["sources"]][0] == "az2"


def test_a_model_that_fails_leaves_the_question_to_the_person(make):
    def boom(ctx):
        raise RuntimeError("no budget")
    f = make(MockLLM({"free_text_answer": boom}), "I built an agent.")
    assert f.answer("Describe an LLM feature you are proud of", "", None) == "I built an agent."
    assert any("no budget" in line for line in f.lines)


def test_the_library_is_runtime_data_not_answers_yaml(settings, make):
    before = (PROFILE_FIX / "answers.yaml").read_bytes()
    make(llm_with(OK_LLM), "y").answer("Describe an LLM feature you are proud of", "", None)
    assert (PROFILE_FIX / "answers.yaml").read_bytes() == before and settings.path("answers_library").is_file()
    assert "answers_library" in load_yaml(SETTINGS_FIX)["paths"]               # the test copy: the real settings file is never read


def test_freetext_only_reads_the_users_files_and_writes_only_the_library():
    src = Path(ft.__file__).read_text(encoding="utf-8")
    assert "answers_mod.save" not in src and ".save(" not in src                      # answers.yaml: only the `save` prompt of the filler appends to it
    assert src.count("open(") == 2 and 'open(self.path, "x"' in src and 'open(self.path, "a"' in src      # the library: created once, appended to
    assert "write_text" not in src and "yaml" not in src.replace("answers.yaml", "").replace("fact_bank.yaml", "").replace("applicant.yaml", "").replace(".yaml", "")


# ------------------------------------------------------------------ the claim guard: the ad is never evidence about you
def with_ad(extra: str) -> dict:
    return {"description": JD + "\n" + extra}


@pytest.mark.parametrize("tool", ["Databricks", "Palantir Foundry"])
@pytest.mark.parametrize("question,draft_for", [
    ("Describe an LLM feature you are proud of", "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab on {tool}."),
    ("Why are you interested in this role?", "I want to build and evaluate LLM agents on a research data platform, and I used {tool} for that at Northwind Research Lab."),
    ("Why Northwind Health AI?", f"Northwind Health AI {AD_PHRASE}, and I used {{tool}} to do the same kind of work."),
])
def test_a_draft_that_claims_a_tool_only_the_job_ad_names_is_rejected(make, tool, question, draft_for):
    """The ad may be quoted for claims about the company and the role, never for what the user did or used."""
    draft = draft_for.format(tool=tool)
    llm = llm_with(draft, ad_phrases=[AD_PHRASE])
    f = make(llm, "", job=with_ad(f"You will work with {tool} every day."))
    assert tool in f.ad and tool.lower() not in ft._fact_corpus(FB).lower()           # it is in the ad and nowhere in the user's sources
    assert f.answer(question, "", None) == "" and len(llm.seen) == 2                    # both drafts fail: nothing is offered
    assert not any("[y] use" in p for p in f.script.prompts)
    assert tool.split()[0].lower() in " ".join(llm.seen[1]["problems"]).lower()
    assert not [r for r in f.lib.records() if tool in r.answer]


def test_the_same_tool_is_fine_once_it_is_in_the_fact_bank(make):
    fb = copy.deepcopy(FB)
    fb["skills"]["Data & Cloud"].append("Databricks")
    fb["experience"][0]["bullets"][0]["text"] += " Ran the evaluation jobs on Databricks."
    draft = "I built a tool-calling LLM agent (LangChain, AWS Bedrock) at Northwind Research Lab and ran the evaluation jobs on Databricks."
    f = make(llm_with(draft), "y", job=with_ad("You will work with Databricks every day."), fb=fb)
    assert f.answer("Describe an LLM feature you are proud of", "", None) == draft


# ------------------------------------------------------------------ a stored answer goes stale when its sources change
Q_NEW = "Tell us about an LLM feature you are proud of"
Q_OLD = "Describe an LLM feature you are proud of"


def approve_one(make, settings):
    first = make(llm_with(OK_LLM), "y")
    assert first.answer(Q_OLD, "", None) == OK_LLM and first.last == "approved"
    (rec,) = [r for r in records(settings) if r["company"] == "Northwind Health AI"]
    return rec


def reuse_attempt(make, **kw):
    llm = llm_with("a fresh draft: never shown")
    return make(llm, **kw), llm


def edit_bullet(fb, bullet_id, **change):
    for sec in ("experience", "projects"):
        for e in fb[sec]:
            for b in e["bullets"]:
                if b["id"] == bullet_id:
                    b.update({k: b.get(k, "") + v for k, v in change.items()})


def test_an_approved_answer_records_what_it_was_built_from(make, settings):
    rec = approve_one(make, settings)
    assert rec["source_ids"] and set(rec["digests"]) == set(rec["source_ids"]) and all(len(d) == 64 for d in rec["digests"].values())
    again, llm = reuse_attempt(make)
    assert again.answer(Q_NEW, "", None) == OK_LLM and again.last == "reused" and llm.seen == []       # unchanged: reused


def test_a_stale_library_answer_is_not_reused_when_a_fact_bank_bullet_it_came_from_changed(make, settings):
    rec = approve_one(make, settings)
    fb = copy.deepcopy(FB)
    edit_bullet(fb, rec["source_ids"][0], text=" Also presented it at a conference.")
    again, llm = reuse_attempt(make, fb=fb)
    again.answer(Q_NEW, "", None)
    assert again.last != "reused" and len(llm.seen) >= 1                                              # drafted afresh, never the old text


def test_a_stale_library_answer_is_not_reused_when_the_entry_around_the_bullet_changed(make, settings):
    rec = approve_one(make, settings)
    fb = copy.deepcopy(FB)
    for sec in ("experience", "projects"):
        for e in fb[sec]:
            if any(b["id"] == rec["source_ids"][0] for b in e["bullets"]):
                e["situation"] = "A different situation than the one the answer was approved against"
    again, llm = reuse_attempt(make, fb=fb)
    again.answer(Q_NEW, "", None)
    assert again.last != "reused" and llm.seen


def test_a_stale_library_answer_is_not_reused_when_a_source_entry_was_removed(make, settings):
    rec = approve_one(make, settings)
    fb = copy.deepcopy(FB)
    for sec in ("experience", "projects"):
        for e in fb[sec]:
            e["bullets"] = [b for b in e["bullets"] if b["id"] != rec["source_ids"][0]]
    again, llm = reuse_attempt(make, fb=fb)
    again.answer(Q_NEW, "", None)
    assert again.last != "reused" and llm.seen


def test_a_change_to_an_entry_the_answer_did_not_use_leaves_it_reusable(make, settings):
    rec = approve_one(make, settings)
    fb = copy.deepcopy(FB)
    other = next(b["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if b["id"] not in rec["source_ids"])
    edit_bullet(fb, other, text=" Changed after approval.")
    again, llm = reuse_attempt(make, fb=fb)
    assert again.answer(Q_NEW, "", None) == OK_LLM and again.last == "reused" and llm.seen == []


def test_an_answer_stored_without_a_record_of_its_sources_is_not_reused(make, settings):
    """A library line written before digests were kept cannot be shown to be unchanged: it is asked about again, not reused."""
    approve_one(make, settings)
    path = settings.path("answers_library")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for d in lines:
        d.pop("digests", None)
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
    again, llm = reuse_attempt(make)
    again.answer(Q_NEW, "", None)
    assert again.last != "reused" and llm.seen


def test_the_seeded_answers_have_no_source_of_yours_to_go_stale(make):
    fb = copy.deepcopy(FB)
    for e in fb["experience"]:
        e["situation"] = "changed"
    f = make(llm_with("never used"), fb=fb)
    assert f.answer("Tell us about an LLM system you shipped", "", None) == SEED_A1 and f.last == "reused"


def test_a_why_company_answer_goes_stale_when_a_motivation_changed(make):
    make(llm_with(WHY, ad_phrases=[AD_PHRASE]), "y").answer("Why Northwind Health AI?", "", None)
    same = make(llm_with("x"))
    assert same.answer("Why Northwind Health AI?", "", None) == WHY and same.last == "reused"
    changed = answers_mod.AnswerBook(BOOK.entries, ["I now want something else entirely."] + list(BOOK.motivations[1:]), BOOK.style)
    llm = llm_with(WHY, ad_phrases=[AD_PHRASE])
    again = make(llm, "", book=changed)
    again.answer("Why Northwind Health AI?", "", None)
    assert again.last != "reused" and llm.seen

