from __future__ import annotations

import json

from careerops import ats_structure, pathstore


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
    old = {"out_dir": "/mnt/d/VS_Codes/X/career-ops/data/outputs/j1", "cv_pdf": "/mnt/d/VS_Codes/X/career-ops/data/outputs/j1/cv.pdf"}
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
