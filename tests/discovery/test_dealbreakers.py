"""Hard disqualifiers (discovery/triage.disqualify) and your dealbreakers (discovery/dealbreakers.py): a dealbreaker needs an ad sentence that STATES the fact,
and the sentence is quoted. An inferred one is not a hit. A missing salary never breaks a pay floor; a senior-sounding title with no stated years breaks nothing."""
import pytest

from careerops.config import load_yaml
from careerops.discovery import blacklist, dealbreakers, gates, liveness, triage
from conftest import PROFILE_FIX
from triage_fakes import ad_text, jd_body


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb):
    gates.load_roles(settings, fb)


def ad(sentence=""):
    return jd_body(extra=sentence)


# ------------------------------------------------------------------ a stated hit is quoted
def test_a_stated_hit_is_quoted_in_the_agreed_form():
    hit = dealbreakers.find(["people management"], ad("You will line-manage a team of 4 data scientists."))
    assert hit is not None
    assert hit.line() == 'DEALBREAKER "people management" ← "You will line-manage a team of 4 data scientists."'


@pytest.mark.parametrize("phrase,sentence", [
    ("people management", "The role has 3 direct reports."),
    ("people management", "You will manage a team of engineers."),
    ("people management", "Previous people management experience is needed for this role."),
    ("SC clearance", "You must be eligible for SC clearance."),
    ("SC clearance", "Candidates need active security clearance."),
    ("clearance", "DV clearance is held by the whole team."),
    ("weekend work", "Occasional weekend work is expected."),
    ("on-call", "You will be on-call one week in four."),
    ("5 days on-site", "This is a fully on-site role."),
    ("5 days on-site", "You will be in the office five days a week."),
    ("4 days on-site", "Four days a week in the office."),
])
def test_each_stated_fact_is_a_hit_with_its_sentence(phrase, sentence):
    hit = dealbreakers.find([phrase], ad(sentence))
    assert hit is not None and hit.sentence == sentence


# ------------------------------------------------------------------ an inferred one is not
@pytest.mark.parametrize("phrase,sentence", [
    ("people management", "There is no people management in this role."),
    ("people management", "You will not line-manage anyone."),
    ("people management", "Mentoring is welcome, but it is not people management."),
    ("people management", "Line management is optional and a nice to have."),
    ("SC clearance", "SC clearance is desirable but not required."),
    ("SC clearance", "No security clearance is needed."),
    ("5 days on-site", "Two days a week in the office."),
    ("weekend work", "We do not expect weekend work."),
])
def test_a_denied_or_optional_fact_is_not_a_hit(phrase, sentence):
    assert dealbreakers.find([phrase], ad(sentence)) is None


def test_a_senior_sounding_title_alone_is_not_a_hit():
    text = "Title: Team Lead, Data Science\n" + ad("We are a friendly group.")
    assert dealbreakers.find(["people management"], text) is None


def test_a_missing_salary_never_breaks_a_pay_floor():
    assert dealbreakers.find(["salary below £50,000"], ad("We offer a competitive package."), top_pay=None) is None


def test_a_stated_band_under_the_floor_is_a_hit_with_its_sentence():
    hit = dealbreakers.find(["salary below £50,000"], ad("The salary is £35,000 - £42,000."), top_pay=42000)
    assert hit is not None and "£35,000" in hit.sentence
    assert dealbreakers.find(["salary below £50,000"], ad("The salary is £45,000 - £65,000."), top_pay=65000) is None


def test_years_need_a_stated_number_a_senior_title_does_not_count():
    assert dealbreakers.find(["5+ years"], "Title: Senior Principal Data Scientist\n" + ad("Lead our data science work.")) is None
    hit = dealbreakers.find(["5+ years"], ad("You have 6 years of experience in machine learning."))
    assert hit is not None and "6 years" in hit.sentence
    assert dealbreakers.find(["5+ years"], ad("You have 3 years of experience in machine learning.")) is None
    assert dealbreakers.find(["more than 5 years"], ad("You have 5 years of experience in data science.")) is None


def test_outside_the_uk_applies_only_to_work_outside_your_home_country():
    s = ad("You will be in the office five days a week.")
    assert dealbreakers.find(["5 days on-site outside the UK"], s, outside_home=False) is None
    assert dealbreakers.find(["5 days on-site outside the UK"], s, outside_home=True) is not None


def test_the_first_dealbreaker_that_hits_wins_and_blank_phrases_are_ignored():
    hit = dealbreakers.find(["", "weekend work", "on-call"], ad("You will be on-call. Weekend work is normal."))
    assert hit.phrase == "weekend work"


# ------------------------------------------------------------------ the hard stops, in order
@pytest.fixture
def blacklist_file(settings, monkeypatch, tmp_path):
    path = tmp_path / "blacklist.yaml"
    monkeypatch.setattr(blacklist, "path_for", lambda s: path)
    return path


def job(**kw):
    base = {"id": "a.txt", "company": "Acme", "title": "Data Scientist", "description": ad("A friendly team."), "url": "https://acme.example/jobs/1",
            "apply_url": None, "agency": False, "work_country": "GB", "salary": None}
    base.update(kw)
    return base


def test_a_clean_ad_passes(settings, db, blacklist_file):
    assert triage.disqualify(job(), settings, db, archive=[]) is None


def test_the_blacklist_stops_it_with_the_reason(settings, db, blacklist_file):
    blacklist_file.write_text("blacklist:\n  - {name: Acme, scope: company, since: 2026-01-05, reason: ghosted me twice}\n", encoding="utf-8")
    d = triage.disqualify(job(), settings, db, archive=[])
    assert d.stage == "blacklist" and "since 2026-01-05" in d.reason and "ghosted me twice" in d.reason


def test_an_unreadable_blacklist_stops_everything_rather_than_passing(settings, db, blacklist_file):
    blacklist_file.write_text("blacklist: [not, a, list, of, entries", encoding="utf-8")
    assert triage.disqualify(job(), settings, db, archive=[]).stage == "blacklist"


def test_the_same_role_under_another_channel_stops_it(settings, db, blacklist_file):
    db.upsert_job({"id": "reed:1", "source": "reed", "company": "Hays", "title": "Data Scientist", "description": "Our client, Acme, hires."})
    db.update_job("reed:1", agency=1)
    d = triage.disqualify(job(), settings, db, archive=[])
    assert d.stage == "channel" and "Hays | Data Scientist" in d.reason


def test_the_same_role_in_the_archive_stops_it(settings, db, blacklist_file):
    d = triage.disqualify(job(), settings, db, archive=[("Acme", "Data Scientist")])
    assert d is not None and d.stage == "channel" and "Acme/Data Scientist" in d.reason


def test_an_agency_ad_that_hides_the_employer_stops_it(settings, db, blacklist_file):
    d = triage.disqualify(job(agency=True, company="Hays", description=ad("A great opportunity.")), settings, db, archive=[])
    assert d.stage == "channel" and "does not name the employer" in d.reason


def test_the_level_exclude_list_stops_it(settings, db, blacklist_file):
    d = triage.disqualify(job(title="Senior Data Scientist"), settings, db, archive=[])
    assert d.stage == "level" and "senior" in d.reason.lower()


def test_a_dealbreaker_stops_it_with_the_quoted_sentence(settings, db, blacklist_file):
    settings.raw["targeting"]["dealbreakers"] = ["people management"]
    d = triage.disqualify(job(description=ad("You will line-manage a team of 4.")), settings, db, archive=[])
    assert d.stage == "dealbreaker" and d.reason == 'DEALBREAKER "people management" ← "You will line-manage a team of 4."'
    assert triage.disqualify(job(), settings, db, archive=[]) is None                          # nothing stated: nothing broken


def test_a_pay_dealbreaker_reads_the_ads_band_not_a_missing_one(settings, db, blacklist_file):
    settings.raw["targeting"]["dealbreakers"] = ["salary below £50,000"]
    assert triage.disqualify(job(), settings, db, archive=[]) is None
    assert triage.disqualify(job(salary="£30,000 - £40,000"), settings, db, archive=[]).stage == "dealbreaker"


def test_the_order_is_liveness_then_blacklist_then_channel_then_level_then_dealbreakers(settings, db, blacklist_file):
    settings.raw["targeting"]["dealbreakers"] = ["weekend work"]
    blacklist_file.write_text("blacklist:\n  - {name: Acme, scope: company, since: 2026-01-05, reason: no}\n", encoding="utf-8")
    bad = job(title="Senior Data Scientist", agency=True, description=ad("Weekend work is normal."))
    assert triage.disqualify(bad, settings, db, archive=[], alive=lambda j: "the posting is closed (404)").stage == "liveness"
    assert triage.disqualify(bad, settings, db, archive=[]).stage == "blacklist"
    blacklist_file.write_text("blacklist: []\n", encoding="utf-8")
    assert triage.disqualify(bad, settings, db, archive=[]).stage == "channel"
    assert triage.disqualify(job(title="Senior Data Scientist", description=ad("Weekend work is normal.")), settings, db, archive=[]).stage == "level"
    assert triage.disqualify(job(description=ad("Weekend work is normal.")), settings, db, archive=[]).stage == "dealbreaker"


def test_no_dealbreakers_set_means_none_are_checked(settings, db, blacklist_file):
    settings.raw["targeting"]["dealbreakers"] = []
    assert triage.disqualify(job(description=ad("You will line-manage a team of 4.")), settings, db, archive=[]) is None
