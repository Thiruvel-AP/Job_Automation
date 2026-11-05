"""v2.6: more employer boards (discovery/more_ats.py), ported from career-ops providers/ at 20b9fd3.

Single-company ATS providers (Workable, SmartRecruiters, Teamtailor, Recruitee, Personio, Pinpoint, Eploy) read from saved responses: postings with title, URL and place;
a guessed slug must pass the 0.90 board-name check, and Pinpoint / Eploy are never guessed; a 401 / 403 stops a provider for the run; no cookie, session or forged header is
sent; an alert whose link is on one of these boards resolves to the employer's posting; the VC portfolio boards (Getro, Consider) go through the region gate and are
refused (not worked around) when they need a session or robots.txt says no. Fixtures only: no network, no model, not the real settings."""
import json
from datetime import datetime, timezone

import pytest
import requests
import yaml
from conftest import FIX
from netfake import Net, Resp
from test_board_check import cfg, folder, fetcher  # noqa: F401  (cfg: a settings copy with no listed boards and no alert probes)

from careerops import setup
from careerops.discovery import applykind, boards as boards_mod, discover as disc, freshness, health, liveness, more_ats, registry as registry_mod
from careerops.discovery.ats import Board
from careerops.discovery.discover import Discover
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from careerops.discovery.seeds import Seed

DIR = FIX / "more_ats"
TODAY = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
AD = "We need a data scientist who writes clear Python and explains results. " * 12


def text(name):
    return (DIR / name).read_text(encoding="utf-8")


def js(name):
    return json.loads(text(name))


def fx(net=None, post=None, **kw):
    return Fetcher(RobotsCache(), get=(net or Net(strict=False)).get, post=post, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, **kw)


def make(cfg, db, net, post=None, **kw):
    d = Discover(cfg, db, fx(net, post=post), today=TODAY, say=lambda s: None, **kw)
    return d


def inbox(cfg):
    box = cfg.path("jds") / "inbox"
    return sorted(box.glob("*.txt")) if box.exists() else []


# ------------------------------------------------------------------ 1. each provider: a saved response gives postings with title, URL and place
def test_workable_widget_gives_postings_folds_the_fanned_out_places_and_drops_off_domain_urls():
    net = Net(strict=False).add("widget/accounts/acme-analytics", js("workable_widget.json"))
    r = more_ats.fetch("workable", "acme-analytics", fx(net))
    assert r.found and r.name == "Acme Analytics" and net.urls()[0].startswith("https://apply.workable.com/api/v1/widget/accounts/acme-analytics")
    assert [r["params"] for r in net.requests if not r["url"].endswith("/robots.txt")] == [{"details": "true"}]
    by = {p.ext_id: p for p in r.postings}
    assert set(by) == {"ABC123", "DEF456"}                                              # the evil.example.com entry is dropped
    ds = by["ABC123"]
    assert (ds.title, ds.url, ds.posted_at) == ("Data Scientist", "https://apply.workable.com/acme-analytics/j/ABC123/", "2026-10-28")
    assert ds.location == "London, United Kingdom · Berlin, Germany" and len(ds.description) > 400      # the London and Berlin entries are one posting


def test_workable_unknown_account_is_not_a_board():
    net = Net(strict=False)                                                              # an unrouted URL is a 404
    assert more_ats.fetch("workable", "nobody-here", fx(net)).found is False


def test_smartrecruiters_lists_postings_and_reads_the_text_of_the_ones_worth_it():
    net = Net(strict=False).add("postings/743999000111", js("smartrecruiters_detail.json")).add("companies/acme-analytics/postings", js("smartrecruiters_postings.json"))
    f = fx(net)
    r = more_ats.fetch("smartrecruiters", "acme-analytics", f)
    assert r.found and r.name == "Acme Analytics" and r.detail and all(p.description == "" for p in r.postings)
    board = Board("smartrecruiters", "acme-analytics", "Acme Analytics", "config")
    posts = more_ats.board_postings(board, f, keep=lambda p: p.location.startswith("London"))
    london, paris = posts
    assert london.url == "https://jobs.smartrecruiters.com/acme-analytics/743999000111-data-scientist"          # the public page, not the API ref
    assert london.location == "London, England, United Kingdom" and london.posted_at == "2026-10-27T09:00:00.000Z"
    assert "Acme Analytics makes forecasting tools" in london.description and "Python" in london.description
    assert paris.description == ""                                                       # not in a place worth reading: no detail request
    assert not [u for u in net.urls() if u.endswith("743999000222")]


def test_smartrecruiters_empty_company_is_not_a_board():
    net = Net(strict=False).add("companies/ghost/postings", {"content": [], "totalFound": 0})
    assert more_ats.fetch("smartrecruiters", "ghost", fx(net)).found is False


def test_teamtailor_rss_gives_postings_and_the_channel_title_as_the_name():
    net = Net(strict=False).add("acme-analytics.teamtailor.com/jobs.rss", text("teamtailor.rss"))
    r = more_ats.fetch("teamtailor", "acme-analytics", fx(net))
    assert r.found and r.name == "Acme Analytics"
    ds, remote = r.postings
    assert (ds.title, ds.url, ds.location, ds.ext_id) == ("Data Scientist", "https://acme-analytics.teamtailor.com/jobs/5001234-data-scientist", "London, United Kingdom", "5001234")
    assert ds.posted_at == "2026-10-27T09:00:00+00:00" and len(ds.description) > 400 and remote.location == "Remote"


def test_recruitee_offers_give_postings_and_skip_the_sample():
    net = Net(strict=False).add("acme-analytics.recruitee.com/api/offers", js("recruitee_offers.json"))
    r = more_ats.fetch("recruitee", "acme-analytics", fx(net))
    assert r.found and r.name == "Acme Analytics"
    (p,) = r.postings
    assert (p.title, p.url, p.location, p.posted_at) == ("Data Scientist", "https://acme-analytics.recruitee.com/o/data-scientist", "London, United Kingdom", "2026-10-27T09:00:00+00:00")
    assert "Python" in p.description


def test_personio_xml_gives_postings_and_is_found_on_the_com_host_when_the_de_host_has_none():
    net = Net(strict=False).add("acme-analytics.jobs.personio.com/xml", text("personio.xml"))
    r = more_ats.fetch("personio", "acme-analytics", fx(net))
    assert r.found and r.name == "Acme Analytics" and r.slug == "acme-analytics.jobs.personio.com"
    ds, ml = r.postings
    assert (ds.title, ds.url, ds.location) == ("Data Scientist", "https://acme-analytics.jobs.personio.com/job/8800001", "London, Remote")
    assert ds.posted_at == "2026-10-27T09:00:00+00:00" and "Your role" in ds.description and ml.location == "Munich"
    assert net.urls()[0].startswith("https://acme-analytics.jobs.personio.de/xml")       # .de first, then .com


def test_pinpoint_feed_gives_postings_without_a_name_and_drops_the_demo_posting():
    net = Net(strict=False).add("acme-analytics.pinpointhq.com/postings.json", js("pinpoint_postings.json"))
    r = more_ats.fetch("pinpoint", "acme-analytics", fx(net))
    assert r.found and r.name == ""                                                      # the feed names no company
    (p,) = r.postings
    assert (p.title, p.location) == ("Data Scientist", "London, UK") and p.url.endswith("9f0c1d2e-aaaa-bbbb-cccc-1234567890ab") and "Build models" in p.description


def test_eploy_sitemap_gives_postings_and_the_vacancy_page_gives_place_and_text():
    net = Net(strict=False).add("vacancies/1234/", text("eploy_vacancy.html")).add("jobs.example-university.ac.uk/live-jobs.xml", text("eploy_live_jobs.xml"))
    f = fx(net)
    board = Board("eploy", "jobs.example-university.ac.uk", "Example University", "config")
    posts = more_ats.board_postings(board, f, keep=lambda p: "scientist" in p.title.lower())
    assert [p.ext_id for p in posts] == ["1234", "1235"]                                 # the other host and the non-vacancy page are dropped
    ds, lecturer = posts
    assert ds.location == "Leeds, West Yorkshire, United Kingdom" and ds.title == "Data Scientist (Research Computing)" and ds.posted_at == "2026-10-26" and "Python" in ds.description
    assert lecturer.title == "Lecturer In History" and lecturer.location == "" and lecturer.description == ""


def test_a_slug_that_is_not_a_name_is_never_put_into_an_address():
    with pytest.raises(Exception, match="not a board name"):
        more_ats.endpoint("workable", "../../etc")


def test_nothing_forged_is_sent_no_cookie_no_referer_no_origin_no_browser_agent():
    net = Net(strict=False).add("widget/accounts/acme-analytics", js("workable_widget.json")).add("acme-analytics.teamtailor.com", text("teamtailor.rss"))
    f = fx(net)
    more_ats.fetch("workable", "acme-analytics", f)
    more_ats.fetch("teamtailor", "acme-analytics", f)
    for r in net.requests:
        names = {k.lower() for k in r["headers"]}
        assert not names & {"cookie", "referer", "origin", "x-csrf-token", "authorization"}, r
        assert r["headers"]["User-Agent"].startswith("career-ops/")


# ------------------------------------------------------------------ 2. guessing a slug: the name check, and what is never guessed
def seed(name="Acme Analytics"):
    return Seed(name, "archive")


def test_a_guessed_workable_slug_whose_board_is_another_company_is_rejected_even_when_it_spells_the_whole_name(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    other = dict(js("workable_widget.json"), name="Globex Inc")
    net = Net(strict=False).add("widget/accounts/acmeanalytics", other)
    f = boards_mod.find(seed(), fx(net), boards_mod.BoardIndex(cfg))
    assert f.boards == [] and [(b.ats, b.slug) for b, _ in f.unverified] == [("workable", "acmeanalytics")]
    assert "called 'Globex Inc', not 'Acme Analytics'" in f.unverified[0][1] and "0.90" in f.unverified[0][1]


def test_a_guessed_slug_whose_board_name_matches_is_accepted(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "recruitee"]
    net = Net(strict=False).add("widget/accounts/acmeanalytics", js("workable_widget.json")).add("acmeanalytics.recruitee.com/api/offers", js("recruitee_offers.json"))
    f = boards_mod.find(seed("Acme Analytics Ltd"), fx(net), boards_mod.BoardIndex(cfg))
    assert sorted((b.ats, b.slug) for b in f.boards) == [("workable", "acmeanalytics")] and f.unverified == []
    assert not [u for u in net.urls(True) if "recruitee.com" in u]                            # v3.0: Recruitee (one host per company) is never guessed


def test_a_provider_whose_reply_has_no_name_cannot_vouch_for_a_guess(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    body = js("workable_widget.json")
    body.pop("name")
    net = Net(strict=False).add("widget/accounts/acmeanalytics", body)
    f = boards_mod.find(seed(), fx(net), boards_mod.BoardIndex(cfg))
    assert f.boards == [] and "gives no company name" in f.unverified[0][1]


def test_pinpoint_and_eploy_are_never_guessed_even_when_the_settings_name_them(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "pinpoint", "eploy"]
    index = boards_mod.BoardIndex(cfg)
    assert index.more == ("workable",)
    net = Net(strict=False)
    boards_mod.find(seed(), fx(net), index)
    assert net.urls() and not [u for u in net.urls(True) if "pinpointhq" in u or "live-jobs" in u]
    assert any("pinpoint" in p and "never guessed" in p for p in more_ats.config_problems(cfg))
    assert more_ats.LINK_ONLY == ("personio", "pinpoint") and more_ats.CONFIG_ONLY == ("eploy",) and not set(more_ats.NAME_CHECKED) & {"personio", "pinpoint", "eploy"}


def test_a_host_that_does_not_resolve_is_never_asked_because_a_company_per_host_provider_is_not_guessed(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["teamtailor"]                                    # v3.0: an unknown name there is a host that does not exist
    net = Net(strict=False).add("teamtailor.com", lambda url, **kw: requests.ConnectionError("no such host"))
    index = boards_mod.BoardIndex(cfg)
    f = boards_mod.find(seed(), fx(net), index)
    assert f.boards == [] and f.errors == [] and index.get("teamtailor", "acmeanalytics") is None and not [u for u in net.urls(True) if "teamtailor" in u]
    assert index.more == () and index.not_guessed == ["teamtailor"] and any("no longer guessed" in n and "teamtailor" in n for n in index.notes())


def test_an_unknown_company_whose_host_answers_with_a_web_page_cannot_happen_because_that_host_is_not_asked(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["recruitee"]
    net = Net(strict=False).add("recruitee.com/api/offers", Resp("<html>You are being redirected.</html>"))
    index = boards_mod.BoardIndex(cfg)
    f = boards_mod.find(seed(), fx(net), index)
    assert f.boards == [] and f.errors == [] and not [u for u in net.urls(True) if "recruitee" in u]


def test_a_401_or_403_stops_the_provider_for_the_rest_of_the_run(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    net = Net(strict=False).add("apply.workable.com/api/v1/widget/accounts/alphalabs", Resp("denied", 403))
    index, f = boards_mod.BoardIndex(cfg), fx(net)
    first = boards_mod.find(seed("Alpha Labs"), f, index)
    second = boards_mod.find(seed("Beta Labs"), f, index)
    assert "workable" in index.stopped and "403" in index.stopped["workable"]
    assert first.boards == [] and second.boards == [] and first.errors == [] and second.errors == []
    assert not [u for u in net.urls(True) if "workable" in u and ("betalabs" in u or "beta-labs" in u)]   # the provider was not asked again, whatever the slug
    assert any("workable stopped for this run" in n for n in index.notes())


def test_a_robots_txt_that_says_no_is_counted_and_not_worked_around(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["smartrecruiters"]
    net = Net(strict=False).add("api.smartrecruiters.com/robots.txt", "User-agent: *\nDisallow: /\n")
    index = boards_mod.BoardIndex(cfg)
    f = boards_mod.find(seed(), fx(net), index)
    assert f.boards == [] and f.errors == [] and index.refused["smartrecruiters"][0] >= 1 and "smartrecruiters" not in index.stopped
    assert not [u for u in net.urls() if "/v1/companies/" in u]                           # not one request to the API itself


# ------------------------------------------------------------------ 3. discover --ats: a guessed board is read, and the report says what it changed
def test_discover_ats_reads_a_guessed_workable_board_and_reports_board_none_before_and_after(cfg, db):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    folder(cfg, "Acme Analytics")
    folder(cfg, "Nowhere Ltd")
    d = make(cfg, db, Net(strict=False).add("widget/accounts/acmeanalytics", js("workable_widget.json")))
    d.from_boards()
    assert [c.title for c, _ in d.run.written] == ["Data Scientist"]                      # the New York ML role is outside the UK gate
    assert d.run.written[0][0].apply_kind == "employer_other" and d.run.written[0][0].board == "workable:acmeanalytics"
    assert d.run.written[0][0].posted_source == "workable.published_on" and "workable.published_on" in freshness.TRUSTED
    line = d.ats_tally()[0]
    assert line.startswith("board: none  2 of 2 companies before the v2.6 providers") and line.endswith("-> 1 of 2 after")
    assert d.run.sources["ats"]["board_none_before"] == 2 and d.run.sources["ats"]["board_none_after"] == 1
    assert "workable/acmeanalytics" in "\n".join(d.ats_report())


def test_the_run_record_carries_the_counts_so_the_report_can_print_them_later():
    ats = {"companies": 267, "board_none_before": 219, "board_none_after": 190}
    assert disc.ats_none_lines(ats) == [f"board: none  219 of 267 companies before the v2.6 providers (Workable, SmartRecruiters, Teamtailor, Recruitee, Personio) -> 190 of 267 after"]
    assert disc.ats_none_lines({}) == []


# ------------------------------------------------------------------ 4. an alert whose link is on one of these boards resolves to the employer's posting
def test_an_alert_linking_to_apply_workable_com_resolves_to_the_employers_posting(cfg, db):
    net = Net(strict=False).add("widget/accounts/acme-analytics", js("workable_widget.json"))
    d = make(cfg, db, net)
    out = d.resolve_link("gmail", "indeed", "Acme Analytics", "Data Scientist", "London", "https://apply.workable.com/acme-analytics/j/ABC123/", "gmail:indeed", source_id="indeed-uk")
    assert out == "written"
    (path,) = inbox(cfg)
    body = path.read_text(encoding="utf-8")
    assert "URL: https://apply.workable.com/acme-analytics/j/ABC123/" in body and "data scientist who writes clear Python" in body
    c = d.run.sources["gmail"]
    assert c["resolved"] == 1 and c["resolved_via_board_link"] == 1 and c["resolved_new_provider"] == 1
    line = disc.resolution_lines(d.run.by_source, "t")[1]
    assert "indeed-uk" in line and "1 of 1 (100%)" in line and "before the v2.6 providers 0 of 1 (0%)" in line
    assert "board_link 1" in disc.step_lines(d.run.sources, "s")[0]


def test_a_pinpoint_link_resolves_although_a_pinpoint_slug_is_never_guessed(cfg, db):
    net = Net(strict=False).add("acme-analytics.pinpointhq.com/postings.json", js("pinpoint_postings.json"))
    d = make(cfg, db, net)
    out = d.resolve_link("gmail", "indeed", "Acme Analytics", "Data Scientist", "London",
                         "https://acme-analytics.pinpointhq.com/en/postings/9f0c1d2e-aaaa-bbbb-cccc-1234567890ab", "gmail:indeed")
    assert out == "written" and d.run.sources["gmail"]["resolved_via_board_link"] == 1


def test_a_link_to_a_posting_that_is_no_longer_on_the_board_is_queued_not_invented(cfg, db):
    net = Net(strict=False).add("widget/accounts/acme-analytics", js("workable_widget.json"))
    d = make(cfg, db, net)
    out = d.resolve_link("gmail", "indeed", "Acme Analytics", "Data Scientist", "London", "https://apply.workable.com/acme-analytics/j/GONE99/", "gmail:indeed")
    d.finish()
    assert out == "queued" and inbox(cfg) == []


def test_a_linkedin_link_names_no_board_and_nothing_is_requested_from_it(cfg, db):
    assert more_ats.link_board("https://www.linkedin.com/jobs/view/4479026825") is None
    net = Net()                                                                           # strict: any request fails the test
    d = make(cfg, db, net)
    assert d.resolve_link("gmail", "linkedin", "", "Data Scientist", "London", "https://www.linkedin.com/jobs/view/4479026825", "gmail:linkedin") == "queued"
    assert net.requests == []


def test_a_workday_link_is_resolved_through_the_public_list_api_only(cfg, db):
    row = {"title": "Data Scientist", "externalPath": "/job/London/Data-Scientist_R123", "locationsText": "London, United Kingdom", "postedOn": "Posted Today", "bulletFields": ["R123"]}
    detail = {"jobPostingInfo": {"jobDescription": f"<p>{AD}</p>", "jobReqId": "R123", "location": "London, United Kingdom", "country": {"descriptor": "United Kingdom"},
                                 "externalUrl": "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/London/Data-Scientist_R123"}}
    net = Net(strict=False).add("/wday/cxs/acme/Ext/job/London/Data-Scientist_R123", detail)
    posted = []

    def post(url, json=None, **kw):
        posted.append(url)
        return Resp({"jobPostings": [row], "total": 1})
    f = fx(net, post=post, never=applykind.NEVER_FETCH, allow_paths=("/wday/cxs/",))
    d = Discover(cfg, db, f, today=TODAY, say=lambda s: None)
    out = d.resolve_link("gmail", "indeed", "Acme", "Data Scientist", "London", "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/London/Data-Scientist_R123", "gmail:indeed")
    assert out == "written" and d.run.sources["gmail"]["resolved_via_board_link"] == 1
    assert all("/wday/cxs/" in u for u in posted) and all(u.endswith("/robots.txt") or "/wday/cxs/" in u for u in net.urls(True) if "myworkday" in u)


# ------------------------------------------------------------------ 5. liveness: the employer's board says whether the posting is still listed
def test_liveness_reads_the_board_of_a_workable_link():
    net = Net(strict=False).add("widget/accounts/acme-analytics", js("workable_widget.json"))
    f = fx(net)
    assert liveness.check("https://apply.workable.com/acme-analytics/j/ABC123/", f).status == liveness.OPEN
    gone = liveness.check("https://apply.workable.com/acme-analytics/j/ZZZ999/", f)
    assert gone.status == liveness.CLOSED and "not on the workable board" in gone.reason
    assert not [u for u in net.urls() if "/j/" in u]                                      # the board answered; the posting page was not fetched


def test_liveness_falls_back_to_the_page_when_the_board_cannot_answer():
    net = Net(strict=False).add("widget/accounts/acme-analytics", Resp("denied", 429)).add("apply.workable.com/acme-analytics/j/ABC123", "<html><body>x</body></html>")
    r = liveness.check("https://apply.workable.com/acme-analytics/j/ABC123/", fx(net))
    assert r.status == liveness.UNCERTAIN


# ------------------------------------------------------------------ 6. VC portfolio boards (multi-employer)
GH_ACME = {"jobs": [{"id": 9001, "title": "Data Scientist", "location": {"name": "London, United Kingdom"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/9001",
                     "content": f"<p>{AD}</p>", "first_published": "2026-10-20T09:00:00Z"}]}
VC_SOURCE = {"sources": [{"id": "vc-fund", "name": "Fund", "url": "https://jobs.fund.example/jobs", "country": "multi", "method": "getro", "regions": ["any"]}]}


def getro_post(sent):
    def post(url, json=None, headers=None, **kw):
        sent.append({"url": url, "json": json, "headers": headers or {}})
        return Resp(js("getro_search.json"))
    return post


def test_getro_reads_the_collection_id_from_the_page_and_names_the_portfolio_company():
    net = Net(strict=False).add("jobs.fund.example/jobs", text("getro_board.html"))
    sent = []
    posts = more_ats.vc_read("getro", "https://jobs.fund.example/jobs", "", fx(net, post=getro_post(sent)))
    assert [(p.company, p.title, p.location) for p in posts] == [("Acme Analytics", "Data Scientist", "London, United Kingdom"), ("Globex", "Data Scientist", "New York, NY, United States")]
    assert sent[0]["url"] == "https://api.getro.com/api/v2/collections/4242/search/jobs" and sent[0]["json"]["hitsPerPage"] == 20
    assert not {k.lower() for k in sent[0]["headers"]} & {"cookie", "referer", "origin"}


def test_getro_api_that_disallows_scripts_is_refused_and_nothing_is_sent():
    net = Net(strict=False).add("api.getro.com/robots.txt", "User-agent: *\nDisallow: /\n")
    sent = []
    with pytest.raises(Blocked, match="robots.txt"):
        more_ats.vc_read("getro", "https://jobs.fund.example/jobs", "4242", fx(net, post=getro_post(sent)))
    assert sent == []


def test_consider_that_needs_a_session_is_stopped_not_worked_around():
    net = Net(strict=False).add("jobs.fund.example/jobs", text("consider_board.html"))
    sent = []

    def post(url, json=None, headers=None, **kw):
        sent.append({"url": url, "json": json, "headers": headers or {}})
        return Resp({"error": "INVALID_CSRF"}, 412)
    with pytest.raises(more_ats.SessionRequired, match="no cookie or session"):
        more_ats.vc_read("consider", "https://jobs.fund.example/jobs", "", fx(net, post=post))
    assert sent[0]["json"]["board"] == {"id": "fund-capital", "isParent": True} and not {k.lower() for k in sent[0]["headers"]} & {"cookie", "x-csrf-token", "referer", "origin"}


def test_a_non_uk_job_from_a_vc_board_is_parked_by_the_region_gate_and_the_uk_one_is_resolved_through_its_link(cfg, db):
    net = Net(strict=False).add("boards-api.greenhouse.io/v1/boards/acme/jobs", GH_ACME).add("jobs.fund.example/jobs", text("getro_board.html"))
    sent = []
    d = make(cfg, db, net, post=getro_post(sent), regions=("uk",))
    d.registry = registry_mod.parse(VC_SOURCE)
    d.from_vc("ats")
    c = d.run.sources["ats"]
    assert c["seen"] == 2 and c["place_filtered"] == 1 and c["resolved"] == 1                # Globex (New York) is outside the UK region
    (path,) = inbox(cfg)
    assert "Company: Acme Analytics" in path.read_text(encoding="utf-8") and "globex" not in path.name
    assert d.run.by_source["vc-fund"]["new"] == 1                                             # credited to the VC board in the health table


def test_a_vc_board_that_cannot_be_read_is_a_note_and_the_run_goes_on(cfg, db):
    net = Net(strict=False).add("api.getro.com/robots.txt", "User-agent: *\nDisallow: /\n").add("jobs.fund.example/jobs", text("getro_board.html"))
    d = make(cfg, db, net)
    d.registry = registry_mod.parse(VC_SOURCE)
    d.from_vc("ats")
    assert any("vc-fund" in n and "robots.txt" in n for n in d.run.notes) and inbox(cfg) == []


# ------------------------------------------------------------------ 7. sources.yaml entries, health rows, doctor
def entries():
    return registry_mod.parse(yaml.safe_load("sources:\n" + "\n".join(more_ats.SOURCES_YAML)))


def test_the_printed_sources_yaml_entries_load_and_carry_regions_and_the_verified_reasons():
    reg = entries()
    assert [s.id for s in reg] == ["workable-boards", "smartrecruiters-boards", "teamtailor-boards", "pinpoint-boards", "recruitee-boards", "personio-boards",
                                   "workday-boards", "vc-atomico", "vc-balderton", "vc-notion"]
    assert all(s.regions == ("any",) for s in reg) and reg.get("pinpoint-boards").note.startswith("link-detected only")
    off = {s.id for s in reg if not s.enabled}
    assert off == {"smartrecruiters-boards", "vc-atomico", "vc-balderton", "vc-notion"}
    assert "Disallow" in reg.get("vc-atomico").note and "INVALID_CSRF" in reg.get("vc-balderton").note and "INVALID_CSRF" in reg.get("vc-notion").note
    assert more_ats.missing_source_lines(reg) == []
    assert len(more_ats.missing_source_lines(registry_mod.Registry())) == 10


def test_health_rows_for_the_new_sources(cfg, db):
    reg = entries()
    on = registry_mod.Registry([registry_mod.Source(**{**s.__dict__, "enabled": True}) if s.id in ("smartrecruiters-boards", "vc-balderton") else s for s in reg.sources])
    net = (Net(strict=False).add("api.smartrecruiters.com/robots.txt", "User-agent: *\nDisallow: /\n")
           .add("careers.balderton.com/api-boards/search-jobs", Resp({"error": "INVALID_CSRF"}, 412)).add("careers.balderton.com/jobs", text("consider_board.html")))
    rows = {r.source.id: r for r in health.check_all(cfg, on, fx(net, post=lambda url, **kw: Resp({"error": "INVALID_CSRF"}, 412)))}
    assert rows["workable-boards"].status == "ok" and rows["workable-boards"].reachable == "yes"
    assert rows["smartrecruiters-boards"].status == "blocked" and "robots.txt" in rows["smartrecruiters-boards"].detail
    assert rows["vc-balderton"].status == "blocked" and "session" in rows["vc-balderton"].detail
    assert rows["teamtailor-boards"].status == "ok" and "one host per company" in rows["teamtailor-boards"].detail
    assert rows["pinpoint-boards"].status == "ok" and "never guessed" in rows["pinpoint-boards"].detail
    assert rows["vc-atomico"].status == "disabled"


def test_doctor_prints_the_lines_to_paste_and_warns_about_a_bad_setting(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "nonsense"]
    cfg.raw["sources"]["eploy"] = [{"company": "X"}]
    checks = setup.provider_checks(cfg)
    names = [c[0] for c in checks]
    assert any("employer-board settings (2 problem(s))" in n for n in names)
    paste = next(c for c in checks if "are not in sources.yaml" in c[0])
    assert paste[1] == setup.WARN and "id: workable-boards" in paste[2] and "never edits sources.yaml" in paste[2]


def test_the_older_boards_keep_their_kinds_and_the_new_ones_apply_on_the_employers_page():
    assert applykind.board_kind("lever_eu") == "lever" and applykind.board_kind("greenhouse") == "greenhouse"
    assert all(applykind.board_kind(a) == "employer_other" for a in more_ats.SINGLE)
