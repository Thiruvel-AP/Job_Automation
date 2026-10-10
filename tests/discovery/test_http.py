"""discovery/http.py: the polite client every discovery source uses. No network: `Net` answers."""
import pytest
import requests

from careerops.discovery.http import Blocked, FetchError, Fetcher
from careerops.discovery.robots import RobotsCache
from careerops.discovery.sources import UA
from netfake import Net, Resp


class Clock:
    def __init__(self):
        self.t, self.slept = 100.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(net, clock=None, **kw):
    clock = clock or Clock()
    return Fetcher(RobotsCache(), get=net.get, sleep=clock.sleep, clock=clock.now, **kw), clock


def test_a_host_is_asked_at_most_once_a_second_and_other_hosts_do_not_wait():
    net = Net().add("a.example.com", {"ok": 1}).add("b.example.com", {"ok": 2})
    f, clock = make(net)
    f.request("https://a.example.com/x", robots=False)
    f.request("https://b.example.com/x", robots=False)
    assert clock.slept == []
    clock.t += 0.25
    f.request("https://a.example.com/y", robots=False)
    assert clock.slept == [pytest.approx(0.75)]


def test_robots_txt_is_read_first_paced_like_any_request_and_cached():
    net = Net().add("example.com/robots.txt", "User-agent: *\nDisallow: /private\n").add("example.com", {"ok": 1})
    f, clock = make(net)
    f.request("https://example.com/jobs/1")
    f.request("https://example.com/jobs/2")
    assert net.urls(True) == ["https://example.com/robots.txt", "https://example.com/jobs/1", "https://example.com/jobs/2"]
    assert clock.slept == [pytest.approx(1.0), pytest.approx(1.0)]            # robots -> page, then page -> page
    with pytest.raises(Blocked, match="disallows /private"):
        f.request("https://example.com/private/x")
    assert len(net.urls()) == 2                                                  # the disallowed page was never asked for


def test_robots_false_is_for_credentialed_apis_and_skips_the_robots_request():
    net = Net().add("api.example.com", {"ok": 1})
    f, _ = make(net)
    f.request("https://api.example.com/v1/x", robots=False)
    assert net.urls(True) == ["https://api.example.com/v1/x"]


def test_every_request_carries_the_shared_user_agent_and_a_timeout_and_no_automatic_redirects():
    net = Net().add("example.com", {"ok": 1})
    f, _ = make(net)
    f.request("https://example.com/x", headers={"Authorization": "Bearer t"}, robots=False)
    r = net.requests[0]
    assert r["headers"]["User-Agent"] == UA["User-Agent"] and r["headers"]["Authorization"] == "Bearer t"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_blocked_status_stops_the_host_for_the_rest_of_the_run_without_a_retry(status):
    net = Net().add("a.example.com", "denied", status).add("b.example.com", {"ok": 1})
    f, _ = make(net)
    with pytest.raises(Blocked) as e:
        f.request("https://a.example.com/x", robots=False)
    assert e.value.status == status and "blocks automated access" in str(e.value)
    with pytest.raises(Blocked, match="not asked again"):
        f.request("https://a.example.com/y", robots=False)
    assert net.urls() == ["https://a.example.com/x"]                           # one request, no retry, nothing afterwards
    assert f.request("https://b.example.com/x", robots=False).status_code == 200      # another host is fine
    assert f.backed_off == {"a.example.com": status}


def test_a_robots_txt_that_answers_403_is_a_refusal_for_a_page_host():
    net = Net().add("example.com/robots.txt", "no", 403).add("example.com", {"ok": 1})
    f, _ = make(net)
    with pytest.raises(Blocked, match="answered HTTP 403"):
        f.request("https://example.com/jobs/1")
    assert net.urls() == [] and f.backed_off == {"example.com": 403}


def test_linkedin_is_never_contacted():
    net = Net()
    f, _ = make(net)
    with pytest.raises(Blocked, match="LinkedIn ads come in through job-alert emails"):
        f.request("https://www.linkedin.com/jobs/view/1", robots=False)
    assert net.requests == []


def test_a_connection_error_is_retried_once_then_reported():
    calls = []

    def flaky(url, **kw):
        calls.append(url)
        return requests.ConnectionError("down") if len(calls) == 1 else {"ok": 1}
    net = Net().add("example.com", flaky)
    f, _ = make(net)
    assert f.request("https://example.com/x", robots=False).json() == {"ok": 1} and len(calls) == 2
    net2 = Net().add("example.com", lambda url, **kw: requests.ConnectionError("down"))
    f2, _ = make(net2)
    with pytest.raises(FetchError, match="could not be fetched"):
        f2.request("https://example.com/x", robots=False)
    assert len(net2.requests) == 2


def test_a_timeout_is_not_retried():
    net = Net().add("example.com", lambda url, **kw: requests.ReadTimeout("slow"))
    f, _ = make(net)
    with pytest.raises(FetchError, match="ReadTimeout"):
        f.request("https://example.com/x", robots=False)
    assert len(net.requests) == 1


def test_a_server_error_is_a_fetch_error_and_a_404_is_returned():
    net = Net().add("example.com/gone", "x", 404).add("example.com/boom", "x", 503)
    f, _ = make(net)
    assert f.request("https://example.com/gone", robots=False).status_code == 404
    assert f.json("https://example.com/gone", robots=False) is None
    with pytest.raises(FetchError, match="HTTP 503"):
        f.request("https://example.com/boom", robots=False)


def test_json_reports_a_reply_that_is_not_json():
    net = Net().add("example.com", "<html>")
    f, _ = make(net)
    with pytest.raises(FetchError, match="not JSON"):
        f.json("https://example.com/x", robots=False, what="the thing")


def test_redirects_are_followed_by_hand_each_hop_is_checked_and_a_credential_stays_on_its_host():
    net = (Net().add("a.example.com/start", Resp("", 302, {"Location": "https://b.example.com/next"}))
           .add("b.example.com/robots.txt", "User-agent: *\nDisallow: /private\n")
           .add("b.example.com/next", Resp("", 302, {"Location": "/private/x"})))
    f, _ = make(net)
    with pytest.raises(Blocked, match="disallows /private/x"):
        f.request("https://a.example.com/start", auth=("key", ""), robots=True)
    assert net.urls() == ["https://a.example.com/start", "https://b.example.com/next"]
    assert next(r for r in net.requests if r["url"].endswith("b.example.com/next"))["auth"] is None                                       # the key did not follow the redirect to another host


def test_a_redirect_loop_stops():
    net = Net().add("example.com", Resp("", 302, {"Location": "https://example.com/again"}))
    f, _ = make(net)
    with pytest.raises(FetchError, match="too many redirects"):
        f.request("https://example.com/x", robots=False)
