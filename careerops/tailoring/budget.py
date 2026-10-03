"""Keep each tailor_cv call inside the provider's token budget (estimated input + max_tokens <= llm.max_call_tokens).

The fact bank can hold far more bullets than a model needs to see. Bullets are ranked by weighted keyword overlap with
the JD (text + terms, same literal/stemmed matching as the ATS score); the prompt carries the top `top_n`, plus the best
bullet of every experience entry (so each entry can still be filled) and of at least two projects (the CV needs two).
If the prompt is still over budget the lowest-ranked optional bullets go first, then the keyword list shrinks.
Nothing here changes what the CV may claim: the CV is still assembled from the full fact bank by id.
"""
from __future__ import annotations

from typing import Callable

from ..llm.pacing import estimate_tokens
from .scorer import Keyword, score

KEYWORD_STEPS = (None, 40, 25)          # how far the keyword list may shrink once every optional bullet is gone


def bullet_scores(fb: dict, kws: list[Keyword]) -> dict[str, float]:
    """Weighted keyword overlap per bullet id: sum of the weights of the JD keywords its text or terms contain."""
    total = sum(k.weight for k in kws) or 1.0
    out = {}
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                out[b["id"]] = score(b["text"] + " " + " ".join(b.get("terms", [])), kws).stemmed * total
    return out


def select_bullets(fb: dict, kws: list[Keyword], top_n: int = 14,
                   preferred: list[str] | None = None) -> tuple[list[str], set[str]]:
    """(ids to send in rank order, ids that may not be dropped). Ties keep fact-bank order. The best bullet of each
    preferred project (the role family's prior) is sent too, but is the first to go if the budget is tight."""
    sc = bullet_scores(fb, kws)
    order = {bid: i for i, bid in enumerate(sc)}
    ranked = sorted(sc, key=lambda b: (-sc[b], order[b]))
    must: set[str] = set()
    for e in fb.get("experience", []):
        must.add(max((b["id"] for b in e["bullets"]), key=lambda b: (sc[b], -order[b])))
    owner = {b["id"]: p["id"] for p in fb.get("projects", []) for b in p["bullets"]}
    chosen = set(ranked[:top_n]) | must
    for p in fb.get("projects", []):
        if p["id"] in (preferred or []):
            chosen.add(max((b["id"] for b in p["bullets"]), key=lambda b: (sc[b], -order[b])))
    have = {owner[b] for b in chosen if b in owner}
    for bid in ranked:                                       # the CV needs two projects to pick from
        if len(have) >= min(2, len(fb.get("projects", []))):
            break
        if bid in owner and owner[bid] not in have:
            chosen.add(bid); must.add(bid); have.add(owner[bid])
    return [b for b in ranked if b in chosen], must


def view(fb: dict, keep: set[str]) -> dict:
    """The fact bank reduced to the kept bullets (entries with none are left out of projects, never out of experience)."""
    exp = [{**e, "bullets": [b for b in e["bullets"] if b["id"] in keep]} for e in fb["experience"]]
    prj = [{**p, "bullets": [b for b in p["bullets"] if b["id"] in keep]} for p in fb["projects"]]
    return {**fb, "experience": [e for e in exp if e["bullets"]], "projects": [p for p in prj if p["bullets"]]}


def fit(fb: dict, kws: list[Keyword], build: Callable[[dict, list[Keyword]], tuple[str, str]], max_tokens: int,
        limit: int, top_n: int = 14, preferred: list[str] | None = None) -> tuple[dict, list[Keyword], dict]:
    """(reduced fact bank, keywords, meta). `build(view, kws)` returns the (system, user) prompt for that view.
    meta: bullets_sent / bullets_total / est_input_tokens / max_tokens / over_budget."""
    ranked, must = select_bullets(fb, kws, top_n, preferred)
    keep = list(ranked)
    total = sum(len(e["bullets"]) for sec in ("experience", "projects") for e in fb[sec])

    def est(ids, k):
        return estimate_tokens(*build(view(fb, set(ids)), k))

    k = kws
    for step in KEYWORD_STEPS:
        k = kws if step is None else kws[:step]
        while est(keep, k) + max_tokens > limit:
            droppable = [b for b in reversed(keep) if b not in must]
            if not droppable:
                break
            keep = [b for b in keep if b != droppable[0]]
        if est(keep, k) + max_tokens <= limit:
            break
    n = est(keep, k)
    return view(fb, set(keep)), k, {"bullets_sent": len(keep), "bullets_total": total, "est_input_tokens": n,
                                    "max_tokens": max_tokens, "over_budget": n + max_tokens > limit}


def call_limits(settings, task: str) -> tuple[int, int]:
    """(max_tokens for the task, llm.max_call_tokens): the completion cap and the whole-call ceiling."""
    from ..llm.router import DEFAULT_MAX_TOKENS
    llm = settings["llm"]
    caps = llm.get("max_tokens") or {}
    return int(caps.get(task) or caps.get("default") or DEFAULT_MAX_TOKENS), int(llm.get("max_call_tokens", 7500))
