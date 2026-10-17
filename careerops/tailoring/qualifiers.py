"""Unsupported qualifier words in a cover letter draft, and their removal.

The claim check rejects a draft for a word the evidence does not say ("my specialised work", "integrated", "robust"). A
small model repeats such an adjective even when told its draft was rejected for it (the Monzo dry run: 'integrated', then
'specialised' twice, then the template letter). When the ONLY violations are qualifier words of this list, they are deleted
from the draft instead of spending another model call:

  * the draft's problems must all be `'<word>' (claim)` or `vague filler '<word>'` with <word> in QUALIFIERS; a name, a
    sector or client word, a capability, a number, a noun, a verb or a length problem means nothing is edited
  * removal only: the edit may delete words (and the punctuation, conjunction or a/an around them), never add or reorder
    any; `only_removed` checks it token by token
  * the caller re-runs the FULL claim check on the edited draft and accepts it only if it passes, so no check is loosened
  * a qualifier that is part of a skill name ("deep learning"), sits after a copula ("is robust"), or whose removal leaves a
    dangling article, conjunction or doubled word, is left alone and the draft is rejected as before
"""
from __future__ import annotations

import re
from typing import Callable

from .grounding import CAPABILITY_WORDS
from .scorer import _contains, load_taxonomy, norm_literal

# Adjectives and adverbs that praise the work without naming it. Ordered (the letter prompt lists them) and lower-case.
QUALIFIER_LIST = ("specialised", "specialized", "integrated", "extensive", "extensively", "deep", "deeply", "proven",
                  "seamless", "seamlessly", "robust", "robustly", "strong", "strongly", "solid", "comprehensive",
                  "advanced", "sophisticated", "significant", "significantly", "substantial", "impressive", "powerful",
                  "innovative", "cutting-edge", "scalable", "efficient", "efficiently", "effective", "effectively",
                  "precise", "precisely", "accurate", "accurately", "instantly", "hands-on", "exceptional", "outstanding",
                  "excellent", "rigorous", "rigorously", "highly", "expert", "passionate", "skilled")
QUALIFIERS = frozenset(QUALIFIER_LIST)

_CLAIM = re.compile(r"^'([^']+)' \(claim\)")
_FILLER = re.compile(r"^vague filler '([^']+)'")
_S = "\x00"                                    # where a qualifier was
_ART, _ART_CAP, _CAP = "\x01", "\x02", "\x03"  # an article to re-choose (a/an), the same capitalised, a sentence start to capitalise
_COPULA = re.compile(r"\b(?:is|are|was|were|be|been|being|feel|feels|felt|seem|seems|seemed|remain|remains|become|becomes)"
                     r"\s+(?:\w+ly\s+)?$", re.I)
_AN_ACRONYM_START = "AEFHILMNORSX"             # SQL, R, MCP, NLP, LLM, API, ML, HTML ... are spoken with a vowel
_BROKEN = (re.compile(r"\b(?:a|an|the|my|our|its|their|with|of|in|on|for|to|at|by|from|is|are|was|were|be|been|than|as)"
                      r"\s*[,.;:!?]", re.I),
           re.compile(r"\b(?:a|an|the|my|our|its|their|is|are|was|were|be|been|with|of)\s+(?:and|or)\b", re.I),
           re.compile(r"\b(\w+)\s+\1\b", re.I))


def is_qualifier(word: str) -> bool:
    return word.strip().lower() in QUALIFIERS


def unsupported_qualifiers(text: str, source: str) -> list[str]:
    """The qualifier words (QUALIFIER_LIST) that `text` uses and `source` (the facts it is written from) does not: a profile that
    calls the candidate "expert", "passionate" or "proven" when no fact says so."""
    src, out = norm_literal(source), []
    for w in QUALIFIER_LIST:
        if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", text, re.I) and not _contains(src, norm_literal(w)):
            out.append(w)
    return out


def prompt_list(evidence_text: str = "") -> list[str]:
    """The standing ban for the letter prompt: every qualifier except the ones the evidence itself uses."""
    ev = norm_literal(evidence_text)
    return [w for w in QUALIFIER_LIST if not _contains(ev, norm_literal(w))]


def qualifier_only(problems: list[str]) -> list[str] | None:
    """The distinct qualifier words the problems name when EVERY problem is one of them; else None (nothing to repair)."""
    words: list[str] = []
    for p in problems:
        m = _CLAIM.match(p) or _FILLER.match(p)
        if not m or not is_qualifier(m.group(1)):
            return None
        w = m.group(1).lower()
        if w not in words:
            words.append(w)
    return words or None


def _spoken_an(word: str) -> bool:
    w = word.lower()
    if w.startswith(("hour", "honest", "honour", "heir")):
        return True
    if w.startswith(("uni", "use", "usu", "eu", "one", "once", "ubiq")):
        return False
    if word.isupper() and len(word) <= 5:
        return word[0] in _AN_ACRONYM_START
    return w[0] in "aeiou"


def _delete(paragraph: str, word: str) -> str | None:
    """The paragraph with every occurrence of `word` removed, with the list words, commas and a/an around it tidied; None
    when an article is left with no word to introduce."""
    rx = re.compile(r"(?<![\w-])(?:(?P<art>an?)\s+)?" + re.escape(word) + r"(?![\w-])", re.I)

    def mark(m: re.Match) -> str:
        if m.group("art"):
            return (_ART_CAP if m.group("art")[0].isupper() else _ART) + _S
        return (_CAP if re.search(r"(?:^|[.!?]\s+)$", m.string[:m.start()]) else "") + _S

    p = rx.sub(mark, paragraph)
    p = re.sub(_S + r"\s+(?:and|or)\s+", "", p)                         # "robust and scalable X"  -> "scalable X"
    p = re.sub(_S + r"\s*,\s*", "", p)                                  # "robust, scalable X", "Seamlessly, I ..."
    p = re.sub(r"(?:\s*,)?\s+(?:and|or)\s+" + _S + r"\s*", " ", p)      # "scalable and robust X"  -> "scalable X"
    p = re.sub(r"\s*,\s*" + _S + r"\s*", " ", p)                        # "scalable, robust X"
    p = re.sub(_S + r"\s*", "", p)

    def article(m: re.Match) -> str:
        art = "an" if _spoken_an(m.group(2)) else "a"
        return (art.capitalize() if m.group(1) == _ART_CAP else art) + " " + m.group(2)

    p = re.sub(r"([\x01\x02])\s*([A-Za-z0-9£$~][\w'’-]*)", article, p)
    if _ART in p or _ART_CAP in p:                                       # "a robust." leaves an article with nothing after it
        return None
    p = re.sub(_CAP + r"\s*([a-z])", lambda m: m.group(1).upper(), p)
    p = p.replace(_CAP, "")
    p = re.sub(r"[ \t]{2,}", " ", p)
    p = re.sub(r"\s+([,.;:!?])", r"\1", p)
    p = re.sub(r",\s*,", ",", p)
    return re.sub(r"\(\s+", "(", p).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w'’-]+|[^\w\s]", text.lower()) if t not in ("a", "an")]


def only_removed(original: list[str], edited: list[str]) -> bool:
    """Is `edited` the `original` with words and punctuation deleted (a/an aside)? Never an insertion or a reorder."""
    if len(original) != len(edited):
        return False
    for o, e in zip(original, edited):
        it = iter(_tokens(o))
        if not all(t in it for t in _tokens(e)):
            return False
    return True


def _breakage(text: str) -> int:
    return sum(len(rx.findall(text)) for rx in _BROKEN)


def remove_qualifiers(body: list[str], words: list[str]) -> list[str] | None:
    """The body with each word of `words` deleted, or None when it cannot be done safely: a word not in the body (it is in
    the template closing, say), part of a skill name, after a copula, or an edit that leaves a dangling article or
    conjunction, a doubled word, or anything but a deletion."""
    text = norm_literal(" ".join(body))
    skills = [t for t in (*load_taxonomy(), *CAPABILITY_WORDS)]
    out = list(body)
    for w in words:
        word = re.compile(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", re.I)
        if not any(word.search(p) for p in out):
            return None
        if any(re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", norm_literal(t)) and _contains(text, norm_literal(t))
               for t in skills):
            return None                                                   # "deep learning": the word is the skill
        if any(_COPULA.search(p[:m.start()]) for p in out for m in word.finditer(p)):
            return None                                                   # "is robust": deleting it breaks the sentence
        out = [_delete(p, w) for p in out]
        if None in out:
            return None
    if any(not re.search(r"\w", p) for p in out) or not only_removed(body, out):
        return None
    if any(_breakage(new) > _breakage(old) for old, new in zip(body, out)):
        return None
    return out


def repair(body: list[str], problems: list[str], recheck: Callable[[list[str]], list[str]]
           ) -> tuple[list[str], list[str]] | None:
    """(edited body, words removed) when the draft's only problems are unsupported qualifier words, they could be deleted
    and the FULL check (`recheck`: body -> problems) passes on what is left; else None and the draft is rejected as before."""
    words = qualifier_only(problems)
    if not words:
        return None
    edited = remove_qualifiers(body, words)
    if edited is None or recheck(edited):
        return None
    return edited, words
