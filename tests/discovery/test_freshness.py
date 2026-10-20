"""Freshness (discovery/freshness.py): the posting time comes from the source, the age in hours puts a job in tier 0 (<= 24 h),
1 (<= 72 h), 2 (<= 7 d) or 3 (older); the order is tier first, then P = F x S, and nothing is dropped. `live --fresh` takes tier 0
only; `discover --report` counts the tiers. No network, no model."""
from datetime import datetime, timedelta, timezone

import pytest

from careerops import cli
from careerops.apply.live import Hooks, Live
from careerops.discovery import discover as disc, freshness as fr, priority as prio
from conftest import fixture

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def ago_now(hours: float) -> str:
    """`hours` before the real clock: for the tests that go through code which reads the time itself (live, the report)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ the tiers
@pytest.mark.parametrize("hours, tier", [(0, 0), (23.9, 0), (24.0, 0), (24.01, 1), (72.0, 1), (72.01, 2), (168.0, 2), (168.01, 3),
                                         (69 * 24, 3), (None, 3)])
def test_tier_boundaries(hours, tier):
    assert fr.tier_of(hours) == tier


def test_a_future_time_is_age_zero():
    assert fr.age_hours({"posted_at": ago(-5), "posted_at_source": "reed.date"}, NOW) == 0.0


def test_the_anaplan_ad_is_69_days_old_and_tier_3_by_first_published_not_10_days_by_updated_at():
    """greenhouse.first_published 69 days ago; updated_at (never read) was 10 days ago after a repost."""
    job = {"posted_at": ago(69 * 24), "posted_at_source": "greenhouse.first_published"}
    assert fr.age_hours(job, NOW) == pytest.approx(69 * 24) and fr.tier(job, NOW) == 3
    assert "posted 1656 h ago (69 d) (Greenhouse first_published), tier 3 (older)" == fr.describe(job, NOW)


# ------------------------------------------------------------------ where the time comes from
@pytest.mark.parametrize("source", ["greenhouse.first_published", "lever.createdAt", "ashby.publishedAt", "reed.date", "adzuna.created",
                                    "gmail.received"])
def test_every_listed_source_is_trusted_for_the_age(source):
    job = {"posted_at": ago(5), "posted_at_source": source, "seen_on": "2026-01-01"}
    assert fr.posted(job)[1] == source and fr.age_hours(job, NOW) == pytest.approx(5.0)


@pytest.mark.parametrize("source", [None, "", "first_seen", "page.datePosted", "greenhouse.updated_at"])
def test_anything_else_counts_from_first_seen(source):
    job = {"posted_at": ago(5), "posted_at_source": source, "seen_on": "2026-10-14"}
    dt, via = fr.posted(job)
    assert via == fr.FIRST_SEEN and dt == datetime(2026, 10, 14, tzinfo=timezone.utc) and fr.tier(job, NOW) == 1


def test_first_seen_is_the_discover_day_then_the_database_timestamp_and_nothing_is_tier_3():
    assert fr.age_hours({"seen_on": "2026-10-15"}, NOW) == pytest.approx(12.0)
    assert fr.age_hours({"first_seen": ago(6)}, NOW) == pytest.approx(6.0)               # a hand-added JD: when it was added
    assert fr.age_hours({}, NOW) is None and fr.tier({}, NOW) == 3 and "date unknown" in fr.describe({}, NOW)


def test_sqlite_rows_work_like_dicts(db):
    db.upsert_job({"id": "m:1", "source": "manual", "company": "A", "title": "T", "location": "L", "url": "u", "apply_url": "u",
                   "description": "d", "posted_at": ago(30), "posted_at_source": "lever.createdAt"})
    row = db.job("m:1")
    assert row["posted_at_source"] == "lever.createdAt" and fr.tier(row, NOW) == 1


@pytest.mark.parametrize("value, ok", [("2026-10-15", True), ("2026-10-15T09:30", True), ("2026-10-15T09:30:00Z", True),
                                       ("2026-10-15T09:30:00.123+01:00", True), ("2026-10-15 09:30:00", True), ("15/10/2026", False),
                                       ("yesterday", False), ("", False), (None, False)])
def test_is_iso(value, ok):
    assert fr.is_iso(value) is ok and (fr.parse(value) is not None) is ok


def test_lever_milliseconds_become_an_iso_time():
    assert fr.from_millis(1760529600000) == "2025-10-15T12:00:00+00:00" and fr.from_millis("x") is None and fr.from_millis(None) is None


# ------------------------------------------------------------------ counts
def test_tier_counts_and_line():
    jobs = [{"posted_at": ago(h), "posted_at_source": "reed.date"} for h in (1, 20, 30, 100, 500, 5000)] + [{}]
    assert fr.tier_counts(jobs, NOW) == [2, 1, 1, 3]
    assert fr.counts_line([2, 1, 1, 3]) == "tier 0 (<= 24 h): 2 · tier 1 (<= 72 h): 1 · tier 2 (<= 7 d): 1 · tier 3 (older): 3"


# ------------------------------------------------------------------ live --fresh
def _put(folder, name, company, jd, **headers):
    folder.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k.capitalize()}: {v}\n" for k, v in {"company": company, "title": "AI Engineer", "url": "https://e.com/1", **headers}.items())
    (folder / name).write_text(head + "\n" + jd, encoding="utf-8")
    return folder / name


@pytest.fixture
def inbox(settings, db, tmp_path):
    from careerops.discovery import register as sponsors
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    jd = fixture("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")
    box = settings.path("jds") / "inbox"
    # a 5-hour-old ad (tier 0), a 2-day-old one (tier 1) and the 69-day-old Anaplan-like one (tier 3), all with the same fit
    return [_put(box, "a_new.txt", "Northwind Health AI", jd, posted=ago_now(5), **{"posted via": "greenhouse.first_published"}),
            _put(box, "b_two_days.txt", "Northwind Health AI", jd, posted=ago_now(48), **{"posted via": "greenhouse.first_published"}),
            _put(box, "c_old.txt", "Northwind Health AI", jd, posted=ago_now(69 * 24), **{"posted via": "greenhouse.first_published"})]


def make_live(db, settings, fresh, lines):
    return Live(db, settings, Hooks(ask=lambda p: "", tailor=None, review=None, preflight=None, assist=None, mark_applied=None),
                out=lines.append, fresh=fresh)


def test_live_without_fresh_takes_every_file_in_tier_order(db, settings, inbox):
    lines = []
    ordered = make_live(db, settings, False, lines)._by_priority(list(reversed(inbox)))
    assert [f.name for f in ordered] == ["a_new.txt", "b_two_days.txt", "c_old.txt"]            # nothing dropped; the 69-day-old one is last
    assert lines[0] == "Inbox order by freshness tier, then the ESTIMATE of P = F x S x L (before tailoring; the review order uses the final P):"


def test_live_fresh_takes_tier_0_only_and_leaves_the_rest_in_the_inbox(db, settings, inbox):
    lines = []
    ordered = make_live(db, settings, True, lines)._by_priority(list(inbox))
    assert [f.name for f in ordered] == ["a_new.txt"]
    assert any("--fresh: 1 file(s) posted at most 24 h ago (tier 0); 2 older file(s) stay in the inbox." in ln for ln in lines)
    assert all(f.exists() for f in inbox)                                                         # nothing was moved or deleted


def test_live_fresh_with_a_single_file_still_filters(db, settings, inbox):
    ordered = make_live(db, settings, True, [])._by_priority([inbox[2]])
    assert ordered == []


def test_live_fresh_says_so_when_nothing_is_fresh(db, settings, inbox):
    for f in inbox[:2]:
        f.unlink()
    lines = []
    live = make_live(db, settings, True, lines)
    assert live._by_priority([inbox[2]]) == []
    live.state = None
    live._loop()
    assert any("Nothing posted in the last 24 hours" in ln for ln in lines)


def test_the_live_command_has_a_fresh_flag(monkeypatch):
    import argparse
    seen = {}
    monkeypatch.setattr(cli, "cmd_live", lambda args: seen.update(vars(args)))
    cli.main(["live", "--fresh", "--max", "3"])
    assert seen["fresh"] is True and seen["max"] == 3
    cli.main(["live"])
    assert seen["fresh"] is False


# ------------------------------------------------------------------ discover --report
def test_the_report_counts_the_tiers_of_the_inbox_and_of_the_last_run(db, settings, inbox):
    disc.save_run(settings, disc.Run(started="2026-10-15T07:00:00+00:00", tiers=[3, 2, 1, 0]))
    text = disc.format_report(settings)
    assert "Freshness of the ads the last run wrote: tier 0 (<= 24 h): 3 · tier 1 (<= 72 h): 2 · tier 2 (<= 7 d): 1 · tier 3 (older): 0" in text
    waiting = next(ln for ln in text.splitlines() if ln.startswith("Waiting in the inbox"))
    assert "`live --fresh` takes tier 0" in waiting and "tier 0 (<= 24 h): 1" in waiting and "tier 3 (older): 1" in waiting


def test_an_empty_inbox_reports_zero_in_every_tier(settings):
    assert disc.inbox_tiers(settings) == [0, 0, 0, 0]


def test_discover_records_the_tiers_of_what_it_wrote(settings):
    run = disc.Run()
    assert run.tiers == [0, 0, 0, 0] and run.as_json()["tiers"] == [0, 0, 0, 0]
