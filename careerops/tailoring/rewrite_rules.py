"""Extra rules a reworded CV bullet must pass, on top of `faithfulness.check`. Each only ever rejects more:

  * qualifiers  — a source bullet that says it was team work, a contribution, coursework, an MSc project, a prototype / PoC,
                  a hackathon, or that the candidate assisted / supported / helped, keeps that word (or a form of it) when
                  reworded: "Contributed to a team LoRA fine-tune" must not become "Fine-tuned a LoRA ..."
  * filler      — patterns a small model adds to reach a keyword ("incorporating llm and retrieval capabilities", "live
                  communication demo") are rejected when the source does not already say them
  * added words — a rewording may not contain a content word that is in neither the original bullet nor the ad's synonyms for a
                  term the original already has (a word from the bullet's `terms:` list that the bullet never says is a new claim)
  * casing      — not a rejection: a keyword written into a bullet takes its canonical casing (LLM, GenAI, NLP)
"""
from __future__ import annotations

import re

from .scorer import Keyword, keyword_hits, norm_stem

# (label, what the SOURCE must say to trigger, what the REWORDING must still say). Whole words, case-insensitive.
QUALIFIERS: tuple[tuple[str, re.Pattern, re.Pattern], ...] = tuple(
    (label, re.compile(src, re.I), re.compile(keep, re.I)) for label, src, keep in (
        ("team", r"\bteams?\b", r"\bteams?\b"),
        ("contributed", r"\bcontributed\b", r"\bcontribut(?:ed|ing|es?|ion|ions)\b"),
        ("coursework", r"\bcoursework\b", r"\bcoursework\b"),
        ("MSc", r"\bm\.?sc\b", r"\bm\.?sc\b"),
        ("prototype", r"\bprototyp(?:e|ed)\b", r"\bprototyp(?:e|es|ed|ing)\b"),
        ("proof of concept", r"\bproof of concept\b|\bpoc\b", r"\bproofs? of concept\b|\bpocs?\b"),
        ("hackathon", r"\bhackathons?\b", r"\bhackathons?\b"),
        ("assisted", r"\bassisted\b", r"\bassist(?:ed|ing|s)?\b"),
        ("supported", r"\bsupported\b", r"\bsupport(?:ed|ing|s)\b"),
        ("helped", r"\bhelped\b", r"\bhelp(?:ed|ing|s)?\b"),
    ))


def qualifier_problems(new: str, source: str) -> list[str]:
    """The qualifiers `source` carries that `new` has dropped, as guard violations."""
    return [f"dropped the qualifier '{label}'" for label, in_src, keep in QUALIFIERS
            if in_src.search(source) and not keep.search(new)]


FILLER_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(p, re.I)) for label, p in (
        ("leveraging/incorporating/applying ... capabilities|techniques",
         r"\b(?:leveraging|incorporating|applying)\b[^.;]{0,60}?\b(?:capabilit(?:y|ies)|techniques?)\b"),
        ("using <gerund> with", r"\busing\s+\w+ing\s+with\b"),
        ("<noun> communication demo", r"\b\w+\s+communication\s+demo\b"),
        # ", demonstrating machine learning deployment." / ", showcasing model serving." / ", achieving regression.": a clause
        # that only exists to carry a keyword
        ("trailing participial clause",
         r",\s*(?:demonstrating|showcasing|emphasi[sz]ing|achieving|forming|highlighting|leveraging)\b[^.]*\.?\s*$"),
    ))


def bare_keyword_problems(new: str, source: str, keywords) -> list[str]:
    """A JD keyword the rewording tacks onto the end of a phrase of the source, with no word to join it ("... on a free T4"
    -> "... on a free T4 regression"): the word before it ended a clause in the source and does not in `new`."""
    out = []
    for k in keywords:
        for w in k.members():
            for m in re.finditer(r"(\w+)\s+" + re.escape(w) + r"(?=[\s.,;)]|$)", new, re.I):
                prev = m.group(1)
                if (re.search(re.escape(w), source, re.I) is None
                        and re.search(r"(?<!\w)" + re.escape(prev) + r"\s*(?:[.,;)]|$)", source, re.I)):
                    out.append(f"bare keyword '{w}' tacked on after '{prev}'")
    return list(dict.fromkeys(out))


def filler_problems(new: str, source: str) -> list[str]:
    """Filler patterns `new` adds (present in it, absent from `source`), as guard violations."""
    return [f"filler pattern '{m.group(0)}' ({label})" for label, p in FILLER_PATTERNS
            if (m := p.search(new)) and not p.search(source)]


# ------------------------------------------------------------------ what a rewording may not ADD (round of 17 Oct)
# Words that carry no claim: articles, prepositions, conjunctions, auxiliaries, pronouns. Everything else a rewording writes must come
# from the original bullet (in any inflection) or be the ad's synonym for a term the original already has.
GLUE = frozenset("""a an the and or but nor so yet of to in on at by for from with without into onto over under across between within through
    via per as is are was were be been being it its this that these those which who whom whose what when where while than then also both
    each their his her your using used use including includes included after before during until""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")


PREFIX = 6                  # two stems of six letters or more that start alike are one word: company/companies, deterministic/deterministically


def _squash(stem: str) -> str:
    """A doubled last letter is one: cutting -> cutt -> cut, running -> runn -> run."""
    return stem[:-1] if len(stem) > 3 and stem[-1] == stem[-2] else stem


def _stems(text: str) -> set[str]:
    return {_squash(norm_stem(w)) for w in _WORD.findall(text) if len(w) > 1}


def _known(stem: str, allowed: set[str]) -> bool:
    stem = _squash(stem)
    return stem in allowed or (len(stem) >= PREFIX and any(len(a) >= PREFIX and a[:PREFIX] == stem[:PREFIX] for a in allowed))


def added_word_problems(new: str, text: str, keywords: list[Keyword], result: str = "", offered=()) -> list[str]:
    """The content words of `new` that are in neither the original bullet (`text`, with its `result:`; any inflection of its words
    counts) nor the ad's keyword synonyms for a term the original already has (every spelling of a keyword the original hits), as guard
    violations. Empty when the rewording only says what the bullet says. Evidence: az3 gained "and MLOps practices" from its `terms:`.
    `offered`: words the caller deliberately allows (the pipeline never offers any)."""
    source = f"{text} {result}".strip()
    allowed = _stems(source) | {_squash(norm_stem(w)) for o in offered for w in _WORD.findall(str(o))}
    for k in keywords:
        if keyword_hits(source, [k]):
            for member in k.members():
                allowed |= _stems(member)
    out: list[str] = []
    for w in _WORD.findall(new):
        if len(w) > 1 and w.lower() not in GLUE and not _known(norm_stem(w), allowed) and f"adds the word '{w.lower()}'" not in " ".join(out):
            out.append(f"adds the word '{w.lower()}', which the original bullet does not say")
    return out


# Canonical casing of the keywords a rewording writes in. The fact bank's own spelling of a one-word skill (XGBoost,
# MLflow, PyTorch) is added by `canonical_forms`, so this table holds the ones the bank may not list.
CANONICAL = {w.lower(): w for w in (
    "LLM", "LLMs", "GenAI", "NLP", "RAG", "MLOps", "AWS", "GCP", "SQL", "NoSQL", "API", "APIs", "ETL", "AI", "ML", "MCP",
    "LoRA", "PyTorch", "TensorFlow", "LangChain", "LangGraph", "FastAPI", "PostgreSQL", "MLflow", "GitHub", "XGBoost",
    "CI/CD", "OpenAI", "scikit-learn", "NumPy", "SciPy", "ChromaDB", "Hugging Face")}


def canonical_forms(fb: dict) -> dict[str, str]:
    """{lower case: canonical spelling}: the built-in table plus every single-token skill the fact bank writes with a capital
    after its first letter or in capitals (the bank's spelling wins)."""
    forms = dict(CANONICAL)
    for items in (fb.get("skills") or {}).values():
        for s in items if isinstance(items, list) else []:
            s = str(s).strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9+#.]*", s) and (s.isupper() or any(c.isupper() for c in s[1:])):
                forms[s.lower()] = s
    return forms


def canonical_case(text: str, forms: dict[str, str]) -> str:
    """`text` with every whole-word occurrence of a known keyword in its canonical casing ("llm" -> "LLM")."""
    if not forms:
        return text
    pat = re.compile(r"(?<![\w/.-])(" + "|".join(re.escape(k) for k in sorted(forms, key=len, reverse=True))
                     + r")(?![\w/])", re.I)
    return pat.sub(lambda m: forms[m.group(1).lower()], text)


# ------------------------------------------------------------------ what a rewording must KEEP (fix round 2)
# Everything above rejects what a rewording ADDS. This rejects what it LOSES: a bullet reworded into a weaker or different claim
# ("Deterministic ..." -> "Determined ...", "validated ... surfacing overfitting" -> "validating with ..."). Rejected = the fact-bank
# text is used. Numbers, the words of the bullet's `result:`, and these qualifiers must all survive.
KEEP_QUALIFIERS: tuple[tuple[str, re.Pattern, re.Pattern], ...] = tuple(
    (label, re.compile(src, re.I), re.compile(keep, re.I)) for label, src, keep in (
        ("deterministic", r"\bdeterministic(?:ally)?\b", r"\bdeterministic(?:ally)?\b"),
        ("overfitting", r"\boverfit\w*", r"\boverfit\w*"),
        ("held-out", r"\bheld[- ]out\b", r"\bheld[- ]out\b"),
        ("validated", r"\bvalidated\b", r"\bvalidat(?:ed|es?|ing|ion)\b"),
        ("under", r"\bunder\b", r"\bunder\b|\bbelow\b|\bless than\b|<"),
        ("per", r"\bper\b", r"\bper\b"),
    ))
_APPROX = re.compile(r"(?:~|\b(?:approximately|approx\.?|about|around|roughly|circa|nearly|almost)\s*)[£$€]?\d", re.I)   # a marker before a number
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*+(?:\.\d++)?+(?!(?:st|nd|rd|th)\b)")        # "3rd" is a rank, not a figure
_RESULT_STOP = frozenset("a an the of to and or in on at by for from with as is are was were be it its this that than then".split())


def _digits(text: str) -> set[str]:
    return {m.group(0).replace(",", "").rstrip(",") for m in _NUMBER.finditer(text)}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]+", text.lower()) if len(w) >= 2 and w not in _RESULT_STOP]


def must_keep_problems(new: str, text: str, result: str = "", award: str = "") -> list[str]:
    """What the fact-bank bullet (`text`, with its `result:`) says that the rewording `new` has dropped, as guard violations: any
    number, the approximation mark of an approximate number (~, approximately), the words of the result, and the qualifiers
    deterministic / overfitting / held-out / validated / under / per. Empty when everything survived. The project's `award`
    (shown in its heading, so a bullet must not repeat it) is not asked of the rewording."""
    out: list[str] = []
    if award.strip():
        gone = re.compile(re.escape(" ".join(award.split())), re.I)
        text, result = gone.sub(" ", text), gone.sub(" ", result)
    source = f"{text} {result}".strip()
    kept = _digits(new)
    for n in sorted(_digits(source) - kept, key=lambda x: (len(x), x)):
        out.append(f"dropped the number {n}")
    if _APPROX.search(source) and not _APPROX.search(new):
        out.append("dropped the approximation '~'")
    for label, in_src, keep in KEEP_QUALIFIERS:
        if in_src.search(source) and not keep.search(new):
            out.append(f"dropped '{label}'")
    have = set(norm_stem(new).split())                              # stemmed, hyphens as spaces: "class-imbalanced" has "imbalance"
    for w in dict.fromkeys(_words(result)):
        stem = norm_stem(w)
        if stem not in have and not (len(stem) >= 3 and any(t.startswith(stem) for t in have)):       # cut -> cutting
            out.append(f"dropped the word '{w}' of the result")
    return out
