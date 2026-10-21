"""Role families match in LIST order (most specific first); `priority` is the user's ranking and never decides a match."""
import copy

import pytest

from careerops.discovery import gates

TABLE = [
    ("Data Scientist", "data_scientist"),
    ("Decision Scientist", "data_scientist"),
    ("Applied AI Engineer, Codex", "ai_engineer"),
    ("Senior AI/ML Engineer", "ai_engineer"),
    ("LLM Engineer", "ai_engineer"),
    ("AI Software Engineer", "ai_engineer"),
    ("Generative AI Engineer", "genai_engineer"),
    ("GenAI Engineer", "genai_engineer"),
    ("Gen AI Developer", "genai_engineer"),
    ("Junior Machine Learning Engineer", "ml_engineer"),
    ("Machine Learning Engineer, Synthetic Data", "ml_engineer"),
    ("Forward Deployed Engineer", "forward_deployed_engineer"),
    ("Data Science Consultant", "data_consultant"),
    ("Senior Data & Insights Consultant", "data_consultant"),
    ("AI Consultant", "ai_consultant"),
    ("Computer Vision Engineer", "computer_vision_engineer"),
    ("Machine Learning Engineer, Computer Vision", "computer_vision_engineer"),
    ("Research Scientist, LLMs", "ai_research_scientist"),
    ("Research Engineer, Machine Learning", "ml_research_scientist"),
    ("AI Automation Engineer", "ai_automation_engineer"),
    ("Data Analyst", "data_analyst"),
    ("Analytics Engineer", "data_engineer"),
    ("Data Engineer", "data_engineer"),
    ("Software Engineer, AI Platform", "sde_ai"),
    ("Software Development Engineer - ML", "sde_ml"),
    ("Software Engineer", None),
    ("Applied AI Architect, Startups", None),
    ("Manager, Applied AI Architects", None),
]


def _family(settings, title):
    hit = gates.classify_role(title, settings)
    return hit[0] if hit else None


@pytest.mark.parametrize("title,family", TABLE)
def test_title_maps_to_family(settings, title, family):
    assert _family(settings, title) == family


def test_first_family_in_list_order_wins_whatever_its_priority_or_tier(settings):
    s = copy.deepcopy(settings)
    fams = s.raw["roles"]["families"]
    for f in fams:                                    # a ranking that disagrees with the list must change nothing
        f["priority"], f["tier"] = 16 - f["priority"], 4 - f["tier"]
    assert [gates.classify_role(t, s) is not None for t, _ in TABLE] == [fam is not None for _, fam in TABLE]
    assert [_family(s, t) for t, _ in TABLE] == [fam for _, fam in TABLE]


def test_load_roles_keeps_list_order(settings):
    assert [f.name for f in gates.load_roles(settings)] == [f["name"] for f in settings.raw["roles"]["families"]]
