"""M1: the sponsor register as daily snapshots (download only when the link changed, header read from the file, diff, one
transaction) and the sponsor check in triage (exact / fuzzy / none, never a skip). HTTP is mocked; no network."""
import gzip
import json
import logging
from pathlib import Path

import pytest
import yaml

from careerops import cli
from careerops.discovery import gates
from careerops.discovery import register as reg
from conftest import fixture

PAGE = fixture("discovery", "register_page.html").read_text(encoding="utf-8")
D1, D2 = fixture("discovery", "register_d1.csv"), fixture("discovery", "register_d2.csv")
LINK = "https://assets.publishing.service.gov.uk/media/6abe33d2efad0f1df6722889/SP_-_Worker_and_Temporary_Worker_Web_Register_-_2026-10-01.csv"


class Resp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content

    def raise_for_status(self):
        pass


class Web:
    """The gov.uk page and the CSV files, served from memory; `calls` records every URL asked for."""

    def __init__(self, monkeypatch):
        self.page_link = LINK
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        monkeypatch.setattr(reg.requests, "get", self.get)

    def publish(self, day: str, path: Path):
        self.page_link = LINK.replace("2026-10-01", day).replace("6abe33d2efad0f1df6722889", "m" + day.replace("-", ""))
        self.files[self.page_link] = path.read_bytes()

    def get(self, url, **kw):
        self.calls.append(url)
        if url == "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers":
            return Resp(text=PAGE.replace(LINK, self.page_link))
        return Resp(content=self.files[url])

    @property
    def downloads(self) -> int:
        return sum(1 for u in self.calls if u.endswith(".csv"))


@pytest.fixture
def web(monkeypatch):
    return Web(monkeypatch)


def _row(db, sql, *args):
    return db.conn.execute(sql, args).fetchall()


def _events(db):
    return [tuple(r) for r in db.conn.execute("SELECT kind, norm, town_norm, day, detail FROM company_events ORDER BY rowid")]


# ---------------------------------------------------------------- reading a register file
def test_names_towns_ratings_counties_and_routes_are_read_from_the_header_and_the_rows():
    got = reg.load_snapshot(D1)
    assert got[("birch and", "bristol")] == ("Birch & Co Limited", "Bristol", "", "A", "Skilled Worker")     # Not set -> ''
    assert got[("cedar data", "leeds")][3] == "A" and got[("cedar data", "leeds")][2] == "West Yorkshire"
    assert all(v[4] == "Skilled Worker" for v in got.values())
    assert ("global care", "london") not in got                                              # another route is left out
    assert ("data lab", "london") in got and ("data lab", "cardiff") in got                  # same name, two towns, two keys


def test_csv_and_gz_give_identical_snapshots_and_a_bom_is_tolerated(tmp_path):
    gz = tmp_path / "2026-09-30.csv.gz"
    gz.write_bytes(gzip.compress(D1.read_bytes()))
    assert D1.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert reg.load_snapshot(gz) == reg.load_snapshot(D1)


def test_a_duplicate_row_keeps_the_first_and_is_counted_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=reg.log.name):
        r = reg.read_register(D1.read_bytes())
    assert r.duplicates == 1 and any("1 duplicate" in m for m in caplog.messages)
    assert len(r.rows) == len(set(r.rows))


def test_columns_are_found_by_the_files_own_header_whatever_their_order():
    data = ("Route,Extra,Type & Rating,County,Town/City,Organisation Name\n"
            "Skilled Worker,x,Worker (B rating),Kent,Dover,Zed Ltd\n").encode()
    r = reg.read_register(data)
    assert r.rows == {("zed", "dover"): ("Zed Ltd", "Dover", "Kent", "B", "Skilled Worker")}


@pytest.mark.parametrize("missing", ["Organisation Name", "Town/City", "County", "Type & Rating", "Route"])
def test_a_missing_field_fails_loudly_naming_it_and_the_header_seen(missing):
    cols = [c for c in ["Organisation Name", "Town/City", "County", "Type & Rating", "Route"] if c != missing]
    with pytest.raises(reg.RegisterFormatError) as e:
        reg.read_register((",".join(cols) + "\n" + ",".join("x" for _ in cols) + "\n").encode())
    needle = {"Organisation Name": "organisation", "Town/City": "town", "County": "county", "Type & Rating": "rating",
              "Route": "route"}[missing]
    assert f"no '{needle}' field" in str(e.value) and str(cols) in str(e.value)


def test_a_register_with_a_bad_header_is_not_saved_and_records_nothing(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.page_link = LINK
    web.files[LINK] = b"Name,City\nAcme,London\n"
    with pytest.raises(reg.RegisterFormatError):
        reg.take_snapshot(db, settings)
    assert not (tmp_path / "reg").exists() or not list((tmp_path / "reg").iterdir())
    assert _row(db, "SELECT * FROM register_snapshots") == [] and _row(db, "SELECT * FROM companies") == []


# ---------------------------------------------------------------- diff
def test_diff_finds_exactly_the_planted_added_removed_and_rating_rows():
    d = reg.diff(reg.load_snapshot(D1), reg.load_snapshot(D2))
    assert d.added == [("harbour analytics", "bath"), ("ivory ai", "cambridge")]
    assert d.removed == [("birch and", "bristol")]
    assert d.rating_changed == [("cedar data", "leeds", "A", "B")]


def test_diff_of_a_snapshot_with_itself_is_empty():
    d = reg.diff(reg.load_snapshot(D1), reg.load_snapshot(D1))
    assert (d.added, d.removed, d.rating_changed) == ([], [], [])


# ---------------------------------------------------------------- snapshots
def test_first_snapshot_stores_the_file_the_row_the_companies_and_one_baseline_event(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    snap = reg.take_snapshot(db, settings)
    assert snap.baseline and snap.day == "2026-09-30" and snap.path == tmp_path / "reg" / "2026-09-30.csv.gz"
    assert gzip.decompress(snap.path.read_bytes()) == D1.read_bytes()
    assert [tuple(r) for r in db.conn.execute("SELECT day, csv_url, rows FROM register_snapshots")] == [
        ("2026-09-30", web.page_link, snap.rows)]
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == snap.rows == 11
    assert _events(db) == [("baseline", "", "", "2026-09-30", "11")]               # one event, not 11 'added'
    assert db.conn.execute("SELECT COUNT(*) FROM companies WHERE first_seen = '2026-09-30' AND last_seen = '2026-09-30'").fetchone()[0] == 11


def test_second_day_records_the_planted_changes_and_keeps_first_seen(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    reg.take_snapshot(db, settings)
    web.publish("2026-10-01", D2)
    snap = reg.take_snapshot(db, settings)
    assert not snap.baseline and reg.summary_line(snap) == "2026-10-01: 12 Skilled Worker sponsors · +2 added · −1 removed · 1 rating changes"
    assert sorted(_events(db)[1:]) == sorted([
        ("added", "harbour analytics", "bath", "2026-10-01", "Harbour Analytics Ltd"),
        ("added", "ivory ai", "cambridge", "2026-10-01", "Ivory AI Ltd"),
        ("removed", "birch and", "bristol", "2026-10-01", "Birch & Co Limited"),
        ("rating", "cedar data", "leeds", "2026-10-01", "A→B")])
    acme = db.conn.execute("SELECT first_seen, last_seen, removed_on FROM companies WHERE norm='acme analytics'").fetchone()
    assert tuple(acme) == ("2026-09-30", "2026-10-01", None)                       # seen before, still there today
    birch = db.conn.execute("SELECT first_seen, last_seen, removed_on FROM companies WHERE norm='birch and'").fetchone()
    assert tuple(birch) == ("2026-09-30", "2026-09-30", "2026-10-01")
    assert db.conn.execute("SELECT rating FROM companies WHERE norm='cedar data'").fetchone()[0] == "B"
    assert db.conn.execute("SELECT first_seen FROM companies WHERE norm='ivory ai'").fetchone()[0] == "2026-10-01"


def test_running_it_again_with_the_same_link_downloads_nothing_and_changes_nothing(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    reg.take_snapshot(db, settings)
    before = (_row(db, "SELECT * FROM companies"), _row(db, "SELECT * FROM company_events"),
              _row(db, "SELECT * FROM register_snapshots"))
    assert web.downloads == 1
    assert reg.take_snapshot(db, settings) is None and reg.snapshot(db, settings) is None
    assert web.downloads == 1                                                       # the CSV link was not fetched again
    assert (_row(db, "SELECT * FROM companies"), _row(db, "SELECT * FROM company_events"),
            _row(db, "SELECT * FROM register_snapshots")) == before
    assert reg.summary_line(None, db) == "Register unchanged (already have 2026-09-30)"


def test_a_same_day_rerun_is_a_no_op_even_when_the_link_changed(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    reg.take_snapshot(db, settings)
    web.page_link = web.page_link.replace("m20260930", "reuploaded")               # same day, other asset URL
    web.files[web.page_link] = D2.read_bytes()
    assert reg.take_snapshot(db, settings) is None
    assert db.conn.execute("SELECT COUNT(*) FROM register_snapshots").fetchone()[0] == 1
    cur = reg.load_snapshot(D2)
    d = reg.diff(reg.load_snapshot(D1), cur)
    reg.apply_diff(db, "2026-09-30", d, cur)                                       # applying a recorded day again: nothing
    assert db.conn.execute("SELECT COUNT(*) FROM company_events").fetchone()[0] == 1


def test_a_restored_sponsor_clears_removed_on_and_logs_readded(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    for day, path in (("2026-09-30", D1), ("2026-10-01", D2), ("2026-10-02", D1)):
        web.publish(day, path)
        reg.take_snapshot(db, settings)
    assert db.conn.execute("SELECT removed_on FROM companies WHERE norm='birch and'").fetchone()[0] is None
    assert ("readded", "birch and", "bristol", "2026-10-02", "Birch & Co Limited") in _events(db)


@pytest.mark.parametrize("url,day,found", [
    ("https://assets.publishing.service.gov.uk/media/x/SP_-_Register_-_2026-10-01.csv", "2026-10-01", True),
    ("https://assets.publishing.service.gov.uk/media/x/2024-10-10_-_Worker_and_Temporary_Worker.csv", "2024-10-10", True),
    ("https://assets.publishing.service.gov.uk/media/2025-01-02/register.csv", None, False),    # a date in the path only
])
def test_the_date_comes_from_the_file_name_else_today(url, day, found):
    got, ok = reg.csv_date(url)
    assert ok is found and (got == day if found else len(got) == 10)


def test_a_failure_part_way_leaves_no_snapshot_row_and_no_partial_events(db, settings, web, tmp_path, monkeypatch):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    reg.take_snapshot(db, settings)
    db.upsert_job({"id": "m:1", "source": "m", "company": "Birch & Co", "title": "t"})
    db.update_job("m:1", status="triaged", sponsor_name="Birch & Co Limited")
    before = (_row(db, "SELECT * FROM companies"), _row(db, "SELECT * FROM company_events"))
    web.publish("2026-10-01", D2)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(reg, "_flag_jobs", boom)                                    # raised after the adds and before the end
    with pytest.raises(RuntimeError):
        reg.take_snapshot(db, settings)
    assert _row(db, "SELECT day FROM register_snapshots") == [("2026-09-30",)] or \
        [tuple(r) for r in _row(db, "SELECT day FROM register_snapshots")] == [("2026-09-30",)]
    assert (_row(db, "SELECT * FROM companies"), _row(db, "SELECT * FROM company_events")) == before


def test_a_removed_sponsor_and_a_rating_drop_flag_only_jobs_still_in_play_and_once(db):
    def job(i, status, sponsor):
        db.upsert_job({"id": f"m:{i}", "source": "m", "company": "c", "title": "t"})
        db.update_job(f"m:{i}", status=status, sponsor_name=sponsor)

    job(1, "triaged", "Birch & Co Limited")
    job(2, "skipped", "Birch & Co Limited")
    job(3, "applied", "Birch & Co Limited")
    job(4, "needs_review", "Cedar Data PLC")
    job(5, "triaged", "Acme Analytics Ltd")
    reg.apply_diff(db, "2026-09-30", reg.diff({}, reg.load_snapshot(D1)), reg.load_snapshot(D1),
                   snapshot=("2026-09-30", "u1", "p1", 11))
    cur = reg.load_snapshot(D2)
    d = reg.diff(reg.load_snapshot(D1), cur)
    reg.apply_diff(db, "2026-10-01", d, cur, snapshot=("2026-10-01", "u2", "p2", 12))
    reg._flag_jobs(db, "Birch & Co Limited", "Sponsor licence no longer on register (since 2026-10-01)")   # again: no duplicate
    flags = {r["id"]: json.loads(r["flags"]) for r in db.conn.execute("SELECT id, flags FROM jobs")}
    assert flags["m:1"] == ["Sponsor licence no longer on register (since 2026-10-01)"]
    assert flags["m:2"] == [] and flags["m:3"] == [] and flags["m:5"] == []
    assert flags["m:4"] == ["Sponsor rating now B (since 2026-10-01)"]


# ---------------------------------------------------------------- the sponsor check
@pytest.fixture
def loaded(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    web.publish("2026-09-30", D1)
    reg.take_snapshot(db, settings)
    return reg.CompanyIndex(db)


@pytest.mark.parametrize("company,register_name", [
    ("Acme Analytics Limited", "Acme Analytics Ltd"),              # Ltd / Limited
    ("ACME ANALYTICS, LTD.", "Acme Analytics Ltd"),                # case and punctuation
    ("Cedar Data", "Cedar Data PLC"),                              # PLC dropped
    ("Evergreen Health Limited", "Evergreen Health LLP"),          # LLP / Limited
    ("Quartz and Sons Limited", "Quartz & Sons Ltd"),              # & / and
    ("Delta Labs", "Delta Robotics Ltd T/A Delta Labs"),           # the trading-as side
    ("Delta Robotics", "Delta Robotics Ltd T/A Delta Labs"),       # the legal side
    ("Delta Robotics Limited t/a Delta Labs", "Delta Robotics Ltd T/A Delta Labs"),   # both sides on the job
    ("Northwind Health AI", "Northwind Health AI Limited"),
])
def test_ltd_punctuation_and_and_and_both_sides_of_ta_match_exactly(loaded, company, register_name):
    m = loaded.lookup(company)
    assert (m.method, m.name, m.label) == ("exact", register_name, "exact")
    assert m.route == "Skilled Worker" and m.rating in ("A", "B")


def test_a_near_miss_is_fuzzy_with_its_score_and_never_promoted_to_exact(loaded):
    m = loaded.lookup("Acme Analytic Ltd")
    assert m.method == "fuzzy" and m.name == "Acme Analytics Ltd" and 0.92 <= m.score < 1.0
    assert m.label == f"fuzzy({m.score:.2f})" and m.label != "exact"
    assert loaded.lookup("Monzo").method == "fuzzy"                                # a prefix of one register name
    assert loaded.lookup("Totally Unknown Company").label == "none"


def test_a_removed_sponsor_no_longer_matches(db, settings, web, tmp_path):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    for day, path in (("2026-09-30", D1), ("2026-10-01", D2)):
        web.publish(day, path)
        reg.take_snapshot(db, settings)
    idx = reg.CompanyIndex(db)
    assert idx.lookup("Birch and Co").method == "none" and idx.lookup("Harbour Analytics").method == "exact"
    assert idx.lookup("Cedar Data").rating == "B"


def test_triage_flags_an_employer_off_the_register_but_never_skips_it(db, settings, loaded, job):
    v = gates.triage(dict(job, company="Not A Sponsor Ltd"), settings, loaded)
    assert v.passed and gates.NOT_ON_REGISTER in v.flags and v.sponsor_match == "none" and v.sponsor_name is None
    assert not any("Not on sponsor" in r for r in v.reasons)
    assert all("not loaded" not in f for f in v.flags)                              # a register exists: it is not "not loaded"


def test_triage_stores_match_route_and_rating_and_flags_a_fuzzy_match(db, settings, loaded, job):
    v = gates.triage(dict(job, company="Acme Analytics Limited"), settings, loaded)
    assert (v.sponsor_match, v.sponsor_name, v.sponsor_route, v.sponsor_rating) == ("exact", "Acme Analytics Ltd",
                                                                                  "Skilled Worker", "A")
    assert gates.NOT_ON_REGISTER not in v.flags
    f = gates.triage(dict(job, company="Acme Analytic Ltd"), settings, loaded)
    assert f.sponsor_match.startswith("fuzzy(") and any("verify" in x for x in f.flags)


def test_register_not_loaded_is_reported_only_when_no_snapshot_exists(db, settings, job):
    assert reg.load_index(db) is None
    v = gates.triage(job, settings, reg.load_index(db))
    assert v.passed and "Sponsor register not loaded — sponsorship unchecked" in v.flags and v.sponsor_match is None


def test_triage_new_writes_the_match_columns_and_keeps_an_unmatched_job_triaged(db, settings, loaded, job):
    ok = dict(job, id="manual:ok", company="Northwind Health AI")
    no = dict(job, id="manual:no", company="Nobody Limited")
    for j in (ok, no):
        db.upsert_job(j)
    cli.triage_new(db, settings)
    a, b = db.job("manual:ok"), db.job("manual:no")
    assert (a["status"], a["sponsor_match"], a["sponsor_route"], a["sponsor_rating"]) == ("triaged", "exact", "Skilled Worker", "A")
    assert b["status"] == "triaged" and b["sponsor_match"] == "none" and gates.NOT_ON_REGISTER in json.loads(b["flags"])


# ---------------------------------------------------------------- the command
def test_careerops_sponsors_prints_the_summary_line_and_a_second_run_says_unchanged(settings, web, tmp_path, capsys):
    settings.raw["paths"]["register_dir"] = str(tmp_path / "reg")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    web.publish("2026-09-30", D1)
    cli.main(["--config", str(cfg), "sponsors"])
    assert "2026-09-30: 11 Skilled Worker sponsors (first snapshot, baseline)" in capsys.readouterr().out
    web.publish("2026-10-01", D2)
    cli.main(["--config", str(cfg), "sponsors"])
    assert "2026-10-01: 12 Skilled Worker sponsors · +2 added · −1 removed · 1 rating changes" in capsys.readouterr().out
    cli.main(["--config", str(cfg), "sponsors"])
    assert "Register unchanged (already have 2026-10-01)" in capsys.readouterr().out
