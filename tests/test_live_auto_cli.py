"""`careerops live --auto` through the real CLI: which modes allow what, and the gate on the isolated --mock data root (MockLLM, no network, no
browser: --mock implies --dry-run, so no browser may be opened)."""
import pytest
import yaml
from test_live_cli import JD_HEAD, _answers, _inbox, world       # noqa: F401  (fixtures and helpers of the live CLI tests)

from careerops import autoapply, cli, tracker
from careerops.db import DB
from careerops.llm import MockLLM

pytestmark = pytest.mark.slow          # --mock tailoring renders PDFs with xelatex


def set_mode(cfg, mode, **extra):
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["apply"] = {**raw["apply"], "mode": mode, **extra}
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")


def run(cfg, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "live", *argv])
    return e.value.code


@pytest.fixture(autouse=True)
def no_browser_no_model(monkeypatch):
    monkeypatch.setattr(autoapply, "playwright_launch", lambda *a, **k: pytest.fail("a browser was opened"))
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: MockLLM())


def test_no_submit_and_confirm_mean_nothing_without_auto(world, capsys):
    cfg, real, mock = world
    assert run(cfg, "--no-submit") == 1 and "only mean something with --auto" in capsys.readouterr().err
    assert run(cfg, "--confirm") == 1


def test_auto_that_could_submit_needs_mode_auto_but_no_submit_is_allowed_in_assist(world, capsys, monkeypatch):
    cfg, real, mock = world
    assert run(cfg, "--auto") == 1
    err = capsys.readouterr().err
    assert "apply.mode is `assist`" in err and "apply: mode: auto" in err and "live --auto --no-submit" in err
    assert not (real / "careerops.db").exists()                       # refused before any database was opened
    seen = []
    from careerops.apply import live as live_mod
    monkeypatch.setattr(live_mod.Live, "run", lambda self: seen.append((self.h.auto_gate is not None, self.h.auto_apply is not None)) or 0)
    cli.main(["--config", str(cfg), "live", "--auto", "--no-submit", "--max", "1"])
    assert seen == [(True, True)]


def test_mode_off_refuses_auto_and_turns_live_into_a_dry_run(world, capsys, monkeypatch):
    cfg, real, mock = world
    set_mode(cfg, "off")
    assert run(cfg, "--auto", "--no-submit") == 1 and "apply.mode is `off`" in capsys.readouterr().err
    from careerops.apply import live as live_mod
    seen = []
    monkeypatch.setattr(live_mod.Live, "run", lambda self: seen.append(self.dry_run) or 0)
    cli.main(["--config", str(cfg), "live", "--max", "1"])
    assert seen == [True] and "apply.mode is `off`" in capsys.readouterr().out


def test_a_bad_apply_setting_is_refused_with_its_name(world, capsys):
    cfg, real, mock = world
    set_mode(cfg, "sometimes")
    assert run(cfg, "--auto", "--no-submit") == 1 and "apply.mode" in capsys.readouterr().err
    set_mode(cfg, "assist", max_auto_per_day="lots")
    assert run(cfg, "--auto", "--no-submit") == 1 and "max_auto_per_day" in capsys.readouterr().err


def test_mode_auto_lets_auto_through(world, monkeypatch):
    cfg, real, mock = world
    set_mode(cfg, "auto")
    from careerops.apply import live as live_mod
    seen = []
    monkeypatch.setattr(live_mod.Live, "run", lambda self: seen.append(self.h.auto_apply is not None) or 0)
    cli.main(["--config", str(cfg), "live", "--auto", "--max", "1"])
    assert seen == [True]


def test_auto_on_the_mock_root_prints_why_the_job_is_blocked_and_opens_no_browser(world, jd, monkeypatch, capsys):
    cfg, real, mock = world
    _inbox(mock, jd)
    seen = _answers(monkeypatch, "a")                                     # the review prompt the blocked job falls through to
    cli.main(["--config", str(cfg), "live", "--mock", "--auto", "--no-submit", "--max", "1"])
    out = capsys.readouterr().out
    assert "AUTO BLOCKED:" in out and "kind: employer_other" in out        # example.com is not a Greenhouse / Ashby / Lever page
    assert len(seen) == 1 and seen[0].startswith("[a]pprove")
    db = DB(mock / "careerops.db")
    assert db.jobs()[0]["status"] == "approved"                           # the human decided, as in assist mode


# ------------------------------------------------------------------ the tracker lookup used by the duplicate gate
class Sheet:
    def __init__(self, values):
        self.values = values

    def get_values(self, a1):
        return self.values[a1]


def test_the_tracker_lookup_reads_only_the_job_id_column_by_its_header(settings):
    sheet = Sheet({"'Applications'!1:1": [["Date applied", "Job ID", "Company"]],
                   "'Applications'!B2:B": [["greenhouse:1"], [" lever:2 "], [], [""], ["ashby:3"]]})
    assert tracker.sheet_job_ids(settings, sheet=sheet, tab="Applications") == {"greenhouse:1", "lever:2", "ashby:3"}
    assert tracker.sheet_job_ids(settings, sheet=Sheet({"'Applications'!1:1": [["Company"]]}), tab="Applications") == set()
    assert tracker.sheet_job_ids(settings, sheet=Sheet({"'Applications'!1:1": []}), tab="Applications") == set()


def test_a_google_refusal_is_a_tracker_error(settings):
    from careerops.googleapi import GoogleError

    class Down:
        def get_values(self, a1):
            raise GoogleError("HTTP 403")
    with pytest.raises(tracker.TrackerError, match="403"):
        tracker.sheet_job_ids(settings, sheet=Down(), tab="Applications")
