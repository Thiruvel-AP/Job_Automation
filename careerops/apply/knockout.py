"""The knock-out pre-scan: before anything is drafted or filled, the whole form is scanned for the questions that decide an application outright, and each
is checked against what the user's own files would answer (applicant.yaml, answers.yaml, the fact bank). A likely mismatch stops the job until the human
decides:  KNOCK-OUT: "<question>" → your answer "<answer>" may auto-reject. [c]ontinue / [s]kip job

  sponsorship / work authorisation  the visa plan's answer (a combined "now or in the future" question is Yes when either is Yes). A Yes is a knock-out;
                                    so is a No under a posting that says it offers no sponsorship when the user will need it later; so is right to work No
  years of experience               "5+ years" against applicant.yaml `years_experience`, else the dated roles of the fact bank
  degree                            PhD / master's / bachelor's / degree against the fact bank's `education`. A missing or empty education section, a
                                    CV without the degree line: never evidence of lacking it. Only an education list that names other degrees, or an
                                    answers.yaml "No", is
  salary floor                      "minimum salary £60,000" above the numeric salary answer of answers.yaml
  on-site / location                on-site, hybrid, commute or relocate answered No in answers.yaml
  clearance                         security clearance answered No in answers.yaml

A question the files cannot answer is not a knock-out here (it is asked later, as before). In auto mode a knock-out always goes to the human
(`stop_reason`). Nothing here touches a page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping

from . import answers as answers_mod
from .filler import NeedsUser

NO_SPONSORSHIP = re.compile(
    r"(?:not|unable\s+to|cannot|can['’]?t|won['’]?t|will\s+not|do\s+not|does\s+not|don['’]?t|doesn['’]?t|no\s+longer)\s+(?:\w+\s+){0,3}"
    r"(?:provide|offer|sponsor|support|consider)\w*\s+(?:\w+\s+){0,3}(?:sponsorship|visas?)|"
    r"\bno\s+(?:visa\s+)?sponsorship\b|sponsorship\s+(?:is\s+|are\s+)?(?:not\s+)?(?:un)?available|without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:offered|provided|possible)", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:or\s+more\s+)?(?:years?|yrs?)\b", re.I)
EXPERIENCE = re.compile(r"experience|years?\s+of|worked|working\s+with", re.I)
HAS = re.compile(r"\b(?:do\s+you|have\s+you|are\s+you|hold|holding|possess|completed|obtained|educated|degree|graduat\w+|qualified|qualification)\b", re.I)
LEVELS = (("phd", re.compile(r"\bph\.?\s?d\b|doctorate|doctoral", re.I)),
          ("master", re.compile(r"\bmaster(?:['’]?s)?\b|\bm\.?\s?sc\b|\bm\.?\s?eng\b|\bm\.\s?tech\b|\bm\.a\b|\bmba\b", re.I)),
          ("bachelor", re.compile(r"\bbachelor(?:['’]?s)?\b|\bb\.?\s?sc\b|\bb\.e\b|\bb\.?\s?eng\b|\bb\.\s?tech\b|\bb\.a\b|undergraduate", re.I)))
DEGREE_ASKED = re.compile(r"\bph\.?\s?d\b|doctorate|doctoral|\bmaster(?:['’]?s)?\b|\bmsc\b|\bbachelor|\bbsc\b|\bdegree\b|\bgraduate\b", re.I)
MONEY = re.compile(r"(?:£|gbp\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?(k)?", re.I)
FLOOR = re.compile(r"minimum|at\s+least|floor|no\s+less\s+than|must\s+accept|starting\s+(?:at|from)|from\s+£", re.I)
ONSITE = re.compile(r"on-?site|in\s+the\s+office|office-?based|hybrid|\bcommut\w*|relocat\w*|days?\s+(?:a|per|in\s+the)\s+(?:week|office)|in-?person", re.I)
CLEARANCE = re.compile(r"clearance|\bBPSS\b|\bSC\b|\bDV\b|\bCTC\b|security\s+vetting|\bvetted\b|vetting", re.I)
PRESENT = re.compile(r"present|current|now|ongoing", re.I)


@dataclass(frozen=True)
class Knock:
    kind: str
    question: str
    answer: str
    why: str = ""

    def line(self) -> str:
        return f'KNOCK-OUT: "{self.question}" → your answer "{self.answer}" may auto-reject. [c]ontinue / [s]kip job'


class SkipJob(NeedsUser):
    """The human chose to skip the job at a knock-out: nothing was filled, nothing is submitted."""


def _fold(text: str) -> str:
    return " ".join(str(text or "").split())


def years_known(applicant: Mapping[str, Any] | None, fb: Mapping[str, Any] | None, today_year: int) -> float | None:
    """The user's years of experience: applicant.yaml `years_experience`, else the summed durations of the fact bank's dated roles (None when neither says)."""
    declared = (applicant or {}).get("years_experience")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        return float(declared)
    total, seen = 0.0, False
    for role in (fb or {}).get("experience") or []:
        text = str(role.get("dates") or "") if isinstance(role, Mapping) else ""
        found = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)]
        if not found:
            continue
        seen = True
        total += max(0, (today_year if PRESENT.search(text) else max(found)) - min(found))
    return total if seen else None


def degrees_held(fb: Mapping[str, Any] | None) -> set[str] | None:
    """{'phd', 'master', 'bachelor', 'degree'} of the fact bank's `education`; None when the section is missing or empty (that proves nothing)."""
    rows = [e for e in (fb or {}).get("education") or [] if isinstance(e, Mapping)]
    if not rows:
        return None
    held = {"degree"}
    for row in rows:
        for name, rx in LEVELS:
            if rx.search(str(row.get("degree") or "")):
                held.add(name)
    return held


def _answer(book: answers_mod.AnswerBook, question: str, job: Mapping[str, Any]) -> str:
    r = book.resolve(question, job)
    return _fold(r.text) if r is not None and r.known else ""


def _money(text: str) -> int | None:
    m = MONEY.search(text or "")
    if not m:
        return None
    n = int(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
    return n if n >= 1000 else None


def scan(questions: Iterable[tuple[str, list[str]]], applicant: Mapping[str, Any] | None, book: answers_mod.AnswerBook | None, job: Mapping[str, Any],
         plan: Any, fb: Mapping[str, Any] | None = None, *, today_year: int | None = None) -> list[Knock]:
    """The knock-outs among `questions` ((question, options) of every field of the form). Each question yields at most one."""
    from .assist import VISA_FIELD, about_another_country, sponsorship_answer, classify_visa, visa_value   # assist imports this module lazily

    book = book or answers_mod.AnswerBook()
    year = today_year or date.today().year
    out: list[Knock] = []
    for raw, options in questions:
        q = _fold(raw).rstrip(" *")
        if not q:
            continue
        found = None
        if VISA_FIELD.search(q) and not about_another_country(q, plan):
            found = _visa(q, plan, sponsorship_answer, classify_visa, visa_value)
        if found is None and CLEARANCE.search(q):
            ans = _answer(book, q, job)
            if re.fullmatch(r"no\b.*", ans, re.I):
                found = Knock("clearance", q, ans, "your answers.yaml says you do not hold the clearance")
        if found is None and ONSITE.search(q):
            ans = _answer(book, q, job)
            if re.fullmatch(r"no\b.*", ans, re.I):
                found = Knock("on-site / location", q, ans, "your answers.yaml says you cannot do it")
        if found is None and FLOOR.search(q) and re.search(r"salary|pay|compensation|remuneration", q, re.I):
            floor, mine = _money(q), None
            r = book.resolve(q, job)
            if r is not None and r.numeric and str(r.numeric).replace(",", "").isdigit():
                mine = int(str(r.numeric).replace(",", ""))
            if floor is not None and mine is not None and mine < floor:
                found = Knock("salary floor", q, f"£{mine:,}", f"the posting's floor is £{floor:,}")
        if found is None and YEARS.search(q) and EXPERIENCE.search(q):
            need = int(YEARS.search(q).group(1))
            have = years_known(applicant, fb, year)
            if have is not None and have < need:
                found = Knock("years of experience", q, f"{have:g} years", f"the form asks for {need}+")
        if found is None and DEGREE_ASKED.search(q) and HAS.search(q) and not EXPERIENCE.search(q):
            found = _degree(q, options, book, job, fb)
        if found is not None:
            out.append(found)
    return out


def _visa(q: str, plan: Any, sponsorship_answer, classify_visa, visa_value) -> Knock | None:
    kind = classify_visa(q)
    if kind is None or plan is None:
        return None
    value = sponsorship_answer(plan, q)
    if kind == "right_to_work_now":
        return Knock("work authorisation", q, value, "you would say you have no right to work here") if value == "No" else None
    if value == "Yes":
        return Knock("sponsorship", q, value, "an application that needs sponsorship is often rejected automatically")
    if value == "No" and NO_SPONSORSHIP.search(q):
        later = any(plan.answers.get(k) is True for k in ("sponsorship_now", "sponsorship_future"))
        if later:
            return Knock("sponsorship", q, value, "the posting offers no sponsorship and your visa block says you will need it in the future")
    return None


def _degree(q: str, options: list[str], book: answers_mod.AnswerBook, job: Mapping[str, Any], fb: Mapping[str, Any] | None) -> Knock | None:
    asked = next((name for name, rx in LEVELS if rx.search(q)), "degree")
    ans = _answer(book, q, job)
    if re.fullmatch(r"no\b.*", ans, re.I):
        return Knock("degree", q, ans, "your answers.yaml says you do not have it")
    if ans:
        return None
    held = degrees_held(fb)
    if held is None or asked in held:
        return None                       # no education section: not evidence; or the user has it
    if asked == "master" and "phd" in held:
        return None
    if asked == "bachelor" and held & {"master", "phd"}:
        return None
    return Knock("degree", q, "No", f"your fact bank's education lists no {asked}'s degree" if asked != "phd" else "your fact bank's education lists no PhD")


def confirm(knocks: list[Knock], ask: Callable[[str], str], out: Callable[[str], None] = print) -> bool:
    """Print every knock-out and ask once; only c / continue goes on. Anything else (Enter, s, x) skips the job. True with nothing to ask."""
    if not knocks:
        return True
    for k in knocks:
        out(f"\n  {k.line()}" + (f"\n    ({k.why})" if k.why else ""))
    return (ask("  KNOCK-OUT: [c]ontinue / [s]kip job > ") or "").strip().lower() in ("c", "continue")


def stop_reason(knocks: list[Knock]) -> str:
    """The reason an auto-apply stops at a knock-out (the human decides); '' with none."""
    if not knocks:
        return ""
    return "KNOCK-OUT: " + "; ".join(f'"{k.question[:90]}" → your answer "{k.answer}" ({k.kind})' for k in knocks[:3]) + \
        " may auto-reject: the human decides, nothing was drafted or submitted"
