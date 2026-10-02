"""MockLLM determinism."""
from careerops.llm import MockLLM


def _proj(pid):
    return {"id": pid, "name": pid, "bullets": [{"id": f"{pid}1", "text": "unrelated work", "terms": []}]}


def test_tailor_breaks_project_ties_with_preferred_list():
    ctx = {"keywords": [{"term": "python", "weight": 2, "required": True}], "experience": [],
           "projects": [_proj(p) for p in ("a", "b", "c", "d")], "profile_facts": ["x"]}
    assert MockLLM().json("tailor_cv", "", "", ctx)["projects"] == ["a", "b", "c"]
    ctx["preferred_projects"] = ["d", "c"]
    assert MockLLM().json("tailor_cv", "", "", ctx)["projects"] == ["d", "c", "a"]
