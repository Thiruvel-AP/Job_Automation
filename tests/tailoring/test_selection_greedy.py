"""Bullet selection by marginal gain: each pick is the bullet adding the most not-yet-covered keyword weight (bullets,
projects and the skills section all count as covered), ties by recency. Pure functions on a tiny fact bank; no model."""
from careerops.tailoring.budget import bullet_scores
from careerops.tailoring.scorer import Keyword, score
from careerops.tailoring.selection import select_cv


def _b(bid, text):
    return {"id": bid, "text": text + " work", "terms": []}


def _kws(*terms, weights=None):
    return [Keyword(t, (weights or {}).get(t, 1.0), True) for t in terms]


def _covered(fb, sel, kws):
    ids = {i for ids in sel["experience"].values() for i in ids}
    ids |= {b["id"] for p in fb["projects"] if p["id"] in sel["projects"] for b in p["bullets"]}
    text = " ".join(b["text"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if b["id"] in ids)
    return set(score(text, kws).hits)


def test_greedy_covers_more_than_ranking_when_the_top_bullets_repeat_each_other():
    fb = {"skills": {}, "projects": [], "experience": [{"id": "e1", "bullets": [
        _b("x1", "alpha beta gamma"), _b("x2", "alpha beta"), _b("x3", "beta gamma"), _b("x4", "alpha gamma"),
        _b("x5", "delta")]}]}
    kws = _kws("alpha", "beta", "gamma", "delta")
    sc = bullet_scores(fb, kws)
    by_rank = sorted(sc, key=lambda b: -sc[b])[:4]                      # what ranking per bullet kept: the 4 best
    ranked_cover = _covered(fb, {"experience": {"e1": by_rank}, "projects": []}, kws)
    sel = select_cv(fb, kws)
    assert ranked_cover == {"alpha", "beta", "gamma"}                   # four bullets, and "delta" is still missing
    assert sel["experience"]["e1"] == ["x1", "x5"]                      # greedy: x1, then the one that adds "delta"
    assert _covered(fb, sel, kws) == {"alpha", "beta", "gamma", "delta"}


def test_a_heavy_keyword_beats_several_light_ones_and_picks_are_listed_in_the_order_made():
    fb = {"skills": {}, "projects": [], "experience": [
        {"id": "e1", "bullets": [_b("a", "one"), _b("b", "two")]},
        {"id": "e2", "bullets": [_b("c", "alpha beta gamma"), _b("d", "big"), _b("e", "alpha")]}]}
    kws = _kws("alpha", "beta", "gamma", "big", weights={"big": 5.0})
    assert select_cv(fb, kws)["experience"]["e2"] == ["d", "c"]         # 5 before 3; "e" would add nothing


def test_keywords_the_skills_section_already_carries_need_no_bullet():
    fb = {"skills": {"Languages": ["alpha", "beta"]}, "projects": [], "experience": [
        {"id": "e1", "bullets": [_b("a1", "one"), _b("a2", "two")]},
        {"id": "e2", "bullets": [_b("m", "alpha"), _b("n", "gamma")]}]}
    kws = _kws("alpha", "beta", "gamma")
    sel = select_cv(fb, kws)
    assert sel["experience"]["e2"] == ["n"]                             # "m" says only what the skills already say
    assert len(sel["experience"]["e1"]) == 2                            # but the most recent entry keeps its minimum


def test_an_entry_never_goes_below_its_minimum_or_above_its_maximum():
    fb = {"skills": {}, "projects": [], "experience": [
        {"id": "e1", "bullets": [_b(f"a{i}", f"k{i}") for i in range(6)]},
        {"id": "e2", "bullets": [_b(f"b{i}", f"k{i + 6}") for i in range(4)]},
        {"id": "e3", "bullets": [_b("c0", "nothing relevant")]}]}
    kws = _kws(*[f"k{i}" for i in range(10)])
    sel = select_cv(fb, kws)
    assert len(sel["experience"]["e1"]) == 4 and len(sel["experience"]["e2"]) == 2 and sel["experience"]["e3"] == ["c0"]


def test_with_equal_gain_the_more_recent_entry_is_served_first():
    fb = {"skills": {}, "projects": [], "experience": [
        {"id": "new", "bullets": [_b("n1", "alpha")]}, {"id": "old", "bullets": [_b("o1", "alpha"), _b("o2", "beta")]}]}
    sel = select_cv(fb, _kws("alpha", "beta"))
    assert sel["experience"]["new"] == ["n1"]                           # "alpha" is covered here, so ...
    assert sel["experience"]["old"] == ["o2"]                           # ... the older entry adds only "beta"


def test_projects_are_picked_by_marginal_gain_too():
    def proj(pid, *texts):
        return {"id": pid, "bullets": [_b(f"{pid}{i}", t) for i, t in enumerate(texts)]}

    fb = {"skills": {}, "experience": [{"id": "e1", "bullets": [_b("a", "x"), _b("b", "y")]}],
          "projects": [proj("p1", "alpha beta"), proj("p2", "alpha"), proj("p3", "delta")]}
    kws = _kws("alpha", "beta", "delta")
    assert select_cv(fb, kws)["projects"] == ["p1", "p3"]               # p2 only repeats p1; ranking would have kept it


def test_selection_is_deterministic():
    fb = {"skills": {}, "projects": [], "experience": [{"id": "e1", "bullets": [_b(f"a{i}", "alpha") for i in range(5)]}]}
    assert select_cv(fb, _kws("alpha")) == select_cv(fb, _kws("alpha"))
