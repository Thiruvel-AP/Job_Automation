"""Choose the CV's content in code. The model never selects: a small model ignored the ranking it was shown, so the
selection is made here and the model is only asked to reword (rephrase batches) and to write the profile.

  * bullets are picked greedily by marginal gain: each step takes the bullet whose keywords add the most weight that no
    selected bullet and no skill already covers (a bullet repeating what is covered adds nothing, however many keywords it
    has); ties go to the more recent entry, then to the bullet with more weighted overlap
  * a bullet whose words overlap an already selected bullet too much (token Jaccard >= `jaccard`, 0.6) is never added
  * how many bullets an entry gets is a `Layout`: TWO_PAGE (newest entry 4-5, the others 3-4, 5 projects with 2-3 bullets
    each) or ONE_PAGE (newest 2-4, the others 1-2, exactly 2 projects with all their bullets). An entry stops early when
    nothing is left to gain, but never drops below its minimum (an entry with fewer bullets shows them all)
  * projects are picked by `_Picker.project_value`: the marginal gain of the union of its bullets PLUS PROJECT_RELEVANCE x its best bullet's
    relevance score (v3.5.6: gain alone put AgenticFriend, rank 8, over mcp_searchagent, rank 1, whose keywords the experience had already
    covered); the role family's preferred projects only break exact ties. A keyword that only the skills line covers still earns a project SKILL_ONLY_CREDIT of its
    weight: "Time series forecasting" in the skills is a claim, the forecasting project is the proof
  * with the JD text, the company domain and topics it names (customer, consumer, behaviour, segmentation, validation,
    statistics, bank ...; domain.py) count like keywords: a bullet (or a project, through its name) that names one nobody on
    the CV names yet gains DOMAIN_WEIGHT times the JD's weight of it, and the same overlap adds to the bullet's score
  * `fill_order` lists the bullets still unused, for the two-page loop that keeps adding until page 2 is full: further
    projects (up to 6) first, then more bullets of the shown projects, then more experience bullets
  * the shrink loop in tailor.py drops the least relevant bullets when the CV is over the page limit
  * `replaces:` — a bullet that stands in for older ones of the same entry (az10 replaces az1, az2). v3.7.1: the ones it replaces are RETIRED: never chosen
    while it exists in the entry, however they score (they still count as evidence in the requirement map, reqmap.py), and where one of them would have
    been chosen the replacer is chosen instead. (Before v3.7.1 a replacer scoring under REPLACE_RATIO of them was passed over and the retired bullet
    printed: the 15 Nov Sirius CV showed az1 instead of az10.) The one exception is tailor.reconcile_proof: a hook's proof bullet stays when its
    replacement does not say the hook's number
  * `situation_included: true` — a bullet that opens with its own situation: at most one per entry (the entry's `situation:` text is not
    added next to it, layout.with_openers)
  * `verbatim: true` — a bullet that is used word for word (tailor.py never sends it to the model); selection treats it like any bullet
  * a role dated within the last `Layout.recent_years` years keeps at least `Layout.recent_min` bullets: its minimum is topped up before
    any project is added, with near-duplicates if nothing else is left
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Sequence, TypeVar

from .budget import DOMAIN_WEIGHT, entry_where, rank_projects, relevance_scores
from .domain import jd_rank_weights, rank_groups
from .scorer import Keyword, _contains, has_literal, keyword_hits, norm_literal, norm_stem

MOST_RECENT_BULLETS = (2, 4)        # (min, max) bullets of the first (most recent) experience entry, one-page layout
OTHER_BULLETS = (1, 2)              # (min, max) bullets of every other experience entry, one-page layout
PROJECTS = 2
JACCARD = 0.6
PROJECT_RELEVANCE = 1.0     # a project's best-bullet relevance score (keywords + domain) counts this much next to its marginal gain when projects are picked
SKILL_ONLY_CREDIT = 0.5     # share of a keyword's weight a PROJECT earns for showing a keyword that only the skills line covers so far
T = TypeVar("T")


def today() -> date:
    """The date a role's recency is measured from (a function so tests can fix it)."""
    return date.today()


@dataclass(frozen=True)
class Layout:
    """How many bullets and projects a CV holds. `project_bullets` None = every bullet of a chosen project. `recent_min` bullets are the
    least a role dated within the last `recent_years` years keeps (0 = no such rule)."""
    first: tuple[int, int]
    other: tuple[int, int]
    projects: int
    project_bullets: tuple[int, int] | None = None
    recent_min: int = 0
    recent_years: int = 2


MAX_PROJECTS = 3                    # the Document rules: at most three projects on a CV, whatever the settings say
ONE_PAGE = Layout(MOST_RECENT_BULLETS, OTHER_BULLETS, PROJECTS, None)
TWO_PAGE = Layout((4, 6), (3, 5), MAX_PROJECTS, (2, 3), 2, 2)


def layout_from(settings) -> Layout:
    """The two-page layout from settings (scoring.cv_bullets / cv_projects / cv_project_bullets / cv_recent_min_bullets / cv_recent_years)."""
    sc = settings["scoring"]
    cb = sc["cv_bullets"]
    return Layout(tuple(cb["most_recent"]), tuple(cb["other"]), min(int(sc["cv_projects"]), MAX_PROJECTS), tuple(sc["cv_project_bullets"]),
                  int(sc.get("cv_recent_min_bullets", 2)), int(sc.get("cv_recent_years", 2)))


def project_cap(settings) -> int:
    """The most projects the fill may put on the CV: scoring.cv_projects_max, never over MAX_PROJECTS (3)."""
    return min(int(settings["scoring"].get("cv_projects_max", MAX_PROJECTS)), MAX_PROJECTS)


# ------------------------------------------------------------------------------------------------------------------ role dates
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DATE_END = re.compile(r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+)?(?P<year>(?:19|20)\d{2})")
_DATE_SPLIT = re.compile(r"\s*(?:[-–—]+|\bto\b)\s*", re.I)
_OPEN_ENDED = re.compile(r"present|current|now|ongoing|today", re.I)


def role_end(dates, ref: date | None = None) -> date | None:
    """The last day of the month (or year) a role's `dates` end in ("May 2026 – Sep 2026" -> 2026-09-30, "2025 – 2026" -> 2026-12-31);
    `ref` (default today) for an open end ("Present"); None when the text has no date."""
    parts = [p for p in _DATE_SPLIT.split(" ".join(str(dates or "").split())) if p]
    if not parts:
        return None
    last = parts[-1]
    if _OPEN_ENDED.fullmatch(last):
        return ref or today()
    m = _DATE_END.fullmatch(last)
    if not m:
        return None
    year = int(m["year"])
    month = _MONTHS.get((m["mon"] or "")[:3].lower())
    if m["mon"] and month is None:
        return None
    month = month or 12
    return date(year, month, calendar.monthrange(year, month)[1])


def is_recent(entry: dict, years: int, ref: date | None = None) -> bool:
    """Does the role (an experience entry with `dates`) end within the last `years` years of `ref` (default today)? An undated role is not."""
    ref = ref or today()
    end = role_end(entry.get("dates"), ref)
    if end is None:
        return False
    try:
        cutoff = ref.replace(year=ref.year - years)
    except ValueError:                                  # 29 Feb
        cutoff = ref.replace(year=ref.year - years, day=28)
    return end >= cutoff


# ------------------------------------------------------------------------------------------------------------- bullet flags
REPLACE_RATIO = 0.8         # v3.7.1: no longer gates anything (a replacer always stands in for what it retires); kept for the tests that document the old rule


def _bullets(fb: dict):
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                yield e, b


def verbatim_ids(fb: dict) -> frozenset[str]:
    """The ids of the bullets marked `verbatim: true`: used word for word, never reworded, shortened, split or given an opener."""
    return frozenset(b["id"] for _, b in _bullets(fb) if b.get("verbatim"))


def situated_ids(fb: dict) -> frozenset[str]:
    """The ids of the bullets marked `situation_included: true`: they open with their own situation."""
    return frozenset(b["id"] for _, b in _bullets(fb) if b.get("situation_included"))


def replacers(fb: dict) -> dict[str, frozenset[str]]:
    """{bullet id: the ids it `replaces:`} for the bullets that replace others: only ids of the SAME entry (a bullet of another role or project is not
    replaced by it)."""
    owner = {b["id"]: e["id"] for e, b in _bullets(fb)}
    return {b["id"]: frozenset(str(x) for x in b["replaces"] if owner.get(str(x)) == e["id"] and str(x) != b["id"])
            for e, b in _bullets(fb) if b.get("replaces")}


def retired_ids(fb: dict) -> frozenset[str]:
    """The ids another bullet of the same entry `replaces:` (v3.7.1): never printed while that bullet exists. They stay in the fact bank and in the
    requirement map as evidence."""
    return frozenset().union(*replacers(fb).values())


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def near_duplicate(text: str, chosen: Sequence[str], limit: float = JACCARD) -> bool:
    """True if `text` has token Jaccard similarity >= `limit` with any text in `chosen`."""
    return any(jaccard(text, c) >= limit for c in chosen)


def _hits(text: str, kws: list[Keyword]) -> frozenset[int]:
    """Indexes of the keywords the text carries: the ATS score's literal-or-stemmed phrase matching, and a multiword keyword also
    when all its words are there (scorer.keyword_hits), so "time-series forecasting" finds a bullet whose terms are "forecasting"
    and "time series"."""
    return keyword_hits(text, kws)


def bullet_hits(fb: dict, kws: list[Keyword]) -> dict[str, frozenset[int]]:
    """{bullet id: indexes of the keywords its text or terms carry}."""
    return {b["id"]: _hits(b["text"] + " " + " ".join(b.get("terms", [])), kws)
            for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}


def skills_text(skills: dict | None) -> str:
    """The skills section as one text: a row is a list of skills, or one line of text (a role family's `skill_lines`)."""
    return " ".join(items if isinstance(items, str) else " ".join(map(str, items)) for items in (skills or {}).values())


class _Picker:
    """The state shared by selection and fill: what the keywords, skills, domain groups and chosen bullets already cover."""

    def __init__(self, fb: dict, kws: list[Keyword], skills: dict | None, jaccard_limit: float, jd: str = "",
                 extra_hits: dict[str, frozenset[int]] | None = None):
        self.fb, self.kws, self.limit = fb, kws, jaccard_limit
        self.sc = relevance_scores(fb, kws, jd)                          # keyword overlap + domain overlap
        self.hits = bullet_hits(fb, kws)
        for bid, ix in (extra_hits or {}).items():                       # v3.6: the requirements a bullet is evidence of (reqmap), aliases included
            if bid in self.hits:
                self.hits[bid] = self.hits[bid] | ix
        self.weight = [k.weight for k in kws]
        self.dweight = jd_rank_weights(jd)                                # {domain / topic group: weight} the JD names
        self.dgroups = {b["id"]: frozenset(rank_groups(" ".join([b["text"], *b.get("terms", []), entry_where(e)])) & set(self.dweight))
                        for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}
        self.text = {b["id"]: b["text"] for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}
        self.order = {b: i for i, b in enumerate(self.sc)}               # fact-bank order: the last tie-break
        self.skill_hits = frozenset(_hits(skills_text(skills), kws))
        self.covered = set(self.skill_hits)                              # the skills section is always on the CV
        self.shown: set[int] = set()                                     # covered by a chosen bullet, not only by the skills line
        self.dcovered: set[str] = set()                                  # a skill row does not show a domain was worked in
        self.chosen: list[str] = []
        # `replaces:` / `situation_included:` (module docstring)
        self.replaces = replacers(fb)
        self.situated = situated_ids(fb)
        self.owner = {b["id"]: e["id"] for e, b in _bullets(fb)}
        self.qualifying: dict[str, list[str]] = {}                       # replaced id -> the replacers that stand in for it
        self.passed_over: set[str] = set()                               # always empty since v3.7.1: no replacer is passed over for scoring lower
        for r, olds in self.replaces.items():
            for o in olds:
                self.qualifying.setdefault(o, []).append(r)
        self.retired = retired_ids(fb)                                   # v3.7.1: replaced bullets, never chosen whatever they score
        self.excluded: set[str] = set(self.retired)                      # never chosen: retired (or replaced by a chosen bullet)
        self.situation_in: set[str] = set()                              # entries with a `situation_included` bullet chosen

    def relevance(self, ids: Sequence[str]) -> float:
        """The best relevance score (keywords + domain, whether or not already covered) of these bullets."""
        return max((self.sc[i] for i in ids), default=0.0)

    def project_value(self, ids: Sequence[str], covered: set[int] | None = None, dcovered: set[str] | None = None) -> float:
        """What a project is worth when projects are picked: its marginal gain (keyword weight nobody shows yet, the skills-only credit) plus
        PROJECT_RELEVANCE x its best bullet's relevance. Gain alone chose AgenticFriend (rank 8, score 2.0: one spurious "packaging" hit) over
        mcp_searchagent (rank 1, score 6.0) because the experience bullets had already covered everything mcp_searchagent says."""
        gain = self.gain(ids) if covered is None else self.gain_over(ids, covered, dcovered or set())
        credit = SKILL_ONLY_CREDIT * self.skill_only_gain(ids) if covered is None else 0.0
        return gain + credit + PROJECT_RELEVANCE * self.relevance(ids)

    def gain_over(self, ids: Sequence[str], covered: set[int], dcovered: set[str]) -> float:
        new = set().union(*(self.hits[i] for i in ids)) - covered if ids else set()
        dnew = set().union(*(self.dgroups[i] for i in ids)) - dcovered if ids else set()
        # summed in a fixed order: a float sum over a set of strings changes in its last digits with the hash seed, and a tie then breaks differently
        # from one run to the next (13 Nov 2026: [r] went 59.1 -> 63.4 -> 59.1 on the same job)
        return sum(self.weight[i] for i in sorted(new)) + DOMAIN_WEIGHT * sum(self.dweight[g] for g in sorted(dnew))

    def gain(self, ids: Sequence[str]) -> float:
        return self.gain_over(ids, self.covered, self.dcovered)

    def skill_only_gain(self, ids: Sequence[str]) -> float:
        """The weight of the keywords these bullets carry that the skills line covers and no chosen bullet shows yet."""
        new = (set().union(*(self.hits[i] for i in ids)) - self.shown) & self.skill_hits if ids else set()
        return sum(self.weight[i] for i in sorted(new))

    def cover(self, ids: Sequence[str]) -> None:
        """Count these bullets' keywords and domain groups as covered, without listing them as chosen."""
        for i in ids:
            self.covered |= self.hits[i]
            self.shown |= self.hits[i]
            self.dcovered |= self.dgroups[i]

    def dup(self, bid: str) -> bool:
        return near_duplicate(self.text[bid], [self.text[c] for c in self.chosen], self.limit)

    def allowed(self, bid: str, strict: bool = False) -> bool:
        """May this bullet still be chosen? Not when it is retired (another bullet of its entry replaces it), and not a second
        `situation_included` bullet of an entry. `strict` (the fill, which cannot take a bullet back) also refuses a replacer whose
        replaced bullets are already chosen."""
        if bid in self.excluded or (bid in self.situated and self.owner[bid] in self.situation_in):
            return False
        return not (strict and self.replaces.get(bid, frozenset()) & set(self.chosen))

    def prefer(self, bid: str, pool: set[str]) -> str:
        """`bid`, or the replacer in `pool` that stands in for it (the one adding most if several)."""
        better = [r for r in self.qualifying.get(bid, []) if r in pool]
        return max(better, key=lambda r: (self.gain([r]), self.sc[r], -self.order[r])) if better else bid

    def settle(self, ids: Sequence[str]) -> list[str]:
        """`ids` (the bullets of one project) without what a remaining replacer replaces, and with at most one `situation_included` bullet."""
        gone = set().union(*(self.replaces.get(i, frozenset()) for i in ids)) if ids else set()
        out, opened = [], False
        for i in ids:
            if i in gone or (i in self.situated and opened):
                continue
            opened = opened or i in self.situated
            out.append(i)
        return out

    def take(self, bid: str) -> list[str]:
        """Choose `bid`; returns the chosen bullets it replaces (they are no longer chosen, nor can they be again)."""
        gone = [c for c in self.chosen if c in self.replaces.get(bid, ())]
        self.chosen = [c for c in self.chosen if c not in gone] + [bid]
        self.excluded |= self.replaces.get(bid, frozenset())
        if bid in self.situated:
            self.situation_in.add(self.owner[bid])
        self.cover([bid])
        return gone


def select_cv(fb: dict, kws: list[Keyword], preferred: list[str] | None = None, skills: dict | None = None,
              layout: Layout = ONE_PAGE, jaccard_limit: float = JACCARD, jd: str = "",
              extra_hits: dict[str, frozenset[int]] | None = None) -> dict:
    """{"experience": {entry id: [bullet ids, in the order picked]}, "projects": [project ids, in the order picked],
    "project_bullets": {project id: [bullet ids]}}. Every experience entry is listed (a CV shows the whole work
    history); `fb["experience"]` is newest first. `skills` is the skills section the CV will show (default: the fact
    bank's own): its keywords need no bullet. `jd` = the job description's text, for the domain and topics it names."""
    skills = fb.get("skills") if skills is None else skills
    pk = _Picker(fb, kws, skills, jaccard_limit, jd, extra_hits)
    entries = [(n, e["id"], [b["id"] for b in e["bullets"]]) for n, e in enumerate(fb["experience"])]
    picked: dict[str, list[str]] = {eid: [] for _, eid, _ in entries}
    bounds = {eid: (layout.first if n == 0 else layout.other) for n, eid, _ in entries}
    ref = today()
    recent = {e["id"]: bool(layout.recent_min) and is_recent(e, layout.recent_years, ref) for e in fb["experience"]}
    floor = {eid: max(1, bounds[eid][0], layout.recent_min if recent[eid] else 0) for _, eid, _ in entries}

    def best(pool: list[tuple[int, str, str]]) -> tuple[int, str, str]:
        # most gain; then the more recent entry; then more weighted overlap; then fact-bank order
        return min(pool, key=lambda c: (-pk.gain([c[2]]), c[0], -pk.sc[c[2]], pk.order[c[2]]))

    def pool() -> list[tuple[int, str, str]]:
        return [(n, eid, b) for n, eid, ids in entries for b in ids
                if b not in picked[eid] and len(picked[eid]) < bounds[eid][1] and pk.allowed(b) and not pk.dup(b)]

    def add(eid: str, b: str) -> None:
        for gone in pk.take(b):                              # a replacer takes the bullets it replaces off the CV
            for ids in picked.values():
                ids[:] = [i for i in ids if i != gone]
        picked[eid].append(b)

    while (cand := pool()):                                  # 1. take bullets while they still add something
        n, eid, b = best(cand)
        if pk.gain([b]) <= 0:
            break
        add(eid, pk.prefer(b, {c[2] for c in cand if c[1] == eid}))
    for n, eid, ids in entries:                              # 2. an entry below its minimum is topped up (gain or not)
        while len(picked[eid]) < floor[eid]:
            rest = [(n, eid, b) for b in ids if b not in picked[eid] and pk.allowed(b) and not pk.dup(b)]
            if not rest and len(picked[eid]) < layout.recent_min and recent[eid]:
                rest = [(n, eid, b) for b in ids if b not in picked[eid] and pk.allowed(b)]   # a recent role keeps two, even near-duplicates
            if not rest:
                break
            _, _, b = best(rest)
            add(eid, pk.prefer(b, {c[2] for c in rest}))
        if not picked[eid] and ids:                          # a CV lists every job: even a lone near-duplicate stays
            add(eid, next((b for b in ids if pk.allowed(b)), next((b for b in ids if b not in pk.retired), ids[0])))

    ranked = rank_projects(fb, kws, preferred, jd)           # best bullet, sum, preferred, fact bank
    rank = {p: i for i, p in enumerate(ranked)}
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb.get("projects", [])}
    projects: list[str] = []
    while len(projects) < min(layout.projects, len(own)):
        pid = min((p for p in own if p not in projects),
                  key=lambda p: (-pk.project_value(own[p]), rank[p]))
        projects.append(pid)
        pk.cover(own[pid])
    # bullets of each project: the whole project (one page), or 2-3 of them by marginal gain (two pages)
    project_bullets: dict[str, list[str]] = {}
    pk2 = _Picker(fb, kws, skills, jaccard_limit, jd, extra_hits)
    pk2.chosen = [b for ids in picked.values() for b in ids]
    pk2.cover(pk2.chosen)
    for pid in projects:
        if layout.project_bullets is None:
            project_bullets[pid] = pk2.settle(own[pid])
            continue
        lo, hi = layout.project_bullets
        got: list[str] = []
        left = [b for b in own[pid]]
        while left and len(got) < hi:
            cand = [b for b in left if pk2.allowed(b) and not pk2.dup(b)]
            if not cand:
                break
            b = min(cand, key=lambda x: (-pk2.gain([x]), -pk2.sc[x], pk2.order[x]))
            if len(got) >= lo and pk2.gain([b]) <= 0:
                break
            b = pk2.prefer(b, set(cand))
            gone = pk2.take(b)
            got = [g for g in got if g not in gone] + [b]
            left = [x for x in left if x != b]
        if not got and own[pid]:
            first = next((b for b in own[pid] if pk2.allowed(b)), next((b for b in own[pid] if b not in pk2.retired), own[pid][0]))
            got.append(first)
            pk2.take(first)
        project_bullets[pid] = got
    return {"experience": picked, "projects": projects, "project_bullets": project_bullets}


def fill_order(fb: dict, kws: list[Keyword], sel: dict, skills: dict | None = None,
               jaccard_limit: float = JACCARD, max_projects: int | None = None, jd: str = "", layout: Layout | None = None,
               extra_hits: dict[str, frozenset[int]] | None = None) -> list[str]:
    """The bullets a selection has not used, in the order the two-page loop should add them while page 2 is not full (Document rules, A5):
      1. experience bullets of any entry: every role is raised to its maximum first (with a `layout`, no role goes past its maximum);
      2. the bullets of further projects, up to `max_projects` projects on the CV in all (scoring.cv_projects_max; None =
         no further project), the project adding the most keyword weight nobody covers yet first, then the better match;
      3. the remaining bullets of the chosen projects (with a `layout`, no project goes past its maximum).
    Inside a stage each step is the bullet adding the most uncovered keyword weight, then the more recent entry, more
    weighted overlap, fact-bank order. A bullet that nearly repeats a chosen one (or an earlier fill) is left out for
    good. Optional fact-bank sections and linespread come after this list is used up (tailor.fill_tail)."""
    skills = fb.get("skills") if skills is None else skills
    pk = _Picker(fb, kws, skills, jaccard_limit, jd, extra_hits)
    used = selected_ids(fb, sel)
    for b in used:
        pk.take(b)
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb.get("projects", [])}
    shown = list(sel["projects"])
    rank = {p: i for i, p in enumerate(rank_projects(fb, kws, None, jd))}
    extra: list[str] = []
    covered, dcovered = set(pk.covered), set(pk.dcovered)        # coverage as the picks go on, without taking bullets
    while len(shown) + len(extra) < (max_projects or 0):
        left = [p for p in own if p not in shown and p not in extra and any(b not in used for b in own[p])]
        if not left:
            break
        value = lambda p: pk.project_value(own[p], covered, dcovered)   # noqa: E731
        best = min(left, key=lambda p: (-value(p), rank.get(p, len(rank))))
        extra.append(best)
        covered |= set().union(*(pk.hits[b] for b in own[best]))
        dcovered |= set().union(*(pk.dgroups[b] for b in own[best]))
    stage = {p: 1 for p in extra} | {p: 2 for p in shown}
    pool = [(n, 0, b["id"]) for n, e in enumerate(fb["experience"]) for b in e["bullets"] if b["id"] not in used]
    pool += [(n, stage[p["id"]], b["id"]) for n, p in enumerate(fb.get("projects", [])) if p["id"] in stage
             for b in p["bullets"] if b["id"] not in used]
    owner = {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}
    room: dict[str, int] = {}                                     # bullets each entry can still take (only with a layout)
    if layout is not None:
        for n, e in enumerate(fb["experience"]):
            room[e["id"]] = max(0, (layout.first if n == 0 else layout.other)[1] - len(sel["experience"].get(e["id"]) or []))
        if layout.project_bullets:
            chosen = sel.get("project_bullets") or {}
            for p in fb.get("projects", []):
                room[p["id"]] = max(0, layout.project_bullets[1] - len(chosen.get(p["id"], own[p["id"]]) if p["id"] in shown else []))
    out: list[str] = []
    while pool:
        pool = [c for c in pool if room.get(owner[c[2]], 1) > 0 and pk.allowed(c[2], strict=True) and not pk.dup(c[2])]
        if not pool:
            break
        n, st, b = min(pool, key=lambda c: (c[1], -pk.gain([c[2]]), c[0], -pk.sc[c[2]], pk.order[c[2]]))
        b = pk.prefer(b, {c[2] for c in pool})
        pool = [c for c in pool if c[2] != b]
        out.append(b)
        pk.take(b)
        if owner[b] in room:
            room[owner[b]] -= 1
    return out


def selected_ids(fb: dict, sel: dict) -> list[str]:
    """Bullet ids of a selection in CV order: experience entries as listed, then each chosen project's bullets."""
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb["projects"]}
    chosen = sel.get("project_bullets") or {}
    return [i for e in fb["experience"] for i in sel["experience"].get(e["id"], [])] + \
           [i for p in sel["projects"] for i in chosen.get(p, own.get(p, []))]


def settle_selection(fb: dict, sel: dict, protect: str | None = None) -> tuple[dict, list[str]]:
    """A copy of `sel` in which no chosen bullet replaces another chosen one (the replaced bullet goes; when it is `protect`, the hook's
    proof bullet, the replacer goes instead) and no entry has two `situation_included` bullets (the first stays). Returns (selection,
    the ids dropped, in the order dropped). A project without an entry in `project_bullets` is listed with all its bullets first."""
    own = {p["id"]: [b["id"] for b in p["bullets"]] for p in fb.get("projects", [])}
    projects = list(sel["projects"])
    if len(projects) > MAX_PROJECTS:          # v3.9.4: the hook's project or a typed fact may be added to three chosen ones: never four on the CV
        owner_of = {b["id"]: p["id"] for p in fb.get("projects", []) for b in p["bullets"]}
        sacred = {owner_of[protect]} if protect in owner_of else set()
        sacred |= {p["id"] for p in fb.get("projects", []) if any(b.get("provisional") for b in p["bullets"])}
        for p in reversed(projects[:]):
            if len(projects) <= MAX_PROJECTS:
                break
            if p not in sacred:
                projects.remove(p)
    out = {"experience": {k: list(v) for k, v in sel["experience"].items()}, "projects": projects,
           "project_bullets": {p: list((sel.get("project_bullets") or {}).get(p, own.get(p, []))) for p in projects}}
    lists =[*out["experience"].values(), *out["project_bullets"].values()]
    present = {i for ids in lists for i in ids}
    dropped: list[str] = []

    def drop(bid: str) -> None:
        for ids in lists:
            ids[:] = [i for i in ids if i != bid]
        present.discard(bid)
        dropped.append(bid)

    for r, olds in replacers(fb).items():
        if r not in present:
            continue
        if protect and protect in olds and protect in present:
            drop(r)
            continue
        for old in sorted(olds & present, key=lambda i: [b["id"] for _, b in _bullets(fb)].index(i)):
            drop(old)
    situated, owner = situated_ids(fb), {b["id"]: e["id"] for e, b in _bullets(fb)}
    for ids in lists:
        opened = False
        for i in list(ids):
            if i in situated and owner[i] and opened:
                drop(i)
            opened = opened or i in situated
    return out, dropped


def missing_keywords(bullet: dict, kws: list[Keyword]) -> list[str]:
    """The JD words a bullet may take on: for each keyword the bullet's text does not already say, the first of its
    words that the bullet's `terms` evidence. A reworded bullet may use exactly these and no other keyword."""
    lit = norm_literal(bullet["text"])
    stems = [norm_stem(t) for t in bullet.get("terms", [])]
    out = []
    for k in kws:
        if any(has_literal(lit, norm_literal(w)) for w in k.members()):
            continue
        word = next((w for w in k.members() if any(_contains(s, norm_stem(w)) for s in stems)), None)
        if word:
            out.append(word)
    return out


def chunks(items: Sequence[T], n: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]
