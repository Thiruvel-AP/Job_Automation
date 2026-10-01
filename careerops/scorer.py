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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


REQ_HEAD = re.compile(r"(requirement|essential|must|you have|you'll have|you will have|what you.ll need|"
                      r"qualifications|what we.re looking for|about you|skills and experience)", re.I)
PREF_HEAD = re.compile(r"(nice to have|desirable|bonus|preferred|a plus|advantageous|ideally)", re.I)


def taxonomy_keywords(jd: str, req_w: float = 2.0, pref_w: float = 1.0) -> list[Keyword]:
    """Scan the JD line by line, tracking whether we are under a 'requirements' or 'nice to have' heading."""
    tax = load_taxonomy()
    mode = "neutral"
    found: dict[str, Keyword] = {}
    for line in jd.splitlines():
        stripped = line.strip()
        if len(stripped) < 80 and not stripped.startswith("-"):
            if PREF_HEAD.search(stripped):
                mode = "pref"
            elif REQ_HEAD.search(stripped):
                mode = "req"
        if PREF_HEAD.search(stripped):
            line_mode = "pref"
        else:
            line_mode = mode
        ln = norm_literal(line)
        for term in tax:
            if _contains(ln, norm_literal(term)):
                w = pref_w if line_mode == "pref" else req_w if line_mode == "req" else (req_w + pref_w) / 2
                prev = found.get(term.lower())
                if prev is None or w > prev.weight:
                    found[term.lower()] = Keyword(term, w, line_mode != "pref")
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
