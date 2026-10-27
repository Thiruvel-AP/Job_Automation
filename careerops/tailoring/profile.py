"""The CV profile, assembled in code from the fact bank. No model writes it.

    profile = the hook's `profile_line` (the user's own library sentence), then the role family's `profile_body`, then further sentences
              of the fact bank's `profile.sentences` (the older `profile_facts` are read too), best keyword overlap first, each a sentence
              that ends with a full stop; 100 to 150 words (Document rules, A2): sentences are added while the profile is under 100 words
              and only those that keep it at or under 150; a text still over 150 is trimmed at a sentence boundary, and a bank with too
              few sentences is a check failure that says how many words are missing and where to add them. Nothing is padded or invented.

Without a word range (`words=None`) the older rule stands: the hook line plus the best one or two facts.

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

PROFILE_FACTS = 2             # profile sentences after the hook line when no word range is given
MIN_WORDS, MAX_WORDS = 100, 150           # scoring.profile_words: the Document rules' profile length
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


def nwords(text: str) -> int:
    return len(str(text).split())


def word_range(settings) -> tuple[int, int]:
    """settings scoring.profile_words: [low, high], whole numbers with 100 <= low <= high <= 150 (the Document rules; the default). The
    range can be narrowed, never widened."""
    raw = settings["scoring"].get("profile_words", [MIN_WORDS, MAX_WORDS])
    ok = (isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in raw)
          and MIN_WORDS <= raw[0] <= raw[1] <= MAX_WORDS)
    if not ok:
        raise ValueError(f"scoring.profile_words must be [low, high], two whole numbers with {MIN_WORDS} <= low <= high <= {MAX_WORDS}, got {raw!r}")
    return int(raw[0]), int(raw[1])


def bank_sentences(fb: dict) -> list[str]:
    """The fact bank's profile sentences: `profile.sentences` first, then the older `profile_facts`; the user's own text, in their order."""
    prof = fb.get("profile")
    own = prof.get("sentences") if isinstance(prof, dict) else None
    return [str(s) for s in [*(own or []), *(fb.get("profile_facts") or [])] if str(s).strip()]


def trim(text: str, high: int = MAX_WORDS) -> str:
    """`text` with whole sentences dropped from its end until it has at most `high` words; the first sentence always stays."""
    parts = sentences(text)
    while len(parts) > 1 and nwords(" ".join(parts)) > high:
        parts.pop()
    return " ".join(parts)


def word_problems(text: str, rng: tuple[int, int] = (MIN_WORDS, MAX_WORDS)) -> list[str]:
    """[] when the profile has between rng[0] and rng[1] words; else the one-line failure naming the shortfall and where to fix it."""
    n, (lo, hi) = nwords(text), rng
    if n < lo:
        return [f"PROFILE {n}/{lo} words: add profile sentences to fact_bank.yaml → profile.sentences"]
    if n > hi:
        cut = "" if len(sentences(text)) > 1 else " and has no sentence to cut at"
        return [f"PROFILE {n}/{hi} words: the profile is over the limit{cut}"]
    return []


def assemble(fb: dict, kws: list[Keyword], jd: str, profile_line: str = "", n: int = PROFILE_FACTS, family_body: str = "",
             words: tuple[int, int] | None = None) -> dict:
    """{"text", "facts", "violations", "warnings", "retried", "fallback", "words", "shortfall"}: `profile_line` (the hook's sentence, may be ""),
    then `family_body` (the role family's profile body, may be ""), then the bank's profile sentences (`bank_sentences`) best keyword score
    first. With `words` = (low, high): sentences are added while the text is under `low` words, skipping any that would pass `high`, and a
    text still over `high` is trimmed at a sentence boundary; `shortfall` is the check failure ("" when it passes). Without `words`: the best
    `n` usable sentences. `facts` are the fact-bank sentences used, `warnings` say what was left out and why."""
    facts = bank_sentences(fb)
    deny = fb.get("deny_terms", [])
    line = " ".join(" ".join(x.split()) for x in (profile_line, family_body) if x and x.strip())
    score = fact_scores(facts, kws, jd) if facts else []
    ranked = sorted(range(len(facts)), key=lambda i: (-score[i], i))                  # best first, the bank's order on a tie
    said, titles = _content(line), role_phrases(line)
    used: list[str] = []
    warnings: list[str] = []
    for i in ranked:
        fact = facts[i]
        if words is None and len(used) >= n:
            break
        if words is not None:
            now = nwords(" ".join([line, *(sentence(f) for f in used)]))
            if now >= words[0]:
                break
            if now + nwords(sentence(fact)) > words[1]:
                continue                                      # too long to fit under the limit; a shorter one may
        content = _content(fact)
        if deny_hits(fact, deny):
            warnings.append(f"profile: left out a fact that names a deny-listed term: '{fact[:50]}'")
        elif set(role_phrases(fact)) & set(titles) or (len(role_phrases(fact)) > 1):
            warnings.append(f"profile: left out a fact that repeats the title: '{fact[:50]}'")
        elif content and len(content & said) / len(content) >= REPEAT_SHARE:
            warnings.append(f"profile: left out a fact that only restates the sentences before it: '{fact[:50]}'")
        else:
            used.append(fact)
            said |= content
            titles += role_phrases(fact)
    text = " ".join([line, *(sentence(f) for f in used)]).strip()
    shortfall = ""
    if words is not None:
        text = trim(text, words[1])
        shortfall = next(iter(word_problems(text, words)), "")
    return {"text": text, "facts": used, "violations": problems(text), "warnings": warnings, "retried": False, "fallback": False,
            "words": nwords(text), "shortfall": shortfall}
