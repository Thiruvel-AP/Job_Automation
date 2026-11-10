"""Hook consistency (v3.5.2, 10 Nov 2026): a hook_library entry may claim nothing its proof bullets do not. A hook that does is BLOCKED (hook.py
skips it, so no CV headline, profile sentence or letter opening uses it) and the two quotes are shown, the hook's and the bullet's, for the user to
fix in fact_bank.yaml. Read only: nothing is reworded for the user.

The proof bullets of an entry: v3.5.3 (10 Nov 2026), `proof:` may be a LIST of bullet ids (`proof: [az10, az11]`); then exactly the listed bullets
are the proof, and a claim passes when ANY of them (its text, `result:` or `limits:`) supports it. A single id (`proof: dc2`) keeps the older scope: the
bullets of the role or project that bullet belongs to. Each text field of the entry (headline, profile_line, letter_line and their plain_ twins) is
checked against their text, `result` and `limits`:

  number      every number of the hook is in a proof bullet ("~65", "11,459", "16.2%"); a number of the organisation's own name ("180 Degrees")
              is not a claim
  name        every name the hook gives (a capitalised word that is not the sentence's first: "Lambda", "Bedrock") is in a proof bullet or is the
              organisation's, title's or project's own name
  outcome     an outcome is in a proof bullet: production, live, in use, adopted each as itself; shipped / deployed / launched / released / rolled
              out as one ("took an agent from prototype to production" needs a proof bullet that says production)
  ownership   when a proof bullet says contributed / helped / supported ..., no field claims a stronger verb (built, led, shipped ...) that no
              proof bullet says; and when the proof bullet that carries the outcome the hook claims says such a word, a hook field in the first person (I, my) keeps
              it ("my prototype went into production" vs dc8 "contributed to their production TypeScript/Node.js MCP server")
  credit      a rating or commendation keeps the subject the proof gives it ("the engagement received a 5-star commendation": the hook names the
              engagement too)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ..hook import proof_ids
from .faithfulness import NUM, numbers
from .grounding import _names, name_head, sentences
from .scorer import norm_literal, norm_stem

FIELDS = ("headline", "profile_line", "letter_line", "plain_headline", "plain_profile_line", "plain_letter_line")
OUTCOME_WORDS = ("production", "live", "shipped", "deployed", "launched", "released", "adopted", "in use", "rolled out")
SAME_OUTCOME = frozenset({"shipped", "deployed", "launched", "released", "rolled out"})     # one claim: it was put out
WEAK = ("contributed", "contributing", "contribution", "helped", "helping", "supported", "supporting", "assisted", "assisting", "participated",
        "took part", "collaborated")
STRONG = ("built", "led", "owned", "designed", "created", "developed", "delivered", "architected", "drove", "launched", "implemented", "shipped",
          "spearheaded", "founded", "engineered", "deployed", "headed", "directed", "oversaw")
CREDIT = re.compile(r"\b(?:commendation|rating|rated|award|prize|praise|stars?|accolade)\b", re.I)
CREDIT_SUBJECT = re.compile(r"\b([A-Za-z]+)\s+(?:received|earned|won|got|gained|was given|was awarded|was rated)\b[^.;]*?"
                            r"\b(?:commendation|rating|award|prize|praise|accolade)\b", re.I)
COMMON = frozenset("i a an the uk us eu ai ml llm llms msc cv".split())
FIRST_PERSON = re.compile(r"\b(?:I|my|me|mine)\b")


@dataclass(frozen=True)
class Conflict:
    hook_id: str
    field: str
    kind: str                       # number | name | outcome | ownership | credit
    what: str                       # what the hook claims that the proof bullets do not, as a sentence
    hook_quote: str
    bullet_id: str
    bullet_quote: str

    def lines(self) -> list[str]:
        return [f"{self.hook_id}.{self.field} ({self.kind}): {self.what}",
                f'    hook   : "{self.hook_quote}"',
                f'    {self.bullet_id:<7}: "{self.bullet_quote}"']

    def line(self) -> str:
        return f"{self.what} ({self.field}: \"{self.hook_quote[:90]}\" vs {self.bullet_id}: \"{self.bullet_quote[:90]}\")"


def _bullets(fb: Mapping) -> dict[str, tuple[dict, dict]]:
    return {str(b.get("id")): (b, e) for sec in ("experience", "projects") for e in fb.get(sec) or [] for b in e.get("bullets") or []}


def proof_bullets(fb: Mapping, entry: Mapping) -> list[tuple[dict, dict]]:
    """[(bullet, its entry)]. A list `proof:` (v3.5.3): the listed bullets that are in the bank, in the listed order. A single id: that bullet first,
    then the other bullets of its role or project (in bank order)."""
    index = _bullets(fb)
    if isinstance(entry.get("proof"), (list, tuple)):
        return [index[b] for b in proof_ids(dict(entry)) if b in index]
    first = str(entry.get("proof") or "")
    if first not in index:
        return []
    owner = index[first][1]
    return [index[first]] + [(b, owner) for b in owner.get("bullets") or [] if str(b.get("id")) != first]


def _owners(proofs: list[tuple[dict, dict]]) -> list[dict]:
    return list({id(e): e for _, e in proofs}.values())


def _text(b: Mapping) -> str:
    return " ".join(str(b.get(k) or "") for k in ("text", "result", "limits"))


def _says(lit: str, word: str) -> bool:
    w = norm_literal(word)
    return re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", lit) is not None


def _stem_says(stems: str, word: str) -> bool:
    w = norm_stem(word)
    return bool(w) and re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", stems) is not None


def conflicts(fb: Mapping, entry: Mapping) -> list[Conflict]:
    """Every claim of the hook entry that its proof bullets do not make (empty: the hook is consistent)."""
    proofs = proof_bullets(fb, entry)
    if not proofs:
        return []
    hid = str(entry.get("id"))
    primary = proofs[0][0]
    texts = {str(b["id"]): _text(b) for b, _ in proofs}
    pool = " ".join(texts.values())
    lit, stems = norm_literal(pool), norm_stem(pool)
    names = [str(o.get(k) or "") for o in _owners(proofs) for k in ("org", "display_org", "title", "name", "display_name") if o.get(k)]
    own = norm_literal(" ".join(names))
    nums = numbers(pool)
    weak_bullets = [(str(b["id"]), w, norm_literal(_text(b))) for b, _ in proofs for w in WEAK if _says(norm_literal(_text(b)), w)]
    credit = next(((str(b["id"]), m.group(1)) for b, _ in proofs for m in [CREDIT_SUBJECT.search(_text(b))] if m), None)
    out: list[Conflict] = []

    def add(field: str, kind: str, what: str, quote: str, bid: str | None = None) -> None:
        b = str(bid or primary["id"])
        out.append(Conflict(hid, field, kind, what, quote, b, " ".join(texts.get(b, _text(primary)).split())))

    for field in FIELDS:
        value = " ".join(str(entry.get(field) or "").split())
        if not value:
            continue
        bare = value
        for name in sorted(names, key=len, reverse=True):                    # "180 Degrees Consulting": the name's number is not a claim
            bare = re.sub(re.escape(name.split(",")[0].strip()), " ", bare, flags=re.I) if name.strip() else bare
        bare = re.sub(r"\b180 Degrees\b", " ", bare) if "180 degrees" in own else bare
        for n in sorted(numbers(bare) - nums):
            add(field, "number", f"gives the number {n}, which no proof bullet holds", value)
        for s in sentences(value):
            for name in _names(s):
                n, head = norm_literal(name), name_head(name)
                if n in COMMON or _says(lit, name) or _stem_says(stems, name) or (n and _says(own, name)):
                    continue
                if head != name and (_says(lit, head) or _says(own, head)):      # "AstraZeneca-affiliated": the name part is the employer's
                    continue
                add(field, "name", f"names '{name}', which no proof bullet says", value)
        vlit = norm_literal(value)
        for word in OUTCOME_WORDS:
            if not _says(vlit, word):
                continue
            same = SAME_OUTCOME if word in SAME_OUTCOME else {word}
            if not any(_says(lit, w) for w in same):
                add(field, "outcome", f"says '{word}', which no proof bullet says", value)
        up = [v for v in STRONG if _says(vlit, v) and not _says(lit, v) and not (v in SAME_OUTCOME and any(_says(lit, w) for w in SAME_OUTCOME))]
        if up and weak_bullets:                                             # ownership inflation: the proof says contributed / helped ...
            add(field, "ownership", f"claims '{up[0]}' where the proof bullet says '{weak_bullets[0][1]}'", value, weak_bullets[0][0])
        claimed = [w for w in OUTCOME_WORDS if _says(vlit, w)]
        for bid, w, blit in weak_bullets:                                   # the bullet that carries the claimed outcome keeps its weak verb
            if FIRST_PERSON.search(value) and any(_says(blit, c) for c in claimed) and not any(_says(vlit, x) for x in WEAK):
                add(field, "ownership", f"claims the outcome as its own and drops '{w}', which the proof bullet says", value, bid)
                break
        if credit is not None and CREDIT.search(value) and not _says(vlit, credit[1]):
            add(field, "credit", f"gives the rating to something other than the '{credit[1]}', which the proof bullet credits", value, credit[0])
    return out


def _quote(text: str, needle: str, span: int = 7) -> str:
    """About `span` words either side of the first place `text` says `needle` (case-insensitive), with ... where it was cut."""
    words = " ".join(str(text).split()).split(" ")
    flat = [norm_literal(w) for w in words]
    target = norm_literal(needle).split()
    at = next((i for i in range(len(flat)) if target and all(j + i < len(flat) and target[j] in flat[i + j] for j in range(len(target)))), None)
    if at is None:
        return " ".join(words[:2 * span]) + (" ..." if len(words) > 2 * span else "")
    a, b = max(0, at - span), min(len(words), at + len(target) + span)
    return ("... " if a else "") + " ".join(words[a:b]) + (" ..." if b < len(words) else "")


def supports(fb: Mapping, entry: Mapping) -> list[tuple[str, str, str]]:
    """[(claim, bullet id, quote)]: for every number, name and outcome word the entry's fields claim, the first proof bullet (listed order) that
    says it and the words around it there. The review of a hook that passes (`careerops hook-check`); a claim no bullet says is a Conflict instead."""
    proofs = proof_bullets(fb, entry)
    fields = [" ".join(str(entry.get(f) or "").split()) for f in FIELDS]
    claims: list[str] = [m.group(0).strip() for v in fields for m in NUM.finditer(v)]
    claims += [n for v in fields for s in sentences(v) for n in _names(s) if norm_literal(n) not in COMMON]
    claims += [w for w in OUTCOME_WORDS if any(_says(norm_literal(v), w) for v in fields)]
    out: list[tuple[str, str, str]] = []
    for claim in dict.fromkeys(c for c in claims if c):
        for b, _ in proofs:
            body = _text(b)
            if _says(norm_literal(body), claim) or _stem_says(norm_stem(body), claim):
                out.append((claim, str(b["id"]), _quote(body, claim)))
                break
    return out


def check_all(fb: Mapping) -> dict[str, list[Conflict]]:
    """{hook id: its conflicts} for every hook_library entry, in library order (an empty list: consistent)."""
    return {str(e["id"]): conflicts(fb, e) for e in fb.get("hook_library") or [] if isinstance(e, Mapping) and e.get("id")}


def report(fb: Mapping) -> str:
    """The `careerops hook-check` text: one block per hook, OK or BLOCKED with the two quotes of every conflict."""
    out: list[str] = []
    results = check_all(fb)
    entries = {str(e["id"]): e for e in fb.get("hook_library") or [] if isinstance(e, Mapping) and e.get("id")}
    for hid, found in results.items():
        listed = ", ".join(proof_ids(dict(entries[hid])))
        if not found:
            out.append(f"{hid}: ok (proof {listed})")
            out += [f'  {claim:<14} {bid:<7}: "{quote}"' for claim, bid, quote in supports(fb, entries[hid])]
            continue
        out.append(f"{hid}: BLOCKED (proof {listed}; {len(found)} claim{'s' if len(found) != 1 else ''} its proof bullets do not make)")
        for c in found:
            out += ["  " + l for l in c.lines()]
    blocked = sum(1 for f in results.values() if f)
    out.append(f"\nhook-check: {len(results) - blocked} ok, {blocked} blocked of {len(results)} hooks (fix the entry or its proof in fact_bank.yaml "
               "yourself; nothing is reworded for you)")
    return "\n".join(out)
