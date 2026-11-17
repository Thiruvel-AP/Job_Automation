"""The knock-out check before tailoring (v3.7.1). Zero tokens, no network. Not the form pre-scan of apply/knockout.py (that one reads an application form).

An ad can state a fact that decides the application before a CV is written. The 15 Nov Sirius Analysis ad said "To gain SC Clearance you will normally need at
least 5 years' UK residency", and `live` still tailored it. `settings.yaml -> knockouts.skip_before_tailoring` lists the knock-outs you want acted on; it is EMPTY
by default, so nothing changes until you list one. Allowed values:

  security_clearance_residency   a sentence with SC / DV / eSC clearance text (or "security clearance") that also names a residency period ("5 years' UK residency",
                                 "resident in the UK for the last 5 years")
  years_experience_min_5         a sentence that asks for 5 or more years: "5+ years", "at least 5 years", "minimum of 5 years", "5 or more years", "5 years' experience".
                                 Not a range that starts lower ("3-5 years"), not an optional ask (desirable, ideally, nice to have ...), not the company's own history
                                 ("founded over 5 years ago") and not a clearance or residency period (that is the other knock-out)

A hit needs a quoted sentence of the ad that states the fact; an inferred fact is never a hit. The file moves to `paths.jds_done/skipped_knockout/` with a
`<name>.reason.txt` beside it, and the decision log (data/evals/decisions.jsonl) records SKIP for it, before any model call; the matched sentence is printed. A dry run only
names the ad and leaves it where it is, like the estimate stop. `careerops unskip --shortlist --match ...` brings the file back to the inbox. A job you promoted
(`careerops promote`) is your override and is never stopped here. Unlike the estimate stop it also checks a file you named (`live --url`): you listed the knock-out yourself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..discovery import manual
from . import decided
from .hardstop import REASON_SUFFIX, Stop

FOLDER = "skipped_knockout"
CLEARANCE_RESIDENCY = "security_clearance_residency"
YEARS_MIN_5 = "years_experience_min_5"
KINDS = (CLEARANCE_RESIDENCY, YEARS_MIN_5)

_NUM_WORDS = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)"
_YEARS = rf"{_NUM_WORDS}\s*\+?\s*(?:years?|yrs?)\b"
CLEARANCE = re.compile(r"\b(?:eSC|SC|DV)\b(?=[^.]{0,30}\b(?:clearance|vetting|cleared|check))|\bsecurity[- ]clear\w*|\bdeveloped vetting\b|\b(?:eSC|SC|DV)\s+clear\w*", re.I)
CLEARANCE_CASE = re.compile(r"\b(?:eSC|SC|DV)\b")                       # the bare letters count only in capitals ("sc" is not a clearance)
RESIDENCY = re.compile(r"\bresiden(?:cy|ce|t|ts|cies)\b|\blived\b|\blive in the (?:UK|United Kingdom)\b|\bbased in the (?:UK|United Kingdom)\b", re.I)
PERIOD = re.compile(rf"{_YEARS}|\b(?:last|past|previous)\s+{_NUM_WORDS}\s+years?\b", re.I)
OPTIONAL = re.compile(r"\b(?:desirable|optional|nice to have|a plus|bonus|ideally|advantageous|beneficial|preferred)\b", re.I)
HISTORY = re.compile(r"\b(?:founded|established|since \d{4}|we have been|we(?:'|’)ve been|for over|in business|our history|years ago)\b", re.I)
_MIN = re.compile(rf"(?P<n>{_NUM_WORDS})\s*\+\s*(?:years?|yrs?)\b"                                  # 5+ years
                  rf"|\b(?:at least|minimum(?: of)?|min\.?|no less than)\s+(?P<n2>{_NUM_WORDS})\s*\+?\s*(?:years?|yrs?)\b"   # at least 5 years
                  rf"|\b(?P<n3>{_NUM_WORDS})\s+or\s+more\s+(?:years?|yrs?)\b"                         # 5 or more years
                  rf"|\b(?P<n4>{_NUM_WORDS})\s+(?:years?|yrs?)(?:'|’)?s?\s+(?:of\s+)?(?:\w+\s+){{0,3}}?experience\b", re.I)    # 5 years' commercial experience
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_MARKER = re.compile(r"^(?:[-*•·▪–]|\d+[.)])\s+")
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n(?=\s*(?:[-*•·▪–]|\d+[.)])\s)")


@dataclass(frozen=True)
class Match:
    kind: str
    sentence: str            # the ad's own sentence that states the fact, whitespace closed up


def sentences(text: str) -> list[str]:
    """The ad cut into sentences (and bullet lines), whitespace closed up, a list marker ("- ", "1. ") taken off the front."""
    return [_MARKER.sub("", " ".join(p.split())) for p in _BOUNDARY.split(str(text or "")) if p and p.strip()]


def _number(token: str) -> int:
    t = token.strip().lower()
    return int(t) if t.isdigit() else _WORDS.get(t, 0)


def _clearance_with_residency(s: str) -> bool:
    return bool((CLEARANCE_CASE.search(s) and CLEARANCE.search(s) or re.search(r"\bsecurity[- ]clear\w*|\bdeveloped vetting\b", s, re.I))
                and RESIDENCY.search(s) and PERIOD.search(s))


def _years_min_5(s: str) -> bool:
    """A sentence that asks for 5 or more years (see the module docstring for what does not count)."""
    if OPTIONAL.search(s) or HISTORY.search(s) or _clearance_with_residency(s) or (RESIDENCY.search(s) and not re.search(r"experience", s, re.I)):
        return False
    for m in _MIN.finditer(s):
        n = _number(next(g for g in m.group("n", "n2", "n3", "n4") if g))
        before = s[:m.start()].rstrip()
        if before.endswith(("-", "–", "—")) or re.search(r"\bto$", before, re.I):            # "3-5 years", "3 to 5 years": the ask starts lower
            continue
        if n >= 5:
            return True
    return False


_CHECKS = {CLEARANCE_RESIDENCY: _clearance_with_residency, YEARS_MIN_5: _years_min_5}


def enabled(settings) -> list[str]:
    """`knockouts.skip_before_tailoring` of settings.yaml: the listed knock-outs this module knows (a name it does not know is ignored; the list is empty when the
    key is missing, which switches the check off)."""
    raw = ((getattr(settings, "raw", None) or {}).get("knockouts") or {}).get("skip_before_tailoring") or []
    return [k for k in dict.fromkeys(str(x).strip() for x in raw) if k in _CHECKS]


def match(text: str, kinds: Sequence[str]) -> list[Match]:
    """The first sentence of the ad that states each listed knock-out (a kind this module does not know checks nothing). [] for an empty list."""
    out = []
    for kind in dict.fromkeys(kinds):
        check = _CHECKS.get(kind)
        if check is None:
            continue
        hit = next((s for s in sentences(text) if check(s)), None)
        if hit:
            out.append(Match(kind, hit))
    return out


def folder(settings) -> Path:
    """Where a knocked-out JD file goes: paths.jds_done/skipped_knockout/."""
    return decided.decision_dir(settings, FOLDER)


def reason_of(path: Path, settings) -> Stop | None:
    """The first listed knock-out the file states, else None. A file that cannot be read as a JD is not stopped here (live rejects it with its own reason)."""
    kinds = enabled(settings)
    if not kinds:
        return None
    try:
        text, _ = manual.read_jd(path)
    except manual.JDError:
        return None
    hits = match(text, kinds)
    if not hits:
        return None
    hdr = manual.header_fields(text)
    first = hits[0]
    return Stop(path, hdr.get("company") or "", hdr.get("title") or "",
                f"knock-out {first.kind} (settings knockouts.skip_before_tailoring): the ad says \"{first.sentence}\"")


def find(files: Iterable[Path], settings) -> list[Stop]:
    """Every file of `files` a listed knock-out applies to (your promoted jobs aside)."""
    if not enabled(settings):
        return []
    from . import hardstop

    exempt = hardstop.promoted_keys(settings)
    out = []
    for p in files:
        st = reason_of(p, settings)
        if st is not None and st.key not in exempt:
            out.append(st)
    return out


def file_away(settings, stops: Sequence[Stop], *, now: datetime | None = None) -> list[Path]:
    """Move each stopped file to skipped_knockout/, write its reason beside it, and log SKIP for it (append-only). Returns the new paths."""
    from ..discovery import decide

    now = now or datetime.now(timezone.utc)
    out, events = [], []
    for st in stops:
        if not st.path.is_file():
            continue
        dst = decided.move_file(settings, st.path, folder(settings))
        (dst.parent / (dst.stem + REASON_SUFFIX)).write_text(f"{st.reason}\n(from {st.path.parent.name}/, {now:%Y-%m-%d %H:%M} UTC; "
                                                             "`careerops unskip --shortlist --match ...` brings it back)\n", encoding="utf-8")
        out.append(dst)
        events.append({"key": st.key, "job_id": decided.file_job_id(dst) or "", "company": st.company, "title": st.title, "decision": decide.SKIP,
                       "by": "agent", "reason": f"knock-out before tailoring: {st.reason}", "file": str(dst)})
    decide.log(settings, events, now)
    return out


def jd_files(settings) -> list[Path]:
    """The JD files in skipped_knockout/ (their .reason.txt notes aside)."""
    return [p for p in decided.txt_files(folder(settings)) if not p.name.endswith(REASON_SUFFIX)]


def restore(settings, keys: set[str]) -> list[tuple[Path, Path]]:
    """`unskip --shortlist`: the knocked-out files of `keys` go back to the inbox (their reason note stays in skipped_knockout/ as the record). Returns (from, to) pairs."""
    moved = []
    for p in jd_files(settings):
        hdr = decided.header_of(p)
        if "|".join(decided.key_of(hdr.get("company"), hdr.get("title"))) not in keys:
            continue
        moved.append((p, decided.move_file(settings, p, decided.inbox_dir(settings))))
    return moved


def lines(stops: Sequence[Stop], *, applied: bool) -> list[str]:
    verb = "moved to" if applied else "would move to"
    return [f"  knock-out: {st.path.parent.name}/{st.path.name} ({st.company} | {st.title}): {st.reason} -> {verb} jds_done/{FOLDER}/" for st in stops]
