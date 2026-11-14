"""The requirement-to-evidence map (v3.6): the ad's requirements pick the evidence, the role family only sets the title.

Ported from career-ops `modes/pdf.md` (ext/career-ops @ 605a1e61, steps 1-11) and `jd-skill-gap.mjs`: the logic, not the text.

    1. the keywords the analysis extracted are the requirements (one requirement per Keyword, same index);
    2. each is looked up in the WHOLE fact bank: bullets (text, result, terms), `skills:`, `profile_facts`, `profile_fragments`, education
       `modules`, certifications. The lookup uses the term, its `alts` and the user's `jd_aliases:` (JD wording -> words already in the file);
    3. it lands in a bucket:  existing  = evidence in a bullet or its result;
                              supported = evidence only in skills, profile_facts, fragments, modules or certifications;
                              gap       = nothing found (a deny-listed term is ALWAYS a gap);
    4. coverage = (sum w existing + 0.5 x sum w supported) / sum w.

Everything here is code: zero tokens, deterministic (the same ad and bank give the same map), and it never writes a word of its own. The
model never creates an alias; `alias_suggestions` only NAMES JD phrases that have none, for the user to add if they are true. A gap is printed
before tailoring and nothing is generated for it: no skill row, no profile fragment, no hook, no letter sentence draws on one.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .faithfulness import deny_hits
from .scorer import Keyword, has_literal, norm_literal, norm_stem
from .skills import covers, line_items, row_repeats

EXISTING, SUPPORTED, GAP = "existing", "supported", "gap"
SUPPORTED_CREDIT = 0.5                 # a requirement the bank backs only by a skill / fact / module counts half toward coverage
MIN_HOOK_REQS = 2                      # a hook must cover at least this many `existing` requirements
FILENAME = "reqmap.json"
MAX_FRAGMENTS = 3                      # profile fragments used (the best 2-3)
MAX_SKILL_ROWS = 4                     # Skills categories on the CV
JD_ROWS = 3                            # ... of which at most this many are named with the ad's own words
MIN_ROW_ITEMS = 2                      # a JD-worded row needs this many distinct evidence items
SHINGLE = 4                            # `profile_fragments` that share this many consecutive words repeat each other (doctor warns)


@dataclass
class Evidence:
    kind: str          # bullet | skills | profile_facts | fragment | modules | certification
    id: str            # a bullet id, "skills:<group>", "profile_facts:<n>", a fragment id, "modules:<degree>", "certification:<n>"
    quote: str         # the exact words of the evidence that matched (as the user wrote them)
    snippet: str = ""  # the first words of the evidence, for the review line
    via: str = ""      # the word that matched: the term itself, or the alias


@dataclass
class Req:
    index: int
    term: str
    weight: float
    importance: str                    # stated (the ad says it is required) | structural (in the ad's title) | inferred
    bucket: str = GAP
    evidence: list[Evidence] = field(default_factory=list)
    alts: list[str] = field(default_factory=list)       # the any-of words the ad offered instead (Keyword.alts)

    def bullet_ids(self) -> list[str]:
        return [e.id for e in self.evidence if e.kind == "bullet"]


@dataclass
class ReqMap:
    reqs: list[Req]
    coverage: float = 0.0

    def of(self, bucket: str) -> list[Req]:
        return [r for r in self.reqs if r.bucket == bucket]

    def gaps(self) -> list[str]:
        return [r.term for r in self.of(GAP)]

    def to_dict(self) -> dict:
        return {"coverage": round(self.coverage, 4),
                "requirements": [{"term": r.term, "alts": r.alts, "weight": r.weight, "importance": r.importance, "bucket": r.bucket,
                                  "evidence": [asdict(e) for e in r.evidence]} for r in self.reqs]}

    @staticmethod
    def from_dict(d: dict) -> "ReqMap":
        reqs = [Req(i, x["term"], x["weight"], x.get("importance", "inferred"), x["bucket"], [Evidence(**e) for e in x.get("evidence") or []],
                    list(x.get("alts") or [])) for i, x in enumerate(d.get("requirements") or [])]
        return ReqMap(reqs, float(d.get("coverage") or 0.0))


# ------------------------------------------------------------------------------------------------------------------ matching
def _key(text: str) -> str:
    return norm_stem(text)


def aliases_of(fb: dict) -> dict[str, list[str]]:
    """{normalised JD phrase: its evidence words}, from the fact bank's `jd_aliases` (the user's own block; never written by code)."""
    out: dict[str, list[str]] = {}
    for k, v in (fb.get("jd_aliases") or {}).items():
        words = [str(w).strip() for w in (v if isinstance(v, (list, tuple)) else [v]) if str(w).strip()]
        out[_key(str(k))] = words
    return out


def _span(text: str, word: str) -> str:
    """The words of `text` that match `word` (case and plural free), as written in `text`; '' when it is not there."""
    low = norm_literal(text)
    if not has_literal(low, norm_literal(word)):
        return ""
    base = re.escape(" ".join(str(word).split()))
    base = base.replace(r"\ ", r"[\s-]+")
    m = re.search(r"(?<![\w])" + base + r"(?:s|es)?(?![\w])", text, re.I)
    if m is None:                                   # matched only through norm_literal (a dash, a quote): quote the word itself
        return " ".join(str(word).split())
    return m.group(0)


def _first_words(text: str, n: int = 28) -> str:
    t = " ".join(str(text).split())
    if len(t) <= n:
        return t
    cut = t[:n]
    return (cut.rsplit(" ", 1)[0] if t[n] != " " and " " in cut else cut).rstrip() + "…"


def _members(req_term: str, alts: Sequence[str], aliases: dict[str, list[str]]) -> list[str]:
    """The words that count as evidence of one requirement, in order: the term, its alts, then the aliases of the term and of each alt."""
    words = [req_term, *alts]
    for w in [req_term, *alts]:
        words.extend(aliases.get(_key(w), []))
    return list(dict.fromkeys(w for w in words if w.strip()))


def _bank_sources(fb: dict) -> tuple[list[tuple[str, str, str, str, str]], list[tuple[str, str, str, str]]]:
    """(bullets, others). bullets = (id, text, result, terms, snippet source); others = (kind, id, text, snippet)."""
    bullets: list[tuple[str, str, str, str, str]] = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                bullets.append((str(b["id"]), str(b.get("text") or ""), str(b.get("result") or ""),
                                " ".join(str(t) for t in b.get("terms") or []), str(b.get("text") or "")))
    others: list[tuple[str, str, str, str]] = []
    for group, items in (fb.get("skills") or {}).items():
        for item in items if isinstance(items, (list, tuple)) else [items]:
            others.append(("skills", f"skills:{group}", str(item), str(item)))
    for n, fact in enumerate(fb.get("profile_facts") or []):
        others.append(("profile_facts", f"profile_facts:{n}", str(fact), str(fact)))
    for f in fb.get("profile_fragments") or []:
        if isinstance(f, dict) and f.get("id"):
            others.append(("fragment", str(f["id"]), str(f.get("text") or ""), str(f.get("text") or "")))
    for ed in fb.get("education") or []:
        for m in ed.get("modules") or []:
            others.append(("modules", f"modules:{ed.get('degree', '')}", str(m), str(m)))
    for n, c in enumerate(fb.get("certifications") or []):
        others.append(("certification", f"certification:{n}", str(c), str(c)))
    return bullets, others


def find_evidence(words: Sequence[str], bullets, others) -> tuple[list[Evidence], list[Evidence]]:
    """(in bullets, elsewhere): every source that carries one of `words`. A bullet's text and result come first, its `terms` after."""
    in_bullets: list[Evidence] = []
    for bid, text, result, terms, snip in bullets:
        hit = next(((w, s) for w in words for src in (text, result) if (s := _span(src, w))), None)
        if hit is None:
            hit = next(((w, s) for w in words if (s := _span(terms, w))), None)
        if hit:
            in_bullets.append(Evidence("bullet", bid, hit[1], _first_words(snip, 26), hit[0]))
    elsewhere: list[Evidence] = []
    for kind, eid, text, snip in others:
        hit = next(((w, s) for w in words if (s := _span(text, w))), None)
        if hit:
            elsewhere.append(Evidence(kind, eid, hit[1], _first_words(snip, 60), hit[0]))
    return in_bullets, elsewhere


def build(kws: Sequence[Keyword], fb: dict, title: str = "") -> ReqMap:
    """The map of this ad's requirements (`kws`, one requirement each, same index) against the whole fact bank."""
    aliases, deny = aliases_of(fb), fb.get("deny_terms", [])
    bullets, others = _bank_sources(fb)
    low_title = norm_literal(title or "")
    reqs: list[Req] = []
    for i, k in enumerate(kws):
        importance = "stated" if k.required else ("structural" if any(has_literal(low_title, norm_literal(w)) for w in k.members()) else "inferred")
        r = Req(i, k.term, float(k.weight), importance, alts=list(k.alts))
        if not deny_hits(" ".join(k.members()), deny):          # a deny-listed term is a gap, whatever the aliases say
            words = _members(k.term, k.alts, aliases)
            inb, elsewhere = find_evidence(words, bullets, others)
            r.evidence = inb + elsewhere
            r.bucket = EXISTING if inb else SUPPORTED if elsewhere else GAP
        reqs.append(r)
    return ReqMap(reqs, coverage_of(reqs))


def coverage_of(reqs: Iterable[Req]) -> float:
    reqs = list(reqs)
    total = sum(r.weight for r in reqs)
    if total <= 0:
        return 0.0
    got = sum(r.weight for r in reqs if r.bucket == EXISTING) + SUPPORTED_CREDIT * sum(r.weight for r in reqs if r.bucket == SUPPORTED)
    return got / total


# ------------------------------------------------------------------------------------------------------------------ files and text
def write(rm: ReqMap, out_dir: Path) -> Path:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / FILENAME
    path.write_text(json.dumps(rm.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def gap_lines(rm: ReqMap) -> list[str]:
    """What is printed BEFORE tailoring (career-ops step 4): the gaps, which nothing is generated for."""
    gaps = rm.gaps()
    head = f"requirements: coverage {rm.coverage:.2f} ({len(rm.of(EXISTING))} existing, {len(rm.of(SUPPORTED))} supported, {len(gaps)} gap)"
    return [head] + ([f"  GAP (nothing is written for these): {', '.join(gaps)}"] if gaps else [])


def review_line(r: Req) -> str:
    """`existing az5 "Surveyed and explored 14…"` | `supported skills "hypothesis testing"` | `GAP structured outputs`."""
    if r.bucket == GAP:
        return f"GAP {r.term}"
    e = r.evidence[0]
    shown = e.snippet if e.kind == "bullet" else e.quote
    where = e.id.split(":")[0] if e.kind != "bullet" else e.id
    return f'{r.bucket} {where} "{shown}"'


def review_block(rm: ReqMap, risks: list[str] | None = None, suggestions: list[str] | None = None) -> list[str]:
    """The REQUIREMENTS block of the review screen and the report: one line per requirement, alias suggestions, the recruiter risk map."""
    out = [f"REQUIREMENTS (coverage {rm.coverage:.2f})"]
    out += [f"  {r.term}: {review_line(r)}" for r in sorted(rm.reqs, key=lambda r: (-r.weight, r.index))]
    if suggestions:
        out += ["alias suggestions (a suggestion only: add it to `jd_aliases` in fact_bank.yaml yourself if it is true):"] + [f"  {s}" for s in suggestions]
    if risks:
        out += ["recruiter risk map:"] + [f"  {x}" for x in risks]
    return out


# ------------------------------------------------------------------------------------------------------------------ bullets
def bullet_coverage(rm: ReqMap) -> dict[str, frozenset[int]]:
    """{bullet id: indexes of the `existing` requirements it is evidence of}. Added to the selection's own keyword hits."""
    out: dict[str, set[int]] = {}
    for r in rm.of(EXISTING):
        for bid in r.bullet_ids():
            out.setdefault(bid, set()).add(r.index)
    return {b: frozenset(v) for b, v in out.items()}


def bullet_scores(rm: ReqMap) -> dict[str, float]:
    """{bullet id: sum of the weights of the `existing` requirements it covers}, summed in index order (a float sum must not depend on a hash seed)."""
    w = {r.index: r.weight for r in rm.reqs}
    return {b: round(sum(w[i] for i in sorted(ix)), 6) for b, ix in bullet_coverage(rm).items()}


def blend(rel: dict[str, float], rm: ReqMap) -> dict[str, float]:
    """The relevance scores with each bullet's requirement coverage added: a bullet that covers nothing the ad asks for keeps only its own score."""
    add = bullet_scores(rm)
    return {b: round(s + add.get(b, 0.0), 6) for b, s in rel.items()}


# ------------------------------------------------------------------------------------------------------------------ hook
def _proofs(entry: dict) -> list[str]:
    raw = entry.get("proof")
    return list(dict.fromkeys(str(x).strip() for x in (raw if isinstance(raw, (list, tuple)) else [raw]) if x is not None and str(x).strip()))


def hook_cover(rm: ReqMap, entry: dict) -> list[Req]:
    """The `existing` requirements the entry's proof bullets are evidence of."""
    proofs = set(_proofs(entry))
    return [r for r in rm.of(EXISTING) if proofs & set(r.bullet_ids())]


def hook_scores(rm: ReqMap, fb: dict) -> dict[str, tuple[float, int]]:
    """{hook_library id: (sum w of the existing requirements its proof bullets cover, how many)}."""
    out: dict[str, tuple[float, int]] = {}
    for e in fb.get("hook_library") or []:
        if isinstance(e, dict) and e.get("id"):
            cov = hook_cover(rm, e)
            out[str(e["id"])] = (round(sum(r.weight for r in sorted(cov, key=lambda r: r.index)), 6), len(cov))
    return out


def best_proof_bullet(rm: ReqMap, fb: dict) -> dict | None:
    """With no qualifying hook: the bullet that covers the most weight (a bullet with a result first on a tie), as {"id", "text", "score"}."""
    scores = bullet_scores(rm)
    if not scores:
        return None
    by_id = {str(b["id"]): b for sec in ("experience", "projects") for e in fb.get(sec) or [] for b in e.get("bullets") or []}
    order = {b: i for i, b in enumerate(by_id)}
    best = max((b for b in scores if b in by_id), key=lambda b: (scores[b], bool(str(by_id[b].get("result") or "").strip()), -order[b]), default=None)
    return {"id": best, "text": " ".join(str(by_id[best]["text"]).split()), "score": scores[best]} if best else None


# ------------------------------------------------------------------------------------------------------------------ profile
def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm_literal(text))


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def fragments(fb: dict) -> list[dict]:
    return [f for f in fb.get("profile_fragments") or [] if isinstance(f, dict) and f.get("id") and str(f.get("text") or "").strip()]


def pick_fragments(rm: ReqMap, fb: dict, avoid: str = "", limit: int = MAX_FRAGMENTS) -> list[dict]:
    """The 2-3 `profile_fragments` that add the most requirement weight, best first, none twice and none that repeats `avoid` (the hook's profile line)
    or an earlier pick (SHINGLE consecutive words). A fragment covers a requirement when its text or `terms` carry the requirement's words (term,
    alts, aliases) and the requirement is not a gap. [] when no fragment covers anything: the caller then uses the family's profile_body."""
    aliases = aliases_of(fb)
    live = [r for r in rm.reqs if r.bucket != GAP]
    members = {r.index: _members(r.term, r.alts, aliases) for r in live}
    cover: dict[str, set[int]] = {}
    for f in fragments(fb):
        hay = str(f.get("text") or "") + " " + " ".join(str(t) for t in f.get("terms") or [])
        cover[str(f["id"])] = {r.index for r in live if any(_span(hay, w) for w in members[r.index])}
    w = {r.index: r.weight for r in rm.reqs}
    picked: list[dict] = []
    seen: set[int] = set()
    used = shingles(avoid)
    pool = fragments(fb)
    while pool and len(picked) < limit:
        def gain(f: dict) -> float:
            return sum(w[i] for i in sorted(cover[str(f["id"])] - seen))
        best = max(pool, key=lambda f: (gain(f), -pool.index(f)))
        pool = [f for f in pool if f is not best]
        if gain(best) <= 0:
            break
        if shingles(str(best["text"])) & used:
            continue
        picked.append(best)
        seen |= cover[str(best["id"])]
        used |= shingles(str(best["text"]))
    return picked


# ------------------------------------------------------------------------------------------------------------------ skills
def _phrase_label(term: str) -> str:
    t = " ".join(str(term).split())
    return t[:1].upper() + t[1:]


def _top_level_pieces(line: str) -> list[str]:
    """A family skill line cut at the commas outside brackets (the user's own items, whole)."""
    out, depth, cur = [], 0, ""
    for ch in str(line):
        depth += ch in "(["
        depth -= ch in ")]" and depth > 0
        if ch == "," and not depth:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [p for p in out if p]


def skill_rows(rm: ReqMap, fb: dict, family_lines: dict[str, str] | None = None, jd_rows: int = JD_ROWS, max_rows: int = MAX_SKILL_ROWS) -> dict[str, str] | None:
    """The CV's Skills rows built from the ad's requirements: up to `jd_rows` rows NAMED with a requirement's own words (the JD phrase, verbatim),
    each listing the evidence words that back it (the term and its aliases, as the user wrote them in the bank), then the family's own `skill_lines`
    as the fallback rows (one best row per remaining requirement weight, a skill never twice). None when no requirement has enough evidence
    for a row, or the section would have fewer than three rows: the caller keeps the family's lines whole. A gap never reaches a row."""
    aliases = aliases_of(fb)
    bullets, others = _bank_sources(fb)
    rows: dict[str, list[str]] = {}
    seen: set[str] = set()
    for r in sorted(rm.reqs, key=lambda r: (-r.weight, r.index)):
        if r.bucket == GAP or len(rows) >= jd_rows:
            continue
        # a row's items are the evidence words as written in the bank: the term itself and its aliases, not the alts of another requirement
        found = []
        for w in _members(r.term, r.alts, aliases):
            inb, elsewhere = find_evidence([w], bullets, others)
            if inb or elsewhere:
                found.append((inb or elsewhere)[0].quote)
        items: list[str] = []
        for it in dict.fromkeys(found):
            keys = covers(it)
            if keys and not keys & seen:
                items.append(it)
                seen |= keys
        if len(items) >= MIN_ROW_ITEMS:
            rows[_phrase_label(r.term)] = items
        else:                                                   # not a row: the words stay free for a later requirement
            for it in items:
                seen -= covers(it)
    if not rows:
        return None
    out = {label: ", ".join(items) for label, items in rows.items()}
    for label, line in (family_lines or {}).items():
        if len(out) >= max_rows:
            break
        pieces = [p for p in _top_level_pieces(line) if not covers(p) & seen and not _inside(p, seen)]
        if pieces:
            out[label] = ", ".join(pieces)
            for p in pieces:
                seen |= covers(p)
    # whatever still repeats (an item inside a bracket) leaves the later row, never the earlier one
    for _ in range(20):
        rep = row_repeats(out)
        if not rep:
            break
        target = covers(rep[0].name)
        pieces = _top_level_pieces(out.get(rep[0].second, ""))
        kept = [p for p in pieces if not {k for it in line_items(p) for k in covers(it)} & target]
        if len(kept) == len(pieces):
            return None
        if kept:
            out[rep[0].second] = ", ".join(kept)
        else:
            del out[rep[0].second]
    return out if len(out) >= 3 else None


def _inside(piece: str, seen: set[str]) -> bool:
    """Does a family piece name (in its brackets) a skill already shown? Then the piece would repeat it."""
    return bool({k for it in line_items(piece) for k in covers(it)} & seen)


# ------------------------------------------------------------------------------------------------------------------ the letter template
def template_cover(rm: ReqMap, text: str, fb: dict | None = None) -> float:
    """The sum of the weights of the non-gap requirements a letter template's own words claim (term, alts or alias present in its text)."""
    aliases = aliases_of(fb or {})
    return round(sum(r.weight for r in sorted(rm.reqs, key=lambda r: r.index)
                     if r.bucket != GAP and any(_span(text, w) for w in _members(r.term, r.alts, aliases))), 6)


def template_order(rm: ReqMap, fb: dict, families: Sequence[str]) -> list[str]:
    """`families` re-ordered for the letter template lookup: the family whose `letter_template` claims the most requirement weight first, a tie to
    the family earlier in `families` (the one the title chose). A family with no template keeps its place after those that have one."""
    by_id = {str(f.get("id")): f for f in fb.get("families") or [] if isinstance(f, dict)}
    top = fb.get("letter_templates") or {}
    base = list(dict.fromkeys(x for x in families if x))

    def text(f: str) -> str:
        return str((top.get(f) if isinstance(top, dict) else None) or (by_id.get(f) or {}).get("letter_template") or "")

    have = [f for f in base if text(f).strip()]
    ranked = sorted(have, key=lambda f: (-template_cover(rm, text(f), fb), base.index(f)))
    return ranked + [f for f in base if f not in have]


# ------------------------------------------------------------------------------------------------------------------ the ad's title
def title_family(fb: dict, title: str) -> str | None:
    """The bank family named by the ad's TITLE words ("Junior Data Scientist" -> data_scientist), from each family's own `title`: nothing in the
    ad's body decides it. None when the title names no family or several (the caller keeps its own rule). The ad's title is never copied."""
    low = norm_literal(title or "")
    hits = [str(f["id"]) for f in fb.get("families") or []
            if isinstance(f, dict) and f.get("id") and str(f.get("title") or "").strip() and has_literal(low, norm_literal(str(f["title"])))]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------------------------------------------------------ risks, suggestions, doctor
def alias_suggestions(rm: ReqMap, fb: dict, proposed: dict[str, str] | None = None) -> list[str]:
    """JD phrases that are a gap, have no alias, and whose substance may be in the bank: a SUGGESTION for the user to add to `jd_aliases` if it is
    true. `proposed` ({phrase: bank word}) is what a model suggested; without it, a gap phrase with one long word (5+ letters) that the bank does
    carry is offered with that word. Each is shown only if its word really is in the fact bank, never for a deny-listed term, and the pipeline never
    writes it."""
    have = aliases_of(fb)
    bullets, others = _bank_sources(fb)
    deny = fb.get("deny_terms", [])
    out = []
    for r in sorted(rm.of(GAP), key=lambda r: (-r.weight, r.index)):
        if _key(r.term) in have or deny_hits(r.term, deny):
            continue
        cands = [(proposed or {}).get(r.term)] if (proposed or {}).get(r.term) else [w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", r.term)
                                                                                    if w.lower() not in _STOP]
        for word in cands:
            inb, elsewhere = find_evidence([word], bullets, others)
            if inb or elsewhere:
                out.append(f'jd_aliases: "{r.term}": [{word}]   (suggestion: "{word}" is in {(inb or elsewhere)[0].id})')
                break
    return out


_STOP = frozenset("using based data experience strong knowledge ability working related skills understanding".split())


def risk_map(rm: ReqMap, ad_title: str, years: str = "", cv_text: str = "", sections: dict[str, str] | None = None) -> list[str]:
    """The top 3 doubts a recruiter would have, each with the section that answers it or "unanswered": the heaviest GAP requirements first, then a
    stated requirement the bank backs only by a skill. `sections` = {"profile": text, "skills": text, ...}; a doubt is answered where its term appears."""
    sections = sections or {}
    doubts: list[tuple[str, str]] = []
    for r in sorted(rm.of(GAP), key=lambda r: (-r.weight, r.index)):
        doubts.append((f"missing stack: {r.term}", r.term))
    for r in sorted(rm.of(SUPPORTED), key=lambda r: (-r.weight, r.index)):
        if r.importance == "stated":
            doubts.append((f"only listed, never shown in a bullet: {r.term}", r.term))
    out = []
    for text, term in doubts[:3]:
        where = next((s for s, t in sections.items() if has_literal(norm_literal(t), norm_literal(term))), None)
        out.append(f"{text} -> {where or 'unanswered'}")
    return out


def dead_aliases(fb: dict) -> list[str]:
    """Doctor: `jd_aliases` entries none of whose evidence words appear anywhere in the fact bank (they can never match)."""
    bullets, others = _bank_sources(fb)
    out = []
    for phrase, words in (fb.get("jd_aliases") or {}).items():
        ws = [str(w) for w in (words if isinstance(words, (list, tuple)) else [words])]
        inb, elsewhere = find_evidence(ws, bullets, others)
        if not (inb or elsewhere):
            out.append(f"jd_aliases '{phrase}': none of {', '.join(ws) or '(no words)'} appears in the fact bank (a dead alias)")
    return out


def fragment_repeats(fb: dict) -> list[str]:
    """Doctor: `profile_fragments` that share SHINGLE or more consecutive words with another fragment."""
    frs = fragments(fb)
    out = []
    for i, a in enumerate(frs):
        for b in frs[i + 1:]:
            if shingles(str(a["text"])) & shingles(str(b["text"])):
                out.append(f"profile_fragments '{a['id']}' and '{b['id']}' share {SHINGLE} or more consecutive words: one profile would repeat itself")
    return out
