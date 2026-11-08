"""The cover letter of `letter.mode: company` (v2.3): up to seven short paragraphs, 200-320 body words, no bullets.

  P1  Hook        the company's problem first, then the proof: `<Company> is hiring for "<a clause of the job ad>"; <the hook's letter line>.`
                  The clause is verbatim from the ad (else from the company's own careers page); the hook's line is the user's own sentence, with
                  a number the fact bank holds, and ends with how it was validated (tailoring/opening.py). Written in code.
  P2  What it shows   the hook fact's `limits` field, else its `result` field, word for word (the hook entry first, then its proof bullet).
                  Left out when the fact bank has neither, and (v3.4) when it repeats a number P1 already gives. Written in code.
  P3  JD-matched skills   the ad's 2 or 3 required skills, shown through a second CV project or bullet (never the hook's own bullet). Model.
  P4  Experience roll-up  one sentence per role (the most relevant role first, then the next): the employer, what I owned, one number. Model.
  P5  Why this team       ONE sentence that quotes or closely follows a sentence of the ad (else of the company's page). Model.
  P6  One honest gap      "One honest gap: I have used <X> only, never <Y>, so <Y> is where I will learn." Y = the heaviest REQUIRED ad keyword the fact
                  bank cannot claim (the gap report's top gap that is a tool, platform, language or framework; never a visa, a degree or years of
                  experience); X = a neighbour skill the fact bank really says. Included only when `letter.honest_gap` is true or the person keeps
                  it in review. Written in code.
  P7  The ask     `Could we spend 20 minutes on the first problem the <role> would own, around <the ad's problem>?` (+ ", and what the
                  <assessment> covers", when the ad names one). Written in code from `letter.ask`.

The model (cover_letter task) writes P3, P4 and P5, up to ATTEMPTS (3) times, each redraft told its previous draft, every rejection of it as an
instruction and, after review's `l`, the reader's feedback word for word (letter_feedback.py); the prompt carries the paragraph table above, so
"shorten P4" works by name. "drop P2" / "drop P6" / "keep P6" in the feedback are read in code. A third rejection is NO LETTER and the review
screen says why (status "none", `reason`, `retry.failures`; `draft_body` keeps the last draft). Nothing falls back to a template. Review's `e`
runs the same guards on the edited text (`edit_problems`) and installs it (`install_edited`).

User guard (P3, P4: what the letter says about the user). Every number must be in the chosen bullets (or the opening); every fact-bank tool and
organisation named must be one of theirs; every sentence is checked against its own bullets (`grounding_problems`); P3 names at least two of its
skills; P4 names each role's employer and gives a number of its bullet; no filler word, no pasted bullet, no deny-listed term, no keyword list,
no "we" / "our", no "I help <Company>", no noun phrase that links one bullet's object to another bullet's tool.

Company guard (P5: what the letter says about the employer). It checks FACTS against the ad (and the company page): every claim of the sentence
must have at least COMPANY_SUPPORT of its stems in one window of up to WINDOW consecutive ad sentences, every number must be in the ad, every
capitalised name must be in the ad, the fact bank or the opening, and the sentence must follow one of the ad's About / role sentences.

Body (Document rules, B3 and B5): P3 and P4 are written from bullets that this job's CV prints, a skill they name must be in the CV's Skills or
bullets, and at least 60 % of the sentences of P1 to P4 are about those bullets (`balance_problems`). P2 to P5 are compared with every earlier
letter (letter_reuse.py): 0.30 or more is redrafted once, told `do not reuse these sentences: ...`, and a second copy is no letter.

Length: the BODY (P1 to P7; the greeting, sign-off and name are not counted) must be inside `letter.company_words` ([low, high], default
[200, 320]; high never over 320, and never over `scoring.letter_max_words_company`); the model is asked for the middle of the range. Beside the PDF
the letter is written as `letter.txt` (plain text, the same words) for forms with a cover-letter text box (apply/assist.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import ats
from .. import hook as hook_mod
from ..hook import Hook, org_short
from ..llm.router import TaskUnavailable
from . import companypage as CP
from . import evidence_fields
from . import letter_feedback as LF
from . import letter_reuse as RU
from . import letters as L
from . import opening as OP
from . import prompts as P
from .faithfulness import deny_hits, numbers
from .grounding import _has, _names, bank_text, grounding_problems, name_allowed, repeated_evidence_problems, sentences
from .letter_rules import bank_names, gap_display, term_dump_problems
from .scorer import Keyword, gap_terms, keyword_hits, norm_literal, norm_stem

LOW_BOUND, HIGH_BOUND = 200, 320          # letter.company_words: [low, high] with 200 <= low <= high <= 320 (the Document rules' body length)
DEFAULT_RANGE = (200, 320)
BODY_CITING = 0.60                        # at least this share of the sentences of P1 to P4 must be about the CV's bullets
ATTEMPTS = 3                              # the draft and up to two redrafts, each told every rejection so far
TEMPERATURES = (0.0, 0.4, 0.7)            # draft 1 is exact; each redraft is allowed to wander further from the rejected one
SAME_DRAFT = 0.8                          # a draft this much (5-word shingles) like the one before it: the model is stuck, no more tries
COMPANY_SUPPORT = 0.6                     # share of a P5 clause's content stems that one window of the ad must hold
WINDOW = 3                                # consecutive ad sentences that may together support one clause
MIN_CLAIM_STEMS = 3                       # a clause with fewer content stems must have all of them somewhere in the ad
MODE = "company"
ABOUT_LIMIT = 10                          # how many of the ad's About / role sentences the prompt offers for P5
P5_MIN_USED = 1                           # P5 quotes or closely follows this many of them (fewer offered: all of them)
P5_FOLLOWS = 0.6                          # share of a P5 sentence's content stems that one offered ad sentence must hold ...
P5_MIN_SHARED = 3                         # ... and at least this many stems, for that ad sentence to count as followed
P5_MAX_WORDS = 45                         # P5 is one sentence
NAMES = ("P1", "P2", "P3", "P4", "P5", "P6", "P7")
MODEL_NAMES = ("P3", "P4", "P5")
OPTIONAL = ("P2", "P6")
REUSE_NAMES = ("P2", "P3", "P4", "P5")     # the 5-word-shingle check (Document rules, B5)
CITED_NAMES = ("P1", "P2", "P3", "P4")     # the sentences the 60 % evidence rule counts (Document rules, B3)
ASK = "Could we spend 20 minutes on the first problem the {role} would own, around {problem}{assessment}?"
ASK_PLAIN = "Could we spend 20 minutes on the first problem the {role} would own{assessment}?"
ASK_ASSESSMENT = ", and what the {assessment} covers"
# filler the letter never uses (the prompt says so; the user guard's own checks are unchanged)
FILLER = ("robust", "seamless", "cutting-edge", "passionate", "leverage", "synergy")
# memory, RAM and GB figures (Document rules, B4): allowed in the body only when the ad is about memory, hardware, efficiency, GPUs or edge deployment
_HARDWARE_FIGURE = re.compile(r"\b\d[\d,.]*\s*(?:gb|gib|mb|tb)\b|\bram\b|\bvram\b|\bmemory footprint\b", re.I)
_HARDWARE_JD = re.compile(r"\b(?:memory|ram|vram|hardware|efficien\w*|gpus?|cuda|edge|on-device|embedded|silicon|accelerat\w*|quantis\w*|quantiz\w*|"
                          r"low-latency|latency|inference|fpga|asic|tpu|chips?)\b", re.I)
# a hard filter is never written up as a gap (Document rules, B5): a visa, a degree, years of experience
_HARD_FILTER = re.compile(r"\b(?:visa|sponsor\w*|right to work|work permit|citizen\w*|clearance|degree|bachelor\w*|master\w*|msc|bsc|phd|doctorate|"
                          r"diploma|years?)\b", re.I)
_ASSESSMENT = re.compile(r"\b(?:(?:technical|online|coding|take-home|skills|aptitude|psychometric|written|data)\s+(?:assessment|test|challenge|exercise|interview)|"
                         r"case study|assessment cent(?:re|er)|assessment)\b", re.I)

# words that carry no claim: function words, and the generic verbs and nouns of any "what we do" sentence
_FUNCTION = frozenset("""the and for with that this from have has had are was were will would can could may might shall should their they them its our
    your you who whom what which when where while into onto over under about above below out off than then such each both been being not but nor
    also only very more most less much many some any all one two its via per across within without between among through during after before until
    here there those these other another own same just still even ever like well too yet now new""".split())
_GENERIC = frozenset("""company companies team teams role roles hiring hire hires hired looking look seeking seek position job join joining work works
    working help helps helping need needs needed solve solves solving problem problems challenge challenges opportunity make makes made use uses used
    using provide provides providing offer offers offering build builds building create creates creating deliver delivers delivering enable enables
    support supports include includes including allow allows get gets give gives take takes bring brings drive drives driving ensure ensures
    focus focuses focused part
    involve involves involved involving require requires required requiring entail entails entailed entailing cover covers covering
    responsible responsibility responsibilities expected expect expects candidate successful ideal person someone""".split())
_SKIP = _FUNCTION | _GENERIC
_CLAUSE = re.compile(r",(?!\d)|[;:]|\s+(?:and|but|while|so|because|which|who|whose|whereas)\s+|\s[-–—]\s")
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")


def _labels(fb: dict) -> dict[str, str]:
    """{normalised label: label} of every employer and project of the fact bank (the short form: the part before the first comma)."""
    out: dict[str, str] = {}
    for sec, kind in (("experience", "experience"), ("projects", "project")):
        for e in (fb or {}).get(sec, []) or []:
            for raw in (org_short(e, kind), e.get("org"), e.get("name")):
                label = re.split(r"\s*,\s*", str(raw or ""))[0].strip()
                if len(label) >= 3:
                    out[norm_literal(label)] = label
    return out


# ------------------------------------------------------------------ settings
def word_range(settings) -> tuple[int, int]:
    """settings letter.company_words: [low, high], two whole numbers with 200 <= low <= high <= 320 (default [200, 320]). They count the BODY."""
    raw = settings["letter"].get("company_words", list(DEFAULT_RANGE))
    bad = ValueError(f"letter.company_words must be [low, high], two whole numbers with {LOW_BOUND} <= low <= high <= {HIGH_BOUND}, got {raw!r}")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or any(isinstance(x, bool) for x in raw):
        raise bad
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        raise bad from None
    if not LOW_BOUND <= lo <= hi <= HIGH_BOUND:
        raise bad
    return lo, hi


def target_words(settings) -> int:
    """The length the model is asked for: the middle of letter.company_words."""
    lo, hi = word_range(settings)
    return (lo + hi) // 2


def word_cap(settings) -> int:
    """The most body words a company-mode letter may have: the top of letter.company_words, or scoring.letter_max_words_company when that is lower
    (never over 320). The other modes keep scoring.letter_max_words."""
    return min(word_range(settings)[1], int(settings["scoring"].get("letter_max_words_company", HIGH_BOUND)), HIGH_BOUND)


def honest_gap_on(settings) -> bool:
    """settings letter.honest_gap (default off): P6 is included in a letter written without a person looking."""
    return bool(settings["letter"].get("honest_gap", False))


def words(paragraphs: list[str]) -> int:
    return len(" ".join(p for p in paragraphs if p).split())


def assemble(parts: dict[str, str]) -> tuple[list[str], list[str]]:
    """(the paragraphs in the letter's order, their names): P1 to P7, the ones that have text."""
    names = [n for n in NAMES if " ".join(str(parts.get(n) or "").split())]
    return [" ".join(str(parts[n]).split()) for n in names], names


def table(parts: dict[str, str]) -> str:
    """The paragraph table of the prompt (Document rules, B7): every paragraph by name, who writes it, and what it says now."""
    who = {"P1": "hook, written in code", "P2": "what it shows, written in code", "P3": "JD-matched skills, YOU write it",
           "P4": "experience roll-up, YOU write it", "P5": "why this team, YOU write it", "P6": "one honest gap, written in code",
           "P7": "the ask, written in code"}
    return "\n".join(f"{n} ({who[n]}): {parts.get(n) or ('(none)' if n in OPTIONAL else '(to be written)')}" for n in NAMES)


def hardware_jd(jd: str) -> bool:
    """Does the ad talk about memory, hardware, efficiency, GPUs or edge deployment?"""
    return bool(_HARDWARE_JD.search(jd or ""))


def hardware_problems(parts: dict[str, str], jd: str) -> list[str]:
    """Document rules, B4: a memory, RAM or GB figure in the letter's body needs an ad that talks about memory, hardware, efficiency, GPUs or edge."""
    if hardware_jd(jd):
        return []
    return [f"{n}: mentions memory / RAM / GB ('{m.group(0)}'), and the ad is not about memory, hardware, efficiency, GPUs or edge deployment"
            for n in NAMES for m in [_HARDWARE_FIGURE.search(str(parts.get(n) or ""))] if m]


# ------------------------------------------------------------------ the ad
def ad_sentences(jd: str) -> list[str]:
    """The ad as sentences: every line (a bullet, a heading) and every sentence of a line, at least three words long."""
    out: list[str] = []
    for line in (jd or "").splitlines():
        line = _BULLET.sub("", line).strip()
        out += [s.strip() for s in sentences(line) if len(s.split()) >= 3]
    return out


def stems(text: str, skip: frozenset[str] = frozenset()) -> set[str]:
    """The content stems of `text`: three letters or more, not a function word or a generic verb, not in `skip` (stems of the company and the role)."""
    out = set()
    for w in re.findall(r"[a-z][a-z0-9+#]*", norm_literal(text)):
        if len(w) < 3 or w in _SKIP:
            continue
        s = norm_stem(w)
        if s and s not in skip and s not in _SKIP:
            out.add(s)
    return out


# the words of the fallback P5's frame ("<Company>'s focus on <ad phrase> is why I applied."): they say nothing about the company, so the company
# guard does not ask the ad for them (v3.4)
P5_FRAME = frozenset(stems("why applied"))


# A heading line (short, no full stop) that opens a section of the ad about the company / the role / the team (kept), or one about what the
# candidate needs or about boilerplate (dropped). A short line that is neither is a line of the section it sits in.
_KEEP_HEAD = re.compile(r"\b(?:about|who we are|our (?:story|mission|company|team|vision|purpose)|the (?:company|role|team|opportunity|position|job)|"
                        r"company|overview|summary|mission|your impact|impact|responsibilit\w*|what you(?:'|’)?ll do|what you will do|role|team|"
                        r"what we do|why join|why us|description|opportunity|position)\b", re.I)
_SKIP_HEAD = re.compile(r"\b(?:skills?|requirements?|qualifications?|experience|desirable|nice to have|bonus|benefits|perks|what we offer|diversity|"
                        r"inclusion|equal opportunit\w*|equity|belonging|commitment|privacy|notice|disclaimer|fraud|salary|compensation|how to apply|"
                        r"application|about you|who you are|you have|what you(?:'|’)?ll bring|what we(?:'|’)?re looking for)\b", re.I)
_CUE = re.compile(r"\b(?:platform|products?|customers?|clients?|users?|team|mission|role|seeking|hiring|provides?|builds?|helps?|enables?)\b", re.I)
_FLUFF = re.compile(r"\b(?:culture|values?|welcome|celebrat\w*|inspired|unique|winning|diversity|innovators|rewarded)\b", re.I)


def about_sentences(jd: str, company: str = "", kws: list[Keyword] | None = None, limit: int = ABOUT_LIMIT) -> list[str]:
    """The ad's About / role sentences, at most `limit`, in the ad's order: the sentences before the first heading and under headings about the
    company, the role, the team or the impact (not under skills, requirements, benefits, diversity or notices). When there are more than `limit`,
    those that name the company, hit the ad's keywords or say what the company makes win. P2 quotes or closely follows two or three of them."""
    keep, out = True, []
    for line in (jd or "").splitlines():
        text = _BULLET.sub("", line).strip()
        if not text:
            continue
        if len(text.split()) <= 9 and not re.search(r"[.!?]$", text):
            if _SKIP_HEAD.search(text):
                keep = False
                continue
            if _KEEP_HEAD.search(text):
                keep = True
                continue
        if keep:
            out += [s.strip() for s in sentences(text) if len(s.split()) >= 5]
    out = list(dict.fromkeys(out))
    if len(out) <= limit:
        return out
    name = norm_literal(company)

    def score(i: int) -> tuple[int, int]:
        text = out[i]
        return (2 * bool(name and name in norm_literal(text)) + len(keyword_hits(text, kws or [])) + bool(_CUE.search(text)) - bool(_FLUFF.search(text)), -i)
    best = sorted(sorted(range(len(out)), key=score, reverse=True)[:limit])
    return [out[i] for i in best]


def _windows(ad: list[str], skip) -> tuple[list[set[str]], list[set[str]]]:
    """(the stems of each ad sentence, the stems of every run of up to WINDOW consecutive sentences)."""
    each = [stems(s, skip) for s in ad]
    runs = [set().union(*each[i:i + n]) for n in range(1, WINDOW + 1) for i in range(len(each) - n + 1)]
    return each, runs


def clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE.split(sentence) if c and c.strip()]



# ------------------------------------------------------------------ the company guard (P5)
def company_problems(p5: str, jd: str, skip: frozenset[str] = frozenset()) -> list[str]:
    """Every factual claim of P5 must be supported by a phrase of the ad. Normalised overlap, see the module docstring; plus: no number the ad
    does not have."""
    ad = ad_sentences(jd)
    each, runs = _windows(ad, skip)
    in_ad = set().union(*each) if each else set()
    out: list[str] = []
    if not p5.strip():
        return ["P5 is empty"]
    extra = numbers(p5) - numbers(jd)
    if extra:
        out.append(f"number(s) about the company not in the ad: {sorted(extra)}")
    for sentence in sentences(p5):
        for clause in clauses(sentence):
            cs = stems(clause, skip)
            if not cs:
                continue
            if len(cs) < MIN_CLAIM_STEMS:
                ok = cs <= in_ad
            else:
                ok = max((len(cs & run) / len(cs) for run in runs), default=0.0) >= COMPANY_SUPPORT
            if not ok:
                out.append(f"claim about the company is not supported by the ad (not in it: {', '.join(sorted(cs - in_ad)[:6]) or 'no single passage holds it'}): "
                           f"'{clause.strip()[:70]}'")
    return list(dict.fromkeys(out))


def p5_sources(p5: str, jd: str, skip: frozenset[str] = frozenset()) -> list[dict]:
    """[{"claim": a sentence of P5, "ad": [the ad sentences that hold the most of its stems]}] for the review screen."""
    ad = ad_sentences(jd)
    each, _ = _windows(ad, skip)
    rows = []
    for sentence in sentences(p5):
        left = stems(sentence, skip)
        floor = 1 if len(left) < MIN_CLAIM_STEMS else 2
        picked: list[int] = []
        while left and len(picked) < 3 and each:
            best = max(range(len(ad)), key=lambda i: (len(left & each[i]), -i))
            if len(left & each[best]) < floor or best in picked:
                break
            picked.append(best)
            left -= each[best]
        rows.append({"claim": sentence.strip(), "ad": [ad[i] for i in sorted(picked)]})
    return rows


def used_sentences(p5: str, offered: list[str], skip: frozenset[str] = frozenset()) -> list[str]:
    """The offered ad sentences P5 quotes or closely follows, in the ad's order: a sentence or clause of P5 whose content stems are at least
    P5_FOLLOWS in one offered sentence, sharing at least P5_MIN_SHARED stems (all of them, for a sentence of fewer)."""
    pieces = [stems(x, skip) for sentence in sentences(p5) for x in (sentence, *clauses(sentence))]
    pieces = [c for c in pieces if c]
    used = []
    for ad in offered:
        have = stems(ad, skip)
        if not have:
            continue
        for c in pieces:
            shared = len(c & have)
            if shared >= min(P5_MIN_SHARED, len(c), len(have)) and shared / len(c) >= P5_FOLLOWS:
                used.append(ad)
                break
    return used


def extractive_problems(p5: str, offered: list[str], skip: frozenset[str] = frozenset()) -> list[str]:
    """P5 is written from the ad's About / role sentences: it must quote or closely follow at least P5_MIN_USED of those offered (fewer
    offered: all of them). Nothing is asked when the ad has none."""
    if not offered:
        return []
    need = min(P5_MIN_USED, len(offered))
    used = used_sentences(p5, offered, skip)
    if len(used) >= need:
        return []
    return [f"P5 follows {len(used)} of the ad's About / role sentences; it must quote or closely follow {P5_MIN_USED} of them "
            f"(for example: '{offered[0][:80]}')"]


def one_sentence_problems(p5: str) -> list[str]:
    """P5 is ONE sentence of at most P5_MAX_WORDS words."""
    n = len(sentences(p5))
    out = [f"P5 is {n} sentences; it must be one"] if n > 1 else []
    if len(p5.split()) > P5_MAX_WORDS:
        out.append(f"P5 is {len(p5.split())} words; it must be one sentence of at most {P5_MAX_WORDS}")
    return out


def name_problems(paragraphs: list[str], allowed_text: str, label: str = "") -> list[str]:
    """A capitalised name (a person, a team, a product, a place) that the ad, the fact bank and the opening do not give is invented."""
    allowed = norm_literal(allowed_text)
    out = []
    for sentence in sentences(" ".join(paragraphs)):
        for name in _names(sentence):
            if not name_allowed(allowed, name):
                out.append(f"'{name}' (a name) is not in the ad or the fact bank{label}: '{sentence.strip()[:60]}'")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the user guard (P3, P4)
def filler_problems(paragraphs: list[str], evidence: list[dict]) -> list[str]:
    """The filler words the prompt forbids (FILLER) in the letter's body, unless a chosen bullet itself says them."""
    said = norm_literal(" ".join(e["text"] for e in evidence))
    out = []
    for w in FILLER:
        pat = re.compile(r"(?<![\w-])" + re.escape(w.lower()).replace(r"\-", "-?") + r"\w*", re.I)
        if any(pat.search(p) for p in paragraphs) and not pat.search(said):
            out.append(f"vague filler '{w}' is not in the evidence")
    return out


def user_problems(paragraphs: list[str], groups: list[list[dict]], opening: str, fb: dict, kws: list[Keyword], reqs: list[list[str]],
                  company: str, title: str, jd: str = "", cv_names: str = "", assembled: frozenset[str] = frozenset()) -> list[str]:
    """Problems with P3 and P4, the two paragraphs about the user. `groups[i]` holds the bullets paragraph i was written from; `reqs[i]` the ad skills
    it must name (P3: two of its two or three; P4: none). `jd` (the ad and the company page) and `cv_names` (cv_from_ctx "names": this job's CV's Skills,
    stacks and bullets) are what name_problems allows too: the per-paragraph guard was called with no ad and no CV and rejected 'CI' (an ad keyword) and
    'stakeholder' (2 Nov 2026)."""
    out: list[str] = []
    evidence = [e for g in groups for e in g]
    bank = bank_text(fb)
    bullets = " ".join(f"{e['text']} {' '.join(e.get('terms', []))} {e.get('where', '')} {e.get('org', '')}" for e in evidence)
    pool = norm_literal(f"{bullets} {opening}")
    allowed_numbers = numbers(bullets) | numbers(opening)
    mine = {norm_literal(e.get("org") or L._owner(fb, e["id"])[0]) for e in evidence} | {norm_literal(e.get("where") or "") for e in evidence}
    for n, par in enumerate(paragraphs):
        tag = f"P{n + 3}"
        extra = numbers(par) - allowed_numbers
        if extra:
            out.append(f"{tag}: number(s) about you not in the chosen bullets: {sorted(extra)}")
        lit = norm_literal(par)
        for tool in L._tools(fb):
            if len(tool) >= 3 and re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", lit) and not re.search(
                    r"(?<![\w])" + re.escape(tool) + r"(?![\w])", pool):
                out.append(f"{tag}: names '{tool}', which is not in the chosen bullets")
        for key, label in _labels(fb).items():
            if re.search(r"(?<![\w])" + re.escape(key) + r"(?![\w])", lit) and key not in pool and not any(key in m for m in mine if m):
                out.append(f"{tag}: names '{label}', which is not where the chosen bullets were done")
        names = [kws[i].term for i in sorted(keyword_hits(par, kws))]
        if reqs[n]:
            need = min(2, len(reqs[n]))
            named = {r.lower() for r in reqs[n]} & {x.lower() for x in names}
            if len(named) < need:
                out.append(f"{tag}: does not name an ad requirement its bullet evidences (one of: {', '.join(reqs[n])})" if need == 1 else
                           f"{tag}: names {len(named)} of the ad skills its bullets evidence; it must name {need} (from: {', '.join(reqs[n])})")
        extra_names = f"{company} {title} {' '.join(reqs[n])} {cv_names} " + " ".join(f"{e.get('where', '')} {e.get('org', '')}" for e in groups[n])
        out += [f"{tag}: {p}" for p in grounding_problems(sentences(par), groups[n], jd, opening, extra_names, bank=bank)]
    out += filler_problems(paragraphs, evidence)
    # P3 and P4 are the two paragraphs written here; they must not tell one bullet twice, unless the plan itself gave both the same one
    # because the role had no other bullet (role_group): then no draft could avoid it, and rejecting every redraft is what lost the
    # RightShip letter (31 Oct 2026).
    shared = {e["id"] for e in groups[0]} & {e["id"] for e in groups[1]} if len(groups) > 1 else set()
    out += repeated_evidence_problems(paragraphs, evidence, ("P3", "P4"), shared, groups)
    # a sentence the assembled letter (letter.compose: assembled, v2.5) built word for word from a bullet is that bullet on purpose: only the other
    # sentences are checked for a pasted bullet; every other guard above read all of them
    own = [s for s in sentences(" ".join(paragraphs)) if " ".join(s.split()) not in assembled]
    out += L.paste_problems(own, L._all_bullets(fb))
    return list(dict.fromkeys(out))


def employer(e: dict) -> str:
    """The short employer name of an evidence bullet ("AstraZeneca" for "AstraZeneca, United Kingdom")."""
    return re.split(r"\s*,\s*", str(e.get("org") or ""))[0].strip()


def rollup_problems(p4: str, roles: list[dict]) -> list[str]:
    """P4 names every role's employer and gives one number of that role's bullet (when the bullet has one)."""
    out: list[str] = []
    lit = norm_literal(p4)
    for e in roles:
        name = employer(e)
        if name and not re.search(r"(?<![\w])" + re.escape(norm_literal(name)) + r"(?![\w])", lit):
            out.append(f"P4: does not name the employer {name}")
        nums = numbers(e["text"])
        if nums and not (numbers(p4) & nums):
            out.append(f"P4: gives no number from the {name or e['id']} bullet (one of: {', '.join(sorted(nums)[:4])})")
    return out


# ------------------------------------------------------------------ the whole letter
def citation_tags(parts: dict[str, str], ids: dict[str, list[str]]) -> list[dict]:
    """[{"para": "P1".."P4", "sentence", "ids"}] for every sentence of P1 to P4: the bullet ids it stands on (P1 and P2: the hook's proof bullet; P3 and
    P4: their evidence group). Internal: kept in the report, never printed on the letter."""
    out: list[dict] = []
    for name in CITED_NAMES:
        out += [{"para": name, "sentence": s.strip(), "ids": list(ids.get(name) or [])} for s in sentences(parts.get(name) or "")]
    return out


def balance_problems(tags: list[dict]) -> list[str]:
    """At least BODY_CITING of the sentences of P1 to P4 must cite a bullet or a project (Document rules, B3; P5 to P7 are not counted)."""
    total = len(tags)
    cited = sum(1 for t in tags if t.get("ids"))
    if total and cited / total < BODY_CITING - 1e-9:
        return [f"body: {cited} of {total} sentences cite a bullet or project ({cited / total:.0%}); at least {BODY_CITING:.0%} must"]
    return []


def skill_problems(body: list[str], fb: dict, cv_text: str, first: int = 3) -> list[str]:
    """A skill the fact bank names that the letter's body names too must be in this CV's Skills or bullets (`cv_text`)."""
    have = norm_literal(cv_text)
    out = []
    for n, par in enumerate(body):
        lit = norm_literal(par)
        for tool in L._tools(fb):
            if len(tool) >= 3 and re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", lit) and not re.search(
                    r"(?<![\w])" + re.escape(tool) + r"(?![\w])", have):
                out.append(f"P{n + first}: names '{tool}', which is not in this CV's Skills or bullets")
    return out


def cv_from_ctx(ctx: dict) -> dict:
    """What the guards need to know of a CV (its render context): {"ids": the bullet ids it prints, "text": its Skills, headline, profile and
    bullets as one string, "names": its Skills, stacks and bullets (no profile): a name the letter uses is allowed when this CV prints it (B3)}."""
    entries = [*(ctx.get("experience") or []), *(ctx.get("projects") or [])]
    ids = {i for e in entries for i in e.get("bullet_ids") or []} | {a.get("bullet_id") for a in ctx.get("achievements") or [] if a.get("bullet_id")}
    skills = " ".join(", ".join(v) if isinstance(v, (list, tuple)) else str(v) for v in (ctx.get("skills") or {}).values())
    stacks = " ".join(str(e.get("stack") or "") for e in entries)
    text = " ".join([skills, stacks, str(ctx.get("profile") or ""), *(b for e in entries for b in e.get("bullets") or []),
                     *(str(a.get("text") or "") for a in ctx.get("achievements") or [])])
    names = " ".join([skills, stacks, *(b for e in entries for b in e.get("bullets") or []), *(str(a.get("text") or "") for a in ctx.get("achievements") or [])])
    return {"ids": ids, "text": text, "names": names}


def p2_problems(p2: str, fb: dict) -> list[str]:
    """P2 is the fact bank's own text: every number in it is one the fact bank holds."""
    if not p2.strip():
        return []
    held = numbers(OP._bank_number_text(fb))
    extra = numbers(p2) - held
    return [f"P2: number(s) the fact bank does not hold: {sorted(extra)}"] if extra else []


def p6_problems(p6: str, gap: dict, fb: dict, kws: list[Keyword], claimable: list[str]) -> list[str]:
    """P6 is exactly one sentence of the form "One honest gap: ..."; the gap is a required ad keyword the fact bank cannot claim (not a visa, a degree
    or years), and what the writer has ("<X> only") is a claim the fact bank backs."""
    if not p6.strip():
        return []
    out: list[str] = []
    if len(sentences(p6)) != 1 or not p6.startswith("One honest gap:"):
        out.append("P6: must be exactly one sentence that starts 'One honest gap:'")
    term = str(gap.get("term") or "")
    if not term:
        return out + ["P6: there is no gap in the gap report to write about"]
    if _HARD_FILTER.search(term):
        out.append(f"P6: '{term}' is a hard filter (visa, degree or years), never written up as a gap")
    if term.lower() in {c.lower() for c in claimable}:
        out.append(f"P6: '{term}' is not a gap: the fact bank claims it")
    have = str(gap.get("have") or "")
    if have and not _has(bank_text(fb), norm_literal(have)):
        out.append(f"P6: '{have}' is not in the fact bank, so 'I have used {have} only' is not a claim it backs")
    return out


def check(parts: dict[str, str], job: dict, fb: dict, kws: list[Keyword], plan: "Plan", team: str, settings, cv: dict | None = None,
          claimable: list[str] | None = None, assembled: frozenset[str] = frozenset()) -> tuple[list[str], list[dict]]:
    """(problems, the ad sentences P5 drew from) for a letter given as {"P1": ..., ..., "P7": ...} (P2 and P6 may be missing). `cv`
    (cv_from_ctx) adds the CV rules: the evidence bullets and the skills named must be on this job's CV."""
    from . import tailor as T

    jd, company, title = job.get("description") or "", job["company"], job["title"]
    skip = frozenset(stems(f"{company} {title} {team}"))
    paragraphs, names = assemble(parts)
    out: list[str] = []
    missing = [n for n in ("P1", "P3", "P4", "P5", "P7") if n not in names]
    if missing:
        return [f"missing paragraph(s): {', '.join(missing)}"], []
    body = [parts["P3"], parts["P4"], parts["P5"]]
    n, (lo_words, _), cap = words(paragraphs), word_range(settings), word_cap(settings)
    if n < lo_words or n > cap:
        out.append(f"{n} words: the letter must be between {lo_words} and {cap}")
    page = plan.page_text
    ad_all = jd + ("\n" + page if page else "")
    out += [f"P5: {p}" for p in company_problems(parts["P5"], ad_all, skip | P5_FRAME)]
    out += one_sentence_problems(parts["P5"])
    out += extractive_problems(parts["P5"], about_sentences(ad_all, company, kws), skip | P5_FRAME)
    opening = " ".join([parts["P1"], parts.get("P2") or ""])
    out += user_problems(body[:2], plan.groups, opening, fb, kws, plan.reqs, company, title, jd=ad_all, cv_names=(cv or {}).get("names", ""),
                         assembled=assembled)
    out += rollup_problems(parts["P4"], plan.groups[1])
    allowed = " ".join([ad_all, company, title, team, opening, *bank_names(fb), *_labels(fb).values(),
                        *(f"{e['text']} {e.get('where', '')} {e.get('org', '')}" for e in plan.evidence), (cv or {}).get("names", "")])
    out += name_problems(body, allowed)
    out += L.claim_problems(body[2:], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", ad_all)))      # P5: no "we" / "our", no "I help <Company>"
    framing = " ".join([company, title, *(" ".join(e.get("terms", [])) for e in plan.evidence), *(t for r in plan.reqs for t in r)])
    out += L.claim_problems(body[:2], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", framing)))       # P3, P4: and no linked noun phrase
    out += term_dump_problems(body, fb)
    out += balance_problems(citation_tags(parts, plan.cite_ids()))
    out += OP.problems(parts["P1"], fb, [t for t in (jd, page) if t])
    out += p2_problems(parts.get("P2") or "", fb)
    if parts.get("P6"):
        out += p6_problems(parts["P6"], plan.gap, fb, kws, claimable if claimable is not None else [])
    out += hardware_problems(parts, ad_all)
    if cv is not None:
        out += [f"P3/P4 draw on bullet {e['id']}, which is not printed on this job's CV" for e in plan.evidence if e["id"] not in cv["ids"]]
        out += skill_problems(body, fb, cv["text"])
    from . import fluency as FL

    for name in names:                                                         # v3.4: no paragraph opens "I am excited", "I am writing", a greeting ...
        out += FL.opening_problems(str(parts.get(name) or ""), f"{name}: ")
    out += FL.grammar_problems({n: str(parts.get(n) or "") for n in names})
    text = " ".join(paragraphs)
    out += T._deny_problems(text, fb.get("deny_terms", []), [])
    out += T._style_problems([*body, parts["P7"]], [e["text"] for e in plan.evidence])
    if T.BANNED_CLOSERS.search(text):
        out.append("banned closer")
    return list(dict.fromkeys(out)), p5_sources(parts["P5"], ad_all, skip)


def allowed_vocabulary(evidence: list[dict], kws: list[Keyword], limit: int = 30) -> str:
    """The words P3 and P4 may name, for the prompt: the tools and methods of the chosen fact-bank bullets, and the ad's skill terms."""
    mine = list(dict.fromkeys(t for e in evidence for t in (evidence_fields.fields(e)["method"].split("; ")) if t and t != evidence_fields.NOT_STATED))
    ad = [k.term for k in sorted(kws, key=lambda k: -k.weight)][:limit]
    return (f"from the chosen bullets: {', '.join(mine) or '(none named)'}\n"
            f"the ad's skill terms: {', '.join(ad) or '(none)'}")


# ------------------------------------------------------------------ what each paragraph is built from
def skill_group(fb: dict, kws: list[Keyword], chosen_ids: set[str] | None, jd: str, skip: set[str]) -> tuple[list[str], list[dict]]:
    """(the ad's 2 or 3 heaviest skills the group evidences, the group): P3 is written from the one CV bullet (a project's, on a tie) that carries
    most of the ad's keyword weight, plus a second when the first carries fewer than two; never a bullet of `skip` (the hook's own)."""
    from . import tailor as T

    pool = {b for b in chosen_ids if b not in skip} if chosen_ids else None
    ranked = [] if pool is not None and not pool else [e for e in T.ranked_letter_bullets(fb, kws, pool, jd) if e["id"] not in skip]
    if not ranked:
        return [], []
    carries = {e["id"]: keyword_hits(" ".join([e["text"], *e.get("terms", [])]), kws) for e in ranked}
    project_ids = {b["id"] for p in fb.get("projects", []) for b in p["bullets"]}
    rank = {e["id"]: i for i, e in enumerate(ranked)}

    def gain(e: dict, have: set[int]) -> float:
        return sum(kws[i].weight for i in carries[e["id"]] - have)

    first = max(ranked, key=lambda e: (gain(e, set()) + (0.5 if e["id"] in project_ids else 0.0), -rank[e["id"]]))
    group, have = [first], set(carries[first["id"]])
    if len(have) < 2:
        more = [e for e in ranked if e is not first and gain(e, have) > 0]
        if more:
            second = max(more, key=lambda e: (gain(e, have), -rank[e["id"]]))
            group.append(second)
            have |= carries[second["id"]]
    skills = [kws[i].term for i in sorted(have, key=lambda i: (-kws[i].weight, i))][:3]
    return skills, group


def role_group(fb: dict, kws: list[Keyword], chosen_ids: set[str] | None, jd: str, hk: Hook | None, avoid: set[str]) -> list[dict]:
    """P4's evidence: one bullet of each of the two most relevant roles of the CV (the hook's most relevant role first, then the role whose best
    bullet ranks next), a numbered one that is not in `avoid` when the role has one."""
    from . import tailor as T

    ranked = T.ranked_letter_bullets(fb, kws, set(chosen_ids) if chosen_ids else None, jd)
    entry_of = {b["id"]: e["id"] for e in fb.get("experience", []) for b in e["bullets"]}
    exp = [e for e in ranked if e["id"] in entry_of]
    order: list[str] = []
    for e in exp:
        if entry_of[e["id"]] not in order:
            order.append(entry_of[e["id"]])
    first = getattr(hk, "relevant_role_id", "") if hk is not None else ""
    if first in order:
        order = [first, *(x for x in order if x != first)]

    def has_number(e: dict) -> bool:
        return bool(numbers(e["text"]) or OP.bank_numbers(e["text"], fb))

    # P4 gives one number of each role's bullet (rollup_problems): the roles that have a numbered bullet come first, the hook's role still ahead of the others
    with_number = [x for x in order if any(has_number(e) for e in exp if entry_of[e["id"]] == x)]
    order = with_number + [x for x in order if x not in with_number]
    out: list[dict] = []
    for eid in order[:2]:
        mine = [e for e in exp if entry_of[e["id"]] == eid]
        numbered = [e for e in mine if has_number(e)]
        # a numbered bullet P3 or the hook has not used; else a numbered one they have (a number in P4 matters more than a fresh bullet: the
        # "same evidence" rule is gone); only a role with no numbered bullet gets an unnumbered one (the employer is named, no number is asked)
        pick = next((e for e in numbered if e["id"] not in avoid), None) or (numbered[0] if numbered else None) or \
            next((e for e in mine if e["id"] not in avoid), None) or mine[0]
        out.append(pick)
    return out


def result_paragraph(fb: dict, op: "OP.Opening") -> tuple[str, str]:
    """(P2, where it came from): the hook fact's `limits`, else its `result`, word for word. The hook entry is read first, then its proof bullet.
    ("", "") when the fact bank has neither: P2 is then left out."""
    def clean(v) -> str:
        return " ".join(str(v or "").split())

    entry = next((e for e in hook_mod.library(fb) if str(e.get("id")) == op.hook_id), None) if op.hook_id else None
    holders: list[tuple[str, dict]] = [(f"hook {op.hook_id}", entry)] if entry else []
    bid = str((entry or {}).get("proof") or op.bullet_id or "")
    found = hook_mod.find_bullet(fb, bid) if bid else None
    if found:
        holders.append((f"bullet {bid}", found[2]))
    for where, h in holders:
        text = clean(h.get("limits"))
        if text:
            return (text if text[-1] in ".!?" else text + "."), f"{where} limits"
    for where, h in holders:
        text = clean(h.get("result"))
        if text:
            return f"The result, as measured: {re.sub(r'\s*→\s*', ' to ', text).rstrip('.')}.", f"{where} result"
    return "", ""


def gap_paragraph(job: dict, kws: list[Keyword], fb: dict, claimable: list[str], possible: dict | None) -> dict:
    """The honest-gap candidate: {"term", "have", "paragraph"} ({} when the gap report holds no gap that may be written up). The gap is the heaviest
    REQUIRED ad keyword the fact bank cannot claim, that is a tool, platform, language or framework (never a visa, degree or years, never a term the
    deny list names); `have` is a neighbour skill of the fact bank (evidence family) when there is one."""
    deny = fb.get("deny_terms", [])
    nameable = gap_terms()
    jd = job.get("description") or ""
    bank = bank_text(fb)
    for row in L.learn_list(kws, claimable, deny):
        term = row["term"]
        if not row["required"] or row["confirmed_absent"] or _HARD_FILTER.search(term) or norm_literal(term) not in nameable or _has(bank, norm_literal(term)):
            continue
        gap = gap_display(term, jd)
        have = ""
        for key, via in (possible or {}).items():
            if key.lower() == gap.lower():
                have = next((str(v["via"]) for v in via or [] if v.get("where") and not deny_hits(str(v["via"]), deny)), "")
                break
        text = (f"One honest gap: I have used {have} only, never {gap}, so {gap} is where I will learn." if have
                else f"One honest gap: I have never used {gap}, so it is where I will learn.")
        return {"term": term, "have": have, "paragraph": text}
    return {}


def assessment_in(jd: str) -> str:
    """The assessment the ad names ("technical assessment", "case study"), lower case; '' when it names none."""
    m = _ASSESSMENT.search(jd or "")
    return m.group(0).lower() if m else ""


def ask_text(job: dict, problem: str, settings) -> str:
    """P7: `letter.ask` with the ad's problem phrase, the role and (when the ad names one) the assessment. Never written by a model."""
    jd = job.get("description") or ""
    role = re.split(r"\s*,\s*", re.sub(r"\s*[\(\[].*?[\)\]]", "", str(job.get("title") or "")).split(" - ")[0])[0].strip() or "role"
    assess = assessment_in(jd)
    tail = (settings["letter"].get("ask_assessment") or ASK_ASSESSMENT).format(assessment=assess) if assess else ""
    problem = problem.strip().rstrip(".?")
    generic = not problem or (problem.lower().startswith("the ") and problem.lower().endswith(" role"))
    template = (settings["letter"].get("ask_plain") or ASK_PLAIN) if generic else (settings["letter"].get("ask") or ASK)
    return template.format(problem=problem, role=role, assessment=tail)


def _letter_dict(status: str, **kw) -> dict:
    base = {"paragraphs": [], "names": [], "parts": {}, "problems": [], "score": None, "pages": 0, "words": 0, "status": status, "mode": MODE, "notes": [],
            "retry": {}, "evidence": [], "p5_sources": [], "p5_used": [], "requirements": [], "gap": {}, "dropped": []}
    return {**base, **kw}


@dataclass
class Plan:
    """What the letter is built from, settled in code before any model is asked: P1, P2, P6 and P7 as text, the bullets P3 and P4 are written from
    (with where they were done) and the ad skills P3 names. `error` (when set) says why there is nothing to build a letter from."""
    p1: str = ""
    p2: str = ""
    p7: str = ""
    gap: dict = field(default_factory=dict)                 # the honest-gap candidate {"term", "have", "paragraph"}, kept or not
    evidence: list[dict] = field(default_factory=list)      # the bullets of P3 then P4, flat (the guards read this)
    reqs: list[list[str]] = field(default_factory=list)     # per model paragraph (P3, P4), the ad skills it names (P4: none)
    groups: list[list[dict]] = field(default_factory=list)  # per model paragraph, its bullets (P3: one or two, P4: one per role)
    error: str = ""
    opening: OP.Opening | None = None                       # how P1 was made: its fact, its quoted problem phrase and where the phrase came from
    page_text: str = ""                                     # the company's own careers page, when the ad had no phrase to quote
    p2_source: str = ""

    def cite_ids(self) -> dict[str, list[str]]:
        """The bullet ids each of P1 to P4 stands on."""
        hook_ids = [self.opening.bullet_id] if self.opening is not None and self.opening.bullet_id else []
        return {"P1": hook_ids, "P2": hook_ids, "P3": [e["id"] for e in (self.groups[0] if self.groups else [])],
                "P4": [e["id"] for e in (self.groups[1] if len(self.groups) > 1 else [])]}


def prepare(job: dict, fb: dict, kws: list[Keyword], hk: Hook | None, family_line: str, evidence: list[dict], settings, claimable: list[str],
            chosen_ids: set[str] | None = None, page_text: str = "", possible: dict | None = None) -> Plan:
    """P1 (opening.py: the ad's or the company page's problem phrase, a semicolon, the hook's letter line with a fact-bank number; else the next
    hook's, else the strongest numbered bullet of the CV and the role family's profile line), P2 (the hook fact's limits / result), P6 (the honest
    gap), P7 (the ask), and the bullets and skills P3 and P4 are written from. `evidence` is used when the CV has too few bullets."""
    from . import tailor as T

    jd = job.get("description") or ""
    op = OP.build(job, fb, kws, hk, chosen_ids, family_line, page_text)
    if not op.ok:
        return Plan(error=op.error, opening=op, page_text=page_text)
    skip = {x for x in (op.bullet_id, getattr(hk, "proof_bullet_id", "") if hk is not None else "") if x}
    skills, g3 = skill_group(fb, kws, chosen_ids, jd, skip)
    if not g3 or not skills:
        have = [e for e in evidence]
        return Plan(evidence=T._with_orgs(fb, have), opening=op, page_text=page_text,
                    error="no skill of the ad is evidenced by a bullet on the CV (other than the hook's own) to write P3 from")
    g4 = role_group(fb, kws, chosen_ids, jd, hk, {e["id"] for e in g3} | skip)
    if not g4:
        return Plan(evidence=T._with_orgs(fb, g3), opening=op, page_text=page_text, error="the CV has no work-experience bullet to write the P4 roll-up from")
    groups = [T._with_orgs(fb, g3), T._with_orgs(fb, g4)]
    problem = hk.problem.strip() if hk is not None and hk.problem.strip() else L.ad_problem(job, kws, fb, claimable)
    p2, p2_source = result_paragraph(fb, op)
    same = numbers(p2) & numbers(op.paragraph) if p2 else set()
    if same:                                   # "The result, as measured: ~65 tests ..." right after P1's "~65 tests" (RightShip, 3 Nov 2026)
        p2, p2_source = "", f"left out: repeats P1's number ({', '.join(sorted(same))})"
    return Plan(op.paragraph, p2, ask_text(job, problem, settings), gap_paragraph(job, kws, fb, claimable, possible), [e for g in groups for e in g],
                [skills, []], groups, opening=op, page_text=page_text, p2_source=p2_source)


def feedback_flags(text: str) -> dict:
    """What the reader's feedback decides in code (Document rules, B7): "drop P2" / "drop P6" leave that optional paragraph out, "keep P6" puts the
    honest gap in. Everything else ("shorten P4") goes to the model by name."""
    drop = {f"P{m.group(1)}" for m in re.finditer(r"\b(?:drop|remove|delete|cut|omit|without|no)\s+(?:the\s+)?P([26])\b", text or "", re.I)}
    keep = {f"P{m.group(1)}" for m in re.finditer(r"\b(?:keep|add|include|put back)\s+(?:the\s+)?P([26])\b", text or "", re.I)}
    return {"drop": drop - keep, "keep": keep}


def included(plan: Plan, settings, dropped: set[str], kept_gap: bool | None = None) -> dict[str, str]:
    """P1, P2, P6 and P7 as they stand in the letter: P2 unless dropped; P6 when the gap exists and (letter.honest_gap, or the person kept it) and
    the person did not drop it."""
    parts = {"P1": plan.p1, "P7": plan.p7}
    if plan.p2 and "P2" not in dropped:
        parts["P2"] = plan.p2
    on = honest_gap_on(settings) if kept_gap is None else kept_gap
    if plan.gap and on and "P6" not in dropped:
        parts["P6"] = plan.gap["paragraph"]
    return parts


def write_letter(job: dict, kws: list[Keyword], parts: dict[str, str], team: str, fb: dict, settings, out_dir: Path, final_name: str,
                 header: dict | None = None, gap: dict | None = None, **extra) -> tuple[Path, dict]:
    """Render the letter and return (pdf, the "ok" letter dict); `letter.txt` (the same words in plain text) is written beside the PDF. `header` =
    {"headline", "number"}: the CV's hook headline, printed under the name as the CV prints it (Document rules, B2). `extra` are more keys of the
    dict (notes, retry, evidence, ...)."""
    from .render import page_count, pdf_text, render_letter

    skip = frozenset(stems(f"{job['company']} {job['title']} {team}"))
    jd = job.get("description") or ""
    paragraphs, names = assemble(parts)
    head = {"headline": (header or {}).get("headline") or "", "headline_number": (header or {}).get("number") or ""}
    greeting, signoff = L.greeting(job, team, settings), settings["letter"]["signoff"]
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paragraphs, "date": date.today().strftime("%d %B %Y"),
                         "greeting": greeting, "signoff": signoff, **head}, out_dir, final_name)
    write_text(out_dir, greeting, paragraphs, signoff, fb["identity"]["name"])
    p5 = parts.get("P5") or ""
    return pdf, _letter_dict("ok", paragraphs=paragraphs, names=names, parts=dict(parts), words=words(paragraphs), score=ats.score(pdf_text(pdf), kws).literal,
                             pages=page_count(pdf), p5_sources=p5_sources(p5, jd, skip), p5_used=used_sentences(p5, about_sentences(jd, job["company"], kws), skip),
                             accepted=[word_range(settings)[0], word_cap(settings)], gap=dict(gap or {}), **extra)


TEXT_NAME = "letter.txt"


def write_text(out_dir: Path, greeting: str, paragraphs: list[str], signoff: str, name: str) -> Path:
    """letter.txt beside the PDF: the greeting, the paragraphs, the sign-off and the name as plain text, for a form's cover-letter text box."""
    path = Path(out_dir) / TEXT_NAME
    path.write_text("\n\n".join([f"{greeting},", *paragraphs, f"{signoff},", name]) + "\n", encoding="utf-8", newline="\n")
    return path


def rebuild_plan(job: dict, fb: dict, rep: dict, settings) -> tuple[Plan, list[Keyword]]:
    """The plan (opening, result, ask, evidence groups, requirements) of an already tailored job, rebuilt the way review's `l` rebuilds it."""
    from . import tailor as T

    kws = [Keyword(**k) for k in rep["keywords"]]
    ids = {i for sec in ("experience", "projects") for e in rep["context"][sec] for i in e.get("bullet_ids", [])}
    hk = Hook.from_dict(rep["hook"]) if rep.get("hook") else None
    evidence = T.letter_evidence(fb, kws, ids, job.get("description") or "")
    page = CP.stored(Path(rep["out_dir"])) if rep.get("out_dir") else ""
    return prepare(job, fb, kws, hk, rep.get("family_line") or "", evidence, settings, rep.get("claimable") or [], ids, page,
                   rep.get("possible_evidence")), kws


def edit_problems(job: dict, fb: dict, rep: dict, settings, parts: dict[str, str]) -> list[str]:
    """The company-letter guards on a letter the person edited by hand ({"P1": ..., ...}). [] = it passes every guard."""
    unknown = [n for n in parts if n not in NAMES]
    if unknown:
        return [f"unknown paragraph name(s): {', '.join(unknown)} (use P1 to P7)"]
    plan, kws = rebuild_plan(job, fb, rep, settings)
    if plan.error:
        return [f"the guards cannot be run on this job: {plan.error}"]
    problems, _ = check(parts, job, fb, kws, plan, rep.get("team") or "", settings, cv=cv_from_ctx(rep["context"]), claimable=rep.get("claimable") or [])
    return problems


def install_edited(job: dict, fb: dict, rep: dict, settings, parts: dict[str, str], overridden: list[str]) -> tuple[Path, dict]:
    """Render an edited letter under the job's usual letter file name. `overridden` = the guard problems the person accepted with `use anyway`."""
    plan, kws = rebuild_plan(job, fb, rep, settings)
    stem = Path(rep["letter_pdf"]).stem if rep.get("letter_pdf") else None
    if stem is None:
        from . import tailor as T

        stem = T._names(fb, settings)[1]
    old = rep.get("letter") or {}
    ctx = rep["context"]
    header = {"headline": ctx.get("hook_headline") or (ctx.get("id") or {}).get("headline") or "", "number": ctx.get("hook_number") or ""}
    gap = {**plan.gap, "included": "P6" in parts} if plan.gap else {}
    return write_letter(job, kws, parts, rep.get("team") or "", fb, settings, Path(rep["out_dir"]), stem, header, gap,
                        notes=list(old.get("notes") or []) + ["letter edited by hand" + (" (guards overridden with 'use anyway')" if overridden else "")],
                        retry=old.get("retry") or {}, evidence=[{"id": e["id"], "text": e["text"]} for e in plan.evidence], requirements=plan.reqs,
                        target_words=target_words(settings), hook_source=old.get("hook_source", "edited by hand"),
                        cites=citation_tags(parts, plan.cite_ids()), dropped=[n for n in OPTIONAL if n not in parts],
                        edited=True, overridden=list(overridden))


def set_gap(job: dict, fb: dict, rep: dict, settings, keep: bool) -> tuple[Path | None, dict, list[str]]:
    """Review's [k]eep / [d]rop of P6: re-render the letter with the honest-gap paragraph in or out (no model is asked). Returns (pdf, letter,
    problems); with problems the letter is unchanged (the words must stay inside letter.company_words)."""
    old = rep.get("letter") or {}
    gap = dict(old.get("gap") or {})
    if old.get("status") != "ok" or not gap.get("paragraph"):
        return None, old, ["this letter has no honest-gap paragraph to keep or drop"]
    parts = dict(old.get("parts") or {})
    if keep:
        parts["P6"] = gap["paragraph"]
    else:
        parts.pop("P6", None)
    n, (lo, _), cap = words(assemble(parts)[0]), word_range(settings), word_cap(settings)
    if not lo <= n <= cap:
        return None, old, [f"{n} words: the letter must be between {lo} and {cap}"]
    kws = [Keyword(**k) for k in rep["keywords"]]
    ctx = rep["context"]
    header = {"headline": ctx.get("hook_headline") or (ctx.get("id") or {}).get("headline") or "", "number": ctx.get("hook_number") or ""}
    stem = Path(rep["letter_pdf"]).stem
    skip_keys = {"paragraphs", "names", "parts", "words", "score", "pages", "p5_sources", "p5_used", "accepted", "gap", "status", "mode", "problems", "dropped"}
    extra = {k: v for k, v in old.items() if k not in skip_keys}
    dropped = [n for n in old.get("dropped") or [] if n != "P6"] + ([] if keep else ["P6"])
    pdf, letter = write_letter(job, kws, parts, rep.get("team") or "", fb, settings, Path(rep["out_dir"]), stem, header, {**gap, "included": keep}, **extra)
    letter["dropped"] = dropped
    return pdf, letter, []


def make(job: dict, fb: dict, kws: list[Keyword], hk: Hook | None, family_line: str, evidence: list[dict], llm, settings, out_dir: Path,
         final_name: str, team: str, claimable: list[str], chosen_ids: set[str] | None = None,
         revise: dict | None = None, cv: dict | None = None, page=None, header: dict | None = None,
         possible: dict | None = None) -> tuple[Path | None, dict]:
    """(letter pdf or None, the letter dict `report["letter"]` shows). Status "ok", "none" (every draft rejected, or nothing to build it from;
    `reason` and `retry.failures` say why) or "pending" (no provider may write it now: the job waits). `hk` is the hook (None: none qualifies or
    the hook is off), `family_line` the role family's profile line, `evidence` the letter's two best bullets of the CV. `revise`
    ({"previous": [P3, P4, P5], "problems": [...], "feedback": text, "dropped": [...], "gap_kept": bool|None}) is review's `l`: the draft, its
    rejections and the reader's words go to the model. `cv` (cv_from_ctx of the CV) adds the CV rules to the guards; `page(job, folder)` fetches the
    company's careers page (once, only when the ad has no clause to quote); `header` = the CV's headline for the letter's header."""
    from . import tailor as T

    jd = job.get("description") or ""
    page_text = CP.stored(out_dir)
    plan = prepare(job, fb, kws, hk, family_line, evidence, settings, claimable, chosen_ids, page_text, possible)
    if plan.error and "no problem phrase" in plan.error and page is not None and not page_text:
        page_text = page(job, out_dir) or ""
        if page_text:
            plan = prepare(job, fb, kws, hk, family_line, evidence, settings, claimable, chosen_ids, page_text, possible)
    lo_words = word_range(settings)[0]
    shown = [{"id": e["id"], "text": e["text"]} for e in plan.evidence]
    notes: list[str] = []
    said = str((revise or {}).get("feedback") or "")
    flags = feedback_flags(said)
    dropped = (set((revise or {}).get("dropped") or []) | flags["drop"]) - flags["keep"]
    gap_kept = True if "P6" in flags["keep"] else (revise or {}).get("gap_kept")
    code_parts = included(plan, settings, dropped, gap_kept)

    def none(reason: str, problems: list[str] | None = None, retry: dict | None = None, draft: list[str] | None = None) -> tuple[None, dict]:
        return None, _letter_dict("none", reason=reason, problems=problems or [], notes=notes, retry=retry or {}, evidence=shown, opening=plan.p1,
                                  closing=plan.p7, draft_body=list(draft or []), gap=({**plan.gap, "included": "P6" in code_parts} if plan.gap else {}),
                                  dropped=sorted(dropped))

    if plan.error:
        return none(plan.error)
    from . import assembled_letter as AL

    if AL.compose_mode(settings) == "assembled":
        try:
            return _make_assembled(job, fb, kws, hk, plan, llm, settings, out_dir, final_name, team, claimable, chosen_ids, cv, header, code_parts,
                                   page_text, said, dropped, notes, shown, none)
        except TaskUnavailable as e:
            return None, _letter_dict("pending", reason=str(e)[:300], notes=notes, evidence=shown)
    target, cap = target_words(settings), word_cap(settings)
    body_words = max(target - words(list(code_parts.values())), 60)
    p5_words = min(max(round(body_words * 0.2), 18), 32)
    p3_words = (body_words - p5_words) // 2
    p4_words = body_words - p5_words - p3_words
    deny = fb.get("deny_terms", [])
    ad_all = jd + ("\n" + page_text if page_text else "")
    offered = about_sentences(ad_all, job["company"], kws)
    system = (P.COMPANY_LETTER_SYSTEM.replace("{body_words}", str(body_words)).replace("{lo}", str(lo_words)).replace("{hi}", str(cap))
              .replace("{p3_words}", str(p3_words)).replace("{p4_words}", str(p4_words)).replace("{p5_words}", str(p5_words))
              .replace("{deny}", ", ".join(deny) or "(none)").replace("{filler}", ", ".join(FILLER)))
    user = P.COMPANY_LETTER_USER.format(
        title=job["title"], company=job["company"], table=table(code_parts), ad=ad_all[:9000], evidence=evidence_prompt(plan.groups, plan.reqs),
        requirements="P3: name at least two of: " + ", ".join(plan.reqs[0]) + "\nP4: name each role's employer: " + ", ".join(employer(e) for e in plan.groups[1]),
        about="\n".join(f"[{i}] {x}" for i, x in enumerate(offered, 1)) or "(the ad has no About / role section: use its opening lines)",
        vocabulary=allowed_vocabulary(plan.evidence, kws), filler=", ".join(FILLER))
    ctx = {"mode": MODE, "company": job["company"], "title": job["title"], "opening": plan.p1, "closing": plan.p7, "parts": dict(code_parts), "ad": jd,
           "ad_sentences": ad_sentences(jd), "about_sentences": offered, "filler": list(FILLER), "evidence": [e["text"] for e in plan.evidence],
           "groups": [[e["id"] for e in g] for g in plan.groups], "requirements": plan.reqs, "roles": [employer(e) for e in plan.groups[1]],
           "body_words": body_words, "min_words": lo_words, "max_words": cap}
    retry: dict = {"count": 0, "rejected_words": [], "first_problems": [], "failures": [], "removed": [], "outcome": "first draft", "temperatures": []}
    rejections: list[list[str]] = []                                  # every draft's rejections, in the writer's next prompt word for word
    last_body: list[str] = []
    previous = list((revise or {}).get("previous") or [])
    feedback = (LF.revision_block(previous, LF.instructions(list((revise or {}).get("problems") or []), previous, plan), said) if revise else "")
    problems, body = [], []
    earlier = RU.earlier_letters(settings, exclude=[out_dir.name])
    reuse = {"score": 0.0, "against": "", "redrafted": False}
    reused = 0                                                        # drafts rejected for copying an earlier letter: one redraft, then no letter
    for attempt in range(ATTEMPTS):
        try:
            temperature = TEMPERATURES[min(attempt, len(TEMPERATURES) - 1)]
            retry["temperatures"].append(temperature)
            out = T._llm_json(llm, "cover_letter", system, user + feedback, {**ctx, "temperature": temperature})
            body, letter_notes = T.normalise_letter(out)
            notes.extend(m for m in (f"cover_letter: {n}" for n in letter_notes) if m not in notes)
        except TaskUnavailable as e:
            return None, _letter_dict("pending", reason=str(e)[:300], notes=notes, evidence=shown)
        except Exception as e:  # noqa: BLE001
            problems = [f"letter LLM failed: {e}"]
            retry["failures"].append({"attempt": attempt + 1, "problems": problems})
            break
        if len(body) != 3:
            problems = [f"expected 3 paragraphs (P3, P4, P5), got {len(body)}"]
            sources: list[dict] = []
        else:
            parts = {**code_parts, "P3": body[0], "P4": body[1], "P5": body[2]}
            problems, sources = check(parts, job, fb, kws, plan, team, settings, cv=cv, claimable=claimable)
            if not problems:
                reuse_body = [parts[n] for n in REUSE_NAMES if parts.get(n)]
                score, who = RU.worst(reuse_body, earlier)
                if who is not None and score >= RU.LIMIT:
                    reused += 1
                    problems = [RU.problem(score, who, RU.shared_sentences(reuse_body, who))]
                else:
                    reuse = {"score": round(score, 3), "against": who.name if who else "", "redrafted": bool(reused)}
        if not problems:
            retry["outcome"] = "ok after redraft" if attempt else "first draft"
            break
        retry["failures"].append({"attempt": attempt + 1, "problems": list(problems)})
        if attempt == 0:
            retry["first_problems"] = list(problems)
        rejections.append(list(problems))
        if reused >= 2:
            break                                                     # a second copy of an earlier letter: no more tries
        if len(body) == 3 and len(last_body) == 3:
            same = RU.jaccard(RU.shingles(" ".join(body)), RU.shingles(" ".join(last_body)))
            if same >= SAME_DRAFT:
                retry["stopped_early"] = f"draft {attempt + 1} is {same:.0%} the same as draft {attempt} (5-word shingles)"
                notes.append(f"cover_letter: {retry['stopped_early']}; no more tries")
                break
        last_body = list(body)
        if attempt + 1 < ATTEMPTS:
            retry["count"] = attempt + 1
            notes.append(f"cover_letter: draft {attempt + 1} rejected ({'; '.join(problems)[:200]}); redrafting ({attempt + 2} of {ATTEMPTS})")
        feedback = LF.revision_block(body, LF.instructions(problems, body, plan), said, body_words, history=rejections)
    if problems:
        retry["outcome"] = "no letter"
        shown_problems = "; ".join(problems)[:300]
        return none(f"{len(retry['failures'])} draft(s) rejected by the {MODE} guards; last: {shown_problems}", problems, retry, body)
    retry["feedback"] = said
    op = plan.opening
    parts = {**code_parts, "P3": body[0], "P4": body[1], "P5": body[2]}
    gap = {**plan.gap, "included": "P6" in parts} if plan.gap else {}
    return write_letter(job, kws, parts, team, fb, settings, out_dir, final_name, header, gap, notes=notes, retry=retry, evidence=shown,
                        requirements=plan.reqs, target_words=target, hook_source=(hk.source if hk is not None else "family profile line"),
                        reuse=reuse, cites=citation_tags(parts, plan.cite_ids()), dropped=sorted(dropped), p2_source=plan.p2_source,
                        opening={"fact": op.fact_kind, "hook_id": op.hook_id, "bullet_id": op.bullet_id, "number": op.number,
                                 "problem": op.problem, "source": op.source} if op is not None else {})


def _make_assembled(job: dict, fb: dict, kws: list[Keyword], hk: Hook | None, plan: Plan, llm, settings, out_dir: Path, final_name: str, team: str,
                    claimable: list[str], chosen_ids: set[str] | None, cv: dict | None, header: dict | None, code_parts: dict[str, str], page_text: str,
                    said: str, dropped: set[str], notes: list[str], shown: list[dict], none) -> tuple[Path | None, dict]:
    """letter.compose: assembled (tailoring/assembled_letter.py): P3 and P4 from this CV's bullets by a fixed rule, P5 from the model (or the
    fallback), the whole letter through `check`. A variant is rebuilt from the next bullets when the last one repeats an earlier letter or one of its
    assembled sentences fails a guard (a fact-bank problem: the model is not asked again)."""
    import dataclasses

    from . import assembled_letter as AL
    from . import fluency as FL
    from . import plain as PL

    plain_rule = PL.style(settings) == "plain"                                   # the jargon rule (v3.4): JD keywords stay, other jargon goes
    plain_allow = list(dict.fromkeys([*PL.allow_list(settings), *(k.term for k in kws)]))

    jd = job.get("description") or ""
    ad_all = jd + ("\n" + page_text if page_text else "")
    offered = about_sentences(ad_all, job["company"], kws)
    st = AL.steer(said)
    p5, p5info = AL.write_p5(llm, job, fb, kws, plan, team, ad_all, offered, said, notes)
    earlier = RU.earlier_letters(settings, exclude=[out_dir.name])
    retry: dict = {"mode": "assembled", "count": 0, "failures": [], "outcome": "", "temperatures": [], "p5": p5info, "tokens": p5info["tokens"],
                   "variants": []}
    problems: list[str] = []
    last_body: list[str] = []
    for variant in range(AL.VARIANTS):
        g3, g4, reqs = AL.groups_for(plan, fb, kws, chosen_ids, jd, hk, variant, st)
        g3, g4, moved = AL.split_shared(g3, g4)                                  # a bullet in P3 and P4 is told once (v3.4)
        cparts = dict(code_parts)
        b, assembled, bnotes, g3 = AL.body(plan, g3, g4, reqs, fb, chosen_ids, st, settings, cparts, p5, moved, kws)
        g4 = b.pop("_g4", g4)                                                    # type: ignore[assignment]  (the roles P4 kept: each with a number)
        if b.pop("drop_p2", None):
            cparts.pop("P2", None)
        vplan = dataclasses.replace(plan, groups=[g3, g4], reqs=[reqs, []], evidence=[*g3, *g4])
        parts = {**cparts, "P3": b["P3"], "P4": b["P4"], "P5": p5}
        problems, _ = check(parts, job, fb, kws, vplan, team, settings, cv=cv, claimable=claimable, assembled=frozenset(assembled))
        if any(p.startswith("P5") or "(a name)" in p and p5[:20] in p for p in problems) and p5info["source"] == "model":
            p5 = AL.p5_fallback(offered, job["company"], lambda t: AL.p5_problems(t, job, fb, kws, plan, team, ad_all, offered))   # once
            p5info["source"] = "fallback (after the whole-letter check)"
            parts["P5"] = p5
            problems, _ = check(parts, job, fb, kws, vplan, team, settings, cv=cv, claimable=claimable, assembled=frozenset(assembled))
        last_body = [parts["P3"], parts["P4"], parts["P5"]]
        retry["variants"].append({"variant": variant, "p3": [e["id"] for e in g3], "p4": [e["id"] for e in g4], "problems": list(problems)})
        notes.extend(n for n in bnotes if n not in notes)
        if problems:
            ids = ", ".join(e["id"] for e in [*g3, *g4])
            notes.append(f"cover_letter: assembled variant {variant + 1} (bullets {ids}) failed a guard: a fact-bank problem, the model is not asked "
                         f"again ({'; '.join(problems)[:200]})")
            retry["failures"].append({"attempt": variant + 1, "problems": list(problems), "bullets": [e["id"] for e in [*g3, *g4]]})
            continue
        reuse_body = [parts[n] for n in REUSE_NAMES if parts.get(n)]
        score, who = RU.worst(reuse_body, earlier)
        if who is not None and score >= RU.LIMIT:
            problems = [RU.problem(score, who, RU.shared_sentences(reuse_body, who))]
            retry["failures"].append({"attempt": variant + 1, "problems": list(problems)})
            notes.append(f"cover_letter: assembled variant {variant + 1} repeats {who.name} ({score:.2f}); rebuilt from the next bullets")
            continue
        retry["outcome"] = "assembled" + (f" (variant {variant + 1})" if variant else "")
        retry["feedback"] = said
        draft = dict(parts)                                                      # the fact source of the fluency pass, and its fallback
        fluency: dict = {"source": "off", "tries": 0, "problems": [], "tokens": 0}
        if FL.enabled(settings):
            def judge(new: dict, vplan=vplan, assembled=assembled) -> list[str]:
                # (e) the guards the draft passed; a rewritten P3 / P4 sentence restates a verified bullet on purpose, so only the paste check skips it
                own = frozenset(" ".join(s.split()) for n in ("P3", "P4") for s in sentences(new.get(n) or ""))
                probs, _ = check(new, job, fb, kws, vplan, team, settings, cv=cv, claimable=claimable, assembled=frozenset(assembled) | own)
                if plain_rule:                                                   # cv.style plain (v3.4): technical names only from the allow-list
                    probs += [f"{n}: jargon not on the allow-list: {', '.join(js)}" for n in FL.REWRITTEN
                              if (js := PL.jargon(re.sub(r'"[^"]*"', " ", new.get(n) or ""), fb, None, plain_allow))]
                if probs:
                    return probs
                rb = [new[n] for n in REUSE_NAMES if new.get(n)]
                s2, w2 = RU.worst(rb, earlier)
                return [RU.problem(s2, w2, RU.shared_sentences(rb, w2))] if w2 is not None and s2 >= RU.LIMIT else []
            parts, fluency = FL.polish_letter(llm, draft, job, ad_all, [*L._tools(fb), *(k.term for k in kws)], judge, skills=reqs,
                                              employers=[employer(e) for e in g4], filler=FILLER, notes=notes,
                                              words=(word_range(settings)[0], word_cap(settings)),
                                              extra_rules=("Plain style: the first reader is not technical. Technical names only from this list "
                                                           f"(and the skills P3 must name): {', '.join(plain_allow)}; replace every other one "
                                                           "with plain words.") if plain_rule else "")
            retry["tokens"] += fluency["tokens"]
        if fluency["source"] != "model":                                         # the draft is the letter: say what the rewrite would have fixed
            body_parts = {n: draft[n] for n in FL.REWRITTEN if draft.get(n)}
            notes.extend(f"cover_letter: the assembled draft still has: {w}" for w in FL.length_problems(body_parts) + FL.repeat_problems(body_parts))
        retry["fluency"] = fluency
        reuse_body = [parts[n] for n in REUSE_NAMES if parts.get(n)]
        score, who = RU.worst(reuse_body, earlier)
        print(f"  cover letter: assembled ({words(assemble(parts)[0])} words, {'rewritten for fluency' if fluency['source'] == 'model' else 'the assembled draft'}); "
              f"model tokens ~{retry['tokens']} (P5 {p5info['tokens']}, {p5info['source']}; fluency {fluency['tokens']}, {fluency['tries']} tr"
              f"{'y' if fluency['tries'] == 1 else 'ies'})")
        op = plan.opening
        gap = {**plan.gap, "included": "P6" in parts} if plan.gap else {}
        return write_letter(job, kws, parts, team, fb, settings, out_dir, final_name, header, gap, notes=notes, retry=retry,
                            evidence=[{"id": e["id"], "text": e["text"]} for e in vplan.evidence], requirements=vplan.reqs, target_words=target_words(settings),
                            hook_source=(hk.source if hk is not None else "family profile line"), assembled_parts=draft,
                            reuse={"score": round(score, 3), "against": who.name if who else "", "redrafted": bool(variant)},
                            cites=citation_tags(parts, vplan.cite_ids()), dropped=sorted(dropped | ({"P2"} if "P2" in code_parts and "P2" not in cparts else set())),
                            p2_source=plan.p2_source,
                            opening={"fact": op.fact_kind, "hook_id": op.hook_id, "bullet_id": op.bullet_id, "number": op.number,
                                     "problem": op.problem, "source": op.source} if op is not None else {})
    retry["outcome"] = "no letter"
    return none(f"the assembled letter failed its guards in {len(retry['failures'])} variant(s) (a fact-bank problem: no model redraft); last: "
                f"{'; '.join(problems)[:300]}", problems, retry, last_body)


def evidence_prompt(groups: list[list[dict]], reqs: list[list[str]]) -> str:
    """The EVIDENCE section of the prompt: P3's bullets (for the ad skills it names), then P4's (one per role)."""
    out, n = [], 1
    heads = ("P3 EVIDENCE (ad skills to show: {}; the best bullet for them)", "P4 EVIDENCE (one bullet per role, in this order: {})")
    for g, h, req in zip(groups, heads, (", ".join(reqs[0]) if reqs else "", ", ".join(employer(e) for e in groups[1]) if len(groups) > 1 else "")):
        out.append(h.format(req) + "\n" + evidence_fields.block(g, n))
        n += len(g)
    out.append(assignment(groups))
    return "\n".join(out)


def assignment(groups: list[list[dict]]) -> str:
    """Which evidence each paragraph uses, fixed before the model writes: P3 its items, P4 one per role with the numbers its sentence may use (the
    rollup check asks for one of them), P5 none (the ad's own sentences)."""
    n, rows = 1, []
    for name, g in zip(("P3", "P4"), groups):
        items = ", ".join(f"EVIDENCE {n + i} (bullet {e['id']})" for i, e in enumerate(g))
        if name == "P4":
            items = "; ".join(f"EVIDENCE {n + i}, {employer(e)}: the sentence MUST contain "
                              + (" or ".join(sorted(numbers(e["text"]))[:4]) if numbers(e["text"]) else "no number (this bullet has none)") for i, e in enumerate(g))
        rows.append(f"- {name}: {items}")
        n += len(g)
    rows.append("- P5: no bullet; the ABOUT / ROLE SENTENCES only")
    return "EVIDENCE ASSIGNMENT (fixed: each paragraph uses only its own items):\n" + "\n".join(rows)
