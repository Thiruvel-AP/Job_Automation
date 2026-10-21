"""`careerops live` checks at the start that the Google sign-in can edit the tracker Sheet. Without the spreadsheets scope the tracker has
never been written and the duplicate gate cannot read it, so `--auto` is switched off for the run (assist only) and one line says how to
fix it. The real CLI on tmp paths; no browser, no network, no model."""
import pytest
from test_live_cli import world       # noqa: F401  (the fixture of the live CLI tests)

from careerops import cli, tracker
from careerops.apply import live as live_mod
from careerops.discovery import gmail

pytestmark = pytest.mark.slow          # test_live_cli's module mark (the world fixture is shared with tests that render PDFs)

OLD_TOKEN = {"refresh_token": "rt", "access_token": "x", "expires_at": 9e12, "scope": gmail.SCOPE}
FULL_TOKEN = {**OLD_TOKEN, "scope": " ".join(gmail.SCOPES)}


def hooks_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(live_mod.Live, "run", lambda self: seen.append((self.h.auto_gate is not None, self.h.auto_apply is not None)) or 0)
    return seen


def test_a_token_without_the_spreadsheets_scope_is_a_problem_with_the_fix_in_it(settings):
    assert "--gmail-auth" in tracker.scope_problem(settings)                         # no token at all
    gmail._write_token(settings, OLD_TOKEN)
    assert gmail.SCOPE_SHEETS in tracker.scope_problem(settings) and "--gmail-auth" in tracker.scope_problem(settings)
    gmail._write_token(settings, FULL_TOKEN)
    assert tracker.scope_problem(settings) is None


def test_without_the_scope_auto_is_off_for_the_run_and_the_message_is_printed_once(world, settings, monkeypatch, capsys):
    cfg, real, mock = world
    gmail._write_token(settings, OLD_TOKEN)
    seen = hooks_seen(monkeypatch)
    cli.main(["--config", str(cfg), "live", "--auto", "--no-submit", "--max", "1"])
    out = capsys.readouterr().out
    assert seen == [(False, False)]                                                  # no gate, no auto-fill: assist only
    assert out.count("careerops discover --gmail-auth") == 1 and "assist mode only" in out and "duplicate" in out


def test_with_the_scope_auto_stays_on_and_nothing_is_said(world, settings, monkeypatch, capsys):
    cfg, real, mock = world
    gmail._write_token(settings, FULL_TOKEN)
    seen = hooks_seen(monkeypatch)
    cli.main(["--config", str(cfg), "live", "--auto", "--no-submit", "--max", "1"])
    assert seen == [(True, True)] and "--gmail-auth" not in capsys.readouterr().out


def test_without_auto_the_missing_scope_is_still_said_once(world, settings, monkeypatch, capsys):
    cfg, real, mock = world
    gmail._write_token(settings, OLD_TOKEN)
    seen = hooks_seen(monkeypatch)
    cli.main(["--config", str(cfg), "live", "--max", "1"])
    out = capsys.readouterr().out
    assert seen == [(False, False)] and out.count("careerops discover --gmail-auth") == 1 and "assist mode only" not in out


def test_the_mock_root_has_no_tracker_so_nothing_is_checked(world, settings, monkeypatch, capsys):
    cfg, real, mock = world
    seen = hooks_seen(monkeypatch)
    cli.main(["--config", str(cfg), "live", "--mock", "--auto", "--no-submit", "--max", "1"])
    assert seen == [(True, True)] and "--gmail-auth" not in capsys.readouterr().out
