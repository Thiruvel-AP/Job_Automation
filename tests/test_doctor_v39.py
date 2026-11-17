"""v3.9 doctor checks: a scheduled-discovery log newer than 24 h (warn when none), and no tracked file naming the repo folder by its old name."""
import os
import subprocess
import time

from careerops import setup
from careerops.config import Settings


def settings(root):
    return Settings(raw={"paths": {"logs": str(root / "data" / "logs")}}, root=root)


def test_warn_when_no_discover_log_is_newer_than_24_hours(tmp_path):
    s = settings(tmp_path)
    assert setup.scheduler_log_check(s)[1:] == ("warn", "run scripts/scheduler_install.ps1")          # no logs folder at all
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    old = logs / "discover_2026-01-01.log"
    old.write_text("x")
    os.utime(old, (time.time() - 30 * 3600,) * 2)
    assert setup.scheduler_log_check(s)[1] == "warn"
    (logs / "discover_2026-01-02.log").write_text("x")                                                  # written just now
    assert setup.scheduler_log_check(s)[1] is True


def test_fails_when_a_tracked_file_names_the_old_folder(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init")
    (tmp_path / "doc.md").write_text("cd /mnt/d/" + setup.OLD_FOLDER + "/x\n")
    (tmp_path / "ok.md").write_text("cd /mnt/d/VS_Code/x\n")
    git("add", "doc.md", "ok.md")
    name, ok, hint = setup.old_folder_name_check(Settings(raw={"paths": {}}, root=tmp_path))
    assert ok is False and "doc.md" in name and "ok.md" not in name
    git("rm", "-qf", "doc.md")
    assert setup.old_folder_name_check(Settings(raw={"paths": {}}, root=tmp_path))[1] is True


def test_the_real_repo_has_no_tracked_file_with_the_old_name():
    from careerops.config import ROOT
    assert setup.old_folder_name_check(Settings(raw={"paths": {}}, root=ROOT))[1] is True
