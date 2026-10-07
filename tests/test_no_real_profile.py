"""No test may read the user's real fact bank / applicant (config/profile/). They change with the user's life (the bank gained
Kubernetes and broke two gap tests); every test uses the fictional tests/fixtures/profile/ instead."""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from careerops.config import ROOT
from careerops.setup import core_skill_checks
from careerops.tailoring.tailor import fact_text
from conftest import PROFILE_FIX, REAL_PROFILE, real_profile_hits

TESTS = Path(__file__).parent
REAL_FILES = [ROOT / "config" / "profile" / "fact_bank.yaml", ROOT / "config" / "profile" / "applicant.yaml"]


def test_the_settings_fixture_points_at_the_fictional_profile(settings):
    assert settings.path("fact_bank") == PROFILE_FIX / "fact_bank.yaml"
    assert settings.path("applicant") == PROFILE_FIX / "applicant.yaml"
    assert not any(str(settings.path(k)).startswith(str(ROOT / "config" / "profile")) for k in ("fact_bank", "applicant"))


def test_no_test_source_names_the_real_profile_files():
    """A path to config/profile/ in a test is a path the test can resolve: the only files allowed to spell it are this one
    and conftest.py, which holds the guard."""
    spelled = re.compile(r"config[/\\]+profile", re.I)
    bad = [str(p.relative_to(TESTS)) for p in TESTS.rglob("*.py")
           if p.name not in {"conftest.py", Path(__file__).name} and spelled.search(p.read_text(encoding="utf-8"))]
    assert bad == [], f"tests must use tests/fixtures/profile/, not config/profile/: {bad}"


def test_the_guard_blocks_reading_the_real_fact_bank_and_applicant_however_the_path_is_spelled(monkeypatch):
    with real_profile_hits() as hits:
        for real in REAL_FILES:
            with pytest.raises(PermissionError):
                open(real, "rb")
            with pytest.raises(PermissionError):
                real.read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            open(ROOT / "config" / "profile" / ".." / "profile" / "fact_bank.yaml", "rb")       # ../ does not hide it
        monkeypatch.chdir(ROOT)
        with pytest.raises(PermissionError):
            open("config/profile/applicant.yaml", "rb")                                          # relative either
        assert len(hits) == 6                                                                     # 2 files x (open, read_text) + 2
    assert set(REAL_PROFILE) == {str(p.resolve()) for p in REAL_FILES}


def test_the_guard_allows_the_fixture_profile_and_the_rest_of_config(tmp_path):
    assert yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))["identity"]["name"] == "Ada Lovelace"
    assert yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))["first_name"] == "Ada"
    assert (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")


def test_the_fixture_profile_passes_the_same_doctor_checks_as_a_real_one():
    """Core skills all evidenced and none denied; and no Kubernetes anywhere, so gap tests stay stable."""
    checks = core_skill_checks(PROFILE_FIX / "fact_bank.yaml")
    assert checks and all(ok for _, ok, _ in checks), checks
    fb = yaml.safe_load((PROFILE_FIX / "fact_bank.yaml").read_text(encoding="utf-8"))
    assert "kubernetes" in fb["deny_terms"] and "kubernetes" not in fact_text(fb).lower()


def test_a_test_that_swallows_the_error_still_fails_in_teardown(tmp_path):
    """A throwaway test reads the real fact bank and hides the PermissionError; the guard must still fail it."""
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
                open({str(REAL_FILES[0])!r}, "rb")
            except PermissionError:
                pass
    """), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode != 0 and "read the real fact bank / applicant" in run.stdout, run.stdout
