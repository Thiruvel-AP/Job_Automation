"""`--mock` runs on its own data root (paths.mock_root): the real DB, outputs, exports and archive are never opened."""
import hashlib
import os
from pathlib import Path

import pytest
import yaml

from careerops import cli, mockroot
from careerops.config import ROOT, Settings, load_settings
from careerops.db import DB
from conftest import REAL_DATA


def _tree_hash(folder: Path) -> str:
    """One hash over every file's relative path and bytes (so a changed, added or removed file shows)."""
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        h.update(str(p.relative_to(folder)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def world(tmp_path, settings):
    """A 'real' data folder holding one real triaged job + a finished output folder, plus a config pointing at it
    and at a separate mock root. Returns (config path, real folder, mock folder)."""
    real, mock = tmp_path / "real", tmp_path / "mock"
    raw = settings.raw
    raw["paths"].update({"db": str(real / "careerops.db"), "outputs": str(real / "outputs"),
                         "excel": str(real / "exports" / "companies.xlsx"), "tmp": str(real / "tmp"),
                         "applications": str(real / "Companies"), "mock_root": str(mock)})
    db = DB(real / "careerops.db")
    db.upsert_job({"id": "gh:real:1", "source": "greenhouse", "company": "Realco", "title": "Data Scientist",
                   "location": "London, UK", "url": "u", "apply_url": "u", "description": "d" * 900})
    db.update_job("gh:real:1", status="triaged")
    db.log("gh:real:1", "tailor", {"score": 0.8})
    db.conn.close()                                           # checkpoints the WAL: the file is the whole state
    (real / "outputs" / "Realco_Data_Scientist_1").mkdir(parents=True)
    (real / "outputs" / "Realco_Data_Scientist_1" / "Ada CV.pdf").write_bytes(b"REAL CV")
    (real / "Companies" / "Realco").mkdir(parents=True)
    (real / "Companies" / "Realco" / "Ada CV.pdf").write_bytes(b"SUBMITTED CV")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg, real, mock


def _mock_db(mock: Path) -> DB:
    return DB(mock / "careerops.db")


@pytest.mark.parametrize("cmd", [["run", "--mock", "--limit", "3"], ["tailor", "--mock", "--limit", "3"]])
def test_a_mock_run_leaves_the_real_data_byte_for_byte_unchanged(world, cmd, capsys):
    cfg, real, mock = world
    before = _tree_hash(real)
    db_before = hashlib.sha256((real / "careerops.db").read_bytes()).hexdigest()
    cli.main(["--config", str(cfg), *cmd])
    assert _tree_hash(real) == before
    assert hashlib.sha256((real / "careerops.db").read_bytes()).hexdigest() == db_before
    out = capsys.readouterr().out
    assert "MOCK data root" in out and str(mock) in out and "Tailoring 1 job(s) with MockLLM" in out
    db = _mock_db(mock)
    seeded = db.job(mockroot.SEED_JOB_ID)
    assert seeded["status"] == "needs_review" and seeded["out_dir"].startswith(str(mock / "outputs"))
    assert [r["id"] for r in db.jobs()] == [mockroot.SEED_JOB_ID]       # the real job is not in the mock DB
    assert (Path(seeded["out_dir"]) / "report.json").exists()
    real_db = DB(real / "careerops.db")
    assert real_db.job("gh:real:1")["status"] == "triaged"             # untouched (and not tailored)


def test_run_exports_into_the_mock_root_and_skips_discovery(world, monkeypatch, capsys):
    cfg, real, mock = world

    def no_network(*a, **k):
        raise AssertionError("a mock run must not fetch jobs")

    monkeypatch.setattr(cli.sources, "fetch_all", no_network)
    cli.main(["--config", str(cfg), "run", "--mock", "--limit", "1"])
    assert (mock / "companies.xlsx").exists() and not (real / "exports").exists()   # outside data/: by file name
    assert "Discovery skipped under --mock" in capsys.readouterr().out


def test_every_mock_run_tailors_the_seed_job_afresh(world):
    cfg, real, mock = world
    for _ in range(2):
        cli.main(["--config", str(cfg), "run", "--mock", "--limit", "1"])
        events = [r["stage"] for r in _mock_db(mock).conn.execute(
            "SELECT stage FROM events WHERE job_id=?", (mockroot.SEED_JOB_ID,))]
        assert _mock_db(mock).job(mockroot.SEED_JOB_ID)["status"] == "needs_review"
    assert events.count("tailor") == 2                                   # tailored on both runs, not skipped on the 2nd


def test_a_seed_job_the_rules_now_reject_fails_loudly(world, settings):
    cfg, real, mock = world
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["roles"]["levels"]["exclude"].append(r"\bai engineer\b")
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="mock seed job is 'skipped'"):
        cli.main(["--config", str(cfg), "run", "--mock"])


def test_without_mock_the_real_paths_are_used(world, monkeypatch):
    cfg, real, mock = world
    s, db = cli._ctx(type("A", (), {"config": str(cfg), "mock": False})())
    assert Path(db.conn.execute("PRAGMA database_list").fetchone()["file"]) == real / "careerops.db"
    assert not mock.exists() and s.path("outputs") == real / "outputs"


# ---------------------------------------------------------------- the path mapping itself (no files opened)
def test_default_settings_map_every_generated_path_under_data_mock():
    s = load_settings()
    m = mockroot.mock_settings(s)
    mock = os.path.join(REAL_DATA, "mock")
    for key in s.raw["paths"]:
        if key in mockroot.KEEP_AS_IS:
            assert m.path(key) == s.path(key)
            continue
        assert str(m.path(key)).startswith(mock + os.sep), key
        assert m.path(key) != s.path(key)
    assert m.path("db") == Path(mock) / "careerops.db" and m.path("outputs") == Path(mock) / "outputs"
    assert m.path("excel") == Path(mock) / "exports" / "companies.xlsx"
    assert m.path("applications") == Path(mock) / "applications"           # never the user's real archive
    assert s.path("db") == Path(REAL_DATA) / "careerops.db"                # the original is not modified


def test_paths_outside_data_are_redirected_too_not_reused(tmp_path):
    raw = {"paths": {"db": "data/careerops.db", "applications": "/somewhere/else/Companies",
                     "mystery": "/elsewhere/thing.csv", "fact_bank": "config/profile/fact_bank.yaml",
                     "mock_root": "data/mock"}}
    m = mockroot.mock_settings(Settings(raw=raw, root=tmp_path))
    assert m.path("applications") == tmp_path / "data/mock/applications"
    assert m.path("mystery") == tmp_path / "data/mock/thing.csv"
    assert m.path("fact_bank") == tmp_path / "config/profile/fact_bank.yaml"
    assert m.path("db") == tmp_path / "data/mock/careerops.db"


def test_a_config_without_mock_root_still_gets_data_mock(tmp_path):
    m = mockroot.mock_settings(Settings(raw={"paths": {"db": "data/careerops.db"}}, root=tmp_path))
    assert m.path("db") == tmp_path / "data/mock/careerops.db"


def test_the_seed_job_is_the_northwind_fixture():
    j = mockroot.seed_job()
    assert j["id"].startswith("mock:") and j["title"] == "AI Engineer" and "LLM agents" in j["description"]
