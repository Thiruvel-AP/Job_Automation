"""The CV profile, assembled in code from the fact bank. No model writes it.

    profile = the hook's `profile_line` (the user's own library sentence) + one or two `profile_facts`, each a sentence that ends
              with a full stop

The facts are chosen by keyword score for the job (`pick_facts`: budget.fact_scores, the fact bank's order on a tie). A fact is left out
when it names a deny-listed term, when it repeats the title (a role phrase such as "Data Scientist" that the profile already says), or
when it only restates the sentences before it (most of its words are already there). Evidence for the rule (17 Oct): a model merged two
sentences into "...validating LLM migration Data Scientist with experience..." with no full stop and the title twice. Without a hook
line the profile is the chosen facts alone. Nothing is invented: the text is the user's own sentences.
"""
from __future__ import annotations

import re

from .budget import fact_scores
from .faithfulness import deny_hits
from .scorer import Keyword, norm_stem

PROFILE_FACTS = 2             # profile_facts sentences after the hook line
REPEAT_SHARE = 0.6            # a fact with this share of its content words already in the profile only restates it
ROLE_PHRASES = re.compile(r"\b(data scientist|ai/ml engineer|ai engineer|ml engineer|machine learning engineer|data engineer|"
                          r"data analyst|research scientist|applied scientist|software engineer)\b", re.I)
_END = (".", "!", "?")
_STOP = frozenset("a an the of to in on at by for from with and or as is are was were be it its this that their his her".split())


def pick_facts(facts: list[str], kws: list[Keyword], have: list[int], n: int = PROFILE_FACTS, jd: str = "") -> list[int]:
    """`have` (fact indexes already chosen) topped up to `n` with the facts that rank highest for the JD: keyword overlap
    plus the JD's company domain (budget.fact_scores; fact-bank order on ties). Code, not model. With nothing in `have`
    these are the `n` most relevant facts, returned in fact-bank order."""
    sc = fact_scores(facts, kws, jd)
    rest = sorted((i for i in range(len(facts)) if i not in have), key=lambda i: (-sc[i], i))
    chosen = (list(have) + rest)[:n]
    return chosen if have else sorted(chosen)


def role_phrases(text: str) -> list[str]:
    """The role titles `text` names, lower case, one per mention ("Data scientist ... Data Scientist" -> two)."""
    return [m.group(0).lower() for m in ROLE_PHRASES.finditer(text)]


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def sentence(fact: str) -> str:
    """A profile fact as one sentence: single-spaced, ending with a full stop (a fact that already ends in punctuation keeps it)."""
    s = " ".join(str(fact).split())
    return s if s.endswith(_END) else s + "."


def _content(text: str) -> set[str]:
    return {w for w in norm_stem(text).split() if len(w) > 2 and w not in _STOP}


def problems(text: str) -> list[str]:
    """What is wrong with an assembled profile: a role title said more than once, a sentence that does not end in punctuation."""
    out: list[str] = []
    seen = role_phrases(text)
    for phrase in dict.fromkeys(seen):
        if seen.count(phrase) > 1:
            out.append(f"the title '{phrase}' is repeated")
    out += [f"sentence does not end in punctuation: '{s}'" for s in sentences(text) if not s.endswith(_END)]
    return out


def assemble(fb: dict, kws: list[Keyword], jd: str, profile_line: str = "", n: int = PROFILE_FACTS) -> dict:
    """{"text", "facts", "violations", "warnings", "retried", "fallback"}: `profile_line` (the hook's sentence, may be "") followed by the
    best `n` usable profile_facts as sentences. `facts` are the fact-bank sentences used, `warnings` say what was left out and why."""
    facts = [str(f) for f in fb.get("profile_facts") or [] if str(f).strip()]
    deny = fb.get("deny_terms", [])
    line = " ".join(profile_line.split())
    score = fact_scores(facts, kws, jd) if facts else []
    ranked = sorted(range(len(facts)), key=lambda i: (-score[i], i))                  # best first, the bank's order on a tie
    said, titles = _content(line), role_phrases(line)
    used: list[str] = []
    warnings: list[str] = []
    for i in ranked:
        if len(used) >= n:
            break
        fact = facts[i]
        words = _content(fact)
        if deny_hits(fact, deny):
            warnings.append(f"profile: left out a fact that names a deny-listed term: '{fact[:50]}'")
        elif set(role_phrases(fact)) & set(titles) or (len(role_phrases(fact)) > 1):
            warnings.append(f"profile: left out a fact that repeats the title: '{fact[:50]}'")
        elif words and len(words & said) / len(words) >= REPEAT_SHARE:
            warnings.append(f"profile: left out a fact that only restates the sentences before it: '{fact[:50]}'")
        else:
            used.append(fact)
            said |= words
            titles += role_phrases(fact)
    text = " ".join([line, *(sentence(f) for f in used)]).strip()
    return {"text": text, "facts": used, "violations": problems(text), "warnings": warnings, "retried": False, "fallback": False}
