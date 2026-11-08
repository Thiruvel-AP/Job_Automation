"""v3.4: letter quality after the RightShip letter of 3 Nov 2026 (a duplicated MCP sentence, "What draws me to this role is RightShip is ...", a
70-word sentence, P2 "The result, as measured: ~65 tests across 8 files" after P1's "~65 tests").

The assembled draft stays the fact source; a fluency pass lets the model rewrite it under code checks (facts, <= 35 words a sentence, no repeats,
no banned opening, every existing guard), two tries, then the draft. The templates were fixed too. MockLLM only; the PDF step is a recorder."""
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import assembled_letter as AL
from careerops.tailoring import company_letter as CL
from careerops.tailoring import fluency as FL
from careerops.tailoring.grounding import sentences
from test_company_letter import P5, TEAM, make_world

RIGHTSHIP_AZ10 = ("Needed an agent that researchers could trust on a cancer research platform, including when its tools fail: built a tool-calling "
                  "agent (LangChain, Claude Haiku 4.5 on AWS Bedrock) for three use cases.")
RIGHTSHIP_ABOUT = ["RightShip is the maritime industry's leading safety and risk intelligence platform.",
                   "We exist to advance the vision of a Zero Harm maritime industry by enabling better safety decisions across the ecosystem."]


@pytest.fixture
def world(settings, fb_fixture, monkeypatch):
    w = make_world(settings, fb_fixture)
    w.settings.raw["letter"]["compose"] = "assembled"
    w.written = []

    def record(job, kws, parts, team, fb, settings, out_dir, final_name, header=None, gap=None, **extra):
        w.written.append(dict(parts))
        paragraphs, names = CL.assemble(parts)
        return Path(out_dir) / f"{final_name}.pdf", CL._letter_dict("ok", paragraphs=paragraphs, names=names, parts=dict(parts),
                                                                   words=CL.words(paragraphs), **extra)
    monkeypatch.setattr(CL, "write_letter", record)
    return w


def model(rewrite):
    """P5 from the model (P5), and the fluency call answered by `rewrite(draft) -> dict` (or an exception)."""
    seen = []

    def handler(ctx):
        seen.append(ctx)
        if ctx.get("mode") == "fluency":
            out = rewrite(dict(ctx["draft"]))
            if isinstance(out, Exception):
                raise out
            return out
        return {"p5": P5}
    llm = MockLLM({"cover_letter": handler})
    llm.seen = seen
    return llm


def make(w, llm):
    return CL.make(w.job, w.fb, w.kws, w.hk, "", w.evidence, llm, w.settings, w.settings.path("outputs") / "fluency",
                   "Ada Lovelace Cover Letter", TEAM, w.claimable, w.chosen, possible=w.possible)


def shorter_quote(draft: dict) -> dict:
    """A good rewrite: P1's quoted ad phrase cut from the end (still the ad's own words) until P1 is 35 words or fewer, the rest as it is."""
    p1 = draft["P1"]
    q = p1.split('"')[1]
    words = q.split()
    while len(p1.replace(q, " ".join(words)).split()) > 35 and len(words) > 3:
        words = words[:-1]
    return {**draft, "P1": p1.replace(q, " ".join(words))}


def smooth(draft: dict) -> dict:
    """A good rewrite that changes wording only: "At <employer>, I ..." -> "While at <employer>, I ..." (plus shorter_quote)."""
    d = shorter_quote(draft)
    return {**d, "P4": d["P4"].replace("At ", "While at ", 1)}


# ------------------------------------------------------------------ templates (item 2)
def test_a_problem_action_bullet_becomes_one_sentence_never_i_needed_colon_built():
    out = AL.first_person(RIGHTSHIP_AZ10)
    assert out.startswith("To deliver an agent that researchers could trust") and ", I built a tool-calling agent" in out
    assert "I needed" not in out and ": built" not in out
    assert AL.first_person("A product-startup client needed an agentic AI prototype: led the architecture for a five-person team.") == \
        "For a product-startup client that needed an agentic AI prototype, I led the architecture for a five-person team."
    assert AL.first_person("Built the Python data pipeline: reconciled 14 source datasets") == \
        "I built the Python data pipeline and reconciled 14 source datasets."
    assert AL.first_person("Prototyped a tool in Python (FastMCP, Ollama gpt-oss:20b)") == "I prototyped a tool in Python (FastMCP, Ollama gpt-oss:20b)."
    assert AL.first_person("Problem: nothing here is a verb after the colon") == "Problem: nothing here is a verb after the colon."


def test_a_problem_action_bullet_in_p4_reads_at_employer_to_deliver():
    e = {"id": "az10", "text": RIGHTSHIP_AZ10, "org": "AstraZeneca, United Kingdom"}
    assert AL.at_role(e).startswith("At AstraZeneca, to deliver an agent that researchers could trust")


def test_the_p5_fallback_is_the_noun_phrase_frame_never_is_is():
    cands = AL.p5_candidates(RIGHTSHIP_ABOUT, "RightShip")
    assert cands[0] == "RightShip's focus on safety and risk intelligence is why I applied."
    assert not any("is RightShip is" in c or "role is RightShip" in c for c in cands)
    assert AL.noun_phrases(RIGHTSHIP_ABOUT, "RightShip")[:2] == ["safety and risk intelligence", "a Zero Harm maritime industry"]
    assert AL.possessive("Acme Systems") == "Acme Systems'"


def test_a_bullet_in_both_p3_and_p4_is_told_once_with_its_employer():
    dc1, az10 = {"id": "dc1", "text": "Proposed an MCP tool.", "org": "180 Degrees Consulting Bristol"}, {"id": "az10", "text": "Built 3 agents."}
    g3, g4, moved = AL.split_shared([dc1], [az10, dc1])
    assert [e["id"] for e in g3] == ["dc1"] and [e["id"] for e in g4] == ["az10"] and moved == {"dc1"}
    g3, g4, moved = AL.split_shared([dc1, az10], [dc1])                     # P4 has no other role: P3 lets it go
    assert [e["id"] for e in g3] == ["az10"] and [e["id"] for e in g4] == ["dc1"] and not moved


def test_p2_is_left_out_when_it_repeats_p1s_number(world, monkeypatch):
    monkeypatch.setattr(CL, "result_paragraph", lambda fb, op: ("The result, as measured: ~65 tests across 8 files.", "bullet az4 result"))
    plan = CL.prepare(world.job, world.fb, world.kws, world.hk, "", world.evidence, world.settings, world.claimable, world.chosen,
                      possible=world.possible)
    assert "~65" in plan.p1
    assert plan.p2 == "" and plan.p2_source == "left out: repeats P1's number (65)"


def test_p2_stays_when_its_number_is_new(world, monkeypatch):
    monkeypatch.setattr(CL, "result_paragraph", lambda fb, op: ("The result, as measured: 8 files.", "bullet az4 result"))
    plan = CL.prepare(world.job, world.fb, world.kws, world.hk, "", world.evidence, world.settings, world.claimable, world.chosen,
                      possible=world.possible)
    assert plan.p2 == "The result, as measured: 8 files."


def test_terms_sentence_takes_the_bullets_spelling():
    group = [{"id": "a1", "text": "Built a model in Python with an LLM judge", "terms": ["python", "llm"]}]
    assert AL._terms_sentence(group, ["python", "llm", "sql"], "nothing said yet") == "That work used Python and LLM."


# ------------------------------------------------------------------ the checks (item 1 a-d)
def test_facts_numbers_names_and_tools_must_be_in_the_draft():
    draft = "At Northwind I built an agent in Python, tested by ~65 tests."
    assert FL.fact_problems("At Northwind I built an agent in Python, tested by ~65 tests.", draft, tools=["python", "docker"]) == []
    probs = FL.fact_problems("At Northwind I built an agent in Docker for Acme, tested by 90 tests.", draft, tools=["python", "docker"])
    assert any("90" in p for p in probs) and any("'Acme'" in p for p in probs) and any("'docker'" in p for p in probs)


def test_the_ad_counts_only_where_it_is_given_and_quotes_are_left_to_the_opening_check():
    assert FL.fact_problems('Northwind is hiring for "Kubernetes at scale"; I built 3 agents.', "I built 3 agents. Northwind") == []
    assert FL.fact_problems("The Kubernetes team is why I applied.", "draft", ad="We run Kubernetes.") == []
    assert FL.fact_problems("The Kubernetes team is why I applied.", "draft")


def test_no_sentence_over_35_words():
    long = " ".join(["word"] * 36) + "."
    assert FL.length_problems({"P3": "A short one. " + long}) and not FL.length_problems({"P3": " ".join(["word"] * 35) + "."})


def test_no_sentence_twice_and_nothing_repeated_across_paragraphs():
    s = "I proposed and prototyped an MCP-based agentic tool in Python, presented in a live stakeholder demo."
    assert FL.repeat_problems({"P3": s, "P4": "At 180 Degrees Consulting Bristol, " + s[0].lower() + s[1:]})       # the RightShip duplicate
    assert FL.repeat_problems({"P3": s + " " + s})
    assert not FL.repeat_problems({"P3": s, "P4": "At AstraZeneca, I shipped an LLM agent to AWS Lambda behind ~65 tests."})


@pytest.mark.parametrize("text", ["I am writing to apply for the role.", "I am applying for the AI Engineer role at RightShip.", "Hi team, here goes.",
                                  "I'm excited to apply.", "I would like to apply for this job.", "Dear hiring manager, hello."])
def test_banned_openings(text):
    assert FL.opening_problems(text)


def test_an_ordinary_opening_is_fine():
    assert not FL.opening_problems('RightShip is hiring for "agentic systems"; at AstraZeneca I shipped an agent.')


def test_letter_overlap_flags_a_copied_sentence_and_a_high_jaccard():
    letter = ("RightShip is hiring for \"agentic systems\"; at AstraZeneca I took an LLM agent from prototype to production on AWS Lambda. "
              "RightShip's focus on safety and risk intelligence is why I applied.")
    copied = "Something new to start with here today. At AstraZeneca I took an LLM agent from prototype to production on AWS Lambda."
    assert any("takes a sentence" in p for p in FL.letter_overlap_problems(copied, letter))
    assert any("overlap" in p for p in FL.letter_overlap_problems(letter, letter))
    assert FL.letter_overlap_problems("I want to build AI systems that are trustworthy in production, not just demos.", letter) == []


# ------------------------------------------------------------------ the fluency pass in the letter
def test_fluency_off_in_the_test_copy_asks_the_model_for_p5_only(world):
    llm = model(shorter_quote)
    pdf, letter = make(world, llm)
    assert letter["status"] == "ok" and letter["retry"]["fluency"]["source"] == "off"
    assert [c.get("mode") for c in llm.seen] == ["assembled_p5"]


def test_a_rewrite_that_passes_every_check_is_the_letter_and_the_draft_is_kept(world):
    world.settings.raw["letter"]["fluency"] = True
    llm = model(smooth)
    pdf, letter = make(world, llm)
    assert letter["status"] == "ok", letter.get("reason")
    flu = letter["retry"]["fluency"]
    assert flu["source"] == "model" and flu["tries"] == 1 and flu["tokens"] > 0, flu
    assert world.written[0] != letter["assembled_parts"] and world.written[0] == {**letter["assembled_parts"], **smooth(letter["assembled_parts"])}
    assert letter["retry"]["tokens"] == letter["retry"]["p5"]["tokens"] + flu["tokens"]
    assert all(len(s.split()) <= 35 for n in FL.REWRITTEN for s in sentences(world.written[0].get(n) or ""))
    fl_ctx = [c for c in llm.seen if c.get("mode") == "fluency"][0]
    assert set(fl_ctx["draft"]) <= set(FL.REWRITTEN) and not {"P5", "P6", "P7"} & set(fl_ctx["draft"])      # P5 to P7 are never sent


@pytest.mark.parametrize("break_it, why", [
    (lambda d: {**shorter_quote(d), "P3": d["P3"] + " I also cut costs by 93%."}, "93"),
    (lambda d: {**shorter_quote(d), "P3": d["P3"] + " I used Kubernetes at Globex."}, "Globex"),
    (lambda d: {**shorter_quote(d), "P4": d["P4"] + " " + " ".join(["and more"] * 20) + "."}, "words (at most 35)"),
    (lambda d: {**shorter_quote(d), "P4": d["P4"] + " " + sentences(d["P3"])[0]}, "same sentence twice"),
    (lambda d: {**shorter_quote(d), "P1": "I am writing to apply. " + shorter_quote(d)["P1"]}, "banned phrase"),
    (lambda d: {**shorter_quote(d), "P3": d["P3"] + " We shipped it together as our team."}, "our"),
])
def test_a_rewrite_that_breaks_a_check_is_thrown_away_and_the_draft_is_the_letter(world, break_it, why):
    world.settings.raw["letter"]["fluency"] = True
    llm = model(break_it)
    pdf, letter = make(world, llm)
    assert letter["status"] == "ok", letter.get("reason")
    flu = letter["retry"]["fluency"]
    assert flu["source"] == "draft" and flu["tries"] == FL.TRIES
    assert any(why in p for p in flu["problems"][0]), flu["problems"][0]
    assert world.written[0] == letter["assembled_parts"]                                    # the assembled draft, word for word
    assert any("fluency rewrite not used" in n for n in letter["notes"])
    second = [c for c in llm.seen if c.get("mode") == "fluency"]
    assert len(second) == FL.TRIES


def test_the_second_try_is_told_the_first_ones_rejections(world):
    world.settings.raw["letter"]["fluency"] = True
    prompts = []
    tries = iter([lambda d: {**shorter_quote(d), "P3": d["P3"] + " I also cut costs by 93%."}, shorter_quote])

    def rewrite(d):
        return next(tries)(d)
    llm = model(rewrite)
    orig = llm.json

    def spy(task, system, user, context=None):
        if (context or {}).get("mode") == "fluency":
            prompts.append(user)
        return orig(task, system, user, context)
    llm.json = spy
    pdf, letter = make(world, llm)
    assert letter["retry"]["fluency"]["source"] == "model" and letter["retry"]["fluency"]["tries"] == 2
    assert "REJECTED" not in prompts[0] and "REJECTED" in prompts[1] and "93" in prompts[1]
    assert "I also cut costs by 93%" in prompts[1] and "change nothing else" in prompts[1]               # its own rewrite, to fix, not redo


def test_a_failed_model_call_keeps_the_draft(world):
    world.settings.raw["letter"]["fluency"] = True
    pdf, letter = make(world, model(lambda d: RuntimeError("the provider is down")))
    assert letter["status"] == "ok" and letter["retry"]["fluency"]["source"] == "draft" and world.written[0] == letter["assembled_parts"]


def test_the_mock_model_returns_the_draft_as_it_is():
    draft = {"P1": "a", "P3": "b"}
    assert MockLLM().json("cover_letter", "", "", {"mode": "fluency", "draft": draft}) == draft


def test_the_prompt_gives_the_word_budget_of_p1_to_p4(world):
    world.settings.raw["letter"]["fluency"] = True
    systems = []
    llm = model(shorter_quote)
    orig = llm.json

    def spy(task, system, user, context=None):
        if (context or {}).get("mode") == "fluency":
            systems.append(system)
        return orig(task, system, user, context)
    llm.json = spy
    pdf, letter = make(world, llm)
    fixed = sum(len(str(letter["parts"][n]).split()) for n in letter["parts"] if n not in FL.REWRITTEN and letter["parts"].get(n))
    assert f"P1 to P4 together {200 - fixed} to {320 - fixed} words" in systems[0]


def test_a_plural_in_the_draft_allows_the_singular_name():
    assert FL.fact_problems("I called the API.", "I called the external APIs.") == []


def test_growth_bullets_follow_the_ads_keywords_not_cv_order():
    from careerops.tailoring.scorer import Keyword
    fb = {"experience": [{"org": "X", "bullets": [{"id": "x1", "text": "Built 10 SwiftUI templates", "terms": ["swiftui"]},
                                                  {"id": "x2", "text": "Built 3 LLM agents with LangGraph", "terms": ["llm", "langgraph"]}]}],
          "projects": []}
    kws = [Keyword("llm", 3.0, True), Keyword("langgraph", 2.0, True)]
    assert [b["id"] for b in AL._numbered_unused(fb, None, set(), set(), kws)] == ["x2", "x1"]


def test_growth_skips_a_bullet_the_guard_reads_as_a_list_of_terms(world, monkeypatch):
    from careerops.tailoring import letter_rules
    real = letter_rules.term_dump_problems
    seen = []

    def flag_first(paragraphs, fb):
        seen.append(paragraphs)
        return ["a list of terms, not a sentence"] if len(seen) == 1 else real(paragraphs, fb)
    monkeypatch.setattr(letter_rules, "term_dump_problems", flag_first)
    pdf, letter = make(world, model(shorter_quote))
    assert letter["status"] == "ok" and seen and any("P3 grew by bullet" in n for n in letter["notes"])
    assert seen[0][0] not in world.written[0]["P3"]                                       # the flagged bullet was passed over


def test_a_long_quoted_phrase_is_trimmed_from_its_ends_so_sentence_1_stays_35_words():
    from careerops.tailoring import opening as OP
    ad = "Demonstrated experience building agentic systems and RAG pipelines with LangGraph (and LangChain or similar)"
    assert OP.fit_phrase(ad, 9) == "agentic systems and RAG pipelines with LangGraph"
    assert OP.fit_phrase("reliable production pipelines using tools like MLflow and Optuna", 7) == "reliable production pipelines using tools like MLflow"
    assert OP.fit_phrase("short phrase here", 2) == "short phrase here"                                # never under three words
    assert " ".join(OP.fit_phrase(ad, 9).split()).lower() in " ".join(ad.split()).lower()            # still the ad's own words


def test_the_planned_opening_is_35_words_or_fewer(world):
    first = sentences(world.plan.p1)[0]
    assert len(first.split()) <= 35 and not CL.OP.problems(world.plan.p1, world.fb, [world.job["description"]])


def test_a_pasted_ad_sentence_is_refused():
    ad = "Rapidly prototype and experiment with LLM-based applications, agents and retrieval pipelines to establish whether an idea works."
    assert FL.ad_paste_problems({"answer": "I like it. " + ad}, ad)
    assert FL.ad_paste_problems({"answer": "I like it. Rapidly prototype and experiment with LLM-based applications."}, ad)   # 7 words
    assert not FL.ad_paste_problems({"answer": "I want to prototype LLM-based applications quickly and see whether an idea works."}, ad)


@pytest.mark.parametrize("p5", ["I am excited to join RightShip because the ad says safety.", "I am writing because RightShip builds safety tools."])
def test_a_p5_that_opens_with_a_banned_phrase_is_refused(world, p5):
    probs = AL.p5_problems(p5, world.job, world.fb, world.kws, world.plan, TEAM, world.job["description"], [])
    assert any("banned phrase" in p for p in probs)
    parts = {**CL.included(world.plan, world.settings, set()), "P3": "x", "P4": "y", "P5": p5}
    assert any(p.startswith("P5: opens with a banned phrase") for p in CL.check(parts, world.job, world.fb, world.kws, world.plan, TEAM,
                                                                                 world.settings)[0])


def test_a_text_that_ends_mid_sentence_is_refused():
    assert FL.unfinished_problems({"answer": "I like it. Rapidly prototype and experiment with LLM-based applications"})
    assert not FL.unfinished_problems({"P7": "Could we spend 20 minutes?", "P3": 'It said "yes."'})


def test_with_the_pass_on_the_draft_grows_past_the_low_end(world):
    world.settings.raw["letter"]["fluency"] = True
    pdf, letter = make(world, model(lambda d: RuntimeError("down")))
    assert letter["status"] == "ok" and CL.words(CL.assemble(letter["assembled_parts"])[0]) >= 200
    assert any(f"(under {200 + FL.GROW_MARGIN} words)" in n for n in letter["notes"])


def test_two_grammar_errors_the_letters_made_are_refused():
    assert "('prototyping')" in FL.grammar_problems({"P5": "I look forward to rapidly prototype and experiment with LLM-based applications."})[0]
    assert FL.grammar_problems({"P5": "What draws me to this role is RightShip is the maritime industry's leading platform."})
    assert not FL.grammar_problems({"P5": "I look forward to prototyping with the team.", "P3": "I look forward to discussions about it."})


def test_a_motivation_sentence_built_on_an_ad_bullet_is_a_paste():
    ad = "Rapidly prototype and experiment with LLM-based applications, agents and retrieval pipelines to establish whether an idea works."
    said = "Rapidly prototype and experiment with LLM-based applications, agents and retrieval pipelines is a key part of the role that excites me."
    assert FL.ad_paste_problems({"answer": said}, ad)
