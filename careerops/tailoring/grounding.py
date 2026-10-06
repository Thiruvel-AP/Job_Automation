"""Is each sentence of the cover letter backed by the evidence it was written from?

The letter model sees the company hook and two evidence bullets; a small model still invents things around them ("for a
fintech client"). Every sentence is matched to the evidence bullet it is about (the one sharing the most content words
with it) and checked against that bullet's text and terms plus the job description and company hook. Flagged, as soon as
the sentence says one and neither of those does:

  * a proper noun (a name, a product, an acronym: "Acme", "BERT", "AWS")
  * a client or sector word ("client", "customers", "fintech", "healthcare")
  * a capability word (a taxonomy skill/tool, or "production", "real-time", "end-to-end", ...)

A sentence that matches no bullet (the hook, the call to action) is checked against everything the model was given. The
sentence naming a required gap ("I have not used X yet") may use capability words freely: it is about X, not a claim.
"""
from __future__ import annotations

import re

from .scorer import _contains, load_taxonomy, norm_literal, norm_stem

SECTOR_WORDS = frozenset("""fintech healthcare banking insurance retail e-commerce ecommerce telecom telecoms logistics
    pharma pharmaceutical biotech energy automotive aviation gaming adtech edtech legal government defence manufacturing
    travel advertising media""".split())
CLIENT_WORDS = frozenset("""client clients customer customers consumer consumers stakeholder stakeholders patient
    patients investor investors merchant merchants advertiser advertisers""".split())
CAPABILITY_WORDS = ("production", "real-time", "large-scale", "at scale", "end-to-end", "deployment", "monitoring",
                    "dashboards", "pipelines", "experimentation", "mlops")
MATCH_WORDS = 3                      # content words a sentence must share with a bullet to count as written from it
_STOP = frozenset("with that this from have been into their over using used which what when where while also about "
                  "than then them they were will would could should your more most such each some team".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'’+#./-]*")
_POSSESSIVE = re.compile(r"['’]s$", re.I)          # "Chattermill's" names Chattermill; the 's is not part of the name
_PRONOUN = re.compile(r"^I(['’]m|['’]ve|['’]d|['’]ll)?$")


FILLER_WORDS = ("scalable", "robust", "seamless", "cutting-edge")     # filler: only if the evidence itself says it
_REJECTED = re.compile(r"^'([^']+)' \((?:name|sector|client|capability)\)|^vague filler '([^']+)'|^deny-listed '([^']+)'")


def avoid_words(evidence: list[dict], jd: str, hook: str = "", extra: str = "") -> list[str]:
    """The filler and capability words the grounding guard rejects when nothing the letter may draw on says them: the ones
    ("real-time", "end-to-end", "scalable", "robust", ...) in neither the evidence bullets (text + terms), the job
    description, the company hook nor `extra`. The letter prompt lists them as words not to use."""
    allowed = norm_literal(" ".join([*(e["text"] + " " + " ".join(e.get("terms", [])) for e in evidence), jd, hook, extra]))
    return [w for w in (*CAPABILITY_WORDS, *FILLER_WORDS) if not _has(allowed, w)]


def rejected_words(problems: list[str]) -> list[str]:
    """The words and names a letter's problems name: from "'real-time' (capability) is in neither ...", "vague filler
    'scalable' ..." and "deny-listed 'x'". Other problems (length, opener, numbers) name no word."""
    out = []
    for p in problems:
        m = _REJECTED.match(p)
        if m:
            out.append(next(g for g in m.groups() if g))
    return list(dict.fromkeys(out))


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#-]{4,}", norm_literal(s)) if w not in _STOP}


def _names(sentence: str) -> list[str]:
    """Proper nouns in a sentence: acronyms and CamelCase names anywhere, other capitalised words except the first."""
    out = []
    for i, m in enumerate(_WORD.finditer(sentence)):
        w = _POSSESSIVE.sub("", m.group(0).strip(".'’-/&"))
        if len(w) < 2 or _PRONOUN.match(w):
            continue
        camel = any(c.isupper() for c in w[1:]) and not w.isupper()
        if w.isupper() or camel or (w[0].isupper() and i > 0):
            out.append(w)
    return list(dict.fromkeys(out))


def _has(allowed: str, term: str) -> bool:
    """Does `allowed` (norm_literal'd text) say `term`, literally or after stemming?"""
    return _contains(allowed, norm_literal(term)) or _contains(norm_stem(allowed), norm_stem(term))


def grounding_problems(paragraphs: list[str], evidence: list[dict], jd: str, hook: str = "", extra: str = "",
                       gaps: list[str] | None = None) -> list[str]:
    """Problems with the letter's sentences. `evidence` is [{"text", "terms"}] (the bullets the model was given);
    `extra` is more text that may be named (company, role); `gaps` are the required tools the letter may say it lacks."""
    ev = [norm_literal(e["text"] + " " + " ".join(e.get("terms", []))) for e in evidence]
    shared = norm_literal(" ".join([jd, hook, extra, " ".join(gaps or [])]))
    everything = " ".join(ev) + " " + shared
    capabilities = [*CAPABILITY_WORDS, *load_taxonomy()]
    gap_words = [norm_literal(g) for g in gaps or []]
    out: list[str] = []
    for sentence in sentences(" ".join(paragraphs)):
        words = content_words(sentence)
        overlap = [len(words & content_words(e["text"])) for e in evidence]
        own = max(range(len(evidence)), key=lambda i: overlap[i]) if evidence and max(overlap) >= MATCH_WORDS else None
        allowed = (ev[own] + " " + shared) if own is not None else everything
        lit = norm_literal(sentence)
        about_a_gap = any(_contains(lit, g) for g in gap_words)

        flagged: set[str] = set()

        def flag(term: str, kind: str) -> None:
            if term.lower() not in flagged:                    # "Python" is a name and a capability: say it once
                flagged.add(term.lower())
                out.append(f"'{term}' ({kind}) is in neither the evidence nor the job description: "
                           f"'{sentence.strip()[:60]}'")

        for name in _names(sentence):
            if not _has(allowed, name):
                flag(name, "name")
        for w in dict.fromkeys(re.findall(r"[a-z][a-z-]+", lit)):
            if w in SECTOR_WORDS and not _has(allowed, w):
                flag(w, "sector")
            elif w in CLIENT_WORDS and not _has(allowed, w):
                flag(w, "client")
        if not about_a_gap:
            for term in capabilities:
                if _contains(lit, norm_literal(term)) and not _has(allowed, term):
                    flag(term, "capability")
    return list(dict.fromkeys(out))
