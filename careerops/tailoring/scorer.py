"""Deterministic ATS-style scoring.

score = Σ_k w_k · 1[k ∈ text] / Σ_k w_k   over JD keywords k with weights w_k (required 2, preferred 1)

Two views:
  literal  — case/quote/line-wrap-hyphen normalised substring match. Conservative; this is what a naive
             ATS sees and what the 70–85 band is applied to.
  stemmed  — also equates hyphen/space, British/American spelling and common suffixes
             ("communicating" ~ "communicate"). The gap literal→stemmed is wording, not substance.
ceiling  — the stemmed score the *fact bank itself* would get: the best an honest CV can reach.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from ..config import ROOT


def norm_literal(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("ﬁ", "fi").replace("ﬂ", "fl")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)       # pdftotext line-wrap hyphenation: "scal-\nable"
    t = t.lower()
    return re.sub(r"\s+", " ", t)


_SUFFIX = re.compile(r"(isations?|izations?|ising|izing|ised|ized|ise|ize|ations?|ing|ed|es|s|ive|ly|ment)$")


def _stem(tok: str) -> str:
    if len(tok) <= 4 or not tok.isalpha():
        return tok
    tok = tok.replace("our", "or") if tok.endswith(("our", "ours", "ourite")) else tok
    s = _SUFFIX.sub("", tok)
    if s != tok and s.endswith("ll"):
        s = s[:-1]                                   # British doubling: modelling → model, travelled → travel
    s = s[:-1] if s.endswith("e") and len(s) > 4 else s
    return s if len(s) >= 3 else tok


def norm_stem(text: str) -> str:
    t = norm_literal(text)
    t = re.sub(r"[-/_]", " ", t)
    t = re.sub(r"[^\w+#. ]", " ", t)
    return " ".join(_stem(w) for w in t.split())


def _contains(hay: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", hay) is not None


@dataclass
class Keyword:
    term: str
    weight: float
    required: bool


@dataclass
class Score:
    literal: float
    stemmed: float
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)          # missing even after stemming
    wording_misses: list[str] = field(default_factory=list)  # present in substance, not literally


def score(text: str, keywords: list[Keyword]) -> Score:
    if not keywords:
        return Score(0.0, 0.0)
    lit, stm = norm_literal(text), norm_stem(text)
    tot = sum(k.weight for k in keywords)
    s_lit = s_stm = 0.0
    out = Score(0.0, 0.0)
    for k in keywords:
        a = _contains(lit, norm_literal(k.term))
        b = a or _contains(stm, norm_stem(k.term))
        s_lit += k.weight * a
        s_stm += k.weight * b
        (out.hits if a else out.wording_misses if b else out.misses).append(k.term)
    out.literal, out.stemmed = s_lit / tot, s_stm / tot
    return out


def ceiling(fact_text: str, keywords: list[Keyword]) -> tuple[float, list[str]]:
    """Best honest score: weighted share of keywords evidenced anywhere in the fact bank.
    Returns (ceiling, genuine_gaps)."""
    s = score(fact_text, keywords)
    return s.stemmed, s.misses


# ---------------------------------------------------------------- keyword extraction (no LLM)
def load_taxonomy() -> list[str]:
    p = ROOT / "config" / "taxonomy.txt"
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


# Section headings, matched on norm_literal text (lower-case, straight apostrophes). First kind that matches wins:
# skip before pref so "Perks & bonus" is not a nice-to-have section.
HEADING_MAX = 60
SECTION_PHRASES: list[tuple[str, str]] = [
    ("skip", r"benefits|perks|our perks|why join|life at|(?:our )?(?:hiring|interview) process|our values|diversity|"
             r"equal opportunit|employment terms|about (?!(?:the )?(?:role|job|position|team)\b|you)"),
    ("pref", r"nice to have|desirable|bonus|preferred|a plus|advantageous|ideally"),
    ("req", r"requirements?|essential|must[- ]have|qualifications|what you(?:'ll| will)? (?:need|bring)|"
            r"what we(?:'re| are) looking for|who you are|about you|your (?:skills|experience|background)|"
            r"skills and experience"),
    ("neutral", r"what you(?:'ll| will) do|responsibilities|the role|about the (?:role|job)"),
]
_ANCHORED = [(kind, re.compile(r"^(?:" + p + ")")) for kind, p in SECTION_PHRASES]
_ANYWHERE = [(kind, re.compile(r"\b(?:" + p + ")")) for kind, p in SECTION_PHRASES]
PREF_HEAD = _ANYWHERE[1][1]

_BULLET = ("-", "•", "*", "·", "–", "—", "▪", "●", "◦")
_DECOR_LEAD = re.compile(r"^[^\w]+")
_DECOR_TAIL = re.compile(r"[^\w:.]+$")                  # keep ':' (heading marker) and '.' (sentence marker)
_HASHTAGS = re.compile(r"^\s*(?:#\w+\s*)+$")
# Clause boundaries for "X; Y is a plus" — split before also/ideally, but not before a trailing "… is a bonus".
_CLAUSE = re.compile(r";|—|\s[–-]\s|\(|\s(?=(?:also|ideally)\b)", re.I)


def classify_heading(line: str) -> str | None:
    """'req' | 'pref' | 'neutral' | 'skip' if the line is a section heading, else None.
    A heading is ≤ HEADING_MAX chars after stripping emoji/decoration, is not a bullet, does not end with '.',
    and either ends with ':' or starts with a known heading phrase. Unknown ':' headings are neutral."""
    raw = line.strip()
    if not raw or raw.startswith(_BULLET):
        return None
    text = norm_literal(_DECOR_TAIL.sub("", _DECOR_LEAD.sub("", raw))).strip()
    if not text or len(text) > HEADING_MAX or text.endswith("."):
        return None
    if text.endswith(":"):
        return next((kind for kind, rx in _ANYWHERE if rx.search(text)), "neutral")
    return next((kind for kind, rx in _ANCHORED if rx.match(text)), None)


def _clauses(line: str, mode: str) -> list[tuple[str, str]]:
    """(clause, mode) pairs. Under req/neutral, only the clause holding a 'nice to have' phrase becomes pref."""
    if mode == "pref" or not PREF_HEAD.search(norm_literal(line)):
        return [(line, mode)]
    return [(c, "pref" if PREF_HEAD.search(norm_literal(c)) else mode) for c in _CLAUSE.split(line)]


def taxonomy_keywords(jd: str, req_w: float = 2.0, pref_w: float = 1.0) -> list[Keyword]:
    """Scan the JD line by line, tracking the current section (req / pref / neutral / skip) from its headings.
    Keywords in skip sections (benefits, perks, company blurb, hiring process) and hashtag lines are ignored."""
    tax = load_taxonomy()
    weight = {"req": req_w, "pref": pref_w, "neutral": (req_w + pref_w) / 2}
    mode = "neutral"
    found: dict[str, Keyword] = {}
    for line in jd.splitlines():
        kind = classify_heading(line)
        if kind:
            mode = kind
        if mode == "skip" or _HASHTAGS.match(line):
            continue
        for clause, clause_mode in _clauses(line, mode):
            ln = norm_literal(clause)
            for term in tax:
                if _contains(ln, norm_literal(term)):
                    w = weight[clause_mode]
                    prev = found.get(term.lower())
                    if prev is None or w > prev.weight:
                        found[term.lower()] = Keyword(term, w, clause_mode != "pref")
    return list(found.values())


def merge_keywords(*lists: list[Keyword]) -> list[Keyword]:
    out: dict[str, Keyword] = {}
    for lst in lists:
        for k in lst:
            key = norm_stem(k.term)
            if key not in out or k.weight > out[key].weight:
                out[key] = k
    return list(out.values())


def in_band(s: float, band: tuple[float, float]) -> str:
    lo, hi = band
    return "below" if s < lo else "above" if s > hi else "in"
