"""v3.5.3 (10 Nov 2026, fix 3): an entry's `situation:` ("Needed ...: ") opens only the bullet it belongs to: the entry's `situation_bullet:`, else
its first bullet in the fact bank (or a bullet that `replaces:` that one). On the 10 Nov RightShip CV the Mining Insights project showed p_ia2 (the
team bullet) first, and p_ia1's situation was glued onto it."""
import copy

from careerops.tailoring import layout, tailor

SIT = "Needed structure in unstructured airline support tickets"
IATA = {"id": "iata", "name": "Mining Insights from Customer Feedback", "situation": SIT,
        "bullets": [{"id": "p_ia1", "text": "Designed the preprocessing pipeline and the clustering pipeline (K-Means, HAC, DBSCAN)."},
                    {"id": "p_ia2", "text": "In a five person team, coordinated per member Git branches merged through pull requests."}]}


def shown(entry: dict, ids: list[str]) -> dict:
    """A render-context entry as tailor._context builds it: the bank entry, its situation owner, the shown bullets."""
    text = {b["id"]: b["text"] for b in entry["bullets"]}
    return {**entry, "situation_owner": layout.situation_owner(entry), "bullet_ids": list(ids), "bullets": [text[i] for i in ids]}


def bullets(ctx: dict, sec: str = "projects") -> list[str]:
    return layout.with_openers(ctx)[sec][0]["bullets"]


def test_the_mining_insights_team_bullet_never_takes_the_clustering_bullets_situation():
    """The regression: p_ia2 alone on the CV (p_ia1 not chosen) gets no "Needed ...:" opener."""
    ctx = {"experience": [], "projects": [shown(IATA, ["p_ia2"])]}
    assert bullets(ctx) == [IATA["bullets"][1]["text"]]


def test_the_situation_opens_its_own_bullet_wherever_the_cv_shows_it():
    ctx = {"experience": [], "projects": [shown(IATA, ["p_ia2", "p_ia1"])]}
    out = bullets(ctx)
    assert out[0] == IATA["bullets"][1]["text"]                                     # the team bullet, first on the CV, untouched
    assert out[1] == f"{SIT}: designed the preprocessing pipeline and the clustering pipeline (K-Means, HAC, DBSCAN)."
    assert sum(SIT in b for b in out) == 1
    first = {"experience": [], "projects": [shown(IATA, ["p_ia1", "p_ia2"])]}       # the owner first: as before
    assert bullets(first)[0].startswith(f"{SIT}: designed")


def test_the_owner_is_situation_bullet_else_the_first_bank_bullet_plus_a_bullet_that_replaces_it():
    assert layout.situation_owner(IATA) == ["p_ia1"]
    assert layout.situation_owner({**IATA, "situation_bullet": "p_ia2"}) == ["p_ia2"]
    replacer = copy.deepcopy(IATA)
    replacer["bullets"].append({"id": "p_ia3", "replaces": ["p_ia1"], "text": "Clustered the tickets four ways."})
    assert layout.situation_owner(replacer) == ["p_ia1", "p_ia3"]
    assert layout.situation_owner({**IATA, "situation": "  "}) == []


def test_no_opener_on_a_verbatim_owner_or_beside_a_bullet_with_its_own_opener():
    ctx = {"experience": [], "projects": [shown(IATA, ["p_ia1", "p_ia2"])], "verbatim": ["p_ia1"]}
    assert bullets(ctx) == [b["text"] for b in IATA["bullets"]]
    ctx = {"experience": [], "projects": [shown(IATA, ["p_ia1", "p_ia2"])], "situated": ["p_ia2"]}
    assert bullets(ctx) == [b["text"] for b in IATA["bullets"]]


def test_the_length_fit_counts_the_opener_words_only_on_the_owner_bullet():
    e = shown(IATA, ["p_ia2", "p_ia1"])
    assert layout.opener_words(e, 0) == 0 and layout.opener_words(e, 1) == len(SIT.split())


def test_the_render_context_carries_each_entrys_situation_owner_from_the_bank(fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    p1 = next(p for p in fb["projects"] if p["id"] == "p1")
    p1["situation"] = "Needed to know which customers would leave"
    sel = {"experience": {e["id"]: [e["bullets"][0]["id"]] for e in fb["experience"]}, "projects": ["p1"], "project_bullets": {"p1": ["p1c"]}}
    ctx = tailor._context(fb, sel, {}, "Profile.", [], "AI Engineer")
    entry = next(p for p in ctx["projects"] if p["id"] == "p1")
    assert entry["situation_owner"] == ["p1a"] and entry["bullet_ids"] == ["p1c"]
    assert layout.with_openers(ctx)["projects"][0]["bullets"] == [p1["bullets"][2]["text"]]     # p1c is not p1a: no opener
