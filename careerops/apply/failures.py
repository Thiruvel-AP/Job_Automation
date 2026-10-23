"""Submit failures, counted per job (paths.live/submit_failures.csv: time, job id, host, kind, detail; append-only).

A form that cannot be submitted is a bug to report, not to retry (CLAUDE.md). Four things count as a failure of an attempt:

  verification_error   the page says "error verifying your application" (a bot check turned the automated browser away)
  field_lost           a field was LOST after the re-fill: the form swallowed what was typed
  upload_refused       the form rejected an uploaded file
  form_not_found       no application form appeared (FORM NOT FOUND <url>)

A fifth kind is written to the same file but is NOT counted:

  spam_flag            after a submit the page says "flagged as possible spam" / "couldn't submit your application"; the job goes to the hand-off at
                       once (the first time, not the second), the host is remembered (apply/handoff.py: later jobs there skip the browser), and
                       nothing is refreshed or submitted again from automation

After `apply.max_submit_failures` (2) failed attempts on one job, or at once when the host is on `apply.handoff_hosts` (or handed a job over
before), the job is handed to the person's own browser (apply/handoff.py) and ends `handed_off`. There is never a third attempt: `Failures.gate`
answers before any browser is launched. This module only counts and reports; it opens nothing, clicks nothing, signs in to nothing and changes no
browser profile, user agent or proxy.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from .formfind import SPAM_FLAG as SPAM_PATTERN, host_of

FILE = "submit_failures.csv"
COLUMNS = ["time", "job id", "host", "kind", "detail"]
VERIFICATION_ERROR, FIELD_LOST, UPLOAD_REFUSED, NOT_FOUND = "verification_error", "field_lost", "upload_refused", "form_not_found"
SPAM_FLAG = "spam_flag"
KINDS = (VERIFICATION_ERROR, FIELD_LOST, UPLOAD_REFUSED, NOT_FOUND)             # the kinds that count towards the limit; SPAM_FLAG is recorded, not counted
DEFAULT_LIMIT = 2

_KINDS = ((SPAM_FLAG, SPAM_PATTERN),
          (NOT_FOUND, re.compile(r"form not found", re.I)),
          (VERIFICATION_ERROR, re.compile(r"error verifying your application", re.I)),
          (FIELD_LOST, re.compile(r"emptied field|\blost\b", re.I)),
          (UPLOAD_REFUSED, re.compile(r"refused an upload|upload (?:was )?(?:refused|rejected|failed)", re.I)))


def kind_of(text: str) -> str:
    """Which failure a message from the form step is ('' when it is not one: a question for the person is not a failure of the form).
    A spam flag is its own kind (SPAM_FLAG): it is written to the file and never counted."""
    for kind, pattern in _KINDS:
        if pattern.search(text or ""):
            return kind
    return ""


class Failures:
    def __init__(self, settings, now: Callable[[], datetime] = datetime.now):
        self.settings, self.now = settings, now
        self.path = settings.path("live") / FILE
        raw = ((settings.raw.get("apply") or {}) if hasattr(settings, "raw") else {}).get("max_submit_failures", DEFAULT_LIMIT)
        self.limit = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else DEFAULT_LIMIT

    def rows(self, job_id: str | None = None) -> list[dict[str, str]]:
        try:
            with open(self.path, encoding="utf-8", newline="") as f:
                found = [r for r in csv.DictReader(f) if r]
        except (OSError, csv.Error, UnicodeDecodeError):
            return []
        return [r for r in found if job_id is None or r.get("job id") == job_id]

    def count(self, job_id: str) -> int:
        """The failed attempts of this job that count towards the limit (a spam flag is not one: it is an immediate hand-off)."""
        return len(self.kinds(job_id))

    def kinds(self, job_id: str) -> list[str]:
        return [r.get("kind", "") for r in self.rows(job_id) if r.get("kind", "") in KINDS]

    def flagged(self, job_id: str | None = None) -> list[dict[str, str]]:
        """The rows that record a spam flag (of one job, or all)."""
        return [r for r in self.rows(job_id) if r.get("kind") == SPAM_FLAG]

    def record(self, job_id: str, url: str | None, kind: str, detail: str) -> int:
        """Append one failure and return how many this job has now (a spam flag is written but does not add to the count)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLUMNS)
            w.writerow([self.now().isoformat(timespec="seconds"), job_id, host_of(url), kind, str(detail)])
        return self.count(job_id)

    def over_limit(self, job_id: str) -> bool:
        return self.count(job_id) >= self.limit

    def gate(self, job_id: str) -> str | None:
        """Why this job may not be tried again (it has used up its attempts), or None. Asked before any browser is launched."""
        n = self.count(job_id)
        if n < self.limit:
            return None
        return f"{n} failed attempts on this job ({', '.join(self.kinds(job_id))}); no third attempt"
