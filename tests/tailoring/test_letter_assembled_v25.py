"""v2.5 Phase 2: the assembled company letter (letter.compose: assembled, the code default). P3 and P4 come from this CV's verified bullets by a fixed
first-person rule; the model writes only P5 (two tries, then a fallback); the whole letter still goes through company_letter.check. MockLLM only;
the PDF step is replaced by a recorder (no xelatex here: test_company_letter's slow tests render real PDFs)."""
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.tailoring import assembled_letter as AL
from careerops.tailoring import company_letter as CL
from careerops.tailoring.faithfulness import numbers
from careerops.tailoring.grounding import sentences
from test_company_letter import P5, TEAM, make_world

GARBAGE = "We are 100% the best company in the whole Zorbix galaxy!!"


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


def p5_model(*replies):
    seen = []

    def handler(ctx):
        seen.append(ctx)
        r = replies[min(len(seen), len(replies)) - 1]
        if isinstance(r, Exception):
            raise r
        return {"p5": r}
    llm = MockLLM({"cover_letter": handler})
    llm.seen = seen
    return llm


def make(w, llm, revise=None, job=None):
    return CL.make(job or w.job, w.fb, w.kws, w.hk, "", w.evidence, llm, w.settings, w.settings.path("outputs") / "assembled",
                   "Ada Lovelace Cover Letter", TEAM, w.claimable, w.chosen, revise=revise, possible=w.possible)


def test_the_first_person_rule_is_fixed_and_leaves_other_wording_word_for_word():
    assert AL.first_person("Built a churn model on 7,043 customer records") == "I built a churn model on 7,043 customer records."
    assert AL.first_person("- Designed the guard architecture.") == "I designed the guard architecture."
    assert AL.first_person("Cleaned and joined donor exports with pandas") == "I cleaned and joined donor exports with pandas."
    assert AL.first_person("Python pipeline for 14 datasets") == "Python pipeline for 14 datasets."            # not a verb: verbatim
    assert AL.first_person("AWS Bedrock agent, validated by 3 reviewers") == "AWS Bedrock agent, validated by 3 reviewers."


def test_the_assembled_letter_is_200_to_320_words_passes_every_guard_and_asks_the_model_once(world):
    llm = p5_model(P5)
    pdf, letter = make(world, llm)
    assert pdf is not None and letter["status"] == "ok", letter.get("reason")
    assert 200 <= letter["words"] <= 320 and letter["retry"]["mode"] == "assembled" and letter["retry"]["p5"]["source"] == "model"
    assert llm.calls == ["cover_letter"] and llm.seen[0]["mode"] == "assembled_p5"         # one call: P5 only
    assert letter["retry"]["tokens"] < 2000
    parts = world.written[0]
    assert parts["P1"] == world.plan.p1 and parts["P7"] == world.plan.p7
    assert parts["P5"].startswith(P5.rstrip(".") + AL.P5_TIE) and letter["retry"]["p5"]["fact"]      # v3.5.2: the model's clause, tied to one fact


def _told(world, letter):
    """{paragraph: [bullet ids]} of the letter's last variant."""
    v = letter["retry"]["variants"][-1]
    return {"P3": v["p3"], "P4": v["p4"], "P4b": v.get("p4b") or []}


def test_the_sentences_about_you_are_the_cvs_bullets_in_first_person_each_naming_its_place(world):
    """v3.5.2 (10 Nov 2026): one paragraph per employer or project, at most two bullets each, every sentence opens with where the work was done."""
    pdf, letter = make(world, p5_model(P5))
    parts = world.written[0]
    places = []
    for name, ids in _told(world, letter).items():
        if not ids:
            assert name == "P4b" and name not in parts
            continue
        assert 1 <= len(ids) <= AL.MAX_CLAIMS
        srcs = {AL.source_of(world.fb, i) for i in ids}
        assert len(srcs) == 1, srcs                                                       # one place a paragraph
        src = srcs.pop()
        places.append(src.id)
        for s in sentences(parts[name]):
            assert s.startswith(src.phrase) or s.startswith(f"That {src.name} work"), s
        for i in ids:
            bullet = next(b for e in world.fb["experience"] + world.fb["projects"] for b in e["bullets"] if b["id"] == i)
            text, _ = AL.letter_text(bullet, world.fb, True, [])
            assert AL.first_person(text)[2:].rstrip(".") in parts[name]
    assert len(places) == len(set(places))                                               # a place is told in one paragraph only


def test_p4_is_another_place_an_employer_first_its_numbered_bullets_first(world):
    pdf, letter = make(world, p5_model(P5))
    told = _told(world, letter)
    a, b = AL.source_of(world.fb, told["P3"][0]), AL.source_of(world.fb, told["P4"][0])
    assert a.id != b.id and b.kind == "experience"
    assert all(AL.source_of(world.fb, i) == b for i in told["P4"])


@pytest.mark.parametrize("reply", [GARBAGE, "", RuntimeError("the provider is down")])
def test_a_model_that_gives_garbage_for_p5_still_gives_a_letter_through_the_fallback(world, reply):
    pdf, letter = make(world, p5_model(reply, reply))
    assert letter["status"] == "ok", letter.get("reason")
    p5 = world.written[0]["P5"]                                                          # v3.5.2: the ad's own clause tied to one fact
    ad, mine = AL.p5_split(p5)
    offered = CL.about_sentences(world.job["description"], world.job["company"], world.kws)
    assert ad in AL.ad_clauses(offered, world.job["company"]) and mine.startswith("my ") and ", where I " in mine
    assert letter["retry"]["p5"]["source"].startswith("fallback")
    assert 200 <= letter["words"] <= 320


def test_a_letter_is_never_written_without_the_job_ad(world):
    job = {**world.job, "description": ""}
    pdf, letter = make(world, p5_model(P5), job=job)
    assert pdf is None and letter["status"] == "none" and world.written == []


def test_feedback_steers_the_bullets_and_never_rewrites_a_verified_sentence(world):
    first = world.plan.groups[0][0]["id"]
    pdf, letter = make(world, p5_model(P5), revise={"feedback": f"not {first}", "previous": [], "problems": []})
    assert letter["status"] == "ok", letter.get("reason")
    assert AL.first_person(world.plan.groups[0][0]["text"]) not in world.written[0]["P3"]
    assert AL.steer("use dc1 not az9, shorter") == AL.Steer({"dc1"}, {"az9"}, True, "use dc1 not az9, shorter")


def test_the_free_mode_is_still_there(world):
    world.settings.raw["letter"]["compose"] = "free"
    assert AL.compose_mode(world.settings) == "free" and AL.compose_mode({"letter": {}}) == "assembled"
