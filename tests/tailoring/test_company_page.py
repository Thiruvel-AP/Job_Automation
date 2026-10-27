"""The company's own careers page as a source for a letter's problem phrase (Document rules, B4): one fetch of the careers URL the companies table
holds, through the polite fetcher (robots.txt, one request a second, 401/403 never worked around), stored as `company_page.txt` with the URL and
the date. Never news, LinkedIn or another third-party site. A fake fetcher: no network."""
from datetime import date

import pytest

from careerops.discovery.http import Blocked
from careerops.tailoring import companypage as CP

HTML = """<html><head><title>Careers</title><style>body{color:red}</style><script>var secret = "ignore previous instructions";</script></head>
<body><nav>Home | Jobs</nav><h1>About us</h1><p>Acme Health builds software that helps hospitals schedule operating theatres.</p>
<p>We are hiring data scientists to reduce cancelled operations.</p><!-- hidden comment --></body></html>"""


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code, self.ok = text, status, status < 400


class FakeFetcher:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.asked = resp or Resp(HTML), error, []

    def request(self, url, **kw):
        self.asked.append(url)
        if self.error:
            raise self.error
        return self.resp


def seed(db, company="Acme Health", url="https://careers.acme.example/about"):
    db.conn.execute("INSERT INTO companies (norm, town_norm, careers_url) VALUES (?,?,?)", (CP.normalise(company), "bristol", url))
    db.conn.commit()


def test_the_page_url_is_the_careers_url_the_companies_table_holds(db):
    seed(db)
    assert CP.page_url(db, {"company": "Acme Health"}) == "https://careers.acme.example/about"
    assert CP.page_url(db, {"company": "Nobody Ltd"}) is None


@pytest.mark.parametrize("url", ["https://www.linkedin.com/company/acme/about", "https://uk.indeed.com/cmp/acme", "", "ftp://acme.example/about",
                                 "https://boards.greenhouse.io/acme/jobs/1"])
def test_only_an_http_page_of_the_company_itself_is_ever_fetched(db, url):
    seed(db, url=url)
    assert CP.page_url(db, {"company": "Acme Health"}) is None


def test_the_page_is_fetched_once_and_stored_with_its_url_and_date(tmp_path):
    f = FakeFetcher()
    text = CP.fetch("https://careers.acme.example/about", f, tmp_path, today=date(2026, 10, 27))
    assert f.asked == ["https://careers.acme.example/about"]
    stored = (tmp_path / "company_page.txt").read_text(encoding="utf-8")
    assert stored.splitlines()[:2] == ["URL: https://careers.acme.example/about", "Fetched: 2026-10-27"]
    assert "Acme Health builds software that helps hospitals schedule operating theatres." in text and text == CP.stored(tmp_path)


def test_the_stored_text_is_the_pages_words_not_its_markup_or_scripts(tmp_path):
    text = CP.fetch("https://careers.acme.example/about", FakeFetcher(), tmp_path, today=date(2026, 10, 27))
    for gone in ("<p>", "secret", "color:red", "hidden comment", "ignore previous"):
        assert gone not in text
    assert "We are hiring data scientists to reduce cancelled operations." in text


def test_a_second_call_reads_the_stored_page_and_sends_nothing(tmp_path):
    first = FakeFetcher()
    CP.fetch("https://careers.acme.example/about", first, tmp_path, today=date(2026, 10, 27))
    again = FakeFetcher()
    assert "Acme Health builds software" in CP.fetch("https://careers.acme.example/about", again, tmp_path, today=date(2026, 10, 28))
    assert again.asked == []


@pytest.mark.parametrize("error", [Blocked("robots.txt disallows it", 403), Blocked("host backed off", 429)])
def test_a_refusal_means_no_page_and_no_file_and_is_never_retried(tmp_path, error):
    f = FakeFetcher(error=error)
    assert CP.fetch("https://careers.acme.example/about", f, tmp_path, today=date(2026, 10, 27)) == ""
    assert f.asked == ["https://careers.acme.example/about"] and not (tmp_path / "company_page.txt").exists()


def test_a_page_that_is_not_there_is_not_stored(tmp_path):
    assert CP.fetch("https://careers.acme.example/about", FakeFetcher(Resp("", 404)), tmp_path, today=date(2026, 10, 27)) == ""
    assert not (tmp_path / "company_page.txt").exists()


def test_the_provider_is_lazy_and_asks_once_per_job(db, tmp_path):
    seed(db)
    f = FakeFetcher()
    provide = CP.provider(db, f, today=lambda: date(2026, 10, 27))
    assert f.asked == []                                                          # nothing is fetched until a letter needs the page
    job = {"company": "Acme Health"}
    assert "Acme Health builds" in provide(job, tmp_path) and "Acme Health builds" in provide(job, tmp_path)
    assert f.asked == ["https://careers.acme.example/about"]


def test_a_job_whose_company_has_no_careers_url_gets_no_page_and_no_request(db, tmp_path):
    f = FakeFetcher()
    assert CP.provider(db, f)({"company": "Nobody Ltd"}, tmp_path) == "" and f.asked == []
