"""The letter's opening, P1 (Document rules, B4): the company's problem first, then the proof.

    <Company> is hiring for "<a phrase of the ad>"; <a fact of the fact bank with a number, ending with how it was validated>.

The fact is the hook's `letter_line` (the user's own sentence, word for word, only its first letter lower-cased when it starts with "At" or
the like). It must carry a number that the fact bank itself holds, so a hook
whose line has none (`h_forecast`) cannot open a letter: the next eligible hook is used (its proof bullet must be on this job's CV), else the
strongest numbered bullet of the CV, followed by the role family's profile line. A line that starts "I am writing to apply" and the like is
never used. The problem the company is hiring to solve is a verbatim clause of the job ad (preferred), else of the company's OWN careers page
(one fetch, tailoring/companypage.py); never news, LinkedIn or another third-party site. `problems` checks a finished opening against exactly
those two texts, so a typed or edited opening gets the same check. Nothing here asks a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .. import hook as hook_mod
from ..hook import Hook, iter_eligible
from .faithfulness import deny_hits, numbers
from .grounding import sentences
from .scorer import Keyword, keyword_hits

BANNED = re.compile(r"^\s*(?:i\s+am\s+writing\s+to\s+(?:apply|express)|i\s+would\s+like\s+to\s+apply|please\s+accept)", re.I)
PHRASE_MIN_WORDS, PHRASE_MAX_WORDS = 3, 16
SENTENCE_MAX_WORDS = 35          # v3.4: sentence 1 is at most this many words (tailoring/fluency.py); a longer one has its quoted phrase trimmed
_EXPERIENCE_LEAD = re.compile(r"^(?:(?:demonstrated|proven|strong|solid|extensive|deep|hands-on|practical|good|excellent)\s+)?"
                              r"(?:experience|knowledge|understanding|expertise)(?:\s+(?:of|in|with|building|designing|developing|delivering))?\s+", re.I)
_TRAIL_STOP = frozenset("and or with of for the a an to in on including such as like using by from into at".split())
CUES = re.compile(r"\b(?:hiring|seeking|looking (?:for|to)|solv\w*|build\w*|develop\w*|design\w*|deliver\w*|mission|responsib\w*|you will|help\w*|enabl\w*|"
                  r"improv\w*|optimi[sz]\w*|driv\w*|creat\w*|ship\w*|deploy\w*|forecast\w*|automat\w*|detect\w*|predict\w*|pipelines?|models?|platform|"
                  r"customers?|clients?|products?)\b", re.I)
_NOT_ABOUT_THE_WORK = re.compile(r"equal opportunit|diversity|inclusion|belonging|benefits|salary|privacy|fraud|disabilit|accommodation|culture|"
                                 r"apply (?:now|today)|click|cookie|notice|disclaimer|background check|visa|sponsorship|right to work", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")
_CLAUSE = re.compile(r"\s*(?:[,;:]|\s[-–—]\s)\s*")
_LEADING = re.compile(r"^(?:and|or|but|so|which|that|while|also|then|including)\s+", re.I)
_TO_TAIL = re.compile(r"\b(?:to|so that|in order to)\s+(?=\w)", re.I)
LEAD = {"ad": '{company} is hiring for "{phrase}"', "company_page": '{company} describes its work as "{phrase}"'}
_SENTENCE_START = frozenset("""at in on during for across through with using after before as when while over within throughout from""".split())


def lower_first(fact: str) -> str:
    """A fact that starts with a sentence-opening word ("At AstraZeneca I built ...") continues after the semicolon in lower case; "I ..." and a
    name stay as they are."""
    first = fact.split(" ", 1)[0]
    return fact[:1].lower() + fact[1:] if first.lower() in _SENTENCE_START and first[:1].isupper() else fact


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).split())


def ad_sentences(text: str) -> list[str]:
    """The sentences of a job ad or a web page: every line (a bullet, a heading is too short to count) and every sentence of a line, five words or more."""
    out: list[str] = []
    for line in str(text or "").splitlines():
        line = _BULLET.sub("", line).strip()
        out += [s.strip() for s in sentences(line) if len(s.split()) >= 5]
    return out


def _bank_number_text(fb: dict) -> str:
    """Every string of the fact bank except the deny list and the hook library (a hook line's number must be the bank's own, not its own)."""
    parts: list[str] = []

    def walk(x) -> None:
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k not in ("deny_terms", "hook_library"):
                    walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(fb or {})
    return " ".join(parts)


def bank_numbers(text: str, fb: dict) -> list[str]:
    """The numbers of `text` that the fact bank (bullets, results, profile ...) also holds, in the order the text says them."""
    held = numbers(_bank_number_text(fb))
    seen: list[str] = []
    for m in re.finditer(r"(?<![\w.])[£$€]?\d[\d,]*(?:\.\d+)?\s*(?:%|k|m|x|ms|s)?", text, re.I):
        n = next(iter(numbers(m.group(0))), "")
        if n in held and n not in seen:
            seen.append(n)
    return seen


# ------------------------------------------------------------------------------------------------------------ the problem phrase
def _candidates(sentence: str) -> list[str]:
    """The stretches of one ad sentence a letter could quote: the sentence, its clauses, and what follows "to" ("is hiring to design X" -> "design X")."""
    parts = [sentence, *_CLAUSE.split(sentence)]
    for m in _TO_TAIL.finditer(sentence):
        tail = sentence[m.end():]
        parts += [tail, *_CLAUSE.split(tail)[:1]]
    out = []
    for p in parts:
        p = p.strip(" .;:,–—-")
        while _LEADING.match(p):
            p = _LEADING.sub("", p, count=1).strip()
        if PHRASE_MIN_WORDS <= len(p.split()) <= PHRASE_MAX_WORDS and '"' not in p and p not in out:
            out.append(p)
    return out


def pick_phrase(text: str, hint: Sequence[str], kws: Sequence[Keyword], deny: Sequence[str]) -> str | None:
    """The clause of `text` (an ad or a company page) to quote as the problem the company is hiring to solve: the stretch of an ad sentence about the
    work (not diversity, benefits or notices) that carries most of the hook's terms and the ad's weighted keywords. A verbatim stretch of the text."""
    want = [str(h).casefold() for h in hint if str(h).strip()]
    best: tuple[float, int, str] | None = None
    for s in ad_sentences(text):
        if _NOT_ABOUT_THE_WORK.search(s):
            continue
        for cand in _candidates(s):
            if deny_hits(cand, list(deny)) or not CUES.search(cand):
                continue
            low = cand.casefold()
            weight = sum(kws[i].weight for i in keyword_hits(cand, list(kws)))
            score = 2.0 * sum(1 for w in want if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low)) + weight + 0.5 * len(CUES.findall(cand))
            key = (score, -len(cand.split()), cand)
            if best is None or key[:2] > best[:2]:
                best = key
    return best[2] if best else None


def fit_phrase(phrase: str, limit: int) -> str:
    """`phrase` cut to at most `limit` words, only from its ends so it stays the ad's own words: a closing bracket first ("... LangGraph (and
    LangChain or similar)" -> "... LangGraph"), then a lead such as "Demonstrated experience building", then the last words (never ending on
    "and", "with", "of" ...). Never under PHRASE_MIN_WORDS words."""
    p = " ".join(phrase.split())
    while len(p.split()) > limit:
        if p.endswith(")") and "(" in p and len(p[:p.rfind("(")].split()) >= PHRASE_MIN_WORDS:
            p = p[:p.rfind("(")].rstrip(" ,;")
            continue
        m = _EXPERIENCE_LEAD.match(p)
        if m and len(p[m.end():].split()) >= PHRASE_MIN_WORDS:
            p = p[m.end():]
            continue
        words = p.split()[:-1]
        while words and words[-1].lower().strip(",;") in _TRAIL_STOP:
            words.pop()
        if len(words) < PHRASE_MIN_WORDS:
            break
        p = " ".join(words).rstrip(",;")
    return p


# ------------------------------------------------------------------------------------------------------------------ the opening
@dataclass
class Opening:
    sentence: str = ""              # sentence 1
    paragraph: str = ""             # P1: sentence 1, and the role family's profile line after it when the fact is a bullet
    fact_kind: str = ""             # "hook" | "bullet"
    hook_id: str = ""
    bullet_id: str = ""
    number: str = ""
    problem: str = ""               # the quoted phrase
    source: str = ""                # "ad" | "company_page"
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _fact(job: dict, fb: dict, kws: Sequence[Keyword], hk: Hook | None, chosen: set[str] | None, notes: list[str]) -> tuple[str, str, str, str, list[str]]:
    """(fact text, kind, hook id, bullet id, terms the phrase should favour) of the first fact that can open the letter, or ("", ...)."""
    from . import tailor as T

    def on_cv(bid: str) -> bool:
        return not chosen or bid in chosen

    cands: list[tuple[str, str, str, list[str]]] = []
    if hk is not None and hk.hook_id:
        cands.append((hk.letter_sentence, hk.hook_id, hk.proof_bullet_id, []))
    for hit in (iter_eligible(fb, list(kws), []) if hk is not None else ()):          # no hook on the CV (off, or none qualified): no hook letter line
        entry = hit["entry"]
        if not (hk is not None and hk.hook_id == str(entry["id"])):
            ids = hook_mod.proof_ids(entry)                                   # v3.5.3: a list of proofs: the first one on this CV
            proof = next((b for b in ids if on_cv(b)), ids[0] if ids else "")
            cands.append((" ".join(str(entry["letter_line"]).split()), str(entry["id"]), proof, [str(t) for t in entry.get("terms") or []]))
    for text, hid, proof, terms in cands:
        text = " ".join(str(text).split())
        if BANNED.search(text):
            notes.append(f"opening: hook {hid} skipped (its letter line is a banned opening)")
        elif not bank_numbers(text, fb):
            notes.append(f"opening: hook {hid} skipped (its letter line has no number from the fact bank)")
        elif not on_cv(proof):
            notes.append(f"opening: hook {hid} skipped (its proof bullet {proof} is not on this CV)")
        else:
            return text, "hook", hid, proof, terms
    for b in T.ranked_letter_bullets(fb, list(kws), chosen or None, job.get("description") or ""):
        if bank_numbers(b["text"], fb) and not BANNED.search(b["text"]):
            notes.append(f"opening: the strongest numbered bullet of the CV ({b['id']}) opens the letter")
            return " ".join(b["text"].split()), "bullet", "", b["id"], [str(t) for t in b.get("terms") or []]
    return "", "", "", "", []


def build(job: dict, fb: dict, kws: Sequence[Keyword], hk: Hook | None, chosen: set[str] | None, family_line: str = "", page_text: str = "") -> Opening:
    """Sentence 1 and P1 of the letter, or an Opening whose `error` says what is missing (no numbered fact, no phrase to quote)."""
    out = Opening()
    fact, kind, hid, bid, terms = _fact(job, fb, kws, hk, chosen, out.notes)
    if not fact:
        out.error = "no hook line or bullet of this CV carries a number from the fact bank, so there is nothing numbered to open the letter with"
        return out
    deny = fb.get("deny_terms", [])
    jd = job.get("description") or ""
    for source, text in (("ad", jd), ("company_page", page_text)):
        phrase = pick_phrase(text, terms, kws, deny) if text.strip() else None
        if phrase:
            break
    else:
        out.error = ("no problem phrase to quote: the job ad has no clause about the work" +
                     (", and the company page holds none" if page_text.strip() else ", and there is no company page text (the careers page was not fetched)"))
        return out
    from .letter_rules import i_clause

    core = lower_first(fact.rstrip(".!? ")) if kind == "hook" else i_clause(fact.rstrip(".!? "))
    rest = len(f"{LEAD[source].format(company=job['company'], phrase=phrase)}; {core}.".split()) - len(phrase.split())
    if rest + len(phrase.split()) > SENTENCE_MAX_WORDS:                                  # RightShip, 3 Nov 2026: a 40-word sentence 1
        short = fit_phrase(phrase, max(PHRASE_MIN_WORDS, SENTENCE_MAX_WORDS - rest))
        if short != phrase:
            out.notes.append(f"opening: the quoted phrase was trimmed to keep sentence 1 at {SENTENCE_MAX_WORDS} words or fewer")
            phrase = short
    out.sentence = f"{LEAD[source].format(company=job['company'], phrase=phrase)}; {core}."
    out.paragraph = out.sentence if kind == "hook" else " ".join(x for x in (out.sentence, " ".join(family_line.split())) if x)
    out.fact_kind, out.hook_id, out.bullet_id, out.problem, out.source = kind, hid, bid, phrase, source
    out.number = (bank_numbers(core, fb) or bank_numbers(out.sentence, fb))[0]       # the fact's own number, not one the quoted phrase happens to hold
    return out


def problems(paragraph: str, fb: dict, texts: Sequence[str]) -> list[str]:
    """What is wrong with the opening paragraph of a letter (built here or typed by the person): a banned start, no number the fact bank holds in
    sentence 1, no quoted problem phrase, or a phrase that none of `texts` (the job ad, the company's own page) says."""
    first = (sentences(paragraph) or [""])[0]
    out: list[str] = []
    m = BANNED.search(first)
    if m:
        out.append(f"opening: starts with a banned phrase ('{m.group(0).strip()}')")
    if not bank_numbers(first, fb):
        out.append("opening: no number from the fact bank")
    quoted = re.findall(r'"([^"]+)"', first)
    if not quoted:
        out.append("opening: no problem phrase from the ad or the company's page")
    elif not any(_norm(quoted[-1]) in _norm(t) for t in texts if t):
        out.append(f'opening: the problem phrase "{quoted[-1]}" is not in the ad or the company\'s own page')
    return out
