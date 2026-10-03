"""Normalise LLM text before it is scored, checked or rendered.

Models like typographic characters that an ATS string-match (and our own scorer) trips over: a non-breaking hyphen
inside "end-to-end", curly quotes in "Master's", a no-break space between words. They become their plain ASCII
counterparts. En-dashes (U+2013) are left alone: they belong in date ranges ("Jan 2024 – Mar 2025").
"""
from __future__ import annotations

from typing import Any

_PLAIN = str.maketrans({
    "‐": "-", "‑": "-",                       # hyphen, non-breaking hyphen
    "‘": "'", "’": "'",                       # curly single quotes / apostrophe
    "“": '"', "”": '"',                       # curly double quotes
    " ": " ", " ": " ",                       # no-break space, narrow no-break space
})


def normalise_text(s: str) -> str:
    return s.translate(_PLAIN)


def normalise_json(obj: Any) -> Any:
    """normalise_text on every string in a JSON-like structure (dict keys are left as they are)."""
    if isinstance(obj, str):
        return normalise_text(obj)
    if isinstance(obj, list):
        return [normalise_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: normalise_json(v) for k, v in obj.items()}
    return obj
