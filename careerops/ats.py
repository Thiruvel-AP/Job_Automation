"""Negation-aware ATS scoring, and the three numbers a job is judged by.

    score = sum(w x hit) / sum(w)        over the JD keywords (required 2, preferred 1), as in tailoring/scorer.py

A keyword is NOT a hit where it is only mentioned in a negated or tentative way: one of NEGATORS (not, no, never, haven't, have
not, yet to, without, lack, gap, learning) stands within WINDOW (6) tokens before the term in the same sentence ("I have not yet
used LightGBM", "my forecasting has been on price series, not retail demand", "currently learning dbt"). A keyword counts when at
least one of its mentions is not negated and none is: a text that says "I have not yet used LightGBM" and then "XGBoost shares
LightGBM's boosting ideas" is bridging a gap, not claiming the tool. This is what an honest reader sees; it stops a letter's admitted gap from raising the
score (the Anaplan letter B read 82.5 when it only said it had not used LightGBM, and 75.0 once those mentions are ignored).
"learning" is a negator only as a verb ("learning dbt"): "machine learning", "deep learning", "transfer learning" and
"learning rate" are the skill, not the word.

Three numbers, all in percent (0-100):
    strict    the CV's own text only, negation-aware. The one that is gated.
    ceiling   the claimable ceiling: the weight share of the JD keywords the fact bank backs, leaving out deny-listed terms.
              It is what an honest CV could reach.
    combined  CV and letter together: a keyword counts when the CV or the letter has it, each read negation-aware on its own
              (a gap the letter admits never takes away what the CV shows). Reported; never gated.
The letter alone is reported too (never gated).

Gate on `strict` against the band (settings scoring.band, 70-85):
    below 70 with ceiling >= 70   retry   (tailor once more, the ranking told which keywords the bank evidences)
    below 70 with ceiling <  70   stretch (the job is kept and labelled; a ceiling that low is the fact bank's, not the CV's)
    above 85                      above   (de-stuff: the existing loop reverts rewordings until back in band)
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable

from .tailoring.faithfulness import deny_hits
from .tailoring.scorer import Keyword, Score, _contains, _inflections, _with_group, norm_literal, norm_stem

WINDOW = 6                                    # tokens before the term that are looked at
NEGATORS = frozenset({"not", "no", "never", "haven't", "without", "lack", "lacks", "lacking", "lacked", "gap", "gaps",
                      "learning"})            # "have not" is covered by "not"; "yet to" is a pair, see `_negated_at`
# "learning" is the skill, not the verb, after these ("machine learning") and before these ("learning rate")
_SKILL_BEFORE = frozenset("""machine deep transfer reinforcement supervised unsupervised semi-supervised self-supervised federated
    statistical active online representation contrastive ensemble meta lifelong multi-task few-shot zero-shot metric e""".split())
_SKILL_AFTER = frozenset("""rate rates curve curves algorithm algorithms model models framework frameworks task tasks problem
    problems method methods technique techniques pipeline pipelines approach approaches system systems objective objectives""".split())
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9'+#/&-]|\.(?=[a-z0-9]))*")
# where one sentence ends: . ! ? ; a bullet mark or a blank line (a wrapped line, and a colon, stay inside their sentence)
_BOUNDARY = re.compile(r"(?<=[.!?;])\s+|\s*[•·▪●]\s*|\n\s*\n|\n(?=\s*[-*]\s)")


def sentences(text: str) -> list[str]:
    """The text as sentences, each norm_literal'd (lower case, straight quotes, one space)."""
    return [s for s in (norm_literal(x) for x in _BOUNDARY.split(text or "")) if s]


def _tokens(sentence: str) -> tuple[list[str], list[int]]:
    found = list(_TOKEN.finditer(sentence))
    return [m.group(0) for m in found], [m.start() for m in found]


def _is_negator(toks: list[str], g: int) -> bool:
    t = toks[g]
    if t == "learning":
        before = toks[g - 1] if g > 0 else ""
        after = toks[g + 1] if g + 1 < len(toks) else ""
        return before not in _SKILL_BEFORE and after not in _SKILL_AFTER
    return t in NEGATORS or (t == "yet" and g + 1 < len(toks) and toks[g + 1] == "to")


_TURNS = frozenset({"but", "however", "although", "though", "whereas"})      # "I have not used dbt yet, BUT SQL is ...": a new clause


def _negated_at(toks: list[str], i: int) -> bool:
    """Is the token at index i preceded within WINDOW tokens (in its sentence, in its clause) by a negator? A "but" between the
    negator and the term starts a new clause: the term after it is not what was denied."""
    return any(_is_negator(toks, g) and not _TURNS & set(toks[g + 1:i]) for g in range(max(0, i - WINDOW), i))


def has_negator(sentence: str) -> bool:
    toks, _ = _tokens(sentence)
    return any(_is_negator(toks, g) for g in range(len(toks)))


def _alt(word: str) -> re.Pattern:
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(f) for f in _inflections(word)) + r")(?![\w])")


def mentions(sents: list[str], word: str) -> tuple[int, int]:
    """(mentions, mentions that are not negated) of `word` (norm_literal'd; its plural or singular counts) in the sentences."""
    rx, total, clean = _alt(word), 0, 0
    for s in sents:
        toks, starts = _tokens(s)
        for m in rx.finditer(s):
            total += 1
            clean += not _negated_at(toks, bisect_right(starts, m.start()) - 1)
    return total, clean


def score(text: str, keywords: list[Keyword], synonyms: bool = False) -> Score:
    """tailoring.scorer.score with negation: a keyword is a literal hit when one of its words is mentioned outside a negated
    window and no mention of it in the text is negated; it is a wording miss (the stem is there, not the JD's own form) when only
    the stemmed text says it, in a sentence that holds no negator; `negated` lists the keywords the text says it lacks. `synonyms` as in scorer.score."""
    if not keywords:
        return Score(0.0, 0.0)
    sents = sentences(text)
    stems = [norm_stem(s) for s in sents]
    clean_stems = [st for s, st in zip(sents, stems) if not has_negator(s)]
    tot = sum(k.weight for k in keywords)
    s_lit = s_stm = 0.0
    out = Score(0.0, 0.0)
    for k in keywords:
        words = _with_group(k.members()) if synonyms else k.members()
        seen = clean = 0
        for w in words:
            n, c = mentions(sents, norm_literal(w))
            seen, clean = seen + n, clean + c
        if seen > clean:                         # the text says it lacks the term somewhere: it does not claim it anywhere
            out.misses.append(k.term)
            out.negated.append(k.term)
            continue
        a = clean > 0
        b = a or any(_contains(st, norm_stem(w)) for w in words for st in clean_stems)
        s_lit += k.weight * a
        s_stm += k.weight * b
        (out.hits if a else out.wording_misses if b else out.misses).append(k.term)
    out.literal, out.stemmed = s_lit / tot, s_stm / tot
    return out


def claimable_terms(kws: list[Keyword], gaps: Iterable[str], deny: Iterable[str] = ()) -> list[str]:
    """The keywords the fact bank backs: not a gap (no evidence, or only a neighbour term) and not deny-listed (a tool the user
    has confirmed they lack)."""
    gone = {g.lower() for g in gaps}
    deny = list(deny)
    return [k.term for k in kws if k.term.lower() not in gone and not deny_hits(k.term, deny)]


def claimable_ceiling(kws: list[Keyword], claimable: Iterable[str]) -> float:
    """The weight share (percent) of the keywords in `claimable`."""
    keep = {c.lower() for c in claimable}
    tot = sum(k.weight for k in kws)
    return 100.0 * sum(k.weight for k in kws if k.term.lower() in keep) / tot if tot else 0.0


def gate(strict: float, ceiling: float, band: tuple[float, float] = (70.0, 85.0)) -> str:
    """"ok" in band; "above" over it (de-stuff); "retry" below it when the ceiling could reach it; "stretch" below it when the
    ceiling cannot (the job is kept and labelled, never dropped)."""
    lo, hi = band
    if strict > hi:
        return "above"
    if strict >= lo:
        return "ok"
    return "retry" if ceiling >= lo else "stretch"


@dataclass
class AtsScores:
    strict: float                                  # percent, the CV alone, negation-aware (gated)
    ceiling: float                                 # percent, the claimable ceiling
    combined: float | None = None                  # percent, CV + letter (reported)
    letter: float | None = None                    # percent, the letter alone (reported, never gated)
    gate: str = "ok"                               # ok | above | retry | stretch
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    negated: list[str] = field(default_factory=list)       # in the CV, but only as "not ..."
    letter_negated: list[str] = field(default_factory=list)   # in the letter, but only as "not ..." (its admitted gap)

    def line(self) -> str:
        parts = [f"strict CV {self.strict:.1f}%", f"claimable ceiling {self.ceiling:.1f}%"]
        if self.combined is not None:
            parts.append(f"CV + letter {self.combined:.1f}%")
        if self.letter is not None:
            parts.append(f"letter alone {self.letter:.1f}% (not gated)")
        return "ATS " + " | ".join(parts) + (f" | {self.gate.upper()}" if self.gate in ("stretch", "retry", "above") else "")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def scores(cv_text: str, kws: list[Keyword], claimable: Iterable[str], letter_text: str | None = None,
           band: tuple[float, float] = (70.0, 85.0)) -> AtsScores:
    """The three numbers (and the letter alone) for a CV text and an optional letter text."""
    cv = score(cv_text, kws)
    ceil = claimable_ceiling(kws, claimable)
    out = AtsScores(100.0 * cv.literal, ceil, hits=list(cv.hits), misses=list(cv.misses) + list(cv.wording_misses),
                    negated=list(cv.negated))
    if letter_text is not None:
        alone = score(letter_text, kws)
        out.letter, out.letter_negated = 100.0 * alone.literal, list(alone.negated)
        got = set(cv.hits) | set(alone.hits)             # a gap the letter admits never takes away what the CV itself shows
        tot = sum(k.weight for k in kws)
        out.combined = 100.0 * sum(k.weight for k in kws if k.term in got) / tot if tot else 0.0
    out.gate = gate(out.strict, ceil, band)
    return out
