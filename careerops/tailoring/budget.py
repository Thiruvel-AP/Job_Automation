"""Rank the fact bank against a JD: weighted keyword overlap per bullet, and the projects most relevant first.

Overlap uses the same literal/stemmed matching as the ATS score, over each bullet's text and its `terms`. The ranking
drives selection.select_cv (which bullets and which two projects go on the CV) and the cover letter's two evidence
bullets; nothing here changes what the CV may claim: every bullet is a fact-bank bullet, picked by id.
"""
from __future__ import annotations

from .domain import domain_overlap, jd_domain
from .scorer import Keyword, score


def bullet_scores(fb: dict, kws: list[Keyword]) -> dict[str, float]:
    """Weighted keyword overlap per bullet id: sum of the weights of the JD keywords its text or terms contain."""
    total = sum(k.weight for k in kws) or 1.0
    out = {}
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                out[b["id"]] = round(score(b["text"] + " " + " ".join(b.get("terms", [])), kws).stemmed * total, 6)
    return out


DOMAIN_WEIGHT = 2.0       # one JD domain group the bullet names counts like one required keyword (weight 2)


def letter_scores(fb: dict, kws: list[Keyword], jd: str = "") -> dict[str, float]:
    """The ranking of the cover letter's evidence: every bullet of every experience and project, by overlap with the JD
    keywords (`bullet_scores`) PLUS overlap with the JD's company domain (bank, payments, customers ...; domain.py). A
    bullet's domain is read from its text, its terms and the entry it belongs to (a project's name, a job's org and title:
    "Banking Customer Churn" carries the domain its one-line bullet does not say)."""
    out = bullet_scores(fb, kws)
    weights = jd_domain(jd)
    if not weights:
        return out
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            where = " ".join(str(e.get(k) or "") for k in ("name", "org", "title"))
            for b in e["bullets"]:
                text = " ".join([b["text"], *b.get("terms", []), where])
                out[b["id"]] = round(out[b["id"]] + DOMAIN_WEIGHT * domain_overlap(text, weights), 6)
    return out


def fact_scores(facts: list[str], kws: list[Keyword], jd: str = "") -> list[float]:
    """The same ranking for profile facts (one line each): keyword overlap scaled like a bullet's, plus the JD domain."""
    total = sum(k.weight for k in kws) or 1.0
    weights = jd_domain(jd)
    return [round(score(f, kws).stemmed * total + DOMAIN_WEIGHT * domain_overlap(f, weights), 6) for f in facts]


def rank_projects(fb: dict, kws: list[Keyword], preferred: list[str] | None = None) -> list[str]:
    """Project ids, most relevant to the JD first: by the project's best bullet, then by the sum over its bullets;
    the family's preferred projects only break exact ties."""
    sc = bullet_scores(fb, kws)
    pref = list(preferred or [])
    order = {p["id"]: i for i, p in enumerate(fb.get("projects", []))}

    def key(p):
        s = [sc[b["id"]] for b in p["bullets"]]
        return (-max(s, default=0.0), -sum(s), 0 if p["id"] in pref else 1, order[p["id"]])

    return [p["id"] for p in sorted(fb.get("projects", []), key=key)]
