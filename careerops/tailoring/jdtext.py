"""Cleaning a job description before keywords are read from it.

A benefits section ("Benefits", "What we offer", "Why join us") is about the employer, not the role: its words ("flexible
working", "private healthcare", "pension") are not skills, but a model asked for keywords picks them up and they then count
against the CV's ATS score. `strip_benefits` cuts the section out before extraction.
"""
from __future__ import annotations

import re

# a heading that opens a benefits section: "Benefits", "Our perks & benefits", "What we offer you", "Why join Acme?"
BENEFITS_HEADING = re.compile(
    r"^[\W_]*(?:our |the |your |employee |company |key )?"
    r"(?:benefits|perks|rewards|what we(?:'|’)?(?:ll)? offer|what you(?:'|’)?ll get|why join|why work (?:with|for|at))\b"
    r"[^.!]{0,40}$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·▪–]|\d+[.)])\s")
MAX_HEADING = 60


def _is_heading(line: str, prev_blank: bool) -> bool:
    """A short, unpunctuated line that opens a section: markdown (#, **bold**), ends with a colon, or is a few words
    standing alone after a blank line ("Who you are"). Bullets and sentences are not headings."""
    s = line.strip()
    if not s or len(s) > MAX_HEADING or _BULLET.match(line) or s[-1] in ".,;":
        return False
    if s.startswith("#") or (s.startswith("**") and s.endswith("**")) or s.endswith(":"):
        return True
    words = s.split()
    return prev_blank and len(words) <= 6


def _is_prose(line: str, prev_blank: bool) -> bool:
    """A sentence or long line after a blank line: the JD has moved on to ordinary text ("Proficiency in PyTorch."). A
    benefits section is fragments and bullets ("Private healthcare"), so the cut stops here and keeps the text."""
    s = line.strip()
    return prev_blank and bool(s) and not _BULLET.match(line) and (s.endswith(".") or len(s) > MAX_HEADING)


def _title(line: str) -> str:
    """A heading's words without markdown markers and the trailing colon."""
    return line.strip().lstrip("#* ").rstrip("*: ").strip()


def _opens_benefits(line: str) -> bool:
    return len(line.strip()) <= MAX_HEADING and not _BULLET.match(line) and bool(BENEFITS_HEADING.match(_title(line)))


def strip_benefits(jd: str) -> tuple[str, str | None]:
    """(the JD without its benefits section, a note saying what was cut or None). The section runs from its heading to the
    next heading or the next paragraph of ordinary sentences, or to the end of the text; a JD with no such heading comes
    back unchanged."""
    lines = jd.splitlines()
    for i, line in enumerate(lines):
        if not _opens_benefits(line):
            continue
        end, prev_blank = len(lines), False
        for j in range(i + 1, len(lines)):
            if (_is_heading(lines[j], prev_blank) or _is_prose(lines[j], prev_blank)) and not _opens_benefits(lines[j]):
                end = j
                break
            prev_blank = not lines[j].strip()
        cut = "\n".join(lines[i:end])
        kept = "\n".join(lines[:i] + lines[end:])
        return kept, f"cut the benefits section '{_title(line)[:40]}' ({len(cut)} characters) before extraction"
    return jd, None
