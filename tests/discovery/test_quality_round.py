"""Discovery quality round: alert companies resolved to a board by token (title match 0.8, 30-day cache, resolved % per source), R counted
from the day we first saw a job (an open posting keeps R >= 0.5), the manual queue kept to the best N of a run with the rest in an overflow
file, and the audit of every title the role / place filters turned away. No network: `Net` answers every request."""
import base64
import csv
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from careerops.config import ROOT
from careerops.discovery import candidate as cand, discover as disc, firstseen, gmail, manual, priority as prio, register as sponsors
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net, fixture_json

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def make(cfg, db, net, today=TODAY, **kw):
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(cfg, db, f, today=today, say=lambda s: None, **kw)


@pytest.fixture
def cfg(settings):
    shutil.copy(ROOT / "config" / "sources.yaml", Path(settings.raw["paths"]["sources"]))
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=20, sources=[], listed_only={"enabled": False})
    settings.raw["discover"]["gmail"]["senders"] = []
    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "at", "expires_at": 9e12, "scope": gmail.SCOPE})
    return settings


def linkedin_alert(*jobs: tuple[str, str, str]) -> str:
    """A LinkedIn alert email with these (title, company, place) jobs."""
    rows = "".join(
        f'<tr><td><a href="https://www.linkedin.com/comm/jobs/view/{3900000000 + i}?trackingId=x">{title}</a>'
        f"<p>{company} · {place}</p></td></tr>" for i, (title, company, place) in enumerate(jobs))
    return f"<html><body><table>{rows}</table></body></html>"


def mail_net(mails: dict, extra=None) -> Net:
    """mails: {id: (from, subject, html, internalDate in ms or None)}"""
    net = Net(strict=False)
    for mid, (frm, subject, html, date_ms) in mails.items():
        body = {"data": base64.urlsafe_b64encode(html.encode()).decode()}
        msg = {"id": mid, "payload": {"mimeType": "text/html", "body": body, "headers": [{"name": "From", "value": frm},
                                                                                        {"name": "Subject", "value": subject}]}}
        if date_ms is not None:
            msg["internalDate"] = str(date_ms)
        net.add(f"/messages/{mid}", msg)
    net.add("gmail.googleapis.com/gmail/v1/users/me/messages", {"messages": [{"id": m} for m in mails]})
    for part, reply in reversed(list((extra or {}).items())):
        net.routes.insert(0, (part, reply))
    return net


LINKEDIN = "LinkedIn <jobalerts-noreply@linkedin.com>"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def board(*postings: tuple[str, str, str], published="2026-10-08T09:00:00-04:00") -> dict:
    """A Greenhouse jobs reply: (id, title, place) postings, each with a long enough ad."""
    text = "&lt;p&gt;" + "We build forecasting tools for retailers and need people who like data. " * 12 + "&lt;/p&gt;"
    return {"jobs": [{"id": i, "title": title, "location": {"name": place}, "updated_at": published, "first_published": published,
                      "absolute_url": f"https://job-boards.greenhouse.io/x/jobs/{i}", "content": text} for i, title, place in postings]}


def gh(slug: str, name: str, *postings, **kw) -> dict:
    """Routes for one Greenhouse board; the jobs route first, since the first part found in a URL wins."""
    return {f"boards/{slug}/jobs": board(*postings, **kw), f"boards/{slug}": {"name": name, "content": ""}}


def inbox_text(cfg, fragment):
    (path,) = [p for p in (cfg.path("jds") / "inbox").glob("*.txt") if fragment in p.name]
    return path.read_text(encoding="utf-8")


def rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8", newline=""))) if path.exists() else []


# ------------------------------------------------------------------ 1. the resolver
def test_an_alerts_company_gets_up_to_four_tokens_asked_of_greenhouse_lever_and_ashby_and_its_posting_is_found(cfg, db):
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", linkedin_alert(("Data Scientist", "Maple Analytics Ltd", "London, England, United Kingdom")), None)},
                   gh("maple", "Maple Analytics", ("7", "Data Scientist", "London, UK")))
    d = make(cfg, db, net)
    d.from_gmail()
    assert [(c.company, c.title, c.board) for c, _ in d.run.written] == [("Maple Analytics Ltd", "Data Scientist", "greenhouse:maple")]
    asked = [u for u in net.urls() if "maple" in u and "/messages" not in u]
    for token in ("mapleanalytics", "maple-analytics"):                       # a miss on all four providers before the next token
        assert len([u for u in asked if u.endswith(token + "/jobs") or u.endswith("/" + token) or u.endswith("/" + token + "?mode=json")]) == 4
    assert any(u.endswith("boards/maple/jobs") for u in asked)
    (finding,) = d.lookup.findings
    assert finding.tokens == ["mapleanalytics", "maple-analytics", "maple"] and [b.ats for b in finding.boards] == ["greenhouse"]


def test_hits_and_misses_are_remembered_for_thirty_days_across_runs(cfg, db):
    html = linkedin_alert(("Data Scientist", "Maple Analytics Ltd", "London"), ("AI Engineer", "Unknown Startup Ltd", "Leeds"))
    routes = gh("maple", "Maple Analytics", ("7", "Data Scientist", "London, UK"))
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None)}, routes)
    make(cfg, db, net).from_gmail()
    assert (cfg.path("discover") / "boards.json").exists()
    (cfg.path("discover") / "gmail_seen.json").unlink()                       # read the same mail again
    shutil.rmtree(cfg.path("jds") / "inbox")
    (cfg.path("discover") / "boards").mkdir(exist_ok=True)
    for f in (cfg.path("discover") / "boards").glob("*"):
        f.unlink()                                                            # and the 6-hour posting lists are gone: only boards.json remains
    net2 = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None)}, routes)
    make(cfg, db, net2).from_gmail()
    again = [u for u in net2.urls() if "/messages" not in u]
    assert not [u for u in again if "unknown" in u]                           # a miss is not asked again
    assert not [u for u in again if "mapleanalytics" in u or "maple-analytics" in u]
    assert any(u.endswith("boards/maple/jobs") for u in again)                # a hit goes straight to the board


@pytest.mark.parametrize("alert, posting, place, matched", [
    ("Data Scientist", "Data Scientist", "London", True),
    ("Data Scientist (Hybrid)", "Data Scientist", "London", True),
    ("Machine Learning Engineer", "Machine Learning Engineers", "London", True),             # 0.98
    ("Data Scientist", "Data Scientist, Pricing", "London", False),                           # 0.78: under 0.8
    ("Data Scientist", "Senior Data Scientist", "London", False),                             # exactly 0.8, but another level
    ("Senior Data Scientist", "Data Scientist", "London", False),
    ("AI Engineer", "Data Scientist", "London", False)])
def test_an_alert_title_matches_a_posting_at_0_8_similarity_and_the_same_level(cfg, db, alert, posting, place, matched):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "maple", "name": "Maple Analytics Ltd"}]
    net = Net(strict=False)
    for part, reply in gh("maple", "Maple Analytics", ("7", posting, "London, UK")).items():
        net.add(part, reply)
    found = make(cfg, db, net).lookup.find("Maple Analytics Ltd", alert, place)
    assert (found is not None) is matched


def test_the_title_threshold_is_a_setting(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "maple", "name": "Maple Analytics Ltd"}]
    net = Net(strict=False)
    for part, reply in gh("maple", "Maple Analytics", ("7", "Data Scientist, Pricing", "London, UK")).items():
        net.add(part, reply)
    assert make(cfg, db, net).lookup.find("Maple Analytics Ltd", "Data Scientist") is None
    cfg.raw["discover"]["title_match"] = 0.75
    assert make(cfg, db, net).lookup.find("Maple Analytics Ltd", "Data Scientist") is not None


def test_a_board_found_by_a_guessed_token_must_be_the_companys_not_another_with_a_similar_token(cfg, db):
    html = linkedin_alert(("Data Scientist", "Arc Analytics", "London"))
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None)}, gh("arc", "Arc Capital Partners", ("1", "Data Scientist", "London")))
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.written == []
    (row,) = rows(cfg.path("jds") / "manual_queue.csv")
    assert row["company"] == "Arc Analytics" and "no posting titled like this" in row["reason"]


def test_a_guessed_boards_posting_in_another_town_than_the_alerts_is_not_the_alerts_job(cfg, db):
    html = linkedin_alert(("Data Scientist", "Maple Analytics", "Manchester, England"))
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None)}, gh("maple", "Maple Analytics", ("7", "Data Scientist", "London, UK")))
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.written == [] and len(rows(cfg.path("jds") / "manual_queue.csv")) == 1


def test_the_per_run_limit_of_companies_looked_for_is_said_in_the_queue_reason(cfg, db):
    cfg.raw["discover"]["alert_probes_per_run"] = 1
    html = linkedin_alert(("Data Scientist", "First Co", "London"), ("Data Scientist", "Second Co", "London"))
    d = make(cfg, db, mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None)}))
    d.from_gmail()
    reasons = {r["company"]: r["reason"] for r in rows(cfg.path("jds") / "manual_queue.csv")}
    assert "no public job board known for First Co" in reasons["First Co"]
    assert "alert_probes_per_run" in reasons["Second Co"]


def test_resolved_percent_is_reported_per_source_for_the_run_and_over_all_runs(cfg, db):
    glassdoor = (FIX / "discover" / "alert_glassdoor.html").read_text(encoding="utf-8")
    html = linkedin_alert(("Data Scientist", "Maple Analytics Ltd", "London"), ("AI Engineer", "Unknown Startup Ltd", "Leeds"),
                          ("Data Scientist", "Other Co", "Leeds"), ("Applied Scientist", "Third Co", "Leeds"))
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, None), "m2": ("Glassdoor <noreply@glassdoor.co.uk>", "Data Scientist jobs", glassdoor, None)},
                   {**gh("maple", "Maple Analytics", ("7", "Data Scientist", "London, UK")),
                    **{"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")}})
    d = make(cfg, db, net)
    d.from_gmail()
    c = d.run.by_source
    assert (c["linkedin"]["resolved"], c["linkedin"]["unresolved"]) == (1, 3)
    assert (c["glassdoor-uk"]["resolved"], c["glassdoor-uk"]["unresolved"]) == (1, 1)
    assert disc.resolution_lines(c, "T:")[:3] == ["T:", "  glassdoor-uk     1 of 2 (50%)", "  linkedin         1 of 4 (25%)"]
    disc.save_run(cfg, d.run)
    text = disc.format_report(cfg)
    assert "last run:" in text and "linkedin         1 of 4 (25%)" in text and "all 1 run(s):" in text


def test_the_shipped_defaults_are_the_agreed_ones(settings):
    assert settings["discover"]["title_match"] == 0.8 and settings["discover"]["manual_queue_max"] == 10
    assert settings["discover"]["ats"]["board_cache_days"] == 30 and settings["priority"]["open_listing_floor"] == 0.5


# ------------------------------------------------------------------ 2. recency: counted from the day we first saw the job
ANAPLAN = ("1", "Data Scientist", "London, UK")


def anaplan_net(published):
    net = Net(strict=False)
    for part, reply in gh("acme", "Acme Analytics Ltd", ANAPLAN, published=published).items():
        net.add(part, reply)
    return net


def test_a_64_day_old_posting_that_is_still_open_has_r_at_least_0_5_because_r_counts_from_the_day_we_saw_it(cfg, db, fb_fixture):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    published = (TODAY - timedelta(days=64)).isoformat()
    d = make(cfg, db, anaplan_net(published), fb=fb_fixture)
    d.from_boards()
    (pr, c, path), = d.ranked()
    assert c.posted.startswith(published[:10]) and c.first_seen == "2026-10-10" and c.open_listing
    assert pr.r >= 0.5 and pr.days == pytest.approx(0.5, abs=0.01)             # first seen today, half a day before noon
    text = path.read_text(encoding="utf-8")
    assert "Posted: 2026-08-07" in text and "First seen: 2026-10-10" in text and "Listed: yes" in text


def test_the_inbox_files_priority_counts_from_its_first_seen_header_not_its_posted_one(cfg, db, fb_fixture):
    from careerops.discovery import priority
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    d = make(cfg, db, anaplan_net((TODAY - timedelta(days=64)).isoformat()), fb=fb_fixture)
    d.from_boards()
    (_, path), = d.run.written
    job = priority.file_job(path, cfg, fb_fixture, None)
    assert job["seen_on"] == "2026-10-10" and job["open_listing"] is True and job["posted_at"] == "2026-08-07"
    assert priority.priority(job, cfg, tailored=False, today=TODAY).r > 0.9


def test_a_job_seen_64_days_ago_and_still_listed_stays_at_the_floor_and_one_that_is_not_listed_decays(cfg):
    seen = {"seen_on": (TODAY - timedelta(days=64)).date().isoformat(), "sponsor_match": "exact", "ceiling_pre": 0.8, "description": "x"}
    listed = prio.priority({**seen, "open_listing": 1}, cfg, tailored=False, today=TODAY)
    assert listed.r == 0.5 and listed.floored and "still listed on the employer's board" in listed.line()
    gone = prio.priority(seen, cfg, tailored=False, today=TODAY)
    assert gone.r < 0.02 and not gone.floored
    fresh = prio.priority({**seen, "seen_on": TODAY.date().isoformat(), "open_listing": 1}, cfg, tailored=False, today=TODAY)
    assert fresh.r > 0.95 and not fresh.floored                                # the floor only ever raises


def test_the_floor_is_a_setting(cfg):
    cfg.raw["priority"]["open_listing_floor"] = 0.3
    job = {"seen_on": "2026-01-01", "open_listing": 1}
    assert prio.recency_factor(job, cfg, TODAY)[0] == 0.3


def test_without_a_first_seen_day_posted_at_is_still_what_r_counts_from(cfg):
    r, days = prio.recency_factor({"posted_at": (TODAY - timedelta(days=14)).isoformat()}, cfg, TODAY)
    assert days == pytest.approx(14) and r == pytest.approx(0.368, abs=0.001)


def test_an_alerts_own_date_is_the_first_sighting_when_it_is_older_than_the_run(cfg, db, fb_fixture):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "maple", "name": "Maple Analytics Ltd"}]
    arrived = TODAY - timedelta(days=5)
    html = linkedin_alert(("Data Scientist", "Maple Analytics Ltd", "London"))
    net = mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, ms(arrived))}, gh("maple", "Maple Analytics", ("7", "Data Scientist", "London, UK")))
    d = make(cfg, db, net, fb=fb_fixture)
    d.from_gmail()
    (pr, c, path), = d.ranked()
    assert c.first_seen == "2026-10-05" and "First seen: 2026-10-05" in path.read_text(encoding="utf-8")
    assert pr.days == pytest.approx(5.5, abs=0.01) and 0.6 < pr.r < 0.7 and c.open_listing


def test_the_first_run_that_saw_a_job_is_remembered_even_when_the_file_is_gone(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    make(cfg, db, anaplan_net("2026-08-01T00:00:00Z")).from_boards()
    assert json.loads((cfg.path("discover") / "first_seen.json").read_text(encoding="utf-8")) == {
        "acmeanalytics|data scientist|london": "2026-10-10"}
    shutil.rmtree(cfg.path("jds") / "inbox")                                  # deleted, so the job is new to the inbox again
    later = make(cfg, db, anaplan_net("2026-08-01T00:00:00Z"), today=TODAY + timedelta(days=30))
    later.from_boards()
    (c, _), = later.run.written
    assert c.first_seen == "2026-10-10"                                       # not the day of this run


def test_a_dry_run_records_no_first_sighting(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    make(cfg, db, anaplan_net("2026-08-01T00:00:00Z"), dry_run=True).from_boards()
    assert not (cfg.path("discover") / "first_seen.json").exists()


def test_first_seen_keeps_the_earliest_day_and_forgets_the_oldest_past_its_cap(cfg, monkeypatch):
    fs = firstseen.FirstSeen(cfg)
    assert fs.record("Acme Ltd", "Data Scientist", "London, UK", "2026-10-10") == "2026-10-10"
    assert fs.record("ACME", "Data Scientist (m/f/d)", "London", "2026-10-12") == "2026-10-10"       # the same job, seen again later
    assert fs.record("Acme", "Data Scientist", "London", "2026-10-01") == "2026-10-01"               # an older alert turns up
    fs.save()
    assert list(firstseen.FirstSeen(cfg).data.values()) == ["2026-10-01"]
    monkeypatch.setattr(firstseen, "MAX_KEYS", 2)
    for n, day in enumerate(("2026-09-01", "2026-09-02", "2026-09-03")):
        fs.record(f"Co{n}", "Data Scientist", "", day)
    fs.save()
    assert sorted(firstseen.FirstSeen(cfg).data.values()) == ["2026-09-03", "2026-10-01"]      # the two newest sightings stay


@pytest.mark.parametrize("value, expected", [(TODAY, "2026-10-10"), ("2026-10-08T09:00:00-04:00", "2026-10-08"), ("2026-10-08", "2026-10-08"),
                                             ("", ""), (None, ""), ("garbage", ""), ("2026-13-45", "")])
def test_day_reads_a_date_or_nothing(value, expected):
    assert firstseen.day(value) == expected


def test_message_date_is_gmails_internal_date_else_the_date_header_else_nothing():
    assert gmail.message_date({"internalDate": str(ms(TODAY))}) == "2026-10-10"
    hdr = {"payload": {"headers": [{"name": "Date", "value": "Tue, 06 Oct 2026 23:30:00 -0400"}]}}
    assert gmail.message_date(hdr) == "2026-10-07"                                            # UTC
    assert gmail.message_date({"internalDate": "x", **hdr}) == "2026-10-07"
    assert gmail.message_date({}) == "" and gmail.message_date({"payload": {"headers": [{"name": "Date", "value": "no"}]}}) == ""


def test_add_jd_keeps_the_first_seen_day_and_the_open_flag_and_priority_reads_them_from_the_row(cfg, db):
    jid = manual.add_jd(db, "x" * 600, company="Acme", title="Data Scientist", url="https://a.example/1", posted="2026-08-07",
                        seen_on="2026-08-10", open_listing=True)
    row = db.job(jid)
    assert (row["posted_at"], row["seen_on"], row["open_listing"]) == ("2026-08-07", "2026-08-10", 1)
    assert prio.priority(row, cfg, tailored=False, today=TODAY).r == 0.5               # 61 days since first seen, but still listed
    plain = manual.add_jd(db, "x" * 600, company="Beta", title="Data Scientist", url="https://b.example/1")
    assert db.job(plain)["seen_on"] is None and not db.job(plain)["open_listing"]


# ------------------------------------------------------------------ 3. the manual queue: the best N of a run, the rest in an overflow file
ALERTS = [("Data Analyst", "Nova Ltd"), ("Data Scientist", "Orbit Ltd"), ("Data Analyst", "Northwind Health AI"),
          ("Senior Data Scientist", "Pylon Ltd"), ("Machine Learning Engineer", "Quark Ltd")]


def alert_net(alerts=ALERTS, date_ms=None):
    html = linkedin_alert(*[(title, company, "London") for title, company in alerts])
    return mail_net({"m1": (LINKEDIN, "Data Scientist jobs", html, date_ms)})


def test_title_relevance_follows_the_role_tier_and_halves_for_an_excluded_level(cfg):
    rel = lambda t: cand.title_relevance(t, cfg)  # noqa: E731
    assert rel("Data Scientist") == rel("AI Engineer") == 1.0
    assert rel("Machine Learning Engineer") == pytest.approx(0.8) and rel("Data Analyst") == pytest.approx(0.6)
    assert rel("Senior Data Scientist") == 0.5 and rel("Retail Store Manager") == 0.1
    assert rel("Data Scientist") > rel("Machine Learning Engineer") > rel("Data Analyst") > rel("Senior Data Scientist") > rel("Retail Store Manager")


def test_a_run_keeps_its_best_n_by_title_relevance_times_s_and_writes_the_rest_to_the_overflow_file(cfg, db):
    db.replace_sponsors(sponsors.parse(cfg.path("sponsors_csv")))
    cfg.raw["discover"]["manual_queue_max"] = 3
    d = make(cfg, db, alert_net())
    d.from_gmail()
    queue, over = rows(cfg.path("jds") / "manual_queue.csv"), rows(cfg.path("jds") / "manual_queue_overflow.csv")
    # Orbit 1.0 x 0.75 | Northwind 0.6 x 1.0 (on the register) | Quark 0.8 x 0.75 | Nova 0.6 x 0.75 | Pylon 0.5 x 0.75
    assert [r["company"] for r in queue] == ["Orbit Ltd", "Northwind Health AI", "Quark Ltd"]
    assert [(r["company"], r["score"]) for r in over] == [("Nova Ltd", "0.45"), ("Pylon Ltd", "0.375")]
    assert list(over[0]) == [*disc.MANUAL_COLUMNS, "score"] and list(queue[0]) == disc.MANUAL_COLUMNS
    c = d.run.sources["gmail"]
    assert (c["to_manual_queue"], c["to_manual_queue_overflow"]) == (3, 2)


def test_the_default_keeps_ten_and_a_second_read_of_the_same_links_adds_nothing_to_either_file(cfg, db):
    assert cfg["discover"]["manual_queue_max"] == 10
    twelve = [("Data Scientist", f"Company {chr(65 + i)} Ltd") for i in range(12)]
    d = make(cfg, db, alert_net(twelve))
    d.from_gmail()
    q, o = cfg.path("jds") / "manual_queue.csv", cfg.path("jds") / "manual_queue_overflow.csv"
    assert (len(rows(q)), len(rows(o))) == (10, 2)
    before = (q.read_text(encoding="utf-8"), o.read_text(encoding="utf-8"))
    (cfg.path("discover") / "gmail_seen.json").unlink()                                       # the same mail is read again
    d2 = make(cfg, db, alert_net(twelve))
    d2.from_gmail()
    assert (q.read_text(encoding="utf-8"), o.read_text(encoding="utf-8")) == before


def test_a_dry_run_ranks_and_counts_the_queue_but_writes_neither_file(cfg, db):
    cfg.raw["discover"]["manual_queue_max"] = 1
    d = make(cfg, db, alert_net(), dry_run=True)
    d.from_gmail()
    assert (d.run.sources["gmail"]["to_manual_queue"], d.run.sources["gmail"]["to_manual_queue_overflow"]) == (1, 4)
    assert not (cfg.path("jds") / "manual_queue.csv").exists() and not (cfg.path("jds") / "manual_queue_overflow.csv").exists()


def test_an_alert_email_nothing_could_be_read_from_ranks_as_a_half_relevant_title_not_last(cfg, db):
    cfg.raw["discover"]["manual_queue_max"] = 2
    nolinks = (FIX / "discover" / "alert_nolinks.html").read_text(encoding="utf-8")
    html = linkedin_alert(("Data Analyst", "Nova Ltd", "London"), ("Senior Data Scientist", "Pylon Ltd", "London"))
    net = mail_net({"m2": ("Glassdoor <noreply@glassdoor.co.uk>", "Your job alert", nolinks, None), "m1": (LINKEDIN, "Data Scientist jobs", html, None)})
    d = make(cfg, db, net)                                      # 0.5 x 0.75 each: a tie is broken by arrival, the email came first
    d.from_gmail()
    assert sorted(r["company"] or r["source"] for r in rows(cfg.path("jds") / "manual_queue.csv")) == ["Nova Ltd", "glassdoor-uk"]
    assert [r["company"] for r in rows(cfg.path("jds") / "manual_queue_overflow.csv")] == ["Pylon Ltd"]


# ------------------------------------------------------------------ 4. the role filter audit
def acme_net():
    net = Net(strict=False)
    net.add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))
    return net


def test_every_title_the_filters_turn_away_is_written_with_its_reason(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    d = make(cfg, db, acme_net())
    d.from_boards()
    got = {(r["title"], r["location"]): r for r in rows(disc.filtered_path(cfg))}
    assert list(rows(disc.filtered_path(cfg))[0]) == disc.FILTERED_COLUMNS
    role = got[("Marketing Manager", "London, UK")]
    assert role["filter"] == "role" and role["company"] == "Acme Analytics Ltd" and role["source"] == "greenhouse"
    assert role["reason"] == "no roles.families title pattern matches" and role["link"].endswith("/102")
    place = got[("AI Engineer", "New York, NY")]
    assert place["filter"] == "place" and "not in the UK or remote UK" in place["reason"] and "United States" in place["reason"]
    assert d.run.sources["ats"]["role_filtered"] == 1 and d.run.sources["ats"]["place_filtered"] == 2


def test_a_title_that_only_just_misses_a_family_says_which_words_to_look_at(cfg, db):
    assert disc.role_reason("Research Scientist, Machine Learning") == ("no roles.families title pattern matches "
                                                                       "(title mentions: machine learning, scien)")
    assert disc.role_reason("Marketing Manager") == "no roles.families title pattern matches"


def test_a_title_is_written_once_however_many_runs_see_it(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    make(cfg, db, acme_net()).from_boards()
    first = disc.filtered_path(cfg).read_text(encoding="utf-8")
    make(cfg, db, acme_net(), today=TODAY + timedelta(days=1)).from_boards()
    assert disc.filtered_path(cfg).read_text(encoding="utf-8") == first


def test_a_dry_run_writes_no_audit_file(cfg, db):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "acme", "name": "Acme Analytics Ltd"}]
    make(cfg, db, acme_net(), dry_run=True).from_boards()
    assert not disc.filtered_path(cfg).exists()


def test_alert_titles_outside_the_families_are_audited_with_the_alerts_source(cfg, db):
    d = make(cfg, db, alert_net([("Retail Store Manager", "Shopco"), ("Data Scientist", "Orbit Ltd")]))
    d.from_gmail()
    (row,) = rows(disc.filtered_path(cfg))
    assert (row["source"], row["company"], row["title"], row["filter"]) == ("linkedin", "Shopco", "Retail Store Manager", "role")
    assert row["link"].startswith("https://www.linkedin.com/jobs/view/")
