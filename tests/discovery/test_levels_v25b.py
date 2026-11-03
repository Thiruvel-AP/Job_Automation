"""Fix round after v2.5: `Sr`, `Sr.` and `Senior` are one level. The level rule (roles.levels.exclude) treats them alike: a `senior` pattern excludes all three
spellings, and a list that names only `sr` excludes none of them (the user's list since they removed `\\bsenior\\b`: "Junior, senior and mid only")."""
import pytest
from test_roles import _triage  # noqa: F401  (helper)

from careerops.discovery import gates

REST = [r"\bstaff\b", r"\bprincipal\b", r"\blead\b", r"\bhead of\b", r"\bdirector\b", r"\bmanager\b", r"\bvp\b", r"\b(iii|iv)\b", r"\bintern(ship)?\b"]
USER_LIST = [r"\bsr\.?\b", *REST]                      # config/settings.yaml on 3 Nov 2026: sr, but no senior
SENIOR_LIST = [r"\bsenior\b", *REST]
BOTH_LIST = [r"\bsenior\b", r"\bsr\.?\b", *REST]       # tests/fixtures/config/settings.yaml
NEITHER_LIST = list(REST)

SPELLINGS = ["Senior {}", "Sr. {}", "Sr {}", "sr. {}", "SR {}", "Snr {}", "{} (Senior)", "{} - Sr."]
ROLE = "Data Scientist"


def with_levels(settings, patterns):
    settings.raw["roles"]["levels"]["exclude"] = list(patterns)
    return settings


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("patterns", [USER_LIST, SENIOR_LIST, BOTH_LIST, NEITHER_LIST], ids=["user-list", "senior-only", "both", "neither"])
def test_senior_sr_and_sr_dot_get_the_same_result(settings, patterns, spelling):
    with_levels(settings, patterns)
    assert gates.level_excluded(spelling.format(ROLE), settings) == gates.level_excluded(f"Senior {ROLE}", settings)


def test_the_user_list_allows_the_senior_level_in_every_spelling(settings):
    """The effective list names `sr` and not `senior`: before the fix `Sr. X` was skipped and `Senior X` was not."""
    with_levels(settings, USER_LIST)
    for spelling in SPELLINGS:
        assert gates.level_excluded(spelling.format(ROLE), settings) is None, spelling


def test_a_senior_pattern_excludes_all_three_spellings(settings):
    with_levels(settings, SENIOR_LIST)
    for spelling in SPELLINGS:
        assert gates.level_excluded(spelling.format(ROLE), settings) == r"\bsenior\b", spelling


def test_the_other_levels_are_untouched(settings):
    with_levels(settings, USER_LIST)
    for title in ("Lead Data Scientist", "Staff Data Scientist", "Data Science Manager", "Data Scientist III", "Data Science Intern"):
        assert gates.level_excluded(title, settings), title
    for title in ("Data Scientist", "Junior Data Scientist", "International Data Analyst", "Srinivasan Data Scientist", "Seniority Review Data Scientist"):
        assert gates.level_excluded(title, settings) is None, title


@pytest.mark.parametrize("title, written", [("Sr. Data Scientist", "Senior  Data Scientist"), ("Sr Data Scientist", "Senior Data Scientist"),
                                            ("sr.Data Scientist", "Senior Data Scientist"), ("Data Scientist", "Data Scientist"), ("", "")])
def test_canonical_level_writes_sr_as_senior(title, written):
    assert gates.canonical_level(title) == written


def test_the_triage_gives_senior_and_sr_the_same_verdict(settings, job):
    for patterns in (USER_LIST, SENIOR_LIST):
        with_levels(settings, patterns)
        hits = [any(r.startswith("level excluded (") for r in _triage(settings, job, t).reasons)
                for t in ("Senior AI Engineer", "Sr. AI Engineer", "Sr AI Engineer")]
        assert hits == [patterns is SENIOR_LIST] * 3
