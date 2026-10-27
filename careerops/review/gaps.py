"""The gap report: what the review screen shows, instead of the plain ATS header, for a job in the review band (50 <= strict S < 70) or the
"CV under-uses the fact bank" rescue case. Computed from the numbers the scorer already stored in report.json; no model, no PDF.

    ATS strict S | ceiling C | to reach 70 you need +D points

For every JD keyword the CV does not carry, its weight w (required 2, preferred 1, ... as scored) and its gain g = 100 x w / sum(w): the
points it adds to S if it gets onto the CV. Largest first, each marked

    claimable   the fact bank evidences it (the bullet or skill is named, and why it was left off: page full, rank, guard)
    gap         the fact bank does not: it needs a new fact, which only the user can supply (review's `f`)

The two sums split the points still to win: `claimable total` is what re-tailoring can win (C - S), `gap total` what needs a new fact
(100 - C). A term on the fact bank's deny list is a gap too, but it is never asked about: the user said they lack it.

Every row also carries an importance tier read from the ad's own text (review/tiers.py): `stated` (must-have wording), `structural` (in its Requirements
section, or repeated) or `inferred` (our weight guess). Rows are ranked tier first (stated, structural, inferred), then by gain, so an `inferred` keyword is
never ranked first, and only a stated or structural one is marked "top priority": when every missing keyword is inferred, no row is. Without the ad's text
(`jd` not given) every keyword is inferred.

Nothing here writes anything. The fact bank is read, never changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .. import bands
from ..tailoring.faithfulness import deny_hits
from ..tailoring.scorer import Keyword, _contains, _with_group, has_literal, norm_literal, norm_stem
from . import tiers

MAX_SKILLS, MAX_BULLETS = 2, 3         # evidence named per keyword
DENIED = "deny-listed: you said you lack it"


def gain_text(g: float) -> str:
    """A gain as printed: one decimal, halves up (1.25 -> 1.3, as in the example line)."""
    return str(Decimal(str(round(g, 6))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def applies(rep: dict, b: bands.Bands) -> bool:
    """Does this report get the gap report? The review band, or a rescue. A report from before the bands is judged by its own numbers."""
    outcome = (rep.get("band") or {}).get("outcome")
    if outcome is None:
        a = rep.get("ats") or {}
        if "strict" not in a:
            return False
        outcome = bands.classify(float(a["strict"]), float(a.get("ceiling", 0.0)), b).outcome
    return outcome in ("review", "rescue")


@dataclass
class Row:
    term: str
    weight: float
    required: bool
    gain: float
    kind: str                                   # claimable | gap
    denied: bool = False
    tier: str = tiers.INFERRED                  # stated | structural | inferred (review/tiers.py)
    skills: list[str] = field(default_factory=list)        # fact-bank skills that evidence it
    bullets: list[tuple[str, str]] = field(default_factory=list)        # (bullet id, why it is not on the CV, "" when it is not asked)
    note: str = ""                                          # evidence the fact bank has elsewhere (profile, an entry's stack)

    def line(self) -> str:
        head = f"+{gain_text(self.gain)}  {self.term} ({'required' if self.required else 'preferred'}, w={self.weight:g})  "
        if self.kind == "gap":
            return head + ("gap (" + DENIED + ")" if self.denied else "gap") + f"  [{self.tier}]"
        parts = [f'skills "{s}"' for s in self.skills]
        parts += [f"bullet {b}" + (f" (left off: {why})" if why else "") for b, why in self.bullets]
        return head + "claimable: " + (", ".join(parts) or self.note or "in the fact bank") + f"  [{self.tier}]"


@dataclass
class GapReport:
    strict: float
    ceiling: float
    target: float
    rows: list[Row]
    claimable_total: float
    gap_total: float
    possible: dict = field(default_factory=dict)

    @property
    def needed(self) -> float:
        return max(0.0, self.target - self.strict)

    @property
    def top(self) -> Row | None:
        """The top-priority row: the first row, unless it is inferred (the rows are ranked stated, structural, inferred, so then all are): None."""
        return self.rows[0] if self.rows and self.rows[0].tier != tiers.INFERRED else None

    def askable(self, n: int = 5) -> list[Row]:
        """The top `n` gap keywords by gain, for review's `f`: never a deny-listed one."""
        return [r for r in self.rows if r.kind == "gap" and not r.denied][:n]

    def claimable_terms(self) -> list[str]:
        return [r.term for r in self.rows if r.kind == "claimable"]

    def lines(self) -> list[str]:
        num = bands.num
        out = [f"ATS strict {num(self.strict)} | ceiling {num(self.ceiling)} | to reach {num(self.target)} you need +{num(self.needed)} points"]
        if self.rows:
            out.append("Missing from the CV (on CV? no), most important first; the gain is the points it adds if it gets onto the CV:")
            out.append("  Importance: stated = the ad's must-have wording; structural = in its Requirements section or repeated; "
                       "inferred = our weight guess (never the top priority).")
            out += ["  " + r.line() + (" <- top priority" if r is self.top else "") for r in self.rows]
        out.append(f"claimable total: +{num(self.claimable_total)} points (re-tailoring can win them)")
        out.append(f"gap total: +{num(self.gap_total)} points (each needs a new fact)")
        if self.needed:
            if self.claimable_total + bands.EPS >= self.needed:
                out.append(f"re-tailoring alone could reach {num(self.target)}  [r]")
            else:
                out.append(f"re-tailoring alone reaches {num(self.strict + self.claimable_total)}; the rest needs a new fact  [r] then [f]")
        if self.possible:
            out.append("Possible evidence — you decide (nothing is claimed; add the word to the fact bank if it is true):")
            for term, via in self.possible.items():
                out.append(f"  {term} <- " + "; ".join(f"{v['via']} ({', '.join(v['where'][:2])})" for v in via))
        return out


def _matcher(k: Keyword):
    """text -> does the fact bank's text carry the keyword the way the ceiling counts it (the keyword or an alternative, a keyword-group
    synonym, literal or stemmed)."""
    words = _with_group(k.members())
    lits, stems = [norm_literal(w) for w in words], [norm_stem(w) for w in words]

    def has(text: str) -> bool:
        lit, stm = norm_literal(text), norm_stem(text)
        return any(has_literal(lit, w) for w in lits) or any(_contains(stm, w) for w in stems)
    return has


def _entries(fb: dict):
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                yield b


def _chosen(rep: dict) -> list[str]:
    ctx = rep.get("context") or {}
    return [i for sec in ("experience", "projects") for e in ctx.get(sec) or [] for i in e.get("bullet_ids") or []]


def _printed_skills(rep: dict) -> str:
    skills = (rep.get("context") or {}).get("skills") or {}
    return norm_literal(" ".join(str(i) for items in skills.values() for i in ([items] if isinstance(items, str) else items)))


def _replacer(bid: str, chosen: list[str], fb: dict) -> str | None:
    """The chosen bullet that `replaces:` this one, if any."""
    by_id = {b["id"]: b for b in _entries(fb)}
    return next((c for c in chosen if bid in [str(x) for x in (by_id.get(c) or {}).get("replaces") or []]), None)


def _why_left_off(bid: str, term: str, rep: dict, chosen: list[str], fb: dict) -> str:
    """Why a fact-bank bullet that evidences `term` is not what puts it on the CV: space, rank or the guard."""
    if bid in chosen:
        blocked = [w.lower() for w in (rep.get("placement") or {}).get("blocked") or []]
        if term.lower() in blocked:
            return "guard, its rewording was refused"
        return "guard, it is on the CV but its text does not say it and a rewording may not add a word"
    heir = _replacer(bid, chosen, fb)
    if heir:
        return f"rank, {heir} replaces it"
    if bid in ((rep.get("fill") or {}).get("rejected_overflow") or []):
        return "page full"
    ranked = [b.get("id") for b in (rep.get("ranking") or {}).get("bullets") or []]
    return f"rank #{ranked.index(bid) + 1}" if bid in ranked else "rank, below the bullets chosen"


def _evidence(k: Keyword, rep: dict, fb: dict, chosen: list[str]) -> Row:
    has = _matcher(k)
    printed = _printed_skills(rep)
    skills: list[str] = []
    for items in (fb.get("skills") or {}).values():
        for item in items or []:
            s = str(item)
            if s not in skills and has(s):
                skills.append(s)
    bullets = [b for b in _entries(fb) if has(b["text"] + " " + " ".join(b.get("terms") or []))]
    bullets.sort(key=lambda b: not has(b["text"]))              # the bullets that say it in their own words first
    row = Row(k.term, k.weight, k.required, 0.0, "claimable")
    row.skills = [s for s in skills if not printed or has_literal(printed, norm_literal(s))][:MAX_SKILLS] or skills[:MAX_SKILLS]
    row.bullets = [(b["id"] + (" [provisional]" if b.get("provisional") else ""), _why_left_off(b["id"], k.term, rep, chosen, fb))
                   for b in bullets[:MAX_BULLETS]]
    if not (row.skills or row.bullets):
        row.note = "in the fact bank (profile facts or an entry's stack)"
    return row


def build(rep: dict, fb: dict, b: bands.Bands, jd: str | None = None) -> GapReport:
    """`jd` is the ad's text: it decides each row's tier. Without it every keyword is inferred."""
    kws = [Keyword(**k) for k in rep["keywords"]]
    tier_of = tiers.classify(jd or "", kws)
    tot = sum(k.weight for k in kws)
    ats = rep["ats"]
    hits = {t.lower() for t in ats.get("hits") or []}
    claimable = {t.lower() for t in rep.get("claimable") or []}
    deny = list(fb.get("deny_terms") or [])
    chosen = _chosen(rep)
    rows: list[Row] = []
    for k in kws:
        if k.term.lower() in hits:
            continue
        gain = 100.0 * k.weight / tot if tot else 0.0
        if k.term.lower() in claimable:
            row = _evidence(k, rep, fb, chosen)
            row.gain = gain
        else:
            row = Row(k.term, k.weight, k.required, gain, "gap", denied=bool(deny_hits(k.term, deny)))
        row.tier = tier_of.get(k.term, tiers.INFERRED)
        rows.append(row)
    rows.sort(key=lambda r: (tiers.TIERS.index(r.tier), -r.gain))      # tier first, then gain; sorted is stable: the JD's order within a tie
    return GapReport(float(ats["strict"]), float(ats["ceiling"]), b.auto, rows,
                     sum(r.gain for r in rows if r.kind == "claimable"), sum(r.gain for r in rows if r.kind == "gap"),
                     dict(rep.get("possible_evidence") or {}))
