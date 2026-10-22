"""Letter evidence as fields, not sentences (`letter.mode: draft`).

A model handed a finished bullet sentence tends to paste it (the paste check in letter_rules rejects that, then the draft is
retried or lost). Handed fields it has to write its own sentence. Each evidence bullet becomes

    method   the tools and techniques: the bullet's `method` if the fact bank gives one, else the `terms` that the bullet's own
             text names (in the text's spelling, "Docker; AWS Lambda; IAM"). Never the raw `terms` list: it holds index words and
             synonyms ("containerised", "serverless", "security") that a model copies into the letter as a list
    data     what was worked on: the bullet's `data` if the fact bank gives one, else "(not stated)"
    outcome  the stated result: the bullet's `outcome`, else its `result`, else "(not stated)"

Nothing is derived from the bullet's sentence by code and nothing is invented: a field the fact bank does not hold is sent as
"(not stated)" and the prompt tells the model to say nothing about it. Adding `method:` / `data:` / `outcome:` to a bullet in
fact_bank.yaml (your file; this code never writes it) gives the draft more to work with. The letter checks are unchanged and
still read the full bullet text, so a draft is held to exactly the same facts.
"""
from __future__ import annotations

import re

NOT_STATED = "(not stated)"
OPTIONAL_KEYS = ("method", "data", "outcome", "result")      # fact-bank bullet keys letter_evidence copies when present


def named_terms(text: str, terms) -> list[str]:
    """The `terms` that `text` itself names, each as the text spells it, in the order the text gives them, once each."""
    found: list[tuple[int, str]] = []
    for t in terms or []:
        t = " ".join(str(t).split())
        m = re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", text, re.I) if t else None
        if m and all(m.group(0).lower() != f.lower() for _, f in found):
            found.append((m.start(), m.group(0)))
    return [f for _, f in sorted(found)]


def fields(ev: dict) -> dict[str, str]:
    """{"method", "data", "outcome"} for one letter-evidence dict (see letter_evidence)."""
    def clean(v) -> str:
        return " ".join(str(v or "").split())
    method = clean(ev.get("method")) or "; ".join(named_terms(clean(ev.get("text")), ev.get("terms")))
    return {"method": method or NOT_STATED, "data": clean(ev.get("data")) or NOT_STATED,
            "outcome": clean(ev.get("outcome")) or clean(ev.get("result")) or NOT_STATED}


def block(evidence: list[dict], start: int = 1) -> str:
    """The EVIDENCE section of the letter prompt, one numbered item per bullet (numbered from `start`)."""
    out = []
    for i, ev in enumerate(evidence, start):
        f = fields(ev)
        out.append(f"EVIDENCE {i}\n  method: {f['method']}\n  data: {f['data']}\n  outcome: {f['outcome']}")
    return "\n".join(out)
