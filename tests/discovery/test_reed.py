"""discovery/reed.py: the Reed Jobseeker API. Basic auth with the key as username and an empty password, search then details, agency
posts flagged by running each query twice, closed jobs skipped, the key never shown. No network: `Net` answers."""
from datetime import datetime, timezone

import pytest

from careerops.discovery import reed
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, Resp, fixture_json

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
KEY = "reed-test-key-123"


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def reed_net():
    def search(url, params=None, **kw):
        if params.get("postedByRecruitmentAgency") == "true":
            return Resp(fixture_json("reed_search_agency.json"))
        return Resp(fixture_json("reed_search_direct.json") if params.get("keywords") == "data scientist" else {"results": []})
    def other(url, **kw):                                  # any other job: the first one's full ad under its own id
        jid = int(url.rsplit("/", 1)[1])
        return Resp({**fixture_json("reed_job_5001.json"), "jobId": jid, "jobTitle": f"Job {jid}"})
    return (Net().add("api/1.0/search", search).add("api/1.0/jobs/5001", fixture_json("reed_job_5001.json"))
            .add("api/1.0/jobs/6001", fixture_json("reed_job_6001.json")).add("api/1.0/jobs/", other))


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("REED_API_KEY", KEY)


def cfg(settings, **kw):
    settings.raw["discover"]["reed"].update({"queries": ["data scientist"], **kw})
    return settings


def test_the_key_is_basic_auth_username_with_an_empty_password_and_never_in_the_url(settings, key):
    net = reed_net()
    list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY))
    assert all(r["auth"] == (KEY, "") for r in net.requests)
    assert all(KEY not in r["url"] and KEY not in str(r["params"]) for r in net.requests)
    assert all(r["url"].startswith("https://www.reed.co.uk/api/1.0/") for r in net.requests)


def test_reed_robots_txt_is_not_asked_for_a_credentialed_api(settings, key):
    net = reed_net()
    list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY))
    assert not any(u.endswith("robots.txt") for u in net.urls(True))


def test_each_query_runs_for_direct_employers_and_for_agencies_and_the_agency_ones_are_flagged(settings, key):
    net = reed_net()
    out = [c for kind, c in reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY) if kind == "candidate"]
    searches = [r["params"] for r in net.requests if r["url"].endswith("/search")]
    assert [("postedByDirectEmployer" in p, "postedByRecruitmentAgency" in p) for p in searches] == [(True, False), (False, True)]
    assert all(p["resultsToTake"] == 100 and p["keywords"] == "data scientist" for p in searches)
    by_id = {c.ext_id: c for c in out}
    assert by_id["5001"].agency is False and by_id["6001"].agency is True
    assert (by_id["6001"].company, by_id["6001"].title) == ("Northgate Recruitment Solutions", "Machine Learning Engineer")


def test_the_full_description_comes_from_the_details_call_not_the_search_snippet(settings, key):
    net = reed_net()
    out = {c.ext_id: c for kind, c in reed.fetch(fetcher(net), cfg(settings), want=lambda c: c.ext_id in ("5001", "6001"), today=TODAY)
           if kind == "candidate"}
    c = out["5001"]
    assert "A snippet of the ad" not in c.description and "Reed full description." in c.description and len(c.description) > 400
    assert "<p>" not in c.description
    assert (c.source, c.location, c.salary, c.posted, c.closed) == ("reed", "Leeds", "GBP 45,000-55,000", "2026-10-08", False)
    assert c.url.startswith("https://www.reed.co.uk/jobs/data-scientist/5001")


def test_a_job_the_filters_would_drop_costs_no_details_request(settings, key):
    net = reed_net()
    list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: c.title == "Data Scientist", today=TODAY))
    asked = [u for u in net.urls() if "/jobs/" in u]
    assert asked == ["https://www.reed.co.uk/api/1.0/jobs/5001"]


def test_a_closed_job_is_reported_closed_and_not_fetched(settings, key):
    net = reed_net()
    out = [c for kind, c in reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY) if kind == "candidate"]
    closed = next(c for c in out if c.ext_id == "5003")
    assert closed.closed is True and not any("/jobs/5003" in u for u in net.urls())


def test_the_details_budget_is_a_per_run_limit_with_a_note(settings, key):
    net = reed_net()
    items = list(reed.fetch(fetcher(net), cfg(settings, details_per_run=1), want=lambda c: not c.closed, today=TODAY))
    assert sum(1 for k, c in items if k == "candidate") == 1 and any(k == "note" and "per-run limit" in c for k, c in items)


def test_a_missing_key_is_a_clear_error_and_nothing_is_asked(settings, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    net = reed_net()
    with pytest.raises(reed.ReedKeyMissing, match="REED_API_KEY is not set.*README"):
        list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: True))
    assert net.requests == []


def test_a_401_stops_reed_and_is_reported_without_the_key(settings, key):
    net = Net().add("api/1.0/search", "bad key", 401)
    items = list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: True))
    assert len(net.requests) == 1 and any(k == "note" for k, _ in items)
    assert all(KEY not in str(c) for _, c in items)


def test_a_blank_job_for_an_unknown_id_is_no_job(settings, key):
    net = Net().add("api/1.0/jobs/9", {"jobId": 0})
    assert reed.details(fetcher(net), KEY, 9) is None


@pytest.mark.parametrize("url, jid", [("https://www.reed.co.uk/jobs/data-scientist/5001?source=alert", "5001"),
                                      ("https://www.reed.co.uk/jobs/5001", "5001"), ("https://www.reed.co.uk/jobs/data-scientist", None),
                                      ("https://example.com/jobs/x/5", None)])
def test_job_id_from_url(url, jid):
    assert reed.job_id_from_url(url) == jid
