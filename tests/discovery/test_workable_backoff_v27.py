"""v2.7 item 6: a 429 from Workable keeps every later run away from that host for 24 h, or for as long as its Retry-After says when that is longer (never more
than a week). Nothing is retried or worked around; another host's 429 behaves as before. No network: `Net` answers every request."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from netfake import Net, Resp
from test_board_check import cfg  # noqa: F401  (a settings copy with no listed boards and no alert probes)

from careerops.discovery import blocklog, boards as boards_mod
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from careerops.discovery.seeds import Seed

NOW = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
WORKABLE = "https://apply.workable.com/api/v1/widget/accounts"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board"
HOUR = timedelta(hours=1)


def log_at(tmp_path, now=NOW):
    return blocklog.BlockLog(tmp_path / "blocked_hosts.json", now=lambda: now)


def fetcher(net, log):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, blocklog=log)


def answering(status, retry_after=None, part="apply.workable.com/api") -> Net:
    return Net(strict=False).add(part, Resp("", status, {"Retry-After": retry_after} if retry_after is not None else {}))


def refused_once(tmp_path, retry_after=None, host_url=f"{WORKABLE}/acme"):
    f = fetcher(answering(429, retry_after), log_at(tmp_path))
    with pytest.raises(Blocked) as e:
        f.request(host_url)
    return e.value


def saved(tmp_path, host="apply.workable.com") -> dict:
    return json.loads((tmp_path / "blocked_hosts.json").read_text(encoding="utf-8"))["hosts"][host]


def until(delta: timedelta) -> str:
    return (NOW + delta).isoformat(timespec="seconds")


# ------------------------------------------------------------------ how long
def test_a_429_with_no_retry_after_backs_the_host_off_for_24_hours(tmp_path):
    e = refused_once(tmp_path)
    assert e.status == 429 and "not asked again" in str(e)
    assert saved(tmp_path)["status"] == 429 and saved(tmp_path)["until"] == until(24 * HOUR)


@pytest.mark.parametrize("header, hours", [("172800", 48), ("600", 24), ("0", 24), ("soon", 24), ("", 24), (str(30 * 86400), 7 * 24)])
def test_retry_after_in_seconds_is_used_when_it_is_longer_than_24_hours_and_never_beyond_a_week(tmp_path, header, hours):
    refused_once(tmp_path, header)
    assert saved(tmp_path)["until"] == until(hours * HOUR)


def test_retry_after_as_an_http_date_is_used_when_it_is_longer(tmp_path):
    refused_once(tmp_path, format_datetime(NOW + 36 * HOUR, usegmt=True))
    assert saved(tmp_path)["until"] == until(36 * HOUR)


def test_the_retry_after_reading():
    assert blocklog.parse_retry_after("120", NOW) == 120 and blocklog.parse_retry_after("0", NOW) is None
    assert blocklog.parse_retry_after(None, NOW) is None and blocklog.parse_retry_after("later", NOW) is None
    assert blocklog.parse_retry_after(format_datetime(NOW + HOUR, usegmt=True), NOW) == 3600
    assert blocklog.parse_retry_after(format_datetime(NOW - HOUR, usegmt=True), NOW) is None            # a date in the past asks for nothing
    assert blocklog.backoff_seconds(None, NOW) == 24 * 3600 and blocklog.backoff_seconds("90000", NOW) == 90000


# ------------------------------------------------------------------ the next runs
def test_a_later_run_inside_the_window_sends_nothing_to_workable_not_even_robots_txt(tmp_path):
    refused_once(tmp_path)
    net = answering(200)
    f = fetcher(net, log_at(tmp_path, NOW + 23 * HOUR))
    with pytest.raises(Blocked, match="until 2026-11-05 09:30 UTC") as e:
        f.request(f"{WORKABLE}/acme")
    assert e.value.status == 429                                                    # so a provider is stopped for the run, not just counted
    with pytest.raises(Blocked):
        f.json(f"{WORKABLE}/other")
    assert net.requests == []


def test_after_the_window_workable_is_asked_again(tmp_path):
    refused_once(tmp_path)
    net = answering(200)
    net.add("apply.workable.com/api", Resp({"name": "Acme", "jobs": []}))
    net.routes.reverse()                                                            # the 200 route first
    f = fetcher(net, log_at(tmp_path, NOW + 24 * HOUR + timedelta(minutes=1)))
    assert f.json(f"{WORKABLE}/acme") == {"name": "Acme", "jobs": []} and net.urls()


def test_a_subdomain_of_workable_counts_and_another_host_does_not(tmp_path):
    f = fetcher(Net(strict=False).add("workable.com", Resp("", 429)), log_at(tmp_path))
    with pytest.raises(Blocked):
        f.request("https://jobs.workable.com/api/x")
    assert saved(tmp_path, "jobs.workable.com")["until"] == until(24 * HOUR)
    other = fetcher(Net(strict=False).add("greenhouse.io", Resp("", 429)), log_at(tmp_path))
    with pytest.raises(Blocked):
        other.request("https://boards-api.greenhouse.io/v1/boards/acme")
    assert "until" not in saved(tmp_path, "boards-api.greenhouse.io")               # the old rule: this run, then a slower pace


def test_a_429_from_another_host_is_unchanged_the_next_run_asks_again_at_a_slower_pace(tmp_path):
    f = fetcher(answering(429, "172800", part="api.ashbyhq.com/posting-api"), log_at(tmp_path))
    with pytest.raises(Blocked):
        f.request(f"{ASHBY}/one")
    net = Net(strict=False).add("api.ashbyhq.com/posting-api", Resp({"jobs": []}))
    g = fetcher(net, log_at(tmp_path, NOW + 2 * HOUR))
    assert g.json(f"{ASHBY}/one") == {"jobs": []} and net.urls()


def test_a_401_from_workable_is_not_a_long_backoff(tmp_path):
    f = fetcher(answering(401), log_at(tmp_path))
    with pytest.raises(Blocked):
        f.request(f"{WORKABLE}/acme")
    assert "until" not in saved(tmp_path)


def test_the_health_note_says_when_the_host_may_be_asked_again(tmp_path):
    refused_once(tmp_path)
    note = log_at(tmp_path, NOW + 2 * HOUR).describe("apply.workable.com")
    assert "HTTP 429" in note and "not asked at all until 2026-11-05 09:30 UTC" in note
    assert "not asked at all" not in log_at(tmp_path, NOW + 25 * HOUR).describe("apply.workable.com")          # over: only the slower pace remains


# ------------------------------------------------------------------ through the board search
def test_a_workable_429_stops_the_provider_for_the_run_and_a_later_run_does_not_ask(cfg, tmp_path):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    net = answering(429, "7200")
    f = fetcher(net, log_at(tmp_path))
    index = boards_mod.BoardIndex(cfg)
    boards_mod.find(Seed("Acme Analytics", "archive"), f, index)
    boards_mod.find(Seed("Beta Systems", "archive"), f, index)
    workable = [u for u in net.urls() if "workable" in u]
    assert len(workable) == 1 and "workable" in index.stopped and index.refused_counts()["workable"] == 1       # the second company never asked

    net2 = answering(200)
    again = boards_mod.BoardIndex(cfg)
    boards_mod.find(Seed("Acme Analytics", "archive"), fetcher(net2, log_at(tmp_path, NOW + 3 * HOUR)), again)
    assert not [u for u in net2.urls(True) if "workable" in u] and "workable" in again.stopped
    assert "until 2026-11-05 09:30 UTC" in again.stopped["workable"]
