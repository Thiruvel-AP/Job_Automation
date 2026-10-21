"""No test may read the user's real config/settings.yaml. The user sets values there on purpose (apply.mode: auto, their own
folders), and a test that read it failed the day they did. Every test uses tests/fixtures/config/settings.yaml (a bare
load_settings() already does, through CAREEROPS_SETTINGS) or writes its own file in tmp_path; conftest.py holds the guard."""
import os
import subprocess
import sys
import textwrap

import pytest
import yaml

from careerops.config import ROOT, SETTINGS_ENV, load_settings
from conftest import REAL_SETTINGS, SETTINGS_FIX, real_settings_hits

REAL = ROOT / "config" / "settings.yaml"


def test_the_real_settings_file_is_the_one_the_guard_watches():
    assert REAL_SETTINGS == os.path.realpath(REAL) and REAL.is_file() and SETTINGS_FIX.is_file()
    assert SETTINGS_FIX.resolve() != REAL.resolve()


def test_a_bare_load_settings_reads_the_test_copy_and_never_the_real_file(monkeypatch):
    assert os.environ[SETTINGS_ENV] == str(SETTINGS_FIX)
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(os.path.realpath(os.fsdecode(file)))
        return real_open(file, *a, **k)

    monkeypatch.setattr("careerops.config.open", spy, raising=False)
    s = load_settings()
    assert opened == [os.path.realpath(SETTINGS_FIX)]
    assert s.raw["apply"]["mode"] == "assist"                      # explicit in the test copy, whatever the user set in the real file


def test_the_guard_blocks_reading_the_real_settings_however_the_path_is_spelled(monkeypatch):
    with real_settings_hits() as hits:
        with pytest.raises(PermissionError):
            open(REAL, "rb")
        with pytest.raises(PermissionError):
            REAL.read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            open(ROOT / "config" / ".." / "config" / "settings.yaml", "rb")           # ../ does not hide it
        monkeypatch.chdir(ROOT)
        with pytest.raises(PermissionError):
            open("config/settings.yaml", "rb")                                        # relative either
        with pytest.raises(PermissionError):
            load_settings(REAL)                                                       # the loader with the real path, too
        assert len(hits) == 5


def test_the_guard_allows_the_test_copy_and_the_rest_of_config(tmp_path):
    assert yaml.safe_load(SETTINGS_FIX.read_text(encoding="utf-8"))["apply"]["mode"] == "assist"
    assert (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    p = tmp_path / "settings.yaml"
    p.write_text("paths: {}\n", encoding="utf-8")
    assert load_settings(p).raw == {"paths": {}}


def test_a_test_that_swallows_the_error_still_fails_in_teardown(tmp_path):
    """A throwaway test reads the real settings and hides the PermissionError; the guard must still fail it."""
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_conftest", {str(ROOT / "tests" / "conftest.py")!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        no_real_data = mod.no_real_data
    """), encoding="utf-8")
    (tmp_path / "test_swallow.py").write_text(textwrap.dedent(f"""
        def test_hides_it():
            try:
                open({str(REAL)!r}, "rb")
            except PermissionError:
                pass
    """), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode != 0 and "read the real config/settings.yaml" in run.stdout, run.stdout + run.stderr
