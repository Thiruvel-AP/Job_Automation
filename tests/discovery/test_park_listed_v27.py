"""v2.7 item 4: an ad outside the selected region is never written to jds/inbox: `discover` parks it in data/discover/parked.csv, and `careerops shortlist` moves the
listed-only files that are already in the inbox there. No network: `Net` answers every request."""
import yaml
from netfake import Net
from test_listed_only import acme_net, cfg, inbox_text, make, titles  # noqa: F401  (cfg: the Acme board on the fixture settings)

from careerops import cli
from careerops.discovery import discover as disc, intake, manual


def inbox_names(cfg):
    box = cfg.path("jds") / "inbox"
    return sorted(p.name for p in box.glob("*.txt")) if box.exists() else []


def listed_ad(cfg, *, company="Acme Analytics Ltd", title="AI Engineer", location="New York, NY", country="United States", url="https://job-boards.greenhouse.io/acme/jobs/103"):
    ad = intake.Ad(company=company, title=title, url=url, location=location, text="An AI engineer builds models for retail. " * 20, source="greenhouse",
                   ad_id="103", country=country, listed_only=True, posted="2026-10-01")
    path, _ = intake.write_inbox(cfg.path("jds") / "inbox", ad)
    return path


def uk_ad(cfg):
    ad = intake.Ad(company="Acme Analytics Ltd", title="Data Scientist", url="https://job-boards.greenhouse.io/acme/jobs/101", location="London, UK",
                   text="A data scientist builds models for retail. " * 20, source="greenhouse", ad_id="101")
    return intake.write_inbox(cfg.path("jds") / "inbox", ad)[0]


# ------------------------------------------------------------------ discover parks, it does not write
def test_the_parked_row_has_everything_needed_to_find_the_ad_again(cfg, db):
    make(cfg, db, acme_net()).from_boards()
    (row,) = disc.read_rows(disc.parked_path(cfg))
    assert list(row) == disc.PARKED_COLUMNS
    assert (row["company"], row["title"], row["location"], row["country"], row["source"]) == ("Acme Analytics Ltd", "AI Engineer", "New York, NY", "United States", "greenhouse")
    assert row["link"].startswith("https://") and row["ts"].startswith("2026-10-10") and row["file"] == ""


def test_no_listed_only_file_is_ever_written_to_the_inbox(cfg, db):
    make(cfg, db, acme_net()).from_boards()
    assert inbox_names(cfg) and not [n for n in inbox_names(cfg) if "103" in n]
    assert all("listed_only" not in p.read_text(encoding="utf-8") for p in (cfg.path("jds") / "inbox").glob("*.txt"))


def test_the_same_ad_is_parked_once_however_many_runs_see_it(cfg, db):
    make(cfg, db, acme_net()).from_boards()
    d2 = make(cfg, db, acme_net())
    d2.from_boards()
    assert len(disc.read_rows(disc.parked_path(cfg))) == 1 and d2.run.sources["ats"].get("listed_parked", 0) == 0


def test_a_region_you_choose_brings_the_ad_in_as_a_normal_ad(cfg, db):
    d = make(cfg, db, acme_net(), regions=("us",))
    d.from_boards()
    assert "AI Engineer | New York, NY" in titles(d) and not disc.parked_path(cfg).exists()          # (a bare "Remote" is region-unknown and kept)
    assert "Status: listed_only" not in inbox_text(cfg, "ai-engineer-103")


def test_listed_ads_do_not_count_as_new_inbox_files(cfg, db):
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert d.new_files == 3 and d.listed_files == 1 and d.run.sources["ats"]["written"] == 3


def test_the_report_counts_what_was_parked(cfg, db):
    d = make(cfg, db, acme_net())
    d.from_boards()
    assert d.run.as_json()["sources"]["ats"]["listed_parked"] == 1 and "listed_parked" in disc.COUNTERS


# ------------------------------------------------------------------ the ads already in the inbox
def test_a_listed_only_file_in_the_inbox_is_moved_to_parked_with_a_row(cfg):
    ny, uk = listed_ad(cfg), uk_ad(cfg)
    moved = disc.park_inbox_listed(cfg, ("uk",))
    assert [p.parent for p in moved] == [disc.parked_dir(cfg)] and moved[0].name == ny.name and moved[0].is_file()
    assert not ny.exists() and uk.is_file()                                         # the UK ad is where it was
    (row,) = disc.read_rows(disc.parked_path(cfg))
    assert (row["company"], row["title"], row["country"], row["link"]) == ("Acme Analytics Ltd", "AI Engineer", "United States", "https://job-boards.greenhouse.io/acme/jobs/103")
    assert row["file"] == f"parked/{ny.name}" and "outside region uk" in row["reason"]
    assert manual.header_fields(moved[0].read_text(encoding="utf-8"))["status"] == "listed_only"      # the ad itself is kept as it was


def test_an_ad_in_a_region_you_have_since_chosen_stays_in_the_inbox(cfg):
    ny = listed_ad(cfg)
    assert disc.park_inbox_listed(cfg, ("us",)) == [] and ny.is_file() and not disc.parked_path(cfg).exists()
    assert disc.park_inbox_listed(cfg, ("any",)) == [] and ny.is_file()


def test_with_no_region_chosen_every_listed_only_file_is_out(cfg):
    listed_ad(cfg)
    assert len(disc.park_inbox_listed(cfg, None)) == 1


def test_a_dry_run_moves_and_writes_nothing_and_a_second_run_finds_nothing(cfg):
    ny = listed_ad(cfg)
    assert [p.name for p in disc.park_inbox_listed(cfg, ("uk",), dry_run=True)] == [ny.name]
    assert ny.is_file() and not disc.parked_path(cfg).exists() and not disc.parked_dir(cfg).exists()
    assert len(disc.park_inbox_listed(cfg, ("uk",))) == 1
    assert disc.park_inbox_listed(cfg, ("uk",)) == [] and len(disc.read_rows(disc.parked_path(cfg))) == 1


def test_an_ad_whose_row_is_already_there_is_moved_without_a_second_row(cfg, db):
    make(cfg, db, acme_net()).from_boards()                                         # parks the New York ad: one row
    listed_ad(cfg)                                                                   # the same ad, still in the inbox from before
    assert len(disc.park_inbox_listed(cfg, ("uk",))) == 1
    assert len(disc.read_rows(disc.parked_path(cfg))) == 1 and not [n for n in inbox_names(cfg) if "ai-engineer-103" in n]


def test_two_files_of_the_same_name_are_never_overwritten(cfg):
    first = listed_ad(cfg)
    disc.park_inbox_listed(cfg, ("uk",))
    second = listed_ad(cfg)                                                          # the same file name comes back, then is parked again
    assert second.name == first.name
    moved = disc.park_inbox_listed(cfg, ("uk",))
    assert moved[0].name != first.name and len(list(disc.parked_dir(cfg).glob("*.txt"))) == 2


# ------------------------------------------------------------------ the command
def test_the_shortlist_command_moves_them_and_says_so_and_a_dry_run_does_not(cfg, tmp_path, capsys):
    ny = listed_ad(cfg)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg.raw), encoding="utf-8")
    cli.main(["--config", str(path), "shortlist", "--dry-run", "--no-liveness", "--region", "uk"])
    assert ny.is_file() and "listed-only" not in capsys.readouterr().out
    cli.main(["--config", str(path), "shortlist", "--no-liveness", "--region", "uk"])
    out = capsys.readouterr().out
    assert not ny.exists() and "parked 1 listed-only ad(s) outside" in out and str(disc.parked_path(cfg)) in out
    assert len(disc.read_rows(disc.parked_path(cfg))) == 1
