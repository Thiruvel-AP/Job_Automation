"""The letter is built from the CV (Document rules, B3): its P3 and P4 bullets and project mentions come only from what this job's CV prints, a
skill it names must be in the CV's Skills or bullets, and at least 60 % of the body's sentences cite a bullet or project (an internal tag, never
printed). No network, no model; the guards are called directly."""
import re

import pytest

from careerops.tailoring import company_letter as CL
from careerops.tailoring import letters as L
from careerops.tailoring.scorer import norm_literal
from test_company_letter import JOB, TEAM, body, world  # noqa: F401  (world is a fixture)


def cv_of(w, *, drop_ids=(), text=None) -> dict:
    """What the CL guards know of the CV: the bullet ids it prints and the text of its Skills and bullets."""
    ids = set(w.chosen) - set(drop_ids)
    bullets = " ".join(b["text"] + " " + " ".join(b.get("terms", [])) for sec in ("experience", "projects") for e in w.fb[sec] for b in e["bullets"] if b["id"] in ids)
    return {"ids": ids, "text": bullets if text is None else text}


def run_check(w, paragraphs, cv):
    problems, _ = CL.check(w.plan.p1, paragraphs, w.plan.p5, JOB, w.fb, w.kws, w.plan.evidence, w.plan.reqs, TEAM, w.settings, cv=cv)
    return problems


def test_a_letter_whose_evidence_is_on_the_cv_passes(world):
    assert run_check(world, body(world, 400), cv_of(world)) == []


def test_a_bullet_that_is_not_on_the_cv_is_rejected(world):
    gone = world.plan.evidence[0]["id"]
    problems = run_check(world, body(world, 400), cv_of(world, drop_ids=[gone]))
    assert f"P3/P4 draw on bullet {gone}, which is not printed on this job's CV" in problems


def test_requirement_groups_never_offer_a_bullet_that_is_not_on_the_cv(world):
    chosen = set(world.chosen) - {e["id"] for e in world.plan.evidence}
    groups = CL.requirement_groups(world.fb, world.kws, chosen, JOB["description"], world.hk)
    assert not {e["id"] for _, g in groups for e in g} & {e["id"] for e in world.plan.evidence}


def test_a_skill_the_cv_does_not_have_is_rejected(world):
    paragraphs = body(world, 400)
    text = norm_literal(" ".join(paragraphs))
    named = [t for t in L._tools(world.fb) if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", text)]
    assert named                                                                        # the letter names skills because its bullets do
    assert not [p for p in run_check(world, paragraphs, cv_of(world)) if "Skills or bullets" in p]
    found = [p for p in run_check(world, paragraphs, cv_of(world, text="zzz nothing")) if "Skills or bullets" in p]
    assert found and all("which is not in this CV's Skills or bullets" in p for p in found)
    assert any(f"names '{named[0]}'" in p for p in found)


@pytest.mark.parametrize("p2_sentences,p3,p4,ok", [(2, 2, 2, True), (3, 3, 3, True), (3, 2, 2, False), (6, 1, 1, False), (1, 1, 1, True), (2, 1, 1, False)])
def test_sixty_percent_of_the_body_sentences_must_cite_a_bullet(world, p2_sentences, p3, p4, ok):
    paragraphs = [" ".join(f"The ad says thing number {i} about the work." for i in range(p2_sentences)),
                  " ".join(f"Sentence {i} of the first evidence paragraph." for i in range(p3)),
                  " ".join(f"Sentence {i} of the second evidence paragraph." for i in range(p4))]
    problems = CL.balance_problems(paragraphs)
    assert (problems == []) is ok
    if not ok:
        total = p2_sentences + p3 + p4
        assert problems == [f"body: {p3 + p4} of {total} sentences cite a bullet or project ({(p3 + p4) / total:.0%}); at least 60% must (shorten P2)"]


def test_the_check_reports_a_top_heavy_body(world):
    long_p2 = " ".join(["Anaplan builds an AI-infused scenario planning and analysis platform so its customers can outpace their competition."] * 6)
    problems = run_check(world, [long_p2, *body(world, 400)[1:]], cv_of(world))
    assert any(p.startswith("body: ") and "must (shorten P2)" in p for p in problems)


def test_the_citation_tags_name_the_bullets_each_sentence_stands_on(world):
    paragraphs = body(world, 400)
    tags = CL.citation_tags(paragraphs, world.plan.groups)
    assert [t["para"] for t in tags[:1]] == ["P2"] and tags[0]["ids"] == []
    p3 = [t for t in tags if t["para"] == "P3"]
    assert p3 and all(t["ids"] == [e["id"] for e in world.plan.groups[0]] for t in p3)
    assert all(set(t) == {"para", "sentence", "ids"} for t in tags)
