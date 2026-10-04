"""Choose the CV's content in code. The model never selects: a small model ignored the ranking it was shown, so the
selection is made here and the model is only asked to reword (rephrase batches) and to write the profile.

  * bullets are picked greedily by marginal gain: each step takes the bullet whose keywords add the most weight that no
    selected bullet and no skill already covers (a bullet repeating what is covered adds nothing, however many keywords it
    has); ties go to the more recent entry, then to the bullet with more weighted overlap
  * the most recent experience entry keeps 2-4 bullets, every other entry 1-2; an entry stops early when nothing is left
    to gain, but never drops below its minimum
  * exactly 2 projects, picked the same way (a project's gain is the union of its bullets); the role family's preferred
    projects only break exact ties
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


def _hits(text: str, kws: list[Keyword]) -> frozenset[int]:
    """Indexes of the keywords the text carries, by the same literal-or-stemmed matching as the ATS score."""
    lit, stm = norm_literal(text), norm_stem(text)
    return frozenset(i for i, k in enumerate(kws)
                     if any(_contains(lit, norm_literal(w)) or _contains(stm, norm_stem(w)) for w in k.members()))


def bullet_hits(fb: dict, kws: list[Keyword]) -> dict[str, frozenset[int]]:
    """{bullet id: indexes of the keywords its text or terms carry}."""
    return {b["id"]: _hits(b["text"] + " " + " ".join(b.get("terms", [])), kws)
            for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}


def select_cv(fb: dict, kws: list[Keyword], preferred: list[str] | None = None) -> dict:
    """{"experience": {entry id: [bullet ids, in the order picked]}, "projects": [project ids, in the order picked]}.
    Every experience entry is listed (a CV shows the whole work history); `fb["experience"]` is newest first."""
    sc = bullet_scores(fb, kws)
    hits = bullet_hits(fb, kws)
    weight = [k.weight for k in kws]
    skills = " ".join(s for items in (fb.get("skills") or {}).values() for s in items)
    covered = set(_hits(skills, kws))                       # the whole skills section is always on the CV

    def gain(ids: Sequence[str]) -> float:
        new = set().union(*(hits[i] for i in ids)) - covered if ids else set()
        return sum(weight[i] for i in new)

    entries = [(n, e["id"], [b["id"] for b in e["bullets"]]) for n, e in enumerate(fb["experience"])]
    picked: dict[str, list[str]] = {eid: [] for _, eid, _ in entries}
    bounds = {eid: (MOST_RECENT_BULLETS if n == 0 else OTHER_BULLETS) for n, eid, _ in entries}
    order = {b: i for i, b in enumerate(sc)}                 # fact-bank order: the last tie-break

    def best(pool: list[tuple[int, str, str]]) -> tuple[int, str, str]:
        # most gain; then the more recent entry; then more weighted overlap; then fact-bank order
        return min(pool, key=lambda c: (-gain([c[2]]), c[0], -sc[c[2]], order[c[2]]))

    def pool() -> list[tuple[int, str, str]]:
        return [(n, eid, b) for n, eid, ids in entries for b in ids
                if b not in picked[eid] and len(picked[eid]) < bounds[eid][1]]

    while (cand := pool()):                                  # 1. take bullets while they still add something
        n, eid, b = best(cand)
        if gain([b]) <= 0:
            break
        picked[eid].append(b)
        covered |= hits[b]
    for n, eid, ids in entries:                              # 2. an entry below its minimum is topped up (gain or not)
        while len(picked[eid]) < max(1, bounds[eid][0]):
            rest = [(n, eid, b) for b in ids if b not in picked[eid]]
            if not rest:
                break
            _, _, b = best(rest)
            picked[eid].append(b)
            covered |= hits[b]

    ranked = rank_projects(fb, kws, preferred)               # the old order: best bullet, sum, preferred, fact bank
    rank = {p: i for i, p in enumerate(ranked)}
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb.get("projects", [])}
    projects: list[str] = []
    while len(projects) < min(PROJECTS, len(own)):
        pid = min((p for p in own if p not in projects), key=lambda p: (-gain(own[p]), rank[p]))
        projects.append(pid)
        covered |= set().union(*(hits[b] for b in own[pid])) if own[pid] else set()
    return {"experience": picked, "projects": projects}


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
