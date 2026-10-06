"""Phase 3: the outcome log, the funnel (counts, rates, 95% Wilson intervals, splits), no_response computed not stored, the
Excel sheet and the read-only archive backfill. No network, no model. The archive is always a folder under tmp_path:
nothing here may touch the real paths.applications."""
import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook

from careerops import cli
from careerops.apply.archive import mark_applied_unarchived
from careerops.db import DB
from careerops.reporting import outcomes as oc
from careerops.reporting.excel import export

TODAY = date(2026, 10, 30)


def add_job(db, jid, company="Acme", title="Data Scientist", **cols):
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": title})
    if cols:
        db.update_job(jid, **cols)


def log(db, jid, event, day, **kw):
    assert oc.record(db, jid, event, day, **kw)


def ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- table and events
def test_the_outcomes_table_exists_additively_and_old_databases_gain_it(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL, "
                "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
    con.execute("INSERT INTO jobs VALUES ('a:1','a','C','T','x','x','applied')")
    con.commit()
    con.close()
    db = DB(p)                                                       # the user's existing database: nothing dropped
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(outcomes)")]
    assert cols == ["job_id", "event", "on_date", "note", "source", "created_at"]
    assert db.conn.execute("SELECT status FROM jobs WHERE id='a:1'").fetchone()[0] == "applied"


def test_the_seven_events_and_validation(db):
    assert oc.EVENTS == ("applied", "acknowledged", "screen", "interview", "offer", "rejected", "withdrawn")
    add_job(db, "m:1")
    with pytest.raises(oc.OutcomeError, match="unknown event"):
        oc.record(db, "m:1", "ghosted", "2026-10-01")
    with pytest.raises(ValueError):
        oc.record(db, "m:1", "screen", "01/10/2026")
    assert oc.record(db, "m:1", "screen", "2026-10-01", "phone call", "manual") is True
    assert oc.record(db, "m:1", "screen", "2026-10-01") is False       # the same event on the same day is logged once
    row = db.conn.execute("SELECT * FROM outcomes").fetchone()
    assert (row["event"], row["on_date"], row["note"], row["source"]) == ("screen", "2026-10-01", "phone call", "manual")
    assert row["created_at"]


def test_mark_applied_writes_an_applied_event_even_when_the_archive_refused(db):
    add_job(db, "m:2", status="approved")
    mark_applied_unarchived(db, db.job("m:2"), "ref-1", "no archive root", date(2026, 10, 3))
    rows = [tuple(r) for r in db.conn.execute("SELECT job_id, event, on_date, source FROM outcomes")]
    assert rows == [("m:2", "applied", "2026-10-03", "mark-applied")]
    mark_applied_unarchived(db, db.job("m:2"), "ref-1", "again", date(2026, 10, 9))      # a resubmission: still one event
    assert db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1


# ---------------------------------------------------------------- statistics
@pytest.mark.parametrize("k,n,lo,hi", [(5, 10, 0.2366, 0.7634), (0, 10, 0.0, 0.2775), (10, 10, 0.7225, 1.0),
                                       (1, 20, 0.0089, 0.2361)])
def test_wilson_matches_known_values(k, n, lo, hi):
    got = oc.wilson(k, n)
    assert got[0] == pytest.approx(lo, abs=1e-3) and got[1] == pytest.approx(hi, abs=1e-3)


def test_wilson_of_nothing_is_none():
    assert oc.wilson(0, 0) is None


def test_a_later_stage_counts_the_earlier_ones_and_withdrawn_is_not_a_reply(db):
    for jid in ("j:offer", "j:interview", "j:screen", "j:ack", "j:rej", "j:wd", "j:none"):
        add_job(db, jid)
        log(db, jid, "applied", ago(40))
    log(db, "j:offer", "offer", ago(5))                              # no screen or interview logged: they are implied
    log(db, "j:interview", "interview", ago(6))
    log(db, "j:screen", "screen", ago(7))
    log(db, "j:ack", "acknowledged", ago(30))
    log(db, "j:rej", "rejected", ago(20))
    log(db, "j:wd", "withdrawn", ago(20))
    got = {f["stage"]: f["n"] for f in oc.funnel(oc.applied_jobs(db))}
    assert got == {"applied": 7, "any reply": 5, "screen": 3, "interview": 2, "offer": 1}
    top = oc.funnel(oc.applied_jobs(db))[1]
    assert top["of"] == 7 and top["rate"] == pytest.approx(5 / 7)
    assert (top["lo"], top["hi"]) == pytest.approx(oc.wilson(5, 7))


def test_a_job_applied_over_28_days_ago_with_nothing_since_is_no_response_computed_not_stored(db):
    add_job(db, "old")
    log(db, "old", "applied", ago(29))
    add_job(db, "edge")
    log(db, "edge", "applied", ago(28))
    add_job(db, "new")
    log(db, "new", "applied", ago(3))
    add_job(db, "wd")
    log(db, "wd", "applied", ago(60))
    log(db, "wd", "withdrawn", ago(50))
    add_job(db, "ack")
    log(db, "ack", "applied", ago(60))
    log(db, "ack", "acknowledged", ago(55))
    rep = oc.build_report(db, TODAY)
    assert rep["applied"] == 5 and rep["no_response"] == 1 and rep["pending"] == 2
    st = {a.job_id: a.status(TODAY) for a in oc.applied_jobs(db)}
    assert st == {"old": "no_response", "edge": "pending", "new": "pending", "wd": "withdrawn", "ack": "replied"}
    assert "no_response" not in {r[0] for r in db.conn.execute("SELECT event FROM outcomes")}     # never written
    assert oc.build_report(db, TODAY + timedelta(days=1))["no_response"] == 2                  # a day later 'edge' is too


def test_the_report_splits_by_family_tier_cv_pages_and_sponsor_match(db):
    rows = [("a", "data_scientist", 1, 2, "exact", "screen"), ("b", "data_scientist", 1, 2, "fuzzy(0.95)", None),
            ("c", "ai_engineer", 1, 1, "none", "offer"), ("d", None, None, None, None, "rejected")]
    for jid, fam, tier, pages, match, event in rows:
        add_job(db, jid, family=fam, tier=tier, cv_pages=pages, sponsor_match=match)
        log(db, jid, "applied", ago(40))
        if event:
            log(db, jid, "applied" if event == "applied" else event, ago(10))
    rep = oc.build_report(db, TODAY)
    n = lambda split, key: rep["splits"][split][key][0]["n"]
    assert [n("role family", k) for k in ("ai_engineer", "data_scientist", "unknown")] == [1, 2, 1]
    assert [n("tier", k) for k in ("T1", "unknown")] == [3, 1]
    assert [n("cv pages", k) for k in ("1", "2", "unknown")] == [1, 2, 1]
    assert set(rep["splits"]["sponsor match"]) == {"exact", "fuzzy", "none", "unknown"}       # fuzzy(0.95) -> fuzzy
    ds = {f["stage"]: f["n"] for f in rep["splits"]["role family"]["data_scientist"]}
    assert ds == {"applied": 2, "any reply": 1, "screen": 1, "interview": 0, "offer": 0}


def test_the_printed_report_shows_counts_rates_and_intervals_and_says_when_empty(db):
    assert "Nothing to report yet" in oc.format_report(oc.build_report(db, TODAY))
    add_job(db, "x", family="data_scientist", tier=1, cv_pages=2, sponsor_match="exact")
    log(db, "x", "applied", ago(40))
    log(db, "x", "screen", ago(30))
    text = oc.format_report(oc.build_report(db, TODAY))
    assert "1 applied job(s)" in text and "95% Wilson" in text
    lo, hi = oc.wilson(1, 1)
    assert f"[{lo:.0%} - {hi:.0%}]" in text and "By role family" in text and "By sponsor match" in text
    assert "By tier" in text and "By cv pages" in text


def test_an_event_without_an_applied_event_is_reported_not_silently_dropped(db):
    add_job(db, "lonely")
    log(db, "lonely", "screen", ago(1))
    rep = oc.build_report(db, TODAY)
    assert rep["applied"] == 0 and rep["orphans"] == ["lonely"] and "lonely" in oc.format_report(rep)


# ---------------------------------------------------------------- the commands
@pytest.fixture
def cfg(settings, tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return str(p)


def test_careerops_outcome_logs_an_event_and_refuses_unknown_jobs_and_events(db, cfg, capsys):
    add_job(db, "m:7")
    cli.main(["--config", cfg, "outcome", "--id", "m:7", "--event", "interview", "--date", "2026-10-12", "--note", "panel"])
    assert "m:7" in capsys.readouterr().out
    row = db.conn.execute("SELECT * FROM outcomes").fetchone()
    assert (row["event"], row["on_date"], row["note"], row["source"]) == ("interview", "2026-10-12", "panel", "manual")
    cli.main(["--config", cfg, "outcome", "--id", "m:7", "--event", "interview", "--date", "2026-10-12"])
    assert "already logged" in capsys.readouterr().out.lower() and db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", cfg, "outcome", "--id", "nope", "--event", "offer"])
    assert e.value.code == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", cfg, "outcome", "--id", "m:7", "--event", "ghosted"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--config", cfg, "outcome", "--id", "m:7", "--event", "offer", "--date", "yesterday"])


def test_the_date_defaults_to_today(db, cfg):
    add_job(db, "m:8")
    cli.main(["--config", cfg, "outcome", "--id", "m:8", "--event", "acknowledged"])
    assert db.conn.execute("SELECT on_date FROM outcomes").fetchone()[0] == datetime.now(timezone.utc).date().isoformat()


def test_careerops_outcomes_prints_the_funnel(db, cfg, capsys):
    add_job(db, "m:9", family="ai_engineer", tier=1)
    oc.record(db, "m:9", "applied", date.today() - timedelta(days=40))
    oc.record(db, "m:9", "screen", date.today() - timedelta(days=30))
    cli.main(["--config", cfg, "outcomes"])
    out = capsys.readouterr().out
    assert "1 applied job(s)" in out and "ai_engineer" in out and "T1" in out


# ---------------------------------------------------------------- Excel
def test_the_excel_export_has_an_outcomes_sheet(db, tmp_path):
    add_job(db, "m:10", company="Lendable", title="AI Engineer", family="ai_engineer", tier=1, cv_pages=2,
            sponsor_match="exact")
    log(db, "m:10", "applied", ago(40))
    log(db, "m:10", "screen", ago(30), note="call")
    add_job(db, "m:11", company="Monzo", title="Data Scientist")
    log(db, "m:11", "applied", ago(35))
    out = tmp_path / "x.xlsx"
    export(db, out)
    ws = load_workbook(out)["Outcomes"]
    head = [c.value for c in ws[1]]
    assert head[:4] == ["Company", "Title", "Applied on", "Status"] and "Events" in head and "Sponsor match" in head
    rows = {r[0].value: [c.value for c in r] for r in ws.iter_rows(min_row=2)}
    assert set(rows) == {"Lendable", "Monzo"}
    assert rows["Lendable"][head.index("Status")] == "screen" and "screen" in rows["Lendable"][head.index("Events")]
    assert rows["Monzo"][head.index("Status")] in ("no_response", "pending")


# ---------------------------------------------------------------- backfill from the archive
def tree(root: Path) -> list[tuple]:
    """Everything under root: path, kind, size, mtime_ns, content hash. Equal before and after = nothing was touched."""
    out = []
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out.append((p.relative_to(root).as_posix(), p.is_dir(), st.st_size, st.st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""))
    return out


@pytest.fixture
def archive(settings, tmp_path):
    root = settings.path("applications")
    assert tmp_path in root.parents, "the tests must never point at the real archive"
    for company, role, day in (("Lendable", "AI Engineer", "2025-11-03"), ("Lendable", "Data Scientist", "2025-12-09"),
                               ("Monzo", "ML Engineer", "2026-02-14")):
        d = root / company / role
        d.mkdir(parents=True)
        (d / "CV.pdf").write_bytes(b"%PDF fake " + role.encode())
        ts = datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()
        os.utime(d, (ts, ts))
    return root


def test_a_dry_run_lists_the_folders_and_writes_nothing_anywhere(db, settings, archive):
    before = tree(archive)
    props, skipped = oc.propose_from_archive(db, archive)
    assert [(p.company, p.role, p.applied_on.isoformat()) for p in props] == [
        ("Lendable", "AI Engineer", "2025-11-03"), ("Lendable", "Data Scientist", "2025-12-09"),
        ("Monzo", "ML Engineer", "2026-02-14")]
    assert skipped == [] and tree(archive) == before
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_apply_writes_manual_jobs_and_approximate_applied_events_to_the_db_only(db, settings, archive):
    before = tree(archive)
    props, _ = oc.propose_from_archive(db, archive)
    assert oc.apply_proposals(db, props) == 3
    assert tree(archive) == before                                     # the archive: byte for byte, mtime for mtime
    jobs = {r["id"]: r for r in db.conn.execute("SELECT * FROM jobs")}
    assert len(jobs) == 3 and all(j["source"] == "archive" and j["status"] == "applied" for j in jobs.values())
    lend = jobs[oc.archive_job_id("Lendable", "AI Engineer")]
    assert (lend["company"], lend["title"]) == ("Lendable", "AI Engineer") and lend["applied_at"].startswith("2025-11-03")
    ev = [tuple(r) for r in db.conn.execute("SELECT job_id, event, on_date, source, note FROM outcomes ORDER BY on_date")]
    assert [e[1:4] for e in ev] == [("applied", "2025-11-03", "archive"), ("applied", "2025-12-09", "archive"),
                                    ("applied", "2026-02-14", "archive")]
    assert all("approximate" in e[4] for e in ev)
    rep = oc.build_report(db, TODAY)
    assert rep["applied"] == 3 and rep["approximate"] == 3 and "approximate" in oc.format_report(rep)


def test_the_import_can_be_run_again_and_skips_folders_the_pipeline_archived(db, settings, archive):
    props, _ = oc.propose_from_archive(db, archive)
    oc.apply_proposals(db, props)
    again, skipped = oc.propose_from_archive(db, archive)
    assert again == [] and len(skipped) == 3
    (archive / "Zed" / "Role One").mkdir(parents=True)
    (archive / "Acme" / "Mine").mkdir(parents=True)
    db.conn.execute("INSERT INTO archive_index VALUES ('Acme/Mine', 'role', 'acme', 'm:1', 'x')")   # archived by A1
    db.conn.commit()
    new, skipped = oc.propose_from_archive(db, archive)
    assert [p.folder for p in new] == ["Zed/Role One"] and "Acme/Mine" in skipped


def test_a_missing_archive_folder_is_an_error_not_a_crash(db, tmp_path):
    with pytest.raises(oc.OutcomeError, match="not found"):
        oc.propose_from_archive(db, tmp_path / "nope")


def test_cli_import_is_a_dry_run_unless_apply_is_given(db, settings, archive, cfg, capsys):
    before = tree(archive)
    cli.main(["--config", cfg, "outcomes", "--import-archive"])
    out = capsys.readouterr().out
    assert "dry run" in out.lower() and "Lendable / AI Engineer" in out and "2025-11-03" in out
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0 and tree(archive) == before
    cli.main(["--config", cfg, "outcomes", "--import-archive", "--apply"])
    assert "Imported 3" in capsys.readouterr().out
    assert db.conn.execute("SELECT COUNT(*) FROM jobs WHERE source='archive'").fetchone()[0] == 3 and tree(archive) == before
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", cfg, "outcomes", "--apply"])               # --apply alone writes nothing: it needs the import
    assert e.value.code == 1
