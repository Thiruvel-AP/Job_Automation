"""The hook: one idea shown four times, so that a 10-second skim lands on it.

    problem   what the ad hires someone to solve (one clause, at least two nouns that are in the ad)
    proof     ONE fact-bank bullet that has a number or a `result:`, chosen in code
    number    a string that appears verbatim in that bullet's text or result (never invented)

and four strings built from them: the CV headline (a slot under the name), the first line of the profile, the proof bullet
moved to the top of its role, and sentence 1 of the cover letter (written in tailoring/tailor.py from `letter_sentence`).

How the proof is chosen: every bullet that has a number (or a `result:`) is scored as the sum over the JD keywords of
weight x [the keyword is one of the bullet's `terms`] (a multiword keyword also matches when all its words are among the terms); the
"most relevant role" is the experience entry with the highest summed bullet score. `proof_candidates` ranks the usable bullets in
four tiers: a numbered bullet of that role (so the proof is the first bullet of the most relevant role), a numbered bullet of another job,
a numbered project bullet, a bullet without a number (the hook then carries the flag `hook has no number`); inside a tier the highest score wins and a tie goes
to the more recent bullet (the fact bank lists the newest entry first). `build_hook` tries the best MAX_PROOFS of them.

How the strings are made: NO MODEL WRITES THEM. The fact bank's `hook_library` (the user's own wording, one entry per idea: `id`,
`proof` bullet id, `terms`, `headline`, `profile_line`, `letter_line`) is the only source. An entry is ELIGIBLE only when its `terms`
hit at least MIN_OVERLAP of the ad's TOP_KEYWORDS heaviest keywords (weight, then the list order the analysis gave). Among the eligible
the highest sum of keyword weight x [the keyword is one of its `terms`] wins (a tie goes to the earlier entry), and its three strings are
used word for word: the headline under the name, profile sentence 1, letter sentence 1. `Hook.reason` says why it won: the overlapping
top keywords and their weights. An entry whose proof bullet is not in the bank, or that names a deny-listed term, is skipped with a
note. The problem phrase (the call to action's subject) is the entry's own terms that the ad asked for, as a lower-case noun phrase.
When no entry is eligible (or the bank has no library) there is NO hook: build_hook returns None, `notes` says why, and the CV keeps
the role family's headline (no number, no hook line, no proof bullet moved, no hook letter; see tailor._tailor). Nothing writes a new
hook: no model and no code template (`template_strings` and `problems` remain only as helpers, build_hook no longer calls them).
`headline_lint` warns (never rewrites) about a library headline that is long, carries several numbers or names a hardware unit.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .tailoring import typography
from .tailoring.faithfulness import check, deny_hits, numbers
from .tailoring.grounding import grounding_problems
from .tailoring.letter_rules import PASTE_OVERLAP, bank_names, gap_display, noun_phrase, paste_overlap, term_dump_problems
from .tailoring.qualifiers import unsupported_qualifiers
from .tailoring.scorer import Keyword, norm_literal, norm_stem
from .tailoring.selection import _hits

VAGUE_ADJECTIVES = ("passionate", "proven", "dynamic", "results-driven", "results driven", "extensive", "strong", "skilled")
NO_NUMBER_FLAG = "hook has no number"
HEADLINE_MAX = 95              # characters of the CV headline line (role title included): it must stay on one line at 12pt
PROBLEM_MAX_WORDS = 14
PROFILE_LINE_MAX_WORDS = 35
LETTER_SENTENCE_MAX_WORDS = 45
MIN_AD_NOUNS = 2               # words of the problem clause that the ad also uses
PROBLEM_TERMS = 3              # entry terms the ad asked for that name a library hook's problem phrase
PHRASE_WORDS = 4               # words of the bullet a template hook quotes from the number on (short, so it is not a paste)
MAX_PROOFS = 4                 # proof bullets tried, best first, before the template hook is given up
TOP_KEYWORDS = 10              # a library hook is judged against the ad's heaviest keywords only
MIN_OVERLAP = 2                # ... and is eligible only when its terms hit at least this many of them
LINT_HEADLINE_MAX = 60         # characters; a longer library headline gets a lint warning (the CV limit is HEADLINE_MAX)
_LINT_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,:]\d+)*")                                  # "65", "11,459", "8.2", "200:1"; not the 4 of "T4"
# v3.5.4: model and version tokens are not result numbers: "Llama 3.1 8B", "GPT-4o", "Python 3.12", "Qwen 2.5 7B", a bare parameter count "70B".
_LINT_MODEL_NAMES = ("llama", "gpt", "claude", "gemma", "gemini", "mistral", "mixtral", "qwen", "phi", "bert", "roberta", "distilbert", "t5", "yolo",
                     "resnet", "python", "deepseek", "falcon", "whisper", "sdxl", "stable diffusion", "sonnet", "opus", "haiku", "vit", "clip", "llava")
_LINT_MODEL = re.compile(r"(?i)\b(?:" + "|".join(re.escape(n) for n in _LINT_MODEL_NAMES) + r")(?![a-z])(?:[ \t-]?v?\d+(?:\.\d+)*[a-z]{0,2}\b)+")
# "16.2% -> 27.3%", "32% → 52%", "8.2 => 5.5 GB": the first number of a before-to-after pair stands for the pair (the second is not a second result)
_LINT_PAIR = re.compile(r"(?P<first>(?<![A-Za-z0-9])\d+(?:[.,:]\d+)*\s*%?)\s*(?:->|=>|-->|→|⇒|⟶|➔|➜|➝|–>|—>)\s*\d+(?:[.,:]\d+)*\s*%?")
_LINT_PARAMS = re.compile(r"(?<![\w.])\d+(?:\.\d+)?[bB]\b")                                           # 8B, 70B, 1.5B: a model's parameter count
_LINT_UNIT = re.compile(r"(?<![A-Za-z])(?:(?i:gb|mb|ram|gpu|cpu)|ms)(?![A-Za-z])")             # a hardware or resource unit
_STOP = frozenset("""with that this from have been into their over using used which what when where while also about than then
    them they were will would could should your more most such each some team work works working role roles help helps
    and the for you our are not but all can has how its new may who use""".split())


@dataclass
class Hook:
    problem: str
    proof_bullet_id: str
    number: str                                   # "" when no bullet has a number
    headline: str                                 # the whole line under the name: "<role title> · <tail>", at most HEADLINE_MAX
    profile_line: str
    letter_sentence: str
    entry_id: str = ""                            # the experience entry or project the proof bullet belongs to
    entry_kind: str = "experience"                # experience | project
    org: str = ""                                 # the employer, or the project's name
    relevant_role_id: str = ""                    # the experience entry with the highest summed bullet score
    score: float = 0.0                            # the proof bullet's score
    flags: list[str] = field(default_factory=list)
    source: str = "template"                      # library (the user's hook_library, word for word) | template (code, no library match)
    notes: list[str] = field(default_factory=list)     # why a library entry or a template was refused
    hook_id: str = ""                             # the hook_library entry's id
    overlap: list[list] = field(default_factory=list)  # [[keyword, weight], ...]: the ad's top keywords the entry's terms hit
    reason: str = ""                              # why this entry won, printed on the review screen

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Hook":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def enabled(settings) -> bool:
    """settings hook.enabled (default on): build a hook for every job. Off = no headline slot, no hook line, no reordering."""
    raw = ((getattr(settings, "raw", None) or {}).get("hook") or {}).get("enabled")
    return True if raw is None else bool(raw)


# ------------------------------------------------------------------ the proof bullet
def _entries(fb: dict) -> list[tuple[str, dict, dict, int]]:
    """(kind, entry, bullet, fact-bank position) for every bullet: experience entries first, then projects, each as listed."""
    out, n = [], 0
    for kind, sec in (("experience", "experience"), ("project", "projects")):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                out.append((kind, e, b, n))
                n += 1
    return out


_NUM = re.compile(r"(?<![\w.:/#-])[~£$€]?\d++(?::\d++)?(?:,\d{3})*(?:\.\d++)?(?:%|x\b|k\b)?", re.I)
_ORDINAL = re.compile(r"(?:st|nd|rd|th)\b", re.I)                # "3rd place": a rank, not a result
_MODIFIER = re.compile(r"-[A-Za-z]+")                            # "10-class head": the number describes the next word
_UNITS = frozenset("gb mb ms sec secs min mins hours hour days day weeks week months month years year".split())
_VERSION_WORD = re.compile(r"(?:^|\s)(?:[A-Z][A-Za-z]+|v|version|python|gpt)\s*$")


def _plural(word: str) -> bool:
    """A plural noun ("tests", "datasets"), not "class", "loss", "analysis" or "bonus"."""
    return len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "is", "us"))


def _countish(text: str, m: re.Match) -> bool:
    """Does the number look like a measurement (a count, a size, a share) rather than a model version or a date?"""
    s = m.group(0)
    if s[0] in "~£$€" or s[-1] in "%xXkK" or "," in s or ":" in s:
        return True
    ahead = re.match(r"(?:[-\s]+[A-Za-z]+){1,3}", text[m.end():])              # "65 tests", "12 unit tests"; not "10 class head"
    return bool(ahead) and any(w.lower() in _UNITS or _plural(w.lower()) for w in re.findall(r"[A-Za-z]+", ahead.group(0)))


def _numbers_in(text: str) -> list[re.Match]:
    """The numbers of `text` that can be a result: ordinals ("3rd") are left out."""
    return [m for m in _NUM.finditer(text) if not _ORDINAL.match(text[m.end():])]


def _version_like(text: str, m: re.Match) -> bool:
    """"Haiku 4.5", "Python 3.12", "v2": a number that names a release, not a result."""
    before = _VERSION_WORD.search(text[:m.start()])
    if before and text[:m.start()].strip() == before.group(0).strip() and before.group(0).strip().lower() not in ("v", "version", "python", "gpt"):
        return False                                    # the bullet's own first word is a verb ("Ran 20 jobs"), not a product
    return bool(before) and ("." in m.group(0) or len(m.group(0)) <= 2)


def _quantities(text: str, found: list[re.Match]) -> list[re.Match]:
    """`found` without the numbers that describe the next word ("10-class head", "5-star commendation", "3-tier"): a hyphen and a
    word that does not make it a count. "19,213-gene" is a count (a thousands comma), "10-class" is not."""
    return [x for x in found if not (_MODIFIER.match(text[x.end():]) and not _countish(text, x))]


def find_number(bullet: dict) -> str:
    """The bullet's number, verbatim from its text or `result:`, or "" when it has none. The first number of a `result:` (the
    measured outcome the user wrote) wins; then the first count-like number in the text ("~65" in "~65 tests across 8 files", "12 unit
    tests"): never a version ("Haiku 4.5"), a rank ("3rd"), a modifier ("10-class", "10 class head") or a bare digit in a word."""
    result = " ".join(str(bullet.get("result") or "").split())
    in_result = _quantities(result, _numbers_in(result))
    if in_result:
        return in_result[0].group(0)
    text = " ".join(str(bullet.get("text") or "").split())
    for x in _quantities(text, [x for x in _numbers_in(text) if not _version_like(text, x)]):
        if _countish(text, x):
            return x.group(0)
    return ""


def bullet_hook_scores(fb: dict, kws: list[Keyword]) -> dict[str, float]:
    """{bullet id: sum over the JD keywords of weight x (the keyword is one of the bullet's `terms`)}."""
    out = {}
    for _, _, b, _ in _entries(fb):
        hit = _hits(" ".join(str(t) for t in b.get("terms") or []), kws)
        out[b["id"]] = round(sum(kws[i].weight for i in hit), 6)
    return out


def most_relevant_role(fb: dict, scores: dict[str, float]) -> str:
    """The experience entry whose bullets have the highest summed score (the newest one on a tie); "" without experience."""
    best, best_sum = "", -1.0
    for e in fb.get("experience", []):
        total = sum(scores.get(b["id"], 0.0) for b in e["bullets"])
        if total > best_sum:
            best, best_sum = e["id"], total
    return best


def proof_candidates(fb: dict, kws: list[Keyword]) -> list[dict]:
    """Every usable proof bullet, best first: [{"bullet", "entry", "kind", "number", "score", "relevant_role"}]. Four tiers, each
    by score (a tie goes to the more recent bullet): 1) a bullet with a number or a `result:` in the MOST RELEVANT ROLE (the
    experience entry with the highest summed bullet score), so that the proof is the first bullet of that role; 2) such a bullet in
    another job (work done for an employer outranks a project); 3) such a bullet of a project; 4) a bullet without a number (the hook
    then carries the flag `hook has no number`). A bullet naming a deny-listed term is never used."""
    scores = bullet_hook_scores(fb, kws)
    deny = fb.get("deny_terms", [])
    role = most_relevant_role(fb, scores)
    rows = [(kind, e, b, pos, find_number(b)) for kind, e, b, pos in _entries(fb)
            if not deny_hits(b["text"] + " " + str(b.get("result") or ""), deny)]

    def tier(r) -> int:
        if not r[4]:
            return 3
        return 0 if r[0] == "experience" and r[1]["id"] == role else 1 if r[0] == "experience" else 2

    rows.sort(key=lambda r: (tier(r), -scores[r[2]["id"]], r[3]))
    return [{"bullet": b, "entry": e, "kind": kind, "number": number, "score": scores[b["id"]], "relevant_role": role}
            for kind, e, b, _, number in rows]


def choose_proof(fb: dict, kws: list[Keyword]) -> dict | None:
    """The proof bullet (the first of `proof_candidates`) or None for a fact bank with no usable bullet."""
    found = proof_candidates(fb, kws)
    return found[0] if found else None


def org_short(entry: dict, kind: str) -> str:
    """Where the proof was done, as the words after "at": the employer ("AstraZeneca" for "AstraZeneca · GeneTraceAI"), or the
    project's name without its subtitle or brackets ("Image Classification" for "Image Classification — CNN and Transfer Learning")."""
    if kind == "project":
        name = re.split(r"\s+[—–-]\s+|[(:]", str(entry.get("name") or entry["id"]))[0]
        return " ".join(name.split()) or str(entry["id"])
    return str(entry.get("org") or entry.get("title") or "").split("·")[0].strip()


# ------------------------------------------------------------------ the checks on a hook's strings
def _words(text: str) -> int:
    return len(text.split())


def _ad_nouns(problem: str, jd: str) -> list[str]:
    """The content words of `problem` that the ad also uses (compared after stemming)."""
    ad = norm_stem(jd)
    seen: list[str] = []
    for w in re.findall(r"[a-z][a-z0-9+#-]{2,}", norm_literal(problem)):           # three letters and up: SQL, LLM, AWS, RAG
        if w in _STOP or w in seen:
            continue
        if re.search(r"(?<![\w])" + re.escape(norm_stem(w)) + r"(?![\w])", ad):
            seen.append(w)
    return seen


def vague_words(text: str) -> list[str]:
    return [w for w in VAGUE_ADJECTIVES if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", text, re.I)]


def _skill_name_words(text: str, kws: list[Keyword]) -> set[str]:
    """The words of the JD keywords that `text` names in full: "deep" in "deep learning" is part of a skill's name, not praise."""
    lit = norm_literal(text)
    return {w for k in kws for m in k.members() if len(m.split()) > 1 and norm_literal(m) in lit for w in norm_literal(m).split()}


def problems(hk: Hook, fb: dict, kws: list[Keyword], jd: str, company: str = "", role: str = "", company_hook: str = "") -> list[str]:
    """Every reason this hook may not be used (empty = fine). Each string passes the claim check against the proof bullet (the
    existing faithfulness and grounding checks), the number is in it, and none is a paste of any fact-bank bullet."""
    bullet = next((b for _, _, b, _ in _entries(fb) if b["id"] == hk.proof_bullet_id), None)
    if bullet is None:
        return [f"proof bullet {hk.proof_bullet_id!r} is not in the fact bank"]
    deny = fb.get("deny_terms", [])
    own = " ".join(x for x in (bullet["text"], str(bullet.get("result") or ""), hk.org) if x)
    ev = [{"text": bullet["text"] + " " + str(bullet.get("result") or ""), "terms": list(bullet.get("terms") or []),
           "where": hk.org}]
    vocabulary = " ".join([" ".join(b["text"] for _, _, b, _ in _entries(fb)), jd])
    out: list[str] = []

    def add(label: str, msgs: list[str]) -> None:
        out.extend(f"{label}: {m}" for m in msgs)

    if _words(hk.problem) > PROBLEM_MAX_WORDS:
        add("problem", [f"{_words(hk.problem)} words > {PROBLEM_MAX_WORDS}"])
    if len(_ad_nouns(hk.problem, jd)) < MIN_AD_NOUNS:
        add("problem", [f"fewer than {MIN_AD_NOUNS} nouns that the ad uses (has {_ad_nouns(hk.problem, jd)})"])
    add("problem", [f"deny-listed '{d}'" for d in deny_hits(hk.problem, deny)])
    strings = (("headline", hk.headline), ("profile_line", hk.profile_line), ("letter_sentence", hk.letter_sentence))
    if len(hk.headline) > HEADLINE_MAX:
        add("headline", [f"{len(hk.headline)} characters > {HEADLINE_MAX}"])
    if "\n" in hk.headline:
        add("headline", ["more than one line"])
    for label, text in strings:
        if "—" in text:
            add(label, ["an em dash (the CV has none)"])
    if _words(hk.profile_line) > PROFILE_LINE_MAX_WORDS:
        add("profile_line", [f"{_words(hk.profile_line)} words > {PROFILE_LINE_MAX_WORDS}"])
    if _words(hk.letter_sentence) > LETTER_SENTENCE_MAX_WORDS:
        add("letter_sentence", [f"{_words(hk.letter_sentence)} words > {LETTER_SENTENCE_MAX_WORDS}"])
    for label, text in strings:
        if not text.strip():
            add(label, ["empty"])
            continue
        if hk.number and hk.number not in text:
            add(label, [f"does not carry the number {hk.number!r} verbatim"])
        add(label, [f"vague word '{w}'" for w in vague_words(text)])
        add(label, term_dump_problems([text], fb))                      # a list of terms, or a technology in lower case
        c = check(text, own + " " + role, list(bullet.get("terms") or []), deny, kws, vocabulary=vocabulary, max_growth=4.0)
        framing = norm_stem(hk.problem)            # the problem clause is the ad's wording: it may name the ad's own keywords
        for v in c.violations:
            m = re.match(r"keyword '(.+)' not evidenced by source", v)
            if m and re.search(r"(?<![\w])" + re.escape(norm_stem(m.group(1))) + r"(?![\w])", framing):
                continue
            add(label, [v])
        skill_words = _skill_name_words(text, kws)
        add(label, [f"unsupported qualifier '{w}'" for w in unsupported_qualifiers(text, own) if w not in skill_words])
        add(label, grounding_problems([text], ev, jd, company_hook, f"{company} {role} {hk.org} {hk.problem}", []))
        share = max((paste_overlap(text, b["text"]) for _, _, b, _ in _entries(fb)), default=0.0)
        if share >= PASTE_OVERLAP:
            add(label, [f"{share:.0%} of its words are one fact-bank bullet's: a paste, not a hook"])
    extra = numbers(" ".join(t for _, t in strings)) - numbers(own + " " + (hk.number or ""))
    if extra:
        add("hook", [f"new numbers {sorted(extra)}"])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the strings
def _clean(text) -> str:
    return " ".join(str(text or "").split()).strip()


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not any(c.isupper() for c in s[1:3]) else s


_DANGLING = frozenset("and with the a an of to for in on by it its that which while but or as from into over across behind within after before via using under per at than then so".split())


def number_phrase(bullet: dict, number: str) -> str:
    """The short stretch of the proof that carries the number, verbatim: the bullet's `result:` when it holds the number
    ("~65 tests across 8 files"), else the words from the number to the next comma, bracket or full stop (at most PHRASE_WORDS)."""
    result = _clean(bullet.get("result"))
    if number and number in result:
        return result
    text = _clean(bullet.get("text"))
    if number and number in text:
        tail = text[text.index(number):]
        words = re.split(r",(?!\d{3})|[;:()]|\.\s|\.$", tail, maxsplit=1)[0].split()[:PHRASE_WORDS]
        while len(words) > 1 and words[-1].lower() in _DANGLING:                  # "10 and compared it" -> "10"
            words.pop()
        return " ".join(words).strip()
    first = re.split(r",(?!\d{3})|[;:()]", text, maxsplit=1)[0]
    return _lower_first(" ".join(first.split()[:7]).rstrip(".")).strip()


def problem_terms(kws: list[Keyword], bullet: dict, deny: list[str], jd: str = "", n: int = 2, most: int = 3) -> list[str]:
    """The problem as ad keywords: the heaviest JD keywords the proof bullet's terms evidence (the heaviest overall when none
    do), longer phrases first on a tie, never a deny-listed one. `n` of them, and up to `most` until the clause holds
    MIN_AD_NOUNS words the ad uses (two bare acronyms may be only two words)."""
    hit = _hits(" ".join(str(t) for t in bullet.get("terms") or []), kws)
    pool = [k for k in kws if not deny_hits(k.term, deny)]
    ranked = [k.term for k in sorted(pool, key=lambda k: (kws.index(k) not in hit, -k.weight, -len(k.term.split()), kws.index(k)))]
    out = ranked[:n]
    while len(out) < min(most, len(ranked)) and len(_ad_nouns(" and ".join(out), jd)) < MIN_AD_NOUNS:
        out.append(ranked[len(out)])
    return out


def template_strings(proof: dict, kws: list[Keyword], fb: dict, role: str, company: str, team: str = "", jd: str = "") -> dict[str, str]:
    """The hook's strings from fixed templates over the checked pieces: used when the model is unavailable or its answer failed
    a check. Plain, and made only of the ad's own keywords and the proof's own words."""
    bullet, number = proof["bullet"], proof["number"]
    org = org_short(proof["entry"], proof["kind"])
    terms = [gap_display(t, jd) for t in problem_terms(kws, bullet, fb.get("deny_terms", []), jd)]     # "MLflow", not "mlflow"
    problem = " and ".join(terms) if terms else _clean(proof["entry"].get("title") or role)
    phrase = number_phrase(bullet, number)
    tail = f"{problem}: {phrase}"
    if len(f"{role} · {tail}") > HEADLINE_MAX:
        tail = f"{terms[0] if terms else problem}: {phrase}"
    if len(f"{role} · {tail}") > HEADLINE_MAX:
        tail = phrase
    where = f"in my {org} project" if proof["kind"] == "project" else f"at {org}"
    team_part = f" {team}" if team else ""
    return {"problem": problem, "headline": f"{role} · {tail}",
            "profile_line": f"{role} focused on {problem}, including {phrase} {where}.",
            "letter_sentence": f"{company}{team_part} needs {problem}, and {where} my work includes {phrase}."}


def _typeset(hk: Hook, kws: list[Keyword]) -> Hook:
    """The hook's three strings as the CV prints them (hyphens closed up, the JD's own hyphenated keywords kept), so that the review
    screen, the report and the PDF all say the same words. The checks have already passed on the strings as written."""
    keep = [w for k in kws for w in k.members() if "-" in w]
    hk.headline, hk.profile_line, hk.letter_sentence = (typography.clean(t, keep=keep) for t in (hk.headline, hk.profile_line, hk.letter_sentence))
    return hk


# ------------------------------------------------------------------ the user's hook_library
LIBRARY_STRINGS = ("headline", "profile_line", "letter_line")


def library(fb: dict) -> list[dict]:
    """The fact bank's `hook_library` entries, in the user's order ([] when it has none)."""
    return [e for e in (fb.get("hook_library") or []) if isinstance(e, dict) and e.get("id")]


def library_scores(fb: dict, kws: list[Keyword]) -> dict[str, float]:
    """{entry id: sum over the JD keywords of weight x [the keyword is one of the entry's `terms`]} (the bullets' hook score, applied to
    the entry's own terms), in library order."""
    return {str(e["id"]): round(sum(kws[i].weight for i in _hits(" ".join(str(t) for t in e.get("terms") or []), kws)), 6)
            for e in library(fb)}


def find_bullet(fb: dict, bullet_id: str) -> tuple[str, dict, dict] | None:
    """(kind, entry, bullet) of a fact-bank bullet id, or None."""
    return next(((kind, e, b) for kind, e, b, _ in _entries(fb) if b["id"] == bullet_id), None)


def proof_ids(entry: dict) -> list[str]:
    """The proof bullet ids of a hook_library entry, in the user's order: `proof: az4` or (v3.5.3, 10 Nov 2026) `proof: [az4, az3, az12]`."""
    raw = entry.get("proof")
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return list(dict.fromkeys(str(x).strip() for x in items if x is not None and str(x).strip()))


def find_proof(fb: dict, entry: dict) -> tuple[str, dict, dict] | None:
    """(kind, entry, bullet) of the entry's first listed proof bullet that is in the fact bank (the one the CV moves to the top of its role),
    or None when none of them is."""
    return next((found for bid in proof_ids(entry) if (found := find_bullet(fb, bid)) is not None), None)


def top_keyword_overlap(entry: dict, kws: list[Keyword]) -> list[tuple[str, float]]:
    """[(keyword, weight)] for the ad's TOP_KEYWORDS heaviest keywords (weight, then list order) that the entry's `terms` hit, heaviest first."""
    top = sorted(range(len(kws)), key=lambda i: (-kws[i].weight, i))[:TOP_KEYWORDS]
    hit = _hits(" ".join(str(t) for t in entry.get("terms") or []), kws)
    return [(kws[i].term, float(kws[i].weight)) for i in top if i in hit]


def _weights(pairs) -> str:
    return ", ".join(f"'{t}' ({w:g})" for t, w in pairs)


def selection_reason(entry_id: str, overlap, score: float) -> str:
    """Why a library entry won, for the review screen: the overlapping top keywords with their weights."""
    total = sum(w for _, w in overlap)
    return (f"{entry_id} won: its terms hit {len(overlap)} of the ad's top {TOP_KEYWORDS} keywords, {_weights(overlap)} "
            f"(top-keyword weight {total:g}; all-keyword score {score:g})")


def result_numbers(text: str) -> list[str]:
    """The numbers of a headline that state a result: model names with their version and size ("Llama 3.1 8B", "GPT-4o") and bare parameter counts
    ("70B") are left out first, and a before-to-after pair written with an arrow ("16.2% -> 27.3%", "32% → 52%") counts once, as one result (the
    pair is the result; v3.5.5). "from 12 to 3" is not an arrow pair: it still counts twice."""
    return _LINT_NUMBER.findall(_LINT_PAIR.sub(r"\g<first>", _LINT_PARAMS.sub(" ", _LINT_MODEL.sub(" ", text))))


def headline_lint(headline: str) -> list[str]:
    """Warnings about a hook headline; it is never rewritten. Over LINT_HEADLINE_MAX characters, more than one RESULT number (a model name, version or
    size such as "Llama 3.1 8B" is not one: result_numbers), or a hardware or
    resource unit (GB, MB, RAM, GPU, ms, CPU)."""
    text = _clean(headline)
    out = []
    if len(text) > LINT_HEADLINE_MAX:
        out.append(f"{len(text)} characters > {LINT_HEADLINE_MAX}")
    nums = result_numbers(text)
    if len(nums) > 1:
        out.append(f"more than one number ({', '.join(nums)})")
    units = list(dict.fromkeys(_LINT_UNIT.findall(text)))
    if units:
        out.append(f"hardware or resource unit ({', '.join(units)})")
    return out


def library_lint(fb: dict) -> list[tuple[str, str]]:
    """[(entry id, warning)] for every hook_library headline that headline_lint complains about (read only)."""
    return [(str(e["id"]), w) for e in library(fb) for w in headline_lint(str(e.get("headline") or ""))]


def iter_eligible(fb: dict, kws: list[Keyword], notes: list[str] | None = None):
    """The eligible library entries for this ad, best first, as pick_library_entry's dicts (a generator: the first is the pick). Only an entry
    whose terms hit at least MIN_OVERLAP of the ad's TOP_KEYWORDS heaviest keywords is eligible. Highest score first, a tie to the earlier entry in
    the library. An entry is skipped (and `notes` says why) when its proof bullet is not in the fact bank, when a string is missing, when it
    (or its proof bullet) names a deny-listed term, or (v3.5.2) when it claims something its proof bullets do not (tailoring/hook_claims.py). The cover letter walks the rest of the ranking when the CV's hook has no number (opening.py)."""
    notes = notes if notes is not None else []
    deny = fb.get("deny_terms", [])
    scores = library_scores(fb, kws)
    entries = library(fb)
    overlaps = {str(e["id"]): top_keyword_overlap(e, kws) for e in entries}
    for e in sorted(entries, key=lambda e: (-scores[str(e["id"])], entries.index(e))):
        score, eid = scores[str(e["id"])], str(e["id"])
        if len(overlaps[eid]) < MIN_OVERLAP:
            continue
        found = find_proof(fb, e)
        if found is None:
            notes.append(f"hook_library '{eid}': proof bullet {e.get('proof')!r} is not in the fact bank; entry skipped")
            continue
        lost = [b for b in proof_ids(e) if find_bullet(fb, b) is None]
        if lost:
            notes.append(f"hook_library '{eid}': listed proof bullet(s) {', '.join(lost)} are not in the fact bank (the others are used)")
        missing = [k for k in LIBRARY_STRINGS if not _clean(e.get(k))]
        if missing:
            notes.append(f"hook_library '{eid}': no {', '.join(missing)}; entry skipped")
            continue
        kind, owner, bullet = found
        denied = deny_hits(" ".join([*(_clean(e[k]) for k in LIBRARY_STRINGS), bullet["text"], str(bullet.get("result") or "")]), deny)
        if denied:
            notes.append(f"hook_library '{eid}': names a deny-listed term ({', '.join(denied)}); entry skipped")
            continue
        from .tailoring import hook_claims                                # v3.5.2: a hook may claim nothing its proof bullets do not
        clash = hook_claims.conflicts(fb, e)
        if clash:
            notes.append(f"hook_library '{eid}': BLOCKED, it claims what its proof bullets do not (careerops hook-check): "
                         + "; ".join(c.line() for c in clash[:3]))
            continue
        yield {"entry": e, "score": score, "kind": kind, "bullet_entry": owner, "bullet": bullet, "overlap": overlaps[eid],
               "reason": selection_reason(eid, overlaps[eid], score)}


def pick_library_entry(fb: dict, kws: list[Keyword], notes: list[str] | None = None) -> dict | None:
    """The library entry to use for this ad: {"entry", "score", "kind", "bullet_entry", "bullet", "overlap", "reason"}, or None (the first of
    `iter_eligible`; its rules decide). None adds a note naming the closest entry."""
    notes = notes if notes is not None else []
    found = next(iter_eligible(fb, kws, notes), None)
    if found is not None:
        return found
    entries = library(fb)
    overlaps = {str(e["id"]): top_keyword_overlap(e, kws) for e in entries}
    if entries:
        best = max(entries, key=lambda e: (len(overlaps[str(e["id"])]), -entries.index(e)))
        notes.append(f"no hook_library entry hits at least {MIN_OVERLAP} of the ad's top {TOP_KEYWORDS} keywords (closest: '{best['id']}' "
                     f"with {len(overlaps[str(best['id'])])}: {_weights(overlaps[str(best['id'])]) or 'none'})")
    return None


def entry_problem(entry: dict, kws: list[Keyword], jd: str, known=()) -> str:
    """The subject of the call to action: the entry's own `terms` that the ad asked for (heaviest keyword first, the entry's order on a
    tie, one term per keyword), at most PROBLEM_TERMS of them, as a lower-case noun phrase."""
    rows = []
    for n, t in enumerate(entry.get("terms") or []):
        hit = _hits(str(t), kws)
        if hit:
            rows.append((-sum(kws[i].weight for i in hit), n, str(t), hit))
    seen: set[frozenset] = set()
    chosen: list[str] = []
    for _, _, term, hit in sorted(rows, key=lambda r: r[:2]):
        if hit not in seen:
            seen.add(hit)
            chosen.append(term)
    return noun_phrase(chosen[:PROBLEM_TERMS], jd, known)


def library_hook(picked: dict, fb: dict, kws: list[Keyword], jd: str, notes: list[str]) -> Hook:
    """The Hook of a library entry: its three strings word for word (no typesetting, no checks beyond the deny list: they are the user's
    own words), its proof bullet, and a problem phrase made of its terms."""
    e, bullet, owner, kind = picked["entry"], picked["bullet"], picked["bullet_entry"], picked["kind"]
    number = find_number(bullet)
    role = most_relevant_role(fb, bullet_hook_scores(fb, kws))
    return Hook(entry_problem(e, kws, jd, bank_names(fb)), bullet["id"], number, _clean(e["headline"]), _clean(e["profile_line"]), _clean(e["letter_line"]),
                owner["id"], kind, org_short(owner, kind), role, picked["score"], [] if number else [NO_NUMBER_FLAG], "library",
                list(notes), str(e["id"]), [[t, w] for t, w in picked["overlap"]], picked["reason"])


def build_hook(job: dict, kws: list[Keyword], fb: dict, llm=None, role: str = "", team: str = "", company_hook: str = "",
               notes: list[str] | None = None) -> Hook | None:
    """The hook for this job: the fact bank's `hook_library` entry that hits at least MIN_OVERLAP of the ad's TOP_KEYWORDS heaviest
    keywords (see the module docstring), its strings word for word, no model. None when no entry qualifies (or there is no library):
    nothing is written instead, the CV keeps the role family's headline with no number, and `notes` says why so that the run can
    report it. `llm`, `role`, `team` and `company_hook` are accepted for the old signature and never used."""
    notes = notes if notes is not None else []
    picked = pick_library_entry(fb, kws, notes)
    if picked:
        return library_hook(picked, fb, kws, job.get("description") or "", notes)
    if not library(fb):
        notes.append("the fact bank has no hook_library")
    return None


# ------------------------------------------------------------------ putting the proof bullet on the CV
def ensure_selected(sel: dict, fb: dict, hk: Hook) -> dict:
    """`sel` (selection.select_cv's answer) with the proof bullet in it: an experience bullet joins its entry's list; a project
    bullet puts its project first in the projects with that bullet among its chosen ones. A copy is returned."""
    sel = {"experience": {k: list(v) for k, v in sel["experience"].items()}, "projects": list(sel["projects"]),
           "project_bullets": {k: list(v) for k, v in (sel.get("project_bullets") or {}).items()}}
    bid = hk.proof_bullet_id
    if hk.entry_kind == "experience":
        ids = sel["experience"].setdefault(hk.entry_id, [])
        if bid not in ids:
            ids.append(bid)
    else:
        sel["projects"] = [hk.entry_id] + [p for p in sel["projects"] if p != hk.entry_id]
        chosen = sel["project_bullets"].get(hk.entry_id)
        if chosen is not None and bid not in chosen:
            chosen.append(bid)
        elif chosen is None:
            sel["project_bullets"][hk.entry_id] = [bid]
    return sel


def proof_first(ctx: dict, hk: Hook) -> bool:
    """Move the proof bullet to the first place of its entry in the render context (the experience entry, or the project, which
    also goes to the top of the projects). The bullet keeps its text. True when the bullet is on the CV."""
    sec = "experience" if hk.entry_kind == "experience" else "projects"
    for e in ctx[sec]:
        if e.get("id") == hk.entry_id and hk.proof_bullet_id in e["bullet_ids"]:
            i = e["bullet_ids"].index(hk.proof_bullet_id)
            e["bullet_ids"].insert(0, e["bullet_ids"].pop(i))
            e["bullets"].insert(0, e["bullets"].pop(i))
            if sec == "projects":
                ctx["projects"].insert(0, ctx["projects"].pop(ctx["projects"].index(e)))
            return True
    return False


def keeps_number(text: str, hk: Hook) -> bool:
    """Does a (reworded) proof bullet still say the hook's number? Without a number there is nothing to keep."""
    return not hk.number or hk.number in text
