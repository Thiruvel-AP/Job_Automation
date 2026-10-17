"""The twice-daily discovery (scripts/discover.sh, docs/scheduler.md): `careerops discover --ats --reed --adzuna --gmail-alerts --report`
runs the sources and THEN prints the report (tier counts included); the command exits 1 when a source failed (a lapsed Gmail sign-in);
the script logs to data/logs/discover_<date>.log and passes the exit code on. It only discovers: nothing is tailored or applied."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from careerops import cli
from careerops.config import ROOT
from careerops.discovery import discover as disc, gmail
from netfake import Net

# the helpers of test_discover.py: a Greenhouse board, the settings and the `discover` command with a fake network
from test_discover import ACME, cfg, cmd, gh_net, inbox_files  # noqa: F401  (fixtures)

SCRIPT = ROOT / "scripts" / "discover.sh"
COMMAND = "careerops discover --ats --reed --adzuna --gmail-alerts --report"


def test_report_with_a_source_flag_runs_the_source_first_and_then_prints_the_report(cmd, cfg, capsys):
    cmd(gh_net(), "--ats", "--report")
    out = capsys.readouterr().out
    assert inbox_files(cfg)                                                           # the board was read and ads were written
    assert out.index("discover: ats ...") < out.index("Last run (")                   # sources first, then the report
    assert "Freshness of the ads the last run wrote: tier 0 (<= 24 h):" in out
    assert "Waiting in the inbox for `careerops live` (`live --fresh` takes tier 0): " in out and "tier 3 (older):" in out


def test_report_alone_still_only_prints_the_report(cmd, cfg, capsys):
    cmd(Net(strict=False), "--report")
    out = capsys.readouterr().out
    assert "No discover run recorded yet" in out and "discover: ats" not in out and inbox_files(cfg) == []


def test_a_source_that_raises_makes_the_command_exit_1_after_the_others_ran(cmd, cfg, monkeypatch, capsys):
    monkeypatch.setattr(disc.Discover, "from_reed", lambda self: 1 / 0)
    with pytest.raises(SystemExit) as e:
        cmd(gh_net(), "--ats", "--reed", "--report")
    assert e.value.code == 1
    out = capsys.readouterr()
    assert inbox_files(cfg) and "! reed:" in out.out and "FAILED: reed: ZeroDivisionError" in out.err      # ats still wrote its ads
    assert "Last run (" in out.out                                                                       # and the report was still printed


def test_a_lapsed_gmail_sign_in_exits_1(cmd, cfg, capsys):
    with pytest.raises(SystemExit) as e:
        cmd(Net(strict=False), "--gmail-alerts")
    assert e.value.code == 1 and "FAILED: gmail: not signed in to Gmail" in capsys.readouterr().err
    assert disc.read_runs(cfg)[-1]["failures"]                                                           # kept in the run record


def test_a_clean_run_exits_0(cmd, cfg):
    cmd(gh_net(), "--ats", "--report")                                                                    # no SystemExit


# ------------------------------------------------------------------ the script
def fake_careerops(folder: Path, body: str) -> dict:
    folder.mkdir(parents=True)
    exe = folder / "careerops"
    exe.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}


@pytest.fixture
def repo(tmp_path):
    """A copy of the script in a repo-shaped folder of its own: the script cds to its parent, so nothing real is touched."""
    (tmp_path / "repo" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "repo" / "scripts" / "discover.sh")
    return tmp_path / "repo"


def run_script(repo, env):
    return subprocess.run(["bash", str(repo / "scripts" / "discover.sh")], capture_output=True, text=True, env=env, cwd=repo.parent)


def test_the_script_runs_exactly_the_discovery_command_and_nothing_else():
    text = SCRIPT.read_text(encoding="utf-8")
    assert COMMAND in text and text.startswith("#!/usr/bin/env bash")
    code = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert sum("careerops" in ln for ln in code) == 1                           # no tailor, no live, no apply
    for word in ("tailor", "live", "apply", "mark-applied", "submit"):
        assert not any(f"careerops {word}" in ln for ln in code)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0           # it parses


def test_the_script_logs_to_a_dated_file_and_exits_zero_on_success(repo):
    env = fake_careerops(repo.parent / "bin", 'echo "args: $@"; echo "found 3"; exit 0')
    r = run_script(repo, env)
    assert r.returncode == 0 and "args: discover --ats --reed --adzuna --gmail-alerts --report" in r.stdout
    (log,) = (repo / "data" / "logs").glob("discover_*.log")
    import re
    assert re.fullmatch(r"discover_\d{4}-\d{2}-\d{2}\.log", log.name)
    text = log.read_text(encoding="utf-8")
    assert "discover.sh start" in text and "found 3" in text and "discover.sh end, exit 0" in text


def test_the_script_exits_non_zero_when_the_command_fails_and_appends_to_the_same_days_log(repo):
    env = fake_careerops(repo.parent / "bin", 'echo "boom" >&2; exit 3')
    r = run_script(repo, env)
    assert r.returncode == 3 and "boom" in r.stdout + r.stderr
    again = run_script(repo, env)
    assert again.returncode == 3
    (log,) = (repo / "data" / "logs").glob("discover_*.log")
    assert log.read_text(encoding="utf-8").count("discover.sh start") == 2 and "exit 3" in log.read_text(encoding="utf-8")
