"""Rank the fact bank against a JD: weighted keyword overlap per bullet, and the projects most relevant first.

Overlap uses the same literal/stemmed matching as the ATS score, over each bullet's text and its `terms`. The ranking
drives selection.select_cv (which bullets and which two projects go on the CV) and the cover letter's two evidence
bullets; nothing here changes what the CV may claim: every bullet is a fact-bank bullet, picked by id.
"""
from __future__ import annotations

from .domain import domain_overlap, jd_domain, jd_rank_weights, rank_overlap
from .scorer import Keyword, keyword_hits, score


def bullet_scores(fb: dict, kws: list[Keyword]) -> dict[str, float]:
    """Weighted keyword overlap per bullet id: sum of the weights of the JD keywords its text or terms contain."""
    total = sum(k.weight for k in kws) or 1.0
    out = {}
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                text = b["text"] + " " + " ".join(b.get("terms", []))
                found = score(text, kws)
                seen = set(found.hits) | set(found.wording_misses)
                extra = sum(kws[i].weight for i in keyword_hits(text, kws) if kws[i].term not in seen)       # a multiword keyword's words, split
                out[b["id"]] = round(found.stemmed * total + extra, 6)
    return out


DOMAIN_WEIGHT = 2.0       # one JD domain group the bullet names counts like one required keyword (weight 2)


def entry_where(entry: dict) -> str:
    """Where a bullet comes from, as text: the project's name, the job's org and title ("Banking Customer Churn", "Lloyd's
    Banking Group Datathon", "Avasoft"). Its domain words belong to every bullet of the entry."""
    return " ".join(str(entry[k]).strip() for k in ("name", "org", "title") if str(entry.get(k) or "").strip())


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
            where = entry_where(e)
            for b in e["bullets"]:
                text = " ".join([b["text"], *b.get("terms", []), where])
                out[b["id"]] = round(out[b["id"]] + DOMAIN_WEIGHT * domain_overlap(text, weights), 6)
    return out


def fact_scores(facts: list[str], kws: list[Keyword], jd: str = "") -> list[float]:
    """The same ranking for profile facts (one line each): keyword overlap scaled like a bullet's, plus the JD domain."""
    total = sum(k.weight for k in kws) or 1.0
    weights = jd_domain(jd)
    return [round(score(f, kws).stemmed * total + DOMAIN_WEIGHT * domain_overlap(f, weights), 6) for f in facts]


def relevance_parts(fb: dict, kws: list[Keyword], jd: str = "") -> dict[str, tuple[float, float]]:
    """{bullet id: (keyword overlap, domain overlap)} for choosing the CV's content. Keyword overlap is `bullet_scores`; domain
    overlap is DOMAIN_WEIGHT times the JD's weight of every company-domain or topic group (customer, consumer, behaviour,
    segmentation, validation, statistics, bank, payments ...; domain.py) the bullet names in its text, its terms or the entry it
    belongs to (a project's name, a job's org and title). Without a JD the second number is 0."""
    kw = bullet_scores(fb, kws)
    weights = jd_rank_weights(jd)
    out = {}
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            where = entry_where(e)
            for b in e["bullets"]:
                dom = DOMAIN_WEIGHT * rank_overlap(" ".join([b["text"], *b.get("terms", []), where]), weights) if weights else 0.0
                out[b["id"]] = (kw[b["id"]], round(dom, 6))
    return out


def relevance_scores(fb: dict, kws: list[Keyword], jd: str = "") -> dict[str, float]:
    """{bullet id: keyword overlap + domain overlap} (`relevance_parts`)."""
    return {b: round(k + d, 6) for b, (k, d) in relevance_parts(fb, kws, jd).items()}


def rank_projects(fb: dict, kws: list[Keyword], preferred: list[str] | None = None, jd: str = "") -> list[str]:
    """Project ids, most relevant to the JD first: by the project's best bullet (keyword overlap plus the JD's domain and topics
    when `jd` is given), then by the sum over its bullets; the family's preferred projects only break exact ties."""
    sc = relevance_scores(fb, kws, jd)
    pref = list(preferred or [])
    order = {p["id"]: i for i, p in enumerate(fb.get("projects", []))}

    def key(p):
        s = [sc[b["id"]] for b in p["bullets"]]
        return (-max(s, default=0.0), -sum(s), 0 if p["id"] in pref else 1, order[p["id"]])

    return [p["id"] for p in sorted(fb.get("projects", []), key=key)]


def ranking_table(fb: dict, kws: list[Keyword], jd: str, chosen: set[str], top: int = 10) -> dict:
    """What the review screen prints: the `top` bullets and the `top` projects by relevance to the JD, each with its score
    (keyword + domain) and whether the CV uses it. {"bullets": [{"id", "entry", "score", "keywords", "domain", "chosen", "text"}],
    "projects": [{"id", "name", "score", "sum", "chosen"}]}; ties keep fact-bank order."""
    parts = relevance_parts(fb, kws, jd)
    rows = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                k, d = parts[b["id"]]
                rows.append({"id": b["id"], "entry": e.get("org") or e.get("name") or "", "score": round(k + d, 2),
                             "keywords": round(k, 2), "domain": round(d, 2), "chosen": b["id"] in chosen,
                             "text": " ".join(b["text"].split())})
    bullets = sorted(rows, key=lambda r: -r["score"])[:top]            # sorted is stable: fact-bank order within a tie
    by_id = {r["id"]: r for r in rows}
    projects = []
    for p in fb.get("projects", []):
        s = [by_id[b["id"]]["score"] for b in p["bullets"]]
        projects.append({"id": p["id"], "name": p.get("name") or p["id"], "score": max(s, default=0.0), "sum": round(sum(s), 2),
                         "chosen": any(by_id[b["id"]]["chosen"] for b in p["bullets"])})
    projects.sort(key=lambda r: (-r["score"], -r["sum"]))
    return {"bullets": bullets, "projects": projects[:top]}
