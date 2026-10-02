"""Role families, tiers and level exclusion (M4b)."""
import copy
import json

import pytest

from careerops.cli import retriage
from careerops.config import Settings, load_yaml
from careerops.discovery import gates

FAMILY_REASON = "title not in target families"

EXAMPLES = [
    ("Data Scientist", "data_scientist", 1),
    ("Decision Scientist", "data_scientist", 1),
    ("Applied Data Scientist", "data_scientist", 1),
    ("AI Engineer", "ai_engineer", 1),
    ("AI/ML Engineer", "ai_engineer", 1),
    ("LLM Engineer", "ai_engineer", 1),
    ("GenAI Engineer", "ai_engineer", 1),
    ("Generative AI Engineer", "ai_engineer", 1),
    ("Applied AI Engineer", "ai_engineer", 1),
    ("AI Software Engineer", "ai_engineer", 1),
    ("Machine Learning Engineer", "ml_engineer", 2),
    ("ML Engineer", "ml_engineer", 2),
    ("MLOps Engineer", "ml_engineer", 2),
    ("Applied Scientist", "ml_engineer", 2),
    ("NLP Engineer", "ml_engineer", 2),
    ("Data Analyst", "data_analyst", 3),
    ("Product Analyst", "data_analyst", 3),
    ("Insight Analyst", "data_analyst", 3),
    ("Insights Analyst", "data_analyst", 3),
    ("BI Analyst", "data_analyst", 3),
    ("Business Intelligence Analyst", "data_analyst", 3),
    ("Analytics Engineer", "data_analyst", 3),
    ("Reporting Analyst", "data_analyst", 3),
]


def _triage(settings, job, title):
    return gates.triage(dict(job, title=title), settings, None)


@pytest.mark.parametrize("title,family,tier", EXAMPLES)
def test_example_titles_map_to_family(settings, title, family, tier):
    assert gates.classify_role(title, settings) == (family, tier)


def test_ambiguous_title_takes_higher_tier(settings):
    assert gates.classify_role("Data Scientist / ML Engineer", settings) == ("data_scientist", 1)
    assert gates.classify_role("ML Engineer / Data Analyst", settings) == ("ml_engineer", 2)


@pytest.mark.parametrize("title", ["Senior Data Scientist", "Lead AI Engineer", "Staff Machine Learning Engineer",
                                   "Principal Data Scientist", "Data Science Manager", "Data Scientist III",
                                   "Data Science Intern", "Data Scientist Internship"])
def test_excluded_levels_skip_with_level_reason(settings, job, title):
    v = _triage(settings, job, title)
    assert not v.passed
    assert any(r.startswith("level excluded (") for r in v.reasons), v.reasons
    assert FAMILY_REASON not in v.reasons


def test_international_and_internal_not_excluded(settings, job):
    """Regression: the old 'intern' pattern matched 'International' and 'Internal'."""
    for title, fam in (("International Data Analyst", "data_analyst"), ("Internal Tools AI Engineer", "ai_engineer")):
        v = _triage(settings, job, title)
        assert not any("level excluded" in r for r in v.reasons), (title, v.reasons)
        assert FAMILY_REASON not in v.reasons
        assert v.family == fam


@pytest.mark.parametrize("title", ["Data Engineer", "Research Scientist", "Software Engineer"])
def test_out_of_family_titles_skip_with_family_reason(settings, job, title):
    assert gates.classify_role(title, settings) is None
    v = _triage(settings, job, title)
    assert not v.passed and FAMILY_REASON in v.reasons
    assert v.family is None and v.tier is None


def test_shipped_roles_config_validates(settings):
    fams = gates.load_roles(settings, load_yaml(settings.path("fact_bank")))
    assert [f.tier for f in fams] == sorted(f.tier for f in fams)
    assert {f.name for f in fams} == {"data_scientist", "ai_engineer", "ml_engineer", "data_analyst"}


def test_unknown_project_id_is_clear_load_error(settings):
    raw = copy.deepcopy(settings.raw)
    raw["roles"]["families"][1]["projects"].append("no_such_project")
    bad = Settings(raw=raw, root=settings.root)
    with pytest.raises(ValueError, match=r"ai_engineer.*unknown project id 'no_such_project'"):
        gates.load_roles(bad, load_yaml(settings.path("fact_bank")))


# ---------------------------------------------------------------- retriage
def _insert(db, job, jid, title, status, **extra):
    db.upsert_job(dict(job, id=jid, title=title))
    db.update_job(jid, status=status, **extra)


def test_retriage_backfills_and_never_touches_applied(settings, db, job):
    _insert(db, job, "t:ok", "Data Scientist", "triaged")
    _insert(db, job, "t:de", "Data Engineer", "triaged")
    _insert(db, job, "t:skip", "AI Engineer", "skipped", reason="title not DS/AI")
    _insert(db, job, "t:rev", "Senior Data Scientist", "needs_review", flags=["existing flag"])
    _insert(db, job, "t:appl", "Senior Data Scientist", "applied")
    before = dict(db.job("t:appl"))

    counts = retriage(db, settings)

    ok = db.job("t:ok")
    assert (ok["family"], ok["tier"], ok["status"]) == ("data_scientist", 1, "triaged")
    assert ok["ceiling_pre"] is not None and 0 < ok["ceiling_pre"] <= 1
    assert db.job("t:de")["status"] == "skipped" and FAMILY_REASON in db.job("t:de")["reason"]
    assert db.job("t:skip")["status"] == "triaged" and db.job("t:skip")["family"] == "ai_engineer"

    rev = db.job("t:rev")
    assert rev["status"] == "needs_review"                     # post-tailoring status kept
    assert (rev["family"], rev["tier"]) == ("data_scientist", 1) and rev["ceiling_pre"] is not None
    flags = json.loads(rev["flags"])
    assert "existing flag" in flags
    skip_flags = [f for f in flags if f.startswith("new rules would skip: ")]
    assert len(skip_flags) == 1 and "level excluded" in skip_flags[0]

    assert dict(db.job("t:appl")) == before                    # applied rows are never touched
    assert counts["applied_untouched"] == 1

    retriage(db, settings)                                     # idempotent: no duplicate flag
    flags = json.loads(db.job("t:rev")["flags"])
    assert sum(f.startswith("new rules would skip: ") for f in flags) == 1
