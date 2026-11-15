"""The browser an application form is filled in (v2.5).

`apply.browser` (code default "fresh"; the user's settings.yaml is not edited for it):

  fresh    one shared Chromium per run, `chromium.launch(channel="chrome", headless=False)`, falling back to the bundled Chromium; a NEW
           non-persistent context for every job (viewport 1280x900, default timeout 8 s). No profile, no saved sign-in, no debugging port: nothing
           here signs in anyway (CLAUDE.md). The window starts off-screen (`--window-position=-2400,0`) so the terminal shows the progress while the
           form is filled; `show(page)` brings it on-screen (CDP `Browser.setWindowBounds`) for the human steps (the declarations, Submit). A window
           manager that ignores the position (WSLg) just shows the window: not an error. The shared browser is closed when the run ends, and
           relaunched when it has been idle for IDLE_S (5 minutes) by the time the next job needs it.
  profile  the persistent profile of `paths.browser_profile`, as before v2.5. If its lock is there (SingletonLock / SingletonSocket /
           SingletonCookie: a window of an earlier run still holds it), `A Chrome window from an earlier run is still open, close it and press Enter`
           is printed and the run waits. That window is never killed.

`headless=True` (the `live --dry-run --record` run) never shows a window. Ideas from career-ops-hq/career-ops `session.ts` / `apply-scope.mjs`
(20b9fd3); this is our own Python.
"""
from __future__ import annotations

import atexit
import logging
import time
from pathlib import Path
from typing import Any, Callable

DRIVER_NOISE = ("Connection closed while reading from the driver", "TargetClosedError", "Target page, context or browser has been closed")


class _DriverNoise(logging.Filter):
    """Drops asyncio's `Future exception was never retrieved` record that Playwright's own pending calls leave behind when Ctrl+C (or a closed
    window) takes the driver away mid-call. Anything else asyncio logs passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return not ("never retrieved" in text and any(m in text for m in DRIVER_NOISE))


_filter = _DriverNoise()


def quiet_driver_noise() -> None:
    """Install the filter once (idempotent)."""
    log = logging.getLogger("asyncio")
    if _filter not in log.filters:
        log.addFilter(_filter)


_started: list = []         # every Playwright this process started (here, assist.py, autoapply.py): shutdown() stops them all


def start_playwright():
    """`sync_playwright().start()`, remembered so `shutdown()` can stop it, with the driver-noise filter installed."""
    from playwright.sync_api import sync_playwright

    quiet_driver_noise()
    pw = sync_playwright().start()
    _started.append(pw)
    return pw


def shutdown() -> None:
    """Ctrl+C or the end of the run: close every shared browser, then stop every Playwright, quietly. Never raises (a driver that is already gone
    is not an error)."""
    quiet_driver_noise()
    close_all()
    while _started:
        pw = _started.pop()
        try:
            pw.stop()
        except BaseException:  # noqa: BLE001  (Error, a second Ctrl+C, a driver that exited with the first)
            pass


FRESH, PROFILE = "fresh", "profile"
MODES = (FRESH, PROFILE)
IDLE_S = 5 * 60
VIEWPORT = {"width": 1280, "height": 900}
DEFAULT_TIMEOUT_MS = 8_000
OFFSCREEN = "--window-position=-2400,0"
# Keep timers and rendering at full speed in a window that is hidden, off screen or behind another (wait_quiet and the fill timing rely on it).
CHROMIUM_ARGS = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"]
ONSCREEN = {"left": 80, "top": 60, "width": 1280, "height": 920, "windowState": "normal"}
LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
LOCK_MESSAGE = "A Chrome window from an earlier run is still open, close it and press Enter"

_shared: dict[str, Any] = {}


def mode_of(settings) -> str:
    """`apply.browser`, "fresh" when absent or unknown."""
    raw = getattr(settings, "raw", settings) or {}
    m = str(((raw.get("apply") or {}) if isinstance(raw, dict) else {}).get("browser") or FRESH).strip().lower()
    return m if m in MODES else FRESH


def lock_present(profile_dir: Path | str | None) -> bool:
    """Is the profile held by a running Chrome (its Singleton* lock files, which are symlinks: lexists)?"""
    if not profile_dir:
        return False
    import os
    return any(os.path.lexists(Path(profile_dir) / n) for n in LOCK_FILES)


def wait_for_lock(profile_dir: Path | str, ask: Callable[[str], str] = input, out: Callable[[str], None] = print, tries: int = 3) -> bool:
    """Wait while the profile is locked: the message, then Enter, up to `tries` times. True when the lock is gone. Nothing is killed."""
    for _ in range(tries):
        if not lock_present(profile_dir):
            return True
        out(LOCK_MESSAGE)
        ask("  press Enter once that window is closed > ")
    return not lock_present(profile_dir)


def _browser(headless: bool, offscreen: bool, clock: Callable[[], float] = time.monotonic):
    """The shared browser for this run (launched on first use, relaunched after IDLE_S idle or when it went away)."""
    key = "headless" if headless else "headed"
    cur = _shared.get(key)
    if cur is not None:
        b, last = cur["browser"], cur["last"]
        alive = False
        try:
            alive = b.is_connected()
        except Exception:  # noqa: BLE001
            alive = False
        if alive and clock() - last < IDLE_S:
            cur["last"] = clock()
            return b
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass
        _shared.pop(key, None)
    pw = _shared.get("pw")
    if pw is None:
        pw = _shared["pw"] = start_playwright()
        atexit.register(shutdown)
    args = list(CHROMIUM_ARGS) + ([] if headless or not offscreen else [OFFSCREEN])
    try:
        b = pw.chromium.launch(channel="chrome", headless=headless, args=args)
    except Exception:  # noqa: BLE001  (no Google Chrome installed: the bundled Chromium)
        b = pw.chromium.launch(headless=headless, args=args)
    _shared[key] = {"browser": b, "last": clock()}
    return b


def close_all() -> None:
    """Close every shared browser and Playwright (end of the run). Never raises."""
    for key in ("headed", "headless"):
        cur = _shared.pop(key, None)
        if cur is not None:
            try:
                cur["browser"].close()
            except Exception:  # noqa: BLE001
                pass
    pw = _shared.pop("pw", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


def fresh_page(headless: bool = False, offscreen: bool = True):
    """(page, close) in a new context of the shared browser. `close` closes this job's context only and never raises."""
    b = _browser(headless, offscreen)
    ctx = b.new_context(viewport=VIEWPORT)
    ctx.set_default_timeout(DEFAULT_TIMEOUT_MS)
    page = ctx.new_page()

    def close() -> None:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001  (already closed by the person)
            pass
        cur = _shared.get("headless" if headless else "headed")
        if cur is not None:
            cur["last"] = time.monotonic()
    return page, close


def profile_page(profile_dir: Path, ask: Callable[[str], str] = input, out: Callable[[str], None] = print):
    """(page, close) of a headed Chromium with the persistent profile (apply.browser: profile), after waiting for a lock to go."""
    if not wait_for_lock(profile_dir, ask, out):
        raise RuntimeError(f"{profile_dir} is still locked by another Chrome window; nothing was opened")
    pw = start_playwright()
    ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=False, args=CHROMIUM_ARGS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def close() -> None:
        for stop in (ctx.close, pw.stop):
            try:
                stop()
            except Exception:  # noqa: BLE001
                pass
    return page, close


def launcher(settings, *, headless: bool = False, ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> Callable[[Path], tuple]:
    """The `launch(profile_dir) -> (page, close)` the assist and the auto-apply take, for `apply.browser`."""
    mode = mode_of(settings)
    if headless:
        return lambda profile_dir: fresh_page(headless=True)
    if mode == PROFILE:
        return lambda profile_dir: profile_page(Path(profile_dir), ask, out)
    return lambda profile_dir: fresh_page(headless=False, offscreen=True)


def show(page, out: Callable[[str], None] = print) -> bool:
    """Bring the (off-screen) window on-screen for the human steps. False when CDP is not available (the window is raised as it is)."""
    moved = False
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": ONSCREEN})
        moved = True
        try:
            cdp.detach()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001  (a persistent profile, a headless page, a fake page)
        moved = False
    try:
        page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    return moved
