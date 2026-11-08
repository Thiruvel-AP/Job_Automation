"""The fluency pass (v3.4): the assembled letter stays the FACT SOURCE; the model only rewrites it into natural prose, and code decides whether the
rewrite may be used.

Why: the assembled letter (assembled_letter.py) is faithful but reads as pasted CV bullets. The RightShip letter of 3 Nov 2026 printed one
sentence twice (the same bullet in P3 and P4), a 70-word sentence, "What draws me to this role is RightShip is ..." and "The result, as
measured: ~65 tests across 8 files" right after P1 had said ~65 tests.

Every rewrite (the letter's P1 to P5 here; the motivation answer in apply/freetext.py, which uses the same checks) must pass, in code:
  (a) facts: every number, capitalised name and tool of the rewrite is in the fact source (the assembled draft). The ad is a source only for
      what is said about the company: P5 and the quoted ad phrase of P1. A claim about you never comes from the ad (CLAUDE.md);
  (b) no sentence over MAX_SENTENCE_WORDS (35) words;
  (c) no two sentences share 5-word shingles at REPEAT (0.5, Jaccard) or more, and no sentence repeats one of another paragraph (0.5 of the
      shorter sentence's shingles are in the other);
  (d) no banned opening: a greeting, "I am writing", "I am applying", "I would like to apply", "please accept", "I am excited"; and no
      sentence of the ad pasted in (ad_paste_problems);
  (e) the guards that already judge the draft: company_letter.check (except the "pastes a fact-bank bullet" check on P3 / P4: restating the
      verified bullets is what this pass is for) and the reuse check against earlier letters.
TRIES (2) model calls, the second shown its own rejected rewrite and told to fix only what was rejected; then the assembled draft is used as it is (`source: draft`). P5 (already
judged by its own company guards), P6 and P7 are never sent. The prompt gives the word budget of P1 to P4 (the letter's range minus the words of
P5 to P7). Off with `letter.fluency: false`.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Mapping, Sequence

from .faithfulness import numbers
from .grounding import _names, name_allowed, sentences
from .letter_reuse import jaccard, shingles
from .scorer import norm_literal

TASK = "cover_letter"
TRIES = 2
MAX_SENTENCE_WORDS = 35
REPEAT = 0.5
LETTER_REUSE = 0.30                     # the motivation answer against this job's letter (5-word shingles, Jaccard): must stay under this
REWRITTEN = ("P1", "P2", "P3", "P4")    # the paragraphs about you; P5 (about the company) has its own guards and is not rewritten
ABOUT_COMPANY: tuple[str, ...] = ()     # paragraphs that may take words from the ad (P1's quoted phrase is checked by opening.problems)
BANNED_OPENINGS = re.compile(
    r"^\s*(?:(?:hi|hello|dear|hey|greetings)\b|i(?:\s+am|'m)\s+(?:writing|applying|excited|thrilled|delighted)\b|"
    r"i(?:\s+would|'d)\s+like\s+to\s+apply\b|please\s+accept\b)", re.I)
_QUOTED = re.compile(r'"[^"]*"')


def enabled(settings) -> bool:
    """letter.fluency (code default: on)."""
    raw = getattr(settings, "raw", settings) or {}
    v = ((raw.get("letter") or {}) if isinstance(raw, dict) else {}).get("fluency", True)
    return v is not False and str(v).strip().lower() not in ("false", "off", "no", "0")


# ------------------------------------------------------------------ the checks
def _word_in(lit: str, term: str) -> bool:
    t = norm_literal(term)
    return bool(t) and re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", lit) is not None


def _name_ok(pool: str, name: str) -> bool:
    """name_allowed, singular or plural ("API" when the draft says "APIs")."""
    return name_allowed(pool, name) or name_allowed(pool, name + "s") or (len(name) > 3 and name.endswith("s") and name_allowed(pool, name[:-1]))


def fact_problems(text: str, source: str, ad: str = "", tools: Sequence[str] = (), label: str = "") -> list[str]:
    """(a) Every number, capitalised name and tool of `text` must be in `source` (or in `ad`, when one is given). Quoted text is left to the caller
    (P1's quoted phrase must be the ad's own words: opening.problems)."""
    body = _QUOTED.sub(" ", text)
    pool = f"{source}\n{ad}" if ad else source
    where = "the draft or the ad" if ad else "the draft"
    out: list[str] = []
    extra = numbers(body) - numbers(pool)
    if extra:
        out.append(f"{label}number(s) not in {where}: {sorted(extra)}")
    lit_pool = norm_literal(pool)
    for s in sentences(body):
        for name in _names(s):
            if not _name_ok(lit_pool, name):
                out.append(f"{label}'{name}' (a name) is not in {where}")
    lit = norm_literal(body)
    for tool in tools:
        if len(norm_literal(tool)) >= 2 and _word_in(lit, tool) and not _word_in(lit_pool, tool):
            out.append(f"{label}names '{tool}', which is not in {where}")
    return list(dict.fromkeys(out))


def length_problems(parts: Mapping[str, str], limit: int = MAX_SENTENCE_WORDS) -> list[str]:
    """(b) No sentence over `limit` words."""
    out = []
    for name, text in parts.items():
        for s in sentences(text or ""):
            n = len(s.split())
            if n > limit:
                out.append(f"{name}: a sentence of {n} words (at most {limit}): '{s.strip()[:60]}'")
    return out


def _containment(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def repeat_problems(parts: Mapping[str, str]) -> list[str]:
    """(c) No two sentences share 5-word shingles at REPEAT or more (Jaccard), and no sentence repeats one of another paragraph (REPEAT of the
    shorter one's shingles are in the other)."""
    rows = [(name, s.strip(), shingles(s)) for name, text in parts.items() for s in sentences(text or "")]
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            (na, sa, ha), (nb, sb, hb) = rows[i], rows[j]
            jac = jaccard(ha, hb)
            if jac >= REPEAT:
                out.append(f"{na} and {nb} say the same sentence twice ({jac:.2f} of 5-word runs shared): '{sb[:60]}'")
            elif na != nb and _containment(ha, hb) >= REPEAT:
                out.append(f"{nb} repeats what {na} already says: '{sb[:60]}'")
    return list(dict.fromkeys(out))


AD_PASTE = 0.5                          # a sentence with this share of its 5-word shingles in the ad is the ad's sentence pasted in
AD_PASTE_MIN_WORDS = 5
AD_OPENING = 6                          # ... or one that opens with the first 6 words of an ad line ("Rapidly prototype and experiment with ...")
_WORDS = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_LINE_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")
GROW_MARGIN = 30                        # with the pass on, the draft grows to the range's low end + this: a rewrite runs ~10 % shorter


def ad_paste_problems(parts: Mapping[str, str], ad: str) -> list[str]:
    """No sentence of 8+ words is a sentence of the ad pasted in (AD_PASTE of its 5-word shingles in the ad); quoted text is not counted.
    RightShip, 8 Nov 2026: "Rapidly prototype and experiment with LLM-based applications, ..." (the ad's bullet) in the motivation answer."""
    if not ad.strip():
        return []
    ash = shingles(ad)
    starts = {tuple(_WORDS.findall(s.casefold())[:AD_OPENING]) for line in ad.splitlines() for s in sentences(_LINE_BULLET.sub("", line))
              if len(_WORDS.findall(s.casefold())) >= AD_OPENING}
    out = []
    for name, text in parts.items():
        for s in sentences(_QUOTED.sub(" ", text or "")):
            sh = shingles(s)
            opening = tuple(_WORDS.findall(s.casefold())[:AD_OPENING])
            if len(s.split()) >= AD_PASTE_MIN_WORDS and sh and (len(sh & ash) / len(sh) >= AD_PASTE or opening in starts):
                out.append(f"{name}: pastes a sentence of the ad: '{s.strip()[:60]}'")
    return out


_BASE_VERBS = frozenset("""design build develop deploy write explore communicate lead own create deliver drive partner collaborate maintain improve
shape help support define run apply present contribute propose produce gather translate prototype experiment evaluate integrate work join learn
discuss talk meet hear bring use""".split())
_GERUND_AFTER = re.compile(r"\b(?:look(?:s|ing|ed)?\s+forward\s+to|interested\s+in|excited\s+about)\s+(?:\w+ly\s+)?(?P<w>[a-z]+)\b", re.I)
_IS_IS = re.compile(r"\bis\s+(?:[A-Z][\w'’-]*\s+){1,3}is\b")


def grammar_problems(parts: Mapping[str, str]) -> list[str]:
    """Two errors the letters made: a base verb after "look forward to" / "interested in" ("I look forward to rapidly prototype", 8 Nov 2026), and
    "is <Name> is" ("What draws me to this role is RightShip is ...", 3 Nov 2026)."""
    out = []
    for name, text in parts.items():
        for m in _GERUND_AFTER.finditer(text or ""):
            if m.group("w").lower() in _BASE_VERBS:
                w = m.group("w")
                ing = (w[:-1] if w.endswith("e") and not w.endswith("ee") else w) + "ing"
                out.append(f"{name}: '{m.group(0)}' needs the -ing form ('{ing}')")
        m = _IS_IS.search(text or "")
        if m:
            out.append(f"{name}: '{m.group(0)}' is two verbs in a row: write it as one clause")
    return out


def unfinished_problems(parts: Mapping[str, str]) -> list[str]:
    """Every paragraph ends a sentence (". ! ?"): RightShip, 8 Nov 2026, an answer ended "... experiment with LLM-based applications"."""
    return [f"{name}: ends mid-sentence ('...{text.strip()[-40:]}')" for name, text in parts.items()
            if text and text.strip() and text.strip()[-1] not in '.!?"\'”']


def opening_problems(text: str, label: str = "") -> list[str]:
    """(d) The text does not open with a greeting, "I am writing / applying", "I would like to apply", "please accept" or "I am excited"."""
    m = BANNED_OPENINGS.search(text or "")
    return [f"{label}opens with a banned phrase ('{m.group(0).strip()}')"] if m else []


def letter_problems(parts: Mapping[str, str], draft: Mapping[str, str], ad: str, tools: Sequence[str]) -> list[str]:
    """(a) to (d) for a rewritten letter: `parts` P1 to P7, `draft` the assembled letter it was rewritten from."""
    source = "\n".join(str(v) for v in draft.values() if v)
    out: list[str] = []
    for name in REWRITTEN:
        if draft.get(name) and not str(parts.get(name) or "").strip():
            out.append(f"{name} is missing (the draft has it)")
        if parts.get(name):
            out += fact_problems(parts[name], source, ad if name in ABOUT_COMPANY else "", tools, f"{name}: ")
    if any(n in parts and n not in draft for n in REWRITTEN):
        out.append("a paragraph the draft does not have was added")
    body = {n: parts[n] for n in REWRITTEN if parts.get(n)}
    out += length_problems(body)
    out += repeat_problems(body)
    out += ad_paste_problems({n: v for n, v in body.items() if n != "P1"}, ad)
    out += unfinished_problems(body)
    out += grammar_problems(body)
    out += opening_problems(parts.get("P1") or "", "P1: ")
    return list(dict.fromkeys(out))


def letter_overlap_problems(text: str, letter: str) -> list[str]:
    """The motivation answer against this job's letter: 5-word-shingle Jaccard under LETTER_REUSE, and no sentence of it taken from the letter
    (REPEAT or more of the sentence's shingles in the letter)."""
    if not text.strip() or not letter.strip():
        return []
    lsh = shingles(letter)
    out = []
    score = jaccard(shingles(text), lsh)
    if score >= LETTER_REUSE:
        out.append(f"reuses this job's letter: overlap {score:.2f} of 5-word runs (limit {LETTER_REUSE})")
    for s in sentences(text):
        sh = shingles(s)
        if sh and len(sh & lsh) / len(sh) >= REPEAT:
            out.append(f"takes a sentence from this job's letter: '{s.strip()[:60]}'")
    return out


# ------------------------------------------------------------------ the letter's rewrite
LETTER_SYSTEM = """You copy-edit a cover letter so it reads as natural, fluent English. The DRAFT is right on the facts but stiff: pasted CV
bullets, long lists, repeated points. Rewrite each paragraph you are given and keep its job:
  P1  ONE sentence: <Company> is hiring for "<words copied exactly from the AD>"; then the proof, keeping its number and how it was validated.
      You may shorten the quoted phrase to its most relevant words; they stay copied exactly from the AD, inside double quotes.
  P2  what that proof showed, briefly.
  P3  what was built and with what; it must still name: {skills}.
  P4  one sentence per employer, in this order: {employers}; each names the employer and keeps one number of its draft sentence.
Length: P1 to P4 together {lo} to {hi} words (the draft's are {now}); aim for about {aim}. The rest of the letter is fixed. Keep each
paragraph close to its draft length ({sizes}): split long sentences and smooth the wording; do not summarise.
Hard rules, every one checked by code (a rewrite that breaks one is thrown away):
- Facts: every number, name, tool and organisation you write must already be in the DRAFT, spelled as the DRAFT spells it (only P1's quote
  comes from the AD). Never move a fact to another employer; add no outcome, adjective, skill or tool.
- No sentence over 35 words (aim for 25 or fewer): split long sentences; cut a long list in brackets down to the two or three tools that
  matter for this ad.
- Say each thing once: no sentence or fact repeats another paragraph.
- First person ("I"), past tense for past work, acronyms as written (LLM, AWS). Never "we", "our" or "us". Never open with "I am writing",
  "I am applying", "I am excited" or a greeting.
- No filler: {filler}.
Return JSON only, with exactly the paragraphs you were given: {{"P1": "...", "P2": "...", ...}}."""
LETTER_USER = """COMPANY: {company}
ROLE: {title}
DRAFT (the only source of facts about the candidate):
{draft}
AD (for P1's quoted phrase and P5 only; never a source for a claim about the candidate):
{ad}
{feedback}"""
AD_CHARS = 6000


def _spent(llm, start: int, estimate: int) -> int:
    """Tokens of the calls since `start`: the router's own count when it has one, else the estimate (characters / 4)."""
    rows = list(getattr(llm, "usage", []) or [])[start:]
    real = sum(int(r.get("total_tokens") or 0) for r in rows if isinstance(r, dict))
    return real or estimate


def polish_letter(llm, draft: Mapping[str, str], job: Mapping, ad: str, tools: Sequence[str], judge: Callable[[dict], list[str]], *,
                  skills: Sequence[str], employers: Sequence[str], filler: Sequence[str], notes: list[str],
                  words: tuple[int, int] = (200, 320)) -> tuple[dict, dict]:
    """(the letter's parts, info): the model's rewrite of P1 to P5 when one of TRIES passes (a) to (d) and `judge` (e); else the draft as it is.
    info = {"source": "model" | "draft", "tries", "problems" (per try), "tokens"}."""
    from ..llm.router import TaskUnavailable
    from . import tailor as T

    info: dict = {"source": "draft", "tries": 0, "problems": [], "tokens": 0}
    send = {n: str(draft[n]) for n in REWRITTEN if draft.get(n)}
    fixed = sum(len(str(v).split()) for n, v in draft.items() if n not in send and v)
    lo, hi = max(0, words[0] - fixed), max(0, words[1] - fixed)
    system = LETTER_SYSTEM.format(skills=", ".join(skills) or "(none)", employers=", ".join(employers) or "(none)", filler=", ".join(filler),
                                  lo=lo, hi=hi, aim=(lo + hi) // 2, now=sum(len(v.split()) for v in send.values()),
                                  sizes=", ".join(f"{n} about {len(v.split())} words" for n, v in send.items()))
    feedback = ""
    start = len(getattr(llm, "usage", []) or [])
    estimate = 0
    for attempt in range(TRIES):
        user = LETTER_USER.format(company=job["company"], title=job["title"], draft=json.dumps(send, indent=1, ensure_ascii=False),
                                  ad=(ad or "")[:AD_CHARS], feedback=feedback)
        info["tries"] = attempt + 1
        try:
            out = T._llm_json(llm, TASK, system, user, {"mode": "fluency", "draft": dict(send), "company": job["company"], "title": job["title"],
                                                        "temperature": 0.2 if attempt == 0 else 0.5})
        except TaskUnavailable as e:
            info["problems"].append([f"no provider can take the rewrite now: {str(e)[:120]}"])
            break
        except Exception as e:  # noqa: BLE001  (a bad reply, no network: the assembled draft is the letter)
            info["problems"].append([f"the rewrite failed: {str(e)[:120]}"])
            break
        got = {n: " ".join(str((out or {}).get(n) or "").split()) for n in send}
        estimate += (len(system) + len(user) + len(json.dumps(got))) // 4
        parts = {**dict(draft), **got}
        probs = letter_problems(parts, draft, ad, tools)
        if not probs:
            probs = judge(parts)
        if not probs:
            info.update(source="model", problems=info["problems"], tokens=_spent(llm, start, estimate))
            return parts, info
        info["problems"].append(probs)
        said = sum(len(v.split()) for v in got.values())
        if any("a sentence of" in p for p in probs):
            probs = probs + ["split every sentence named above into two or three sentences of at most 25 words each"]
        if any("the letter must be between" in p for p in probs):
            probs = probs + [f"P1 to P4 must come to {lo} to {hi} words: put back details from the DRAFT that you cut, do not invent new ones"]
        feedback = (f"YOUR LAST REWRITE ({said} words in P1 to P4) WAS REJECTED:\n{json.dumps(got, indent=1, ensure_ascii=False)}\n"
                    "Fix exactly these problems IN THAT REWRITE and change nothing else (the DRAFT stays the only source of facts):\n- "
                    + "\n- ".join(probs[:12]))
    info["tokens"] = _spent(llm, start, estimate)
    notes.append(f"cover_letter: fluency rewrite not used ({info['tries']} tr{'y' if info['tries'] == 1 else 'ies'}); the assembled draft is the letter"
                 + (f" (last: {'; '.join(info['problems'][-1])[:200]})" if info["problems"] else ""))
    return dict(draft), info
