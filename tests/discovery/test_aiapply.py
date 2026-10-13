"""data/aiapply_applied.csv (paths.aiapply_applied): jobs AIApply already applied to are skipped as duplicates, matched on company +
normalised title. The file is only read. No network."""
from datetime import datetime, timezone

import pytest

from careerops.discovery import discover as disc
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, fixture_json

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def make(cfg, db, net):
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(cfg, db, f, today=TODAY, say=lambda s: None)


def acme_net():
    return Net(strict=False).add("boards/acme/jobs", fixture_json("gh_jobs_acme.json")).add("boards/acme", fixture_json("gh_board_acme.json"))


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[{"slug": "acme", "name": "Acme Analytics Ltd"}], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[], listed_only={"enabled": False})
    return settings


def export(cfg, text, name="aiapply_applied.csv"):
    p = cfg.path("aiapply_applied")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_a_job_aiapply_applied_to_is_skipped_whatever_its_place_and_wording_of_the_title(cfg, db):
    export(cfg, "Company,Job Title,Date\nacme analytics LTD,Data Scientist (Remote),2026-09-30\n")
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert sorted(c.location for c, _ in d.run.written) == ["Remote"]                  # both Data Scientist ads (London, Manchester) are out
    assert d.run.sources["ats"]["duplicate"] == 2


def test_the_known_set_says_where_the_duplicate_came_from(cfg, db):
    export(cfg, "company,title\nAcme Analytics Ltd,Data Scientist\n")
    assert disc.load_known(cfg, db).where("Acme Analytics Ltd", "Data Scientist", "London") == "already applied (AIApply)"


@pytest.mark.parametrize("header", ["company,title", "Employer,Position", "COMPANY_NAME,JOB_TITLE", "organisation,role"])
def test_the_column_names_may_vary(cfg, db, header):
    export(cfg, f"{header}\nAcme Analytics Ltd,Data Scientist\n")
    assert disc.load_known(cfg, db).where("Acme Analytics Ltd", "Data Scientist", "") is not None


def test_no_file_means_nothing_is_skipped_and_no_note(cfg, db):
    d = make(cfg, db, acme_net())
    assert len(d.known.full) == 0 and d.run.notes == []
    d.from_boards()
    assert len(d.run.written) == 3


def test_a_file_without_the_two_columns_is_a_note_and_skips_nothing(cfg, db):
    export(cfg, "when,what\n2026-09-30,something\n")
    d = make(cfg, db, acme_net())
    assert any("no company and title columns" in n and "when, what" in n for n in d.run.notes)
    d.from_boards()
    assert len(d.run.written) == 3


def test_a_file_that_is_not_text_is_a_note_not_a_crash(cfg, db):
    p = export(cfg, "")
    p.write_bytes(b"\xff\xfe\x00\x00\x9f\x92\x96" * 50)
    d = make(cfg, db, acme_net())
    assert any("aiapply_applied.csv" in n for n in d.run.notes) or len(d.known.full) == 0


def test_the_export_is_never_written(cfg, db):
    p = export(cfg, "company,title\nAcme Analytics Ltd,Data Scientist\n")
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    make(cfg, db, acme_net()).from_boards()
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before


def test_the_path_is_a_setting_and_a_config_without_it_still_works(cfg, db):
    del cfg.raw["paths"]["aiapply_applied"]
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert len(d.run.written) == 3
