"""Priority = what to look at first. Sponsorship is a weight on the order, never a filter.

  P = F x S x R

  F  the honest ATS ceiling, 0-1 (the LLM-checked ceiling once tailored, else the triage-time one; 0 when not known yet)
  S  how sure the employer is to sponsor, from `priority.sponsor_weights` in settings.yaml:
       exact    on the sponsor register by name                                  1.00
       fuzzy    on the register by a near match                                  0.90
       unknown  not on the register, a recruitment agency, or no register yet    0.75
       no_sponsorship   the JD itself says it does not sponsor / needs the right to work without sponsorship   0.65
     When several apply the lowest one is used. A holder of the UK Graduate visa can take any of these jobs, so none of them
     removes a job; they only move it down.
  R  exp(-days_since_posted / priority.recency_days), 1.0 when the posting date is unknown

It orders the `live` inbox, `discover`'s listing and `status`; the review screen shows P and the three factors. Nothing here
skips, drops or hides a job.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_WEIGHTS = {"exact": 1.00, "fuzzy": 0.90, "unknown": 0.75, "no_sponsorship": 0.65}
DEFAULT_RECENCY_DAYS = 14.0

# ------------------------------------------------------------------ what the JD says about sponsorship (an info line, not a flag)
_NEG = (r"(?:do(?:es)? not|don[’']t|doesn[’']t|cannot|can[’']t|can not|unable to|not able to|will not|won[’']t|"
        r"not in a position to|not willing to|(?:are|is) not (?:able|willing|currently able) to|not currently able to)")
WORDING: list[tuple[str, str]] = [
    # (regex, kind). kind: no_sponsorship | right_to_work | sponsorship_offered. Tried in this order; the first kind that has a hit per
    # sentence is the sentence's kind.
    (r"\bwithout (?:the need for |requiring |needing |any )?(?:visa |employer |company )?sponsorship", "no_sponsorship"),
    (rf"{_NEG}\s+(?:currently\s+)?sponsor(?:ing)?\b", "no_sponsorship"),
    (rf"{_NEG}\s+(?:currently\s+)?(?:offer|provid|support|consider)\w*\s+(?:\w+\s+){{0,3}}?sponsorship", "no_sponsorship"),
    (r"\bno\s+(?:visa\s+|uk\s+visa\s+)?sponsorship\b", "no_sponsorship"),
    (r"\bsponsorship\s+(?:is\s+|will\s+be\s+)?(?:not|unavailable)\b", "no_sponsorship"),
    (r"\b(?:not|n[’']t)\s+(?:need|require)\s+(?:\w+\s+){0,2}?sponsorship", "no_sponsorship"),
    (r"\b(?:must|should)\s+not\s+require\s+(?:\w+\s+){0,2}?sponsorship", "no_sponsorship"),
    (r"\b(?:must|need to)\s+(?:already\s+)?(?:have|hold|possess)\s+(?:the\s+)?(?:full\s+|existing\s+|unrestricted\s+)?right to work\s+"
     r"in the (?:uk|united kingdom)", "right_to_work"),
    (r"\blicen[cs]ed sponsor\b", "sponsorship_offered"),
    (r"\b(?:visa\s+)?sponsorship\s+(?:is\s+|may\s+be\s+|can\s+be\s+)?(?:available|offered|provided|possible)\b", "sponsorship_offered"),
    (r"\b(?:who|that)\s+(?:need|require)s?\s+(?:visa\s+)?sponsorship\b", "sponsorship_offered"),
    (r"\b(?:can|will|able to|happy to|willing to)\s+(?:\w+\s+){0,2}?sponsor\b", "sponsorship_offered"),
]
_COMPILED = [(re.compile(p, re.I), kind) for p, kind in WORDING]
SNIPPET_CHARS = 200


@dataclass
class Wording:
    kind: str          # no_sponsorship | right_to_work | sponsorship_offered
    text: str          # the sentence the JD says it in, one line, cut to SNIPPET_CHARS


def _sentences(jd: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", jd or "") if s.strip()]


def sponsor_wording(jd: str) -> list[Wording]:
    """Every sentence of the JD that speaks of sponsorship or the right to work, with what it says, in the order of the text
    (at most one entry per sentence; the first matching pattern in WORDING decides its kind)."""
    out: list[Wording] = []
    for sentence in _sentences(jd):
        for rx, kind in _COMPILED:
            if rx.search(sentence):
                one_line = " ".join(sentence.split())
                out.append(Wording(kind, one_line if len(one_line) <= SNIPPET_CHARS else one_line[: SNIPPET_CHARS - 1] + "…"))
                break
    return out


def says_no_sponsorship(jd: str) -> bool:
    return any(w.kind == "no_sponsorship" for w in sponsor_wording(jd))


def wording_line(jd: str) -> str | None:
    """One info line for the review screen, or None when the JD says nothing about sponsorship."""
    found = sponsor_wording(jd)
    if not found:
        return None
    label = {"no_sponsorship": "says no sponsorship", "right_to_work": "asks for the right to work",
             "sponsorship_offered": "offers or welcomes sponsorship"}
    return "Sponsorship wording in the JD (information only): " + " | ".join(f"\"{w.text}\" ({label[w.kind]})" for w in found[:3])


# ------------------------------------------------------------------ the score
@dataclass
class Priority:
    p: float
    f: float
    s: float
    r: float
    s_reason: str          # why S is what it is
    days: float | None     # days since posted, None when unknown

    def line(self) -> str:
        age = "posting date unknown" if self.days is None else f"{self.days:.0f} d old"
        return (f"Priority P={self.p:.2f} = F {self.f:.2f} (ATS ceiling) x S {self.s:.2f} ({self.s_reason}) x R {self.r:.2f} ({age})")


def weights(settings) -> dict[str, float]:
    given = ((settings.raw.get("priority") or {}).get("sponsor_weights") or {}) if settings is not None else {}
    return {k: float(given.get(k, v)) for k, v in DEFAULT_WEIGHTS.items()}


def recency_days(settings) -> float:
    v = ((settings.raw.get("priority") or {}).get("recency_days") if settings is not None else None) or DEFAULT_RECENCY_DAYS
    return float(v)


def _get(job: Mapping[str, Any], key: str) -> Any:
    return job[key] if key in job.keys() else None            # dicts and sqlite3.Row


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sponsor_factor(job: Mapping[str, Any], settings=None) -> tuple[float, str]:
    """(S, why). The lowest of the applicable weights; every weight is above zero, so a job is only ever moved down."""
    w = weights(settings)
    options: list[tuple[float, str]] = []
    match = str(_get(job, "sponsor_match") or "")
    if match.startswith("exact"):
        options.append((w["exact"], "exact register match"))
    elif match.startswith("fuzzy"):
        options.append((w["fuzzy"], "fuzzy register match"))
    elif match == "none":
        options.append((w["unknown"], "not on the register"))
    else:
        options.append((w["unknown"], "register not checked"))
    if _get(job, "agency"):
        options.append((w["unknown"], "agency"))
    if says_no_sponsorship(str(_get(job, "description") or "")):
        options.append((w["no_sponsorship"], "JD says no sponsorship"))
    return min(options, key=lambda o: o[0])


def recency_factor(job: Mapping[str, Any], settings=None, today: datetime | None = None) -> tuple[float, float | None]:
    posted = parse_date(_get(job, "posted_at"))
    if posted is None:
        return 1.0, None
    days = max(((today or datetime.now(timezone.utc)) - posted).total_seconds() / 86400.0, 0.0)
    return math.exp(-days / recency_days(settings)), days


def ats_factor(job: Mapping[str, Any], tailored: bool = True) -> float:
    c = _get(job, "ceiling") if tailored else None
    c = c if c is not None else _get(job, "ceiling_pre")
    return min(max(float(c), 0.0), 1.0) if c is not None else 0.0


def priority(job: Mapping[str, Any], settings=None, tailored: bool = True, today: datetime | None = None) -> Priority:
    """P = F x S x R for a job row (or dict with the same keys)."""
    f = ats_factor(job, tailored)
    s, why = sponsor_factor(job, settings)
    r, days = recency_factor(job, settings, today)
    return Priority(f * s * r, f, s, r, why, days)


def sort_key(job: Mapping[str, Any], settings=None, tailored: bool = True, today: datetime | None = None) -> tuple:
    """Ascending key: highest P first; ties go to the better S x R (so jobs with no ceiling yet still order by how sure and how
    fresh they are), then to the older id for a stable order."""
    pr = priority(job, settings, tailored, today)
    return (-pr.p, -(pr.s * pr.r), str(_get(job, "id") or ""))


# ------------------------------------------------------------------ jobs that are not in the database yet
def file_job(path, settings, fb: dict, idx) -> dict | None:
    """A job-like dict for an inbox .txt file (its Company / Title / Posted / Agency headers and its text), with the register
    match and the triage-time ceiling filled in, so that `priority()` can rank it. None when the file cannot be read as a JD."""
    from . import gates, manual     # lazy: both import modules that import this one's neighbours

    try:
        text, _ = manual.read_jd(path)
    except manual.JDError:
        return None
    hdr = manual.header_fields(text)
    match = idx.lookup(hdr.get("company", ""), settings["sponsors"].get("fuzzy_threshold", 0.92)).label if idx is not None and hdr.get("company") else None
    try:
        ceiling_pre = gates.pre_ceiling(text, fb, settings)
    except Exception:  # noqa: BLE001 — a file the taxonomy cannot read is ranked last, not refused
        ceiling_pre = None
    return {"id": str(path.name), "company": hdr.get("company", ""), "title": hdr.get("title", ""), "description": text,
            "sponsor_match": match, "agency": (hdr.get("agency") or "").lower() in ("yes", "true", "1"),
            "posted_at": hdr.get("posted"), "ceiling_pre": ceiling_pre}


def order_files(files, settings, db, fb: dict, today: datetime | None = None) -> list[tuple[Any, Priority | None]]:
    """[(path, Priority or None)] best first: highest P, ties to the better S x R, then by file name. A file that cannot be read
    as a JD has no priority and goes last (live then rejects it with its reason). Nothing is dropped."""
    from . import register as sponsors

    idx = sponsors.load_index(db)
    ranked = []
    for f in files:
        job = file_job(f, settings, fb, idx)
        ranked.append((f, priority(job, settings, tailored=False, today=today) if job else None))
    return sorted(ranked, key=lambda fp: (fp[1] is None, -(fp[1].p if fp[1] else 0), -(fp[1].s * fp[1].r if fp[1] else 0), fp[0].name))
