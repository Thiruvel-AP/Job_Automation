"""The two-page CV's section order and its compact Education (user decision, 8 Nov 2026; replaces v3.4's graduate order).

  Order      Profile, Education, Work Experience, Projects, Skills, Certifications & Achievements (EDUCATION_FIRST) until cv.education_first_until
             (code default 2027-09-30); after it, Profile, Work Experience, Projects, Education, Skills, Certifications (EXPERIENCE_FIRST), and
             `doctor` says so. An ad that asks for N+ years of experience with N >= 3 gets EXPERIENCE_FIRST for that job, and the review says why.
             The optional fact-bank sections (research focus, publications, languages) follow at the end. The headings never change (the v3.3
             ATS structure check reads them; the order of sections is not scored).
  Education  at most EDUCATION_MAX_LINES (4) printed lines: one line per degree (degree, university, dates; a classification when it is not an
             expected one), and under the degree that has a `dissertation:` (a plain one-line title the user wrote) the line "Dissertation with
             <affiliation> affiliation: <title>". No module lists. The affiliation is the entry's `dissertation_affiliation:`, else the
             "dissertation with X affiliation" its own `display_line` says.
  Position   in Education-first order the first Work Experience bullet must start in the top FIRST_BULLET_MAX (55 %) of page 1, read from the
             PDF's text layer (layout.pdf_lines); if it does not, the dissertation line is dropped, the CV rendered again, and a warning says so.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Mapping, Sequence

EDUCATION_FIRST = ("profile", "education", "experience", "projects", "skills", "certifications")
EXPERIENCE_FIRST = ("profile", "experience", "projects", "education", "skills", "certifications")
TAIL = ("research_focus", "publications", "languages")
DEFAULT_UNTIL = date(2027, 9, 30)
SENIOR_YEARS = 3
EDUCATION_MAX_LINES = 4
LINE_CHARS = 100                  # one full-width line of the 11pt body (Carlito, A4 less 0.6in margins)
SMALL_LINE_CHARS = 112            # the same line in \small (the dissertation line)
FIRST_BULLET_MAX = 0.55
A4_HEIGHT = 841.89                # points
_EXPECTED = re.compile(r"\bexpected\b|\bpredicted\b|\bon track\b", re.I)
_CLASS = re.compile(r"\b(?:first|second|third|upper|lower|2:1|2:2|1st|distinction|merit|pass|honours|hons|cum laude|gpa|cgpa|grade)\b", re.I)
CLASS_MAX_WORDS = 6


def classification(ed: Mapping) -> str:
    """The degree's classification to print: `classification:`, else a short `note:` that reads like one ("First Class with Distinction"); never
    an expected grade, never a longer note ("Distinction-level dissertation on ...")."""
    for key in ("classification", "note"):
        v = " ".join(str(ed.get(key) or "").split())
        if v and not _EXPECTED.search(v) and (key == "classification" or (_CLASS.search(v) and len(v.split()) <= CLASS_MAX_WORDS)):
            return v
    return ""
_AFFILIATION = re.compile(r"dissertation\s+with\s+(.+?)\s+affiliation", re.I)


def education_first_until(settings) -> date:
    """cv.education_first_until (YYYY-MM-DD), else DEFAULT_UNTIL."""
    raw = getattr(settings, "raw", settings) or {}
    v = ((raw.get("cv") or {}) if isinstance(raw, dict) else {}).get("education_first_until")
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()) if v else DEFAULT_UNTIL
    except ValueError:
        return DEFAULT_UNTIL


def compact_on(settings) -> bool:
    """cv.education_compact (code default true): the compact Education block; false keeps the older one (school, degree, modules)."""
    raw = getattr(settings, "raw", settings) or {}
    return ((raw.get("cv") or {}) if isinstance(raw, dict) else {}).get("education_compact", True) is not False


def years_asked(jd: str) -> int:
    """The highest N of an "N+ years of ... experience" requirement of the ad (0 when it asks none). Read by the v3.3 screener (`jd_years`):
    a sentence that names years and experience (commercial, professional, industry, hands-on ...)."""
    from ..review.screeners import jd_years

    return max((n for skill, n in jd_years(jd or "", []) if skill is None), default=0)


def order_for(job: Mapping, settings, today: date | None = None) -> tuple[list[str], str]:
    """(the section keys in order, why: "" for the default order)."""
    today = today or date.today()
    until = education_first_until(settings)
    if today > until:
        return [*EXPERIENCE_FIRST, *TAIL], f"Experience first: cv.education_first_until ({until.isoformat()}) has passed"
    n = years_asked(str(job.get("description") or ""))
    if n >= SENIOR_YEARS:
        return [*EXPERIENCE_FIRST, *TAIL], f"Experience first for this job: the ad asks for {n}+ years of experience"
    return [*EDUCATION_FIRST, *TAIL], ""


def doctor_line(settings, today: date | None = None) -> tuple[str, object, str]:
    today = today or date.today()
    until = education_first_until(settings)
    if today > until:
        return (f"CV order: Experience first (cv.education_first_until {until.isoformat()} has passed; Education now comes after Projects)",
                "warn", "set cv.education_first_until to a later date to keep Education first")
    return (f"CV order: Education first until {until.isoformat()} (cv.education_first_until), then Experience first; an ad asking for "
            f"{SENIOR_YEARS}+ years of experience gets Experience first", True, "")


def _lines(text: str, width: int = LINE_CHARS) -> int:
    return max(1, math.ceil(len(text) / width))


def degree_name(ed: Mapping) -> str:
    """The degree as the user wrote it shortest: the first part of its `display_line` (before ", " or ";", brackets taken out) when that starts
    like `degree` ("B.E. Electrical & Electronics Engineering"), else `degree`."""
    degree = " ".join(str(ed.get("degree") or "").split())
    shown = re.split(r",\s|;\s", re.sub(r"\s*\([^)]*\)", "", " ".join(str(ed.get("display_line") or "").split())), maxsplit=1)[0].strip()
    return shown if shown and degree and shown.split()[0] == degree.split()[0] and len(shown) <= len(degree) else degree


def compact_education(education: Sequence[Mapping], dissertation: bool = True) -> tuple[list[dict], list[str]]:
    """(rows, notes). A row is {"kind": "degree", "head" (degree, italic), "school" (bold), "cls", "tail" (school and cls), "dates"} or
    {"kind": "dissertation", "text"}; together at most EDUCATION_MAX_LINES printed lines (the dissertation line goes first, then the last degrees)."""
    rows: list[dict] = []
    notes: list[str] = []
    for ed in education:
        degree = degree_name(ed)
        school = " ".join(str(ed.get("display_org") or ed.get("org") or "").split())
        cls = classification(ed)
        tail = ", ".join(x for x in (school, cls) if x)
        rows.append({"kind": "degree", "head": degree, "school": school, "cls": cls, "tail": tail,
                     "dates": " ".join(str(ed.get("dates") or "").split())})
        title = " ".join(str(ed.get("dissertation") or "").split())
        if title and dissertation:
            m = _AFFILIATION.search(str(ed.get("display_line") or ""))
            aff = " ".join(str(ed.get("dissertation_affiliation") or (m.group(1) if m else "")).split())
            rows.append({"kind": "dissertation", "text": f"Dissertation with {aff} affiliation: {title}" if aff else f"Dissertation: {title}"})
        elif not title and _AFFILIATION.search(str(ed.get("display_line") or "")):
            notes.append(f"education: {degree} mentions a dissertation but has no `dissertation:` (a plain one-line title) in the fact bank, so the "
                         "CV prints no dissertation line")

    def printed(rs: list[dict]) -> int:
        return sum(_lines(f"{r['head']}, {r['tail']}  {r['dates']}") if r["kind"] == "degree" else _lines(r["text"], SMALL_LINE_CHARS) for r in rs)

    while printed(rows) > EDUCATION_MAX_LINES and any(r["kind"] == "dissertation" for r in rows):
        rows = [r for r in rows if r["kind"] != "dissertation"]
        notes.append(f"education: the dissertation line was left out to keep Education at {EDUCATION_MAX_LINES} lines")
    while printed(rows) > EDUCATION_MAX_LINES and len(rows) > 1:
        gone = rows.pop()
        notes.append(f"education: {gone.get('head') or gone.get('text')} was left out to keep Education at {EDUCATION_MAX_LINES} lines")
    return rows, notes


def first_bullet_fraction(lines: Sequence, heading: str = "Work Experience") -> float | None:
    """How far down page 1 the first Work Experience bullet starts (0 = top, 1 = bottom), from layout.pdf_lines; None when the heading is not on
    page 1 or no bullet follows it there. A bullet line is the first line under the heading that is indented past it."""
    page1 = [l for l in lines if l.page == 1]
    at = next((i for i, l in enumerate(page1) if " ".join(l.words).strip().lower() == heading.lower()), None)
    if at is None:
        return None
    x = page1[at].x
    for l in page1[at + 1:]:
        if l.x >= x + 3:                                  # itemize leftmargin 1.3em: the bullet sits ~4-14 pt right of the margin
            return l.y / A4_HEIGHT
    return None
