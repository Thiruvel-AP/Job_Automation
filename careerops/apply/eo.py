"""Equal-opportunities answers (ethnicity first) for the apply assist. They are never inferred and never guessed: the assist fills ONLY a
question that applicant.yaml `equal_opportunities` lists, with an option YOU wrote down, and leaves everything else empty.

applicant.yaml:
  equal_opportunities:
    ethnicity:                            # a built-in question (ethnic / race / racial in the label); another key needs `question:`
      answer: "Asian - Indian"            # what you would say (shown for your reference; never typed anywhere)
      match_any:                          # the form options you accept, best first: the first form option equal to the first entry wins,
        - "Asian or Asian British - Indian"   # else the first equal to the second ... Equal ignoring case, dashes and spacing,
        - "Indian"                            # never "contains" ("American Indian or Alaska Native" is not "Indian").
        - "Asian"
      question: ["ethnic"]                # optional: regexes for the question's label (built-in for ethnicity)

If no option matches, the field stays empty. A question that is about equal opportunities (ethnicity, gender, disability, religion, sexual
orientation, veteran status, pronouns ...) but is not listed stays empty too. Every such field, filled or empty, is outlined blue and printed
as `CHECK EO ANSWER [key]: question -> option or EMPTY`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

BUILTIN_QUESTIONS = {"ethnicity": (r"ethnic", r"\brace\b", r"racial")}
ALLOWED_KEYS = ("answer", "match_any", "question")
# any form question that is about equal opportunities, listed or not: all of them are printed, only the listed ones are ever filled
EO_FIELD = re.compile(r"ethnic|\brace\b|racial|gender|\bsex\b|disab|veteran|pronoun|sexual orientation|religio|\bbelief|transgender|marital"
                      r"|equal opportunit|equality monitoring", re.I)
DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−-"), " ")


def norm(text: Any) -> str:
    """Case-, dash- and spacing-insensitive form of an option: "Asian – Indian", "ASIAN-INDIAN" and "Asian  -  Indian" are one."""
    t = unicodedata.normalize("NFKC", str(text or "")).replace(" ", " ").casefold().translate(DASHES)
    return " ".join(t.split()).rstrip("*:").strip()


@dataclass(frozen=True)
class Entry:
    key: str
    answer: str
    match_any: tuple[str, ...]
    question: tuple[re.Pattern[str], ...]

    def asks(self, text: str) -> bool:
        return any(rx.search(text or "") for rx in self.question)


def problems(block: Any) -> list[str]:
    """Everything wrong with an `equal_opportunities` block's structure (empty list = fine). One line each, naming the entry."""
    if block is None:
        return ["equal_opportunities is empty (remove the key, or list a question)"]
    if not isinstance(block, Mapping):
        return ["equal_opportunities must be a mapping of question -> {answer, match_any}"]
    out: list[str] = []
    for key, e in block.items():
        where = f"equal_opportunities.{key}"
        if not isinstance(e, Mapping):
            out.append(f"{where} must be a mapping with match_any (and answer)")
            continue
        for extra in sorted(set(e) - set(ALLOWED_KEYS)):
            out.append(f"{where}: unknown key {extra!r} (allowed: {', '.join(ALLOWED_KEYS)})")
        m = e.get("match_any")
        if not isinstance(m, list) or not m or not all(isinstance(x, str) and norm(x) for x in m):
            out.append(f"{where}.match_any must be a non-empty list of option texts")
        if "answer" in e and not (isinstance(e["answer"], str) and e["answer"].strip()):
            out.append(f"{where}.answer must be a non-empty text")
        q = e.get("question")
        if q is not None:
            bad = _bad_regexes(q)
            if bad:
                out.append(f"{where}.question must be a list of regular expressions ({bad})")
        elif key not in BUILTIN_QUESTIONS:
            out.append(f"{where}: no question to recognise it by; add question: [\"regex of the label\"] (only {', '.join(BUILTIN_QUESTIONS)} is built in)")
    return out


def _bad_regexes(q: Any) -> str:
    if not isinstance(q, list) or not q or not all(isinstance(x, str) and x.strip() for x in q):
        return "a non-empty list of strings"
    for x in q:
        try:
            re.compile(x)
        except re.error as e:
            return f"{x!r}: {e}"
    return ""


def entries(applicant: Mapping[str, Any] | None) -> list[Entry]:
    """The valid entries of applicant.yaml `equal_opportunities` (an invalid one is skipped: doctor reports it, nothing is guessed)."""
    block = (applicant or {}).get("equal_opportunities")
    if not isinstance(block, Mapping):
        return []
    out = []
    for key, e in block.items():
        if problems({key: e}):
            continue
        patterns = e.get("question") or list(BUILTIN_QUESTIONS[key])
        out.append(Entry(str(key), str(e.get("answer") or ""), tuple(str(x) for x in e["match_any"]),
                         tuple(re.compile(p, re.I) for p in patterns)))
    return out


def is_eo_field(text: str) -> bool:
    return bool(EO_FIELD.search(text or ""))


def entry_for(text: str, listed: list[Entry]) -> Entry | None:
    """The listed entry whose question this label is about (the first, in the order of applicant.yaml)."""
    return next((e for e in listed if e.asks(text)), None)


def choose(options: list[str], entry: Entry) -> str | None:
    """The form option to pick: for each `match_any` entry in order, the first option (in form order) that equals it ignoring case,
    dashes and spacing; None when nothing matches."""
    normed = [(norm(o), o) for o in options]
    for want in entry.match_any:
        w = norm(want)
        hit = next((o for n, o in normed if n and n == w), None)
        if hit is not None:
            return hit
    return None
