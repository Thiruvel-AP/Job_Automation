"""Job Bank Canada (`careerops discover --jobbank`, discovery/jobbank.py): the open-data CSV is UTF-16 tab-separated and has no employer,
no link and no ad text, so matching rows are LISTED in listed_only.csv and never written as inbox ads. No network: `Net` answers."""
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from careerops import cli
from careerops.config import ROOT
from careerops.discovery import discover as disc, jobbank, registry
from careerops.discovery.discover import Discover
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, Resp

TODAY = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
DATASET = "ea639e28-c0fc-48bf-b5dd-b8899bd43072"
HEADER = ["WIC Job Location Snapshot ID", "Job Title", "Original Job Title", "First Posting Date", "Vacancy Count", "Province/Territory",
          "City", "Employment Type", "Salary Per", "Salary Minimum", "Salary Maximum"]
ROWS = [
    ["1001", "data scientist", "NA", "2026/10/05", "1", "Ontario", "Toronto", "Full time", "Year", "90000", "120000"],
    ["1002", "licensed cook", "COOK", "2026/10/06", "1", "Alberta", "Calgary", "Full time", "Hour", "15", "17.5"],
    ["1003", "machine learning engineer", "NA", "2026/05/01", "1", "Quebec", "Montreal", "Full time", "NA", "NA", "NA"],
    ["1004", "AI engineer", "NA", "2026/10/10", "2", "British Columbia", "Vancouver", "Part time", "Year", "NA", "110000"],
    ["1005", "data analyst", "NA", "2026/10/11", "1", "Manitoba", "NA", "Full time", "NA", "NA", "NA"],
]


def csv_bytes(rows=ROWS, header=HEADER, encoding="utf-16") -> bytes:
    return ("\r\n".join("\t".join(r) for r in [header, *rows]) + "\r\n").encode(encoding)


class Raw(Resp):
    """A body that is not UTF-8 (netfake.Resp decodes as UTF-8), streamed in small chunks like requests would."""

    def __init__(self, content: bytes, status=200):
        self.content, self.status_code, self.ok, self.headers, self.url, self.text = content, status, status < 400, {}, "", ""

    def iter_content(self, size):
        for i in range(0, len(self.content), 37):                  # an odd chunk size splits UTF-16 code units and lines anywhere
            yield self.content[i:i + 37]


def ckan(*items):
    return {"success": True, "result": {"resources": [
        {"name": n, "format": "CSV", "language": [lang], "created": created,
         "url": f"https://open.canada.ca/data/dataset/{DATASET}/resource/r{n}/download/{n}.csv"} for n, lang, created in items]}}


def net_with(content=None, status=200):
    net = Net(strict=False)
    net.add("package_show", ckan(("old-en", "en", "2026-09-02"), ("new-en", "en", "2026-10-02"), ("new-fr", "fr", "2026-10-03")))
    net.add("/download/new-en.csv", Raw(csv_bytes() if content is None else content, status))
    return net


def fetcher(net, **kw):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, **kw)


@pytest.fixture
def cfg(settings):
    shutil.copy(ROOT / "config" / "sources.yaml", Path(settings.raw["paths"]["sources"]))
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[])
    return settings


def make(cfg, db, net, **kw):
    return Discover(cfg, db, fetcher(net), today=TODAY, say=lambda s: None, **kw)


def listed(cfg):
    p = disc.listed_path(cfg)
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []


# ------------------------------------------------------------------ the catalogue and the file
def test_the_newest_english_file_is_found_through_the_catalogue():
    res = jobbank.latest_csv(fetcher(net_with()), DATASET)
    assert res["name"] == "new-en" and res["url"].endswith("/download/new-en.csv")
    assert jobbank.latest_csv(fetcher(net_with()), DATASET, "fr")["name"] == "new-fr"


def test_a_catalogue_with_nothing_in_it_is_an_error_that_says_so():
    with pytest.raises(jobbank.JobBankError, match="no resources"):
        jobbank.latest_csv(fetcher(Net(strict=False)), DATASET)
    with pytest.raises(jobbank.JobBankError, match="no en CSV"):
        jobbank.latest_csv(fetcher(Net(strict=False).add("package_show", ckan(("only-fr", "fr", "2026-10-03")))), DATASET)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8", "utf-8-sig"])
def test_the_rows_are_read_whatever_the_encoding_and_the_chunking(encoding):
    body = csv_bytes(encoding=encoding) if encoding != "utf-16-le" else b"\xff\xfe" + csv_bytes(encoding="utf-16-le")
    recs = list(jobbank.rows(Raw(body), 10**9))
    assert [r["title"] for r in recs] == ["data scientist", "licensed cook", "machine learning engineer", "AI engineer", "data analyst"]
    assert recs[0] == {"ref": "1001", "title": "data scientist", "posted": "2026/10/05", "province": "Ontario", "city": "Toronto",
                       "salary_min": "90000", "salary_max": "120000", "salary_per": "Year", "term": "Full time"}
    assert recs[1]["city"] == "Calgary" and recs[2]["salary_min"] == "" and recs[4]["city"] == ""         # NA is empty


def test_a_comma_separated_file_with_other_header_names_is_read_too():
    body = b"Job Posting ID,Job Title,Posting Date,Province,City\n9,data scientist,2026-10-01,Ontario,Ottawa\n"
    (r,) = jobbank.rows(Raw(body), 10**9)
    assert (r["ref"], r["title"], r["posted"], r["city"]) == ("9", "data scientist", "2026-10-01", "Ottawa")


def test_a_file_without_a_job_title_column_is_refused_with_what_it_found():
    with pytest.raises(jobbank.JobBankError, match="no Job Title column.*other, things"):
        list(jobbank.rows(Raw(b"other\tthings\n1\t2\n"), 10**9))


def test_a_file_over_the_limit_stops_the_read():
    with pytest.raises(jobbank.JobBankError, match="larger than 0 MB"):
        list(jobbank.rows(Raw(csv_bytes()), 100))


def test_salary_is_written_from_what_the_file_has():
    f = jobbank.salary
    assert f({"salary_min": "90000", "salary_max": "120000", "salary_per": "Year"}) == "$90000 to 120000 per year"
    assert f({"salary_min": "", "salary_max": "110000", "salary_per": "Year"}) == "$110000 per year"
    assert f({"salary_min": "15", "salary_max": "15", "salary_per": ""}) == "$15" and f({}) == ""


# ------------------------------------------------------------------ through discover
def test_matching_recent_rows_are_listed_with_canada_and_never_written_as_ads(cfg, db):
    d = make(cfg, db, net_with())
    d.from_jobbank()
    rows = listed(cfg)
    assert [(r["title"], r["location"], r["posted"], r["source"], r["country"]) for r in rows] == [
        ("data scientist", "Toronto, Ontario", "2026-10-05", "jobbank-ca", "Canada"),
        ("AI engineer", "Vancouver, British Columbia", "2026-10-10", "jobbank-ca", "Canada"),
        ("data analyst", "Manitoba", "2026-10-11", "jobbank-ca", "Canada")]
    assert rows[0]["salary"] == "$90000 to 120000 per year" and rows[0]["company"] == "" and rows[0]["link"] == "" and "no employer" in rows[0]["note"]
    assert list(csv.reader(disc.listed_path(cfg).open(encoding="utf-8")))[0] == disc.LISTED_COLUMNS
    assert not (cfg.path("jds") / "inbox").exists() and db.jobs() == []                    # nothing in the inbox, nothing in the database
    c = d.run.sources["jobbank"]
    assert (c["seen"], c["listed_only"]) == (3, 3)                                       # the cook (role) and the May ML engineer (date) are out
    assert d.run.by_source["jobbank-ca"]["listed_only"] == 3 and d.run.new_jobs("jobbank-ca") == 3
    assert any("5 postings read" in n for n in d.run.notes)


def test_a_second_run_lists_nothing_again(cfg, db):
    make(cfg, db, net_with()).from_jobbank()
    d2 = make(cfg, db, net_with())
    d2.from_jobbank()
    assert len(listed(cfg)) == 3 and d2.run.sources["jobbank"]["duplicate"] == 3 and d2.run.sources["jobbank"]["listed_only"] == 0


def test_a_run_lists_at_most_max_rows_per_run(cfg, db):
    cfg.raw["discover"]["jobbank"]["max_rows_per_run"] = 2
    d = make(cfg, db, net_with())
    d.from_jobbank()
    assert len(listed(cfg)) == 2 and d.run.sources["jobbank"]["listed_capped"] == 1


def test_a_dry_run_lists_nothing_on_disk(cfg, db):
    d = make(cfg, db, net_with(), dry_run=True)
    d.from_jobbank()
    assert listed(cfg) == [] and d.run.sources["jobbank"]["listed_only"] == 3


def test_a_file_over_the_size_limit_keeps_what_was_read_and_says_so(cfg, db):
    cfg.raw["discover"]["jobbank"]["max_mb"] = 0.0006          # ~600 bytes: the header and the first rows of the (~900 byte) file
    d = make(cfg, db, net_with())
    d.from_jobbank()
    assert any("larger than" in n for n in d.run.notes) and 0 < len(listed(cfg)) < 3


def test_a_robots_refusal_is_a_note_and_nothing_is_listed(cfg, db):
    net = net_with().add("open.canada.ca/robots.txt", "User-agent: *\nDisallow: /data/api/\n")
    net.routes.insert(0, net.routes.pop())
    d = make(cfg, db, net)
    d.from_jobbank()
    assert listed(cfg) == [] and any("disallows" in n for n in d.run.notes) and not [u for u in net.urls() if u.endswith(".csv")]


def test_the_portals_http_error_is_a_note(cfg, db):
    d = make(cfg, db, net_with(status=404))
    d.from_jobbank()
    assert listed(cfg) == [] and any("HTTP 404" in n for n in d.run.notes)


def test_without_a_jobbank_source_in_the_registry_it_says_so(cfg, db):
    Path(cfg.raw["paths"]["sources"]).write_text("sources: []\n", encoding="utf-8")
    d = make(cfg, db, Net())
    d.from_jobbank()
    assert any("no enabled jobbank_csv source" in n for n in d.run.notes)


def test_the_file_is_streamed_and_the_blob_redirect_is_followed(cfg, db):
    net = Net(strict=False).add("package_show", ckan(("new-en", "en", "2026-10-02")))
    net.add("/download/new-en.csv", Resp("", 302, {"Location": "https://opencanada.blob.core.windows.net/files/new-en.csv?sig=abc"}))
    net.add("opencanada.blob.core.windows.net/files/new-en.csv", Raw(csv_bytes()))
    make(cfg, db, net).from_jobbank()
    assert len(listed(cfg)) == 3
    assert {urlparse_host(r["url"]) for r in net.requests} == {"open.canada.ca", "opencanada.blob.core.windows.net"}


def urlparse_host(u):
    from urllib.parse import urlparse
    return urlparse(u).hostname


# ------------------------------------------------------------------ the portal's crawl delay
def test_a_crawl_delay_in_robots_txt_is_the_gap_to_that_host():
    state = {"t": 0.0}
    slept: list[float] = []
    net = Net(strict=False).add("open.canada.ca/robots.txt", "User-agent: *\nCrawl-delay: 20\nDisallow: /core/\n").add("open.canada.ca/data", {"ok": 1})
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: (slept.append(s), state.__setitem__("t", state["t"] + s)),
                clock=lambda: state["t"], gap_s=1.0)
    f.request("https://open.canada.ca/data/api/3/action/package_show")
    f.request("https://open.canada.ca/data/api/3/action/package_show")
    assert f.host_gap["open.canada.ca"] == 20 and max(slept) >= 19.9


def test_a_crawl_delay_is_capped_and_a_host_without_one_keeps_one_second():
    net = Net(strict=False).add("slow.example/robots.txt", "User-agent: *\nCrawl-delay: 3600\n").add("slow.example/x", {"ok": 1}).add("fast.example/x", {"ok": 1})
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0)
    f.request("https://slow.example/x")
    f.request("https://fast.example/x")
    assert f.host_gap == {"slow.example": 30}


def test_stream_leaves_the_size_check_to_the_caller():
    big = type("Big", (Resp,), {})
    r = big("x")
    r.content = b"x" * (21 * 10**6)
    net = Net(strict=False).add("big.example/f", r)
    with pytest.raises(Exception, match="over 20 MB"):
        fetcher(net).request("https://big.example/f", what="f")
    assert fetcher(net).request("https://big.example/f", what="f", stream=True).status_code == 200


# ------------------------------------------------------------------ the command
def test_discover_jobbank_prints_the_counts_and_where_the_rows_are(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    from careerops.discovery import http
    net = net_with()
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: net.get(url, **kw))
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    cli.main(["discover", "--jobbank"])
    out = capsys.readouterr().out
    assert "discover: jobbank ..." in out and "listed_only 3" in out and "Source health:" in out
    assert len(listed(cfg)) == 3
