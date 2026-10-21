"""Role families without `skill_lines` of their own (settings roles.families[*].skills_from) and the CV headline.

The fact bank has skill_lines for two families only (data_scientist, ai_engineer) and is never edited by the pipeline: every other role
family points at one of those two in settings (`skills_from`) and prints that family's lines word for word. A bank family of the same name
as the role family replaces the mapping. The headline is the hook library's, word for word; the role family's `headline` is only the fallback."""
import copy
from pathlib import Path

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_yaml
from careerops.discovery import gates
from careerops.tailoring import skills, tailor
from careerops.tailoring.scorer import Keyword

HERE = Path(__file__).parent.parent
GOLDEN = HERE / "golden" / "anaplan"
BANK = HERE / "fixtures" / "golden" / "fact_bank_verbatim.yaml"

DS_LINES = ["data_scientist", "data_consultant", "computer_vision_engineer", "ml_research_scientist", "data_analyst", "data_engineer"]


def bank() -> dict:
    return load_yaml(BANK)


def anaplan_kws() -> list[Keyword]:
    """A data-science-heavy ad: on keywords alone the bank picks data_scientist."""
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    return [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]


def lines_of(fb: dict, fid: str) -> dict[str, str]:
    fam = next(f for f in fb["families"] if f["id"] == fid)
    return {l["label"]: l["items"] for l in fam["skill_lines"]}


# ------------------------------------------------------------------------------------------------ the mapping in settings
def test_every_role_family_names_the_bank_family_its_skill_lines_come_from(settings):
    fams = settings.raw["roles"]["families"]
    assert len(fams) == 15
    for f in fams:
        assert f["skills_from"] == ("data_scientist" if f["name"] in DS_LINES else "ai_engineer"), f["name"]


def test_load_roles_carries_skills_from_and_priority(settings):
    fde = gates.family(settings, "forward_deployed_engineer")
    assert (fde.skills_from, fde.priority) == ("ai_engineer", 5)
    assert gates.family(settings, "data_analyst").skills_from == "data_scientist"


# ------------------------------------------------------------------------------------------------ the lines
def test_forward_deployed_engineer_prints_the_ai_engineer_skill_lines_word_for_word(settings):
    fb = bank()
    assert skills.family_skill_lines(fb, anaplan_kws()) == lines_of(fb, "data_scientist")        # the ad alone would pick data_scientist
    got = skills.family_skill_lines(fb, anaplan_kws(), "forward_deployed_engineer", gates.skills_from(settings, "forward_deployed_engineer"))
    assert got == lines_of(fb, "ai_engineer")
    assert list(got) == list(lines_of(fb, "ai_engineer"))                                          # same order, labels and items


@pytest.mark.parametrize("family", ["data_consultant", "computer_vision_engineer", "ml_research_scientist", "data_analyst", "data_engineer"])
def test_the_data_science_group_prints_the_data_scientist_lines(settings, family):
    fb = bank()
    agent = [Keyword("LLM", 3.0, True), Keyword("agent", 3.0, True), Keyword("RAG", 2.0, True)]    # an ad that alone would pick ai_engineer
    assert skills.family_skill_lines(fb, agent, family, gates.skills_from(settings, family)) == lines_of(fb, "data_scientist")


@pytest.mark.parametrize("family", ["genai_engineer", "ai_consultant", "ai_research_scientist", "ai_automation_engineer", "sde_ai", "sde_ml",
                                    "ml_engineer"])
def test_every_other_family_prints_the_ai_engineer_lines(settings, family):
    fb = bank()
    assert skills.family_skill_lines(fb, anaplan_kws(), family, gates.skills_from(settings, family)) == lines_of(fb, "ai_engineer")


def test_a_bank_family_of_the_same_name_replaces_the_mapping():
    fb = bank()
    own = copy.deepcopy(next(f for f in fb["families"] if f["id"] == "data_scientist"))
    own.update(id="forward_deployed_engineer", match=["forward deployed", "customer"],
               skill_lines=[{"label": "Customer-facing delivery", "items": "Written by the user."}])
    fb["families"].append(own)
    kws = [Keyword("forward deployed", 3.0, True), Keyword("customer", 2.0, True)]
    assert skills.family_skill_lines(fb, kws, "forward_deployed_engineer", "ai_engineer") == {"Customer-facing delivery": "Written by the user."}
    assert skills.family_skill_lines(fb, [Keyword("zzz-nothing", 3.0, True)], "forward_deployed_engineer", "ai_engineer") == \
        {"Customer-facing delivery": "Written by the user."}                                          # no match terms: the family named for the role


def test_the_two_bank_families_still_pick_by_the_ad_when_the_role_family_is_one_of_them(settings):
    fb = bank()
    agent = [Keyword("LLM", 3.0, True), Keyword("agent", 3.0, True), Keyword("RAG", 2.0, True)]
    assert skills.pick_family(fb, agent, "data_scientist", gates.skills_from(settings, "data_scientist"))["id"] == "ai_engineer"
    assert skills.pick_family(fb, anaplan_kws(), "ai_engineer", gates.skills_from(settings, "ai_engineer"))["id"] == "data_scientist"


def test_a_mapping_to_a_family_the_bank_lacks_falls_back_to_the_old_pick():
    fb = bank()
    assert skills.pick_family(fb, anaplan_kws(), "forward_deployed_engineer", "no_such_family")["id"] == "data_scientist"
    assert skills.pick_family(fb, anaplan_kws(), "forward_deployed_engineer", None)["id"] == "data_scientist"


def test_the_fact_bank_file_is_not_touched_by_any_of_it():
    before = BANK.read_bytes()
    skills.family_skill_lines(bank(), anaplan_kws(), "forward_deployed_engineer", "ai_engineer")
    assert BANK.read_bytes() == before


# ------------------------------------------------------------------------------------------------ the headline
def fde_job() -> dict:
    return {"id": "x:fde:1", "source": "manual", "company": "Acme", "title": "Forward Deployed Engineer", "location": "London, UK",
            "url": "https://e.com/1", "description": (GOLDEN / "jd.txt").read_text(encoding="utf-8")}


def test_the_family_headline_is_only_the_fallback_a_library_hook_headline_is_printed_as_written(settings, fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    fb["hook_library"] = [{"id": "lib", "proof": "a4", "terms": ["forecasting", "demand", "mlops", "experimentation", "analytics"],
                           "headline": "Library headline, word for word", "profile_line": "Profile line.", "letter_line": "Letter line."}]
    family, tier, headline, _ = tailor.role_prior(fde_job(), fb, settings)
    assert (family, headline) == ("forward_deployed_engineer", "Forward Deployed Engineer")        # the fallback
    kws = [Keyword(t, 3.0, True) for t in fb["hook_library"][0]["terms"]]

    class Boom:
        def json(self, *a, **k):
            raise AssertionError("no model writes a headline")

    hk = H.build_hook(fde_job(), kws, fb, Boom(), role=headline)
    assert hk.source == "library" and hk.headline == "Library headline, word for word"
    assert "Forward Deployed" not in hk.headline


def test_with_no_library_entry_there_is_no_hook_and_no_model_is_asked(settings, fb_fixture):
    class Boom:
        def json(self, *a, **k):
            raise AssertionError("no model writes a headline")

    fb = copy.deepcopy(fb_fixture)
    fb.pop("hook_library", None)
    kws = [Keyword("forecasting", 3.0, True), Keyword("demand", 2.0, True)]
    notes: list[str] = []
    assert H.build_hook(fde_job(), kws, fb, Boom(), role="Forward Deployed Engineer", notes=notes) is None      # the CV keeps the family headline
    assert notes == ["the fact bank has no hook_library"]


def test_a_title_in_no_family_falls_back_to_the_identity_headline(settings, fb_fixture):
    job = dict(fde_job(), title="Software Engineer")
    assert tailor.role_prior(job, fb_fixture, settings) == (None, None, fb_fixture["identity"].get("headline", ""), [])
