"""Which option of a drop-down the fill may choose for an answer (v2.5, a port of career-ops-hq's `pickComboboxOption`, option-match.mjs @ 20b9fd3).

An exact option (trimmed, spaces collapsed, case-insensitive) wins. Otherwise only a WHOLE-WORD match is accepted, and only when exactly one option has
it: a substring or token-overlap match let "US" pick "Australia" and "Male" pick "Female". Anything else is -1: the field is left for the person (the
filler presses Escape and marks it NEEDS USER). Nothing is guessed.
"""
from __future__ import annotations

import re
from typing import Sequence


def norm(text: object) -> str:
    return " ".join(str(text if text is not None else "").split()).lower()


def pick(options: Sequence[str], value: str) -> int:
    """The index of the option to choose for `value`, or -1."""
    want = norm(value)
    if not want:
        return -1
    texts = [norm(o) for o in options]
    if want in texts:
        return texts.index(want)
    word = re.compile(r"(?:^|[^\w])" + re.escape(want) + r"(?:$|[^\w])")
    hits = [i for i, t in enumerate(texts) if word.search(t)]
    return hits[0] if len(hits) == 1 else -1


def choose(options: Sequence[str], value: str) -> str | None:
    """The option text itself, or None."""
    i = pick(options, value)
    return options[i] if i >= 0 else None
