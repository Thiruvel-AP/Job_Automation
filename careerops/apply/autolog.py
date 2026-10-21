"""data/live/applied_auto.csv: one row for every form `careerops live --auto` filled, whether or not it was submitted.

  time, job id, company, role, ATS strict, screenshots, result

The file is only ever appended to. The daily cap and the "first N auto-submits ever ask y/N" rule read it back: a row counts as an
auto-submit when the submit button was clicked (`submitted` or `submit_unconfirmed`), and not when the form was only filled.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

FILE = "applied_auto.csv"
COLUMNS = ["time", "job id", "company", "role", "ATS strict", "screenshots", "result"]

SUBMITTED = "submitted"                  # clicked, and the confirmation page appeared
UNCONFIRMED = "submit_unconfirmed"       # clicked, no confirmation within 30 s: the user checks
NOT_SUBMITTED = "not_submitted"          # --no-submit: filled and photographed, never clicked
NEEDS_USER = "needs_user"                # a CAPTCHA, or a question with no answer: the user takes over
DECLINED = "declined"                    # the user answered N at `Submit? [y/N]`
ERROR = "error"                          # the page could not be driven
CLICKED = (SUBMITTED, UNCONFIRMED)


def path(settings) -> Path:
    return settings.path("live") / FILE


def rows(settings) -> list[dict[str, str]]:
    try:
        with open(path(settings), encoding="utf-8", newline="") as f:
            return [r for r in csv.DictReader(f) if r]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def append(settings, *, job: Mapping[str, Any], ats_strict: float | None, screenshots: list[str], result: str,
           when: datetime | None = None) -> None:
    p = path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerow([(when or datetime.now()).isoformat(timespec="seconds"), job["id"], job["company"], job["title"],
                    "" if ats_strict is None else f"{ats_strict:.1f}", ";".join(screenshots), result])


def clicked_total(log: list[dict[str, str]]) -> int:
    """Auto-submits ever (the button was clicked)."""
    return sum(1 for r in log if r.get("result") in CLICKED)


def clicked_on(log: list[dict[str, str]], day: date) -> int:
    """Auto-submits on this calendar day; a row with an unreadable time counts for no day."""
    return sum(1 for r in log if r.get("result") in CLICKED and (r.get("time") or "")[:10] == day.isoformat())


def clicked_ids(log: list[dict[str, str]]) -> set[str]:
    return {r["job id"] for r in log if r.get("result") in CLICKED and r.get("job id")}
