"""v2.5 apply/browser.py: `apply.browser` fresh (the code default) ignores a profile lock; `profile` waits for it with the message and never
kills anything. No browser is started: the launch functions are stubbed."""
import os

import pytest

from careerops.apply import browser as B


def lock(tmp_path):
    prof = tmp_path / "browser-profile"
    prof.mkdir()
    os.symlink("host-12345", prof / "SingletonLock")                       # what Chrome leaves while it holds the profile (a dangling symlink)
    return prof


def test_the_default_mode_is_fresh_and_an_unknown_one_is_fresh_too(settings):
    assert B.mode_of(settings) == "fresh" and B.mode_of({"apply": {"browser": "PROFILE"}}) == "profile" and B.mode_of({"apply": {"browser": "x"}}) == "fresh"


def test_the_lock_is_seen_even_as_a_dangling_symlink(tmp_path):
    assert B.lock_present(lock(tmp_path)) and not B.lock_present(tmp_path / "nothing")


def test_profile_mode_waits_for_the_lock_with_the_message_and_kills_nothing(tmp_path, monkeypatch):
    prof, out, asked = lock(tmp_path), [], []

    def enter(prompt):
        asked.append(prompt)
        (prof / "SingletonLock").unlink()                                     # the person closed the old window
        return ""
    assert B.wait_for_lock(prof, enter, out.append)
    assert out == [B.LOCK_MESSAGE] and len(asked) == 1
    assert B.LOCK_MESSAGE == "A Chrome window from an earlier run is still open, close it and press Enter"


def test_profile_mode_launches_only_after_the_wait(tmp_path, monkeypatch):
    prof, order = lock(tmp_path), []
    monkeypatch.setattr(B, "wait_for_lock", lambda d, ask, out: order.append("waited") or True)
    monkeypatch.setattr(B, "profile_page", lambda d, ask, out: (B.wait_for_lock(d, ask, out), order.append("launched"))[1])
    B.launcher({"apply": {"browser": "profile"}})(prof)
    assert order == ["waited", "launched"]


def test_fresh_mode_ignores_the_lock(tmp_path, monkeypatch):
    prof = lock(tmp_path)
    monkeypatch.setattr(B, "wait_for_lock", lambda *a, **k: pytest.fail("fresh mode never looks at the profile"))
    monkeypatch.setattr(B, "fresh_page", lambda headless=False, offscreen=True: ("page", (headless, offscreen)))
    assert B.launcher({"apply": {}})(prof) == ("page", (False, True))               # off-screen while it is filled
    assert B.launcher({"apply": {"browser": "profile"}}, headless=True)(prof) == ("page", (True, True))   # the dry-run record: headless always
