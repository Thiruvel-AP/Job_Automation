"""Choose the CV's content in code. The model never selects: a small model ignored the ranking it was shown, so the
ranking is applied here and the model is only asked to reword (rephrase batches) and to write the profile.

  * every bullet, experience and project alike, is ranked by weighted keyword overlap (text + terms, budget.bullet_scores)
  * the most recent experience entry keeps 2-4 bullets, every other entry 1-2, as many as have any overlap
  * exactly 2 projects, the most relevant first; the role family's preferred projects only break exact ties
  * the shrink loop in tailor.py then drops the least relevant bullets until the CV fits scoring.max_pages
"""
from __future__ import annotations

from typing import Iterator, Sequence, TypeVar

from .budget import bullet_scores, rank_projects
from .scorer import Keyword, _contains, norm_literal, norm_stem

MOST_RECENT_BULLETS = (2, 4)        # (min, max) bullets of the first (most recent) experience entry
OTHER_BULLETS = (1, 2)              # (min, max) bullets of every other experience entry
PROJECTS = 2
T = TypeVar("T")


def select_cv(fb: dict, kws: list[Keyword], preferred: list[str] | None = None) -> dict:
    """{"experience": {entry id: [bullet ids, most relevant first]}, "projects": [project ids, most relevant first]}.
    An entry keeps as many bullets as overlap the JD, within its (min, max); ties keep fact-bank order. Every
    experience entry is listed (a CV shows the whole work history); `fb["experience"]` is newest first."""
    sc = bullet_scores(fb, kws)
    order = {bid: i for i, bid in enumerate(sc)}
    experience = {}
    for n, e in enumerate(fb["experience"]):
        lo, hi = MOST_RECENT_BULLETS if n == 0 else OTHER_BULLETS
        ranked = sorted((b["id"] for b in e["bullets"]), key=lambda b: (-sc[b], order[b]))
        take = max(lo, min(hi, sum(sc[b] > 0 for b in ranked)))
        experience[e["id"]] = ranked[:max(1, take)]
    return {"experience": experience, "projects": rank_projects(fb, kws, preferred)[:PROJECTS]}


def selected_ids(fb: dict, sel: dict) -> list[str]:
    """Bullet ids of a selection in CV order: experience entries as listed, then each chosen project's bullets."""
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb["projects"]}
    return [i for e in fb["experience"] for i in sel["experience"].get(e["id"], [])] + \
           [i for p in sel["projects"] for i in own.get(p, [])]


def missing_keywords(bullet: dict, kws: list[Keyword]) -> list[str]:
    """The JD words a bullet may take on: for each keyword the bullet's text does not already say, the first of its
    words that the bullet's `terms` evidence. A reworded bullet may use exactly these and no other keyword."""
    lit = norm_literal(bullet["text"])
    stems = [norm_stem(t) for t in bullet.get("terms", [])]
    out = []
    for k in kws:
        if any(_contains(lit, norm_literal(w)) for w in k.members()):
            continue
        word = next((w for w in k.members() if any(_contains(s, norm_stem(w)) for s in stems)), None)
        if word:
            out.append(word)
    return out


def chunks(items: Sequence[T], n: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]
