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
    # any-of: the JD offered these instead ("PyTorch or TensorFlow", llm / llms); a hit if the term or any of them appears
    alts: list[str] = field(default_factory=list)

    def members(self) -> list[str]:
        return [self.term, *self.alts]


@dataclass
class Score:
    literal: float
    stemmed: float
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)          # missing even after stemming
    wording_misses: list[str] = field(default_factory=list)  # present in substance, not literally


def score(text: str, keywords: list[Keyword], synonyms: bool = False) -> Score:
    """`synonyms=True` also accepts the other words of a keyword's group (config/keyword_groups.txt); only the
    honest ceiling uses it, since an ATS looks for what the JD says."""
    if not keywords:
        return Score(0.0, 0.0)
    lit, stm = norm_literal(text), norm_stem(text)
    tot = sum(k.weight for k in keywords)
    s_lit = s_stm = 0.0
    out = Score(0.0, 0.0)
    for k in keywords:
        words = _with_group(k.members()) if synonyms else k.members()
        a = any(_contains(lit, norm_literal(w)) for w in words)
        b = a or any(_contains(stm, norm_stem(w)) for w in words)
        s_lit += k.weight * a
        s_stm += k.weight * b
        (out.hits if a else out.wording_misses if b else out.misses).append(k.term)
    out.literal, out.stemmed = s_lit / tot, s_stm / tot
    return out


def ceiling(fact_text: str, keywords: list[Keyword]) -> tuple[float, list[str]]:
    """Best honest score: weighted share of keywords evidenced anywhere in the fact bank.
    A keyword is a gap only if none of its words (the JD's any-of alternatives and its keyword-group synonyms)
    appears in the fact text, after stemming. Returns (ceiling, genuine_gaps)."""
    s = score(fact_text, keywords, synonyms=True)
    return s.stemmed, s.misses


# ---------------------------------------------------------------- keyword extraction (no LLM)
def _taxonomy_rows() -> list[tuple[str, str]]:
    """(term, category) per taxonomy line; `term | category`, category "" when untagged (a broad skill)."""
    p = ROOT / "config" / "taxonomy.txt"
    rows = []
    for l in p.read_text(encoding="utf-8").splitlines():
        if l.strip() and not l.startswith("#"):
            term, _, cat = l.partition("|")
            rows.append((term.strip(), cat.strip().lower()))
    return rows


def load_taxonomy() -> list[str]:
    return [t for t, _ in _taxonomy_rows()]


# categories a candidate can honestly say they have not used; broad skills (statistics, research, ...) never qualify
GAP_CATEGORIES = frozenset({"tool", "platform", "language", "framework"})


def gap_terms() -> set[str]:
    """norm_literal'd taxonomy terms whose category lets them be named as a gap in the cover letter."""
    return {norm_literal(t) for t, c in _taxonomy_rows() if c in GAP_CATEGORIES}


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
    return merge_keywords(list(found.values()))        # llm / llms / large language models count once


def load_groups() -> list[list[str]]:
    """Wording groups from config/keyword_groups.txt (`a | b | c` per line)."""
    p = ROOT / "config" / "keyword_groups.txt"
    rows = [[w.strip() for w in l.split("|") if w.strip()] for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]
    return [r for r in rows if len(r) > 1]


def _group_of() -> dict[str, int]:
    out: dict[str, int] = {}
    for gi, g in enumerate(load_groups()):
        for w in g:
            out.setdefault(norm_stem(w), gi)
    return out


def _with_group(words: list[str]) -> list[str]:
    """`words` plus every other member of the keyword groups they belong to."""
    groups, of = load_groups(), _group_of()
    out = list(words)
    for w in words:
        gi = of.get(norm_stem(w))
        if gi is not None:
            out += [m for m in groups[gi] if m not in out]
    return out


def merge_keywords(*lists: list[Keyword]) -> list[Keyword]:
    """One keyword per set of words that are the same keyword: equal after stemming, offered as alternatives
    ("A or B"), or in one keyword group (llm / llms). The merged keyword keeps the heaviest member's term and weight
    (first one on a tie), is required if any member was, and lists the other words as `alts` (any-of)."""
    kws = [k for lst in lists for k in lst]
    of = _group_of()
    parent = list(range(len(kws)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[str, int] = {}
    for i, k in enumerate(kws):
        for w in k.members():
            stem = norm_stem(w)
            for key in (stem,) + ((f"\0group{of[stem]}",) if stem in of else ()):
                if key in seen:
                    parent[find(i)] = find(seen[key])
                else:
                    seen[key] = i
    comps: dict[int, list[Keyword]] = {}
    for i, k in enumerate(kws):
        comps.setdefault(find(i), []).append(k)
    out = []
    for comp in comps.values():                          # in order of each keyword's first member
        best = max(comp, key=lambda k: k.weight)
        alts: list[str] = []
        for k in comp:
            for w in k.members():
                if norm_stem(w) != norm_stem(best.term) and all(norm_stem(w) != norm_stem(a) for a in alts):
                    alts.append(w)
        out.append(Keyword(best.term, best.weight, any(k.required for k in comp), alts))
    return out


# Head words that add nothing a recruiter's ATS would search for: "retrieval pipelines" is just "retrieval".
GENERIC_HEADS = frozenset({"pipelines", "systems", "frameworks", "tasks", "approaches", "workflows", "features",
                           "architectures", "loops"})


def _generic_extension(stem: str, host_stems: list[str]) -> str | None:
    """The kept term (as a stem) that `stem` is made of plus generic head words only, else None. Whole words only."""
    heads = {norm_stem(h) for h in GENERIC_HEADS}
    for h in sorted(host_stems, key=len, reverse=True):
        if not h or not _contains(stem, h):
            continue
        rest = re.sub(r"(?<![\w])" + re.escape(h) + r"(?![\w])", " ", stem, count=1).split()
        if rest and all(w in heads for w in rest):
            return h
    return None


def drop_contained(kws: list[Keyword]) -> tuple[list[Keyword], list[str]]:
    """Merge near-duplicates where one keyword's words are wholly inside another's ("customer feedback" inside
    "customer feedback analysis"): the shorter one stays and takes the heavier weight (required if either was). Matched
    on stems, whole words only. An any-of keyword ("A or B") is never dropped and, as a host, only absorbs a longer term
    that is one of its words plus generic head words alone (GENERIC_HEADS: "retrieval pipelines" next to "RAG or
    retrieval"). Returns (keywords in their input order, notes naming each dropped longer keyword and why)."""
    stems = {id(k): norm_stem(k.term) for k in kws}
    kept: list[Keyword] = []
    notes: list[str] = []
    gone: set[int] = set()
    for k in sorted(kws, key=lambda k: (len(stems[id(k)].split()), len(stems[id(k)]))):    # shortest first
        host = None if k.alts else next((h for h in kept if not h.alts and h is not k and stems[id(h)]
                                         and _contains(stems[id(k)], stems[id(h)])), None)
        if host is None and not k.alts:                       # an any-of host: only generic head words may be shed
            for h in kept:
                if h.alts and (found := _generic_extension(stems[id(k)], [norm_stem(m) for m in h.members()])):
                    host = h
                    notes.append(f"dropped '{k.term}' (contains '{found}' plus only generic head words)")
                    break
        elif host is not None:
            notes.append(f"dropped '{k.term}' (contains '{host.term}')")
        if host is None:
            kept.append(k)
            continue
        gone.add(id(k))
        host.weight, host.required = max(host.weight, k.weight), host.required or k.required
    return [k for k in kws if id(k) not in gone], notes


def jd_frequency(k: Keyword, jd: str) -> int:
    """How many times the JD says the keyword (any of its words), whole words, case/quote-normalised."""
    njd = norm_literal(jd)
    return sum(len(re.findall(r"(?<![\w])" + re.escape(norm_literal(w)) + r"(?![\w])", njd)) for w in k.members())


def cap_keywords(kws: list[Keyword], jd: str, limit: int) -> tuple[list[Keyword], list[str]]:
    """The `limit` most important keywords: by weight, then by how often the JD says them, then input order. The result
    is in that order (most important first). Returns (keywords, notes naming what was cut)."""
    freq = {id(k): jd_frequency(k, jd) for k in kws}
    order = {id(k): i for i, k in enumerate(kws)}
    ranked = sorted(kws, key=lambda k: (-k.weight, -freq[id(k)], order[id(k)]))
    cut = ranked[limit:]
    return ranked[:limit], ([f"capped at {limit}; dropped " + ", ".join(k.term for k in cut)] if cut else [])


def in_band(s: float, band: tuple[float, float]) -> str:
    lo, hi = band
    return "below" if s < lo else "above" if s > hi else "in"
