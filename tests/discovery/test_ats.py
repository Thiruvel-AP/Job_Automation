"""discovery/ats.py (M3): slug candidates, probing the three public job-board APIs, verification, resolving the companies table, the
careers-page detector and the board lists discover reads. No network: `Net` answers."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from careerops.discovery import ats
from careerops.discovery.ats import Board, Hit
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, Resp, fixture_json


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


# ------------------------------------------------------------------ slug candidates
def test_slug_candidates_for_a_bank():
    assert ats.slug_candidates("Monzo Bank Ltd") == ["monzobank", "monzo-bank", "monzo"]


def test_stop_words_do_not_make_collisions_and_there_are_at_most_four_deduped_candidates():
    c = ats.slug_candidates("The Data Group Ltd")
    assert "group" not in c and c[:2] == ["datagroup", "data-group"]
    assert "global" not in ats.slug_candidates("Acme Global Ltd")
    many = ats.slug_candidates("Northwind Health Analytics Platform Limited", "Northwind Health Analytics Platform Ltd (CH)")
    assert len(many) <= 4 and len(set(many)) == len(many)
    assert ats.slug_candidates("Stripe Ltd") == ["stripe"]                      # one word: joined == hyphen == first
    assert ats.slug_candidates("Acme Ltd T/A Widgets") == ["acme"]                # the legal part before T/A


# ------------------------------------------------------------------ probe
def test_probe_reads_a_greenhouse_board_with_its_name_and_counts_uk_jobs():
    net = Net(strict=False).add("boards/acme/jobs", fixture_json("gh_jobs_acme_plain.json")).add("boards/acme", fixture_json("gh_board_acme.json"))
    (hit,) = ats.probe("acme", fetcher=fetcher(net))
    assert (hit.ats, hit.slug, hit.board_name, hit.total_jobs) == ("greenhouse", "acme", "Acme Analytics Ltd", 6)
    assert hit.uk_jobs == 3                                    # counted from the location text: London x2 and Manchester


def test_probe_reads_lever_global_and_eu_and_ashby():
    net = (Net(strict=False).add("api.eu.lever.co", fixture_json("lever_acme.json")).add("api.lever.co", fixture_json("lever_acme.json"))
           .add("ashbyhq.com", fixture_json("ashby_acme.json")))
    hits = {h.ats: h for h in ats.probe("acme", fetcher=fetcher(net))}
    assert set(hits) == {"lever", "lever_eu", "ashby"} and hits["lever"].board_name is None
    assert (hits["lever"].uk_jobs, hits["lever"].total_jobs) == (2, 2)
    assert (hits["ashby"].uk_jobs, hits["ashby"].total_jobs) == (2, 3)          # addressCountry United Kingdom counts; Germany does not


@pytest.mark.parametrize("reply, status", [("", 404), ({"ok": False, "error": "Document not found"}, 200), ({"error": "no such board"}, 200),
                                           ([], 200), ({"jobs": []}, 200), ("denied", 400)])
def test_an_unknown_slug_in_any_of_its_forms_is_no_hit(reply, status):
    net = Net(strict=False).add("boards-api", reply, status).add("lever.co", reply, status).add("ashbyhq", reply, status)
    assert ats.probe("nope", fetcher=fetcher(net)) == []


def test_a_host_that_blocks_is_left_alone_and_the_others_are_still_asked():
    net = (Net(strict=False).add("boards-api.greenhouse.io", "no", 429).add("lever.co", fixture_json("lever_acme.json"))
           .add("ashbyhq.com", fixture_json("ashby_acme.json")))
    f = fetcher(net)
    hits = ats.probe("acme", fetcher=f)
    assert {h.ats for h in hits} == {"lever", "lever_eu", "ashby"} and f.backed_off == {"boards-api.greenhouse.io": 429}
    ats.probe("acme2", fetcher=f)
    assert sum("boards-api" in u for u in net.urls(True)) == 1               # the blocked host was asked once, ever


# ------------------------------------------------------------------ verify: one test per row of the decision table
@pytest.mark.parametrize("hit, company, full, expect", [
    (Hit("greenhouse", "monzo", "Monzo Ltd", 0, 10), "monzo", False, (True, "name_exact")),
    (Hit("greenhouse", "stripe", "Stripe", 3, 50), "stripe payments", False, (True, "name_prefix_uk")),
    (Hit("greenhouse", "stripe", "Stripe Payments UK", 3, 50), "stripe", False, (True, "name_prefix_uk")),
    (Hit("greenhouse", "stripe", "Stripe", 0, 50), "stripe payments", False, (False, "name_mismatch")),      # a prefix but no UK job
    (Hit("lever", "acmedata", None, 2, 5), "acme data", True, (True, "full_slug_uk")),
    (Hit("greenhouse", "apex", "Apex Fintech Solutions", 0, 40), "apex capital", False, (False, "name_mismatch")),   # same slug, other firm
    (Hit("lever", "acme", None, 4, 5), "acme data", False, (False, "weak")),                                   # short slug: unverified
    (Hit("ashby", "acmedata", None, 0, 5), "acme data", True, (False, "weak")),                                # full slug but no UK job
])
def test_verify_decision_table(hit, company, full, expect):
    assert ats.verify_rule(hit, company, full) == expect
    assert ats.verify(hit, company, full) is expect[0]


# ------------------------------------------------------------------ resolve
def add_company(db, name, *, first_seen="2026-10-01", rating="A", probe_status=None, last_probed=None, public_sector=0, sector=None,
                ch_status=None, removed_on=None, town="London"):
    from careerops.discovery.register import normalise
    db.conn.execute("INSERT INTO companies (norm, town_norm, register_name, town, rating, route, first_seen, removed_on, public_sector, "
                    "probe_status, last_probed, sector, ch_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (normalise(name), town.lower(), name, town, rating, "Skilled Worker", first_seen, removed_on, public_sector,
                     probe_status, last_probed, sector, ch_status))
    db.conn.commit()


def row(db, name):
    return db.conn.execute("SELECT * FROM companies WHERE register_name = ?", (name,)).fetchone()


def gh_net(name_for_slug: dict):
    """Greenhouse boards named as given for each slug; everything else unknown."""
    net = Net(strict=False)
    for slug, board_name in name_for_slug.items():
        net.add(f"boards/{slug}/jobs", fixture_json("gh_jobs_acme_plain.json"))
        net.add(f"boards/{slug}", {"name": board_name, "content": ""})
    return net


def test_resolve_verifies_by_board_name_stops_at_the_first_verified_hit_and_stores_the_evidence(db, settings):
    add_company(db, "Acme Analytics Ltd")
    net = gh_net({"acmeanalytics": "Acme Analytics Ltd", "acme-analytics": "Acme Analytics Ltd"})
    st = ats.resolve(db, settings, fetcher=fetcher(net))
    assert (st.probed, st.verified, st.unverified, st.not_found) == (1, 1, 0, 0)
    r = row(db, "Acme Analytics Ltd")
    assert (r["ats"], r["ats_slug"], r["ats_verified"], r["probe_status"]) == ("greenhouse", "acmeanalytics", 1, "verified")
    ev = json.loads(r["ats_evidence"])
    assert ev["rule"] == "name_exact" and ev["board_name"] == "Acme Analytics Ltd" and ev["tried"] == ["acmeanalytics"] and ev["total_jobs"] == 6
    assert not any("acme-analytics" in u for u in net.urls())                    # later candidates were never asked
    assert str(st).startswith("probed 1 · verified 1 · unverified 0 · not found 0 · errors 0")


def test_resolve_rejects_a_board_of_another_company_and_marks_not_found(db, settings):
    add_company(db, "Apex Capital Ltd")
    st = ats.resolve(db, settings, fetcher=fetcher(gh_net({"apexcapital": "Apex Fintech Solutions", "apex-capital": "Apex Fintech Solutions",
                                                           "apex": "Apex Fintech Solutions"})))
    assert st.not_found == 1 and st.verified == 0
    r = row(db, "Apex Capital Ltd")
    assert (r["probe_status"], r["ats"], r["ats_verified"]) == ("not_found", None, 0) and r["last_probed"]


def test_resolve_keeps_a_weak_hit_as_unverified_for_a_human(db, settings):
    add_company(db, "Birchwood Data Ltd")
    net = Net(strict=False).add("api.lever.co", lambda url, **kw: Resp(fixture_json("lever_acme.json")) if url.endswith("/postings/birchwood") else Resp("", 404))
    st = ats.resolve(db, settings, fetcher=fetcher(net))
    r = row(db, "Birchwood Data Ltd")
    assert st.unverified == 1 and (r["probe_status"], r["ats"], r["ats_slug"], r["ats_verified"]) == ("unverified", "lever", "birchwood", 0)
    assert json.loads(r["ats_evidence"])["rule"] == "weak"


def test_resolve_goes_newest_sponsors_first_and_respects_the_limit(db, settings):
    add_company(db, "Oldest Co Ltd", first_seen="2026-01-01")
    add_company(db, "Newest Co Ltd", first_seen="2026-10-09")
    add_company(db, "Middle Co Ltd", first_seen="2026-06-01")
    ats.resolve(db, settings, limit=2, fetcher=fetcher(Net(strict=False)))
    probed = [r["register_name"] for r in db.conn.execute("SELECT register_name FROM companies WHERE probe_status IS NOT NULL ORDER BY last_probed, first_seen DESC")]
    assert sorted(probed) == ["Middle Co Ltd", "Newest Co Ltd"] and row(db, "Oldest Co Ltd")["probe_status"] is None


def test_resolve_skips_manual_public_sector_removed_and_b_rated_rows(db, settings):
    add_company(db, "Hand Picked Ltd", probe_status="manual")
    add_company(db, "City University", public_sector=1)
    add_company(db, "NHS Trust Ltd")                                             # the public_sector_pattern in settings
    add_company(db, "Gone Ltd", removed_on="2026-09-01")
    add_company(db, "Watched Ltd", rating="B")
    add_company(db, "Fine Ltd")
    st = ats.resolve(db, settings, fetcher=fetcher(Net(strict=False)))
    assert st.probed == 1 and row(db, "Fine Ltd")["probe_status"] == "not_found"
    assert row(db, "Hand Picked Ltd")["probe_status"] == "manual" and row(db, "Watched Ltd")["probe_status"] is None


def test_resolve_needs_a_sector_and_an_active_company_once_companies_house_has_been_joined(db, settings):
    add_company(db, "Joined Ltd", sector="software", ch_status="Active")
    add_company(db, "Dissolved Ltd", sector="software", ch_status="Dissolved")
    add_company(db, "Unjoined Ltd")
    st = ats.resolve(db, settings, fetcher=fetcher(Net(strict=False)))
    assert st.probed == 1 and row(db, "Joined Ltd")["probe_status"] == "not_found" and row(db, "Unjoined Ltd")["probe_status"] is None


def test_not_found_is_probed_again_only_after_the_configured_days_and_errors_always(db, settings):
    now = datetime.now(timezone.utc)
    add_company(db, "Recent Ltd", probe_status="not_found", last_probed=(now - timedelta(days=5)).isoformat())
    add_company(db, "Stale Ltd", probe_status="not_found", last_probed=(now - timedelta(days=45)).isoformat())
    add_company(db, "Errored Ltd", probe_status="error", last_probed=now.isoformat())
    add_company(db, "Verified Ltd", probe_status="verified", last_probed=(now - timedelta(days=400)).isoformat())
    st = ats.resolve(db, settings, fetcher=fetcher(Net(strict=False)))
    assert st.probed == 2
    assert row(db, "Recent Ltd")["last_probed"] == (now - timedelta(days=5)).isoformat()
    assert row(db, "Verified Ltd")["probe_status"] == "verified"
    only_new = ats.resolve(db, settings, only_new=True, fetcher=fetcher(Net(strict=False)))
    assert only_new.probed == 0


def test_an_interrupted_run_keeps_what_it_finished(db, settings):
    for n in ("Aaa Ltd", "Bbb Ltd", "Ccc Ltd"):
        add_company(db, n, first_seen={"Aaa Ltd": "2026-10-03", "Bbb Ltd": "2026-10-02", "Ccc Ltd": "2026-10-01"}[n])
    calls = []

    def boom(url, **kw):
        calls.append(url)
        if "ccc" in url:
            raise KeyboardInterrupt()
        return Resp("", 404)
    net = Net(strict=False).add("boards-api", boom).add("lever", boom).add("ashbyhq", boom)
    with pytest.raises(KeyboardInterrupt):
        ats.resolve(db, settings, fetcher=fetcher(net))
    assert row(db, "Aaa Ltd")["probe_status"] == "not_found" and row(db, "Bbb Ltd")["probe_status"] == "not_found"
    assert row(db, "Ccc Ltd")["probe_status"] is None


# ------------------------------------------------------------------ detect
@pytest.mark.parametrize("html, expect", [
    ('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>', ("greenhouse", "acme")),
    ('<a href="https://job-boards.greenhouse.io/acme/jobs/1">Jobs</a>', ("greenhouse", "acme")),
    ('<a href="https://jobs.lever.co/acme">Jobs</a>', ("lever", "acme")),
    ('<script src="https://jobs.ashbyhq.com/acme/embed"></script>', ("ashby", "acme")),
    ('<a href="https://acme.wd3.myworkdayjobs.com/en-US/Careers">x</a>', ("workday", "acme")),
    ("<p>Email us your CV.</p>", None)])
def test_detect_finds_each_signature_and_none_for_a_plain_page(html, expect):
    assert ats.detect("https://acme.example.com/careers", fetcher=fetcher(Net(strict=False).add("acme.example.com", html))) == expect


# ------------------------------------------------------------------ the postings of a board
def test_greenhouse_postings_have_their_text_location_and_link():
    net = Net(strict=False).add("boards/acme/jobs", fixture_json("gh_jobs_acme.json"))
    posts = ats.board_postings(Board("greenhouse", "acme", "Acme Analytics Ltd"), fetcher(net))
    assert len(posts) == 6 and posts[0].company == "Acme Analytics Ltd" and posts[0].location == "London, UK"
    assert "<" not in posts[0].description and "Python, SQL and scikit-learn" in posts[0].description and len(posts[0].description) > 400
    assert posts[0].url == "https://job-boards.greenhouse.io/acme/jobs/101" and net.requests[-1]["params"] == {"content": "true"}


def test_lever_and_ashby_postings():
    lever = ats.board_postings(Board("lever", "acme", "Acme"), fetcher(Net(strict=False).add("lever.co", fixture_json("lever_acme.json"))))
    assert (lever[0].title, lever[0].country, lever[0].posted_at[:4]) == ("AI Engineer", "GB", "2026")
    a = ats.board_postings(Board("ashby", "acme", "Acme"), fetcher(Net(strict=False).add("ashbyhq.com", fixture_json("ashby_acme.json"))))
    assert [p.listed for p in a] == [True, False, True] and a[0].country == "United Kingdom"
    eu = fetcher(Net(strict=False).add("lever.co", fixture_json("lever_acme.json")))
    ats.board_postings(Board("lever_eu", "acme", "Acme"), eu)
    assert eu._get.__self__.urls()[0].startswith("https://api.eu.lever.co/")


def test_a_board_that_does_not_exist_has_no_postings():
    assert ats.board_postings(Board("greenhouse", "nope", "Nope"), fetcher(Net(strict=False).add("boards-api", "", 404))) == []


# ------------------------------------------------------------------ the boards discover reads
def test_configured_boards_come_from_sources_with_the_names_given(settings):
    settings.raw["sources"].update(greenhouse=["monzo", {"slug": "acme", "name": "Acme Analytics Ltd"}], lever=["palantir"], lever_eu=[], ashby=["synthesia"])
    boards = ats.configured_boards(settings)
    assert [(b.ats, b.slug, b.name) for b in boards] == [("greenhouse", "monzo", "Monzo"), ("greenhouse", "acme", "Acme Analytics Ltd"),
                                                         ("lever", "palantir", "Palantir"), ("ashby", "synthesia", "Synthesia")]


def test_resolved_boards_use_the_register_name_each_board_once_and_ignore_unverified_ones(db, settings):
    settings.raw["sources"].update(greenhouse=["acmeanalytics"], lever=[], lever_eu=[], ashby=[])
    add_company(db, "Acme Analytics Ltd")
    db.conn.execute("UPDATE companies SET ats='greenhouse', ats_slug='acmeanalytics', ats_verified=1, probe_status='verified'")
    add_company(db, "Weak Ltd", town="Leeds")
    db.conn.execute("UPDATE companies SET ats='lever', ats_slug='weak', ats_verified=0, probe_status='unverified' WHERE register_name='Weak Ltd'")
    db.conn.commit()
    boards = ats.all_boards(settings, db)
    assert [(b.ats, b.slug, b.name, b.origin) for b in boards] == [("greenhouse", "acmeanalytics", "Acmeanalytics", "config")]
    settings.raw["sources"]["greenhouse"] = []
    assert [(b.ats, b.name) for b in ats.all_boards(settings, db)] == [("greenhouse", "Acme Analytics Ltd")]
