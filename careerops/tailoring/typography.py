"""CV typography, applied when the PDF is rendered and never earlier: the faithfulness and claim checks have already run on the
original wording by then, so nothing here can change what a check saw. `typeset` returns a copy of the render context; the
context kept in report.json stays as the checks left it.

  * dashes     — an em dash (or "--", or a spaced hyphen used as a dash) becomes a comma, a colon or parentheses; a number
                 range written with one (3.9—3.11) becomes an en dash. None survives into the PDF.
  * hyphens    — every word-word becomes word word (class-imbalance -> class imbalance, 19,213-gene -> 19,213 gene), the special
                 spellings of config/typography.yaml `rewrite` first (e-commerce -> ecommerce, 5-star -> five star). Never touched:
                 the `protected` list (scikit-learn, k-means, gpt-oss, faster-whisper, CIFAR-10, TF-IDF, Skip-gram ...), the links'
                 handles (thiruvel-a-p), URLs and e-mail addresses, number ranges (3.9-3.11, 2025-2026), names such as CIFAR-10 /
                 GPT-4o (letters-digits), a one-letter prefix (t-SNE, U-Net), proper-noun pairs (Rolls-Royce), and a hyphenated form
                 that a JD keyword spells with the hyphen (the ATS matches the JD's own spelling, so closing it up could lose a hit).
  * awards     — a project's award is in its heading; a bullet (or its result) that repeats it has the repeat taken out
  * `audit`    — what a rendered PDF still contains: em dashes (must be none), en dashes in prose, hyphenated words
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..config import ROOT, load_yaml

RULES_PATH = ROOT / "config" / "typography.yaml"
# a URL with or without its scheme (a PDF shows "github.com/x/y"), or an e-mail address
_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|"
                  r"(?<![\w@.-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s)>\]]*)?", re.I)
_HOLD = "\x00"


@dataclass(frozen=True)
class Rules:
    rewrite: tuple[tuple[str, str], ...]        # (hyphenated form, what the page shows), longest form first
    protected: tuple[str, ...]


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> Rules:
    raw = load_yaml(Path(path)) or {}
    table = {str(k): str(v) for k, v in (raw.get("rewrite") or {}).items()}
    return Rules(tuple(sorted(table.items(), key=lambda kv: -len(kv[0]))), tuple(str(p) for p in raw.get("protected") or []))


def load_rules(path: Path | None = None) -> Rules:
    p = Path(path or RULES_PATH)
    return _load(str(p), p.stat().st_mtime) if p.exists() else Rules((), ())


# ---------------------------------------------------------------------------------------------------------- protection
def _mask(text: str, protected: Iterable[str]) -> tuple[str, list[str]]:
    held: list[str] = []

    def hold(m: re.Match) -> str:
        held.append(m.group(0))
        return f"{_HOLD}{len(held) - 1}{_HOLD}"

    text = _URL.sub(hold, text)
    for term in sorted(protected, key=len, reverse=True):
        text = re.sub(r"(?<![\w-])" + re.escape(term) + r"(?!\w)", hold, text, flags=re.I)
    return text, held


def _unmask(text: str, held: list[str]) -> str:
    return re.sub(_HOLD + r"(\d+)" + _HOLD, lambda m: held[int(m.group(1))], text)


# -------------------------------------------------------------------------------------------------------------- dashes
_NUM_RANGE = re.compile(r"(?<=\d)\s*(?:—|--+)\s*(?=\d)")
_SPACED_HYPHEN = re.compile(r"(?<=[^\s\d])\s+-\s+(?=[^\s\d])")
_DASH = re.compile(r"\s*(?:—|--+)\s*")
_CONNECT = re.compile(r"(?:which|that|so|and|but|or|with|while|where|who|whom|whose|then|thus|because|as|to|for|from|by|in|on|at|"
                      r"of|via|plus|without|not|no)\b")
LABEL_WORDS = 6               # a dash after a heading-sized phrase (a name, a title) reads as a colon
_SMALL_WORDS = frozenset("a an the of to for and in on with at by from via per vs or".split())


def _is_label(phrase: str) -> bool:
    """A name or title ("Lloyd's Banking Group Datathon", "Bitcoin Forecasting"): at most LABEL_WORDS words, no punctuation that
    ends a clause, and every word that is not a small joining word starts with a capital letter or a digit. A clause
    ("Built an AI agent") is not one."""
    words = phrase.split()
    if not words or len(words) > LABEL_WORDS or re.search(r"[,;:.!?]", phrase):
        return False
    return all(w[0].isupper() or w[0].isdigit() for w in words if w.lower() not in _SMALL_WORDS and w[0].isalnum())


def _join(before: str, after: str, colon: bool = False) -> str:
    """`before` and `after` of one dash: a colon when `before` is a label (a name or title of up to LABEL_WORDS words and
    `after` is not a continuation; `colon` says it is one: a project's name), else a comma. Punctuation already on either side
    is not doubled."""
    b, a = before.rstrip(), after.lstrip()
    if not b or not a:
        return b or a
    if b[-1] in ",:;(":
        return f"{b} {a}"
    if a[0] in ",.;:)!?":
        return f"{b}{a}"
    first = a.split()[0]
    continues = a[0].islower() and (bool(_CONNECT.match(a)) or first.endswith(("ing", "ed")))
    return f"{b}{':' if (colon or _is_label(b)) and not continues else ','} {a}"


def dashes(text: str, colon: bool = False) -> str:
    """`text` with every em dash, "--" and spaced hyphen used as a dash replaced: a pair around an aside becomes commas
    (parentheses when the aside has a comma of its own); a lone one becomes a colon after a name or title, else a comma."""
    t = _NUM_RANGE.sub("–", text)
    t = _SPACED_HYPHEN.sub(" — ", t)
    if "—" not in t and not re.search("--", t):
        return t
    parts = _DASH.split(t)
    if len(parts) == 3 and all(p.strip() for p in parts):
        before, aside, after = (p.strip() for p in parts)
        if "," in aside:
            out = f"{before} ({aside})"
            out = out + (after if after[0] in ",.;:)!?" else f" {after}")
        else:
            out = f"{before}, {aside}" + (after if after[0] in ",.;:)!?" else f", {after}")
    else:
        out = parts[0]
        for nxt in parts[1:]:
            out = _join(out, nxt, colon)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    return re.sub(r",\s*,", ",", out).strip()


# ------------------------------------------------------------------------------------------------------------- hyphens
# a hyphenated word: letters / digits joined by hyphens, or a number with thousands commas then a word (19,213-gene). Not part of a
# number range with a point (3.9-3.11) or a longer dashed token.
_COMPOUND = re.compile(r"(?<![\w.-])(?:\d{1,3}(?:,\d{3})+|[A-Za-z0-9]+)(?:-[A-Za-z0-9]+)+(?![\w-]|\.\d)")
_MODEL_NAME = re.compile(r"[A-Za-z]+-\d+[A-Za-z]?")                 # CIFAR-10, GPT-4o, Qwen-2: a name, not a compound
_PROPER_PAIR = re.compile(r"[A-Z][a-z]+-[A-Z][a-z]+")                # Rolls-Royce, Coca-Cola


def exempt(word: str) -> bool:
    """A hyphenated word the general rule leaves alone: a number range (2025-2026), a name with a number (CIFAR-10), a one-letter
    prefix (t-SNE, U-Net) or a pair of proper nouns (Rolls-Royce)."""
    parts = word.split("-")
    return (all(p.replace(",", "").isdigit() for p in parts) or bool(_MODEL_NAME.fullmatch(word)) or (len(parts[0]) == 1 and parts[0].isalpha())
            or bool(_PROPER_PAIR.fullmatch(word)))


def close_hyphens(text: str, keep: Iterable[str] = ()) -> str:
    """Every hyphenated word of `text` closed up (word-word -> word word) except the exempt ones and `keep` (compared in lower case)."""
    kept = {k.casefold() for k in keep}
    return _COMPOUND.sub(lambda m: m.group(0) if m.group(0).casefold() in kept or exempt(m.group(0)) else m.group(0).replace("-", " "), text)


def _like(found: str, form: str, value: str) -> str:
    """`value` written the way `found` (a match of the table's `form`) was cased: as the table has it when the form matched
    exactly, in capitals for a word in capitals, with a capital first letter for a capitalised one."""
    if found == form:
        return value
    if found.isupper():
        return value.upper()
    return value[0].upper() + value[1:] if found[:1].isupper() and value[:1].islower() else value


def _rewrite(text: str, table: tuple[tuple[str, str], ...], keep: set[str]) -> str:
    for form, value in table:
        if form.casefold() in keep:
            continue
        pat = re.compile(r"(?<![\w-])" + re.escape(form) + r"(?=s?(?![\w-]))", re.I)
        text = pat.sub(lambda m, f=form, v=value: _like(m.group(0), f, v), text)
    return text


def clean(text: Any, rules: Rules | None = None, keep: Iterable[str] = (), colon: bool = False) -> str:
    """One piece of CV text as the page shows it: dashes replaced, table hyphens closed up, protected terms, URLs and e-mail
    addresses untouched. `keep` = hyphenated forms to leave alone (the JD keywords that are spelled with the hyphen). `colon`:
    the text is a name (a project's), so a lone dash in it is always a colon."""
    s = str(text if text is not None else "")
    if not s:
        return s
    rules = rules or load_rules()
    masked, held = _mask(s, rules.protected)
    kept = {k.casefold() for k in keep}
    masked = close_hyphens(_rewrite(masked, rules.rewrite, kept), kept)                      # hyphens first: "AI-agent" is not a name
    masked = dashes(masked, colon)
    return _unmask(masked, held)


# -------------------------------------------------------------------------------------------------------------- awards
def strip_award(text: str, award: Any) -> str:
    """`text` without a repeat of the project's `award` ("...; 3rd place; 200:1 class imbalance" -> "200:1 class imbalance"),
    with the comma or semicolon that joined it. Unchanged when the award is not in it."""
    a = " ".join(str(award or "").split())
    if not a or not text:
        return text
    pat = re.compile(r"(?P<lead>\s*[;,]\s*)?(?<!\w)" + re.escape(a) + r"(?!\w)(?P<trail>\s*[;,]\s*)?", re.I)
    out = pat.sub(lambda m: m.group("lead") if m.group("lead") and m.group("trail") else "", text)
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(r"\s+([,.;:])", r"\1", out).strip()


def award_repeats(text: str, award: Any) -> bool:
    """Does `text` say the award again: its words, or the ordinal that names the placing ("3rd")?"""
    a = " ".join(str(award or "").split())
    if not a:
        return False
    if re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I):
        return True
    ordinal = re.search(r"\b\d+(?:st|nd|rd|th)\b", a, re.I)
    return bool(ordinal and re.search(r"(?<!\w)" + re.escape(ordinal.group(0)) + r"(?!\w)", text, re.I))


# ------------------------------------------------------------------------------------------------------ certifications
_CERT = re.compile(r"^(?P<name>.+?)\s+(?:—|–|--+)\s+(?P<issuer>[^,]+?)(?P<rest>\s*,.*)?$")


def parse_cert(cert: Any) -> dict[str, str]:
    """A certification as {"name", "issuer", "rest"}: "Course — Anthropic, Jun 2026" is the course, its issuer and ", Jun 2026";
    {name, issuer, date} says the same. A plain string without an issuer is all name."""
    if isinstance(cert, dict) and "rest" in cert:                 # already split (typeset)
        return {"name": str(cert.get("name") or ""), "issuer": str(cert.get("issuer") or ""), "rest": str(cert.get("rest") or "")}
    if isinstance(cert, dict):
        date = str(cert.get("date") or cert.get("year") or "").strip()
        return {"name": str(cert.get("name") or cert.get("title") or "").strip(),
                "issuer": str(cert.get("issuer") or "").strip(), "rest": f", {date}" if date else ""}
    s = " ".join(str(cert).split())
    m = _CERT.match(s)
    if not m:
        return {"name": s, "issuer": "", "rest": ""}
    return {"name": m["name"].strip(), "issuer": m["issuer"].strip(), "rest": (m["rest"] or "").strip()}


def cert_text(cert: Any) -> str:
    """The certification as plain text: "Name, Issuer, Jun 2026"."""
    c = parse_cert(cert)
    return ", ".join(x for x in (c["name"], c["issuer"]) if x) + c["rest"]


# ------------------------------------------------------------------------------------------------------------- context
_SKIP = {"email", "phone", "links", "github", "url", "dates", "bullet_ids", "linespread", "employment_type", "verbatim", "situated"}


def handles(ctx: dict) -> tuple[str, ...]:
    """The hyphenated handles of the CV's own profile links (linkedin.com/in/thiruvel-a-p, github.com/Thiruvel-AP): never closed up
    wherever they appear in the text. A repository or page name further along a path is not a handle."""
    out: list[str] = []
    for url in ((ctx.get("id") or {}).get("links") or {}).values():
        u = urlparse(str(url) if "//" in str(url) else "//" + str(url))
        parts = [x for x in u.path.split("/") if x]
        host = (u.netloc or "").lower().removeprefix("www.")
        handle = parts[1] if host.endswith("linkedin.com") and len(parts) > 1 and parts[0] == "in" else parts[0] if host == "github.com" and parts else ""
        if "-" in handle:
            out.append(handle)
    return tuple(dict.fromkeys(out))


def typeset(ctx: dict, rules: Rules | None = None, keep: Iterable[str] = ()) -> dict:
    """A copy of the render context with every piece of prose passed through `clean`, an award taken out of its project's
    bullets, and the certifications split into name / issuer / rest. Links, e-mail, phone, dates (the dates filter prints those),
    ids and numbers are left as they are."""
    rules, keep = rules or load_rules(), list(keep)
    rules = Rules(rules.rewrite, rules.protected + handles(ctx))
    src = dict(ctx)
    results = dict(src.get("results") or {})
    projects = []
    for p in src.get("projects") or []:
        award = p.get("award")
        if award:
            p = {**p, "bullets": [strip_award(b, award) for b in p["bullets"]]}
            for bid in p.get("bullet_ids") or []:
                if bid in results:
                    results[bid] = strip_award(results[bid], award)
        projects.append(p)
    if "projects" in src:
        src["projects"] = projects
    if "results" in src:
        src["results"] = results

    def walk(v: Any, key: str | None = None) -> Any:
        if key in _SKIP or (key == "id" and isinstance(v, str)):
            return copy.deepcopy(v)
        if isinstance(v, str):
            return clean(v, rules, keep, colon=key == "name")
        if isinstance(v, dict):
            return {k: walk(x, k) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x, key) for x in v]
        return v

    out = walk({k: v for k, v in src.items() if k != "certifications"})
    if "certifications" in src:
        out["certifications"] = [{k: clean(x, rules, keep) for k, x in parse_cert(c).items()}
                                 for c in src["certifications"] or []]
    return out


# --------------------------------------------------------------------------------------------------------------- audit
_HYPHENATED = _COMPOUND
_MONTH = re.compile(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?|present|\d", re.I)


def audit(text: str, rules: Rules | None = None, keep: Iterable[str] = ()) -> dict:
    """What the text of a rendered PDF still holds: {"em_dashes": n (must be 0), "prose_en_dashes": [context, ...] (an en dash
    that is not a date or number range), "hyphens": {word: count} (hyphenated words other than protected ones and URLs),
    "kept": [hyphenated JD keywords that stayed hyphenated on purpose]}."""
    rules = rules or load_rules()
    masked, _ = _mask(text, rules.protected)
    em = masked.count("—") + len(re.findall(r"(?<!-)--(?!-)", masked))
    prose = []
    for m in re.finditer(r"\s–\s", masked):
        left = masked[:m.start()].split()[-1:] or [""]
        right = masked[m.end():].split()[:1] or [""]
        if not (re.search(r"\d$|^present$", left[0], re.I) or _MONTH.match(right[0])):
            prose.append(" ".join(masked[max(0, m.start() - 25):m.end() + 25].split()))
    found: dict[str, int] = {}
    for m in _HYPHENATED.finditer(masked):
        if exempt(m.group(0)):
            continue
        found[m.group(0)] = found.get(m.group(0), 0) + 1
    keep_cf = {k.casefold() for k in keep}
    kept = sorted(w for w in found if w.casefold() in keep_cf)
    hyphens = dict(sorted(((w, n) for w, n in found.items() if w.casefold() not in keep_cf), key=lambda kv: (-kv[1], kv[0].casefold())))
    return {"em_dashes": em, "prose_en_dashes": prose, "hyphens": hyphens, "kept": kept}
