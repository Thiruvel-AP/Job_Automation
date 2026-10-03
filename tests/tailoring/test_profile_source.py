"""CV-2: the profile may draw on profile_facts + skills + the bullets selected for THIS CV — still fact-bank only —
and the CV carries exactly two projects."""
from careerops.tailoring.tailor import _assemble

PROFILE = "Data Scientist with LLM agent work and a three-tier framework of ~65 tests."


def _violations(viol):
    return [v for v in viol if v.startswith("profile")]


def test_a_number_from_a_selected_bullet_is_allowed_in_the_profile(fb_fixture):
    prop = {"profile": PROFILE, "experience": {"ex1": ["a1", "a4"]}, "projects": ["p1", "p2"]}
    ctx, viol, _ = _assemble(fb_fixture, prop, [])
    assert ctx["profile"] == PROFILE and _violations(viol) == []


def test_the_same_number_is_blocked_when_its_bullet_was_not_selected(fb_fixture):
    prop = {"profile": PROFILE, "experience": {"ex1": ["a1", "a2"]}, "projects": ["p1", "p2"]}
    ctx, viol, _ = _assemble(fb_fixture, prop, [])
    assert any("new numbers ['65']" in v for v in _violations(viol))
    assert ctx["profile"].startswith(fb_fixture["profile_facts"][0])    # fell back to the fact-bank profile


def test_a_number_that_is_nowhere_in_the_fact_bank_is_blocked_even_with_everything_selected(fb_fixture):
    prop = {"profile": "Data Scientist who wrote ~99 tests.", "projects": ["p1", "p2"],
            "experience": {e["id"]: [b["id"] for b in e["bullets"]] for e in fb_fixture["experience"]}}
    ctx, viol, _ = _assemble(fb_fixture, prop, [])
    assert any("new numbers ['99']" in v for v in _violations(viol))


def test_a_project_bullet_number_is_also_allowed_once_its_project_is_selected(fb_fixture):
    prop = {"profile": "Data Scientist with churn models built on 7,043 records.", "experience": {"ex1": ["a1"]},
            "projects": ["p1", "p2"]}
    assert _violations(_assemble(fb_fixture, prop, [])[1]) == []
    prop["projects"] = ["p2", "p3"]
    assert any("new numbers" in v for v in _violations(_assemble(fb_fixture, prop, [])[1]))


def test_deny_list_and_years_claims_are_still_blocked(fb_fixture):
    for bad in ("Data Scientist with Kubernetes experience.", "Data Scientist with 5 years of Python."):
        ctx, viol, _ = _assemble(fb_fixture, {"profile": bad, "experience": {"ex1": ["a1"]}, "projects": ["p1"]}, [])
        assert ctx["profile"].startswith(fb_fixture["profile_facts"][0])


def test_projects_are_capped_at_two(fb_fixture):
    ctx, _, _ = _assemble(fb_fixture, {"profile": "x", "projects": ["p3", "p1", "p4", "p2"]}, [])
    assert [p["id"] for p in ctx["projects"]] == ["p3", "p1"]


def test_without_a_usable_proposal_the_first_two_projects_are_used(fb_fixture):
    ctx, _, _ = _assemble(fb_fixture, {"profile": "x", "projects": ["nope"]}, [])
    assert [p["id"] for p in ctx["projects"]] == ["p1", "p2"]
