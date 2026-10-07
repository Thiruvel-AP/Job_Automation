"""Extra rules a reworded CV bullet must pass, on top of `faithfulness.check`. Each only ever rejects more:

  * qualifiers  — a source bullet that says it was team work, a contribution, coursework, an MSc project, a prototype / PoC,
                  a hackathon, or that the candidate assisted / supported / helped, keeps that word (or a form of it) when
                  reworded: "Contributed to a team LoRA fine-tune" must not become "Fine-tuned a LoRA ..."
  * filler      — patterns a small model adds to reach a keyword ("incorporating llm and retrieval capabilities", "live
                  communication demo") are rejected when the source does not already say them
  * casing      — not a rejection: a keyword written into a bullet takes its canonical casing (LLM, GenAI, NLP)
"""
from __future__ import annotations

import re

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
