"""What real screeners read besides keyword %: reported on the review screen, in the gap report and in the dry-run report.md. Advisory: none of these is a
gate (the structural check of ats_structure.py is the one that gates). Zero tokens, nothing written.

    title match       does the most recent role title, or the CV headline, carry the ad's title family (roles.families)? yes / partial (another of your
                      families) / no
    years             "N+ years" in the ad (overall, and per skill where a keyword follows or precedes it) against the CV's dated roles: months summed
                      without counting an overlap twice; per skill only the roles whose text names it. met / short by X months / not stated
    degree            the lowest level the ad asks (bachelor < master < PhD; "desirable" is not asked) against the levels the CV's Education section
                      shows: met / not met / not stated
    skills-only       required keywords the CV has ONLY in Skills (no bullet, no profile line): many screeners rank those lower
    knock-outs        for each knock-out the ad states (right to work / sponsorship, on-site days, security clearance), what applicant.yaml and
                      answers.yaml would answer: a mismatch is caught before a form is opened

The one-line summary:
    ATS structure 94/A (0 critical) | lints: multiple-date-ranges (Avasoft) | title match: yes | years: Python 3+ met; ML 2+ short by 4 months | degree met | skills-only required: 2
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from .. import ats_structure as AS
from ..discovery import dealbreakers
from ..discovery import gates
from ..apply import knockout

RED, RESET = "\033[31m", "\033[0m"
LEVEL_RANK = {"degree": 1, "bachelor": 1, "master": 2, "phd": 3}
LEVEL_NAME = {"degree": "a degree", "bachelor": "a bachelor's", "master": "a master's", "phd": "a PhD"}
YEARS_RX = re.compile(r"(?:at\s+least\s+|minimum\s+(?:of\s+)?|over\s+)?(\d{1,2})\s*(?:\+|plus)?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b(?:['’]s?)?", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
ROLE_HEADER = re.compile(rf"^(?P<left>\S.*?)\s{{2,}}(?P<dates>{AS.DATE_POINT}{AS.DATE_SEP}{AS.DATE_POINT})\s*$", re.I)
WORK_RIGHTS = re.compile(r"right to work|eligib\w+ to work|work permit|visa|sponsor", re.I)


# ------------------------------------------------------------------ dates and months
def month_of(point: str, today: date, *, end: bool) -> int | None:
    """A date point as a month index (year * 12 + month - 1); Present / current is `today`; a bare year is January (start) or December (end)."""
    s = point.strip().lower().rstrip(".")
    if re.fullmatch(r"present|current|now|ongoing", s):
        return today.year * 12 + today.month - 1
    if (m := re.fullmatch(r"(\d{1,2})\s*/\s*(\d{4})", s)):
        return int(m.group(2)) * 12 + int(m.group(1)) - 1
    if (m := re.fullmatch(r"(\d{4})", s)):
        return int(m.group(1)) * 12 + (11 if end else 0)
    if (m := re.match(r"([a-z]{3})[a-z]*\.?\s*,?\s*(\d{4})", s)) and m.group(1) in _MONTHS:
        return int(m.group(2)) * 12 + _MONTHS[m.group(1)] - 1
    return None


def span_of(text: str, today: date) -> tuple[int, int] | None:
    m = AS.DATE_RANGE.search(text or "")
    if not m:
        return None
    a, b = month_of(m.group("a"), today, end=False), month_of(m.group("b"), today, end=True)
    return (a, b) if a is not None and b is not None and b >= a else None


def merged_months(spans: Sequence[tuple[int, int]]) -> int:
    """Months covered by the spans (each inclusive at both ends), an overlap counted once."""
    total, cur = 0, None
    for a, b in sorted(spans):
        if cur is None or a > cur[1] + 1:
            if cur is not None:
                total += cur[1] - cur[0] + 1
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    return total + (cur[1] - cur[0] + 1 if cur is not None else 0)


@dataclass
class Block:
    header: str
    span: tuple[int, int] | None
    text: str                               # the header, the title and the bullets


def role_blocks(text: str, today: date) -> list[Block]:
    """The roles of the CV's Experience section in the text layer: a line ending in a date range (after a gap) starts one."""
    out: list[Block] = []
    for line in AS.section(text, "experience").splitlines():
        m = ROLE_HEADER.match(line.strip())
        if m:
            out.append(Block(m.group("left"), span_of(m.group("dates"), today), line.strip()))
        elif out:
            out[-1].text += "\n" + line.strip()
    return out


# ------------------------------------------------------------------ the signals
def _families(settings) -> list:
    return list(gates.load_roles(settings)) if settings is not None else []


def title_match(job_title: str, candidates: Sequence[str], settings) -> tuple[str, str]:
    """(yes | partial | no | n/a, why). n/a: the ad's title is in none of your families."""
    fam = gates.match_family(job_title or "", settings) if settings is not None else None
    if fam is None:
        return "n/a", f'"{job_title}" is in none of roles.families'
    cands = [c for c in candidates if c]
    hit = next((c for c in cands if gates._any(fam.title, c)), None)
    if hit:
        return "yes", f'"{hit}" carries {fam.name}'
    other = next(((c, f) for c in cands for f in _families(settings) if f.name != fam.name and gates._any(f.title, c)), None)
    if other:
        return "partial", f'"{other[0]}" is {other[1].name}, the ad is {fam.name}'
    return "no", f"neither {', '.join(repr(c) for c in cands) or 'title'} carries {fam.name}"


@dataclass
class Req:
    skill: str | None                      # None: overall experience
    years: int
    have_months: int | None = None         # None: the CV shows no dated role

    @property
    def label(self) -> str:
        return f"{self.skill or 'experience'} {self.years}+"

    def text(self) -> str:
        if self.have_months is None:
            return f"{self.label} not shown (no dated role found)"
        short = self.years * 12 - self.have_months
        return f"{self.label} met" if short <= 0 else f"{self.label} short by {short} months"


def _kw_terms(keywords: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[str]]]:
    out = []
    for k in keywords:
        if isinstance(k, Mapping) and k.get("term"):
            out.append((str(k["term"]), [str(k["term"]), *[str(a) for a in (k.get("alts") or [])]]))
    return out


def _has(text: str, words: Sequence[str]) -> bool:
    low = " " + " ".join(text.lower().split()) + " "
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", low) for w in words if w)


def jd_years(jd: str, keywords: Sequence[Mapping[str, Any]]) -> list[tuple[str | None, int]]:
    """(skill or None, years) of every "N+ years" requirement of the ad. The skill is the first keyword after the number (within 60 characters), or right
    before it ("Python (3+ years)"); with none, a sentence about experience is the overall requirement. The highest N per skill."""
    terms = _kw_terms(keywords)
    best: dict[str | None, int] = {}
    for s in dealbreakers.sentences(jd):
        for m in YEARS_RX.finditer(s):
            n = int(m.group(1))
            if not 1 <= n <= 30:
                continue
            tail, head = s[m.end():m.end() + 60], s[max(0, m.start() - 40):m.start()]
            skill = None
            found = [(tail.lower().find(w.lower()), t) for t, ws in terms for w in ws if w and _has(tail, [w])]
            if found:
                skill = min(found)[1]
            elif re.search(r"\(\s*$", head) and (back := [t for t, ws in terms if _has(head, ws)]):
                skill = back[-1]
            elif not re.search(r"experience|experienced|working|worked|industry|commercial", s, re.I):
                continue
            best[skill] = max(best.get(skill, 0), n)
    return sorted(best.items(), key=lambda x: (x[0] is not None, x[0] or ""))


def years(jd: str, text: str, keywords: Sequence[Mapping[str, Any]], today: date) -> list[Req]:
    blocks = role_blocks(text, today)
    spans = [b.span for b in blocks if b.span]
    terms = dict(_kw_terms(keywords))
    out = []
    for skill, n in jd_years(jd, keywords):
        if skill is None:
            have = merged_months(spans) if spans else None
        else:
            mine = [b.span for b in blocks if b.span and _has(b.text, terms.get(skill, [skill]))]
            have = merged_months(mine) if spans else None
        out.append(Req(skill, n, have))
    return out


def degree(jd: str, text: str) -> tuple[str, str]:
    """(met | not met | not stated | unknown, why). The lowest level the ad ASKS (a sentence calling it desirable / preferred does not ask) against the
    highest the CV's Education section shows."""
    asked: list[str] = []
    for s in dealbreakers.sentences(jd):
        if not knockout.DEGREE_ASKED.search(s) or dealbreakers.OPTIONAL.search(s):
            continue
        if re.search(r"\bgraduate\b", s, re.I) and not re.search(r"degree|bachelor|master|phd|bsc|msc", s, re.I):
            continue                                                # "graduate scheme", "graduate role": not a degree requirement
        levels = [name for name, rx in knockout.LEVELS if rx.search(s)] or ["degree"]
        asked.append(min(levels, key=lambda x: LEVEL_RANK[x]))
    if not asked:
        return "not stated", "the ad asks for no degree"
    need = min(asked, key=lambda x: LEVEL_RANK[x])
    edu = AS.section(text, "education")
    if not edu.strip():
        return "unknown", "the CV has no Education section"
    held = [name for name, rx in knockout.LEVELS if rx.search(edu)] or (["degree"] if re.search(r"degree|\bb\.?\s?[a-z]{1,4}\b", edu, re.I) else [])
    if not held:
        return "not met", f"the ad asks for {LEVEL_NAME[need]}; the Education section names no degree"
    top = max(held, key=lambda x: LEVEL_RANK[x])
    ok = LEVEL_RANK[top] >= LEVEL_RANK[need]
    return ("met" if ok else "not met"), f"the ad asks for {LEVEL_NAME[need]}; the CV shows {LEVEL_NAME[top]}"


def skills_only(keywords: Sequence[Mapping[str, Any]], text: str) -> list[str]:
    """Required keywords present in the CV's Skills section and nowhere else in its text."""
    skills = AS.section(text, "skills")
    if not skills.strip():
        return []
    rest = text.replace(skills, " ")
    return [t for t, ws in _kw_terms([k for k in keywords if k.get("required")]) if _has(skills, ws) and not _has(rest, ws)]


def knockouts(jd: str, job: Mapping[str, Any], applicant: Mapping[str, Any] | None, book) -> list[str]:
    """One line per knock-out the ad states, with what your files would answer. `RISK` marks a likely auto-reject."""
    from ..apply import visa as visa_mod

    out: list[str] = []
    sents = dealbreakers.sentences(jd)
    work_s = next((s for s in sents if WORK_RIGHTS.search(s)), None)
    if work_s:
        plan = visa_mod.plan_for(applicant, visa_mod.work_for(job, applicant)) if applicant is not None else None
        if plan is None:
            out.append(f'knock-out right to work / sponsorship: "{work_s[:120]}" → applicant.yaml has no visa answers for this job: you will be asked')
        else:
            rtw, now, later = plan.answer("right_to_work"), plan.answer("sponsorship_now"), plan.answer("sponsorship_future")
            risk = ""
            if rtw == "No":
                risk = " RISK: you would say you have no right to work here"
            elif knockout.NO_SPONSORSHIP.search(jd) and "Yes" in (now, later):
                risk = " RISK: the ad offers no sponsorship and your answers say you need it" + (" now" if now == "Yes" else " later")
            out.append(f'knock-out right to work / sponsorship: "{work_s[:120]}" → right to work {rtw or "ASK"}, sponsorship now {now or "ASK"}, '
                       f"in the future {later or 'ASK'} ({plan.source}){risk}")
    for kind, rx, question in (("on-site days", dealbreakers.ONSITE_DAYS, "Are you able to work on-site in the office as required?"),
                               ("security clearance", None, "Do you hold, or are you eligible for, security clearance?")):
        if rx is not None:
            s = next((x for x in sents if dealbreakers._states(rx, x)), None)
        else:
            from ..discovery import agent_triage
            s = agent_triage.clearance_sentence(jd)
        if not s:
            continue
        r = book.resolve(question, job) if book is not None else None
        ans = " ".join(r.text.split()) if r is not None and r.known else ""
        risk = " RISK" if re.fullmatch(r"no\b.*", ans, re.I) else ""
        out.append(f'knock-out {kind}: "{s[:120]}" → answers.yaml: ' + (f'"{ans}"{risk}' if ans else "no answer: you will be asked"))
    return out


# ------------------------------------------------------------------ one job
@dataclass
class Signals:
    title: tuple[str, str] = ("n/a", "")
    years: list[Req] = field(default_factory=list)
    degree: tuple[str, str] = ("not stated", "")
    skills_only: list[str] = field(default_factory=list)
    knockouts: list[str] = field(default_factory=list)


@dataclass
class AtsCheck:
    structure: AS.JobStructure
    signals: Signals

    @property
    def ok(self) -> bool:
        return self.structure.ok

    def line(self) -> str:
        st, sg = self.structure, self.signals
        head = st.cv.line() if st.cv is not None else (f"ATS structure: not checked ({st.error})" if st.error else "ATS structure: no CV")
        codes = list(dict.fromkeys(f"{l.code} ({l.where})" for l in st.lints))
        parts = [head, f"lints: {', '.join(codes)}" if codes else "lints: none", f"title match: {sg.title[0]}",
                 "years: " + ("; ".join(r.text() for r in sg.years) if sg.years else "not stated"), f"degree {sg.degree[0]}",
                 f"skills-only required: {len(sg.skills_only)}"]
        return " | ".join(parts)

    def lines(self, color: bool = True) -> list[str]:
        """The summary line (red when the CV may not go out), then the issues, the lints with the decision each needs, and the signals' details."""
        st, sg = self.structure, self.signals
        first = self.line()
        if not st.ok:
            first = (f"{RED}{first}{RESET}" if color else first) + f"  ← {st.why_not()}: auto-submit is blocked; in assist, check the PDF before you send it"
        out = [first]
        if st.cv is not None:
            out += [f"  {i.line()}" for i in st.cv.issues]
        if st.letter is not None:
            out.append(f"  {st.letter.line()}" + ("" if not st.letter.issues else ": " + "; ".join(i.line() for i in st.letter.issues)))
        out += [f"  {l.line()}" for l in st.lints]
        if sg.title[1]:
            out.append(f"  title match {sg.title[0]}: {sg.title[1]}")
        if sg.degree[1]:
            out.append(f"  degree {sg.degree[0]}: {sg.degree[1]}")
        if sg.skills_only:
            out.append(f"  skills-only required (no bullet or profile line shows them): {', '.join(sg.skills_only)}")
        out += [f"  {k}" for k in sg.knockouts]
        return out

    def as_dict(self) -> dict:
        st = self.structure
        return {"line": self.line(), "lines": self.lines(color=False), "ok": self.ok, "cv": st.cv.as_dict() if st.cv else None,
                "letter": st.letter.as_dict() if st.letter else None, "lints": [l.__dict__ for l in st.lints], "error": st.error}


def _jd_of(job) -> str:
    try:
        return str(job["description"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def check(rep: Mapping[str, Any], settings, fb: Mapping[str, Any] | None, job, *, today: date | None = None, applicant=None, book=None) -> AtsCheck:
    """The structural check, the lints and the signals of one tailored job. `applicant` / `book` default to the files settings name (read only)."""
    today = today or date.today()
    st = AS.for_report(rep, fb)
    sg = Signals()
    if st.cv is not None:
        jd, kws, text = _jd_of(job), list(rep.get("keywords") or []), st.text
        jobd = dict(job) if job is not None else {}
        if applicant is None and settings is not None:
            from ..config import load_yaml
            try:
                applicant = load_yaml(settings.path("applicant"))
            except Exception:                                   # noqa: BLE001 - no applicant.yaml: the knock-outs say "you will be asked"
                applicant = None
        if book is None and settings is not None:
            from ..apply import answers as answers_mod
            try:
                book = answers_mod.load(answers_mod.path_for(settings))
            except Exception:                                   # noqa: BLE001
                book = None
        latest = st.roles[0]["title"] if st.roles else ""
        headline = str(rep.get("headline") or "")
        hook_line = next((l.strip() for l in text.splitlines()[1:3] if l.strip()), "")
        sg = Signals(title_match(str(jobd.get("title") or rep.get("title") or ""), [latest, headline, hook_line], settings),
                     years(jd, text, kws, today), degree(jd, text), skills_only(kws, text), knockouts(jd, jobd, applicant, book))
    return AtsCheck(st, sg)
