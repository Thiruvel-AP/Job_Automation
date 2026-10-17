"""The CV's skills section: `skills.core` is pinned (first row, the user's order, never dropped), skill names appear once
across all rows, and the other groups are ordered by the JD. Also the fact-bank checks `careerops doctor` runs on `core`."""
from __future__ import annotations

import re

from .faithfulness import deny_hits
from .scorer import Keyword, has_literal, norm_literal
from .selection import _hits

CORE_KEY = "core"            # the fact-bank key the user maintains
CORE_LABEL = "Core"          # what the row is called on the CV
_WANTS_DATABASES = re.compile(r"\b\w*sql\b|\bdatabases?\b|\brdbms\b", re.I)   # SQL, PostgreSQL, NoSQL, database(s) in a JD


def wants_databases(jd: str) -> bool:
    """Does the JD mention SQL or databases? Then the Databases row is on the CV."""
    return bool(_WANTS_DATABASES.search(jd or ""))


def is_database_row(group: str) -> bool:
    return "database" in group.casefold()


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


def _key(skill) -> str:
    return " ".join(str(skill).split()).casefold()


def skill_weight(skill: str, kws: list[Keyword]) -> float:
    """The JD weight a skill carries: the summed weight of the keywords it hits (the scorer's literal-or-stemmed matching)."""
    return sum(kws[i].weight for i in _hits(str(skill), kws))


def skill_group_config(settings) -> dict:
    """settings cv.skill_groups / skills_always / skill_group_max / skill_umbrellas / skills_when_asked as one dict ({} when the settings
    define no groups: the two-page CV then keeps the older Core + fact-bank rows of `cv_skills`)."""
    cv = (getattr(settings, "raw", None) or {}).get("cv") or {}
    keys = ("skill_groups", "skills_always", "skill_group_max", "skill_umbrellas", "skills_when_asked")
    return {k: cv[k] for k in keys if cv.get(k) is not None} if cv.get("skill_groups") else {}


_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")


def _head(skill: str) -> str:
    """A skill name without its trailing bracket, as a key: "Kubernetes (AWS EKS)" -> "kubernetes"."""
    return _key(_QUALIFIER.sub("", str(skill)))


def collapse_skills(entries: list[tuple[str, str, bool]], umbrellas: list[str]) -> list[tuple[str, str, bool]]:
    """`entries` ((source, skill, pinned), listed order) with the redundant names taken out; the specific skill stays and takes the pin of
    the name it replaces. A skill and its qualified form are one skill ("Kubernetes" goes, "Kubernetes (AWS EKS)" stays); an umbrella name
    (settings cv.skill_umbrellas: "AWS") goes when a skill that starts with it and a space ("AWS Lambda") is listed. A name that only
    shares letters with another ("R", "R Markdown") is not an umbrella."""
    keys = [_key(s) for _, s, _ in entries]
    pins = [p for _, _, p in entries]
    gone: set[int] = set()
    for i, (_, skill, _) in enumerate(entries):
        if _QUALIFIER.search(str(skill)) and _head(skill) in keys:            # the unqualified name is the general one
            j = keys.index(_head(skill))
            gone.add(j)
            pins[i] = pins[i] or pins[j]
    for u in umbrellas:
        u = _key(u)
        if u not in keys:
            continue
        j = keys.index(u)
        specific = [i for i, k in enumerate(keys) if k.startswith(u + " ") and i not in gone]
        if specific:
            gone.add(j)
            for i in specific:
                pins[i] = pins[i] or pins[j]
    return [(src, s, pins[i]) for i, (src, s, _) in enumerate(entries) if i not in gone]


def terms_group(fb: dict, patterns: list[str], taken: set[str]) -> list[str]:
    """The `terms:` the user wrote on the fact bank's bullets that match one of `patterns` (regexes), as skill names for a group with no
    skills row of its own: bank order, capitalised, no deny-listed word, none already on the CV, and a term that is a whole-word part of
    a longer one left out ("stakeholder" goes when "stakeholder communication" stays). Only the user's own words."""
    rx = [re.compile(p, re.I) for p in patterns]
    deny = fb.get("deny_terms", [])
    terms: list[str] = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                for t in b.get("terms") or []:
                    t = " ".join(str(t).split())
                    if (t and any(r.search(t) for r in rx) and not deny_hits(t, deny) and _key(t) not in taken
                            and _key(t) not in {_key(x) for x in terms}):
                        terms.append(t)

    def part_of(short: str, long: str) -> bool:
        return re.search(r"(?<![\w])" + re.escape(short) + r"(?![\w])", long, re.I) is not None

    kept = [t for t in terms if not any(len(o) > len(t) and part_of(t, o) for o in terms)]
    return [t[:1].upper() + t[1:] for t in kept]


def asked_for(skill: str, jd: str) -> bool:
    """Does the ad name this skill as a whole word?"""
    return re.search(r"(?<![\w])" + re.escape(str(skill)) + r"(?![\w])", jd or "", re.I) is not None


def cv_skill_groups(fb: dict, kws: list[Keyword], cfg: dict, jd: str = "") -> tuple[dict[str, list[str]], list[str], list[str]]:
    """(groups, left out bank groups, empty groups): the two-page CV's Skills section as the fixed groups of settings cv.skill_groups,
    in their order. Every fact-bank skill lands in exactly one group (the first occurrence of a name wins), in this order of
    preference: the group whose `skills` names it; for a skill of a fact-bank group, the first configured group whose `banks`
    regex matches that fact-bank group's name; for a `skills.core` skill nobody names, the `default` group. A fact-bank group
    that no configured group claims is not shown (it is in the second list). `skills.core` and `cv.skills_always` are pinned: never
    cut by the per-group cap `skill_group_max`. A skill appears once: a name and its qualified form, and an umbrella name with a
    specific skill of it, are one (`collapse_skills`). A language in `skills_when_asked` (TypeScript, JavaScript) is shown only when the
    ad `jd` names it. A group with no skill of its own but `from_terms` shows the matching `terms:` of the bank's bullets
    (`terms_group`); one with nothing at all is left off (the third list: the review screen says so). Inside a group skills are ordered
    by JD keyword weight (`skill_weight`), then by fact-bank order (`skills.core` first, then the groups as listed): a pin is not an
    edge. Nothing is invented: only the fact bank's own words are placed. Raises ValueError for a core skill on the deny list, like
    `cv_skills`."""
    problems = core_deny_problems(fb)
    if problems:
        raise ValueError("; ".join(problems) + " — remove it from skills.core or from deny_terms (`careerops doctor`)")
    groups = cfg["skill_groups"]
    labels = [g["label"] for g in groups]
    route = {_key(s): g["label"] for g in groups for s in g.get("skills") or []}
    banks = [(g["label"], [re.compile(p, re.I) for p in g.get("banks") or []]) for g in groups]
    default = next((g["label"] for g in groups if g.get("default")), labels[0])
    always = {_key(s) for s in cfg.get("skills_always") or []}
    cap = cfg.get("skill_group_max")
    when_asked = {_key(s) for s in cfg.get("skills_when_asked") or []}
    sources = [(CORE_KEY, core_skills(fb))] + [(g, [str(s) for s in items]) for g, items in (fb.get("skills") or {}).items() if g != CORE_KEY]
    seen: set[str] = set()
    entries: list[tuple[str, str, bool]] = []                    # (source, skill, pinned): the first occurrence of each name
    for source, items in sources:
        for s in items:
            k = _key(s)
            if k not in seen and not (k in when_asked and not asked_for(s, jd)):
                seen.add(k)
                entries.append((source, s, source == CORE_KEY or k in always))
    placed: dict[str, list[tuple[str, bool]]] = {label: [] for label in labels}
    left_out: list[str] = []
    for source, s, pinned in collapse_skills(entries, [str(u) for u in cfg.get("skill_umbrellas") or []]):
        label = route.get(_key(s))
        if label is None and source != CORE_KEY:
            label = next((lab for lab, pats in banks if any(p.search(source) for p in pats)), None)
        if label is None and pinned:
            label = default
        if label is None:
            if source not in left_out:
                left_out.append(source)
            continue
        placed[label].append((s, pinned))
    for g in groups:                                              # a group the bank has no skills for shows the user's own terms
        if not placed[g["label"]] and g.get("from_terms"):
            shown = {_key(s) for items in placed.values() for s, _ in items}
            placed[g["label"]] = [(t, False) for t in terms_group(fb, [str(p) for p in g["from_terms"]], shown)]
    out: dict[str, list[str]] = {}
    for label in labels:
        ranked = sorted(enumerate(placed[label]), key=lambda x: (-skill_weight(x[1][0], kws), x[0]))
        if cap is not None and len(ranked) > cap:
            room, keep = max(0, cap - sum(pin for _, (_, pin) in ranked)), set()
            for i, (_, pin) in ranked:                 # best first: every pinned skill stays, the others while there is room
                if pin or room > 0:
                    keep.add(i)
                    room -= 0 if pin else 1
            ranked = [r for r in ranked if r[0] in keep]
        if ranked:
            out[label] = [s for _, (s, _) in ranked]
    return out, left_out, [label for label in labels if label not in out]


def cv_skills(fb: dict, kws: list[Keyword], max_groups: int | None = None, jd: str = "") -> tuple[dict[str, list[str]], list[str]]:
    """(rows, dropped groups). `core` is the first row ("Core"), exactly the user's list and order, never shortened except
    for the case-insensitive repeats of a name it already shows. Every other group follows in fact-bank order with the
    skills the JD keywords match first (every skill of a shown group is listed, no per-group cap); with more than
    `max_groups` of them the groups the JD matches least are left off (the later group on a tie). A Databases row is never
    left off when the JD (`jd`) mentions SQL or databases. Raises ValueError for a core skill on the deny list: it would
    reach the page unchecked."""
    problems = core_deny_problems(fb)
    if problems:
        raise ValueError("; ".join(problems) + " — remove it from skills.core or from deny_terms (`careerops doctor`)")
    skills = fb.get("skills") or {}
    rows = {CORE_LABEL: core_skills(fb)} if core_skills(fb) else {}
    rows.update({g: list(items) for g, items in skills.items() if g != CORE_KEY})
    raw = rows
    rows = dedupe_skills(raw)
    weight = [k.weight for k in kws]

    def relevant(skill: str) -> frozenset[int]:
        return _hits(str(skill), kws)

    groups = [g for g in rows if g != CORE_LABEL]
    dropped: list[str] = []
    if max_groups is not None and len(groups) > max_groups:
        rel = {g: sum(weight[i] for i in set().union(*(relevant(s) for s in rows[g]))) for g in groups}
        pinned = [g for g in groups if is_database_row(g)] if wants_databases(jd) else []
        rest = sorted((g for g in groups if g not in pinned), key=lambda g: (-rel[g], groups.index(g)))
        keep = sorted([*pinned, *rest[:max(0, max_groups - len(pinned))]], key=groups.index)
        dropped = [g for g in groups if g not in keep]
    # a name is listed once, in the first row that is SHOWN: a skill only a dropped row had does not vanish from the CV
    shown = dedupe_skills({g: items for g, items in raw.items() if g not in dropped})
    out: dict[str, list[str]] = {}
    for g, items in shown.items():
        out[g] = list(items) if g == CORE_LABEL else sorted(items, key=lambda s: not relevant(s))   # stable: bank order
    return out, dropped
