"""Plain-language CV text (v3.4): a `plain:` version of each fact-bank bullet, written for a non-technical first reader, with the ATS keywords kept.

A UK recruiter's advice (8 Nov 2026): the first person to read the CV is not technical. Each bullet says, in plain words, what the situation was,
what the user did and what came of it (STAR); technologies appear as keywords, not as a chain of tool names. The ATS strict score is
sum(w * hit) / sum(w) over the JD's keywords only, so dropping a term the JD does not name (HGNC, parquet, circuit breaker, Leiden) costs nothing,
and dropping a JD keyword costs its weight: keep every JD keyword, drop the jargon the JD does not name.

  * `careerops plain-draft` (review/plaindraft.py) asks the model for ONE candidate per bullet that has no `plain:` (and per hook entry with no
    `plain_headline` / `plain_profile_line` / `plain_letter_line`), checks it here (`problems`), shows it, and saves it only on your `y` (or your
    edit) through factbank_append.add_field, which writes a dated backup first. The bullet's own text is never changed.
  * `cv.style: plain` (the code default; `technical` keeps the old wording): tailoring reads each bullet's `plain:` (`plain_bank`), adds back at the
    end, in brackets, up to MAX_BRACKET JD keywords the original bullet says and the plain one does not ("..., checked by ~65 automated tests
    (Python, AWS)."), and uses the hook entries' plain fields. A bullet with no `plain:` keeps its text and is listed as "plain version missing".

The checks on a candidate (all in code, before you see it):
  facts      every number is the bullet's own (text or `result:`); no tool, organisation or name the bullet (or its role) does not have; no outcome
             phrase ("in daily use", "placed", "reduced") the bullet does not state; no deny-listed term;
  jargon     a technical term only when it is on the allow-list (ALLOW + settings cv.plain_allow); library and product names, file formats,
             algorithm names, gene and data-standard names, internal component names are refused (`jargon`);
  length     one sentence, at most MAX_WORDS (30) words and two CV lines (2 x CHARS_PER_LINE characters);
  readable   Flesch reading ease >= cv.plain_min_flesch (50), the built-in formula (`flesch`; textstat is not a dependency);
  shape      a result clause: a number or a concrete outcome. A bullet that states no result is NEEDS RESULT and is not drafted.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .faithfulness import deny_hits, numbers
from .grounding import _names, name_allowed, sentences
from .scorer import Keyword, _contains, norm_literal

TASK = "plain_bullet"
MAX_WORDS = 30
MAX_HEADLINE_WORDS = 12
MAX_LINE_WORDS = 35
CHARS_PER_LINE = 92                     # a bullet line of the two-page CV's 11pt body (tailor.CHARS_PER_LINE)
MAX_LINES = 2
MIN_FLESCH = 50.0
MAX_BRACKET = 3
TRIES = 2
STYLES = ("plain", "technical")
HOOK_FIELDS = (("headline", "plain_headline"), ("profile_line", "plain_profile_line"), ("letter_line", "plain_letter_line"))

# The keyword allow-list: names a recruiter recognises. Languages and platforms, areas, widely known tools (the task's list, 8 Nov 2026).
ALLOW_LANGUAGES = ("Python", "SQL", "R", "AWS", "Azure", "GCP", "Docker")
ALLOW_AREAS = ("machine learning", "AI", "LLM", "LLMs", "NLP", "AI agents", "AI agent", "AI assistant", "RAG", "forecasting", "A/B testing",
               "dashboards", "dashboard", "data science", "deep learning", "statistics", "cloud")
ALLOW_TOOLS = ("PyTorch", "TensorFlow", "Spark", "Databricks", "Power BI", "Tableau", "LangChain", "LangGraph", "MLflow", "Excel")
ALLOW = ALLOW_LANGUAGES + ALLOW_AREAS + ALLOW_TOOLS
TOOLS_ON_LIST = ALLOW_LANGUAGES + ALLOW_TOOLS                 # the allow-listed names that are tools: a candidate may name one only if its bullet does

# Terms a layperson does not know that the fact bank does not list as a skill (it lists libraries and products; these are in bullets' prose).
JARGON = ("parquet", "circuit breaker", "circuit breakers", "leiden", "hgnc", "ensembl", "pydantic", "modularity", "isolation forest", "k-means",
          "dbscan", "hac", "tf-idf", "sbert", "skip-gram", "silhouette", "lstm", "cnn", "qlora", "lora", "embeddings", "embedding", "vector search",
          "tool-calling", "tool calling", "deterministic", "hallucination guards", "ecr", "iam", "least-privilege", "ci/cd", "mcp", "websocket",
          "full-duplex", "json", "yaml", "csv", "api", "apis", "endpoint", "endpoints", "lambda", "serverless", "kubernetes", "microservice",
          "microservices", "schema", "inference", "float16", "cuda", "ensemble", "classifier", "classifiers", "regex", "crosswalks", "crosswalk",
          "bedrock", "orchestration", "react-based", "barge-in", "identity-spine", "metadata", "gradient boosting", "xgboost", "react")
# Acronyms and words everyone reads: never jargon.
COMMON = frozenset("""uk us usa eu cv msc bsc ba ma phd ceo cto cfo hr it nhs ai ml i a vs ltd plc tv""".split())
# Outcome phrases: a candidate may state one only when its bullet does.
OUTCOMES = ("in daily use", "in live use", "in production", "now used", "used by", "reused", "adopted", "placed", "place", "won", "award",
            "reduced", "cut", "saved", "faster", "fewer", "rating", "ranked", "selected", "shipped", "replacing", "replaced")
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th)$", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./:'’-]*[A-Za-z0-9+#]|[A-Za-z]")


# ------------------------------------------------------------------ settings
def style(settings) -> str:
    """cv.style: "plain" (the code default) or "technical"."""
    raw = getattr(settings, "raw", settings) or {}
    v = str(((raw.get("cv") or {}) if isinstance(raw, dict) else {}).get("style") or "plain").strip().lower()
    return v if v in STYLES else "plain"


def allow_list(settings=None) -> list[str]:
    """ALLOW plus settings cv.plain_allow (names you add yourself)."""
    raw = getattr(settings, "raw", settings) or {}
    extra = ((raw.get("cv") or {}) if isinstance(raw, dict) else {}).get("plain_allow") or []
    return list(dict.fromkeys([*ALLOW, *(str(x) for x in extra if str(x).strip())]))


def min_flesch(settings=None) -> float:
    raw = getattr(settings, "raw", settings) or {}
    v = ((raw.get("cv") or {}) if isinstance(raw, dict) else {}).get("plain_min_flesch")
    try:
        return float(v) if v is not None else MIN_FLESCH
    except (TypeError, ValueError):
        return MIN_FLESCH


def evidenced_allow(fb: Mapping, settings=None) -> list[str]:
    """The allow-list names the fact bank says anywhere (for `doctor`)."""
    from .grounding import bank_text

    bank = bank_text(dict(fb))
    return [a for a in allow_list(settings) if _contains(bank, norm_literal(a))]


# ------------------------------------------------------------------ readability
def syllables(word: str) -> int:
    """A word's syllables, the usual vowel-group rule: a number is one, a short all-capitals acronym is spelt out ("AWS" = 3)."""
    w = word.strip(".,;:!?()\"'’[]")
    if not w:
        return 0
    if re.fullmatch(r"[~£$€]?[\d.,:/%]+", w) or _ORDINAL.match(w):
        return 1
    if w.isupper() and 1 < len(w) <= 4 and w.isalpha():
        return len(w) + (w.count("W") * 2)                     # "W" is "double-u"
    low = re.sub(r"[^a-z]", "", w.lower())
    if not low:
        return 1
    groups = re.findall(r"[aeiouy]+", low)
    n = len(groups)
    if low.endswith("e") and not low.endswith(("le", "ee", "ye")) and n > 1:
        n -= 1
    if low.endswith(("ed", "es")) and n > 1 and not re.search(r"[td]ed$|[sxz]es$|[cs]hes$", low):
        n -= 1
    return max(1, n)


def flesch(text: str) -> float:
    """Flesch reading ease: 206.835 - 1.015 (words / sentences) - 84.6 (syllables / words)."""
    words = [w for w in text.split() if re.search(r"\w", w)]
    if not words:
        return 0.0
    n_sent = max(1, len(sentences(text)))
    syl = sum(syllables(w) for w in words)
    return round(206.835 - 1.015 * (len(words) / n_sent) - 84.6 * (syl / len(words)), 1)


# ------------------------------------------------------------------ jargon
def _technical_look(name: str) -> bool:
    """A fact-bank skill or stack name that reads as a product or library: one lower-case word (pandas, pytest), a capitalised or CamelCase
    name, letters with digits or symbols (Llama 3.1, scikit-learn, CI/CD). A lower-case phrase of ordinary words ("stakeholder demos") is not."""
    n = name.strip()
    if not n:
        return False
    if re.search(r"[0-9+#./:]", n) or re.search(r"[a-z][A-Z]", n) or re.fullmatch(r"[A-Z]{2,}\w*", n):
        return True
    words = n.split()
    if len(words) == 1:
        return True
    return any(w[:1].isupper() for w in words)


def _bank_tools(fb: Mapping) -> list[str]:
    names: set[str] = set()
    for items in (fb.get("skills") or {}).values():
        if isinstance(items, list):
            names.update(str(s) for s in items)
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            names.update(x.strip() for x in re.split(r"\s*,\s*", re.sub(r"\([^)]*\)", " ", str(e.get("stack") or ""))) if x.strip())
    return sorted((n for n in names if _technical_look(n)), key=len, reverse=True)


def _plain_ok(fb: Mapping, owner: Mapping | None) -> str:
    """norm_literal text of what is never jargon: the allow-list, the role's organisation, title, location, the project's name and award."""
    bits = [*ALLOW]
    if owner:
        bits += [str(owner.get(k) or "") for k in ("org", "title", "location", "name", "award")]
    ident = fb.get("identity") or {}
    bits += [str(ident.get(k) or "") for k in ("name", "location")]
    return norm_literal(" ".join(bits))


def jargon(text: str, fb: Mapping, owner: Mapping | None = None, allow: Sequence[str] = ALLOW) -> list[str]:
    """The technical terms of `text` a layperson would not know and the allow-list does not hold: fact-bank tool names, JARGON, and tokens that
    look like code (an acronym, a CamelCase name, letters mixed with digits, a file extension). The role's own organisation and the project's own
    name are never jargon."""
    lit = norm_literal(text)
    allowed = {norm_literal(a) for a in allow}
    ok = _plain_ok(fb, owner) + " " + " ".join(allowed)
    out: list[str] = []

    def allowed_here(term: str) -> bool:
        t = norm_literal(term)
        return t in allowed or t in COMMON or (bool(t) and _contains(ok, t))

    for term in [*_bank_tools(fb), *JARGON]:
        t = norm_literal(term)
        if len(t) >= 2 and _contains(lit, t) and not allowed_here(term):
            out.append(term)
    for tok in _TOKEN.findall(text):
        bare = re.sub(r"['’]s$", "", tok).strip(".:/-")
        if not bare or allowed_here(bare) or _ORDINAL.match(bare):
            continue
        looks = (re.fullmatch(r"[A-Z]{2,}[a-z]?", bare) is not None or re.search(r"[a-z][A-Z]", bare) is not None
                 or (re.search(r"\d", bare) is not None and re.search(r"[A-Za-z]", bare) is not None)
                 or re.search(r"\.[a-z]{2,5}$", bare) is not None or ":" in bare)
        if looks and not any(norm_literal(bare) == norm_literal(o) or _contains(norm_literal(o), norm_literal(bare)) for o in out):
            out.append(bare)
    seen: set[str] = set()
    uniq = []
    for t in out:                                              # "circuit breakers" and "circuit breaker": say it once
        k = norm_literal(t)
        if not any(k != s and (_contains(s, k) or _contains(k, s)) for s in seen) and k not in seen:
            uniq.append(t)
            seen.add(k)
    return uniq


# ------------------------------------------------------------------ the checks
def has_result(bullet: Mapping) -> bool:
    """A bullet states a result: its `result:` field, a number, or an outcome phrase."""
    text = f"{bullet.get('text') or ''} {bullet.get('result') or ''}"
    return bool(str(bullet.get("result") or "").strip()) or bool(numbers(text)) or _outcome(text) is not None


def _outcome(text: str) -> str | None:
    lit = norm_literal(text)
    return next((o for o in OUTCOMES if _contains(lit, norm_literal(o))), None)


def _source(bullet: Mapping) -> str:
    return " ".join(str(bullet.get(k) or "") for k in ("text", "result"))


def fact_problems(cand: str, source: str, terms: Sequence[str], owner: Mapping | None, fb: Mapping, allow: Sequence[str] = ALLOW) -> list[str]:
    """facts: no number, tool, name or outcome the source does not have; no deny-listed term."""
    out: list[str] = []
    extra = numbers(cand) - numbers(source)
    if extra:
        out.append(f"adds number(s) the bullet does not have: {sorted(extra)}")
    pool = norm_literal(" ".join([source, *terms, *(str((owner or {}).get(k) or "") for k in ("org", "title", "location", "name", "award")),
                                  *ALLOW_AREAS]))
    for s in sentences(cand):
        for name in _names(s):
            if norm_literal(name) in COMMON:
                continue
            if not name_allowed(pool, name):
                out.append(f"names '{name}', which the bullet does not")
    lit, src = norm_literal(cand), norm_literal(source + " " + " ".join(terms))
    for tool in [*TOOLS_ON_LIST, *_bank_tools(fb)]:
        t = norm_literal(tool)
        if len(t) >= 2 and _contains(lit, t) and not _contains(src, t):
            out.append(f"names the tool '{tool}', which the bullet does not")
    for o in OUTCOMES:
        if _contains(lit, norm_literal(o)) and not _contains(norm_literal(source), norm_literal(o)):
            out.append(f"states an outcome the bullet does not ('{o}')")
    out += [f"deny-listed term '{d}'" for d in deny_hits(cand, list(fb.get("deny_terms") or [])) if d not in deny_hits(source, list(fb.get("deny_terms") or []))]
    return list(dict.fromkeys(out))


def problems(cand: str, bullet: Mapping, owner: Mapping | None, fb: Mapping, settings=None, kind: str = "bullet",
             source: str | None = None) -> list[str]:
    """Every check on one candidate. `kind`: bullet | headline | profile_line | letter_line (a hook entry's field: `source` is that field and
    its proof bullet; it must keep the field's number)."""
    t = " ".join(str(cand or "").split())
    if not t:
        return ["the candidate is empty"]
    allow = allow_list(settings)
    src = source if source is not None else _source(bullet)
    out = fact_problems(t, src, [str(x) for x in bullet.get("terms") or []], owner, fb, allow)
    words = len(t.split())
    if kind == "bullet":
        if len(sentences(t)) != 1:
            out.append(f"{len(sentences(t))} sentences: a bullet is one")
        if words > MAX_WORDS:
            out.append(f"{words} words (at most {MAX_WORDS})")
        if len(t) > MAX_LINES * CHARS_PER_LINE:
            out.append(f"{len(t)} characters: more than {MAX_LINES} CV lines")
        score = flesch(t)
        if score < min_flesch(settings):
            out.append(f"Flesch reading ease {score} (at least {min_flesch(settings):g})")
        if not numbers(t) and _outcome(t) is None:
            out.append("no result clause: it needs a number or a concrete outcome")
    else:
        limit = MAX_HEADLINE_WORDS if kind == "headline" else MAX_LINE_WORDS
        if words > limit:
            out.append(f"{words} words (at most {limit})")
        own = numbers(source or "")
        if own and not (numbers(t) & own):
            out.append(f"drops the number ({', '.join(sorted(own))})")
    j = jargon(t, fb, owner, allow)
    if j:
        out.append(f"jargon not on the allow-list: {', '.join(j)}")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ what to draft
@dataclass
class Item:
    """One thing to draft: a bullet (`field` = "plain") or a hook entry's field (`plain_headline` ...)."""
    kind: str                        # bullet | headline | profile_line | letter_line
    owner_id: str                    # the bullet id, or the hook entry id
    field: str                       # the key factbank_append.add_field writes
    original: str
    source: str                      # what the candidate may be built from (the bullet, or the hook field + its proof bullet)
    bullet: dict = field(default_factory=dict)
    owner: dict = field(default_factory=dict)      # the role / project (bullet), or the hook entry
    needs_result: bool = False


def items(fb: Mapping, hooks: bool = True) -> list[Item]:
    """Every bullet with no `plain:` (fact-bank order), then every hook entry field with no plain twin."""
    out: list[Item] = []
    index: dict[str, dict] = {}
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                index[str(b["id"])] = b
                if str(b.get("plain") or "").strip():
                    continue
                out.append(Item("bullet", str(b["id"]), "plain", " ".join(str(b["text"]).split()), _source(b), dict(b), dict(e),
                                needs_result=not has_result(b)))
    if hooks:
        for h in fb.get("hook_library") or []:
            proof = index.get(str(h.get("proof") or ""), {})
            for src_key, key in HOOK_FIELDS:
                text = " ".join(str(h.get(src_key) or "").split())
                if text and not str(h.get(key) or "").strip():
                    out.append(Item(src_key, str(h["id"]), key, text, f"{text} {_source(proof)}", dict(proof), dict(h)))
    return out


SYSTEM = """You rewrite one line of a CV for a NON-TECHNICAL first reader (a recruiter), keeping it true.
{shape}
Rules (checked by code; a line that breaks one is thrown away):
- Use ONLY what the ORIGINAL says. Keep every number you use exactly as written there. Add no number, tool, organisation, name or outcome.
- Plain everyday words. Remove library names, file formats, algorithm names, gene or data-standard names and internal component names.
- A technical name may stay only if it is in ALLOWED and the ORIGINAL uses it.
- British spelling, past tense for past work, no "we" or "our".
Return JSON only: {{"plain": "<the line>"}}."""
SHAPES = {
    "bullet": ("ONE sentence of at most 30 words, in this shape: <the situation or task in at most 12 plain words>, I <what I did, in plain words>"
               "<, using KEYWORD1 and KEYWORD2>, <the result: a number or a concrete outcome>. Short, common words: it must score 50 or more on "
               "Flesch reading ease."),
    "headline": "A CV headline of at most 12 words: what I am and one result with its number, plain words (for example \"AI assistant running on "
                "AWS, checked by ~65 automated tests\").",
    "profile_line": "The first sentence of a CV profile, at most 35 words: my role, then one plain result with its number.",
    "letter_line": "The proof clause of a cover letter's first sentence, at most 35 words, starting as the ORIGINAL starts: one plain result with "
                   "its number and how it was checked.",
}


def draft(llm, it: Item, fb: Mapping, settings=None) -> tuple[str, list[str], int]:
    """(candidate, problems, tokens): TRIES model calls, the second told the first's problems; a candidate is returned only when it passes
    every check ("" and the last problems otherwise). A NEEDS RESULT bullet is not drafted."""
    from ..llm.router import TaskUnavailable
    from . import tailor as T

    if it.needs_result:
        return "", ["NEEDS RESULT: the bullet states no number or outcome, so a plain version would have no result"], 0
    allow = [a for a in allow_list(settings) if _contains(norm_literal(it.source), norm_literal(a))]
    system = SYSTEM.format(shape=SHAPES[it.kind])
    probs: list[str] = []
    tokens = 0
    start = len(getattr(llm, "usage", []) or [])
    for attempt in range(TRIES):
        user = json.dumps({"ORIGINAL": it.original, "RESULT": str(it.bullet.get("result") or "") if it.kind == "bullet" else "",
                           "ROLE": " ".join(str(it.owner.get(k) or "") for k in ("title", "org", "name") if it.owner.get(k)) if it.kind == "bullet" else "",
                           "ALLOWED": allow, "LAST": "" if not probs else last, "PROBLEMS": probs}, ensure_ascii=False, indent=1)
        try:
            out = T._llm_json(llm, TASK, system, user, {"mode": "plain", "kind": it.kind, "id": it.owner_id, "original": it.original,
                                                         "temperature": 0.2 if attempt == 0 else 0.5})
        except TaskUnavailable:
            raise
        except Exception as e:  # noqa: BLE001  (a bad reply, no network: nothing to show for this one)
            return "", [f"the model could not draft it: {str(e)[:120]}"], tokens
        last = " ".join(str((out or {}).get("plain") or "").split())
        tokens += (len(system) + len(user) + len(last)) // 4
        probs = problems(last, it.bullet, it.owner, fb, settings, it.kind, None if it.kind == "bullet" else it.source)
        if not probs:
            break
    rows = list(getattr(llm, "usage", []) or [])[start:]
    tokens = sum(int(r.get("total_tokens") or 0) for r in rows if isinstance(r, dict)) or tokens
    return ("", probs, tokens) if probs else (last, [], tokens)


# ------------------------------------------------------------------ tailoring in plain style
def _literal(text: str, term: str) -> bool:
    return _contains(norm_literal(text), norm_literal(term))


def _spelled(term: str, text: str) -> str:
    m = re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text, re.I)
    return m.group(0) if m else term


def missing_keywords(original: str, plain: str, kws: Sequence[Keyword]) -> list[str]:
    """The JD keywords the original bullet says (literally) and its plain version does not, heaviest first, as the original spells them."""
    out = []
    for k in sorted(kws, key=lambda k: -k.weight):
        member = next((w for w in k.members() if _literal(original, w)), None)
        if member and not any(_literal(plain, w) for w in k.members()):
            out.append(_spelled(member, original))
    return list(dict.fromkeys(out))


def with_keywords(plain: str, terms: Sequence[str]) -> str:
    """"<plain> (Python, AWS)." : the keywords in brackets before the full stop."""
    if not terms:
        return plain
    body = plain.rstrip()
    stop = body[-1] if body and body[-1] in ".!?" else "."
    body = body[:-1] if body and body[-1] in ".!?" else body
    return f"{body} ({', '.join(terms)}){stop}"


def plain_bank(fb: dict, kws: Sequence[Keyword], cap: int = MAX_BRACKET) -> tuple[dict, dict]:
    """(a copy of the fact bank whose bullets read their `plain:` with up to `cap` missing JD keywords in brackets, and whose hook entries read
    their plain fields; info). info = {"plain": [ids], "missing": [ids with no plain], "added": {id: [terms]}, "readability": {id: score},
    "technical": {id: original text}}. The fact bank itself is not changed."""
    out = copy.deepcopy(fb)
    info: dict = {"plain": [], "missing": [], "added": {}, "readability": {}, "technical": {}}
    for sec in ("experience", "projects"):
        for e in out.get(sec) or []:
            for b in e.get("bullets") or []:
                plain = " ".join(str(b.get("plain") or "").split())
                if not plain:
                    info["missing"].append(str(b["id"]))
                    continue
                original = " ".join(str(b["text"]).split())
                add = missing_keywords(original, plain, kws)[:cap]
                info["technical"][str(b["id"])] = original
                b["text_technical"] = original
                b["text"] = with_keywords(plain, add)
                info["plain"].append(str(b["id"]))
                info["readability"][str(b["id"])] = flesch(plain)
                if add:
                    info["added"][str(b["id"])] = add
    for h in out.get("hook_library") or []:
        for src_key, key in HOOK_FIELDS:
            if str(h.get(key) or "").strip():
                h[src_key] = " ".join(str(h[key]).split())
    return out, info


def top_up(ctx: dict, info: dict, kws: Sequence[Keyword], cv_text: str) -> list[str]:
    """When the plain CV misses a JD keyword its technical bullets carry, add it in brackets (no cap) to the first plain bullet on the CV whose
    original says it. Returns the keywords added. `ctx` is changed in place."""
    added: list[str] = []
    for k in sorted(kws, key=lambda k: -k.weight):
        if any(_literal(cv_text, w) for w in k.members()):
            continue
        for sec in ("experience", "projects"):
            done = False
            for e in ctx.get(sec) or []:
                for i, bid in enumerate(e.get("bullet_ids") or []):
                    orig = info["technical"].get(bid)
                    member = next((w for w in k.members() if orig and _literal(orig, w)), None)
                    if not member:
                        continue
                    term = _spelled(member, orig)
                    text = e["bullets"][i]
                    m = re.search(r"\(([^()]*)\)([.!?])$", text)
                    e["bullets"][i] = (text[:m.start()] + f"({m.group(1)}, {term}){m.group(2)}") if m else with_keywords(text, [term])
                    info["added"].setdefault(bid, []).append(term)
                    added.append(term)
                    done = True
                    break
                if done:
                    break
            if done:
                break
    return added


def ctx_text(ctx: Mapping, technical: Mapping[str, str] | None = None) -> str:
    """The CV's words as one text (headline, profile, skills, bullets, achievements, certifications), for the strict ATS score of either style:
    `technical` = {bullet id: original text} puts the technical wording back."""
    bits = [str((ctx.get("id") or {}).get("headline") or ""), str(ctx.get("hook_headline") or ""), str(ctx.get("profile") or "")]
    for group, items_ in (ctx.get("skills") or {}).items():
        bits.append(f"{group}: " + (items_ if isinstance(items_, str) else ", ".join(map(str, items_))))
    for sec in ("experience", "projects"):
        for e in ctx.get(sec) or []:
            bits.append(" ".join(str(e.get(k) or "") for k in ("title", "org", "name", "stack")))
            for bid, text in zip(e.get("bullet_ids") or [], e.get("bullets") or []):
                bits.append((technical or {}).get(bid) or text)
    bits += [str(a.get("text") or "") for a in ctx.get("achievements") or []]
    bits += [c if isinstance(c, str) else " ".join(map(str, c.values())) for c in ctx.get("certifications") or []]
    return "\n".join(b for b in bits if b)


def strict_both(ctx: Mapping, info: Mapping, kws: Sequence[Keyword], claimable: Iterable[str]) -> tuple[float, float]:
    """(strict % of the plain CV, strict % of the same CV with the technical wording), both on ctx_text."""
    from .. import ats

    plain_t = ctx_text(ctx)
    tech_t = ctx_text(ctx, info.get("technical") or {})
    return ats.scores(plain_t, list(kws), list(claimable), None, (0, 100)).strict, ats.scores(tech_t, list(kws), list(claimable), None, (0, 100)).strict
