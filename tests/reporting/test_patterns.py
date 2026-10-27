"""`careerops patterns` (reporting/patterns.py): applications, replies, interviews and rejections by role family, ATS kind and triage band, from the tracker
Sheet's Status column. Information only: it may suggest, it never changes a setting. The Sheet is `FakeSheet`; no network."""
import copy

import pytest
import yaml

from careerops import cli, tracker
from careerops.reporting import patterns
from outreach_fakes import FakeSheet

TAB = "Applications"


def seed(db, n, company, *, t=None, status="Applied", family="data_scientist", url="https://boards.greenhouse.io/x/jobs/1", sheet_rows=None):
    jid = f"manual:p{n}"
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": "Data Scientist", "description": "x", "url": url})
    db.update_job(jid, status="applied", applied_at="2026-10-01T09:00:00+00:00", family=family)
    if t is not None:
        db.conn.execute("INSERT INTO triage_scores (key, company, title, t, factors, scored_on) VALUES (?,?,?,?,?,?)",
                        ("|".join(__import__("careerops.apply.decided", fromlist=["x"]).key_of(company, "Data Scientist")), company, "Data Scientist", t, "{}", "2026-10-01"))
        db.conn.commit()
    if sheet_rows is not None:
        sheet_rows.append([jid if h == "Job ID" else status if h == "Status" else family if h == "Role family" else url if h == "Posting URL" else ""
                           for h in tracker.HEADERS])
    return jid


def sheet(rows):
    return FakeSheet([list(tracker.HEADERS), *rows])


# ------------------------------------------------------------------ the rows
@pytest.mark.parametrize("status,replied,interviewed,rejected", [
    ("", False, False, False), ("Applied", False, False, False), ("Withdrawn", False, False, False), ("No response", False, False, False),
    ("Rejected", True, False, True), ("Interview", True, True, False), ("Assessment", True, True, False), ("Offer", True, True, False),
    ("Acknowledged", True, False, False), ("Phone screen", True, True, False)])
def test_what_a_status_counts_as(status, replied, interviewed, rejected):
    r = patterns.Row("x", status)
    assert (r.replied, r.interviewed, r.rejected) == (replied, interviewed, rejected)


def test_rows_are_read_from_the_sheets_status_column_by_header_text(db):
    rows = []
    seed(db, 1, "Acme", status="Interview", sheet_rows=rows, family="ai_engineer")
    seed(db, 2, "Bolt", status="Rejected", sheet_rows=rows)
    got = patterns.rows_from_sheet(sheet(rows), TAB, db)
    assert [(r.job_id, r.status, r.family) for r in got] == [("manual:p1", "Interview", "ai_engineer"), ("manual:p2", "Rejected", "data_scientist")]
    assert got[0].kind == "greenhouse"


def test_a_sheet_with_other_column_order_and_missing_trailing_cells_is_read(db):
    jid = seed(db, 1, "Acme")
    s = FakeSheet([["Notes", "Status", "Job ID"], ["", "Rejected", jid], ["", "", "manual:nope"]])
    got = patterns.rows_from_sheet(s, TAB, db)
    assert [(r.job_id, r.status) for r in got] == [(jid, "Rejected"), ("manual:nope", "")]


def test_a_tab_without_the_columns_is_an_error(db):
    with pytest.raises(tracker.TrackerError, match="Status"):
        patterns.rows_from_sheet(FakeSheet([["Company"], ["x"]]), TAB, db)


def test_the_database_fallback_derives_the_status_from_the_outcome_log(db):
    a, b, c = (seed(db, i, f"Co{i}") for i in (1, 2, 3))
    for jid, ev in ((a, "interview"), (b, "rejected"), (b, "acknowledged")):
        db.conn.execute("INSERT INTO outcomes (job_id, event, on_date, source) VALUES (?,?,?,?)", (jid, ev, "2026-10-10", "manual"))
    db.conn.commit()
    assert {r.job_id: r.status for r in patterns.rows_from_db(db)} == {a: "Interview", b: "Rejected", c: "Applied"}


# ------------------------------------------------------------------ the tallies
def test_counts_per_family_kind_and_band(db):
    rows = []
    seed(db, 1, "A", t=4.3, status="Interview", family="ai_engineer", sheet_rows=rows)
    seed(db, 2, "B", t=4.1, status="Rejected", family="ai_engineer", sheet_rows=rows)
    seed(db, 3, "C", t=3.7, status="Applied", family="data_scientist", url="https://jobs.lever.co/c/1", sheet_rows=rows)
    seed(db, 4, "D", status="Applied", family="data_scientist", sheet_rows=rows)
    got = patterns.rows_from_sheet(sheet(rows), TAB, db)
    fam = patterns.tally(got, lambda r: r.family)
    assert (fam["ai_engineer"].n, fam["ai_engineer"].replies, fam["ai_engineer"].interviews, fam["ai_engineer"].rejections) == (2, 2, 1, 1)
    assert fam["data_scientist"].replies == 0
    kinds = patterns.tally(got, lambda r: r.kind)
    assert kinds["lever"].n == 1 and kinds["greenhouse"].n == 3
    bands = patterns.tally(got, lambda r: patterns.band_of(r.t))
    assert (bands["T >= 4.0"].n, bands["T 3.5-4.0"].n, bands["T unknown"].n) == (2, 1, 1)
    assert patterns.band_of(4.0) == "T >= 4.0" and patterns.band_of(3.99) == "T 3.5-4.0" and patterns.band_of(3.4) == "T < 3.5"


def test_the_report_names_every_group_and_the_rate(db, settings):
    rows = []
    seed(db, 1, "A", t=4.3, status="Interview", sheet_rows=rows)
    seed(db, 2, "B", t=3.6, status="Applied", sheet_rows=rows)
    text = "\n".join(patterns.report(patterns.rows_from_sheet(sheet(rows), TAB, db), settings, "tracker Sheet, Status column"))
    for part in ("By role family:", "By ATS kind:", "By triage score:", "data_scientist: 2 applied · 1 replies (50%) · 1 interviews · 0 rejections",
                 "T >= 4.0: 1 applied", "T 3.5-4.0: 1 applied"):
        assert part in text
    assert "information only" in text


def test_an_empty_log_says_so(settings):
    assert "no applications on record" in patterns.report([], settings, "outcome log")[0]


# ------------------------------------------------------------------ suggestions, never changes
def two_bands(db, high_replies, high_n, low_replies, low_n):
    rows = []
    n = 0
    for band_t, replies, count in ((4.2, high_replies, high_n), (3.7, low_replies, low_n)):
        for i in range(count):
            n += 1
            seed(db, n, f"Co{n}", t=band_t, status="Interview" if i < replies else "Applied", sheet_rows=rows)
    return patterns.rows_from_sheet(sheet(rows), TAB, db)


def test_the_agreed_suggestion_when_the_better_band_leads_clearly(db, settings):
    rows = two_bands(db, 3, 9, 0, 7)
    assert patterns.suggestions(rows, settings) == [
        "T >= 4.0 had 3/9 replies; T 3.5-4.0 had 0/7: consider `min_triage_score: 4.0` (your call: nothing was changed)"]


@pytest.mark.parametrize("high,low", [((3, 4), (0, 7)), ((1, 9), (1, 9)), ((3, 9), (0, 4)), ((2, 9), (1, 7))])
def test_a_band_that_is_too_small_or_a_lead_that_is_too_thin_suggests_nothing(db, settings, high, low):
    assert patterns.suggestions(two_bands(db, *high, *low), settings) == []         # 4 applications is too few; no lead; a lead under 20 points


def test_a_threshold_you_already_have_is_not_suggested(db, settings):
    rows = two_bands(db, 3, 9, 0, 7)
    settings.raw["targeting"]["min_triage_score"] = 4.0
    assert patterns.suggestions(rows, settings) == []


def test_it_never_changes_a_setting(db, settings):
    before = copy.deepcopy(settings.raw)
    patterns.report(two_bands(db, 3, 9, 0, 7), settings, "x")
    assert settings.raw == before


# ------------------------------------------------------------------ the command
@pytest.fixture
def cfg(tmp_path, settings):
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return p


def test_the_command_with_db_prints_the_report_and_writes_nothing(cfg, settings, db, capsys):
    seed(db, 1, "A", t=4.3)
    before = (cfg.read_bytes(), db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
    cli.main(["--config", str(cfg), "patterns", "--db"])
    out = capsys.readouterr().out
    assert "outcome log" in out and "By role family:" in out
    assert before == (cfg.read_bytes(), db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])


def test_the_command_falls_back_to_the_database_when_the_sheet_cannot_be_read(cfg, settings, db, capsys):
    seed(db, 1, "A", t=4.3)
    cli.main(["--config", str(cfg), "patterns"])                                       # tracker is configured but nobody is signed in
    out = capsys.readouterr().out
    assert "could not be read" in out and "using the outcome log" in out and "By role family:" in out
