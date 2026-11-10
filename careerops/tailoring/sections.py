"""The two-page CV's section order and its compact Education (user rule, 10 Nov 2026; replaces the 8 Nov Education-first-until-a-date order).

  Order      a graduate / junior / scheme ad (GRADUATE_TITLE in its title, or GRADUATE_TEXT in its text: "graduate scheme", "entry-level",
             "early careers", "2027 start" ...): Profile, Education, Skills, Work Experience, Projects, Certifications & Achievements (GRADUATE).
             Every other ad: Profile, Skills, Work Experience, Projects, Education, Certifications & Achievements (STANDARD). The review says which
             and why. The optional fact-bank sections (research focus, publications, languages) follow at the end. The headings never change (the
             v3.3 ATS structure check reads them; the order of sections is not scored). cv.education_first_until is no longer read.
  Education  one line per degree (degree, university, dates; a classification when it is not an expected one), and under the degree that has a
             dissertation the line "Dissertation with <affiliation> affiliation: <title>" (its `dissertation:`, a plain one-line title the user
             wrote), else the dissertation clause of its own `display_line` word for word ("Dissertation with AstraZeneca affiliation (GeneTraceAI)").
             No module lists. The dissertation line is never left out to save space (honesty over lint, 10 Nov 2026): the EDUCATION_MAX_LINES (4)
             cap takes the oldest degrees out first, never the dissertation line.
  Position   when Education comes before Work Experience, the first Work Experience bullet should start in the top FIRST_BULLET_MAX (55 %) of page
             1 (layout.pdf_lines); when it does not, the review warns. Nothing is taken out for it.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Mapping, Sequence

GRADUATE = ("profile", "education", "skills", "experience", "projects", "certifications")
STANDARD = ("profile", "skills", "experience", "projects", "education", "certifications")
EDUCATION_FIRST, EXPERIENCE_FIRST = GRADUATE, STANDARD           # the 8 Nov names, kept for callers
TAIL = ("research_focus", "publications", "languages")
DEFAULT_UNTIL = date(2027, 9, 30)
# A graduate / junior / scheme ad (10 Nov 2026). The title decides most ads; the text only by phrases that cannot be about someone else
# ("mentor junior engineers", "a graduate degree" and "pension scheme" are not graduate ads).
GRADUATE_TITLE = re.compile(r"\b(?:graduate|grad|junior|jr|entry[- ]level|early[- ]careers?|scheme|new[- ]grad)\b", re.I)
GRADUATE_TEXT = re.compile(r"\bgraduate\s+(?:scheme|programme|program|role|position|opportunit(?:y|ies)|intake|cohort|job)s?\b"
                           r"|\b(?:training|development|early[- ]careers?)\s+(?:scheme|programme)s?\b|\bentry[- ]level\b|\bearly[- ]careers?\b"
                           r"|\b20\d\d\s+(?:start|intake|cohort)\b|\brecent\s+graduates?\b|\bnew\s+grads?\b", re.I)
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


def _get(job: Mapping, key: str) -> str:
    try:
        return str(job[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def graduate_reason(job: Mapping) -> str:
    """Why this is a graduate / junior / scheme ad ("title says 'Graduate'", "the ad says 'graduate scheme'"), or "" when it is not."""
    m = GRADUATE_TITLE.search(_get(job, "title"))
    if m:
        return f"the title says '{m.group(0)}'"
    m = GRADUATE_TEXT.search(_get(job, "description"))
    return f"the ad says '{m.group(0)}'" if m else ""


def order_for(job: Mapping, settings=None, today: date | None = None) -> tuple[list[str], str]:
    """(the section keys in order, why). GRADUATE for a graduate / junior / scheme ad, else STANDARD (`settings` and `today` are not read any more)."""
    why = graduate_reason(job)
    if why:
        return [*GRADUATE, *TAIL], f"graduate order (Education, then Skills, before Work Experience): {why}"
    return [*STANDARD, *TAIL], "standard order (Skills, Work Experience, Projects, then Education): not a graduate, junior or scheme ad"


def doctor_line(settings=None, today: date | None = None) -> tuple[str, object, str]:
    return ("CV order: graduate / junior / scheme ads get Profile, Education, Skills, Work Experience, Projects, Certifications; every other ad "
            "Profile, Skills, Work Experience, Projects, Education, Certifications", True, "")


def _lines(text: str, width: int = LINE_CHARS) -> int:
    return max(1, math.ceil(len(text) / width))


def degree_name(ed: Mapping) -> str:
    """The degree as the user wrote it shortest: the first part of its `display_line` (before ", " or ";", brackets taken out) when that starts
    like `degree` ("B.E. Electrical & Electronics Engineering"), else `degree`."""
    degree = " ".join(str(ed.get("degree") or "").split())
    shown = re.split(r",\s|;\s", re.sub(r"\s*\([^)]*\)", "", " ".join(str(ed.get("display_line") or "").split())), maxsplit=1)[0].strip()
    return shown if shown and degree and shown.split()[0] == degree.split()[0] and len(shown) <= len(degree) else degree


_CLAUSE = re.compile(r"\bdissertation\b[^;]*", re.I)


def dissertation_clause(ed: Mapping) -> str:
    """The dissertation part of an education entry's `display_line`, word for word with a capital D ("Dissertation with AstraZeneca affiliation
    (GeneTraceAI)"), or "" when it names none."""
    m = _CLAUSE.search(" ".join(str(ed.get("display_line") or "").split()))
    if not m:
        return ""
    text = m.group(0).strip().rstrip(",.")
    return text[0].upper() + text[1:]


def compact_education(education: Sequence[Mapping], dissertation: bool = True) -> tuple[list[dict], list[str]]:
    """(rows, notes). A row is {"kind": "degree", "head" (degree, italic), "school" (bold), "cls", "tail" (school and cls), "dates"} or
    {"kind": "dissertation", "text"}; at most EDUCATION_MAX_LINES printed lines by taking the last degrees out, never the dissertation line
    (10 Nov 2026: context is never dropped to fit a rule). `dissertation=False` is kept for old callers and ignored."""
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
        if title:
            m = _AFFILIATION.search(str(ed.get("display_line") or ""))
            aff = " ".join(str(ed.get("dissertation_affiliation") or (m.group(1) if m else "")).split())
            rows.append({"kind": "dissertation", "text": f"Dissertation with {aff} affiliation: {title}" if aff else f"Dissertation: {title}"})
        elif dissertation_clause(ed):
            rows.append({"kind": "dissertation", "text": dissertation_clause(ed)})

    def printed(rs: list[dict]) -> int:
        return sum(_lines(f"{r['head']}, {r['tail']}  {r['dates']}") if r["kind"] == "degree" else _lines(r["text"], SMALL_LINE_CHARS) for r in rs)

    def droppable() -> list[int]:                                            # a degree with no dissertation line under it
        return [i for i, r in enumerate(rows) if r["kind"] == "degree" and not (i + 1 < len(rows) and rows[i + 1]["kind"] == "dissertation")]

    while printed(rows) > EDUCATION_MAX_LINES and sum(r["kind"] == "degree" for r in rows) > 1 and droppable():
        gone = rows.pop(droppable()[-1])
        notes.append(f"education: {gone.get('head')} was left out to keep Education at {EDUCATION_MAX_LINES} lines")
    if printed(rows) > EDUCATION_MAX_LINES:
        notes.append(f"education: {printed(rows)} printed lines (more than {EDUCATION_MAX_LINES}); the dissertation line is kept")
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
