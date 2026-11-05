"""v3.0: is a posting still open? The ATS's own public API first (Greenhouse, Lever, Ashby, Workday), the page only when the API cannot say. No network: `Net` answers."""
from netfake import Net, Resp
from test_board_check import cfg, fetcher as fx  # noqa: F401

from careerops.discovery import liveness, liveness_api, pretailor


# ------------------------------------------------------------------ liveness by the ATS's own API
def test_greenhouse_lever_and_ashby_postings_are_judged_by_their_api():
    net = (Net(strict=False).add("boards-api.greenhouse.io/v1/boards/acme/jobs/123", {"id": 123, "title": "DS"})
           .add("api.lever.co/v0/postings/acme/0a1b2c3d-1111-2222-3333-444455556666", {"id": "0a1b2c3d-1111-2222-3333-444455556666"}))
    f = fx(net)
    assert liveness_api.check("https://boards.greenhouse.io/acme/jobs/123", f)[0] == "open"
    assert liveness_api.check("https://job-boards.greenhouse.io/acme/jobs/999", f) == ("closed", "not on the Greenhouse board of acme any more (HTTP 404)")
    assert liveness_api.check("https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666", f)[0] == "open"
    assert liveness_api.check("https://jobs.lever.co/acme/ffffffff-1111-2222-3333-444455556666", f)[0] == "closed"
    assert liveness_api.check("https://jobs.eu.lever.co/acme/ffffffff-1111-2222-3333-444455556666", f)[0] == "closed"
    assert any("api.eu.lever.co" in u for u in net.urls())


def test_an_ashby_posting_is_open_when_the_board_lists_it_and_closed_when_it_does_not():
    jid = "0a1b2c3d-1111-2222-3333-444455556666"
    board = {"jobs": [{"id": jid, "title": "DS", "isListed": True}, {"id": "ffffffff-1111-2222-3333-444455556666", "isListed": False}]}
    net = Net(strict=False).add("api.ashbyhq.com/posting-api/job-board/acme", board)
    f = fx(net)
    assert liveness_api.check(f"https://jobs.ashbyhq.com/acme/{jid}", f)[0] == "open"
    assert liveness_api.check("https://jobs.ashbyhq.com/acme/ffffffff-1111-2222-3333-444455556666", f)[0] == "closed"      # on the board but not listed
    assert liveness_api.check("https://jobs.ashbyhq.com/acme/eeeeeeee-1111-2222-3333-444455556666", f)[0] == "closed"
    assert liveness_api.check(f"https://jobs.ashbyhq.com/nosuch/{jid}", f) is None                                          # a board that cannot be read says nothing


def test_a_workday_posting_is_judged_by_its_public_detail_api():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/London/Data-Scientist_R1"
    net = Net(strict=False).add("/wday/cxs/acme/Ext/job/London/Data-Scientist_R1", {"jobPostingInfo": {"title": "Data Scientist"}})
    f = fx(net)
    assert liveness_api.check(url, f)[0] == "open"
    assert liveness_api.check(url.replace("Data-Scientist_R1", "Gone_R2"), f) == ("closed", "not on the Workday site of acme any more (HTTP 404)")
    assert all("/wday/cxs/" in u for u in net.urls())                               # the careers PAGE of the tenant is never requested


def test_when_the_api_cannot_say_the_answer_is_none_and_the_page_check_goes_on():
    url = "https://boards.greenhouse.io/acme/jobs/123"
    assert liveness_api.check(url, fx(Net(strict=False).add("boards-api.greenhouse.io", Resp("", 500)))) is None
    assert liveness_api.check(url, fx(Net(strict=False).add("boards-api.greenhouse.io", Resp("", 403)))) is None            # never worked around
    assert liveness_api.check(url, fx(Net(strict=False).add("boards-api.greenhouse.io", Resp("<html>", 200)))) is None
    assert liveness_api.check("https://careers.acme.com/job?gh_jid=123", fx(Net())) is None                                # an employer-hosted link: not ours to read
    assert liveness_api.check("https://boards.greenhouse.io/acme", fx(Net())) is None                                      # no posting id
    assert liveness_api.check("", fx(Net())) is None


def test_the_liveness_check_asks_the_api_first_and_then_remembers_the_answer(cfg):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    net = Net().add("boards-api.greenhouse.io/v1/boards/acme/jobs/123", {"id": 123})
    cache = liveness.Cache(None)
    r = liveness.check(url, fx(net), cache)
    assert r.status == "open" and "Greenhouse board of acme" in r.reason and net.urls() == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"]
    again = liveness.check(url, fx(net), cache)
    assert again.cached and len(net.urls()) == 1
    closed = liveness.check("https://boards.greenhouse.io/acme/jobs/9", fx(Net(strict=False)), cache)
    assert closed.status == "closed"


def test_other_pages_are_still_judged_by_the_page_and_a_workday_page_is_never_read():
    page = "<html><title>Data Scientist</title><body>Data Scientist at Acme. Apply now <a href='/apply'>Apply</a></body></html>"
    net = Net().add("careers.acme.com/jobs/1", page)
    assert liveness.check("https://careers.acme.com/jobs/1", fx(net)).status in ("open", "uncertain")
    assert net.urls() == ["https://careers.acme.com/jobs/1"]
    f = fx(Net(strict=False))
    f.never = ("myworkdayjobs.com",)
    r = liveness.check("https://acme.wd5.myworkdayjobs.com/en-US/Ext", f)                       # a careers page of a Workday tenant, not a posting
    assert r.status == "uncertain" and "never fetched" in r.reason


def test_the_default_fetcher_may_ask_workday_s_api_but_not_its_pages(cfg, monkeypatch):
    monkeypatch.undo()                                                              # conftest swaps default_fetcher for a network-less one; this test wants the real constructor
    f = pretailor.default_fetcher(cfg)
    assert f.allow_paths == ("/wday/cxs/",) and "myworkdayjobs.com" in f.never
