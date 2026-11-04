"""Country gate: a job outside the UK is not dropped but PARKED (v2.7: a row in data/discover/parked.csv, never a file in the inbox), counted apart from
the UK limit. An inbox file marked `Status: listed_only` is still ranked with S = 0.5 and refused by `live` (tests/apply/test_live.py). No network: `Net` answers every request."""
from datetime import datetime, timezone

import pytest

from careerops.discovery import countries, discover as disc, intake, manual, priority
from careerops.discovery.candidate import Candidate
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, fixture_json

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
ACME = {"slug": "acme", "name": "Acme Analytics Ltd"}


def make(cfg, db, net, **kw):
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(cfg, db, f, today=TODAY, say=lambda s: None, **kw)


def acme_net():
    return Net(strict=False).add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[ACME], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"]["alert_probes_per_run"] = 0
    settings.raw["discover"]["sources"] = []
    return settings


def inbox_text(cfg, fragment):
    box = cfg.path("jds") / "inbox"
    (path,) = [p for p in box.glob("*.txt") if fragment in p.name]
    return path.read_text(encoding="utf-8")


def titles(d):
    return sorted(c.title + " | " + c.location for c, _ in d.run.written)


# ------------------------------------------------------------------ which country a place is in
@pytest.mark.parametrize("place, country", [
    ("San Francisco, CA", "United States"), ("New York, NY", "United States"), ("Remote - US", "United States"), ("Austin, TX", "United States"),
    ("Toronto, ON", "Canada"), ("London, Ontario", "Canada"), ("Victoria, BC", "Canada"), ("Remote, Canada", "Canada"),
    ("Sydney NSW", "Australia"), ("Auckland", "New Zealand"), ("Berlin, DE", "Germany"), ("Amsterdam, NL", "Netherlands"),
    ("Dublin, Ireland", "Ireland"), ("Singapore", "Singapore"), ("Bangalore, IN", "India"), ("Zurich", "Switzerland"),
    ("London, UK", "United Kingdom"), ("Belfast, Northern Ireland", "United Kingdom"), ("England", "United Kingdom"),
    ("Remote", ""), ("", ""), ("Anywhere", ""), ("Narnia", ""),
])
def test_a_place_is_read_for_its_country(place, country):
    assert countries.detect(place) == country


def test_codes_and_names_agree_and_multi_is_not_a_country():
    assert [countries.canon(x) for x in ("CA", "ca", "Canada", "canada", "GB", "uk", "Deutschland", "Eire")] == \
        ["Canada", "Canada", "Canada", "Canada", "United Kingdom", "United Kingdom", "Germany", "Ireland"]
    assert countries.canon("multi") == "" and countries.canon(None) == "" and countries.is_uk("GB") and not countries.is_uk("CA")


def test_a_visa_block_is_found_by_country_name_or_code_and_must_not_be_empty():
    by = {"CA": {"summary": "x"}, "Germany": {"right_to_work": True}, "AU": {}, "NZ": None, "GB": {"summary": "uk"},
          "default": {"summary": "anyone"}}
    applicant = {"visa": {"by_country": by}}
    assert countries.visa_block(applicant, "Canada") == {"summary": "x"}
    assert countries.visa_block(applicant, "DE") == {"right_to_work": True}
    assert countries.visa_block(applicant, "Australia") is None and countries.visa_block(applicant, "New Zealand") is None
    assert countries.visa_block(applicant, "France") is None and countries.visa_block({}, "Canada") is None
    assert countries.visa_block(applicant, "default") is None and countries.visa_block(applicant, "") is None      # `default` is not a country
    assert countries.visa_block(applicant, "United Kingdom") == {"summary": "uk"}
    assert countries.visa_block({"visa_by_country": {"CA": {"summary": "x"}}}, "Canada") is None                  # the earlier layout is gone


# ------------------------------------------------------------------ discover writes it listed_only
def test_a_job_outside_the_uk_is_parked_and_not_written_to_the_inbox(cfg, db):
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert titles(d) == ["Data Scientist | London, UK", "Data Scientist | Manchester", "Machine Learning Engineer | Remote"]
    assert not [p for p in (cfg.path("jds") / "inbox").glob("*.txt") if "ai-engineer-103" in p.name]
    (row,) = disc.read_rows(disc.parked_path(cfg))
    assert (row["company"], row["title"], row["location"], row["country"]) == ("Acme Analytics Ltd", "AI Engineer", "New York, NY", "United States")
    assert row["link"] and "outside the UK" in row["reason"]
    assert "Country:" not in inbox_text(cfg, "data-scientist-101") and "listed_only" not in inbox_text(cfg, "data-scientist-101")
    c = d.run.sources["ats"]
    assert (c["written"], c["listed_only"], c["listed_parked"], c["place_filtered"]) == (3, 1, 1, 1)          # the bare "Remote" ad of Applied Scientist is still out


def test_the_headers_come_in_the_first_ten_lines_even_with_every_other_header(cfg):
    ad = intake.Ad(company="Acme", title="Data Scientist", url="https://x.example/1", location="Toronto, ON", text="t" * 600, source="ashby",
                   ad_id="1", salary="CAD 90k", agency=True, posted="2026-10-01", country="Canada", listed_only=True)
    head = manual.header_fields(intake.render(ad))
    assert (head["country"], head["status"], head["posted"], head["agency"]) == ("Canada", "listed_only", "2026-10-01", "yes")


def test_the_listed_only_flag_is_off_for_a_uk_ad_so_its_file_is_unchanged():
    c = Candidate(source="ashby", company="Acme", title="Data Scientist", location="London", url="https://x.example/1", description="d",
                  country="United Kingdom")
    assert "Country" not in intake.render(c.to_ad())


def test_the_listed_only_limit_is_its_own(cfg, db):
    cfg.raw["discover"]["listed_only"] = {"enabled": True, "max_per_run": 0}
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert "AI Engineer | New York, NY" not in titles(d) and len(d.run.written) == 3
    assert d.run.sources["ats"]["listed_capped"] == 1


def test_the_uk_limit_does_not_stop_a_listed_ad_and_a_listed_ad_does_not_use_the_uk_limit(cfg, db):
    d = make(cfg, db, acme_net(), limit=1)
    d.from_boards()
    assert titles(d) == ["Data Scientist | London, UK"]                                        # one UK ad (the limit) ...
    assert d.new_files == 1 and d.listed_files == 1 and len(disc.read_rows(disc.parked_path(cfg))) == 1       # ... and the parked one


def test_turned_off_it_is_the_old_behaviour(cfg, db):
    cfg.raw["discover"]["listed_only"] = {"enabled": False}
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert len(d.run.written) == 3 and d.run.sources["ats"]["place_filtered"] == 2


def test_a_dry_run_counts_it_and_writes_nothing(cfg, db):
    d = make(cfg, db, acme_net(), dry_run=True)
    d.from_boards()
    assert len(d.run.written) == 3 and d.run.sources["ats"]["listed_only"] == 1 and d.run.sources["ats"]["listed_parked"] == 1
    assert not (cfg.path("jds") / "inbox").exists() and not disc.parked_path(cfg).exists()


def test_a_listed_ad_is_credited_to_its_board_in_the_by_source_counts(cfg, db):
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert dict(d.run.by_source["other"]) == {"new": 3, "listed_only": 1}                # no registry here: nobody to credit but "other"
    assert d.run.as_json()["by_source"]["other"]["listed_only"] == 1


def test_the_same_job_is_not_listed_twice(cfg, db):
    make(cfg, db, acme_net()).from_boards()
    d2 = make(cfg, db, acme_net())
    d2.from_boards()
    assert d2.run.written == [] and d2.run.sources["ats"]["duplicate"] >= 4


# ------------------------------------------------------------------ priority
def test_a_parked_ad_is_not_ranked_or_listed_and_the_ads_that_are_written_keep_their_s(cfg, db):
    d = make(cfg, db, acme_net())
    d.from_boards()
    by = {c.title + " | " + c.location: pr for pr, c, _ in d.ranked()}
    assert "AI Engineer | New York, NY" not in by and all(pr.s > 0.5 for pr in by.values())
    assert not any("listed only" in line or "New York" in line for line in d.listing())


def test_the_weight_is_a_setting_and_can_only_lower_s(cfg):
    assert priority.weights(cfg)["non_home"] == 0.5
    job = {"company": "X", "description": "a job", "sponsor_match": "exact", "country": "Canada"}
    assert priority.sponsor_factor(job, cfg)[0] == 0.5
    cfg.raw["priority"]["sponsor_weights"]["non_home"] = 0.2
    assert priority.sponsor_factor(job, cfg)[0] == 0.2
    assert priority.sponsor_factor({**job, "country": "United Kingdom"}, cfg)[0] == 1.0         # a UK job is unaffected
    assert priority.sponsor_factor({k: v for k, v in job.items() if k != "country"}, cfg)[0] == 1.0


def test_an_inbox_file_is_ranked_by_its_country_header(cfg, db, tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("Company: Acme\nTitle: Data Scientist\nURL: https://x.example/1\nSource: ashby\nCountry: Canada\nStatus: listed_only\n\n"
                 + "Data scientist job. " * 40, encoding="utf-8")
    job = priority.file_job(p, cfg, {}, None)
    assert job["country"] == "Canada" and priority.priority(job, cfg, tailored=False).s == 0.5


# ------------------------------------------------------------------ a visa block also opens the location rule for that country
def test_a_country_with_a_visa_block_passes_the_location_rule_and_the_others_do_not(settings, tmp_path):
    from pathlib import Path

    from careerops.discovery import gates
    job = {"title": "Data Scientist", "location": "Toronto, ON", "description": "A data science job."}
    berlin = {**job, "location": "Berlin, Germany"}
    assert gates.visa_countries(settings) == ("United Kingdom",)               # the fixture applicant has a GB block and a default
    assert any("not in allow-list" in r for r in gates.triage(job, settings, None).reasons)
    p = tmp_path / "applicant.yaml"
    import yaml
    a = yaml.safe_load(Path(settings.raw["paths"]["applicant"]).read_text(encoding="utf-8"))
    a["visa"]["by_country"].update({"CA": {"summary": "open work permit"}, "Germany": {}})
    p.write_text(yaml.safe_dump(a), encoding="utf-8")
    settings.raw["paths"]["applicant"] = str(p)
    assert set(gates.visa_countries(settings)) == {"United Kingdom", "Canada"}   # Germany's block is empty: it does not count
    assert not any("allow-list" in r for r in gates.triage(job, settings, None).reasons)
    assert any("allow-list" in r for r in gates.triage(berlin, settings, None).reasons)


def test_a_missing_applicant_file_means_no_visa_countries(settings, tmp_path):
    from careerops.discovery import gates
    settings.raw["paths"]["applicant"] = str(tmp_path / "nowhere.yaml")
    assert gates.visa_countries(settings) == ()
