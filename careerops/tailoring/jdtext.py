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


# ---------------------------------------------------------------- the role / requirements part of a JD (the judge's input)
# A section the judge needs: what the job is and what it asks for. Checked before DROP_HEADING, so "About the role" is kept
# while "About Monzo" goes.
KEEP_HEADING = re.compile(
    r"\b(?:role|responsibilit\w*|requirements?|qualifications?|skills?|experience|duties|essential|desirable|must[- ]have|"
    r"nice[- ]to[- ]have|day[- ]to[- ]day|key tasks|tech(?:nical|nology)?(?: stack)?|"
    r"what you(?:'|’)?(?:ll|\s+will|\s+bring|\s+have|\s+do|\s+need)|you will|you(?:'|’)ll|you have|you are|who you|about you|"
    r"about the (?:job|position|team|opportunity)|the (?:job|position|opportunity)|we(?:'|’)re looking|"
    r"your (?:profile|background|impact))", re.I)
# A section about the employer or the process: no part of what the CV is judged against.
DROP_HEADING = re.compile(
    r"\b(?:benefits|perks|what we offer|why join|equal opportunit\w*|diversity|inclusion|how to apply|application process|"
    r"our (?:values|culture|mission|story|company|benefits)|about (?:us|the company|[a-z][\w&.-]*)|who we are|salary|"
    r"data protection|privacy|reasonable adjustments|accessibility)\b", re.I)
PREAMBLE_CHARS = 1200      # text before the first heading is usually a one-paragraph summary; more than this is company talk
MIN_ROLE_CHARS = 400       # a trim that leaves less than this was not a trim of a JD: the full text is returned


def split_sections(jd: str) -> list[tuple[str, str]]:
    """[(heading or "", text)] in order; the first entry has an empty heading when the JD starts with text before any heading.
    The heading line is part of its section's text."""
    out: list[tuple[str, list[str]]] = [("", [])]
    prev_blank = True
    for line in jd.splitlines():
        if _is_heading(line, prev_blank) and not _is_prose(line, prev_blank):
            out.append((_title(line), [line]))
        else:
            out[-1][1].append(line)
        prev_blank = not line.strip()
    return [(h, "\n".join(ls).strip()) for h, ls in out if "".join(ls).strip()]


def role_sections(jd: str) -> tuple[str, str | None]:
    """(the JD cut to its role / requirements sections, a note saying what was dropped or None). A section whose heading
    names the role, duties, requirements or skills is kept; one about the employer, benefits, equal opportunities or how
    to apply is dropped; a heading that is neither is kept (nothing is dropped that cannot be told apart). The text before
    the first heading is kept up to PREAMBLE_CHARS. A JD with no headings, or one that would shrink below MIN_ROLE_CHARS,
    comes back unchanged."""
    sections = split_sections(jd)
    if len(sections) < 2:
        return jd, None
    kept, dropped = [], []
    for heading, text in sections:
        if not heading:
            kept.append(text[:PREAMBLE_CHARS])
        elif KEEP_HEADING.search(heading) or not DROP_HEADING.search(heading):
            kept.append(text)
        else:
            dropped.append(heading[:40])
    trimmed = "\n\n".join(kept).strip()
    if not dropped or len(trimmed) < MIN_ROLE_CHARS:
        return jd, None
    return trimmed, f"dropped {len(dropped)} section(s) about the employer or process: {', '.join(dropped)}"
