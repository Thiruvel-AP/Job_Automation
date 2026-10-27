"""The zero-token triage score T (discovery/triage.py): each factor's scale, the formula, the display, and E counting only what the fact bank has. No model, no
network."""
import pytest

from careerops.config import load_yaml
from careerops.discovery import gates, triage
from conftest import PROFILE_FIX
from triage_fakes import NOW, ad_text, jd_body


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb):
    gates.load_roles(settings, fb)


def job(**kw):
    base = {"id": "x", "company": "Acme", "title": "Data Scientist", "description": jd_body(), "sponsor_match": None, "agency": False,
            "work_country": "GB", "seen_on": "2026-10-25", "first_seen": "2026-10-25T00:00:00+00:00"}
    base.update(kw)
    return base


# ------------------------------------------------------------------ A: the family priority
@pytest.mark.parametrize("title,a", [
    ("Data Scientist", 5),                 # priority 1
    ("Gen AI Engineer", 5),                # priority 3
    ("Machine Learning Engineer", 4),      # priority 4
    ("AI Consultant", 4),                  # priority 7
    ("Computer Vision Engineer", 3),       # priority 8
    ("AI Automation Engineer", 3),         # priority 11
    ("Data Analyst", 2),                   # priority 12
    ("Software Engineer, ML", 2),          # priority 15
    ("Plumber", 1),                        # no family
])
def test_role_a_follows_the_family_priority(settings, title, a):
    assert triage.role_score(title, settings) == a


# ------------------------------------------------------------------ L: the level words
@pytest.mark.parametrize("title,l", [("Data Scientist", 5), ("Senior Data Scientist", 2), ("Lead Data Scientist", 1), ("Data Science Manager", 1),
                                     ("Head of Data Science", 1)])
def test_level_l(title, l):
    assert triage.level_score(title) == l


# ------------------------------------------------------------------ Fr: freshness
@pytest.mark.parametrize("days,fr", [(0, 5), (3, 5), (3.1, 4), (7, 4), (7.5, 3), (30, 3), (31, 2), (120, 2), (121, 1), (900, 1), (None, 3)])
def test_freshness_fr(days, fr):
    assert triage.fresh_score(days) == fr


def test_the_age_comes_from_the_ads_dates(settings, fb):
    t = triage.score(job(seen_on=None, first_seen="2026-10-24T12:00:00+00:00"), settings, fb, now=NOW)
    assert t.age_days == pytest.approx(3.0) and t.fr == 5


# ------------------------------------------------------------------ V: the visa factor
def test_visa_scale(settings):
    v = lambda **kw: triage.visa_score(job(**kw), settings)            # noqa: E731
    assert v(sponsor_match="exact (Skilled Worker A)")[0] == 5
    assert v(sponsor_match="fuzzy(0.95) (Acme Ltd)")[0] == 4.5
    assert v(sponsor_match="none")[0] == 3.5 and v(sponsor_match=None)[0] == 3.5
    assert v(sponsor_match="exact (x)", agency=True)[0] == 3.5                                    # an agency counts as unchecked
    no = v(sponsor_match="exact (x)", description=jd_body(extra="Unfortunately we cannot offer visa sponsorship for this role."))
    assert no[0] == 1.5 and "no sponsorship" in no[1]
    far = v(sponsor_match="exact (x)", work_country="DE")
    assert far[0] == 1 and "not home" in far[1]
    assert v(sponsor_match="exact (x)", agency=True, work_country="DE")[0] == 1                  # the lowest that applies


# ------------------------------------------------------------------ P: pay against the floor in answers.yaml
def test_the_floor_is_the_numeric_salary_of_answers_yaml(settings):
    assert triage.salary_floor(settings) == 50000


@pytest.mark.parametrize("top,floor,p", [(80000, 50000, 5), (50000, 50000, 5), (40000, 50000, 1), (None, 50000, 3), (80000, None, 3)])
def test_pay_scale(top, floor, p):
    assert triage.pay_score(top, floor) == p


@pytest.mark.parametrize("header,text,top", [
    ("£60,000 - £80,000", "", 80000), ("£45k to £65k", "", 65000), ("50000-70000", "", 70000), ("Competitive", "", None), ("", "", None),
    ("", "The salary is up to £75,000 depending on experience.", 75000),
    ("", "Contract at £400 per day.", None), ("", "Day rate £450/day inside IR35.", None), ("", "We raised £250 million last year.", None),
    ("", "A £5,000 sign-on bonus.", None),                                                       # under £15,000: a bonus, not a salary
])
def test_the_top_of_the_ads_band(header, text, top):
    assert triage.pay_top(header, text) == top


def test_no_band_in_the_ad_is_neutral_never_a_penalty(settings, fb):
    assert triage.score(job(), settings, fb, floor=50000, now=NOW).p == 3
    assert triage.score(job(salary="£30,000 - £40,000"), settings, fb, floor=50000, now=NOW).p == 1


# ------------------------------------------------------------------ E: evidence from the fact bank
def test_e_is_one_plus_four_times_the_share_of_required_keywords_the_bank_has(settings, fb):
    e = lambda req: triage.evidence(jd_body(req), fb, settings)         # noqa: E731
    assert e(("Python", "SQL", "scikit-learn"))[0] == 5.0
    assert e(("Python", "SQL", "scikit-learn", "Spark"))[0] == pytest.approx(4.0)              # 3 of 4
    assert e(("Python", "Spark"))[0] == pytest.approx(3.0)
    assert e(("Spark", "Airflow"))[0] == 1.0
    assert triage.evidence("We are hiring a thoughtful person. Nothing technical is listed.", fb, settings)[0] == triage.NO_REQUIRED == 3.0    # none found: neutral


def test_e_lists_what_the_bank_has_and_lacks(settings, fb):
    _, have, lack = triage.evidence(jd_body(("Python", "Spark", "SQL", "Airflow")), fb, settings)
    assert set(have) == {"python", "sql"} and set(lack) == {"spark", "airflow"}      # the taxonomy spells them in lower case


def test_ad_terms_the_bank_lacks_cannot_raise_e_however_often_the_ad_says_them(settings, fb):
    plain = triage.evidence(jd_body(("Python", "Spark")), fb, settings)[0]
    shouting = triage.evidence(jd_body(("Python", "Spark"), extra="Spark, Spark and more Spark. Spark is key; Spark Spark Spark. " * 5), fb, settings)[0]
    assert shouting == plain
    assert triage.evidence(jd_body(("Python", "Spark", "Airflow", "Kubernetes")), fb, settings)[0] < plain       # more unknown asks lower it


def test_only_required_keywords_count(settings, fb):
    body = jd_body(("Python",), extra="Nice to have:\n- Spark\n- Airflow\n")
    assert triage.evidence(body, fb, settings)[0] == 5.0


# ------------------------------------------------------------------ the formula and the display
def test_t_is_the_weighted_sum_of_the_six_factors(settings, fb):
    j = job(title="Machine Learning Engineer", sponsor_match="fuzzy(0.95) (Acme)", salary="£70,000", description=jd_body(("Python", "Spark")))
    t = triage.score(j, settings, fb, floor=50000, now=NOW)
    assert (t.a, t.e, t.v, t.l, t.fr, t.p) == (4, 3.0, 4.5, 5, 5, 5)
    assert t.t == pytest.approx(0.30 * 4 + 0.25 * 3.0 + 0.15 * 4.5 + 0.10 * 5 + 0.10 * 5 + 0.10 * 5)
    assert sum(triage.WEIGHTS.values()) == pytest.approx(1.0)


def test_the_best_and_worst_possible_ads(settings, fb):
    best = triage.score(job(sponsor_match="exact (x)", seen_on="2026-10-27", salary="£90,000"), settings, fb, floor=50000, now=NOW)
    assert best.t == pytest.approx(5.0)
    worst = triage.score(job(title="Lead Plumber", description=jd_body(("Spark", "Airflow")), seen_on="2020-01-01", first_seen="2020-01-01T00:00:00+00:00",
                             work_country="DE", salary="£20,000"), settings, fb, floor=50000, now=NOW)
    assert worst.t == pytest.approx(1.0)


def test_the_display_is_the_agreed_line():
    t = triage.Triage(5, 3.8, 3.5, 5, 4, 3, 4.1)
    assert t.text() == "T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3"


def test_the_display_of_a_scored_ad_has_every_factor(settings, fb):
    text = triage.score(job(), settings, fb, floor=50000, now=NOW).text()
    import re
    assert re.fullmatch(r"T \d(\.\d)? = A\d(\.\d)? E\d(\.\d)? V\d(\.\d)? L\d(\.\d)? Fr\d(\.\d)? P\d(\.\d)?", text), text


def test_an_inbox_file_is_scored_from_its_headers(settings, fb, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text(ad_text(title="Data Scientist", age_days=2, salary="£60,000 - £80,000"), encoding="utf-8")
    j = triage.inbox_job(p, settings, fb, None)
    assert j["company"] == "Acme" and j["salary"] == "£60,000 - £80,000" and j["url"].startswith("https://")
    t = triage.score(j, settings, fb, floor=50000, now=NOW)
    assert (t.a, t.l, t.fr, t.p, t.e) == (5, 5, 5, 5, 5.0)
    assert triage.inbox_job(tmp_path / "missing.txt", settings, fb, None) is None
