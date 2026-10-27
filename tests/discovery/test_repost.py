"""The repost / ghost-job signal (discovery/repost.py): information only, never a score. Same company, same title identity (the same SET of title words, so
"Engineer - Munich" is not "Engineer - Berlin"), 2+ URLs, 2+ distinct first-seen dates inside 90 days, a span over 0 days: `possible repost (n listings since
<date>)`. A posting over 120 days old: `evergreen?`."""
from datetime import date

import pytest

from careerops.discovery import repost
from careerops.discovery.repost import Listing

TODAY = date(2026, 10, 27)


def L(title, day, url=None, company="Acme", posted=""):
    return Listing(company=company, title=title, url=url or f"https://acme.com/jobs/{abs(hash((title, day))) % 99999}", first_seen=day, posted=posted)


def flags(listings, which=0):
    return repost.analyse(listings, TODAY)[listings[which].key]


def test_the_same_title_on_two_dates_twenty_days_apart_is_a_repost():
    a, b = L("Data Scientist", "2026-10-01", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2")
    assert flags([a, b], 0) == flags([a, b], 1) == ["possible repost (2 listings since 2026-10-01)"]


def test_one_sweep_with_two_cities_is_not_a_repost():
    rows = [L("Data Scientist - London", "2026-10-20", "https://acme.com/jobs/1"), L("Data Scientist - Manchester", "2026-10-20", "https://acme.com/jobs/2")]
    assert flags(rows, 0) == [] and flags(rows, 1) == []


def test_two_cities_on_two_dates_are_still_not_a_repost_because_the_title_words_differ():
    rows = [L("Engineer - Munich", "2026-10-01", "https://acme.com/jobs/1"), L("Engineer - Berlin", "2026-10-21", "https://acme.com/jobs/2")]
    assert flags(rows, 0) == [] and flags(rows, 1) == []


def test_the_same_title_and_two_urls_on_one_date_is_not_a_repost():
    rows = [L("Data Scientist", "2026-10-20", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-20", "https://acme.com/jobs/2")]
    assert flags(rows) == []


def test_one_url_seen_on_two_dates_is_one_listing_not_a_repost():
    rows = [L("Data Scientist", "2026-10-01", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/1?utm=x#top")]
    assert flags(rows) == []


def test_only_first_seen_dates_inside_90_days_count():
    rows = [L("Data Scientist", "2026-07-01", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2")]           # 118 days ago
    assert flags(rows, 1) == []
    rows = [L("Data Scientist", "2026-08-01", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2")]           # 87 days ago
    assert flags(rows, 1) == ["possible repost (2 listings since 2026-08-01)"]


def test_three_listings_are_counted():
    rows = [L("Data Scientist", d, f"https://acme.com/jobs/{i}") for i, d in enumerate(("2026-09-01", "2026-09-20", "2026-10-10"))]
    assert flags(rows) == ["possible repost (3 listings since 2026-09-01)"]


def test_title_identity_is_the_set_of_words_so_order_case_and_gender_tags_do_not_matter():
    assert repost.title_identity("Senior Data Scientist (m/f/d)") == repost.title_identity("data scientist, SENIOR")
    assert repost.title_identity("Engineer - Munich") != repost.title_identity("Engineer - Berlin")
    assert repost.title_identity("ML Engineer") != repost.title_identity("Senior ML Engineer")
    rows = [L("Senior Data Scientist (m/f/d)", "2026-10-01", "https://acme.com/jobs/1"), L("Data Scientist, Senior", "2026-10-21", "https://acme.com/jobs/2")]
    assert flags(rows)


def test_the_company_is_the_employer_family_not_the_exact_string():
    rows = [L("Data Scientist", "2026-10-01", "https://acme.com/jobs/1", company="Acme Ltd"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2", company="ACME")]
    assert flags(rows)
    rows = [L("Data Scientist", "2026-10-01", "https://acme.com/jobs/1", company="Acme"), L("Data Scientist", "2026-10-21", "https://b.com/jobs/2", company="Bravo")]
    assert flags(rows) == []


def test_a_listing_without_a_date_or_a_company_is_left_out():
    rows = [L("Data Scientist", "", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2"), L("Data Scientist", "2026-10-01", "https://x.com/3", company="")]
    assert flags(rows, 1) == []


# ------------------------------------------------------------------ evergreen
@pytest.mark.parametrize("posted,expected", [("2026-06-28", True), ("2026-06-29", False), ("2025-01-01", True), ("2026-10-26", False), ("", False), ("nonsense", False)])
def test_a_posting_over_120_days_old_is_evergreen(posted, expected):
    assert repost.evergreen(posted, TODAY) is expected                         # 2026-06-29 is exactly 120 days before 2026-10-27


def test_the_evergreen_flag_is_in_the_listings_flags():
    row = L("Data Scientist", "2026-10-25", "https://acme.com/jobs/1", posted="2025-03-01")
    assert flags([row]) == ["evergreen?"]
    both = [row, L("Data Scientist", "2026-10-01", "https://acme.com/jobs/2")]
    assert flags(both, 0) == ["possible repost (2 listings since 2026-10-01)", "evergreen?"]


def test_the_age_is_given_for_the_report():
    assert repost.age_days("2025-10-27", TODAY) == 365 and repost.age_days("", TODAY) is None


def test_the_counts_for_the_report():
    rows = [L("Data Scientist", "2026-10-01", "https://acme.com/jobs/1"), L("Data Scientist", "2026-10-21", "https://acme.com/jobs/2"),
            L("ML Engineer", "2026-10-21", "https://acme.com/jobs/3", posted="2024-01-01"), L("ML Engineer", "2026-10-21", "https://acme.com/jobs/4", company="Other")]
    c = repost.summary(rows, TODAY)
    assert (c.repost_groups, c.repost_listings, c.evergreen, c.total) == (1, 2, 1, 4)
    assert "1 possible repost" in c.line() and "1 evergreen?" in c.line()


def test_flags_are_information_only():
    import inspect
    src = inspect.getsource(repost)
    assert "score" not in src.lower().replace("never a score", "")
