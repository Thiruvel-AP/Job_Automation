"""The conftest guard that stops tests touching the real data/ folder (careerops.db, outputs/, exports/ ...)."""
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from careerops.config import ROOT
from careerops.db import DB
from conftest import REAL_DATA, real_data_hits

PROBE = ROOT / "data" / "__guard_probe__.db"


def test_guard_blocks_open_sqlite_pathlib_os_open_and_relative_paths(monkeypatch):
    assert not PROBE.exists()
    with real_data_hits() as hits:
        with pytest.raises(PermissionError):
            open(PROBE, "w")
        with pytest.raises(PermissionError):
            sqlite3.connect(str(PROBE))
        with pytest.raises(PermissionError):
            DB(PROBE)
        with pytest.raises(PermissionError):
            PROBE.write_text("x")
        with pytest.raises(PermissionError):
            os.open(PROBE, os.O_RDONLY)
        monkeypatch.chdir(ROOT)
        with pytest.raises(PermissionError):
            open("data/careerops.db", "rb")                     # relative, and the real database itself
        with pytest.raises(PermissionError):
            open(ROOT / "data" / ".." / "data" / "x", "w")      # ../ does not hide it
        assert len(hits) == 7
    assert not PROBE.exists()


def test_guard_allows_tmp_paths_and_the_rest_of_the_repo(tmp_path):
    DB(tmp_path / "x.db")
    (tmp_path / "f.txt").write_text("ok")
    assert (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert not str(tmp_path).startswith(REAL_DATA)


def test_a_test_that_swallows_the_error_still_fails_in_teardown(tmp_path):
    """A throwaway test opens the real data/ and hides the PermissionError; the guard must still fail it."""
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_conftest", {str(ROOT / "tests" / "conftest.py")!r})
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        no_real_data = guard.no_real_data
    """), encoding="utf-8")
    (tmp_path / "test_sneaky.py").write_text(textwrap.dedent(f"""
        def test_sneaky():
            try:
                open({str(PROBE)!r}, "w")
            except PermissionError:
                pass
    """), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode != 0 and "opened the real data/ folder" in run.stdout, run.stdout
    assert not PROBE.exists()
