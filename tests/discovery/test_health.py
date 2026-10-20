"""The health check of the sources (discovery/health.py) and the commands that print it: `careerops sources check|list` and the table at
the end of every `careerops discover` run. No network: `Net` answers every request."""
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from careerops import cli
from careerops.config import ROOT
from careerops.discovery import discover as disc, gmail, health, registry
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net, Resp, fixture_json

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def src(**kw) -> dict:
    return {"id": "x", "name": "X", "url": "https://x.example", "country": "GB", "method": "greenhouse", **kw}


def check(cfg, entries, net, **kw):
    reg = registry.parse({"sources": entries})
    f = fetcher(net)
    return health.check_all(cfg, reg, f, cache=disc.BoardCache(cfg, f), **kw)


def one(cfg, entry, net, **kw):
    (row,) = check(cfg, [entry], net, **kw)
    return row


@pytest.fixture
def signed_in(settings):
    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "at", "expires_at": 9e12, "scope": gmail.SCOPE})
    return settings


def acme_net() -> Net:
    return Net(strict=False).add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))


# ------------------------------------------------------------------ job boards
def test_a_board_that_answers_is_ok_with_its_open_postings_and_robots_allowed(settings):
    row = one(settings, src(token="acme"), acme_net())
    assert (row.status, row.reachable, row.robots, row.postings) == ("ok", "yes", "allowed", 6)


def test_an_unknown_board_is_not_found_not_an_error(settings):
    row = one(settings, src(token="nosuchboard"), Net(strict=False))
    assert row.status == "not_found" and row.reachable == "yes" and row.postings == 0 and "nosuchboard" in row.detail


def test_robots_that_disallow_the_api_block_the_source_and_nothing_is_fetched(settings):
    net = Net(strict=False).add("boards-api.greenhouse.io/robots.txt", "User-agent: *\nDisallow: /\n")
    row = one(settings, src(token="acme"), net)
    assert (row.status, row.robots, row.reachable) == ("blocked", "disallowed", "no") and "disallows" in row.detail
    assert not [u for u in net.urls() if "/v1/boards/" in u]


def test_a_403_blocks_that_source_and_the_others_are_still_checked(settings):
    net = Net(strict=False).add("api.ashbyhq.com", Resp("", 403)).add("boards/acme/jobs", fixture_json("gh_jobs_acme.json"))
    rows = check(settings, [src(id="a", name="A", method="ashby", token="locked"), src(id="b", name="B", token="acme")], net)
    assert [(r.source.id, r.status) for r in rows] == [("a", "blocked"), ("b", "ok")]
    assert "403" in rows[0].detail


def test_a_server_error_is_unreachable(settings):
    row = one(settings, src(token="acme"), Net(strict=False).add("boards/acme", Resp("", 503)))
    assert row.status == "unreachable" and row.reachable == "no" and "503" in row.detail


def test_a_connection_failure_is_unreachable(settings):
    import requests
    row = one(settings, src(token="acme"), Net(strict=False).add("boards/acme", lambda url, **kw: requests.ConnectionError("down")))
    assert row.status == "unreachable"


def test_a_source_without_a_token_checks_the_api_host_and_reports_the_run_postings(settings):
    run = disc.Run()
    run.count("ats", "seen", 7)
    row = one(settings, src(), Net(strict=False), run=run)
    assert (row.status, row.reachable, row.postings) == ("ok", "yes", 7)
    assert one(settings, src(), Net(strict=False)).postings is None                # no discover run: nothing to count


def test_a_registry_board_is_not_fetched_twice_when_discover_just_read_it(settings, db):
    net = acme_net()
    f = fetcher(net)
    cache = disc.BoardCache(settings, f)
    reg = registry.parse({"sources": [src(token="acme", name="Acme Analytics Ltd")]})
    cache.postings(reg.boards()[0])
    n = len(net.urls())
    health.check_all(settings, reg, f, cache=cache)
    assert len(net.urls()) == n


# ------------------------------------------------------------------ Reed
def test_reed_without_a_key_is_no_credentials_and_nothing_is_sent(settings, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    net = Net()
    row = one(settings, src(id="reed", method="reed"), net)
    assert (row.status, row.credentials) == ("no_credentials", "missing") and net.requests == []


def test_reed_with_a_key_makes_one_tiny_search_and_reports_the_total(settings, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "k")
    net = Net(strict=False).add("reed.co.uk/api/1.0/search", {"results": [{"jobId": 1}], "totalResults": 321})
    row = one(settings, src(id="reed", method="reed"), net)
    assert (row.status, row.credentials, row.postings, row.robots) == ("ok", "present", 321, "n/a")
    (req,) = [r for r in net.requests if "/search" in r["url"]]
    assert req["params"]["resultsToTake"] == 1 and req["auth"] == ("k", "")


def test_reed_refusing_the_key_is_blocked(settings, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "bad")
    row = one(settings, src(id="reed", method="reed"), Net(strict=False).add("reed.co.uk/api", Resp("", 401)))
    assert row.status == "blocked" and "401" in row.detail


# ------------------------------------------------------------------ Gmail alerts
GM = src(id="glassdoor-uk", name="Glassdoor UK", method="gmail_alerts", domains=["glassdoor.co.uk"])


def test_gmail_alerts_without_a_sign_in_say_so_and_ask_gmail_nothing(settings):
    net = Net()
    row = one(settings, GM, net)
    assert (row.status, row.credentials) == ("no_credentials", "missing") and "--gmail-auth" in row.detail and net.requests == []


def test_alerts_seen_in_the_last_seven_days_are_counted_with_a_read_only_list_call(signed_in):
    net = Net(strict=False).add("users/me/messages", {"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    row = one(signed_in, GM, net)
    assert (row.status, row.alerts, row.credentials, row.reachable) == ("parsed_none", 3, "present", "yes")       # mails, but no job read from them (tests/discovery/test_alert_health.py)
    (req,) = [r for r in net.requests if "gmail" in r["url"]]
    assert req["params"]["q"] == "from:glassdoor.co.uk newer_than:7d" and req["headers"]["Authorization"] == "Bearer at"
    assert req["url"].endswith("/users/me/messages")                               # a list, never a read of a message or any write


def test_no_alerts_in_a_week_is_quiet_and_says_the_domain_may_be_wrong(signed_in):
    row = one(signed_in, GM, Net(strict=False).add("users/me/messages", {"resultSizeEstimate": 0}))
    assert (row.status, row.alerts) == ("quiet", 0) and "glassdoor.co.uk" in row.detail and "sources.yaml" in row.detail


def test_the_window_is_a_setting(signed_in):
    signed_in.raw["discover"].setdefault("health", {})["alert_days"] = 3
    net = Net(strict=False).add("users/me/messages", {"messages": [{"id": "a"}]})
    one(signed_in, GM, net)
    assert [r["params"]["q"] for r in net.requests if "gmail" in r["url"]] == ["from:glassdoor.co.uk newer_than:3d"]


def test_a_shared_sender_is_asked_for_with_its_subject_words(signed_in):
    net = Net(strict=False).add("users/me/messages", {"messages": []})
    one(signed_in, src(id="dm", method="gmail_alerts", domains=["greenhouse.io"], subject_contains=["DeepMind"]), net)
    assert [r["params"]["q"] for r in net.requests if "gmail" in r["url"]] == ["((from:greenhouse.io) subject:(DeepMind)) newer_than:7d"]


def test_the_gmail_api_failing_is_that_sources_row_only(signed_in):
    net = Net(strict=False).add("users/me/messages", Resp("", 500))
    rows = check(signed_in, [GM, src(id="b", name="B", token="acme")], net.add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")))
    assert [(r.source.id, r.status) for r in rows] == [("glassdoor-uk", "unreachable"), ("b", "ok")]


def test_a_signed_in_check_asks_for_the_token_once_for_all_sources(signed_in):
    net = Net(strict=False).add("users/me/messages", {"messages": []})
    entries = [{**GM, "id": f"s{i}", "name": f"S{i}", "domains": [f"s{i}.example"]} for i in range(5)]
    check(signed_in, entries, net)
    assert len([r for r in net.requests if "users/me/messages" in r["url"]]) == 5 and not [r for r in net.requests if "oauth2" in r["url"]]


# ------------------------------------------------------------------ Job Bank, AIApply, disabled, new jobs
JB = src(id="jobbank-ca", name="Job Bank Canada", country="CA", method="jobbank_csv", dataset="ea639e28-c0fc-48bf-b5dd-b8899bd43072")


def ckan(*names):
    return {"success": True, "result": {"resources": [{"name": n, "format": "CSV", "language": ["en"], "created": c,
                                                       "url": f"https://open.canada.ca/data/dataset/x/resource/r/download/{n}.csv"}
                                                      for n, c in names]}}


def test_job_bank_is_checked_through_its_catalogue_and_the_big_file_is_not_downloaded(settings):
    net = Net(strict=False).add("open.canada.ca/data/api/3/action/package_show", ckan(("old", "2026-09-02"), ("new", "2026-10-02")))
    row = one(settings, JB, net)
    assert (row.status, row.reachable, row.robots) == ("ok", "yes", "allowed") and "new" in row.detail
    assert not [u for u in net.urls() if u.endswith(".csv")]


def test_job_bank_whose_catalogue_is_gone_is_unreachable(settings):
    row = one(settings, JB, Net(strict=False))
    assert row.status == "unreachable" and "no resources" in row.detail


def test_aiapply_is_checked_by_its_file(settings):
    dd = {"id": "aiapply", "name": "AIApply", "method": "dedupe_only", "country": "multi"}
    assert one(settings, src(**dd), Net()).status == "no_file"
    p = settings.path("aiapply_applied")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("company,title\nAcme,Data Scientist\nBeta,ML Engineer\n", encoding="utf-8")
    row = one(settings, src(**dd), Net())
    assert (row.status, row.postings) == ("ok", 2) and "2 applied" in row.detail
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    assert one(settings, src(**dd), Net()).status == "error"


def test_a_disabled_source_is_listed_and_not_checked(settings):
    net = Net()
    row = one(settings, src(token="acme", enabled=False), net)
    assert row.status == "disabled" and net.requests == []


def test_a_source_that_raises_is_an_error_row_and_the_others_go_on(settings, monkeypatch):
    def boom(row, ctx):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(health, "_check_reed", boom)
    monkeypatch.setenv("REED_API_KEY", "k")
    rows = check(settings, [src(id="reed", name="Reed", method="reed"), src(id="b", name="B", token="acme")], acme_net())
    assert [(r.source.id, r.status) for r in rows] == [("reed", "error"), ("b", "ok")] and "parser exploded" in rows[0].detail


def test_new_jobs_come_from_the_run(settings):
    run = disc.Run()
    run.credit("a", "new", 3)
    run.credit("b", "listed_only", 2)
    run.credit("b", "new", 1)
    rows = check(settings, [src(id="a", name="A", token="acme"), src(id="b", name="B", token="acme"), src(id="c", name="C", token="acme")],
                 acme_net(), run=run)
    assert [r.new_jobs for r in rows] == [3, 3, 0]


# ------------------------------------------------------------------ the table and the csv
def test_the_table_has_source_method_status_and_new_jobs_and_explains_what_is_not_ok(settings):
    rows = check(settings, [src(id="a", name="Acme board", token="acme"), src(id="b", name="Missing board", token="nosuch")], acme_net())
    text = health.format_table(rows)
    head = text.splitlines()[0].split()
    assert head == ["source", "method", "status", "alerts", "jobs", "parsed", "new", "jobs"]
    assert any(l.startswith("Acme board") and " ok " in l for l in text.splitlines())
    assert "Missing board: not_found:" in text and "Acme board:" not in text


def test_the_check_is_saved_to_a_csv_named_by_the_date_and_a_second_check_appends(settings):
    rows = check(settings, [src(token="acme")], acme_net())
    path = health.save(settings, rows, NOW)
    assert path == settings.path("discover") / "health" / "2026-10-13.csv"
    health.save(settings, rows, NOW.replace(hour=15))
    recs = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["source"] for r in recs] == ["x", "x"] and recs[0]["ts"].startswith("2026-10-13T09:30") and recs[1]["ts"].startswith("2026-10-13T15:30")
    assert list(recs[0]) == health.COLUMNS and recs[0]["status"] == "ok" and recs[0]["postings"] == "6" and recs[0]["new_jobs"] == "0"
    assert path.read_text(encoding="utf-8").count("ts,source") == 1


# ------------------------------------------------------------------ the commands
@pytest.fixture
def cmd(settings, monkeypatch):
    shutil.copy(ROOT / "tests" / "fixtures" / "discovery" / "sources_registry.yaml", Path(settings.raw["paths"]["sources"]))
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.delenv("REED_API_KEY", raising=False)

    def run(net, *argv):
        from careerops.discovery import http
        monkeypatch.setattr(http.requests, "get", lambda url, **kw: net.get(url, **kw))
        monkeypatch.setattr(http.time, "sleep", lambda s: None)
        cli.main(list(argv))
    return run


def shipped_net():
    return (Net(strict=False).add("api.ashbyhq.com/posting-api/job-board/openai", {"jobs": [{"id": "1", "title": "x", "isListed": True}]})
            .add("package_show", ckan(("jan", "2026-10-02"))))


def test_sources_check_prints_a_row_per_source_and_saves_the_csv(cmd, settings, capsys):
    cmd(shipped_net(), "sources", "check")
    out = capsys.readouterr().out
    assert "Source health:" in out and "saved to" in out
    for name in ("LinkedIn", "OpenAI", "Job Bank Canada", "Google DeepMind", "Reed", "AIApply", "SEEK NZ"):
        assert name in out
    (path,) = list((settings.path("discover") / "health").glob("*.csv"))
    recs = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(recs) == 24 and {r["source"]: r["status"] for r in recs}["openai"] == "ok"
    assert {r["source"]: r["status"] for r in recs}["reed"] == "no_credentials" and {r["source"]: r["status"] for r in recs}["linkedin"] == "no_credentials"


def test_sources_check_never_fetches_linkedin(cmd, settings):
    net = shipped_net()
    cmd(net, "sources", "check")
    assert not [u for u in net.urls(True) if "linkedin" in u]


def test_sources_list_prints_the_registry_and_asks_nothing(cmd, capsys):
    net = Net()
    cmd(net, "sources", "list")
    out = capsys.readouterr().out
    assert "24 source(s)" in out and "openai" in out and "token=openai" in out and net.requests == []


def test_a_broken_registry_is_an_error_message_not_a_traceback(cmd, settings, capsys):
    Path(settings.raw["paths"]["sources"]).write_text("sources:\n  - {id: a, name: A}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cmd(Net(), "sources", "list")
    assert e.value.code == 1 and "'url' is missing" in capsys.readouterr().err


def test_every_discover_run_ends_with_the_health_table_and_saves_it(cmd, settings, capsys):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    net = shipped_net().add("boards/acme/jobs", fixture_json("gh_jobs_acme.json"))
    cmd(net, "discover", "--ats")
    out = capsys.readouterr().out
    assert "Source health:" in out and "OpenAI" in out and "saved to" in out
    assert list((settings.path("discover") / "health").glob("*.csv"))


def test_a_dry_run_prints_the_table_and_saves_nothing(cmd, settings, capsys):
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    cmd(shipped_net(), "discover", "--ats", "--dry-run")
    out = capsys.readouterr().out
    assert "Source health:" in out and "(dry run: not saved)" in out and not (settings.path("discover") / "health").exists()


def test_the_health_check_failing_does_not_fail_the_discover_run(cmd, settings, capsys, monkeypatch):
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    monkeypatch.setattr(health, "check_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("table broke")))
    cmd(shipped_net(), "discover", "--ats")
    out = capsys.readouterr().out
    assert "the health check failed: RuntimeError: table broke" in out and "new ad(s)" in out


def test_the_new_jobs_column_counts_what_the_run_wrote(cmd, settings, capsys):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    ashby = {"jobs": [{"id": "9", "title": "AI Engineer", "isListed": True, "location": "London, UK", "jobUrl": "https://jobs.ashbyhq.com/openai/9",
                       "descriptionPlain": "OpenAI is hiring an AI engineer in London. " * 30, "publishedAt": "2026-10-09T09:00:00+00:00"}]}
    net = Net(strict=False).add("api.ashbyhq.com/posting-api/job-board/openai", ashby).add("package_show", ckan(("jan", "2026-10-02")))
    cmd(net, "discover", "--ats")
    capsys.readouterr()
    (path,) = list((settings.path("discover") / "health").glob("*.csv"))
    by = {r["source"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}
    assert by["openai"]["new_jobs"] == "1" and by["openai"]["postings"] == "1" and by["linkedin"]["new_jobs"] == "0"
