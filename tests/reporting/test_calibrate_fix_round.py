"""Fix round from calibration report 20261006-151213, calibrate side: the sponsor flag in the log and report (the JD is still
tailored), page count and page-2 fill in the per-JD table, and the stale-rubric check of `--summarise`. MockLLM, tmp_path."""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from careerops import cli
from careerops.discovery import register as reg
from careerops.reporting import calibrate as cal
from test_calibrate import _fill, _report_with_rows, _row, _run, fb, manifest   # noqa: F401 — fb / manifest are fixtures

FIX = Path(__file__).resolve().parents[1] / "fixtures"
BANNER = "RUBRIC PREDATES THIS RUN — grades are not for these outputs"
STAMP = "20261004-120000"


def _manifest_for(tmp_path, company):
    jd = tmp_path / "nw.txt"
    shutil.copy(FIX / "tailoring" / "jd_northwind.txt", jd)
    path = tmp_path / "m1.yaml"
    path.write_text(yaml.safe_dump([{"id": "cal-nw", "company": company, "title": "AI Engineer", "jd_file": str(jd)}]),
                    encoding="utf-8")
    return path


def _load_register(db):
    snap = reg.load_snapshot(FIX / "discovery" / "register_d1.csv")
    reg.apply_diff(db, "2026-09-30", reg.diff({}, snap), snap, snapshot=("2026-09-30", "u", "p", len(snap)))


# ---------------------------------------------------------------- 1. not on the register: a flag, never a skip
def test_an_employer_off_the_register_is_flagged_in_the_log_and_the_report_and_the_jd_is_still_tailored(
        settings, db, fb, tmp_path):
    _load_register(db)
    log = []
    res = _run(settings, db, fb, _manifest_for(tmp_path, "Nobody At All Ltd"), tmp_path / "out", log=log.append)
    line = next(l for l in log if l.startswith("- cal-nw"))
    assert "triage passed" in line and "flag: not on sponsor register" in line.lower()      # flagged, not skipped
    row = res["rows"][0]
    assert row["status"] == "ok" and row["triage"]["passed"] and "Not on sponsor register" in row["triage"]["flags"]
    assert "flags: Not on sponsor register" in res["report"].read_text(encoding="utf-8")
    log.clear()                                                                       # on the register: no flag
    ok = _run(settings, db, fb, _manifest_for(tmp_path, "Northwind Health AI"), tmp_path / "out2", log=log.append)
    assert "flag:" not in next(l for l in log if l.startswith("- cal-nw"))
    assert "Not on sponsor register" not in ok["rows"][0]["triage"]["flags"]


# ---------------------------------------------------------------- 2. page 2 fill in the per-JD table
def test_the_per_jd_table_has_the_page_count_and_page_two_fill(settings, db, fb, manifest, tmp_path):
    res = _run(settings, db, fb, manifest, tmp_path / "out", only=["cal-northwind"])
    row = res["rows"][0]
    assert row["final"]["pages"] == 2 and 0.0 < row["final"]["page2_fill"] <= 1.0
    text = res["report"].read_text(encoding="utf-8")
    header = next(l for l in text.splitlines() if l.startswith("| id | tier"))
    assert "| pages | page 2 fill |" in header
    line = next(l for l in text.splitlines() if l.startswith("| cal-northwind |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert len(cells) == len(cols)
    assert cells[cols.index("pages")] == "2" and cells[cols.index("page 2 fill")] == f"{row['final']['page2_fill']:.0%}"
    assert cal._row_line({"id": "x", "status": "error", "error": "boom"}).count("|") == header.count("|")


# ---------------------------------------------------------------- 5. a rubric older than the run
def _graded(tmp_path, settings):
    rows = [_row(i, ceiling=0.9, final=0.78, band="in") for i in ("a", "b", "c", "d")]
    _report_with_rows(tmp_path, rows, settings, stamp=STAMP)
    cal.write_rubric(tmp_path / "rubric.csv", ["a", "b", "c", "d"])
    good = {"cv_false_claim(Y/N)": "N", "letter_false_claim(Y/N)": "N", "would_send(Y/N)": "Y"}
    _fill(tmp_path / "rubric.csv", a=good, b=good, c=good, d=good)


def _set_mtime(path, when):
    os.utime(path, (when, when))


def test_a_rubric_older_than_the_run_is_flagged_and_its_graded_criteria_are_neither_ticked_nor_crossed(tmp_path, settings):
    _graded(tmp_path, settings)
    started = datetime.strptime(STAMP, "%Y%m%d-%H%M%S").timestamp()
    _set_mtime(tmp_path / "rubric.csv", started - 86400)                              # graded the day before the run
    assert cal.rubric_predates_run(tmp_path)
    text = cal.summarise(tmp_path).read_text(encoding="utf-8")
    assert BANNER in text and text.index(BANNER) < text.index("| id | final | letter |")      # above the table
    for name in ("zero false claims", "would_send"):
        assert f"✓ {name}" not in text and f"✗ {name}" not in text
    assert "(not assessed: rubric predates this run) zero false claims" in text
    assert "(not assessed: rubric predates this run) would_send" in text
    assert "✓ band hit rate" in text and "✓ no letter with rule problems" in text         # run-based criteria still count


def test_a_rubric_saved_after_the_run_is_graded_normally(tmp_path, settings):
    _graded(tmp_path, settings)
    assert not cal.rubric_predates_run(tmp_path)
    text = cal.summarise(tmp_path).read_text(encoding="utf-8")
    assert "PREDATES" not in text and "✓ zero false claims" in text and "✓ would_send" in text


def test_a_rubric_the_run_wrote_and_nobody_has_saved_since_predates_it_until_it_is_edited(tmp_path, settings, db, fb,
                                                                                         manifest):
    out = tmp_path / "out"
    res = _run(settings, db, fb, manifest, out, only=["cal-northwind"])
    sidecar = json.loads(res["json"].read_text(encoding="utf-8"))
    assert sidecar["meta"]["rubric_mtime_at_run"] == res["rubric"].stat().st_mtime
    assert cal.rubric_predates_run(out)                        # the blank rubric the run wrote: no grades for these outputs
    _fill(res["rubric"], **{"cal-northwind": {"would_send(Y/N)": "Y"}})
    _set_mtime(res["rubric"], res["rubric"].stat().st_mtime + 5)
    assert not cal.rubric_predates_run(out)


def test_the_cli_prints_the_banner_when_summarising_a_stale_rubric(tmp_path, settings, capsys):
    _report_with_rows(tmp_path, [_row("a")], settings, stamp=STAMP)
    cal.write_rubric(tmp_path / "rubric.csv", ["a"])
    _set_mtime(tmp_path / "rubric.csv", datetime.strptime(STAMP, "%Y%m%d-%H%M%S").timestamp() - 3600)
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "calibrate", "--summarise", "--out", str(tmp_path)])
    assert BANNER in capsys.readouterr().out


# ---------------------------------------------------------------- needs_result in a calibration run
def test_a_calibration_run_records_the_bullets_without_a_result_in_report_json_the_row_and_the_log(
        settings, db, fb, manifest, tmp_path):
    log = []
    res = _run(settings, db, fb, manifest, tmp_path / "out", only=["cal-northwind"], log=log.append)
    row = res["rows"][0]
    rep = json.loads((Path(row["out_dir"]) / "report.json").read_text(encoding="utf-8"))
    assert "needs_result" in rep and [b["id"] for b in rep["needs_result"]] == row["needs_result"]
    bank = {b["id"]: b for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    assert row["needs_result"] and all(not str(bank[i].get("result") or "").strip() for i in row["needs_result"])
    line = next(l for l in log if "bullet(s) need a result" in l)
    assert f"{len(row['needs_result'])} bullet(s) need a result" in line


# ---------------------------------------------------------------- keyword placement per JD
def test_a_calibration_row_and_report_say_which_missed_keywords_were_placed_not_placed_or_blocked(
        settings, db, fb, manifest, tmp_path):
    res = _run(settings, db, fb, manifest, tmp_path / "out", only=["cal-northwind"])
    row = res["rows"][0]
    assert set(row["placement"]) == {"placed", "not_placed", "blocked", "unoffered"}
    assert all(isinstance(v, list) for v in row["placement"].values())
    text = res["report"].read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("- keyword placement:"))
    assert "placed" in line and "not placed" in line and "blocked" in line
    assert cal._placement_line(None) == [] and cal._placement_line({}) == []          # an old row without it still renders
    assert "2 rewording(s) refused" in cal._placement_line({"placed": []}, 2)[0]
    assert row["rephrase"]["regressed"] >= 0                                             # refused-for-regression count is kept
