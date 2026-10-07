"""The CV's skills section: `skills.core` is pinned (first row, the user's order, never dropped), skill names appear once
across all rows, and the other groups are ordered by the JD. Also the fact-bank checks `careerops doctor` runs on `core`."""
from __future__ import annotations

from .faithfulness import deny_hits
from .scorer import Keyword, has_literal, norm_literal
from .selection import _hits

CORE_KEY = "core"            # the fact-bank key the user maintains
CORE_LABEL = "Core"          # what the row is called on the CV


def core_skills(fb: dict) -> list[str]:
    return [str(s) for s in ((fb.get("skills") or {}).get(CORE_KEY) or [])]


def dedupe_skills(skills: dict[str, list[str]]) -> dict[str, list[str]]:
    """The skill groups without case-insensitive repeats, within a group and across groups: the first occurrence stays
    ("Python" in Languages keeps it; "python" in Tools goes). A group left empty is dropped."""
    seen: set[str] = set()
    out: dict[str, list[str]] = {}
    for group, items in skills.items():
        kept = []
        for s in items:
            key = " ".join(str(s).split()).casefold()
            if key not in seen:
                seen.add(key)
                kept.append(s)
        if kept:
            out[group] = kept
    return out


def core_deny_problems(fb: dict) -> list[str]:
    deny = fb.get("deny_terms", [])
    return [f"core skill '{s}' contains deny-listed '{d}'" for s in core_skills(fb) for d in deny_hits(s, deny)]


def _bank_text_without_core(fb: dict) -> str:
    parts = list(fb.get("profile_facts", []))
    for g, items in (fb.get("skills") or {}).items():
        if g != CORE_KEY:
            parts += [str(i) for i in items]
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            parts += [e.get("title", ""), e.get("org", ""), e.get("name", ""), e.get("stack", "")]
            for b in e["bullets"]:
                parts += [b["text"], b.get("result", ""), *b.get("terms", [])]
    return "\n".join(str(p) for p in parts if p)


def core_skill_problems(fb: dict) -> list[str]:
    """What is wrong with `skills.core`, one message naming each skill: on the deny list, or mentioned nowhere else in
    the fact bank (other skill rows, bullets and their terms, project stacks, profile facts) so nothing evidences it.
    Matching is the scorer's literal view (`has_literal`): singular and plural are one term in both directions, so "llm"
    evidences "LLMs" and "api" evidences "APIs"; the suffix stemmer is not used, it would let "plots" evidence "Plotly"."""
    problems = core_deny_problems(fb)
    lit = norm_literal(_bank_text_without_core(fb))
    for s in core_skills(fb):
        if not has_literal(lit, norm_literal(s)):               # "Plotly" is not evidenced by a bank that says "plots"
            problems.append(f"core skill '{s}' appears nowhere else in the fact bank (no bullet, term or skill row evidences it)")
    return problems


def cv_skills(fb: dict, kws: list[Keyword], max_groups: int | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """(rows, dropped groups). `core` is the first row ("Core"), exactly the user's list and order, never shortened except
    for the case-insensitive repeats of a name it already shows. Every other group follows in fact-bank order with the
    skills the JD keywords match first (every skill of a shown group is listed, no per-group cap); with more than
    `max_groups` of them the groups the JD matches least are left off (the later group on a tie). Raises ValueError for a
    core skill on the deny list: it would reach the page unchecked."""
    problems = core_deny_problems(fb)
    if problems:
        raise ValueError("; ".join(problems) + " — remove it from skills.core or from deny_terms (`careerops doctor`)")
    skills = fb.get("skills") or {}
    rows = {CORE_LABEL: core_skills(fb)} if core_skills(fb) else {}
    rows.update({g: list(items) for g, items in skills.items() if g != CORE_KEY})
    rows = dedupe_skills(rows)
    weight = [k.weight for k in kws]

    def relevant(skill: str) -> frozenset[int]:
        return _hits(str(skill), kws)

    groups = [g for g in rows if g != CORE_LABEL]
    dropped: list[str] = []
    if max_groups is not None and len(groups) > max_groups:
        rel = {g: sum(weight[i] for i in set().union(*(relevant(s) for s in rows[g]))) for g in groups}
        keep = sorted(sorted(groups, key=lambda g: (-rel[g], groups.index(g)))[:max_groups], key=groups.index)
        dropped = [g for g in groups if g not in keep]
    out: dict[str, list[str]] = {}
    for g, items in rows.items():
        if g in dropped:
            continue
        out[g] = list(items) if g == CORE_LABEL else sorted(items, key=lambda s: not relevant(s))   # stable: bank order
    return out, dropped
