"""How strongly the ad asks for a keyword (Phase E2), for the gap report. Each JD keyword gets one of three tiers, read from the ad's own text:

    stated       the ad names it in a sentence with must-have wording ("must have", "required", "essential", "mandatory", "minimum", "you will need" ...)
    structural   not stated that way, but it sits in the ad's Requirements-type section ("Requirements", "What you'll need", "Qualifications", "About you" ...)
                 or the ad mentions it more than once
    inferred     anything else: a single mention in the about-us text or under a nice-to-have heading, or no mention in the ad's words at all. The keyword is
                 in the list because OUR extraction and weighting guessed it matters. An inferred keyword is never shown as the top priority (gaps.py).

Pure text rules: no model, no network. A keyword is looked for by its own words (the term and the alternatives the ad offered), never through the keyword-group
synonyms the honest ceiling uses: a synonym is not what the ad said.
"""
from __future__ import annotations

import re
from typing import Iterable

from ..tailoring.scorer import Keyword, _contains, has_literal, norm_literal, norm_stem

STATED, STRUCTURAL, INFERRED = "stated", "structural", "inferred"
TIERS = (STATED, STRUCTURAL, INFERRED)                     # strongest first: the order the gap report ranks in
MUST = re.compile(r"\b(?:must|required|requires?|requirements?|essential|mandatory|prerequisite|minimum|you(?:'|’)?ll need|you will need|you need|"
                  r"needs? to have|need to have)\b", re.I)
NICE = re.compile(r"\b(?:nice to have|desirable|bonus|a plus|preferred|ideally|advantageous|beneficial|would be (?:great|a plus)|not essential)\b", re.I)
REQUIREMENTS_HEAD = re.compile(r"requirements?|what you(?:'|’)?ll need|what you will need|what we(?:'|’)?re looking for|what we are looking for|"
                               r"qualifications?|essential|must[- ]haves?|about you|your skills|skills (?:and|&) experience|who you are|"
                               r"you (?:have|will have|bring)|what you(?:'|’)?ll bring|what you bring", re.I)
NICE_HEAD = re.compile(r"nice[- ]to[- ]haves?|desirable|bonus|preferred|a plus|advantageous|beneficial", re.I)
MAX_HEADING, MAX_HEADING_WORDS = 60, 5
BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


def _heading(line: str) -> str | None:
    """The heading text when `line` is a heading: short, no sentence end, not a bullet, and either marked (markdown, bold, a colon, capitals) or a few words
    that start with a known section name ("Essential skills", "About you")."""
    raw = line.strip()
    text = raw.strip("#*_:- \t")
    if not text or len(text) > MAX_HEADING or raw.endswith((".", "?", "!", ",", ";")) or BULLET.match(line):
        return None
    if raw.startswith(("#", "**", "__")) or raw.endswith(":") or text.isupper():
        return text
    if len(text.split()) <= MAX_HEADING_WORDS and (REQUIREMENTS_HEAD.match(text) or NICE_HEAD.match(text)):
        return text
    return None


def _mentions(words: list[str]):
    lits, stems = [norm_literal(w) for w in words], [norm_stem(w) for w in words]

    def has(text: str) -> bool:
        lit, stm = norm_literal(text), norm_stem(text)
        return any(has_literal(lit, w) for w in lits) or any(_contains(stm, w) for w in stems)
    return has


def _units(jd: str) -> list[tuple[str, str]]:
    """[(section, sentence)] in reading order. section is `requirements`, `nice` or `other`, set by the last heading."""
    section, out = "other", []
    for line in (jd or "").splitlines():
        head = _heading(line)
        if head is not None:
            section = "requirements" if REQUIREMENTS_HEAD.search(head) and not NICE_HEAD.search(head) else "nice" if NICE_HEAD.search(head) else "other"
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if sentence.strip():
                out.append((section, sentence.strip()))
    return out


def classify(jd: str, keywords: Iterable[Keyword]) -> dict[str, str]:
    """{keyword term: tier} for every keyword (see the module text). An empty or missing ad makes every keyword inferred."""
    units = _units(jd)
    out: dict[str, str] = {}
    for k in keywords:
        has = _mentions(k.members())
        hits = [(sec, s) for sec, s in units if has(s)]
        if any(MUST.search(s) and not NICE.search(s) and sec != "nice" for sec, s in hits):
            out[k.term] = STATED
        elif any(sec == "requirements" and not NICE.search(s) for sec, s in hits) or len(hits) >= 2:
            out[k.term] = STRUCTURAL
        else:
            out[k.term] = INFERRED
    return out
