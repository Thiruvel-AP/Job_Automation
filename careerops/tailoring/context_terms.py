"""Context terms: the names of teams, product areas and organisational units a JD mentions (Growth, Borrowing, Fincrime,
Payments, Wealth, Core, "product squad", "cross-functional squads", "data science teams").

They are not skills. An ATS does not look for them in a CV, and the candidate cannot "have" them, so scoring them lowers
the ATS score and the honest ceiling for nothing. They are taken out of the keyword list before scoring (out of the
numerator AND the denominator) and shown separately in review.

Four signals, all deterministic and all checked against the JD:
  * an organisational noun as the last word of the term (squad, team, collective, guild, tribe, department ...)
  * a generic head word as the last word ("data discipline", "engineering culture", "growth mindset", "business
    perspective", "analytical thinking", "can-do attitude", "central data function"), or "decision making" /
    "decision-making" as its last words ("data-driven decision making"): it names how a company is organised or what it
    values, not something a CV can evidence
  * a name in a list of three or more capitalised names in the JD ("... lie in Borrowing, Growth, Payments, Fincrime,
    Wealth, or Core"), unless it is a taxonomy skill
  * a term the model listed under `context_terms` (copied verbatim from the JD), unless it is a taxonomy skill, or the
    team's own name
A taxonomy term (Python, Looker, SQL ...) is never context, whatever the model says.
"""
from __future__ import annotations

import re

from .scorer import Keyword, load_taxonomy, norm_literal

ORG_HEADS = frozenset("""squad squads team teams collective collectives guild guilds tribe tribes chapter chapters pod pods
    department departments division divisions organisation organisations organization organizations""".split())
# Last words that make a phrase about the company, not a skill: "data discipline", "analytics culture", "growth mindset",
# "business perspective", "analytical thinking", "positive attitude".
FRAMING_HEADS = frozenset("""discipline disciplines culture cultures mindset mindsets ethos perspective perspectives thinking
    attitude attitudes""".split())
# Last words of more than one word: "decision making" names a trait, not a method ("decision trees" and "decision making
# under uncertainty" are not caught: only the phrase at the END of the term counts).
FRAMING_TAILS = frozenset({"decision making", "decision-making"})
# "function" is ambiguous: "loss function" and "window functions" are skills, "data function" is a business unit. It frames
# only after a word that names a unit of the company.
UNIT_WORDS = frozenset("""data analytics business product engineering finance risk central technical support platform
    operations commercial""".split())
# three or more capitalised names separated by commas, the last one possibly after "or" / "and" / "&"
_NAME_LIST = re.compile(r"(?<![\w'’])[A-Z][a-z]{2,}(?:\s*,\s*[A-Z][a-z]{2,}){2,}(?:\s*,?\s*(?:or|and|&)\s+[A-Z][a-z]{2,})?")
_NAME = re.compile(r"[A-Z][a-z]{2,}")


def listed_names(jd: str) -> set[str]:
    """Lower-case names in capitalised lists of three or more in the JD."""
    return {n.lower() for m in _NAME_LIST.finditer(jd or "") for n in _NAME.findall(m.group(0))
            if n.lower() not in {"or", "and"}}


def model_terms(raw, jd: str) -> set[str]:
    """The model's `context_terms` that really are in the JD (norm_literal'd)."""
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    njd = norm_literal(jd or "")
    return {norm_literal(str(t)) for t in items if str(t).strip() and norm_literal(str(t)) in njd}


def why_context(k: Keyword, names: set[str], from_model: set[str], team: str, taxonomy: set[str]) -> str | None:
    """Why this keyword is a context term, or None when it is a skill. Taxonomy terms are never context."""
    members = [norm_literal(m) for m in k.members()]
    if any(m in taxonomy for m in members):
        return None
    term = members[0]
    words = term.split()
    if words and words[-1] in ORG_HEADS:
        return f"organisational noun '{words[-1]}'"
    if words and words[-1] in FRAMING_HEADS:
        return f"generic head word '{words[-1]}' (how the company works, not a skill)"
    tail = " ".join(words[-2:])
    if words and (words[-1] in FRAMING_TAILS or tail in FRAMING_TAILS):
        return f"generic head phrase '{words[-1] if words[-1] in FRAMING_TAILS else tail}' (how the company works, not a skill)"
    if len(words) > 1 and words[-1] in ("function", "functions") and words[-2] in UNIT_WORDS:
        return f"business unit '{words[-2]} {words[-1]}', not a skill"
    if any(m in names for m in members):
        return "a team / product-area name listed in the JD"
    if team and term == norm_literal(team):
        return "the team's own name"
    if any(m in from_model for m in members):
        return "named as context by the model"
    return None


def split_context(kws: list[Keyword], jd: str, model: set[str] = frozenset(), team: str = "",
                  ) -> tuple[list[Keyword], list[dict]]:
    """(scored keywords, context terms as [{"term", "weight", "required", "why"}]); both keep the input order."""
    names, taxonomy = listed_names(jd), {norm_literal(t) for t in load_taxonomy()}
    kept: list[Keyword] = []
    context: list[dict] = []
    for k in kws:
        why = why_context(k, names, set(model), team, taxonomy)
        if why:
            context.append({"term": k.term, "weight": k.weight, "required": k.required, "why": why})
        else:
            kept.append(k)
    return kept, context
