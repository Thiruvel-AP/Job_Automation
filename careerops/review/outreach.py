"""LinkedIn outreach drafts (M4, after prompt upgrades). Spec: docs/IMPLEMENTATION.md §6.

Two drafts per approved job, from details you supply (nothing is scraped or sent):
  1. connection note — ≤ 200 characters (fits both reported LinkedIn note limits)
  2. follow-up message after they accept — ≤ 5 sentences, referral + team question, one small ask
Banned openers are rejected; drafts are logged to preferences.jsonl like CV edits.
"""
from __future__ import annotations

from dataclasses import dataclass

NOTE_MAX_CHARS = 200
MESSAGE_MAX_SENTENCES = 5
BANNED_OPENERS = [r"^\s*i came across your profile", r"^\s*i hope (this|you)", r"impressed"]


@dataclass
class Outreach:
    note: str
    message: str
    problems: list[str]


def draft(job: dict, report: dict, person: str, title: str, team: str | None, llm) -> Outreach:
    """Generate and validate both drafts for one job and one contact."""
    raise NotImplementedError("M4 — outreach.draft")


def validate(note: str, message: str) -> list[str]:
    """Length, sentence count and banned-opener checks."""
    raise NotImplementedError("M4 — outreach.validate")
