"""Archive errors. ArchiveError = refused, nothing changed. ArchiveIncomplete = the copy failed part-way."""
from __future__ import annotations

from pathlib import Path


class ArchiveError(RuntimeError):
    """The archive step was refused before it changed anything (no folder, no row, no status change)."""


class ArchiveRootMissing(ArchiveError):
    pass


class LetterMissing(ArchiveError):
    pass


class WrongFileError(ArchiveError):
    """A CV/letter path does not belong to this job (wrong folder, or the letter is for another company)."""


class ArchiveIncomplete(RuntimeError):
    """Copying failed after the application was submitted. The partial folder is left in place and flagged;
    the job is already `applied`. `folder` is the partial role folder (None if nothing was created)."""

    def __init__(self, message: str, folder: Path | None = None):
        super().__init__(message)
        self.folder = folder


class BrowserClosed(RuntimeError):
    """The page, its context or the browser was closed while the form was being read or filled (v2.5). Nothing was submitted by code; the run
    stops at once, says `BROWSER CLOSED: nothing submitted`, points at answers_to_paste.txt and never asks whether the form was submitted."""


CLOSED_WORDS = ("has been closed", "target closed", "browser has disconnected", "context or browser", "page closed", "browser closed")


def is_closed_error(e: BaseException, page=None) -> bool:
    """Is `e` a Playwright error saying the page, context or browser is gone (or is `page` itself closed)?"""
    if isinstance(e, BrowserClosed) or type(e).__name__ == "TargetClosedError":
        return True
    if any(w in str(e).lower() for w in CLOSED_WORDS):
        return True
    try:
        return bool(page is not None and callable(getattr(page, "is_closed", None)) and page.is_closed())
    except Exception:  # noqa: BLE001  (a fake page, or one that cannot even say)
        return False


def raise_if_closed(e: BaseException, page=None) -> None:
    """Re-raise `e` as BrowserClosed when it says the browser is gone; return otherwise (the caller handles any other error its own way)."""
    if is_closed_error(e, page):
        raise BrowserClosed(str(e).splitlines()[0][:200] if str(e) else "the browser was closed") from e
