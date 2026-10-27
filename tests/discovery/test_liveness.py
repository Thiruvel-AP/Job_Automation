"""Is a posting still open? (discovery/liveness.py) closed = HTTP 404/410, a redirect to a generic careers or search page, a page with no job title and no
apply path, or an expired phrase; open; uncertain (proceeds with a flag). Phrases are normalised first (curly quotes, accents, whitespace). LinkedIn, Indeed
and CV-Library are never fetched; robots.txt and one request a second per host are the Fetcher's. A 6 hour cache. No network: a fake `get`."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from careerops.discovery import applykind, liveness
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache

JOB_PAGE = "<html><head><title>Data Scientist - Acme</title></head><body><h1>Data Scientist</h1><p>{body}</p><a href='/apply'>Apply now</a></body></html>"
LONG = "We build forecasting tools for retailers and need a data scientist who ships models. " * 5


# ------------------------------------------------------------------ the phrases
@pytest.mark.parametrize("text", [
    "This job is no longer available",
    "Sorry, this role is no longer available.",
    "We are no longer accepting applications for this position",
    "We’re no longer accepting applications",                                      # curly apostrophe
    "THIS JOB HAS EXPIRED",
    "This job has expired",
    "The job posting has expired.",
    "Thïs job has expîred",                                                      # accents are stripped
    "This position is no longer open",
    "This role is closed",
    "this job is closed.",
    "This role is no longer needed",
    "Unfortunately this position has been filled",
    "This vacancy has been filled.",
    "The role has now been filled",
    "The job has already been filled",
    "no   longer\n  available",                                                      # whitespace collapsed
    "This\u00a0job is no longer available",                                     # a no-break space
])
def test_an_expired_phrase_is_found(text):
    assert liveness.expired_phrase(text), text


@pytest.mark.parametrize("text", [
    "We use a closed-loop feedback system",
    "This role is closed-loop experimentation",                                      # a compound word, not a closed job
    "This role is closed_loop",
    "Your application has been filled out",
    "The application form has been filled in",
    "Once the form has been filled up we reply",
    "The application has been filled out and sent",
    "Data Scientist, closed beta products",
    "You will own a role that is key to the business",
    "Apply today: this job is open until 30 November",
    "",
])
def test_these_are_not_expired(text):
    assert liveness.expired_phrase(text) is None, text


def test_normalise_straightens_quotes_strips_accents_and_collapses_whitespace():
    assert liveness.normalise("  Wé’re   NO longer\nAccepting “applications” ") == 'we\'re no longer accepting "applications"'


# ------------------------------------------------------------------ the judgement (no network)
def judge(url="https://acme.com/careers/jobs/data-scientist-12345", final=None, status=200, html=""):
    return liveness.judge(url, final or url, status, html)


@pytest.mark.parametrize("status", [404, 410])
def test_http_404_and_410_are_closed(status):
    r = judge(status=status)
    assert r.status == liveness.CLOSED and str(status) in r.reason


def test_other_errors_are_uncertain():
    for status in (400, 405, 451):
        assert judge(status=status).status == liveness.UNCERTAIN


@pytest.mark.parametrize("requested,final", [
    ("https://boards.greenhouse.io/acme/jobs/4567890", "https://boards.greenhouse.io/acme?error=true"),         # Greenhouse sends a closed job to its board
    ("https://acme.com/careers/jobs/data-scientist-12345", "https://acme.com/careers"),
    ("https://acme.com/careers/jobs/data-scientist-12345", "https://acme.com/careers/"),
    ("https://acme.com/careers/jobs/data-scientist-12345", "https://acme.com/"),
    ("https://acme.com/jobs/view/998877", "https://acme.com/jobs/search?q=data+scientist"),
    ("https://acme.com/en-gb/jobs/998877", "https://acme.com/en-gb/jobs"),
    ("https://jobs.acme.com/posting/ab12cd34-1111-2222-3333-444455556666", "https://jobs.acme.com/search"),
    ("https://acme.com/vacancy/data-scientist-london-1234", "https://acme.com/vacancies"),
])
def test_a_redirect_to_a_generic_careers_or_search_page_is_closed(requested, final):
    r = liveness.judge(requested, final, 200, JOB_PAGE.format(body=LONG))              # even though the page it lands on has a title and an Apply link
    assert r.status == liveness.CLOSED and "redirect" in r.reason


@pytest.mark.parametrize("requested,final", [
    ("http://acme.com/careers/jobs/data-scientist-12345", "https://acme.com/careers/jobs/data-scientist-12345"),       # http -> https
    ("https://acme.com/careers/jobs/data-scientist-12345", "https://www.acme.com/careers/jobs/data-scientist-12345"),
    ("https://acme.com/careers/jobs/data-scientist-12345", "https://acme.com/en/careers/jobs/data-scientist-12345"),   # still a posting
    ("https://acme.com/jobs/12345?utm=x", "https://acme.com/jobs/12345"),
    ("https://acme.com/jobs/12345", "https://acme.com/jobs/12345/"),
    ("https://boards.greenhouse.io/acme/jobs/4567890", "https://job-boards.greenhouse.io/acme/jobs/4567890"),
])
def test_a_redirect_to_the_same_posting_is_not_closed(requested, final):
    r = liveness.judge(requested, final, 200, JOB_PAGE.format(body=LONG))
    assert r.status == liveness.OPEN


def test_a_page_with_a_title_and_an_apply_path_is_open():
    r = judge(html=JOB_PAGE.format(body=LONG))
    assert r.status == liveness.OPEN


def test_a_job_posting_in_json_ld_is_open_even_without_a_visible_apply_link():
    html = ("<html><head><script type='application/ld+json'>{\"@context\":\"https://schema.org\",\"@type\":\"JobPosting\",\"title\":\"Data Scientist\"}</script>"
            "</head><body><div id=root>" + LONG + "</div></body></html>")
    assert judge(html=html).status == liveness.OPEN


def test_a_long_page_with_no_job_title_and_no_apply_path_is_closed():
    html = "<html><head><title>Acme</title></head><body><p>" + LONG + " Browse our teams, read our blog and follow us.</p><a href='/blog'>Blog</a></body></html>"
    r = judge(html=html)
    assert r.status == liveness.CLOSED and "no job title" in r.reason


def test_a_script_shell_with_no_text_is_uncertain_not_closed():
    html = "<html><head><title>Jobs</title></head><body><div id='root'></div><script src='/app.js'></script></body></html>"
    assert judge(html=html).status == liveness.UNCERTAIN


def test_only_a_title_or_only_an_apply_link_is_uncertain():
    only_title = "<html><body><h1>Data Scientist</h1><p>" + LONG + "</p></body></html>"
    only_apply = "<html><body><p>" + LONG + "</p><a href='/apply'>Apply</a></body></html>"
    assert judge(html=only_title).status == liveness.UNCERTAIN and judge(html=only_apply).status == liveness.UNCERTAIN


def test_an_expired_phrase_beats_a_page_that_still_has_an_apply_button():
    r = judge(html=JOB_PAGE.format(body="This job has expired. " + LONG))
    assert r.status == liveness.CLOSED and "expired" in r.reason


def test_script_and_style_text_is_not_page_text():
    html = JOB_PAGE.format(body=LONG) + "<script>var s = 'this job has expired';</script><style>.x::after{content:'no longer available'}</style>"
    assert judge(html=html).status == liveness.OPEN


# ------------------------------------------------------------------ fetching it
class Net:
    def __init__(self, pages=None, default=(200, JOB_PAGE.format(body=LONG))):
        self.pages, self.default, self.requests = pages or {}, default, []

    def get(self, url, **kw):
        self.requests.append(url)
        if url.endswith("/robots.txt"):
            return SimpleNamespace(status_code=404, text="", ok=False, headers={}, content=b"", url=url)
        status, body, *loc = self.pages.get(url, self.default)
        headers = {"Location": loc[0]} if loc else {}
        return SimpleNamespace(status_code=status, text=body, ok=status < 400, headers=headers, content=body.encode(), url=url)

    def pages_fetched(self):
        return [u for u in self.requests if not u.endswith("/robots.txt")]


def fetcher(net, sleeps=None, **kw):
    clock = {"t": 0.0}

    def sleep(s):
        (sleeps if sleeps is not None else []).append(s)
        clock["t"] += s
    return Fetcher(RobotsCache(), get=net.get, sleep=sleep, clock=lambda: clock["t"], never=applykind.NEVER_FETCH, **kw)


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
URL = "https://acme.com/careers/jobs/data-scientist-12345"


def test_check_fetches_the_page_and_judges_it():
    net = Net({URL: (404, "")})
    r = liveness.check(URL, fetcher(net), now=NOW)
    assert r.status == liveness.CLOSED and net.pages_fetched() == [URL]


def test_check_follows_a_redirect_and_sees_where_it_ended():
    net = Net({URL: (302, "", "https://acme.com/careers"), "https://acme.com/careers": (200, JOB_PAGE.format(body=LONG))})
    r = liveness.check(URL, fetcher(net), now=NOW)
    assert r.status == liveness.CLOSED and "redirect" in r.reason


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://uk.indeed.com/viewjob?jk=1", "https://www.cv-library.co.uk/job/1/x"])
def test_linkedin_indeed_and_cv_library_are_never_fetched(url):
    net = Net()
    r = liveness.check(url, fetcher(net), now=NOW)
    assert r.status == liveness.UNCERTAIN and "never" in r.reason and net.requests == []


def test_a_host_that_blocks_us_is_uncertain_and_not_asked_again():
    net = Net({URL: (403, "")})
    f = fetcher(net)
    assert liveness.check(URL, f, now=NOW).status == liveness.UNCERTAIN
    other = "https://acme.com/careers/jobs/ml-engineer-777"
    assert liveness.check(other, f, now=NOW).status == liveness.UNCERTAIN
    assert net.pages_fetched() == [URL]                                              # the second sent nothing: 403 backs the host off for the run


def test_robots_txt_that_disallows_is_respected():
    class Disallow(Net):
        def get(self, url, **kw):
            if url.endswith("/robots.txt"):
                self.requests.append(url)
                return SimpleNamespace(status_code=200, text="User-agent: *\nDisallow: /careers/", ok=True, headers={}, content=b"x", url=url)
            return super().get(url, **kw)
    net = Disallow()
    r = liveness.check(URL, fetcher(net), now=NOW)
    assert r.status == liveness.UNCERTAIN and net.pages_fetched() == []


def test_a_server_error_is_uncertain():
    assert liveness.check(URL, fetcher(Net({URL: (503, "")})), now=NOW).status == liveness.UNCERTAIN


def test_one_request_a_second_per_host():
    sleeps = []
    net = Net()
    f = fetcher(net, sleeps)
    for n in range(3):
        liveness.check(f"https://acme.com/jobs/{1000 + n}", f, now=NOW)
    assert len([s for s in sleeps if s > 0]) >= 2 and all(s <= 1.0 + 1e-9 for s in sleeps)


def test_no_url_is_uncertain():
    assert liveness.check("", fetcher(Net()), now=NOW).status == liveness.UNCERTAIN


# ------------------------------------------------------------------ the 6 hour cache
def test_an_answer_is_kept_for_six_hours(tmp_path):
    cache = liveness.Cache(tmp_path / "liveness.json")
    net = Net()
    f = fetcher(net)
    first = liveness.check(URL, f, cache=cache, now=NOW)
    again = liveness.check(URL, f, cache=cache, now=NOW + timedelta(hours=5, minutes=59))
    assert first.status == again.status == liveness.OPEN and not first.cached and again.cached and len(net.pages_fetched()) == 1
    liveness.check(URL, f, cache=cache, now=NOW + timedelta(hours=6, minutes=1))
    assert len(net.pages_fetched()) == 2


def test_the_cache_survives_a_new_process_and_a_damaged_file_is_ignored(tmp_path):
    path = tmp_path / "liveness.json"
    net = Net({URL: (410, "")})
    liveness.check(URL, fetcher(net), cache=liveness.Cache(path), now=NOW)
    again = liveness.check(URL, fetcher(net), cache=liveness.Cache(path), now=NOW + timedelta(hours=1))
    assert again.status == liveness.CLOSED and again.cached and len(net.pages_fetched()) == 1
    path.write_text("{not json", encoding="utf-8")
    assert liveness.check(URL, fetcher(net), cache=liveness.Cache(path), now=NOW).status == liveness.CLOSED


def test_the_cache_key_ignores_the_tracking_parts_of_the_address(tmp_path):
    cache = liveness.Cache(tmp_path / "l.json")
    net = Net()
    f = fetcher(net)
    liveness.check(URL + "?utm_source=x#top", f, cache=cache, now=NOW)
    assert liveness.check(URL, f, cache=cache, now=NOW).cached
