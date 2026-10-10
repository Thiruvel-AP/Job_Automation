"""robots.txt handling (discovery/robots.py): RFC 9309 matching (`*`, `$`, longest rule wins), the 24-hour cache, what each status of
robots.txt itself means, and the hosts that are never fetched. No network: `fetch` is a function the test supplies."""
import json

import pytest

from careerops.discovery import robots
from careerops.discovery.robots import RobotsCache, RobotsError
from conftest import FIX

CVL = (FIX / "intake" / "robots_cvlibrary.txt").read_text(encoding="utf-8")


def allowed(text, path, token="career-ops"):
    return robots.parse(text, token).allowed(path)


# ------------------------------------------------------------------ matching
def test_the_cv_library_rules_allow_a_job_page_and_refuse_search_and_apply():
    r = robots.parse(CVL)
    assert r.allowed("/job/219034011") and r.allowed("/job/219034011/data-scientist")
    assert not r.allowed("/job/apply/219034011")                       # Disallow: /job/apply/
    assert not r.allowed("/jobs/data-scientist?q=python&page=2")        # Disallow: /*?
    assert not r.allowed("/job/219034011?utm_source=x")                  # any other query string
    assert r.allowed("/job/219034011?jobId=77")                          # Allow: *?jobId= is the longer, more specific rule
    assert r.allowed("/search?page_number=2")                            # Allow: *?page_number=
    assert not r.allowed("/candidate/profile") and not r.allowed("/api/jobs")


def test_the_group_for_our_token_wins_over_star_and_other_agents_do_not_apply():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: career-ops\nAllow: /\nDisallow: /private\n\nUser-agent: AhrefsBot\nDisallow: /\n"
    assert allowed(text, "/jobs/1") and not allowed(text, "/private/x")
    assert not allowed(text, "/jobs/1", token="someone-else")           # falls to *
    assert allowed("User-agent: AhrefsBot\nDisallow: /\n", "/x")         # no group for us and no *: nothing is disallowed


def test_the_longest_rule_wins_and_allow_wins_a_tie():
    text = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /a/b/c\nAllow: /t\nDisallow: /t\n"
    assert not allowed(text, "/a/x") and allowed(text, "/a/b/x") and not allowed(text, "/a/b/c/d") and allowed(text, "/t")


def test_dollar_anchors_and_star_matches_anything():
    text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*/draft\n"
    assert not allowed(text, "/x/cv.pdf") and allowed(text, "/x/cv.pdf?x=1") and not allowed(text, "/tmp9/y/draft")


def test_empty_disallow_allows_all_and_comments_and_blank_lines_are_ignored():
    assert allowed("User-agent: *\nDisallow:\n", "/anything")
    assert not allowed("# robots\nUser-agent: *   # everyone\nDisallow: /x  # no\n\n", "/x/1")


def test_consecutive_user_agent_lines_share_one_group():
    text = "User-agent: Bingbot\nUser-agent: career-ops\nDisallow: /nope\n"
    assert not allowed(text, "/nope") and allowed(text, "/yes")


def test_crawl_delay_is_read():
    assert robots.parse("User-agent: *\nCrawl-delay: 2\n").crawl_delay == 2.0


# ------------------------------------------------------------------ the checker
class Fetcher:
    def __init__(self, status=200, text="User-agent: *\nDisallow: /private\n"):
        self.status, self.text, self.calls = status, text, []

    def __call__(self, url):
        self.calls.append(url)
        return self.status, self.text


def test_a_disallowed_path_is_refused_with_a_message_that_names_the_rule_and_the_way_out():
    cache = RobotsCache()
    cache.check("https://example.com/jobs/1", Fetcher())
    with pytest.raises(RobotsError) as e:
        cache.check("https://example.com/private/job?id=3", Fetcher())
    msg = str(e.value)
    assert "example.com/robots.txt disallows /private/job?id=3" in msg and "--clipboard" in msg and "\n" not in msg


def test_robots_txt_is_asked_for_once_per_host_and_kept_24_hours(tmp_path):
    now = [1000.0]
    cache, f = RobotsCache(tmp_path, clock=lambda: now[0]), Fetcher()
    cache.check("https://example.com/a", f)
    cache.check("https://example.com/b?x=1", f)
    assert f.calls == ["https://example.com/robots.txt"]
    cache2 = RobotsCache(tmp_path, clock=lambda: now[0])                 # a new process reads the file
    cache2.check("https://example.com/c", f)
    assert len(f.calls) == 1 and json.loads((tmp_path / "example.com.json").read_text(encoding="utf-8"))["fetched"] == 1000.0
    now[0] += robots.TTL_S - 1
    RobotsCache(tmp_path, clock=lambda: now[0]).check("https://example.com/d", f)
    assert len(f.calls) == 1
    now[0] += 2                                                          # past 24 hours: read again
    RobotsCache(tmp_path, clock=lambda: now[0]).check("https://example.com/e", f)
    assert len(f.calls) == 2


def test_each_host_has_its_own_robots_txt():
    cache, f = RobotsCache(), Fetcher()
    cache.check("https://a.example.com/x", f)
    cache.check("https://b.example.com/x", f)
    assert f.calls == ["https://a.example.com/robots.txt", "https://b.example.com/robots.txt"]


@pytest.mark.parametrize("status", [404, 410])
def test_no_robots_txt_means_everything_is_allowed(status):
    RobotsCache().check("https://example.com/private/x", Fetcher(status, ""))


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503, None])
def test_a_robots_txt_that_says_no_or_cannot_be_read_refuses_the_fetch(status):
    with pytest.raises(RobotsError, match="cannot be checked"):
        RobotsCache().check("https://example.com/jobs/1", Fetcher(status, ""))


def test_a_refusal_is_not_written_to_disk(tmp_path):
    with pytest.raises(RobotsError):
        RobotsCache(tmp_path).check("https://example.com/jobs/1", Fetcher(503, ""))
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("host", sorted(robots.DOCUMENTED_API_HOSTS))
def test_a_documented_public_api_host_is_not_refused_for_an_unreadable_robots_txt_but_obeys_one_it_can_read(host):
    RobotsCache().check(f"https://{host}/v1/boards/x/jobs", Fetcher(401, ""))
    with pytest.raises(RobotsError, match="disallows"):
        RobotsCache().check(f"https://{host}/v1/boards/x/jobs", Fetcher(200, "User-agent: *\nDisallow: /v1/\n"))


def test_a_page_host_that_answers_401_is_refused_even_though_an_api_host_is_not():
    with pytest.raises(RobotsError):
        RobotsCache().check("https://www.cv-library.co.uk/job/1", Fetcher(401, ""))


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/123", "https://linkedin.com/comm/jobs/view/1",
                                 "https://uk.linkedin.com/jobs/x", "https://lnkd.in/abc", "http://WWW.LINKEDIN.COM/jobs"])
def test_linkedin_is_never_fetched_and_robots_txt_is_not_even_asked_for(url):
    f = Fetcher()
    with pytest.raises(RobotsError, match="LinkedIn ads come in through job-alert emails"):
        RobotsCache().check(url, f)
    assert f.calls == [] and robots.never_fetch(url)


def test_other_hosts_are_not_taken_for_linkedin():
    assert robots.never_fetch("https://notlinkedin.com/x") is None and robots.never_fetch("https://example.com/linkedin.com") is None
