"""The role factor R of the priority: P = F x S x L x R, R = max(0.5, 1 - priority.role_step x (family priority - 1)).
The family `priority` (1-15, settings roles.families) is the user's ranking; it never decides which family a title belongs to
(test_role_match_order.py). A title with no family has no R."""
import pytest

from careerops.discovery import priority as prio


def job_(title, **kw):
    return {"id": "j", "company": "Acme", "title": title, "description": "Build models in Python.", "sponsor_match": "exact", "agency": 0,
            "posted_at": None, "ceiling": None, "ceiling_pre": 0.80, **kw}


def test_the_shipped_role_step_is_003(settings):
    assert settings.raw["priority"]["role_step"] == 0.03


@pytest.mark.parametrize("title, family, rank, r", [
    ("Data Scientist", "data_scientist", 1, 1.00),
    ("Forward Deployed Engineer", "forward_deployed_engineer", 5, 0.88),
    ("Data Analyst", "data_analyst", 12, 0.67),
    ("Software Development Engineer - ML", "sde_ml", 15, 0.58)])
def test_r_for_priorities_1_5_12_and_15(settings, title, family, rank, r):
    got = prio.role_factor(title, settings)
    assert got is not None and got.family == family and got.rank == rank
    assert round(got.r, 2) == r


def test_r_never_goes_below_one_half(settings):
    settings.raw["priority"]["role_step"] = 0.10
    assert prio.role_factor("Software Development Engineer - ML", settings).r == 0.5


def test_a_job_with_no_matching_family_is_not_given_an_r(settings):
    assert prio.role_factor("Software Engineer", settings) is None
    pr = prio.priority(job_("Software Engineer"), settings, tailored=False)
    assert pr.r is None and pr.family is None
    assert pr.p == pytest.approx(0.80 * 1.0 * 1.0)                         # F x S x L, nothing else
    assert " x R " not in pr.line()


def test_p_is_f_times_s_times_l_times_r(settings):
    pr = prio.priority(job_("Forward Deployed Engineer"), settings, tailored=False)
    assert pr.r == pytest.approx(0.88) and pr.p == pytest.approx(0.80 * 1.0 * 1.0 * 0.88)
    senior = prio.priority(job_("Senior Forward Deployed Engineer"), settings, tailored=False)
    assert senior.p == pytest.approx(0.80 * 1.0 * 0.6 * 0.88)             # L still multiplies


def test_the_line_shows_r_with_the_family_and_its_priority(settings):
    pr = prio.priority(job_("Forward Deployed Engineer"), settings, tailored=False)
    assert "x R 0.88 (forward_deployed_engineer, #5)" in pr.line()
    assert prio.priority(job_("Forward Deployed Engineer"), settings, tailored=False).role_text() == "x R 0.88 (forward_deployed_engineer, #5)"


def test_without_settings_there_is_no_r():
    pr = prio.priority(job_("Data Scientist"))
    assert pr.r is None and pr.p == pytest.approx(0.80)


def test_the_better_ranked_family_wins_a_tie_on_the_rest(settings):
    a = prio.priority(job_("Data Scientist"), settings, tailored=False)
    b = prio.priority(job_("Software Development Engineer - ML"), settings, tailored=False)
    assert a.p > b.p
    assert prio.sort_key(job_("Data Scientist", id="a"), settings, tailored=False) < prio.sort_key(
        job_("Software Development Engineer - ML", id="b"), settings, tailored=False)
