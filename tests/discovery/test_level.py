"""Level fit (discovery/priority.py): the ATS score cannot see seniority, so a level factor L is read from the title words and
P = F x S x L. L only moves a job down: nothing is dropped. The values are `priority.level_factor` in config/settings.yaml."""
import pytest

from careerops.discovery import priority as prio

BASE = {"id": "j", "company": "Acme", "description": "Build models in Python.", "sponsor_match": "exact", "agency": 0,
        "posted_at": None, "ceiling": None, "ceiling_pre": 0.80}


def job_(title, **kw):
    return {**BASE, "title": title, **kw}


@pytest.mark.parametrize("title, level", [
    ("Manager, Applied AI Architects", "manager"),           # the word that ranks highest wins: manager over architect
    ("Data Product & Analytics Engineering Senior Manager", "manager"),
    ("ML Engineering Manager", "manager"),
    ("Director of Data Science", "manager"),
    ("Head of Machine Learning", "manager"),
    ("Lead Data Scientist", "lead"),
    ("Principal Machine Learning Engineer", "lead"),
    ("Staff Data Scientist", "lead"),
    ("Applied AI Architect", "lead"),
    ("Senior Machine Learning Engineer", "senior"),
    ("Sr. Data Scientist", "senior"),
    ("Sr Data Scientist", "senior"),
    ("Data Scientist", "other"),
    ("AI Engineer", "other"),
    ("Junior Data Scientist", "other"),
    ("Data Scientist (Leadership Analytics)", "other"),      # whole words only: "Leadership" is not "lead"
    ("Seniority Review Data Scientist", "other"),
    ("", "other")])
def test_the_level_is_read_from_the_title_words(title, level):
    assert prio.level_of(title) == level


def test_the_shipped_factors_are_the_agreed_ones(settings):
    assert settings["priority"]["level_factor"] == {"manager": 0.3, "lead": 0.4, "senior": 0.6, "other": 1.0}


@pytest.mark.parametrize("title, l", [("Senior Data Scientist", 0.6), ("Lead Data Scientist", 0.4), ("Head of Data", 0.3),
                                      ("Data Scientist", 1.0)])
def test_p_is_f_times_s_times_l(settings, title, l):
    pr = prio.priority(job_(title), settings, tailored=False)
    assert pr.l == l and pr.p == pytest.approx(0.80 * 1.0 * l)
    assert pr.f == 0.80 and pr.s == 1.0                                   # F and S are as before


def test_a_title_without_level_words_has_l_of_one_even_with_no_settings():
    pr = prio.priority(job_("Machine Learning Engineer"), None, tailored=False)
    assert (pr.l, pr.level, pr.p) == (1.0, "other", pytest.approx(0.80))


def test_a_job_without_a_title_key_has_l_of_one():
    job = {k: v for k, v in BASE.items()}
    assert prio.priority(job, None, tailored=False).l == 1.0              # discover's own dicts and old rows may lack the key


def test_the_user_edits_the_values_in_settings(settings):
    settings.raw["priority"]["level_factor"] = {"manager": 0.1, "senior": 0.9}                   # a missing key falls back to the default
    assert prio.level_factor("Senior Data Scientist", settings) == (0.9, "senior")
    assert prio.level_factor("Head of Data", settings) == (0.1, "manager")
    assert prio.level_factor("Lead Data Scientist", settings) == (0.4, "lead")
    assert prio.level_factor("Data Scientist", settings) == (1.0, "other")


def test_the_line_and_flag_name_the_level(settings):
    senior = prio.priority(job_("Senior Data Scientist"), settings, tailored=False)
    plain = prio.priority(job_("Data Scientist"), settings, tailored=False)
    assert "L 0.60" in senior.line() and "senior" in senior.line()
    assert senior.flag() == "LEVEL: senior" and plain.flag() == ""


def test_a_senior_title_ranks_below_a_plain_one_of_equal_fit_and_nobody_is_dropped(settings):
    jobs = [job_("Manager, Applied AI Architects", id="m"), job_("Senior Machine Learning Engineer", id="s"),
            job_("Machine Learning Engineer", id="p"), job_("Applied AI Architect", id="a")]
    order = sorted(jobs, key=lambda j: prio.sort_key(j, settings, tailored=False))
    assert [j["id"] for j in order] == ["p", "s", "a", "m"] and len(order) == len(jobs)
