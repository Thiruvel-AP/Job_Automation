"""A 401 / 403 / 429 from a host (the first live run: `ashby API: HTTP 401: api.ashbyhq.com blocks automated access`) stops that host for the rest
of the run, is logged with its time and the number of requests that came before it, makes the NEXT run wait at least 2 s between requests to
that host, and shows in the health table. Nothing is retried or worked around. No network: `Net` answers every request."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from careerops.discovery import blocklog, discover as disc, health, registry
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, Resp

ASHBY = "https://api.ashbyhq.com/posting-api/job-board"
NOW = datetime(2026, 10, 21, 14, 7, 46, tzinfo=timezone.utc)


def ashby_net(blocked_after: int = 3) -> Net:
    """Ashby answers 200 until `blocked_after` requests to it were made, then 401."""
    net = Net(strict=False)
    net.add("api.ashbyhq.com/posting-api", lambda url, **kw: Resp({"jobs": []}) if sum("ashbyhq" in r["url"] and not r["url"].endswith("robots.txt")
                                                                                         for r in net.requests) <= blocked_after else Resp("", 401))
    return net


def fetcher(net, log=None, sleeps=None, clock=lambda: 0.0):
    return Fetcher(RobotsCache(), get=net.get, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), clock=clock, gap_s=0.0,
                   blocklog=log)


def log_at(tmp_path, now=NOW):
    return blocklog.BlockLog(tmp_path / "blocked_hosts.json", now=lambda: now)


def ashby_calls(net) -> int:
    return sum("ashbyhq" in r["url"] and not r["url"].endswith("robots.txt") for r in net.requests)


def test_the_401_is_logged_with_its_time_and_the_requests_before_it(tmp_path):
    net, log = ashby_net(blocked_after=2), log_at(tmp_path)
    f = fetcher(net, log)
    f.request(f"{ASHBY}/one")
    f.request(f"{ASHBY}/two")
    with pytest.raises(Blocked):
        f.request(f"{ASHBY}/three")
    saved = json.loads((tmp_path / "blocked_hosts.json").read_text(encoding="utf-8"))["hosts"]["api.ashbyhq.com"]
    assert saved["status"] == 401 and saved["at"] == "2026-10-21T14:07:46+00:00"
    assert saved["requests_before"] == 3 and saved["run_requests"] == 4          # robots.txt, two boards; then the refused one


def test_after_a_401_no_further_ashby_request_is_made_in_that_run(tmp_path):
    net = ashby_net(blocked_after=1)
    f = fetcher(net, log_at(tmp_path))
    f.request(f"{ASHBY}/one")
    with pytest.raises(Blocked):
        f.request(f"{ASHBY}/two")
    seen = ashby_calls(net)
    for board in ("three", "four", "five"):
        with pytest.raises(Blocked, match="not asked again"):
            f.request(f"{ASHBY}/{board}")
        with pytest.raises(Blocked):
            f.json(f"{ASHBY}/{board}")
    assert ashby_calls(net) == seen == 2                                      # the 401 itself was the last request sent


def test_the_next_run_waits_at_least_two_seconds_between_ashby_requests(tmp_path):
    first = fetcher(ashby_net(blocked_after=0), log_at(tmp_path))
    with pytest.raises(Blocked):
        first.request(f"{ASHBY}/one")
    net, sleeps, now = ashby_net(blocked_after=99), [], [0.0]
    f = fetcher(net, log_at(tmp_path, NOW + timedelta(hours=2)), sleeps, clock=lambda: now[0])
    f.request(f"{ASHBY}/one")
    f.request(f"{ASHBY}/two")
    assert sleeps and min(sleeps) >= 2.0 - 1e-9 and sum(sleeps) >= 2.0


def test_another_host_is_not_slowed_down(tmp_path):
    first = fetcher(ashby_net(blocked_after=0), log_at(tmp_path))
    with pytest.raises(Blocked):
        first.request(f"{ASHBY}/one")
    sleeps = []
    f = fetcher(Net(strict=False), log_at(tmp_path), sleeps)
    f.request("https://boards-api.greenhouse.io/v1/boards/acme")
    f.request("https://boards-api.greenhouse.io/v1/boards/beta")
    assert sleeps == []


def test_an_old_block_is_forgotten_after_a_week(tmp_path):
    first = fetcher(ashby_net(blocked_after=0), log_at(tmp_path))
    with pytest.raises(Blocked):
        first.request(f"{ASHBY}/one")
    sleeps = []
    f = fetcher(ashby_net(blocked_after=99), log_at(tmp_path, NOW + timedelta(days=8)), sleeps)
    f.request(f"{ASHBY}/one")
    f.request(f"{ASHBY}/two")
    assert sleeps == []


def test_a_log_that_cannot_be_read_is_an_empty_one(tmp_path):
    (tmp_path / "blocked_hosts.json").write_text("{not json", encoding="utf-8")
    assert log_at(tmp_path).recent("api.ashbyhq.com") is None
    log_at(tmp_path).record("api.ashbyhq.com", 401, 0, 1)                         # and is written over, whole
    assert log_at(tmp_path).recent("api.ashbyhq.com")["status"] == 401


def test_the_health_table_shows_blocked_with_the_time_it_was_last_seen(settings, tmp_path):
    reg = registry.parse({"sources": [{"id": "ashby-boards", "name": "Ashby boards", "url": "https://x.example", "country": "GB", "method": "ashby"}]})
    log = log_at(tmp_path)
    f = fetcher(ashby_net(blocked_after=0), log)
    (row,) = health.check_all(settings, reg, f, cache=disc.BoardCache(settings, f))
    assert row.status == "blocked" and row.reachable == "no"
    assert "14:07:46" in row.detail and "api.ashbyhq.com" in row.detail and "401" in row.detail


def test_a_recent_block_is_noted_on_a_row_that_works_again(settings, tmp_path):
    reg = registry.parse({"sources": [{"id": "ashby-boards", "name": "Ashby boards", "url": "https://x.example", "country": "GB", "method": "ashby"}]})
    first = fetcher(ashby_net(blocked_after=0), log_at(tmp_path))
    with pytest.raises(Blocked):
        first.request(f"{ASHBY}/x")
    f = fetcher(ashby_net(blocked_after=99), log_at(tmp_path, NOW + timedelta(hours=3)))
    (row,) = health.check_all(settings, reg, f, cache=disc.BoardCache(settings, f))
    assert row.status == "ok" and "14:07:46" in row.detail and "2 s apart" in row.detail
