"""v3.4: the motivation answer reuses nothing from this job's letter (5-word shingles: Jaccard under 0.30, no sentence with half its shingles in the
letter) and is a fluency rewrite under the same code checks as the letter (facts, <= 35 words a sentence, no repeats, no banned opening such as
"I am applying"), two tries, then a code-built answer that takes nothing from the letter. RightShip, 3 Nov 2026: the answer was "I am applying for
the AI Engineer role at RightShip." + the letter's P5 + the letter's 70-word P4 sentence, word for word. MockLLM only, no network."""
import pytest
from test_freetext import AD_PHRASE, REP, llm_with, make  # noqa: F401  (make is a fixture)

from careerops.apply import freetext as ft
from careerops.llm import MockLLM
from careerops.tailoring import fluency as FL
from careerops.tailoring.grounding import sentences

Q = "What motivated you to apply for this position?"
LETTER_PARTS = {
    "P1": 'Northwind Health AI is hiring for "tools that help oncology researchers"; at Northwind Research Lab I built a tool-calling LLM agent '
          "(LangChain, AWS Bedrock) for a multi-omics cancer research platform.",
    "P3": "I cleaned and joined 14 heterogeneous source datasets so the agent used trusted data.",
    "P4": "At Northwind Research Lab, I built a tool-calling LLM agent (LangChain, AWS Bedrock) used by the whole research team.",
    "P5": "Northwind Health AI's focus on clinical decision-support tools is why I applied.",
    "P7": "Could we spend 20 minutes on the first problem the AI Engineer would own?",
}
LETTER = {"status": "ok", "parts": LETTER_PARTS, "assembled_parts": LETTER_PARTS, "evidence": [{"id": "az1"}, {"id": "a1"}]}
LETTER_BODY = " ".join(LETTER_PARTS[n] for n in ("P1", "P3", "P4", "P5", "P7"))
GOOD = ("The work Northwind Health AI does, building tools that help oncology researchers, is the work I want to do next. "
        "I want AI systems that are trustworthy in production, not just demos, and safe when their dependencies fail. "
        "In my own work I designed a deterministic guard architecture with a local 19,213-gene parquet fallback behind circuit breakers, "
        "so answers stay reproducible during upstream outages. "
        "I am drawn to applied AI in domains where correctness matters, such as healthcare and biomedical research.")
COPY = " ".join([LETTER_PARTS["P5"], LETTER_PARTS["P1"], LETTER_PARTS["P4"]])
OPENS_BADLY = "I am applying for the AI Engineer role at Northwind Health AI. " + GOOD
TOO_LONG = GOOD + " " + "I also want to keep learning " + " ".join(["and building"] * 16) + " with the team every single day."


def rep_with_letter():
    return {**REP, "letter": LETTER}


def model(*answers):
    """The fluency call answered by `answers` in order (the last again); any other free-text call answered by GOOD."""
    seen, queue = [], list(answers)

    def handler(ctx):
        seen.append(ctx)
        if ctx.get("mode") == "fluency_motivation":
            return {"answer": queue.pop(0) if len(queue) > 1 else queue[0]}
        return {"answer": GOOD, "ad_phrases": [AD_PHRASE]}
    llm = MockLLM({"free_text_answer": handler})
    llm.seen = seen
    return llm


def no_copy(text: str) -> None:
    assert FL.letter_overlap_problems(text, LETTER_BODY) == [], FL.letter_overlap_problems(text, LETTER_BODY)
    for s in sentences(LETTER_BODY):
        assert s not in text


def test_the_old_assembled_answer_is_now_refused_by_the_guard(make):
    f = make(model(GOOD), rep=rep_with_letter())
    old = " ".join(["I am applying for the AI Engineer role at Northwind Health AI.", LETTER_PARTS["P1"], LETTER_PARTS["P5"], LETTER_PARTS["P4"]])
    assert any("I am applying" in p for p in f.guard.problems(old, f.plan("why_role", Q), ft.Limit(words=150)))
    assert FL.letter_overlap_problems(old, LETTER_BODY)


def test_fluency_off_gives_the_code_built_answer_which_copies_nothing_from_the_letter(make):
    llm = model(GOOD)
    f = make(llm, rep=rep_with_letter())
    p = f.propose(Q)
    assert p.origin == "draft" and p.text.startswith("The AI Engineer role at Northwind Health AI stands out to me for its focus on "), p.problems
    assert 60 <= len(p.text.split()) <= 150 and not FL.opening_problems(p.text)
    no_copy(p.text)
    assert "I designed a deterministic guard architecture" in p.text                  # the hook's proof bullet: the letter does not print it
    assert f.motivation_log[-1]["assembled"] is True and not any(c.get("mode") == "fluency_motivation" for c in llm.seen)


def test_a_fluent_rewrite_that_passes_every_check_is_the_answer(make, settings):
    settings.raw["letter"]["fluency"] = True
    llm = model(GOOD)
    f = make(llm, rep=rep_with_letter())
    p = f.propose(Q)
    assert p.origin == "draft" and p.text == GOOD, p.problems
    no_copy(p.text)
    info = f.motivation_log[-1]["fluency"]
    assert info["source"] == "model" and info["tries"] == 1 and info["tokens"] > 0
    ctx = [c for c in llm.seen if c.get("mode") == "fluency_motivation"][0]
    assert "letter" not in ctx and ctx["facts"] and ctx["words"] == [60, 150]            # the letter is never sent: it lent the model facts
    assert not any(s in " ".join(ctx["facts"]) for s in sentences(LETTER_BODY))


@pytest.mark.parametrize("bad, why", [(COPY, "letter"), (OPENS_BADLY, "I am applying"), (TOO_LONG, "words (at most 35)"),
                                      (GOOD.replace("19,213", "25,000"), "25000")])
def test_a_rewrite_that_breaks_a_check_falls_back_to_the_code_built_answer(make, settings, bad, why):
    settings.raw["letter"]["fluency"] = True
    llm = model(bad)
    f = make(llm, rep=rep_with_letter())
    p = f.propose(Q)
    assert p.origin == "draft" and p.text.startswith("The AI Engineer role at Northwind Health AI"), p.problems
    no_copy(p.text)
    info = f.motivation_log[-1]["fluency"]
    assert info["source"] == "fallback" and info["tries"] == FL.TRIES
    assert any(why in x for x in info["problems"][0]), info["problems"][0]


def test_the_second_try_is_told_the_first_ones_problems(make, settings):
    settings.raw["letter"]["fluency"] = True
    llm = model(COPY, GOOD)
    f = make(llm, rep=rep_with_letter())
    p = f.propose(Q)
    assert p.text == GOOD
    ctxs = [c for c in llm.seen if c.get("mode") == "fluency_motivation"]
    assert not ctxs[0]["problems"] and any("letter" in x for x in ctxs[1]["problems"]) and ctxs[1]["last_answer"] == COPY


def test_a_draft_that_opens_i_am_applying_is_refused_without_a_letter_too(make):
    f = make(llm_with("I am applying for this role because " + GOOD, ad_phrases=[AD_PHRASE]))
    p = f.propose(Q)
    assert p.text == "" and any("I am applying" in x for x in p.problems)


def test_the_mock_model_returns_the_code_built_answer():
    assert MockLLM().json("free_text_answer", "", "", {"mode": "fluency_motivation", "draft": "x"}) == {"answer": "x"}
