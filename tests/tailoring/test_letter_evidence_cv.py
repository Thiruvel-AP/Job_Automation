"""The letter is built from the CV (Document rules, B3): its P3 and P4 bullets and project mentions come only from what this job's CV prints, a
skill it names must be in the CV's Skills or bullets, and at least 60 % of the sentences of P1 to P4 cite a bullet or project (an internal tag, never
printed; P5 to P7 are not counted). No network, no model; the guards are called directly."""
import re

import pytest

from careerops.tailoring import company_letter as CL
from careerops.tailoring import letters as L
from careerops.tailoring.scorer import norm_literal
from test_company_letter import JOB, TEAM, P3, P4, P5, letter_parts, world  # noqa: F401  (world is a fixture)


def cv_of(w, *, drop_ids=(), text=None) -> dict:
    """What the CL guards know of the CV: the bullet ids it prints and the text of its Skills and bullets."""
    ids = set(w.chosen) - set(drop_ids)
    bullets = " ".join(b["text"] + " " + " ".join(b.get("terms", [])) for sec in ("experience", "projects") for e in w.fb[sec] for b in e["bullets"] if b["id"] in ids)
    return {"ids": ids, "text": bullets if text is None else text}


def run_check(w, parts, cv):
    problems, _ = CL.check(parts, JOB, w.fb, w.kws, w.plan, TEAM, w.settings, cv=cv, claimable=w.claimable)
    return problems


def test_a_letter_whose_evidence_is_on_the_cv_passes(world):
    assert run_check(world, letter_parts(world), cv_of(world)) == []


def test_a_bullet_that_is_not_on_the_cv_is_rejected(world):
    gone = world.plan.evidence[0]["id"]
    problems = run_check(world, letter_parts(world), cv_of(world, drop_ids=[gone]))
    assert f"P3/P4 draw on bullet {gone}, which is not printed on this job's CV" in problems


def test_the_skill_and_role_groups_never_offer_a_bullet_that_is_not_on_the_cv(world):
    chosen = set(world.chosen) - {e["id"] for e in world.plan.evidence}
    skills, group = CL.skill_group(world.fb, world.kws, chosen, JOB["description"], {world.hk.proof_bullet_id})
    roles = CL.role_group(world.fb, world.kws, chosen, JOB["description"], world.hk, set())
    assert not {e["id"] for e in [*group, *roles]} & {e["id"] for e in world.plan.evidence}


def test_a_skill_the_cv_does_not_have_is_rejected(world):
    parts = letter_parts(world)
    text = norm_literal(" ".join(parts.values()))
    named = [t for t in L._tools(world.fb) if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", text)]
    assert named                                                                        # the letter names skills because its bullets do
    assert not [p for p in run_check(world, parts, cv_of(world)) if "Skills or bullets" in p]
    found = [p for p in run_check(world, parts, cv_of(world, text="zzz nothing")) if "Skills or bullets" in p]
    assert found and all("which is not in this CV's Skills or bullets" in p for p in found)
    assert any(f"names '{named[0]}'" in p for p in found) or any(p.startswith("P5:") or p.startswith("P3:") or p.startswith("P4:") for p in found)


def tags(**per_paragraph):
    """Citation tags with `cited` / `uncited` sentence counts per paragraph name."""
    out = []
    for name, (cited, uncited) in per_paragraph.items():
        out += [{"para": name, "sentence": f"s{i}", "ids": ["x"]} for i in range(cited)] + [{"para": name, "sentence": f"u{i}", "ids": []} for i in range(uncited)]
    return out


@pytest.mark.parametrize("cited,uncited,ok", [(6, 0, True), (6, 4, True), (3, 2, True), (3, 3, False), (2, 6, False), (1, 0, True), (0, 3, False)])
def test_sixty_percent_of_the_sentences_of_p1_to_p4_must_cite_a_bullet(cited, uncited, ok):
    problems = CL.balance_problems(tags(P3=(cited, uncited)))
    assert (problems == []) is ok
    if not ok:
        total = cited + uncited
        assert problems == [f"body: {cited} of {total} sentences cite a bullet or project ({cited / total:.0%}); at least 60% must"]


def test_a_letter_with_no_sentence_to_count_has_nothing_to_complain_about():
    assert CL.balance_problems([]) == []


def test_the_citation_tags_name_the_bullets_each_sentence_stands_on(world):
    parts = letter_parts(world)
    tags_ = CL.citation_tags(parts, world.plan.cite_ids())
    assert {t["para"] for t in tags_} == {"P1", "P3", "P4"}                         # P5 to P7 are not counted
    p3 = [t for t in tags_ if t["para"] == "P3"]
    assert p3 and all(t["ids"] == [e["id"] for e in world.plan.groups[0]] for t in p3)
    p1 = [t for t in tags_ if t["para"] == "P1"]
    assert p1 and all(t["ids"] == [world.plan.opening.bullet_id] for t in p1)
    assert all(set(t) == {"para", "sentence", "ids"} for t in tags_) and tags_[0]["sentence"].startswith("Anaplan is hiring for")


def test_a_hook_with_no_proof_bullet_leaves_p1_uncited_and_a_long_uncited_p4_can_break_the_rule(world):
    world.plan.opening.bullet_id = ""
    parts = letter_parts(world)
    t = CL.citation_tags(parts, world.plan.cite_ids())
    assert [x for x in t if x["para"] == "P1"] and not any(x["ids"] for x in t if x["para"] == "P1")
