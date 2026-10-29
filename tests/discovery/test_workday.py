"""Workday discovery (discovery/workday.py): the public CXS endpoint, `POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs` with
{appliedFacets:{}, limit:20, offset:n, searchText}, for the sites you list under `sources.workday: [{company, url}]`. robots.txt is read before the first request
and a tenant that disallows /wday/cxs/ is skipped; one request a second per host; at most max_pages (10) list pages per tenant per run. "Posted Today / Yesterday /
N Days Ago" become posted_at; "30+ Days Ago" is tier 3. A 422, or a redirect to community.workday.com/maintenance-page or /wday/drs/outage, is transient: retried
next run, never "zero jobs". No network: fake `get` / `post`."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from netfake import Resp

from careerops.discovery import applykind, discover as disc, freshness, workday
from careerops.discovery.ats import Board, Posting, board_postings, configured_boards
from careerops.discovery.discover import Discover
from careerops.discovery.http import Blocked, FetchError, Fetcher
from careerops.discovery.robots import RobotsCache

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def ago(days: int) -> str:
    """The date `days` before TODAY: what the parser must give, whatever the real day is."""
    return (TODAY - timedelta(days=days)).isoformat()


URL = "https://acme.wd3.myworkdayjobs.com/en-US/External_Careers"
DESC = "<p>You will build forecasting models for retailers.</p><ul><li>Python</li><li>Statistics</li></ul>" + "<p>We value curiosity and clear writing.</p>" * 40


# ------------------------------------------------------------------ the address
@pytest.mark.parametrize("url,host,tenant,site,locale", [
    ("https://acme.wd3.myworkdayjobs.com/en-US/External_Careers", "acme.wd3.myworkdayjobs.com", "acme", "External_Careers", "en-US"),
    ("https://acme.wd1.myworkdayjobs.com/Careers", "acme.wd1.myworkdayjobs.com", "acme", "Careers", ""),
    ("https://ACME.wd5.myworkdayjobs.com/en-GB/Acme_Jobs/", "acme.wd5.myworkdayjobs.com", "acme", "Acme_Jobs", "en-GB"),
    ("https://acme.wd3.myworkdayjobs.com/fr-FR/External/job/London/Data-Scientist_R123?q=x#top", "acme.wd3.myworkdayjobs.com", "acme", "External", "fr-FR"),
    ("https://acme.wd103.myworkdayjobs.com/External", "acme.wd103.myworkdayjobs.com", "acme", "External", ""),
    ("https://wd5.myworkdaysite.com/recruiting/acme/External", "wd5.myworkdaysite.com", "acme", "External", ""),
    ("https://wd5.myworkdaysite.com/en-US/recruiting/acme/External/", "wd5.myworkdaysite.com", "acme", "External", "en-US"),
    ("https://wd3.myworkdaysite.com/recruiting/acme/Acme-Careers/job/Leeds/X_1", "wd3.myworkdaysite.com", "acme", "Acme-Careers", ""),
])
def test_both_host_forms_are_parsed(url, host, tenant, site, locale):
    s = workday.parse_url(url)
    assert (s.host, s.tenant, s.site, s.locale) == (host, tenant, site, locale)
    assert s.api == f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


@pytest.mark.parametrize("url", [
    "", "https://example.com/careers", "https://acme.wd3.myworkdayjobs.com", "https://acme.wd3.myworkdayjobs.com/", "https://acme.wd3.myworkdayjobs.com/en-US",
    "https://acme.wd3.myworkdayjobs.com.evil.example/External", "https://notmyworkdayjobs.com/External", "https://myworkdayjobs.com/External",
    "https://wd5.myworkdaysite.com/", "https://wd5.myworkdaysite.com/recruiting/acme", "https://wd5.myworkdaysite.com/acme/External",
    "ftp://acme.wd3.myworkdayjobs.com/External", "not a url",
])
def test_anything_else_is_not_a_workday_site(url):
    assert workday.parse_url(url) is None


def test_the_detail_and_public_addresses():
    s = workday.parse_url(URL)
    assert s.detail("/job/London/Data-Scientist_R1") == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External_Careers/job/London/Data-Scientist_R1"
    assert s.public("/job/London/Data-Scientist_R1") == "https://acme.wd3.myworkdayjobs.com/en-US/External_Careers/job/London/Data-Scientist_R1"
    bare = workday.parse_url("https://acme.wd1.myworkdayjobs.com/Careers")
    assert bare.public("/job/X_1") == "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/X_1"
    site = workday.parse_url("https://wd5.myworkdaysite.com/recruiting/acme/External")
    assert site.public("/job/X_1") == "https://wd5.myworkdaysite.com/en-US/recruiting/acme/External/job/X_1"


def test_a_site_survives_being_a_board_slug():
    for url in (URL, "https://wd5.myworkdaysite.com/recruiting/acme/External", "https://acme.wd1.myworkdayjobs.com/Careers"):
        s = workday.parse_url(url)
        back = workday.site_from_slug(s.slug)
        assert (back.host, back.tenant, back.site) == (s.host, s.tenant, s.site)
        assert "/" not in s.slug and ":" not in s.slug and " " not in s.slug                         # it is part of a cache file name


# ------------------------------------------------------------------ "Posted N Days Ago"
@pytest.mark.parametrize("text,expected", [
    ("Posted Today", 0),
    ("Posted Yesterday", 1),
    ("Posted 2 Days Ago", 2),
    ("Posted 1 Day Ago", 1),
    ("Posted 29 Days Ago", 29),
    ("posted today", 0),
    ("  Posted   3   days ago ", 3),
    ("Posted 30+ Days Ago", 30),                       # at least 30 days: dated 30 days back, which is tier 3 whatever the real day was
    ("Posted 45+ Days Ago", 45),
])
def test_posted_on_becomes_a_date(text, expected):
    assert workday.posted_on(text, TODAY) == ago(expected)


def test_posted_on_counts_from_the_day_it_is_given_not_from_the_real_one():
    assert workday.posted_on("Posted Today", date(2001, 2, 3)) == "2001-02-03"
    assert workday.posted_on("Posted 2 Days Ago", date(2001, 3, 1)) == "2001-02-27"


@pytest.mark.parametrize("text", ["", None, "Posted recently", "Today", "Posted Last Week", "gestern"])
def test_anything_else_gives_no_date(text):
    assert workday.posted_on(text, TODAY) is None


def test_today_is_tier_0_yesterday_tier_0_or_1_and_30_plus_is_tier_3():
    def tier(text):
        return freshness.tier({"posted_at": workday.posted_on(text, TODAY), "posted_at_source": "workday.postedOn", "first_seen": "2026-10-27T11:00:00+00:00"}, NOW)
    assert tier("Posted Today") == 0 and tier("Posted Yesterday") in (0, 1) and tier("Posted 5 Days Ago") == 2
    assert tier("Posted 30+ Days Ago") == 3                                  # even though discover saw it for the first time just now
    assert "workday.postedOn" in freshness.TRUSTED


# ------------------------------------------------------------------ the fake Workday
def listing(title, path, loc="London, United Kingdom", posted="Posted Today", req="R1"):
    return {"title": title, "externalPath": path, "locationsText": loc, "postedOn": posted, "bulletFields": [req]}


def detail(title="Data Scientist", html=DESC, loc="London, United Kingdom", country="United Kingdom", req="R1", extra=None):
    info = {"title": title, "jobDescription": html, "location": loc, "country": {"descriptor": country}, "jobReqId": req, "postedOn": "Posted Today",
            "additionalLocations": extra or []}
    return {"jobPostingInfo": info}


class WD:
    """A Workday tenant: `jobs` per search text, a page `size` of 20 (the first page alone carries `total`, as the real endpoint does), details by path."""

    def __init__(self, jobs=None, details=None, robots="", post_reply=None, get_reply=None):
        self.jobs = jobs or {}
        self.details = details or {}
        self.robots, self.post_reply, self.get_reply = robots, post_reply, get_reply
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=None, **kw):
        self.posts.append({"url": url, "json": json, "headers": headers or {}, "allow_redirects": allow_redirects})
        if self.post_reply is not None:
            return self.post_reply(url, json)
        rows = self.jobs.get(json["searchText"], self.jobs.get("*", []))
        page = rows[json["offset"]:json["offset"] + json["limit"]]
        return Resp({"total": len(rows) if json["offset"] == 0 else 0, "jobPostings": page, "facets": []})

    def get(self, url, headers=None, params=None, auth=None, timeout=None, allow_redirects=None, **kw):
        self.gets.append(url)
        if url.endswith("/robots.txt"):
            return Resp(self.robots, 200 if self.robots else 404)
        if self.get_reply is not None:
            return self.get_reply(url)
        for path, d in self.details.items():
            if url.endswith(path):
                return Resp(d)
        return Resp("", 404)

    def pages(self):
        return [p for p in self.gets if not p.endswith("/robots.txt")]


def fetcher(wd, **kw):
    kw.setdefault("gap_s", 0.0)
    return Fetcher(RobotsCache(), get=wd.get, post=wd.post, sleep=kw.pop("sleep", lambda s: None), clock=kw.pop("clock", lambda: 0.0), **kw)


def board(url=URL, name="Acme"):
    return Board("workday", workday.parse_url(url).slug, name, "config")


def many(n, prefix="Data Scientist"):
    return [listing(f"{prefix} {i}", f"/job/London/{prefix.replace(' ', '-')}-{i}_R{i}", req=f"R{i}") for i in range(n)]


# ------------------------------------------------------------------ the request
def test_the_list_request_is_exactly_the_documented_one():
    wd = WD({"*": many(1)}, {"_R0": detail()})
    workday.board_postings(board(), fetcher(wd), queries=["data scientist"], today=TODAY)
    p = wd.posts[0]
    assert p["url"] == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External_Careers/jobs"
    assert p["json"] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "data scientist"}
    assert p["headers"]["Accept"] == "application/json" and "career-ops" in p["headers"]["User-Agent"].lower() or p["headers"].get("User-Agent")


def test_the_myworkdaysite_host_posts_to_its_own_cxs_path():
    wd = WD({"*": []})
    workday.board_postings(board("https://wd5.myworkdaysite.com/recruiting/acme/External"), fetcher(wd), queries=["x"], today=TODAY)
    assert wd.posts[0]["url"] == "https://wd5.myworkdaysite.com/wday/cxs/acme/External/jobs"


def test_pages_follow_the_offset_until_a_short_page():
    wd = WD({"q": many(45)}, {})
    workday.board_postings(board(), fetcher(wd), queries=["q"], keep=lambda p: False, today=TODAY)
    assert [p["json"]["offset"] for p in wd.posts] == [0, 20, 40]


def test_at_most_max_pages_list_requests_per_tenant_per_run():
    wd = WD({"q": many(1000)}, {})
    got = workday.board_postings(board(), fetcher(wd), queries=["q"], keep=lambda p: False, today=TODAY)
    assert len(wd.posts) == 10 and got == []                                  # the default is 10
    wd = WD({"q": many(1000)}, {})
    workday.board_postings(board(), fetcher(wd), queries=["q"], keep=lambda p: False, today=TODAY, max_pages=3)
    assert len(wd.posts) == 3


def test_the_page_budget_is_shared_by_the_queries():
    wd = WD({"a": many(1000), "b": many(1000)}, {})
    workday.board_postings(board(), fetcher(wd), queries=["a", "b"], keep=lambda p: False, today=TODAY, max_pages=4)
    assert len(wd.posts) == 4 and {p["json"]["searchText"] for p in wd.posts} == {"a", "b"}          # "a" does not eat the whole budget


def test_without_queries_the_role_searches_are_used():
    wd = WD({"*": []})
    workday.board_postings(board(), fetcher(wd), today=TODAY)
    assert [p["json"]["searchText"] for p in wd.posts] == list(workday.DEFAULT_QUERIES)


def test_one_request_a_second_per_host():
    sleeps, clock = [], {"t": 0.0}

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s
    wd = WD({"q": many(41)}, {})
    workday.board_postings(board(), fetcher(wd, gap_s=1.0, sleep=sleep, clock=lambda: clock["t"]), queries=["q"], keep=lambda p: False, today=TODAY)
    assert len(wd.posts) == 3 and len([s for s in sleeps if s > 0]) >= 3 and all(s <= 1.0 + 1e-9 for s in sleeps)


# ------------------------------------------------------------------ the postings
def test_a_posting_has_the_description_from_the_detail_the_public_address_and_the_posted_date():
    wd = WD({"*": [listing("Data Scientist", "/job/London/Data-Scientist_R1", posted="Posted 2 Days Ago")]},
            {"/job/London/Data-Scientist_R1": detail(req="R1")})
    (p,) = workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert (p.company, p.title, p.location, p.ext_id) == ("Acme", "Data Scientist", "London, United Kingdom", "R1")
    assert p.url == "https://acme.wd3.myworkdayjobs.com/en-US/External_Careers/job/London/Data-Scientist_R1"
    assert p.posted_at == ago(2) and p.country == "United Kingdom"
    assert "forecasting models" in p.description and "<p>" not in p.description and "Python" in p.description
    assert wd.pages() == ["https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External_Careers/job/London/Data-Scientist_R1"]


def test_the_detail_is_read_only_for_the_postings_that_are_kept():
    wd = WD({"*": [listing("Data Scientist", "/job/L/DS_R1"), listing("Chef", "/job/L/Chef_R2", req="R2")]}, {"_R1": detail(), "_R2": detail("Chef")})
    got = workday.board_postings(board(), fetcher(wd), queries=["x"], keep=lambda p: "Scientist" in p.title, today=TODAY)
    assert [p.title for p in got] == ["Data Scientist"] and len(wd.pages()) == 1


def test_a_posting_that_says_n_locations_is_read_first_and_kept_by_its_real_places():
    rows = [listing("Data Scientist", "/job/L/DS_R1", loc="2 Locations"), listing("ML Engineer", "/job/L/ML_R2", loc="3 Locations", req="R2")]
    d1 = detail(loc="Paris, France", country="France", extra=["London, United Kingdom"])
    d2 = detail("ML Engineer", loc="Paris, France", country="France", req="R2")
    wd = WD({"*": rows}, {"_R1": d1, "_R2": d2})
    uk = lambda p: "United Kingdom" in p.location or p.country == "United Kingdom"             # noqa: E731
    got = workday.board_postings(board(), fetcher(wd), queries=["x"], keep=uk, today=TODAY)
    assert [p.title for p in got] == ["Data Scientist"] and "United Kingdom" in got[0].location


def test_a_posting_is_listed_once_however_many_searches_find_it():
    row = listing("Data Scientist", "/job/L/DS_R1")
    wd = WD({"a": [row], "b": [row]}, {"_R1": detail()})
    got = workday.board_postings(board(), fetcher(wd), queries=["a", "b"], today=TODAY)
    assert len(got) == 1 and len(wd.pages()) == 1


def test_a_missing_or_broken_detail_gives_the_posting_no_description_not_an_error():
    wd = WD({"*": [listing("Data Scientist", "/job/L/DS_R1")]}, {})
    (p,) = workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert p.description == "" and p.title == "Data Scientist"


def test_the_number_of_details_read_in_one_run_is_capped():
    wd = WD({"*": many(30)}, {f"_R{i}": detail(req=f"R{i}") for i in range(30)})
    got = workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY, max_details=5)
    assert len(got) == 30 and len(wd.pages()) == 5 and sum(1 for p in got if p.description) == 5


def test_ats_board_postings_hands_a_workday_board_to_this_module():
    wd = WD({"*": [listing("Data Scientist", "/job/L/DS_R1")]}, {"_R1": detail()})
    got = board_postings(board(), fetcher(wd))
    assert [p.title for p in got] == ["Data Scientist"]


# ------------------------------------------------------------------ outages are transient, never "zero jobs"
@pytest.mark.parametrize("make", [
    lambda url, body: Resp("", 422),
    lambda url, body: Resp("", 302, {"Location": "https://community.workday.com/maintenance-page"}),
    lambda url, body: Resp("", 301, {"Location": "https://community.workday.com/maintenance-page?ref=acme"}),
    lambda url, body: Resp("", 302, {"Location": "https://acme.wd3.myworkdayjobs.com/wday/drs/outage"}),
    lambda url, body: Resp("", 302, {"Location": "/wday/drs/outage"}),
    lambda url, body: Resp("<html><body>We are down for maintenance</body></html>", 200),
    lambda url, body: Resp("", 200),
    lambda url, body: Resp({"unexpected": "shape"}, 200),
])
def test_an_outage_is_transient(make):
    wd = WD(post_reply=make)
    with pytest.raises(workday.Transient) as e:
        workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert "retried next run" in str(e.value) and isinstance(e.value, FetchError)


def test_a_server_error_is_an_error_not_an_empty_list():
    with pytest.raises(FetchError):
        workday.board_postings(board(), fetcher(WD(post_reply=lambda url, body: Resp("", 503))), queries=["x"], today=TODAY)


def test_a_404_says_the_address_is_wrong():
    with pytest.raises(workday.WorkdayError, match="404"):
        workday.board_postings(board(), fetcher(WD(post_reply=lambda url, body: Resp("", 404))), queries=["x"], today=TODAY)


def test_an_outage_half_way_through_throws_away_the_pages_already_read():
    calls = {"n": 0}

    def reply(url, body):
        calls["n"] += 1
        return Resp({"total": 60, "jobPostings": many(20)}) if calls["n"] == 1 else Resp("", 422)
    with pytest.raises(workday.Transient):
        workday.board_postings(board(), fetcher(WD(post_reply=reply)), queries=["q"], keep=lambda p: False, today=TODAY)


def test_a_real_empty_answer_is_zero_jobs():
    wd = WD(post_reply=lambda url, body: Resp({"total": 0, "jobPostings": [], "facets": []}))
    assert workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY) == []


def test_a_blocked_host_is_not_asked_again():
    wd = WD(post_reply=lambda url, body: Resp("", 403))
    f = fetcher(wd)
    with pytest.raises(Blocked):
        workday.board_postings(board(), f, queries=["x"], today=TODAY)
    with pytest.raises(Blocked):
        workday.board_postings(board(), f, queries=["x"], today=TODAY)
    assert len(wd.posts) == 1


# ------------------------------------------------------------------ robots.txt
def test_robots_txt_is_read_before_the_first_request():
    wd = WD({"*": []})
    workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert wd.gets[0] == "https://acme.wd3.myworkdayjobs.com/robots.txt" and len(wd.posts) == 1


def test_a_tenant_that_disallows_wday_cxs_is_skipped_without_a_single_post():
    wd = WD({"*": many(3)}, robots="User-agent: *\nDisallow: /wday/cxs/\n")
    with pytest.raises(Blocked, match="disallow"):
        workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert wd.posts == [] and wd.pages() == []


def test_a_tenant_that_disallows_only_refreshfacet_is_read():
    wd = WD({"*": many(1)}, {"_R0": detail()}, robots="User-agent: *\nDisallow: /refreshFacet/\n")
    assert len(workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)) == 1


def test_the_detail_path_is_checked_too():
    wd = WD({"*": many(1)}, {"_R0": detail()}, robots="User-agent: *\nDisallow: /wday/cxs/acme/External_Careers/job/\n")
    (p,) = workday.board_postings(board(), fetcher(wd), queries=["x"], today=TODAY)
    assert p.description == "" and wd.pages() == []                          # the list was allowed; the posting's page was not read


# ------------------------------------------------------------------ the never-fetch list and the CXS path
def test_the_cxs_path_is_allowed_on_a_never_fetched_host_and_a_page_is_not():
    wd = WD({"*": []})
    f = fetcher(wd, never=applykind.NEVER_FETCH, allow_paths=("/wday/cxs/",))
    workday.board_postings(board(), f, queries=["x"], today=TODAY)
    assert len(wd.posts) == 1
    with pytest.raises(Blocked, match="never"):
        f.request("https://acme.wd3.myworkdayjobs.com/en-US/External_Careers/job/London/X_R1")
    with pytest.raises(Blocked, match="never"):
        fetcher(WD(), never=applykind.NEVER_FETCH).request("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External_Careers/jobs")      # no allow list: refused as before


# ------------------------------------------------------------------ the configuration
def cfg_with(settings, entries):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[], workday=entries)
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[], listed_only={"enabled": False})
    return settings


def test_sources_workday_is_read_as_boards(settings):
    cfg_with(settings, [{"company": "Acme Analytics Ltd", "url": URL}, {"company": "Beta", "url": "https://wd5.myworkdaysite.com/recruiting/beta/Jobs"}])
    boards = [b for b in configured_boards(settings) if b.ats == "workday"]
    assert [(b.name, workday.site_from_slug(b.slug).tenant) for b in boards] == [("Acme Analytics Ltd", "acme"), ("Beta", "beta")]
    assert workday.config_problems(settings) == []


def test_no_workday_key_or_an_empty_list_is_no_boards(settings):
    settings.raw["sources"].pop("workday", None)
    assert [b for b in configured_boards(settings) if b.ats == "workday"] == [] and workday.config_problems(settings) == []
    cfg_with(settings, [])
    assert [b for b in configured_boards(settings) if b.ats == "workday"] == []


def test_an_entry_that_is_not_a_workday_address_is_reported_and_left_out(settings):
    cfg_with(settings, [{"company": "Good", "url": URL}, {"company": "Bad", "url": "https://careers.example.com/jobs"}, {"company": "", "url": URL}, "oops"])
    assert [b.name for b in configured_boards(settings) if b.ats == "workday"] == ["Good"]
    problems = workday.config_problems(settings)
    assert len(problems) == 3 and any("careers.example.com" in p for p in problems) and any("company" in p for p in problems) and any("entry 4" in p for p in problems)


def test_an_entry_may_set_its_own_queries_and_max_pages(settings):
    cfg_with(settings, [{"company": "Acme", "url": URL, "queries": ["nlp", "ai"], "max_pages": 4}])
    (e,) = workday.entries(settings)
    assert (e.queries, e.max_pages) == (("nlp", "ai"), 4)
    cfg_with(settings, [{"company": "Acme", "url": URL}])
    (e,) = workday.entries(settings)
    assert (e.queries, e.max_pages) == (workday.DEFAULT_QUERIES, workday.MAX_PAGES) and workday.MAX_PAGES == 10


# ------------------------------------------------------------------ through discover
def run_discover(settings, db, wd, **kw):
    f = Fetcher(RobotsCache(), get=wd.get, post=wd.post, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, never=applykind.NEVER_FETCH,
                allow_paths=("/wday/cxs/",))
    d = Discover(settings, db, f, today=datetime(2026, 10, 27, 12, tzinfo=timezone.utc), say=lambda s: None, **kw)
    d.from_boards()
    return d


@pytest.fixture
def wd_ok():
    rows = [listing("Data Scientist", "/job/London/Data-Scientist_R1", posted="Posted Today", req="R1"),
            listing("Machine Learning Engineer", "/job/London/ML-Engineer_R2", posted="Posted 30+ Days Ago", req="R2"),
            listing("Barista", "/job/London/Barista_R3", req="R3"),
            listing("Data Scientist", "/job/Paris/Data-Scientist_R4", loc="Paris, France", req="R4")]
    return WD({"*": rows}, {"_R1": detail("Data Scientist"), "_R2": detail("Machine Learning Engineer", req="R2"), "_R3": detail("Barista", req="R3")})


def test_discover_writes_inbox_ads_for_the_matching_uk_postings_only(settings, db, wd_ok):
    cfg_with(settings, [{"company": "Acme", "url": URL, "queries": ["data"]}])
    d = run_discover(settings, db, wd_ok)
    written = {c.title: c for c, _ in d.run.written}
    assert set(written) == {"Data Scientist", "Machine Learning Engineer"}
    ds = written["Data Scientist"]
    assert ds.company == "Acme" and ds.posted == TODAY.isoformat() and ds.posted_source == "workday.postedOn" and ds.apply_kind == "workday"
    assert ds.url == "https://acme.wd3.myworkdayjobs.com/en-US/External_Careers/job/London/Data-Scientist_R1"
    assert freshness.tier({"posted_at": written["Machine Learning Engineer"].posted, "posted_at_source": written["Machine Learning Engineer"].posted_source,
                           "first_seen": "2026-10-27T11:00:00+00:00"}, NOW) == 3
    assert len([g for g in wd_ok.pages()]) == 2                              # the Barista and the Paris job were never read in full


def test_an_outage_is_a_note_nothing_is_cached_and_the_next_run_asks_again(settings, db):
    cfg_with(settings, [{"company": "Acme", "url": URL, "queries": ["data"]}])
    down = WD(post_reply=lambda url, body: Resp("", 422))
    d = run_discover(settings, db, down)
    assert d.run.written == [] and any("retried next run" in n for n in d.run.notes) and not any("0 job" in n for n in d.run.notes)
    cached = list((settings.path("discover") / "boards").glob("workday-*.json")) if (settings.path("discover") / "boards").exists() else []
    assert cached == []
    up = WD({"*": [listing("Data Scientist", "/job/London/DS_R1")]}, {"_R1": detail()})
    d2 = run_discover(settings, db, up)
    assert len(d2.run.written) == 1


def test_a_tenant_whose_robots_forbids_the_api_is_skipped_with_a_note_and_the_others_still_run(settings, db, wd_ok):
    cfg_with(settings, [{"company": "Closed Co", "url": "https://closed.wd1.myworkdayjobs.com/Jobs", "queries": ["x"]},
                        {"company": "Acme", "url": URL, "queries": ["data"]}])

    class Two(WD):
        def get(self, url, **kw):
            if url == "https://closed.wd1.myworkdayjobs.com/robots.txt":
                self.gets.append(url)
                return Resp("User-agent: *\nDisallow: /wday/cxs/\n")
            return super().get(url, **kw)
    wd = Two(wd_ok.jobs, wd_ok.details)
    d = run_discover(settings, db, wd)
    assert any("closed.wd1" in n and "disallow" in n.lower() for n in d.run.notes)
    assert {c.company for c, _ in d.run.written} == {"Acme"} and all("closed.wd1" not in p["url"] for p in wd.posts)


def test_a_bad_entry_is_a_note_in_the_run(settings, db):
    cfg_with(settings, [{"company": "Bad", "url": "https://example.com/x"}])
    d = run_discover(settings, db, WD())
    assert any("sources.workday" in n and "example.com" in n for n in d.run.notes)


def test_the_second_run_inside_the_cache_window_sends_nothing(settings, db, wd_ok):
    cfg_with(settings, [{"company": "Acme", "url": URL, "queries": ["data"]}])
    run_discover(settings, db, wd_ok)
    sent = len(wd_ok.posts)
    run_discover(settings, db, wd_ok)
    assert len(wd_ok.posts) == sent
