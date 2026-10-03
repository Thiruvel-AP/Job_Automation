"""MockLLM determinism."""
from careerops.llm import MockLLM


def _proj(pid):
    return {"id": pid, "name": pid, "bullets": [{"id": f"{pid}1", "text": "unrelated work", "terms": []}]}


def test_tailor_breaks_project_ties_with_preferred_list():
    ctx = {"keywords": [{"term": "python", "weight": 2, "required": True}], "experience": [],
           "projects": [_proj(p) for p in ("a", "b", "c", "d")], "profile_facts": ["x"]}
    assert MockLLM().json("tailor_cv", "", "", ctx)["projects"] == ["a", "b"]
    ctx["preferred_projects"] = ["d", "c"]
    assert MockLLM().json("tailor_cv", "", "", ctx)["projects"] == ["d", "c"]


def _bullets(prefix, n):
    return [{"id": f"{prefix}{i}", "text": "work", "terms": []} for i in range(n)]


def test_tailor_selects_the_one_page_shape_most_recent_three_bullets_the_rest_two():
    ctx = {"keywords": [], "profile_facts": ["x"], "projects": [],
           "experience": [{"id": "new", "bullets": _bullets("n", 5)}, {"id": "old", "bullets": _bullets("o", 5)}]}
    out = MockLLM().json("tailor_cv", "", "", ctx)
    assert [len(out["experience"][k]) for k in ("new", "old")] == [3, 2]


def test_keywords_come_back_with_an_empty_team_by_default():
    assert MockLLM().json("extract_keywords", "", "", {})["team"] == ""


LETTER_CTX = {"company": "Acme", "title": "Data Scientist", "evidence": ["Built a thing."]}


def test_mock_letter_has_no_gap_sentence_when_gaps_is_empty():
    for ctx in (LETTER_CTX, {**LETTER_CTX, "gaps": []}):
        paras = MockLLM().json("cover_letter", "", "", ctx)["paragraphs"]
        assert len(paras) == 3


def test_mock_letter_names_one_gap_in_one_short_plain_sentence():
    paras = MockLLM().json("cover_letter", "", "", {**LETTER_CTX, "gaps": ["kubernetes", "terraform"]})["paragraphs"]
    assert len(paras) == 4
    hits = [p for p in paras if "kubernetes" in p.lower()]
    assert len(hits) == 1 and len(hits[0].split()) <= 25 and "terraform" not in " ".join(paras).lower()
    assert not any(w in hits[0].lower() for w in ("sorry", "apolog", "unfortunately"))
