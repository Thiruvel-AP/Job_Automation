"""Rank the fact bank against a JD: weighted keyword overlap per bullet, and the projects most relevant first.

Overlap uses the same literal/stemmed matching as the ATS score, over each bullet's text and its `terms`. The ranking
drives selection.select_cv (which bullets and which two projects go on the CV) and the cover letter's two evidence
bullets; nothing here changes what the CV may claim: every bullet is a fact-bank bullet, picked by id.
"""
from __future__ import annotations

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
