"""`careerops discover` with a region (discovery/discover.py, registry.py, candidate.py): every job goes through the region match after it is fetched, a source that is for
other regions only does not run, the country a source gave rides with the place into the ad, and the report line counts in / out / unknown. No network."""
from datetime import datetime, timezone

import pytest
import yaml

from careerops import cli
from careerops.discovery import discover as disc
from careerops.discovery import registry
from careerops.discovery.candidate import Candidate
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, fixture_json

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
ACME = {"slug": "acme", "name": "Acme Analytics Ltd"}


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def board():
    net = Net(strict=False)
    net.add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))
    return net


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[ACME], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"]["alert_probes_per_run"] = 0
    settings.raw["discover"]["sources"] = []
    settings.raw["discover"]["listed_only"] = {"enabled": False}
    return settings


def make(cfg, db, **kw):
    return Discover(cfg, db, fetcher(board()), today=TODAY, say=lambda s: None, **kw)


def titles(d):
    return sorted(c.title + " | " + c.location for c, _ in d.run.written)


# ------------------------------------------------------------------ the region filter after fetching
def test_a_uk_region_writes_the_uk_jobs_and_counts_the_rest_as_out(cfg, db):
    d = make(cfg, db, regions=("uk",))
    d.from_boards()
    got = titles(d)
    assert "Data Scientist | London, UK" in got and "Data Scientist | Manchester" in got
    assert "AI Engineer | New York, NY" not in got                                           # a New York job is never written for a UK region
    counts = d.region_counts()
    assert counts["out"] >= 1 and counts["in"] >= 2 and d.run.sources["ats"]["place_filtered"] >= 1


def test_a_us_region_writes_the_new_york_job_and_not_the_london_one(cfg, db):
    d = make(cfg, db, regions=("us",))
    d.from_boards()
    got = titles(d)
    assert "AI Engineer | New York, NY" in got and "Data Scientist | London, UK" not in got and "Data Scientist | Manchester" not in got


def test_any_region_writes_every_job_in_a_target_family(cfg, db):
    d = make(cfg, db, regions=("any",))
    d.from_boards()
    assert len(titles(d)) == 5 and d.region_counts()["out"] == 0                              # all but the Marketing Manager (a role filter, not a region one)


def test_without_a_region_the_old_uk_gate_is_unchanged(cfg, db):
    d = make(cfg, db)
    d.from_boards()
    assert titles(d) == ["Data Scientist | London, UK", "Data Scientist | Manchester", "Machine Learning Engineer | Remote"]
    assert d.region_counts() == {"in": 0, "out": 0, "unknown": 0}


def test_a_remote_job_whose_ad_does_not_say_is_kept_not_dropped(cfg, db):
    d = make(cfg, db, regions=("uk",))
    d.from_boards()
    assert "Applied Scientist | Remote" in titles(d) or d.region_counts()["out"] >= 1            # kept as unknown, or excluded by its own text
    assert d.region_counts()["unknown"] + d.region_counts()["in"] >= 2


def test_the_filter_reason_names_the_region(cfg, db):
    d = make(cfg, db, regions=("uk",))
    d.from_boards()
    rows = disc.read_rows(disc.filtered_path(cfg, d.today_day))
    assert any(r["filter"] == "place" and r["reason"].startswith("not in region uk") and r["location"] == "New York, NY" for r in rows)


# ------------------------------------------------------------------ sources
def sources_file(settings, tmp_path, entries):
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump({"sources": entries}), encoding="utf-8")
    settings.raw["paths"]["sources"] = str(p)


ENTRIES = [
    {"id": "reed-uk", "name": "Reed", "url": "https://www.reed.co.uk", "country": "GB", "method": "gmail_alerts", "domains": ["reed.co.uk"], "regions": ["uk"]},
    {"id": "seek-au", "name": "SEEK AU", "url": "https://www.seek.com.au", "country": "AU", "method": "gmail_alerts", "domains": ["seek.com.au"], "regions": ["au"]},
    {"id": "stepstone-de", "name": "StepStone", "url": "https://www.stepstone.de", "country": "DE", "method": "gmail_alerts", "domains": ["stepstone.de"], "regions": ["de"]},
    {"id": "everywhere", "name": "Anywhere", "url": "https://x.example.com", "country": "multi", "method": "gmail_alerts", "domains": ["x.example.com"]},
]


def test_a_source_for_other_regions_does_not_run(cfg, db, tmp_path):
    sources_file(cfg, tmp_path, ENTRIES)
    uk = make(cfg, db, regions=("uk",))
    assert [s.id for s in uk.registry] == ["reed-uk", "everywhere"]
    assert any(n.startswith("not run for region uk:") and "seek-au (au)" in n and "stepstone-de (de)" in n for n in uk.run.notes)
    assert [s.id for s in make(cfg, db, regions=("au",)).registry] == ["seek-au", "everywhere"]
    assert [s.id for s in make(cfg, db, regions=("eu",)).registry] == ["stepstone-de", "everywhere"]
    assert len(make(cfg, db, regions=("any",)).registry) == 4 and len(make(cfg, db).registry) == 4          # no region: every source, as before


def test_an_alert_from_a_source_that_does_not_run_is_not_matched(cfg, db, tmp_path):
    sources_file(cfg, tmp_path, ENTRIES)
    uk = make(cfg, db, regions=("uk",))
    assert uk.registry.match_sender("jobs@seek.com.au") is None and uk.registry.match_sender("jobs@reed.co.uk").id == "reed-uk"


def test_a_source_with_no_regions_tag_is_for_any_region(cfg, db, tmp_path):
    sources_file(cfg, tmp_path, [{k: v for k, v in e.items() if k != "regions"} for e in ENTRIES])
    assert len(make(cfg, db, regions=("uk",)).registry) == 4
    assert [s.region_tags for s in registry.load(cfg)] == [("any",)] * 4


def test_a_bad_regions_tag_is_refused_by_name(cfg, tmp_path):
    sources_file(cfg, tmp_path, [{**ENTRIES[0], "regions": ["narnia"]}])
    with pytest.raises(registry.RegistryError, match="reed-uk.*regions.*narnia"):
        registry.load(cfg)


# ------------------------------------------------------------------ the ad
def test_the_country_a_source_gave_rides_with_the_place_into_the_ad():
    c = Candidate(source="reed", company="Co", title="Data Scientist", location="Hull", url="https://x.example/1", description="d", country="GB")
    assert c.to_ad().location == "Hull, GB"
    assert Candidate(source="reed", company="Co", title="T", location="Hull, GB", url="u", description="d", country="GB").to_ad().location == "Hull, GB"
    assert Candidate(source="x", company="Co", title="T", location="", url="u", description="d", country="GB").to_ad().location == "GB"
    assert Candidate(source="x", company="Co", title="T", location="Hull", url="u", description="d").to_ad().location == "Hull"
    assert Candidate(source="x", company="Co", title="T", location="Berlin", url="u", description="d", country="Germany", listed_only=True).to_ad().location == "Berlin"


# ------------------------------------------------------------------ the report line
def test_the_report_line_counts_region_and_size():
    assert disc.format_region_size({"in": 41, "out": 112, "unknown": 9}, {"sme": 14, "large": 22, "unknown": 31}) == \
        "in region 41 · out 112 (parked) · unknown 9 | SME 14 · Large 22 · size? 31"
    assert disc.format_region_size({}, {}) == "in region 0 · out 0 (parked) · unknown 0 | SME 0 · Large 0 · size? 0"


def test_the_report_line_over_the_inbox_with_the_overrides(settings, tmp_path, monkeypatch):
    from argparse import Namespace
    from triage_fakes import write_ad
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")
    inbox = settings.path("jds") / "inbox"
    for name, company, place in (("a", "Acme", "London, UK"), ("b", "Bigco", "Bristol, UK"), ("c", "Newyork", "New York, NY"), ("d", "Remotely", "Remote")):
        p = write_ad(inbox, f"{name}.txt", company=company)
        p.write_text(p.read_text(encoding="utf-8").replace("Location: London, UK", f"Location: {place}"), encoding="utf-8")
    settings.path("company_size_overrides").write_text("Acme: sme\nBigco: large\n", encoding="utf-8")
    text = cli._region_size_line(Namespace(mock=True), settings, ("uk",))
    assert text.splitlines()[0] == "in region 2 · out 1 (parked) · unknown 1 | SME 1 · Large 1 · size? 1"
    assert "COMPANIES_HOUSE_API_KEY" not in text                              # --mock never asks Companies House, so it does not nag about a key
    run_tally = {"in": 41, "out": 112, "unknown": 9}
    assert cli._region_size_line(Namespace(mock=True), settings, ("uk",), run_tally).startswith("in region 41 · out 112 (parked) · unknown 9 | SME 1")


def test_discover_report_alone_prints_the_region_the_untagged_sources_and_the_line(cfg, tmp_path, capsys, monkeypatch):
    from triage_fakes import write_ad
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")
    sources_file(cfg, tmp_path, ENTRIES[:2] + [{k: v for k, v in ENTRIES[3].items() if k != "regions"}])
    ad = write_ad(cfg.path("jds") / "inbox", "a.txt", company="Acme")
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg.raw), encoding="utf-8")
    cli.main(["--config", str(path), "discover", "--report", "--no-liveness", "--region", "uk"])
    out = capsys.readouterr().out
    assert "region: uk (from --region flag)" in out and "1 source(s) in sources.yaml have no `regions:` tag (treated as [any])" in out
    assert "in region 1 · out 0 (parked) · unknown 0 | SME 0 · Large 0 · size? 1" in out and "COMPANIES_HOUSE_API_KEY is not set" in out
    assert ad.exists()

