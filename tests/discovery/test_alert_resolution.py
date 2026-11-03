"""Resolving an alert to the employer's page (discover.py resolve_link, applykind.py): the board lookup at title match 0.9 with a place that
agrees, every step counted, apply_url / apply_kind on what is found, nothing ever requested from linkedin.com, indeed, cv-library or a Workday
host, and an unresolved job kept in the manual queue with its apply_kind. No network: `Net` answers."""
import csv
import json
import shutil
from pathlib import Path

import pytest

from careerops.config import ROOT
from careerops.discovery import applykind, discover as disc, intake, manual
from careerops.discovery.discover import Discover
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, Resp, fixture_json
from test_discover import ACME, TODAY, cfg, gh_net, gmail_net, make, read_inbox, signed_in, titles    # noqa: F401  (fixtures)
from test_quality_round import gh as gh_routes

BANNED = ("linkedin.com", "indeed.com", "indeed.co.uk", "cv-library.co.uk", "myworkdayjobs.com", "myworkday.com")


def rows(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8", newline=""))) if path.exists() else []


def queue(cfg) -> list[dict]:
    return rows(cfg.path("jds") / "manual_queue.csv")


@pytest.fixture(autouse=True)
def no_intake_network(monkeypatch):
    """intake.py has its own fetcher (live --url, --clipboard): resolving an alert must never reach it."""
    def boom(url, **kw):
        raise AssertionError(f"resolving an alert fetched {url} through intake")
    monkeypatch.setattr(intake, "_get", boom)


# ------------------------------------------------------------------ apply_kind
@pytest.mark.parametrize("url, kind", [
    ("https://job-boards.greenhouse.io/monzo/jobs/123", "greenhouse"),
    ("https://boards.greenhouse.io/embed/job_app?for=x&token=1", "greenhouse"),
    ("https://boards.eu.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/valarian/abc", "lever"),
    ("https://jobs.eu.lever.co/x/y", "lever"),
    ("https://jobs.ashbyhq.com/moneybox/a59a8872", "ashby"),
    ("https://acme.wd3.myworkdayjobs.com/en-US/careers/job/London/Data-Scientist_R1", "workday"),
    ("https://wd5.myworkday.com/blackrock/d/inst/1", "workday"),
    ("https://careers.example.com/jobs/42", "employer_other"),
    ("https://www.linkedin.com/jobs/view/4479026825", "aggregator_only"),
    ("https://uk.indeed.com/viewjob?jk=abc", "aggregator_only"),
    ("https://www.cv-library.co.uk/job/219034011/data-scientist", "aggregator_only"),
    ("https://www.reed.co.uk/jobs/data-scientist/5001", "aggregator_only"),
    ("", "")])
def test_the_kind_is_read_from_the_host(url, kind):
    assert applykind.kind_of(url) == kind


def test_workday_means_a_host_ending_in_myworkdayjobs_or_myworkday_and_not_a_look_alike():
    assert applykind.kind_of("https://notmyworkday.com/x") == "employer_other"
    assert applykind.kind_of("https://myworkdayjobs.com.evil.example/x") == "employer_other"
    assert applykind.kind_of("https://x.myworkdayjobs.com/") == "workday"


def test_the_apply_line():
    assert applykind.line("https://jobs.lever.co/a/b") == "Apply: lever https://jobs.lever.co/a/b"
    assert applykind.line("https://x.example/1", "greenhouse") == "Apply: greenhouse https://x.example/1"      # a board's kind wins over its host
    assert "none" in applykind.line("")


# ------------------------------------------------------------------ the steps, counted
def test_each_resolution_step_is_counted_and_credited_to_its_source(signed_in, db, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "k")
    net = gmail_net({"api/1.0/jobs/5001": fixture_json("reed_job_5001.json"), "api/1.0/jobs/5555": {"jobId": 0},
                     "boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = make(signed_in, db, net)
    d.from_gmail()
    c = d.run.sources["gmail"]
    assert c["resolved_via_board_known"] == 1 and c["resolved_via_reed"] == 1 and not c.get("resolved_via_board_guessed")
    assert c["resolved"] == c["resolved_via_board_known"] + c["resolved_via_reed"] + c.get("resolved_via_board_guessed", 0)
    assert d.run.by_source["linkedin"]["via_board_known"] == 1 and d.run.by_source["reed"]["via_reed"] == 1


def test_a_board_found_from_the_alerts_company_name_is_the_guessed_step(signed_in, db):
    signed_in.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    signed_in.raw["discover"]["alert_probes_per_run"] = 10
    net = gmail_net({**gh_routes("acmeanalytics", "Acme Analytics Ltd", ("101", "Data Scientist", "London, UK"))})
    d = make(signed_in, db, net)
    d.from_gmail()
    c = d.run.sources["gmail"]
    assert c["resolved_via_board_guessed"] == 1 and not c.get("resolved_via_board_known")
    assert d.run.by_source["linkedin"]["via_board_guessed"] == 1


def test_the_report_prints_the_steps_and_the_rates(signed_in, db, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    net = gmail_net({"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = make(signed_in, db, net)
    d.from_gmail()
    disc.save_run(signed_in, d.run)
    text = disc.format_report(signed_in)
    assert "Alerts resolved by step" in text and "board_known 1" in text
    assert "of " in text and "linkedin" in text                                      # the per-source rate lines stay


# ------------------------------------------------------------------ title 0.9 and a place that agrees
def maple(cfg, *postings):
    cfg.raw["sources"].update(greenhouse=[{"slug": "maple", "name": "Maple Analytics Ltd"}], lever=[], lever_eu=[], ashby=[])
    net = Net(strict=False)
    for part, reply in gh_routes("maple", "Maple Analytics Ltd", *postings).items():          # the jobs route first: the first part found in a URL wins
        net.add(part, reply)
    return net


def resolve(cfg, db, net, title, place, company="Maple Analytics Ltd", **kw):
    d = make(cfg, db, net, **kw)
    out = d.resolve_link("gmail", "linkedin", company, title, place, "https://www.linkedin.com/jobs/view/1", "gmail:linkedin",
                         source_id="linkedin")
    d.finish()                                                                  # the queue is written at the end of a step
    return d, out


def test_the_title_must_be_nine_tenths_alike_not_eight(cfg, db):
    net = maple(cfg, ("1", "Data Science Engineer", "London, UK"))
    d, out = resolve(cfg, db, net, "Data Scientist Engineer", "London")          # 0.86 alike
    assert out == "queued" and "no posting titled like this" in queue(cfg)[0]["reason"]
    cfg.raw["discover"]["title_match"] = 0.85                                     # the setting is honoured
    (cfg.path("jds") / "manual_queue.csv").unlink()
    d, out = resolve(cfg, db, maple(cfg, ("1", "Data Science Engineer", "London, UK")), "Data Scientist Engineer", "London")
    assert out == "written"


def test_the_shipped_title_match_is_nine_tenths(settings):
    assert settings["discover"]["title_match"] == 0.9


def test_a_posting_in_another_place_is_not_the_alerts_job(cfg, db):
    net = maple(cfg, ("1", "AI Engineer", "Leeds"))
    d, out = resolve(cfg, db, net, "AI Engineer", "Bristol, England")
    assert out == "queued" and d.run.written == []


def test_a_place_that_is_inside_a_multi_place_posting_agrees(cfg, db):
    net = maple(cfg, ("1", "AI Engineer", "London, UK or Manchester, UK"))
    d, out = resolve(cfg, db, net, "AI Engineer", "Manchester")
    assert out == "written" and d.run.sources["gmail"]["resolved_via_board_known"] == 1


@pytest.mark.parametrize("alert_place", ["", "United Kingdom", "Remote (UK)"])
def test_an_alert_with_no_town_agrees_with_any_posting_place(cfg, db, alert_place):
    d, out = resolve(cfg, db, maple(cfg, ("1", "AI Engineer", "Leeds")), "AI Engineer", alert_place)
    assert out == "written"


# ------------------------------------------------------------------ what is found carries its apply page
def test_a_resolved_ad_is_written_with_its_apply_kind_and_the_employers_url(cfg, db):
    d, out = resolve(cfg, db, maple(cfg, ("7", "AI Engineer", "London")), "AI Engineer", "London")
    (c, path), = d.run.written
    assert (c.apply_kind, c.apply_url) == ("greenhouse", c.url) and c.url.startswith("https://job-boards.greenhouse.io/")
    text = (cfg.path("jds") / "inbox" / path.name).read_text(encoding="utf-8")
    assert "Apply kind: greenhouse" in text and "URL: https://job-boards.greenhouse.io/x/jobs/7" in text
    fields = manual.header_fields(text)
    assert fields["apply_kind"] == "greenhouse" and fields["listed"] == "yes" and fields["first_seen"]          # headers past line ten are read


def test_the_ats_boards_discover_reads_carry_their_kind_too(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert d.run.written and all(c.apply_kind == "greenhouse" for c, _ in d.run.written)


def test_add_jd_keeps_the_header_kind_or_reads_it_from_the_url(db, jd):
    a = manual.add_jd(db, jd, company="Alpha", title="Analyst", url="https://jobs.lever.co/a/1")
    b = manual.add_jd(db, jd, company="Beta", title="Analyst", url="https://x.example/1", apply_kind="ashby", apply_url="https://jobs.ashbyhq.com/b/1")
    c = manual.add_jd(db, jd, company="Gamma", title="Analyst", url="https://x.example/2", apply_kind="not-a-kind")
    assert [db.job(j)["apply_kind"] for j in (a, b, c)] == ["lever", "ashby", "employer_other"]
    assert db.job(b)["apply_url"] == "https://jobs.ashbyhq.com/b/1" and db.job(a)["apply_url"] == "https://jobs.lever.co/a/1"


# ------------------------------------------------------------------ the unresolved are kept, with their kind
def test_an_unresolved_alert_is_kept_in_the_manual_queue_with_its_apply_kind(signed_in, db, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    net = gmail_net({"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = make(signed_in, db, net)
    d.from_gmail()
    q = queue(signed_in)
    assert q and list(q[0])[:8] == ["ts", "source", "company", "title", "location", "link", "reason", "apply_kind"]
    assert all(r["apply_kind"] == "aggregator_only" for r in q)               # a LinkedIn / Indeed / CV-Library / Reed link, nothing better
    assert {r["source"] for r in q} >= {"linkedin", "indeed", "cvlibrary"}


def workday_only(d, posts=()):
    """The CLI's fetcher rules for Workday (its pages are never asked, the public /wday/cxs/ list API is), and a `post` that answers a Workday list (nothing real is sent)."""
    asked = []

    def post(url, json=None, **kw):
        asked.append(url)
        return Resp({"jobPostings": list(posts), "total": len(posts)})
    d.f.never, d.f.allow_paths, d.f._post = applykind.NEVER_FETCH, ("/wday/cxs/",), post
    return asked


def test_a_workday_link_not_on_its_board_is_queued_as_workday_and_only_the_public_list_api_is_asked(cfg, db):
    net = Net()                                                               # strict: any request but robots.txt fails the test
    d = make(cfg, db, net)
    asked = workday_only(d)
    out = d.resolve_link("gmail", "indeed", "Zeta Holdings", "Data Scientist", "London",
                         "https://zeta.wd3.myworkdayjobs.com/en-US/careers/job/London/Data-Scientist_R1", "gmail:indeed", source_id="indeed-uk")
    d.finish()
    assert out == "queued" and asked and all("/wday/cxs/" in u for u in asked)
    assert all(u.endswith("/robots.txt") for u in net.urls(True))             # no careers PAGE was fetched
    (row,) = queue(cfg)
    assert row["apply_kind"] == "workday" and "myworkdayjobs.com" in row["link"] and "Workday" in row["reason"]


def test_the_queue_header_is_extended_in_place_and_old_rows_are_kept(cfg, db):
    path = cfg.path("jds") / "manual_queue.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ts,source,company,title,location,link,reason\n2026-10-15T13:38:23+00:00,linkedin,iwoca,Data Scientist,London,"
                    "https://www.linkedin.com/jobs/view/1,no public job board known for iwoca\n", encoding="utf-8")
    d = make(cfg, db, Net(strict=False))
    d.to_queue("gmail", "indeed", "Sky", "Data Scientist", "London", "https://uk.indeed.com/viewjob?jk=1", "no board", apply_kind="aggregator_only")
    d.finish()
    got = rows(path)
    assert [(r["company"], r["apply_kind"]) for r in got] == [("iwoca", ""), ("Sky", "aggregator_only")]
    assert path.read_text(encoding="utf-8").count("ts,source,company") == 1


def test_a_resolved_job_the_run_has_no_room_for_goes_to_the_queue_not_to_the_floor(cfg, db):
    d, out = resolve(cfg, db, maple(cfg, ("7", "AI Engineer", "London")), "AI Engineer", "London", limit=0)
    d.finish()
    assert out == "queued" and d.run.written == []
    (row,) = queue(cfg)
    assert row["apply_kind"] == "greenhouse" and row["link"].startswith("https://job-boards.greenhouse.io/") and "discover.max_new_per_run" in row["reason"]
    assert d.run.sources["gmail"]["resolved"] == 1 and d.run.sources["gmail"]["to_manual_queue"] == 1


def test_alerts_are_read_before_the_boards_so_the_fresh_ones_get_the_room(monkeypatch, cfg, capsys):
    from careerops import cli
    order = []
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    for name in ("from_boards", "from_gmail"):
        monkeypatch.setattr(Discover, name, lambda self, n=name: order.append(n))
    monkeypatch.setattr(Discover, "ats_report", lambda self: [])
    from careerops.discovery import http
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp("", 404))
    cli.main(["discover", "--ats", "--gmail-alerts", "--dry-run"])
    assert order == ["from_gmail", "from_boards"]


# ------------------------------------------------------------------ nothing is requested from the four hosts
def test_no_request_goes_to_linkedin_indeed_cv_library_or_workday(signed_in, db, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "k")
    net = gmail_net({"api/1.0/jobs/5001": fixture_json("reed_job_5001.json"), "api/1.0/jobs/5555": {"jobId": 0},
                     "boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = make(signed_in, db, net)
    posted = workday_only(d)
    d.from_gmail()
    d.resolve_link("gmail", "indeed", "Acme Corp", "Data Scientist", "London", "https://acme.wd3.myworkdayjobs.com/x/job/1", "gmail:indeed")
    asked = net.urls(True)
    assert asked and not [u for u in asked if any(b in u for b in BANNED[:4])]       # linkedin, indeed, cv-library: never
    assert all(u.endswith("/robots.txt") for u in asked if "myworkday" in u) and all("/wday/cxs/" in u for u in posted)   # Workday: robots.txt and its public list API only
    assert net.hosts() <= {"gmail.googleapis.com", "boards-api.greenhouse.io", "www.reed.co.uk", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com", "acme.wd3.myworkdayjobs.com"}


def test_the_cv_library_page_of_an_alert_is_not_fetched_and_its_job_is_queued(signed_in, db, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    d = make(signed_in, db, gmail_net())
    d.from_gmail()
    cv = [r for r in queue(signed_in) if r["source"] == "cvlibrary"]
    assert cv and all(r["link"].startswith("https://www.cv-library.co.uk/") and r["apply_kind"] == "aggregator_only" for r in cv)
    assert not [w for c, w in d.run.written if c.source == "cvlibrary"]


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://uk.indeed.com/viewjob?jk=1",
                                 "https://www.cv-library.co.uk/job/1/x", "https://acme.wd3.myworkdayjobs.com/en-US/c/job/1",
                                 "https://wd5.myworkday.com/x"])
def test_a_fetcher_told_to_refuse_these_hosts_sends_nothing(url):
    net = Net()
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, never=applykind.NEVER_FETCH)
    with pytest.raises(Blocked, match="never"):
        f.request(url)
    assert net.requests == []
