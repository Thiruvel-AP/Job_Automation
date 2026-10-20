"""Honest alert health (discovery/health.py): a gmail_alerts source shows `alerts` (mails), `jobs parsed` and `new`; its status is `ok` only when
jobs were parsed from its mails, otherwise `parsed_none` with the first subject line in the detail column. A mail that is not a job alert
(an account mail, a welcome) must not make a source look healthy. No network: `Net` answers."""
import base64
import csv
from datetime import datetime, timedelta, timezone

import pytest

from careerops.discovery import discover as disc, gmail, health, registry
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
WTTJ = "Welcome to the Jungle <welcome@welcometothejungle.com>"
LINKEDIN = "LinkedIn <jobalerts-noreply@linkedin.com>"
ENTRIES = [
    {"id": "linkedin", "name": "LinkedIn", "url": "https://www.linkedin.com/jobs", "country": "multi", "method": "gmail_alerts",
     "domains": ["linkedin.com"], "parser": "linkedin"},
    {"id": "wttj", "name": "Welcome to the Jungle", "url": "https://www.welcometothejungle.com", "country": "multi", "method": "gmail_alerts",
     "domains": ["welcometothejungle.com"]}]
ACCOUNT_MAIL = "<html><body><p>Welcome to the Jungle! Confirm your email address to finish creating your account.</p></body></html>"
JOB_ALERT = ('<html><body><table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/3900000001?trackingId=x">Data Scientist</a>'
             "<p>Maple Analytics Ltd · London, England, United Kingdom</p></td></tr></table></body></html>")


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "at", "expires_at": 9e12, "scope": gmail.SCOPE})
    return settings


def mails(**spec) -> Net:
    """spec: id=(from, subject, html)."""
    net = Net(strict=False)
    for mid, (frm, subject, html) in spec.items():
        body = {"data": base64.urlsafe_b64encode(html.encode()).decode()}
        net.add(f"/messages/{mid}", {"id": mid, "payload": {"mimeType": "text/html", "body": body, "headers": [
            {"name": "From", "value": frm}, {"name": "Subject", "value": subject}]}})
    net.add("gmail.googleapis.com/gmail/v1/users/me/messages", {"messages": [{"id": m} for m in spec]})
    return net


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def run_then_check(cfg, db, net, **kw):
    d = Discover(cfg, db, fetcher(net), today=NOW, say=lambda s: None)
    d.registry = registry.parse({"sources": ENTRIES})
    d.from_gmail()
    rows = health.check_all(cfg, d.registry, fetcher(net), run=d.run, cache=d.cache, now=NOW, **kw)
    return d, {r.source.id: r for r in rows}


def test_a_non_job_mail_gives_parsed_none_with_its_subject(cfg, db):
    net = mails(m1=(WTTJ, "Welcome to Welcome to the Jungle!", ACCOUNT_MAIL), m2=(WTTJ, "Confirm your email", ACCOUNT_MAIL))
    d, rows = run_then_check(cfg, db, net)
    row = rows["wttj"]
    assert row.status == "parsed_none" and row.parsed == 0 and row.alerts == 2
    assert 'first subject: "Welcome to Welcome to the Jungle!"' in row.detail
    assert row.record("ts")["status"] == "parsed_none" and row.record("ts")["jobs_parsed"] == 0


def test_a_source_that_parsed_jobs_is_ok_and_shows_the_three_numbers(cfg, db):
    net = mails(m1=(LINKEDIN, "1 new job for you", JOB_ALERT), m2=(WTTJ, "Welcome", ACCOUNT_MAIL))
    d, rows = run_then_check(cfg, db, net)
    row = rows["linkedin"]
    assert row.status == "ok" and row.parsed == 1 and row.alerts is not None
    table = health.format_table(list(rows.values()))
    head = table.splitlines()[0]
    for col in ("source", "method", "status", "alerts", "jobs parsed", "new jobs"):
        assert col in head
    line = next(l for l in table.splitlines() if l.startswith("LinkedIn"))
    assert " ok " in line and line.split()[-3:] == [str(row.alerts), "1", str(row.new_jobs)]


def test_a_quiet_source_stays_quiet_and_is_not_parsed_none(cfg, db):
    net = Net(strict=False).add("gmail.googleapis.com/gmail/v1/users/me/messages", {})
    d, rows = run_then_check(cfg, db, net)
    assert rows["wttj"].status == "quiet" and rows["wttj"].alerts == 0


def test_the_jobs_parsed_are_counted_over_the_last_alert_days_from_the_saved_runs(cfg, db):
    net = mails(m1=(LINKEDIN, "1 new job for you", JOB_ALERT))
    d = Discover(cfg, db, fetcher(net), today=NOW - timedelta(days=3), say=lambda s: None)
    d.registry = registry.parse({"sources": ENTRIES})
    d.from_gmail()
    disc.save_run(cfg, d.run)                                              # a run three days ago parsed one job
    later = mails(m1=(LINKEDIN, "1 new job for you", JOB_ALERT))           # today: the same mail is still the latest one in the mailbox
    rows = {r.source.id: r for r in health.check_all(cfg, d.registry, fetcher(later), now=NOW)}
    assert rows["linkedin"].parsed == 1 and rows["linkedin"].status == "ok"
    old = {r.source.id: r for r in health.check_all(cfg, d.registry, fetcher(later), now=NOW + timedelta(days=9))}
    assert old["linkedin"].parsed == 0 and old["linkedin"].status == "parsed_none"       # the run is older than discover.health.alert_days


def test_with_no_run_on_record_the_status_says_to_run_discover(cfg, db):
    net = mails(m1=(WTTJ, "Welcome", ACCOUNT_MAIL))
    rows = {r.source.id: r for r in health.check_all(cfg, registry.parse({"sources": ENTRIES}), fetcher(net), now=NOW)}
    assert rows["wttj"].status == "parsed_none" and "discover --gmail-alerts" in rows["wttj"].detail


def test_the_saved_csv_has_the_jobs_parsed_column(cfg, db):
    net = mails(m1=(WTTJ, "Welcome", ACCOUNT_MAIL))
    d, rows = run_then_check(cfg, db, net)
    path = health.save(cfg, list(rows.values()), NOW)
    recs = list(csv.DictReader(path.open(encoding="utf-8")))
    assert "jobs_parsed" in recs[0] and {r["source"]: r["status"] for r in recs}["wttj"] == "parsed_none"


def test_an_old_csv_of_the_same_day_gets_the_new_column_not_a_broken_row(cfg, db):
    path = health.csv_path(cfg, NOW)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_cols = [c for c in health.COLUMNS if c != "jobs_parsed"]
    path.write_text(",".join(old_cols) + "\n" + ",".join(["2026-10-20T08:00:00+00:00", "x", "X", "greenhouse", "GB", "ok", "0", "yes", "allowed",
                                                          "n/a", "", "6", ""]) + "\n", encoding="utf-8")
    net = mails(m1=(WTTJ, "Welcome", ACCOUNT_MAIL))
    d, rows = run_then_check(cfg, db, net)
    health.save(cfg, list(rows.values()), NOW)
    recs = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["source"] for r in recs] == ["x", "linkedin", "wttj"] and recs[0]["jobs_parsed"] == "" and recs[2]["jobs_parsed"] == "0"


def test_a_run_counts_mails_parsed_jobs_and_keeps_the_first_subject_of_a_mail_with_none(cfg, db):
    net = mails(m1=(WTTJ, "Welcome to Welcome to the Jungle!", ACCOUNT_MAIL), m2=(WTTJ, "Second", ACCOUNT_MAIL), m3=(LINKEDIN, "x", JOB_ALERT))
    d, _ = run_then_check(cfg, db, net)
    assert d.run.by_source["wttj"]["mails"] == 2 and not d.run.by_source["wttj"].get("parsed")
    assert d.run.by_source["linkedin"]["parsed"] == 1 and d.run.no_job_subjects == {"wttj": "Welcome to Welcome to the Jungle!"}
    assert d.run.as_json()["no_job_subjects"] == {"wttj": "Welcome to Welcome to the Jungle!"}
