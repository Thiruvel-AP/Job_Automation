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
