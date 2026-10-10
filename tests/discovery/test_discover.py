"""`careerops discover --ats | --reed | --adzuna | --gmail-alerts` (discovery/discover.py): the filters, dedupe, closed postings, the
inbox files, the manual queue, the ranking by priority, and the report. No network: `Net` answers every request.
Sponsorship is never a filter: a JD that says "no sponsorship" is written like any other."""
import json
import os
from datetime import datetime, timezone

import pytest

from careerops import cli
from careerops.discovery import discover as disc, gmail, intake, manual, register as sponsors
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net, Resp, fixture_json, fixture_text

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
ACME = {"slug": "acme", "name": "Acme Analytics Ltd"}


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def gh_net(**extra):
    """Acme's Greenhouse board, everything else unknown (404)."""
    net = Net(strict=False)
    for part, reply in extra.items():
        net.add(part.replace("__", "/"), reply)
    net.add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))
    return net


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[ACME], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"]["alert_probes_per_run"] = 0
    settings.raw["discover"]["ats"]["probe_per_run"] = 0
    return settings


def make(cfg, db, net, **kw):
    return Discover(cfg, db, fetcher(net), today=TODAY, say=lambda s: None, **kw)


def inbox_files(settings):
    box = settings.path("jds") / "inbox"
    return sorted(p.name for p in box.glob("*.txt")) if box.exists() else []


def read_inbox(settings, name):
    return (settings.path("jds") / "inbox" / name).read_text(encoding="utf-8")


def titles(d):
    return sorted(c.title + " | " + c.location for c, _ in d.run.written)


# ------------------------------------------------------------------ ats: filters
def test_a_board_is_filtered_to_the_target_families_and_the_uk_or_remote_uk(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert titles(d) == ["Data Scientist | London, UK", "Data Scientist | Manchester", "Machine Learning Engineer | Remote"]
    c = d.run.sources["ats"]
    assert (c["seen"], c["role_filtered"], c["place_filtered"], c["written"]) == (6, 1, 2, 3)    # Marketing; New York + remote US


def test_remote_counts_only_when_the_ad_says_uk(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    names = " ".join(inbox_files(cfg))
    assert "machine-learning-engineer-104" in names and "applied-scientist" not in names


def test_a_jd_that_says_no_sponsorship_is_written_like_any_other(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    text = read_inbox(cfg, "acme-analytics-ltd-data-scientist-106.txt")
    assert "right to work in the UK without sponsorship" in text


def test_the_inbox_file_has_the_headers_live_reads_and_the_ad_text(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    text = read_inbox(cfg, "acme-analytics-ltd-data-scientist-101.txt")
    text2, _ = manual.read_jd(cfg.path("jds") / "inbox" / "acme-analytics-ltd-data-scientist-101.txt")
    assert manual.header_fields(text2) == {"company": "Acme Analytics Ltd", "title": "Data Scientist", "location": "London, UK",
                                           "url": "https://job-boards.greenhouse.io/acme/jobs/101", "source": "greenhouse", "posted": "2026-10-08"}
    assert text.startswith("Company: Acme Analytics Ltd\nTitle: Data Scientist\nURL: https://job-boards.greenhouse.io/acme/jobs/101\nSource: greenhouse\n")
    assert "Python, SQL and scikit-learn" in text and "<p>" not in text


def test_an_existing_inbox_file_is_never_overwritten(cfg, db):
    make(cfg, db, gh_net()).from_boards()
    path = cfg.path("jds") / "inbox" / "acme-analytics-ltd-data-scientist-101.txt"
    path.write_text(path.read_text(encoding="utf-8") + "\nMY NOTE\n", encoding="utf-8")
    again = make(cfg, db, gh_net())
    again.from_boards()
    assert "MY NOTE" in path.read_text(encoding="utf-8")
    assert again.run.written == [] and again.run.sources["ats"]["duplicate"] == 3          # the inbox files are known jobs now


# ------------------------------------------------------------------ dedupe
def test_a_job_already_in_the_database_is_skipped(cfg, db):
    db.upsert_job({"id": "manual:1", "source": "manual", "company": "Acme Analytics Ltd", "title": "Data Scientist", "location": "London, United Kingdom",
                   "url": "u", "apply_url": "u", "description": "x"})
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert titles(d) == ["Data Scientist | Manchester", "Machine Learning Engineer | Remote"]       # London matched; Manchester is another place
    assert d.run.sources["ats"]["duplicate"] == 1


@pytest.mark.parametrize("folder", ["done", "rejected", "inbox"])
def test_a_job_in_the_inbox_done_or_rejected_folder_is_skipped(cfg, db, folder):
    box = cfg.path("jds") / folder
    box.mkdir(parents=True)
    (box / "old.txt").write_text("Company: Acme Analytics Ltd\nTitle: Machine Learning Engineer\nURL: https://e.com/1\nLocation: Remote, UK\n\n"
                                 + "x" * 600, encoding="utf-8")
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert "Machine Learning Engineer | Remote" not in titles(d) and d.run.sources["ats"]["duplicate"] == 1


def test_a_job_in_the_application_archive_is_skipped_whatever_its_place_and_the_archive_is_not_touched(cfg, db):
    arch = cfg.path("applications")
    (arch / "Acme Analytics" / "Data Scientist" / "v2").mkdir(parents=True)
    (arch / "Acme Analytics" / "Data Scientist" / "CV.pdf").write_bytes(b"%PDF-1.4 mine")
    (arch / "Other Co" / "Analyst").mkdir(parents=True)

    def tree():
        return sorted((os.path.relpath(os.path.join(r, n), arch), os.stat(os.path.join(r, n)).st_mtime_ns, os.stat(os.path.join(r, n)).st_size)
                      for r, ds, fs in os.walk(arch) for n in ds + fs)
    before = tree()
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert titles(d) == ["Machine Learning Engineer | Remote"]                  # both Data Scientist ads: you applied to that role there
    assert d.run.sources["ats"]["duplicate"] == 2
    assert tree() == before                                                       # read only: nothing created, changed or removed


def test_archive_pairs_lists_company_and_role_folders_and_nothing_else(tmp_path):
    (tmp_path / "A" / "Role One" / "v2").mkdir(parents=True)
    (tmp_path / "A" / "stray.pdf").write_bytes(b"x")
    (tmp_path / "B").mkdir()
    assert sorted(disc.archive_pairs(tmp_path)) == [("A", "Role One")]
    assert disc.archive_pairs(tmp_path / "missing") == []


def test_the_same_job_on_two_boards_is_written_once(cfg, db):
    cfg.raw["sources"]["lever"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    lever = fixture_json("lever_acme.json")
    lever[0].update(text="Data Scientist", categories={"location": "London, UK"})
    net = gh_net(**{"api.lever.co": lever})
    d = make(cfg, db, net)
    d.from_boards()
    assert sum(t.startswith("Data Scientist | London") for t in titles(d)) == 1
    assert d.run.sources["ats"]["duplicate"] >= 1


def test_a_posting_that_is_not_listed_is_closed_and_skipped(cfg, db):
    cfg.raw["sources"].update(greenhouse=[], ashby=[{"slug": "acme", "name": "Acme Analytics Ltd"}])
    d = make(cfg, db, Net(strict=False).add("ashbyhq.com", fixture_json("ashby_acme.json")))
    d.from_boards()
    assert titles(d) == ["Machine Learning Engineer | London, UK"]               # the unlisted Data Scientist is closed; Berlin is another place
    c = d.run.sources["ats"]
    assert c["closed"] == 1 and c["place_filtered"] + c["role_filtered"] == 1


def test_the_limit_caps_the_new_files_and_dry_run_writes_nothing(cfg, db):
    d = make(cfg, db, gh_net(), limit=1)
    d.from_boards()
    assert len(inbox_files(cfg)) == 1 and len(d.run.written) == 1
    dry = make(cfg, db, gh_net(), dry_run=True)
    dry.known = disc.Known()
    dry.from_boards()
    assert len(dry.run.written) == 3 and len(inbox_files(cfg)) == 1                 # only the first run's file exists


def test_the_board_list_is_cached_and_reused(cfg, db):
    net = gh_net()
    make(cfg, db, net).from_boards()
    n = len(net.urls())
    again = make(cfg, db, net)
    again.from_boards()
    assert len(net.urls()) == n and (cfg.path("discover") / "boards" / "greenhouse-acme.json").exists()


# ------------------------------------------------------------------ ats: sponsor-register companies
def test_a_register_company_gets_its_board_found_and_is_read_under_its_register_name(cfg, db):
    sponsors_csv = cfg.path("sponsors_csv")
    db.conn.execute("INSERT INTO companies (norm, town_norm, register_name, town, rating, route, first_seen) VALUES "
                    "('acme analytics','london','Acme Analytics Ltd','London','A','Skilled Worker','2026-10-01')")
    db.conn.commit()
    cfg.raw["sources"]["greenhouse"] = []
    cfg.raw["discover"]["ats"]["probe_per_run"] = 5
    net = gh_net()
    net.routes.insert(0, ("boards/acmeanalytics/jobs", fixture_json("gh_jobs_acme.json")))
    net.routes.insert(1, ("boards/acmeanalytics", fixture_json("gh_board_acme.json")))
    d = make(cfg, db, net)
    d.from_boards()
    assert db.conn.execute("SELECT probe_status, ats_slug FROM companies").fetchone()[:] == ("verified", "acmeanalytics")
    assert titles(d)[0].startswith("Data Scientist") and all(c.company == "Acme Analytics Ltd" for c, _ in d.run.written)
    assert any("looked for boards" in n or "ats resolve" in n for n in d.run.notes)


def test_without_a_register_the_configured_boards_are_still_read_and_it_says_so(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    assert any("no sponsor-register companies" in n for n in d.run.notes) and len(d.run.written) == 3


# ------------------------------------------------------------------ the ranking
def test_the_listing_is_ordered_by_priority_and_a_sponsor_with_the_same_ad_comes_first(cfg, db, fb_fixture):
    db.replace_sponsors(sponsors.parse(cfg.path("sponsors_csv")))
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Zeta Widgets Ltd"}, {"slug": "northwind", "name": "Northwind Health AI"}]
    net = gh_net()
    net.routes.insert(0, ("boards/northwind/jobs", fixture_json("gh_jobs_acme.json")))
    net.routes.insert(1, ("boards/northwind", fixture_json("gh_board_acme.json")))
    d = make(cfg, db, net, fb=fb_fixture)
    d.from_boards()
    ranked = d.ranked()
    p = [r[0].p for r in ranked]
    assert p == sorted(p, reverse=True)
    by = {(r[1].company, r[1].ext_id): r[0] for r in ranked}
    north, zeta = by[("Northwind Health AI", "101")], by[("Zeta Widgets Ltd", "101")]
    assert (north.s, zeta.s) == (1.0, 0.75) and north.f == zeta.f and north.p > zeta.p and zeta.s_reason == "not on the register"
    assert ranked[0][1].company == "Northwind Health AI" and ranked[0][1].ext_id == "101"
    line = d.listing()[0]
    assert "Northwind Health AI | Data Scientist" in line and " = " in line and "exact register match" in line


def test_nothing_is_dropped_for_sponsorship_the_job_with_the_worst_s_is_listed(cfg, db, fb_fixture):
    d = make(cfg, db, gh_net(), fb=fb_fixture)
    d.from_boards()
    worst = next(r for r in d.ranked() if r[1].ext_id == "106")
    assert worst[0].s == 0.65 and worst[0].p > 0 and len(d.ranked()) == 3


# ------------------------------------------------------------------ reed
def reed_net():
    def search(url, params=None, **kw):
        if params.get("postedByRecruitmentAgency") == "true":
            return Resp(fixture_json("reed_search_agency.json"))
        return Resp(fixture_json("reed_search_direct.json") if params.get("keywords") == "data scientist" else {"results": []})
    return (Net(strict=False).add("api/1.0/search", search).add("api/1.0/jobs/5001", fixture_json("reed_job_5001.json"))
            .add("api/1.0/jobs/6001", fixture_json("reed_job_6001.json")))


def test_reed_jobs_are_written_with_their_full_text_and_agency_posts_are_flagged(cfg, db, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "k")
    cfg.raw["discover"]["reed"]["queries"] = ["data scientist"]
    d = make(cfg, db, reed_net())
    d.from_reed()
    assert titles(d) == ["Data Scientist | Leeds", "Machine Learning Engineer | Reading"]
    direct = read_inbox(cfg, "birch-health-data-scientist-5001.txt")
    agency = read_inbox(cfg, "northgate-recruitment-solutions-machine-learning-engineer-6001.txt")
    assert "Source: reed" in direct and "Agency: yes" not in direct and "Reed full description." in direct and "Salary: GBP 45,000-55,000" in direct
    assert "Agency: yes" in agency and "Posted: 2026-10-08" in agency
    c = d.run.sources["reed"]
    assert (c["role_filtered"], c["closed"], c["written"]) == (1, 1, 2)                 # Account Manager; the AI Engineer that expired


def test_reed_without_a_key_is_a_note_and_not_a_crash(cfg, db, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    d = make(cfg, db, Net())
    d.from_reed()
    assert any("REED_API_KEY is not set" in n for n in d.run.notes) and d.run.written == []


def test_reed_jobs_that_are_known_cost_no_details_request(cfg, db, monkeypatch):
    monkeypatch.setenv("REED_API_KEY", "k")
    cfg.raw["discover"]["reed"]["queries"] = ["data scientist"]
    db.upsert_job({"id": "manual:b", "source": "manual", "company": "Birch Health", "title": "Data Scientist", "location": "Leeds", "url": "u",
                   "apply_url": "u", "description": "x"})
    net = reed_net()
    d = make(cfg, db, net)
    d.from_reed()
    assert not any(u.endswith("/jobs/5001") for u in net.urls()) and d.run.sources["reed"]["duplicate"] == 1


# ------------------------------------------------------------------ adzuna: snippets are links
def test_adzuna_snippets_are_resolved_to_the_employers_own_board_or_queued(cfg, db, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    cfg.raw["sources"]["adzuna"] = {"enabled": True, "queries": ["data scientist"], "pages_per_query": 1, "max_days_old": 7}
    net = gh_net(**{"api.adzuna.com": fixture_json("adzuna_search.json")})
    d = make(cfg, db, net)
    d.from_adzuna()
    assert titles(d) == ["Data Scientist | London, UK"]                                  # the ad on Acme's own board
    text = read_inbox(cfg, "acme-analytics-ltd-data-scientist-101.txt")
    assert "Source: greenhouse" in text and "Python, SQL and scikit-learn" in text       # the full ad, not Adzuna's snippet
    rows = disc.read_manual_queue(cfg.path("jds") / "manual_queue.csv")
    assert [(r["source"], r["company"], r["title"], r["link"]) for r in rows] == [
        ("adzuna", "Unknown Startup Ltd", "AI Engineer", "https://www.adzuna.co.uk/jobs/land/ad/7002")]
    assert "no public job board known for Unknown Startup Ltd" in rows[0]["reason"]
    c = d.run.sources["adzuna"]
    assert (c["seen"], c["role_filtered"], c["resolved"], c["to_manual_queue"], c["written"]) == (3, 1, 1, 1, 1)
    assert not any("adzuna.co.uk/jobs/land" in u for u in net.urls())                    # Adzuna's redirect links are never followed


def test_a_long_uncut_adzuna_description_is_taken_as_the_ad(cfg, db, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    body = fixture_json("adzuna_search.json")
    body["results"][1]["description"] = "Build LLM agents in Python. " * 80
    cfg.raw["sources"]["adzuna"] = {"enabled": True, "queries": ["ai engineer"], "pages_per_query": 1}
    d = make(cfg, db, Net(strict=False).add("api.adzuna.com", body))
    d.from_adzuna()
    assert any(c.company == "Unknown Startup Ltd" and c.source == "adzuna" for c, _ in d.run.written)


def test_adzuna_without_keys_is_a_note(cfg, db, monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    d = make(cfg, db, Net())
    d.from_adzuna()
    assert any("ADZUNA_APP_ID" in n for n in d.run.notes)


def test_an_unknown_company_is_probed_once_and_remembered_when_probing_is_allowed(cfg, db, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    cfg.raw["discover"]["alert_probes_per_run"] = 5
    cfg.raw["sources"]["adzuna"] = {"enabled": True, "queries": ["x"], "pages_per_query": 1}
    net = gh_net(**{"api.adzuna.com": fixture_json("adzuna_search.json")})
    make(cfg, db, net).from_adzuna()
    probes = [u for u in net.urls() if "unknownstartup" in u]
    assert probes and (cfg.path("discover") / "company_boards.json").exists()
    net2 = gh_net(**{"api.adzuna.com": fixture_json("adzuna_search.json")})
    make(cfg, db, net2).from_adzuna()
    assert not [u for u in net2.urls() if "unknownstartup" in u]                         # a miss is remembered for reprobe_after_days


# ------------------------------------------------------------------ gmail alerts
def gmail_net(extra=None):
    mails = {
        "m1": ("LinkedIn <jobalerts-noreply@linkedin.com>", "alert_linkedin.html"), "m2": ("CV-Library <alerts@cv-library.co.uk>", "alert_cvlibrary.html"),
        "m3": ("Indeed <alert@indeed.com>", "alert_indeed.html"), "m4": ("Reed <jobalerts@reed.co.uk>", "alert_reed.html"),
        "m5": ("Mum <mum@gmail.com>", "alert_reed.html")}
    net = Net(strict=False)
    for mid, (frm, html) in mails.items():
        net.add(f"/messages/{mid}", {"id": mid, "payload": {"mimeType": "text/html", "headers": [{"name": "From", "value": frm}],
                                                              "body": {"data": __import__("base64").urlsafe_b64encode(fixture_text(html).encode()).decode()}}})
    net.add("gmail.googleapis.com/gmail/v1/users/me/messages", {"messages": [{"id": m} for m in mails]})
    for part, reply in reversed(list((extra or {}).items())):          # the first given is the first tried
        net.routes.insert(0, (part, reply))
    return net


@pytest.fixture
def signed_in(cfg):
    d = cfg.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    gmail._write_token(cfg, {"refresh_token": "rt", "access_token": "at", "expires_at": 9e12, "scope": gmail.SCOPE})
    return cfg


def cv_library_pages(monkeypatch):
    """CV-Library job pages for the alert's two links, served to intake's own fetcher (C2); a 404 for anything else."""
    seen = []
    pages = {"/job/219034011": (FIX / "intake" / "cvlibrary_job.html").read_text(encoding="utf-8")}

    def get(url, **kw):
        seen.append(url)
        for part, html in pages.items():
            if url.endswith(part):
                return type("R", (), {"status_code": 200, "ok": True, "text": html, "content": html.encode(), "headers": {}})()
        return type("R", (), {"status_code": 404 if "robots" not in url else 404, "ok": False, "text": "", "content": b"", "headers": {}})()
    monkeypatch.setattr(intake, "_get", get)
    monkeypatch.setattr(intake, "_sleep", lambda s: None)
    intake._last.clear()
    intake._default_robots.memory.clear()
    return seen


def test_alerts_are_resolved_through_the_employers_board_cv_library_and_reed_and_the_rest_is_queued(signed_in, db, monkeypatch):
    cfg = signed_in
    monkeypatch.setenv("REED_API_KEY", "k")
    cv_seen = cv_library_pages(monkeypatch)
    net = gmail_net({"api/1.0/jobs/5001": fixture_json("reed_job_5001.json"), "api/1.0/jobs/5555": {"jobId": 0}, "boards/acme/jobs": fixture_json("gh_jobs_acme.json"),
                     "boards/acme": fixture_json("gh_board_acme.json")})
    d = make(cfg, db, net)
    d.from_gmail()
    written = {(c.company, c.title, c.source) for c, _ in d.run.written}
    assert ("Acme Analytics Ltd", "Data Scientist", "greenhouse") in written                   # LinkedIn -> Acme's own board
    assert ("Brightwell Analytics Ltd", "Data Scientist", "cvlibrary") in written              # CV-Library link -> C2
    assert ("Birch Health", "Data Scientist", "reed") in written                               # Reed link -> the Reed API
    rows = disc.read_manual_queue(cfg.path("jds") / "manual_queue.csv")
    by_title = {(r["source"], r["title"]): r for r in rows}
    assert by_title[("linkedin", "Senior AI Engineer")]["link"] == "https://www.linkedin.com/jobs/view/3999999999"
    assert "no public job board known for Unknown Startup Ltd" in by_title[("linkedin", "Senior AI Engineer")]["reason"]
    assert ("indeed", "Machine Learning Engineer") in by_title and by_title[("indeed", "Machine Learning Engineer")]["link"].startswith("https://uk.indeed.com/viewjob?jk=")
    assert ("reed", "AI Engineer") in by_title and "no ad" in by_title[("reed", "AI Engineer")]["reason"]
    assert ("cvlibrary", "Machine Learning Engineer") in by_title                                # its page was a 404
    assert not any(k[1] in ("Retail Store Manager", "Data Analyst") for k in by_title)         # outside the role families: not queued
    c = d.run.sources["gmail"]
    assert c["alerts_linkedin"] == 3 and c["alerts_cvlibrary"] == 2 and c["alerts_indeed"] == 2 and c["alerts_reed"] == 2
    assert c["duplicate"] >= 1                                                                 # Indeed's Data Scientist at Acme was already written
    # nothing from linkedin.com, indeed.com or their trackers was ever fetched, by either fetcher
    assert not [u for u in net.urls(True) + cv_seen if any(h in u for h in ("linkedin.com", "indeed.com", "indeed.co.uk"))]
    assert all(h in ("gmail.googleapis.com", "boards-api.greenhouse.io", "www.reed.co.uk", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com")
               for h in net.hosts())


def test_a_second_run_does_not_read_the_same_emails_or_queue_the_same_links_again(signed_in, db, monkeypatch):
    cfg = signed_in
    monkeypatch.delenv("REED_API_KEY", raising=False)
    cv_library_pages(monkeypatch)
    net = gmail_net({"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    make(cfg, db, net).from_gmail()
    q1 = (cfg.path("jds") / "manual_queue.csv").read_text(encoding="utf-8")
    gets = [u for u in net.urls() if "/messages/m" in u]
    d2 = make(cfg, db, net)
    d2.from_gmail()
    assert [u for u in net.urls() if "/messages/m" in u] == gets                                # no message was fetched again
    assert (cfg.path("jds") / "manual_queue.csv").read_text(encoding="utf-8") == q1
    assert json.loads((cfg.path("discover") / "gmail_seen.json").read_text(encoding="utf-8")) == sorted(["m1", "m2", "m3", "m4", "m5"])


def test_gmail_without_a_sign_in_is_a_note_that_says_how(cfg, db):
    d = make(cfg, db, Net())
    d.from_gmail()
    assert any("--gmail-auth" in n or "gmail_client.json" in n for n in d.run.notes)


def test_gmail_is_asked_for_nothing_but_gets(signed_in, db, monkeypatch):
    cv_library_pages(monkeypatch)
    net = gmail_net()
    make(signed_in, db, net).from_gmail()
    assert all(r["url"].startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages") and r["headers"]["Authorization"] == "Bearer at"
               for r in net.requests if "gmail" in r["url"])
    assert not any(k in r["url"] for r in net.requests for k in ("/send", "/modify", "/trash", "/delete", "/labels"))


# ------------------------------------------------------------------ the manual queue
def test_the_manual_queue_is_append_only_writes_its_header_once_and_skips_a_link_it_has(cfg):
    path = cfg.path("jds") / "manual_queue.csv"
    assert disc.queue_manual(cfg, "linkedin", "A", "AI Engineer", "London", "https://www.linkedin.com/jobs/view/1", "no board", now=TODAY)
    assert disc.queue_manual(cfg, "indeed", "B", "Data Scientist", "Leeds", "https://uk.indeed.com/viewjob?jk=ab", "no board", now=TODAY)
    first = path.read_text(encoding="utf-8")
    assert not disc.queue_manual(cfg, "linkedin", "A", "AI Engineer", "London", "https://www.linkedin.com/jobs/view/1", "again", now=TODAY)
    assert path.read_text(encoding="utf-8") == first and first.count("ts,source,company") == 1
    assert [r["company"] for r in disc.read_manual_queue(path)] == ["A", "B"]
    assert disc.queue_manual(cfg, "x", "C", "T", "", "", "no link", now=TODAY) and not disc.queue_manual(cfg, "x", "C", "T", "", "", "no link", now=TODAY)


# ------------------------------------------------------------------ the report
def test_the_report_counts_per_source_resolved_and_queued(cfg, db):
    d = make(cfg, db, gh_net())
    d.from_boards()
    disc.queue_manual(cfg, "linkedin", "A", "AI Engineer", "London", "https://www.linkedin.com/jobs/view/1", "no board", now=TODAY)
    d.run.count("gmail", "resolved", 4)
    disc.save_run(cfg, d.run)
    text = disc.format_report(cfg)
    assert "Last run (" in text and "All 1 run(s):" in text
    assert "ats" in text and "written 3" in text and "role_filtered 1" in text and "place_filtered 2" in text
    assert "Resolved to a full ad: 4 · in the manual queue: 1 (linkedin 1)" in text and "careerops live --clipboard" in text


def test_the_report_with_no_runs_says_so(cfg):
    assert "No discover run recorded yet" in disc.format_report(cfg)


# ------------------------------------------------------------------ the command
@pytest.fixture
def cmd(cfg, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)

    def run(net, *argv):
        from careerops.discovery import http
        monkeypatch.setattr(http.requests, "get", lambda url, **kw: net.get(url, **kw))
        monkeypatch.setattr(http.time, "sleep", lambda s: None)
        cli.main(["discover", *argv])
    return run


def test_discover_ats_writes_the_inbox_prints_the_ranking_and_records_the_run(cmd, cfg, capsys):
    cmd(gh_net(), "--ats")
    out = capsys.readouterr().out
    assert "discover: ats ..." in out and "written 3" in out and "3 new ad(s) written to" in out and "best first:" in out
    assert "Acme Analytics Ltd | Data Scientist | London, UK" in out and "Next: careerops live" in out
    assert len(inbox_files(cfg)) == 3 and len(disc.read_runs(cfg)) == 1
    cmd(gh_net(), "--report")
    assert "ats" in capsys.readouterr().out


def test_discover_dry_run_writes_nothing_and_records_nothing(cmd, cfg, capsys):
    cmd(gh_net(), "--ats", "--dry-run")
    out = capsys.readouterr().out
    assert "3 new ad(s) would be written" in out and inbox_files(cfg) == [] and disc.read_runs(cfg) == []


def test_discover_limit(cmd, cfg):
    cmd(gh_net(), "--ats", "--limit", "2")
    assert len(inbox_files(cfg)) == 2


def test_one_source_failing_does_not_lose_the_others(cmd, cfg, capsys, monkeypatch):
    monkeypatch.delenv("REED_API_KEY", raising=False)
    cmd(gh_net(), "--reed", "--ats")
    out = capsys.readouterr().out
    assert "REED_API_KEY is not set" in out and len(inbox_files(cfg)) == 3


def test_discover_without_a_source_flag_keeps_its_older_job(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli.sources, "fetch_all", lambda s: iter([]))
    monkeypatch.setattr(cli, "triage_new", lambda db, s: {"triaged": 0})
    cli.main(["discover"])
    assert "Fetched 0 postings, 0 new." in capsys.readouterr().out


def test_gmail_auth_command_reports_a_missing_client_file_as_an_error(cmd, cfg, capsys):
    with pytest.raises(SystemExit) as e:
        cmd(Net(), "--gmail-auth")
    assert e.value.code == 1 and "gmail_client.json not found" in capsys.readouterr().err
