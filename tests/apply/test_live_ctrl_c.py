"""v2.5 Ctrl+C in `careerops live`: `stopped by you; state saved` and no traceback at any prompt; the summary is written BEFORE the archive walk,
which is announced ("checking the archive folders (~20 s)..."); a second Ctrl+C during the walk exits at once and the next run does the check first."""
import json

import pytest
from test_live import Fake, archive, inbox, put, session, yes  # noqa: F401  (fixtures and helpers of the live tests)

from careerops.apply import live as live_mod
from careerops.apply.live import EXIT_ARCHIVE, EXIT_INTERRUPTED


class CtrlC(Fake):
    """Ctrl+C at the prompt that contains `at`."""

    def __init__(self, *a, at: str = "", **k):
        super().__init__(*a, **k)
        self.at = at

    def ask(self, prompt):
        if self.at and self.at in prompt:
            self.prompts.append(prompt)
            raise KeyboardInterrupt()
        return super().ask(prompt)


@pytest.mark.parametrize("where", ["prompt:Open the application form", "prompt:Did YOU submit", "hook:review", "hook:assist", "hook:tailor"])
def test_ctrl_c_at_any_prompt_stops_cleanly_with_no_traceback(db, settings, inbox, archive, jd, where):
    put(inbox, "1.txt", jd)
    kind, name = where.split(":")
    fake = CtrlC(db, settings, yes(), at=name if kind == "prompt" else "")
    if kind == "hook":
        fake.raise_in[name] = KeyboardInterrupt()
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_INTERRUPTED                                        # returned, not raised: no traceback
    assert "stopped by you; state saved" in "\n".join(lines)
    assert list(settings.path("live").glob("*.md"))
    assert "mark_applied" not in fake.names()


def test_the_summary_is_written_before_the_archive_walk_which_is_announced(db, settings, inbox, archive, jd, monkeypatch):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["assist"] = KeyboardInterrupt()
    live, lines = session(db, settings, fake)
    real, seen = live_mod.Live._archive_check, []

    def walk(self, r):
        seen.append(sorted(p.name for p in settings.path("live").glob("*.md")))
        return real(self, r)
    monkeypatch.setattr(live_mod.Live, "_archive_check", walk)
    assert live.run() == EXIT_INTERRUPTED
    text = "\n".join(lines)
    assert seen and seen[-1], "the summary file existed when the walk after the stop began"      # (the first walk is pass 1's own check)
    assert text.index("Summary written to") < text.index("checking the archive folders (~20 s)") < text.index("archive check: ok")


def test_a_second_ctrl_c_during_the_walk_exits_at_once_and_the_next_run_checks_first(db, settings, inbox, archive, jd, monkeypatch):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["assist"] = KeyboardInterrupt()
    live, lines = session(db, settings, fake)
    monkeypatch.setattr(live_mod.Live, "_archive_check", lambda self, r: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert live.run() == EXIT_INTERRUPTED
    assert "archive check skipped: it runs at the start of the next `careerops live`." in lines
    pending = json.loads((settings.path("live") / "archive_pending.json").read_text())
    assert "Old Co/Data Scientist/CV.pdf" in pending["before"]
    monkeypatch.undo()
    (archive / "Old Co" / "Data Scientist" / "JD.txt").unlink()                  # something changed the archive while the run was stopped
    live2, lines2 = session(db, settings, Fake(db, settings, yes()))
    assert live2.run() == EXIT_ARCHIVE
    assert any("REMOVED  Old Co/Data Scientist/JD.txt" in line for line in lines2)
    assert json.loads((settings.path("live") / "archive_pending.json").read_text()) == {}


def test_a_pending_check_that_finds_nothing_lets_the_run_go_on(db, settings, inbox, archive, jd, monkeypatch):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["assist"] = KeyboardInterrupt()
    live, _ = session(db, settings, fake)
    monkeypatch.setattr(live_mod.Live, "_archive_check", lambda self, r: (_ for _ in ()).throw(KeyboardInterrupt()))
    live.run()
    monkeypatch.undo()
    live2, lines2 = session(db, settings, Fake(db, settings, yes()))
    assert live2.run() == 0 and "archive check of the stopped run: ok (unchanged)" in lines2
