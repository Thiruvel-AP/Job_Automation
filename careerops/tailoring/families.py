"""Possible evidence: a JD term the fact bank does not say, but a related term in the same *family* is there.

config/keyword_groups.txt holds words that MEAN THE SAME (llm | llms): they merge keywords and count as evidence. A family
(config/evidence_families.txt) is looser: "A/B experiments" and "hypothesis testing" are neighbours, not synonyms. Only the
person who did the work can say whether one evidences the other, so a family hit is never counted: the term is not in the
ceiling, not on the CV, not in the letter, and not listed as a genuine gap either. It is shown as `possible evidence — you
decide`, with the fact-bank entries that make it plausible.
"""
from __future__ import annotations

from .scorer import ROOT, _contains, norm_literal, norm_stem


def load_families() -> list[list[str]]:
    """`a | b | c` per line of config/evidence_families.txt."""
    p = ROOT / "config" / "evidence_families.txt"
    rows = [[w.strip() for w in l.split("|") if w.strip()] for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]
    return [r for r in rows if len(r) > 1]


def _plain(text: str) -> str:
    """Words with a plural -s dropped ("experiments" = "experiment"): the stemmer treats those two differently."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in norm_literal(text).split())


def _forms(text: str) -> list[str]:
    return [f for f in (norm_stem(text), _plain(text)) if f]


def _same(a: str, b: str) -> bool:
    return any(x == y or _contains(x, y) or _contains(y, x) for x in _forms(a) for y in _forms(b))


def _related(a: str, b: str) -> bool:
    """Same words after stemming, or one is a whole-word part of the other ("statistics" / "applied statistics")."""
    return _same(a, b)


def _places(fb: dict) -> list[tuple[str, str]]:
    """(where, text) for every place the fact bank says something: skills rows, profile facts, bullets (text + terms)."""
    out = [("profile facts", f) for f in fb.get("profile_facts", [])]
    out += [(f"skills: {g}", ", ".join(items)) for g, items in fb.get("skills", {}).items()]
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                out.append((f"bullet {b['id']}", b["text"] + " " + " ".join(b.get("terms", []))))
    return out


def possible_evidence(fb: dict, gaps: list[str], alts: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    """{gap: [{"via": family member, "where": [places in the fact bank]}]} for the gaps that have a family member in the
    fact bank. A gap with none is not in the result: it is a genuine gap."""
    families, places = load_families(), _places(fb)
    out: dict[str, list[dict]] = {}
    for gap in gaps:
        mine = [gap, *(alts or {}).get(gap, [])]
        via: list[dict] = []
        for fam in families:
            if not any(_related(m, f) for m in mine for f in fam):
                continue
            for member in fam:
                if any(_related(m, member) for m in mine) or any(v["via"] == member for v in via):
                    continue
                where = [w for w, text in places if any(_contains(x, y) for x in _forms(text) for y in _forms(member))]
                if where:
                    via.append({"via": member, "where": where[:4]})
        if via:
            out[gap] = via
    return out
