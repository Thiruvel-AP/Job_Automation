"""Alert emails of the sources in config/sources.yaml that have no dedicated parser (Glassdoor, SEEK, Wellfound ...): the generic parser, the
Gmail query built from the registry, resolution to the employer's own board or the manual queue, and the country of a one-country
source. No network: `Net` answers every request."""
import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from careerops.config import ROOT
from careerops.discovery import discover as disc, gmail, manual, registry
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net, fixture_json, fixture_text

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def make(cfg, db, net):
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(cfg, db, f, today=TODAY, say=lambda s: None)


@pytest.fixture
def cfg(settings):
    shutil.copy(ROOT / "config" / "sources.yaml", Path(settings.raw["paths"]["sources"]))
    settings.raw["sources"].update(greenhouse=[{"slug": "acme", "name": "Acme Analytics Ltd"}], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    settings.raw["discover"]["gmail"]["senders"] = []
    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "at", "expires_at": 9e12, "scope": gmail.SCOPE})
    return settings


def mail_net(mails: dict, extra=None) -> Net:
    """mails: {id: (from, subject, html)}"""
    net = Net(strict=False)
    for mid, (frm, subject, html) in mails.items():
        body = {"data": base64.urlsafe_b64encode(html.encode()).decode()}
        net.add(f"/messages/{mid}", {"id": mid, "payload": {"mimeType": "text/html", "body": body, "headers": [
            {"name": "From", "value": frm}, {"name": "Subject", "value": subject}]}})
    net.add("gmail.googleapis.com/gmail/v1/users/me/messages", {"messages": [{"id": m} for m in mails]})
    for part, reply in reversed(list((extra or {}).items())):
        net.routes.insert(0, (part, reply))
    return net


def acme_routes():
    return {"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")}


def maple_board(location: str) -> dict:
    text = "&lt;p&gt;" + "Maple Analytics builds forecasting tools for retailers and needs a data scientist. " * 12 + "&lt;/p&gt;"
    return {"jobs": [{"id": "7", "title": "Data Scientist", "location": {"name": location}, "updated_at": "2026-10-08T09:00:00-04:00",
                      "absolute_url": "https://job-boards.greenhouse.io/maple/jobs/7", "content": text}]}


# ------------------------------------------------------------------ the generic parser
def test_a_generic_alert_yields_its_job_links_with_company_and_place_and_no_tracking():
    src = registry.Source("glassdoor-uk", "Glassdoor UK", "https://www.glassdoor.co.uk", "GB", "gmail_alerts",
                          domains=("glassdoor.com", "glassdoor.co.uk"))
    items = gmail.parse_alert(src.kind, fixture_text("alert_glassdoor.html"), "", "m1", link_fn=lambda h: gmail.generic_link(src, h))
    assert [(i.title, i.company, i.location) for i in items] == [("Data Scientist", "Acme Analytics Ltd", "London, UK"),
                                                                 ("Senior AI Engineer", "Unknown Startup Ltd", "Leeds")]
    assert items[0].link == ("https://www.glassdoor.co.uk/job-listing/data-scientist-acme-analytics-JV_IC2671300_KO0,14_KE15,31.htm"
                             "?jl=1009")                                           # utm_ and trk parameters dropped, the job id kept
    assert items[0].kind == "glassdoor-uk"


@pytest.mark.parametrize("href, ok", [
    ("https://www.glassdoor.co.uk/job-listing/x-JV_1.htm", True), ("https://www.seek.com.au/job/81234567", True),
    ("https://www.stepstone.de/stellenangebote--Data-Scientist--123.html", True), ("https://www.glassdoor.co.uk/Salary/index.htm", False),
    ("https://evil.example/jobs/view/123456789", False), ("https://www.glassdoor.co.uk/jobs/unsubscribe", False),
    ("https://www.glassdoor.co.uk/member/alerts/", False), ("mailto:jobs@glassdoor.co.uk", False), ("javascript:void(0)", False),
    ("ftp://www.glassdoor.co.uk/jobs/1", False),
])
def test_only_a_job_page_on_the_sources_own_domain_is_a_job_link(href, ok):
    src = registry.Source("s", "S", "https://x.example", "multi", "gmail_alerts", domains=("glassdoor.co.uk", "seek.com.au", "stepstone.de"))
    assert bool(gmail.generic_link(src, href)) is ok


# ------------------------------------------------------------------ the query
def test_the_query_is_built_from_the_registry_and_names_subject_words_for_a_shared_sender(cfg):
    q = gmail.build_query(cfg, registry.load(cfg))
    for d in ("linkedin.com", "glassdoor.co.uk", "seek.com.au", "seek.co.nz", "stepstone.de", "irishjobs.ie", "jobs.nhs.uk", "reed.co.uk"):
        assert f"from:{d}" in q
    assert "subject:(DeepMind)" in q and "newer_than:14d" in q
    assert q.count("(") == q.count(")")


def test_explicit_senders_keep_the_older_fixed_list(cfg):
    cfg.raw["discover"]["gmail"].update(senders=["reed"], query_days=3)
    assert gmail.build_query(cfg, registry.load(cfg)) == "(from:reed.co.uk) newer_than:3d"


def test_without_a_registry_the_four_senders_are_asked_for(cfg):
    q = gmail.build_query(cfg, None)
    assert "from:linkedin.com" in q and "from:glassdoor" not in q


# ------------------------------------------------------------------ through discover
def test_a_glassdoor_alert_is_resolved_on_the_employers_board_or_queued_with_its_link(cfg, db):
    net = mail_net({"g1": ("Glassdoor <noreply@glassdoor.co.uk>", "Data Scientist jobs in London", fixture_text("alert_glassdoor.html"))},
                   acme_routes())
    d = make(cfg, db, net)
    d.from_gmail()
    assert [(c.company, c.title, c.origin) for c, _ in d.run.written] == [("Acme Analytics Ltd", "Data Scientist", "gmail:glassdoor-uk")]
    (row,) = disc.read_manual_queue(cfg.path("jds") / "manual_queue.csv")
    assert (row["source"], row["title"]) == ("glassdoor-uk", "Senior AI Engineer")
    assert row["link"].startswith("https://www.glassdoor.co.uk/job-listing/senior-ai-engineer") and "utm_" not in row["link"]
    assert d.run.sources["gmail"]["alerts_glassdoor-uk"] == 2
    assert dict(d.run.by_source["glassdoor-uk"]) == {"alerts": 2, "new": 1}              # the health table's "new jobs" column
    assert not [u for u in net.urls(True) if "glassdoor" in u]                           # an alert's own site is never fetched


def test_an_email_from_a_sender_that_is_in_no_source_is_ignored_and_remembered_as_read(cfg, db):
    net = mail_net({"m": ("Mum <mum@gmail.com>", "Data Scientist jobs", fixture_text("alert_glassdoor.html"))})
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.written == [] and d.run.sources["gmail"]["alerts"] == 0
    assert json.loads((cfg.path("discover") / "gmail_seen.json").read_text(encoding="utf-8")) == ["m"]


def test_an_alert_with_no_job_link_it_recognises_goes_to_the_manual_queue_as_a_link_to_the_email(cfg, db):
    net = mail_net({"w1": ("Wellfound <jobs@wellfound.com>", "Your weekly job matches", fixture_text("alert_nolinks.html")),
                    "w2": ("Wellfound <hello@wellfound.com>", "Welcome to Wellfound", fixture_text("alert_nolinks.html"))})
    d = make(cfg, db, net)
    d.from_gmail()
    (row,) = disc.read_manual_queue(cfg.path("jds") / "manual_queue.csv")                # the welcome mail is not an alert: not queued
    assert (row["source"], row["link"]) == ("wellfound", gmail.web_link("w1"))
    assert "no job links were recognised" in row["reason"] and d.run.sources["gmail"]["alerts_unparsed"] == 1


def test_an_alert_from_a_one_country_source_gives_the_job_that_country_unless_its_place_says_another(cfg, db):
    net = mail_net({"s1": ("SEEK <jobmail@seek.com.au>", "New jobs for Data Scientist", fixture_text("alert_seek.html"))},
                   {"boards-api.greenhouse.io/v1/boards/maple/jobs": maple_board("Remote"),
                    "boards-api.greenhouse.io/v1/boards/maple": {"name": "Maple Analytics", "content": ""}})
    cfg.raw["sources"]["greenhouse"].append({"slug": "maple", "name": "Maple Analytics"})
    d = make(cfg, db, net)
    d.from_gmail()
    ((c, path),) = d.run.written
    assert (c.company, c.country, c.listed_only, c.origin) == ("Maple Analytics", "Australia", True, "gmail:seek-au")
    head = manual.header_fields(path.read_text(encoding="utf-8"))
    assert (head["country"], head["status"]) == ("Australia", "listed_only")
    assert d.run.by_source["seek-au"]["listed_only"] == 1 and d.run.new_jobs("seek-au") == 1


def test_a_posting_whose_place_says_another_country_keeps_that_one(cfg, db):
    net = mail_net({"s1": ("SEEK <jobmail@seek.com.au>", "New jobs", fixture_text("alert_seek.html"))},
                   {"boards-api.greenhouse.io/v1/boards/maple/jobs": maple_board("Auckland"),
                    "boards-api.greenhouse.io/v1/boards/maple": {"name": "Maple Analytics", "content": ""}})
    cfg.raw["sources"]["greenhouse"].append({"slug": "maple", "name": "Maple Analytics"})
    d = make(cfg, db, net)
    d.from_gmail()
    assert [c.country for c, _ in d.run.written] == ["New Zealand"]


def test_a_uk_source_does_not_make_a_bare_remote_ad_uk(cfg, db):
    html = fixture_text("alert_seek.html").replace("seek.com.au", "glassdoor.co.uk").replace("/job/81234567", "/job-listing/81234567")
    net = mail_net({"g": ("Glassdoor <noreply@glassdoor.co.uk>", "New jobs", html)},
                   {"boards-api.greenhouse.io/v1/boards/maple/jobs": maple_board("Remote"),
                    "boards-api.greenhouse.io/v1/boards/maple": {"name": "Maple Analytics", "content": ""}})
    cfg.raw["sources"]["greenhouse"].append({"slug": "maple", "name": "Maple Analytics"})
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.written == [] and d.run.sources["gmail"]["place_filtered"] == 1          # the existing rule: "Remote" is UK only if the ad says so


def test_the_deepmind_alert_needs_deepmind_in_the_subject(cfg, db):
    net = mail_net({"d1": ("Greenhouse <no-reply@greenhouse.io>", "Weekly digest", fixture_text("alert_glassdoor.html").replace("glassdoor.co.uk", "greenhouse.io")),
                    "d2": ("Greenhouse <no-reply@greenhouse.io>", "DeepMind: new jobs for you", fixture_text("alert_nolinks.html"))})
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.sources["gmail"]["alerts_unparsed"] == 1                                # only d2 was DeepMind's (and had no link)
    (row,) = disc.read_manual_queue(cfg.path("jds") / "manual_queue.csv")
    assert row["source"] == "google-deepmind" and row["link"] == gmail.web_link("d2")


def test_the_dedicated_parsers_and_their_counters_are_unchanged_by_the_registry(cfg, db):
    net = mail_net({"m1": ("LinkedIn <jobalerts-noreply@linkedin.com>", "x", fixture_text("alert_linkedin.html")),
                    "m2": ("Reed <jobalerts@reed.co.uk>", "x", fixture_text("alert_reed.html"))}, acme_routes())
    d = make(cfg, db, net)
    d.from_gmail()
    c = d.run.sources["gmail"]
    assert c["alerts_linkedin"] == 3 and c["alerts_reed"] == 2
    assert d.run.by_source["linkedin"]["alerts"] == 3 and d.run.by_source["reed"]["alerts"] == 2
