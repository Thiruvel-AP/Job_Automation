"""v3.9.1: page re-fit after the ATS fallback, bullet order inside a role, zero-score bullets, skills label echo."""
from types import SimpleNamespace

from careerops.tailoring import doccheck, reqmap, tailor


def entry(id_, ids, texts):
    return {"id": id_, "bullet_ids": list(ids), "bullets": list(texts)}


HK = SimpleNamespace(entry_kind="experience", entry_id="az", proof_bullet_id="az1")


def test_fallback_adding_lines_ends_at_two_pages_dropping_the_no_number_bullet():
    ctx = {"experience": [entry("az", ["az1", "az2", "az3"], ["Cut cost 30%", "Built a thing", "Shipped 4 models"]),
                          entry("av", ["av1"], ["Only bullet"])], "projects": [], "results": {}}
    scores = {"az1": 5.0, "az2": 1.0, "az3": 4.0, "av1": 0.0}
    calls = []

    def render():
        n = sum(len(e["bullets"]) for e in ctx["experience"])
        calls.append(n)
        return "x.pdf", (3 if n > 3 else 2)             # the fallback's extra lines put it on a third page until one bullet goes

    pdf, pages, dropped = tailor.refit_pages(ctx, scores, {"az1"}, render, 2)
    assert pages == 2 and dropped == ["az2"]               # the lowest-ranked bullet with no number
    assert ctx["experience"][1]["bullet_ids"] == ["av1"]   # the last bullet of a role stays
    assert "az1" in ctx["experience"][0]["bullet_ids"]


def test_refit_never_drops_the_proof_bullet_or_a_roles_last_bullet():
    ctx = {"experience": [entry("az", ["az1", "az2"], ["a 5", "b"])], "projects": [], "results": {}}
    pdf, pages, dropped = tailor.refit_pages(ctx, {}, {"az1"}, lambda: ("x.pdf", 3), 2)
    assert pages == 3 and dropped == ["az2"] and ctx["experience"][0]["bullet_ids"] == ["az1"]


def test_over_limit_cv_fails_the_doc_check():
    settings = {"scoring": {"max_pages": 2}}
    assert doccheck.page_problems({"cv_pages": 3}, settings)[0].startswith("PAGES")
    assert doccheck.page_problems({"cv_pages": 2}, settings) == []


def test_bullets_with_a_number_print_before_those_without():
    ids = ["az6", "az7", "az8", "az10", "az11", "az12"]
    texts = ["React UI", "AWS governance", "GNN research", "Cut 40% of cost", "Served 3 teams", "Built pipeline"]
    ctx = {"experience": [entry("az", ids, texts)], "projects": [], "results": {"az12": "r"}}
    scores = {"az6": 3, "az7": 3, "az8": 3, "az10": 2, "az11": 1, "az12": 2}
    tailor.order_in_role(ctx, scores)
    out = ctx["experience"][0]["bullet_ids"]
    assert out[:3] == ["az10", "az12", "az11"] and out[3:] == ["az6", "az7", "az8"]
    tailor.order_in_role(ctx, scores, SimpleNamespace(proof_bullet_id="az8"))
    assert ctx["experience"][0]["bullet_ids"][0] == "az8"


def test_zero_score_bullet_is_not_printed_when_the_role_has_two_scored():
    ctx = {"experience": [entry("av", ["av1", "av2", "av7"], ["a", "b", "Swift UIs"])], "projects": []}
    assert tailor.prune_zero_score(ctx, {"av1": 2, "av2": 1, "av7": 0}, set()) == ["av7"]
    assert ctx["experience"][0]["bullet_ids"] == ["av1", "av2"]


def test_zero_score_bullet_stays_when_the_role_would_have_fewer_than_two():
    ctx = {"experience": [entry("av", ["av1", "av7"], ["a", "Swift UIs"])], "projects": []}
    assert tailor.prune_zero_score(ctx, {"av1": 2, "av7": 0}, set()) == []
    assert ctx["experience"][0]["bullet_ids"] == ["av1", "av7"]


def test_skills_row_never_lists_its_own_label():
    assert reqmap.echoes_label("machine learning concepts", "Machine learning concepts")
    assert reqmap.echoes_label("statistical analysis", "Statistical analyses")
    assert reqmap.echoes_label("Statistical analyses", "statistical analysis")
    assert not reqmap.echoes_label("Machine Learning", "Machine learning concepts")
    assert not reqmap.echoes_label("Statistics", "Statistical analyses")
