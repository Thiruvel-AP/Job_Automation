"""v3.9.5: a requirement is evidenced by the bullet that REPLACES a retired one, and the hook score uses that same evidence."""
from careerops import hook as hook_mod
from careerops.tailoring import reqmap as RM
from careerops.tailoring.scorer import Keyword

BANK = {
    "deny_terms": [],
    "experience": [{"id": "az", "org": "AstraZeneca", "title": "Data Scientist", "bullets": [
        {"id": "az1", "text": "Built a generative AI agent over 14 datasets."},
        {"id": "az10", "text": "Shipped a generative AI assistant for analysts.", "replaces": ["az1"]},
        {"id": "az2", "text": "Cleaned data quality issues in 859 rows."}]}],
    "projects": [],
    "hook_library": [
        {"id": "h_old", "proof": ["az2"], "terms": []},
        {"id": "h_new", "proof": ["az10"], "terms": []}],
}


def _rm():
    return RM.build([Keyword("generative ai", 3.0, True), Keyword("data quality", 1.0, False)], BANK)


def test_evidence_is_the_replacing_bullet_never_the_retired_one():
    r = _rm().reqs[0]
    assert r.bullet_ids() == ["az10"]
    assert "az1" not in RM.bullet_scores(_rm())


def test_hook_with_the_replacing_proof_beats_weaker_overlap():
    sc = RM.hook_scores(_rm(), BANK)
    assert sc["h_new"][0] > sc["h_old"][0]


def test_a_retired_proof_counts_as_its_replacement():
    bank = {**BANK, "hook_library": [{"id": "h_retired", "proof": ["az1"], "terms": []}]}
    assert RM.hook_scores(RM.build([Keyword("generative ai", 3.0, True)], bank), bank)["h_retired"][1] == 1
