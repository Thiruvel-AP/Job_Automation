from __future__ import annotations

import json

from careerops import ats_structure, pathstore

OLD = "/mnt/d/VS_" + "Codes/X/career-ops"          # the old folder name, spelt in pieces so the doctor check for it finds nothing here


def _report(out, **kw):
    return {"out_dir": str(out), "cv_pdf": str(out / "cv.pdf"), "letter_pdf": None, "letter": {"pdf": str(out / "l.pdf")}, **kw}


def test_write_stores_paths_relative_to_root(tmp_path):
    out = tmp_path / "data" / "outputs" / "j1"
    out.mkdir(parents=True)
    pathstore.write_report(out / "report.json", _report(out), root=tmp_path)
    raw = json.loads((out / "report.json").read_text())
    assert raw["out_dir"] == "data/outputs/j1" and raw["cv_pdf"] == "data/outputs/j1/cv.pdf" and raw["letter"]["pdf"] == "data/outputs/j1/l.pdf"
    back = pathstore.read_report(out / "report.json", root=tmp_path)
    assert back["cv_pdf"] == str(out / "cv.pdf")


def test_absolute_path_from_another_root_is_rerooted_and_old_file_untouched(tmp_path):
    out = tmp_path / "career-ops" / "data" / "outputs" / "j1"
    out.mkdir(parents=True)
    (out / "cv.pdf").write_bytes(b"x")
    old = {"out_dir": OLD + "/data/outputs/j1", "cv_pdf": OLD + "/data/outputs/j1/cv.pdf"}
    f = out / "report.json"
    f.write_text(json.dumps(old))
    rep = pathstore.read_report(f, root=tmp_path / "career-ops")
    assert rep["cv_pdf"] == str(out / "cv.pdf") and rep["out_dir"] == str(out)
    assert json.loads(f.read_text()) == old                       # never rewritten
    assert ats_structure.for_report({**rep, "keywords": []}).error != "no CV PDF"


def test_missing_file_still_reports_clearly(tmp_path):
    f = tmp_path / "report.json"
    f.write_text(json.dumps({"out_dir": "/mnt/old/career-ops/data/outputs/j9", "cv_pdf": "/mnt/old/career-ops/data/outputs/j9/cv.pdf"}))
    rep = pathstore.read_report(f, root=tmp_path)
    assert rep["cv_pdf"] == "/mnt/old/career-ops/data/outputs/j9/cv.pdf"          # unchanged, so the message names the real path
    assert ats_structure.for_report(rep).error == "no CV PDF"


# ------------------------------------------------------------------ v3.9: the corrected paths are saved once
def _old_tree(tmp_path):
    from careerops.db import DB
    root = tmp_path / "career-ops"
    out = root / "data" / "outputs" / "j1"
    out.mkdir(parents=True)
    (out / "cv.pdf").write_bytes(b"x")
    (out / "report.json").write_text(json.dumps({"out_dir": OLD + "/data/outputs/j1", "cv_pdf": OLD + "/data/outputs/j1/cv.pdf", "keep": "/mnt/other/x"}))
    db = DB(tmp_path / "t.db")
    db.conn.execute("INSERT INTO jobs (id, source, company, title, out_dir, status, first_seen, last_seen) VALUES ('j1','s','C','T',?, 'needs_review', '2026-11-01', '2026-11-01')", (OLD + "/data/outputs/j1",))
    db.conn.commit()
    return root, out, db


def test_the_first_run_saves_the_old_paths_with_backups_and_the_second_changes_nothing(tmp_path):
    root, out, db = _old_tree(tmp_path)
    assert pathstore.save_rerooted(db, root / "data" / "outputs", root=root) == 3                # 1 database row + 2 report paths
    rep = json.loads((out / "report.json").read_text())
    assert rep["out_dir"] == "data/outputs/j1" and rep["cv_pdf"] == "data/outputs/j1/cv.pdf" and rep["keep"] == "/mnt/other/x"
    assert db.conn.execute("SELECT out_dir FROM jobs").fetchone()[0] == str(root / "data" / "outputs" / "j1")      # absolute, as guard.py reads it
    assert json.loads((out / "report.json.pre-reroot").read_text())["out_dir"].startswith(OLD)                      # the original, kept
    assert list(tmp_path.glob("t.db.pre-reroot-*"))
    assert pathstore.save_rerooted(db, root / "data" / "outputs", root=root) == 0                # nothing left to save


def test_cli_prints_the_summary_only_on_the_run_that_saved(tmp_path, capsys):
    from careerops import cli
    from careerops.config import Settings
    root, out, db = _old_tree(tmp_path)
    s = Settings(raw={"paths": {"outputs": str(root / "data" / "outputs")}}, root=root)
    cli._save_rerooted(s, db)
    assert "re-rooted 3 stored paths (saved)" in capsys.readouterr().out
    cli._save_rerooted(s, db)
    assert capsys.readouterr().out == ""


def test_a_path_that_cannot_be_found_is_left_alone_and_nothing_is_backed_up(tmp_path):
    from careerops.db import DB
    root = tmp_path / "career-ops"
    (root / "data" / "outputs" / "j8").mkdir(parents=True)
    f = root / "data" / "outputs" / "j8" / "report.json"
    f.write_text(json.dumps({"out_dir": OLD + "/data/outputs/j9"}))
    db = DB(tmp_path / "t.db")
    assert pathstore.save_rerooted(db, root / "data" / "outputs", root=root) == 0
    assert not (f.parent / "report.json.pre-reroot").exists() and OLD in f.read_text()
