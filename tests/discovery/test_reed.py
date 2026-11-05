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


def test_by_default_only_direct_employers_are_searched(settings, key):
    net = reed_net()
    out = [c for kind, c in reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY) if kind == "candidate"]
    searches = [r["params"] for r in net.requests if r["url"].endswith("/search")]
    assert len(searches) == 1 and searches[0]["postedByDirectEmployer"] == "true" and "postedByRecruitmentAgency" not in searches[0]
    assert searches[0]["resultsToTake"] == 100 and all(r["url"].startswith("https://www.reed.co.uk/api/1.0/") for r in net.requests)
    assert out and all(c.agency is False for c in out) and not any("/jobs/6001" in u for u in net.urls())          # no agency ad is read


def test_each_query_runs_for_agencies_too_when_include_agencies_is_true_and_the_agency_ones_are_flagged(settings, key):
    net = reed_net()
    out = [c for kind, c in reed.fetch(fetcher(net), cfg(settings, include_agencies=True), want=lambda c: True, today=TODAY) if kind == "candidate"]
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


# ------------------------------------------------------------------ v3.0: externalUrl is the apply URL, provider detection runs on it, counts for the run record
def ext_net(url):
    """A direct-employer search with one job whose details give `externalUrl` = url (None: the field is absent)."""
    one = {**fixture_json("reed_search_direct.json"), "results": [fixture_json("reed_search_direct.json")["results"][0]]}
    full = {**fixture_json("reed_job_5001.json"), **({"externalUrl": url} if url is not None else {})}
    return Net().add("api/1.0/search", one).add("api/1.0/jobs/5001", full)


def run_one(settings, url):
    net = ext_net(url)
    items = list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: True, today=TODAY))
    counts = {}
    for k, v in items:
        if k == "count":
            counts[v[0]] = counts.get(v[0], 0) + v[1]
    return [c for k, c in items if k == "candidate"][0], counts


@pytest.mark.parametrize("url, kind, ats", [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x", "greenhouse", "greenhouse"),
    ("https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666", "lever", "lever"),
    ("https://jobs.ashbyhq.com/acme/0a1b2c3d-1111-2222-3333-444455556666", "ashby", "ashby"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/London/Data-Scientist_R1", "workday", "workday"),
    ("https://apply.workable.com/acme/j/ABCDEF1234/", "employer_other", "workable"),
    ("https://acme.teamtailor.com/jobs/123-data-scientist", "employer_other", "teamtailor"),
])
def test_an_external_url_is_the_apply_url_and_a_known_ats_is_recognised(settings, key, url, kind, ats):
    c, counts = run_one(settings, url)
    assert c.apply_url == url and c.apply_kind == kind and c.url.startswith("https://www.reed.co.uk/jobs/")          # Reed's page stays the ad's address
    assert counts["with_external_url"] == 1 and counts["external_known_ats"] == 1 and counts[f"external_{ats}"] == 1
    assert counts["results"] == 1 and counts["direct_employer"] == 1 and counts["details_read"] == 1 and "agency_results" not in counts


@pytest.mark.parametrize("url, kind", [("https://careers.acme.com/jobs/42", "employer_other"), ("https://www.totaljobs.com/job/42", "aggregator_only")])
def test_an_external_url_on_no_known_ats_is_still_the_apply_url_but_is_not_counted_as_one(settings, key, url, kind):
    c, counts = run_one(settings, url)
    assert c.apply_url == url and c.apply_kind == kind
    assert counts["with_external_url"] == 1 and "external_known_ats" not in counts


@pytest.mark.parametrize("url", [None, "", "   ", "javascript:alert(1)", "ftp://example.com/x", "not a url"])
def test_no_external_url_or_one_that_is_not_http_leaves_the_apply_url_empty(settings, key, url):
    c, counts = run_one(settings, url)
    assert c.apply_url == "" and c.apply_kind == ""
    assert "with_external_url" not in counts and counts["details_read"] == 1


def test_one_request_a_second_to_reed(settings, key):
    net, slept = reed_net(), []
    f = Fetcher(RobotsCache(), get=net.get, sleep=slept.append, clock=lambda: 0.0)               # the default gap, a clock that does not move
    list(reed.fetch(f, cfg(settings), want=lambda c: not c.closed, today=TODAY))
    assert len(net.requests) >= 3 and abs(sum(slept) - (len(net.requests) - 1)) < 1e-6 and set(slept) == {1.0}


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_401_403_or_429_on_the_search_stops_reed_for_the_run(settings, key, status):
    net = Net().add("api/1.0/search", "no", status)
    items = list(reed.fetch(fetcher(net), cfg(settings, queries=["data scientist", "ai engineer"]), want=lambda c: True))
    assert len(net.requests) == 1 and any(k == "note" and str(status) in c for k, c in items)             # the second query is not asked
    assert all(KEY not in str(c) for _, c in items)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refusal_on_a_details_call_stops_the_next_ones(settings, key, status):
    net = Net().add("api/1.0/search", fixture_json("reed_search_direct.json")).add("api/1.0/jobs/", "no", status)
    items = list(reed.fetch(fetcher(net), cfg(settings), want=lambda c: not c.closed, today=TODAY))
    assert len([u for u in net.urls() if "/jobs/" in u]) == 1 and any(k == "note" and str(status) in c for k, c in items)
    assert not [1 for k, c in items if k == "candidate" and not c.closed]


def test_the_run_record_carries_the_counts_and_the_candidate_keeps_the_external_url(settings, db, key):
    from careerops.discovery.discover import Discover
    net = reed_net()
    settings.raw["discover"]["reed"].update({"queries": ["data scientist"], "include_agencies": False})
    d = Discover(settings, db, fetcher(net), today=TODAY, say=lambda s: None)
    kept = []
    d.accept = lambda c, label: kept.append(c) or "ok"
    d.from_reed()
    rec = d.run.sources["reed"]
    assert rec["results"] == 3 and rec["direct_employer"] == 3 and rec["details_read"] >= 1 and "agency_results" not in rec
    assert kept and all(c.apply_url == "" for c in kept)                                              # the fixtures carry no externalUrl
