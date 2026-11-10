"""The assembled company letter (v2.5, `letter.compose: assembled`, the code default): a letter that always exists.

v3.5.2 (10 Nov 2026; `compose`, below, replaces the P3 / P4 rules described next): one paragraph per employer or project. P3 tells the bullets of the
place whose CV bullets carry most of the ad's skills, P4 those of the next place (an employer first); at most MAX_CLAIMS (2) bullets a paragraph;
every sentence opens with where the work was done, with the context the fact bank gives it ("On my AstraZeneca dissertation project, I ...",
"On my CoverFit hackathon project, I ..."); in plain style a bullet is told with its approved `plain:`, else without its bracketed asides, and is
left out when it still holds jargon the ad does not name (Ensembl, HGNC, parquet ...). P5 is the ad's own words about what the company does for its
customers tied to ONE fact of this CV: "<ad clause> — the same aim as my work at <place>, where <the fact's first clause>." The model may write only
the ad clause; the guards read the two halves apart (company guards on the ad half, the fact guards on the other).

Free drafts failed because every sentence that makes a claim about you is a chance to fail a guard: with k ≈ 12 claim sentences and a pass rate of
p ≈ 0.92 per sentence, P(pass) = p^k ≈ 0.37, and three retries near temperature 0 are almost the same draft. Here the sentences about you are
ASSEMBLED from verified text and the model writes only the sentence about the company:

  P1  the plan's opening (opening.py, code): the ad's problem phrase, a semicolon, the hook's letter line (verbatim, verified).
  P2  the hook fact's `limits` / `result` (code; left out when there is neither).
  P3  the one or two bullets of this CV that evidence the ad's required skills (skill_group), each turned into first person by ONE fixed rule:
      a bullet that opens with a past-tense verb gets "I " before it ("Built X" -> "I built X"); a "problem: action" bullet becomes one sentence
      (v3.4: "Needed X: built Y" -> "To deliver X, I built Y."; "A client needed X: led Y" -> "For a client that needed X, I led Y.";
      "Built X: reconciled Y" -> "I built X and reconciled Y."), never "I needed X: built Y"; any other wording stays word for word. When those
      bullets do not say two of the ad skills they evidence, one sentence names them from the bullets' own `terms`: "That work used X and Y."
  P4  one sentence per role (role_group: the most relevant roles), "At <employer>, " + that role's bullet on this CV that holds a number, first person.
      A bullet in both P3 and P4 is told once (v3.4, split_shared): P3 keeps it, with its employer, and P4 leaves that role out.
  P5  the model: ONE sentence on why this team, holding a run of at least 4 words of the ad; two tries, each checked by the letter's own P5 guards;
      then the fallback "<Company>'s focus on <a noun phrase of the ad> is why I applied." (v3.4; never "... is <Company> is ...").
  P6  off unless letter.honest_gap (code, as before).
  P7  the plan's ask (code).

Length: under the range, the next numbered bullet of this CV not used yet (then any other bullet of it) is added to P3 (four at most); over it, P2 is dropped, then P3 keeps one
bullet. The WHOLE letter then goes through company_letter.check, unchanged, except that a sentence this module assembled word for word from a bullet
is not reported as "pastes a fact-bank bullet" (pasting the verified bullet is the point; every other guard still reads it). An assembled sentence that
fails a guard is a fact-bank problem: the bullet id is printed and the model is not asked again. A letter whose P2-P5 repeat an earlier letter
(5-word shingles >= 0.30) is rebuilt from the next bullets (up to VARIANTS times), never sent as it is.

`[l]` feedback steers the bullets ("use dc1 not az9", "shorter") and P5's wording; it never rewrites a verified sentence. Tokens: only P5's prompt and
reply, printed as an estimate (characters / 4).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from .grounding import sentences

TASK = "cover_letter"
P5_TRIES = 2
VARIANTS = 3
GROW_MAX = 4
FALLBACK = "What draws me to this role is {phrase}."
FALLBACK_NP = "{owner} focus on {phrase} is why I applied."        # v3.4: the noun-phrase frame, tried first
AD_RUN = 4
PAST_IRREGULAR = frozenset("""built led ran wrote made set drove took cut grew won taught brought found gave kept held sold spent shipped rebuilt broke chose
got put read saw sent told thought understood began became did drew fed hid hung knew lit met paid rode rose shot showed sang sat slept spoke stood stole
swam threw wore woke""".split())
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")
_WE = re.compile(r"\b(?:we|our|ours|us|we're|we've|we'll)\b", re.I)

P5_SYSTEM = """You write ONE sentence (at most 40 words) for a cover letter: why this team and role appeal, in the first person.
It must contain, word for word, a run of at least 4 consecutive words copied from one of the AD SENTENCES. Make no claim about the candidate's
experience or skills, give no number, never write "we", "our" or "us", invent no name, never open with "I am excited", "I am writing" or "I am
applying". Return JSON only: {"p5": "<the sentence>"}."""
P5_CLAUSE_SYSTEM = """You write ONE clause (at most 26 words) for a cover letter: what the company does for its customers, with the company's
name as its subject, in the ad's own words. It must contain, word for word, a run of at least 4 consecutive words copied from one of the AD
SENTENCES, preferably the one about what customers or clients can do. Say nothing about the candidate, never write "I", "we", "our" or "us", give
no number the ad does not, invent no name. Return JSON only: {"p5": "<the clause>"}."""
P5_USER = """COMPANY: {company}
ROLE: {title}
AD SENTENCES:
{about}
{feedback}"""


def compose_mode(settings) -> str:
    raw = getattr(settings, "raw", settings) or {}
    v = str(((raw.get("letter") or {}) if isinstance(raw, dict) else {}).get("compose") or "assembled").strip().lower()
    return v if v in ("assembled", "free") else "assembled"


def _past(word: str, capital: bool = True) -> bool:
    """A past-tense verb ("Built", "reconciled"); `capital`: it must open with a capital letter (the first word of a bullet)."""
    w = word.strip(",;:")
    low = w.lower()
    if capital and not (w[:1].isupper() and not w.isupper()):
        return False
    if not capital and not w[:1].islower():
        return False
    return low in PAST_IRREGULAR or re.fullmatch(r"[a-z-]+ed", low) is not None


def _colon_split(text: str) -> tuple[str, str] | None:
    """(before, after) the first ": " outside brackets, or None ("gpt-oss:20b" and "(a: b)" do not split)."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0 and text[i + 1:i + 2] == " ":
            return text[:i].strip(), text[i + 2:].strip()
    return None


_NEED = re.compile(r"^(?P<who>.*?)\b(?P<verb>needed|wanted|required|asked for)\s+(?P<what>.+)$", re.I)
_ARTICLE = frozenset({"a", "an", "the"})


def _stop(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


def first_person(text: str) -> str:
    """The fixed rule: a bullet that opens with a past-tense verb gets "I " ("Built X" -> "I built X"); a "problem: action" bullet (the part after
    ": " opens with a past-tense verb) becomes one sentence: "Needed X: built Y" -> "To deliver X, I built Y."; "A client needed X: led Y" ->
    "For a client that needed X, I led Y."; "Built X: reconciled Y" -> "I built X and reconciled Y." Anything else stays word for word."""
    t = _BULLET.sub("", " ".join(str(text or "").split())).strip()
    if not t:
        return ""
    split = _colon_split(t)
    if split and split[0] and split[1] and _past(split[1].split(" ", 1)[0], capital=False):
        left, right = split
        lhead = left.split(" ", 1)[0]
        acted = _past(lhead) and lhead.lower() not in ("needed", "wanted", "required")      # "Built X: ..." is an action, "Needed X: ..." a need
        m = None if acted else _NEED.match(left)
        if m:
            who, what = m.group("who").strip(), m.group("what").strip().rstrip(",")
            if not who:
                return _stop(f"To deliver {what}, I {right}")
            first = who.split(" ", 1)[0]
            who = who[0].lower() + who[1:] if first.lower() in _ARTICLE else who
            return _stop(f"For {who} that {m.group('verb').lower()} {what}, I {right}")
        if acted:
            return _stop(f"I {lhead.lower()}{left[len(lhead):]} and {right}")
    head, _, rest = t.partition(" ")
    word = head.strip(",;:")
    low = word.lower()
    out = f"I {low}{head[len(word):]} {rest}".strip() if _past(word) else t
    return _stop(out)


def at_role(e: dict) -> str:
    """P4's sentence for one role: "At <employer>, " + the bullet in first person."""
    from .company_letter import employer

    who = employer(e)
    body = first_person(e["text"])
    if body.startswith("I "):
        return f"At {who}, {body}" if who else body
    if body.startswith(("To deliver ", "For ")):                               # a "problem: action" bullet: "At X, to deliver Y, I built Z."
        return f"At {who}, {body[0].lower()}{body[1:]}" if who else body
    return f"At {who}: {body}" if who else body


def split_shared(g3: list[dict], g4: list[dict]) -> tuple[list[dict], list[dict], set[str]]:
    """(P3 bullets, P4 bullets, the ids P3 tells with their employer): a bullet in both P3 and P4 is told once. P3 keeps it (it evidences the ad's
    skills) and P4 leaves that role out when another role is left; else P3 drops it when another bullet is left; else both stay (the fluency
    pass then has to say it once). RightShip, 3 Nov 2026: the 180 Degrees bullet was printed in P3 and again in P4."""
    shared = {e["id"] for e in g3} & {e["id"] for e in g4}
    if not shared:
        return g3, g4, set()
    rest4 = [e for e in g4 if e["id"] not in shared]
    if rest4:
        return g3, rest4, shared
    rest3 = [e for e in g3 if e["id"] not in shared]
    if rest3:
        return rest3, g4, set()
    return g3, g4, set()


def _spelling(term: str, group: list[dict]) -> str:
    """How the bullets themselves write `term` ("Python", "LLM"), else their `terms`, else the keyword as given (keywords are lower case)."""
    text = " ".join(e["text"] for e in group)
    m = re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text, re.I)
    if m:
        return m.group(0)
    return next((t for e in group for t in e.get("terms", []) if t.lower() == term.lower() and t != t.lower()), term)


def _terms_sentence(group: list[dict], reqs: list[str], said: str) -> str:
    """'That work used X and Y.' with the ad skills these bullets evidence (their own terms) that the sentences do not say yet."""
    from ..tailoring.scorer import norm_literal

    lit = norm_literal(said)
    have = {t.lower() for e in group for t in e.get("terms", [])}
    missing = [r for r in reqs if r.lower() in have and norm_literal(r) not in lit]
    named = [r for r in reqs if norm_literal(r) in lit]
    need = max(0, min(2, len(reqs)) - len(named))
    missing = [r for r in missing if not (len(r.split()) == 1 and re.search(r"(?:ic|ive|al|ous)$", r.lower()))]     # "agentic" is no tool
    pick = [_spelling(r, group) for r in missing[:need]]
    if not pick:
        return ""
    return f"That work used {pick[0]}." if len(pick) == 1 else f"That work used {pick[0]} and {pick[1]}."


@dataclasses.dataclass
class Steer:
    prefer: set[str]
    avoid: set[str]
    shorter: bool
    text: str


def steer(said: str) -> Steer:
    """What `[l]` feedback decides here: bullet ids after "use" are preferred, after "not" / "instead of" / "avoid" / "drop" avoided; "shorter"."""
    s = said or ""
    avoid = set(re.findall(r"\b(?:not|instead of|avoid|drop|without)\s+([a-z]{1,4}\d{1,3}[a-z]?)\b", s, re.I))      # bullet ids: az9, dc1, p1a
    prefer = set(re.findall(r"\b(?:use|with|prefer)\s+([a-z]{1,4}\d{1,3}[a-z]?)\b", s, re.I)) - avoid
    return Steer({p.lower() for p in prefer}, {a.lower() for a in avoid}, bool(re.search(r"\bshort(?:er|en)\b", s, re.I)), s)


def _numbered_unused(fb: dict, chosen: set[str] | None, used: set[str], avoid: set[str], kws=None) -> list[dict]:
    """The bullets of this CV not used yet, the ones that say most of the ad's keywords first (v3.4: CV order put an iOS bullet in the RightShip
    letter), the numbered ones first among equals (the length rule's "next-ranked numbered bullet"), then the rest."""
    from .scorer import keyword_hits

    from . import tailor as T

    out = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                if (chosen is None or b["id"] in chosen) and b["id"] not in used and b["id"].lower() not in avoid:
                    out.append(b)
    def weight(b: dict) -> float:
        return sum(kws[i].weight for i in keyword_hits(f"{b['text']} {' '.join(b.get('terms', []))}", kws)) if kws else 0.0
    out.sort(key=lambda b: (-weight(b), 0 if re.search(r"\d", b["text"]) else 1))
    return T._with_orgs(fb, out)


def groups_for(plan, fb: dict, kws, chosen: set[str] | None, jd: str, hk, variant: int, st: Steer) -> tuple[list[dict], list[dict], list[str]]:
    """(P3 bullets, P4 bullets, P3's ad skills) for one variant: variant 0 is the plan's own; each later one also avoids the bullets used before."""
    from . import company_letter as CL
    from . import tailor as T

    if variant == 0 and not st.avoid and not st.prefer:
        return plan.groups[0], plan.groups[1], plan.reqs[0]
    op = plan.opening
    skip = {x for x in (getattr(op, "bullet_id", ""), getattr(hk, "proof_bullet_id", "") if hk is not None else "") if x} | st.avoid
    tried = {e["id"] for e in plan.groups[0]} if variant else set()
    skills, g3 = CL.skill_group(fb, kws, chosen, jd, skip | tried) if variant else CL.skill_group(fb, kws, chosen, jd, skip)
    if not g3:
        skills, g3 = plan.reqs[0], [e for e in plan.groups[0] if e["id"].lower() not in st.avoid] or plan.groups[0]
    preferred = [e for e in T._with_orgs(fb, [b for sec in ("experience", "projects") for x in fb.get(sec, []) for b in x["bullets"]
                                               if b["id"].lower() in st.prefer])]
    g3 = (preferred + [e for e in T._with_orgs(fb, g3) if e["id"] not in {p["id"] for p in preferred}])[:2]
    avoid4 = {e["id"] for e in g3} | skip | ({e["id"] for e in plan.groups[1]} if variant else set())
    g4 = CL.role_group(fb, kws, chosen, jd, hk, avoid4) or plan.groups[1]
    return g3, T._with_orgs(fb, g4), skills or plan.reqs[0]


_YOU_WILL = re.compile(r"^(?:in this (?:role|position|job),?\s*)?(?:you will|you'll|you would)\s+", re.I)
_IS_TO = re.compile(r"\b(?:is|are)\s+to\s+(.+)$", re.I)
_YOU = re.compile(r"\b(?:you|your|yours)\b", re.I)


_HEAD_NOUNS = frozenset("""platform platforms company companies provider providers business businesses solution solutions firm organisation
organization leader leaders marketplace service services brand group""".split())
_ADJ_LEAD = frozenset("leading largest biggest best top premier first global trusted fastest-growing only foremost".split())
_NP_STOP = re.compile(r"\s*(?:[,;:.!?()]|\s[-–—]\s|\b(?:that|which|who|whose|where|while|by|so|because|to|as|when|if|primarily|through|via|"
                      r"using|and you)\b)", re.I)
BASE_VERBS = frozenset("""design build develop deploy write explore communicate lead own create deliver drive partner collaborate maintain improve
shape help support define run apply present contribute propose produce gather translate prototype experiment evaluate integrate work""".split())
_NP_PUNCT = re.compile(r"\s*(?:[,;:.!?()]|\s[-–—]\s)")
_FINITE = re.compile(r"\b(?:is|are|was|were|has|have)\b", re.I)


def possessive(name: str) -> str:
    """"RightShip" -> "RightShip's"; "Acme Systems" -> "Acme Systems'"."""
    n = name.strip()
    return f"{n}'" if n.endswith("s") else f"{n}'s"


def noun_phrases(offered: list[str], company: str) -> list[str]:
    """Noun phrases of the ad's About / role sentences, best first, for "<Company>'s focus on <phrase> is why I applied.": what "<Company> is a / the
    ..." names ("the maritime industry's leading safety and risk intelligence platform" -> "safety and risk intelligence"), what "<Company> builds /
    provides / develops ..." makes, then what follows "vision of", "focus on", "dedicated to", "committed to", "specialises in", "work on / across /
    with". 3-14 words, no verb, no we / our / you."""
    pats = [(re.compile(rf"^{re.escape(company)}\s+(?:is|are)\s+(?:the|a|an)\s+(.+)$", re.I), True),
            (re.compile(rf"^{re.escape(company)}\s+(?:builds|makes|provides|develops|delivers|offers|creates|designs|runs|operates)\s+(.+)$", re.I), False),
            ] if company.strip() else []
    pats.append((re.compile(r"\b(?:vision of|focus(?:es|ed)? on|dedicated to|committed to|speciali[sz](?:es|ing) in|work(?:s|ing)? (?:on|across|with))"
                            r"\s+(.+)$", re.I), False))
    out: list[str] = []
    longer: list[str] = []                       # the same phrase cut only at punctuation ("tools that help oncology researchers ..."): tried after
    for pat, predicate in pats:
        for s in offered:
            s = re.sub(r"\bour\b", "the", " ".join(s.split()).rstrip("."), flags=re.I)
            m = pat.search(s)
            if not m:
                continue
            phrase = _NP_STOP.split(m.group(1), 1)[0].strip()
            whole = _NP_PUNCT.split(m.group(1), 1)[0].strip()
            if not predicate and whole != phrase and 4 <= len(whole.split()) <= 14 and not _FINITE.search(whole) and not _WE.search(whole) \
                    and not _YOU.search(whole):
                longer.append(whole)
            if predicate:
                phrase = phrase.rsplit("'s ", 1)[-1]                          # "the maritime industry's leading X" -> "leading X"
                words = phrase.split()
                while words and words[0].lower() in _ADJ_LEAD:
                    words = words[1:]
                if len(words) > 4 and words[-1].lower() in _HEAD_NOUNS:      # "safety and risk intelligence platform" -> "... intelligence"
                    words = words[:-1]
                phrase = " ".join(words)
            if 3 <= len(phrase.split()) <= 14 and not _FINITE.search(phrase) and not _WE.search(phrase) and not _YOU.search(phrase):
                out.append(phrase)
    return list(dict.fromkeys(out + longer))


def p5_candidates(offered: list[str], company: str) -> list[str]:
    """Fallback P5 sentences, best first: "<Company>'s focus on <a noun phrase of the ad> is why I applied." (v3.4); then an ad sentence's "...
    is to <do X>" or a clause of the ad (no "we" / "our" / "you", no verb of its own: never "What draws me to this role is RightShip is ..."),
    each 6-30 words, in the frame "What draws me to this role is <phrase>."."""
    from . import tailor as T

    vague = [*T.VAGUE_WORDS, *__import__("careerops.tailoring.company_letter", fromlist=["FILLER"]).FILLER]
    out: list[str] = [FALLBACK_NP.format(owner=possessive(company), phrase=p) for p in noun_phrases(offered, company)
                      if not any(v.lower() in p.lower() for v in vague)]
    for s in offered:
        s = re.sub(r"\bour\b", "the", " ".join(s.split()).rstrip("."), flags=re.I)        # "the core mission of our team" -> "... of the team"
        picks = []
        m = re.match(r"^(.*?)\s+(?:is|are)\s+to\s+(.+)$", s)
        if m and len(m.group(1).split()) >= 3:
            picks.append(f"{m.group(1)}: to {m.group(2)}")                    # the ad's own noun phrase first: its words carry the clause the guard reads
        picks += [c.strip() for c in re.split(r"[;:!?]|\s[-–—]\s", s)]
        for phrase in picks:
            phrase = phrase.strip().rstrip(",")
            words = phrase.split()
            if not 6 <= len(words) <= 30 or _WE.search(phrase) or _YOU.search(phrase) or any(v.lower() in phrase.lower() for v in vague):
                continue
            if _FINITE.search(phrase.split(":", 1)[0]) or re.match(re.escape(company) + r"\b", phrase, re.I):
                continue                                                      # "is RightShip is ..." / "is Northwind builds ..." (3 Nov 2026)
            if words[0].lower() in BASE_VERBS:                                 # an ad bullet "Design and build X": "the chance to design and build X"
                phrase = "the chance to " + phrase[0].lower() + phrase[1:]
            if phrase[:1].isupper() and not words[0].isupper() and words[0] != company.split()[0]:
                phrase = phrase[0].lower() + phrase[1:]
            out.append(FALLBACK.format(phrase=phrase))
    return list(dict.fromkeys(out)) or [FALLBACK.format(phrase=f"the work of the {company} team")]


def p5_fallback(offered: list[str], company: str, check=None) -> str:
    """The fallback P5: the first candidate that passes `check` (the P5 guards; None = the first candidate)."""
    cands = p5_candidates(offered, company)
    if check is not None:
        for c in cands:
            if not check(c):
                return c
    return cands[0]


def _ad_run(p5: str, ad: str, n: int = AD_RUN) -> bool:
    from ..tailoring.scorer import norm_literal

    w, a = norm_literal(p5).split(), f" {norm_literal(ad)} "
    return any(f" {' '.join(w[i:i + n])} " in a for i in range(0, max(0, len(w) - n + 1)))


def p5_problems(p5: str, job: dict, fb: dict, kws, plan, team: str, ad_all: str, offered: list[str], fact: dict | None = None) -> list[str]:
    """The letter's own P5 guards (company_letter), plus the run of ad words this mode asks for. v3.5.2: a P5 tied to a fact (P5_TIE) is read in
    two halves: the company guards, the ad run and "no number" on the ad half; `fact_problems` on the half about my work."""
    from . import company_letter as CL
    from . import letters as L

    company, title = job["company"], job["title"]
    skip = frozenset(CL.stems(f"{company} {title} {team}")) | CL.P5_FRAME
    ad, mine = p5_split(p5)
    out = [f"P5: {p}" for p in CL.company_problems(ad, ad_all, skip)] + CL.one_sentence_problems(p5) + CL.extractive_problems(ad, offered, skip)
    allowed = " ".join([ad_all, company, title, team, *CL.bank_names(fb), *CL._labels(fb).values(),
                        *((fact["text"], fact["source"].name) if fact is not None else ())])
    out += CL.name_problems([p5], allowed)
    out += L.claim_problems([ad], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", ad_all)))
    if re.search(r"\d", ad):
        out.append("P5 gives a number")
    if mine and re.search(r"\bI\b", ad):
        out.append("P5: the half in the ad's words speaks about you")
    if mine:
        out += fact_problems(mine, fact, fb) if fact is not None else ["P5: tied to a fact this letter does not hold"]
    from .fluency import grammar_problems, opening_problems

    out += opening_problems(p5, "P5: ")                                      # "I am excited to join ..." (RightShip, 8 Nov 2026)
    out += grammar_problems({"P5": p5})                                      # "I look forward to rapidly prototype ..." (8 Nov 2026)
    if not _ad_run(ad, ad_all):
        out.append(f"P5 holds no run of {AD_RUN} words of the ad")
    return out


def write_p5(llm, job: dict, fb: dict, kws, plan, team: str, ad_all: str, offered: list[str], said: str, notes: list[str],
             fact: dict | list[dict] | None = None) -> tuple[str, dict]:
    """P5 from the model (P5_TRIES tries), else the fallback; (text, info: source, tries, problems, tokens). With `fact` (v3.5.2) the model writes
    only the ad clause and the code ties it to the fact (tie_p5); the fallback ties the ad's own clauses (ad_clauses) the same way, and only when
    none passes is P5 the untied sentence of before (a note says so)."""
    facts = [f for f in (fact if isinstance(fact, list) else [fact]) if f is not None]
    if facts:
        return _write_tied_p5(llm, job, fb, kws, plan, team, ad_all, offered, said, notes, facts)
    from ..llm.router import TaskUnavailable
    from . import tailor as T

    from .fluency import _spent

    info: dict[str, Any] = {"source": "fallback", "tries": 0, "problems": [], "tokens": 0}
    start = len(getattr(llm, "usage", []) or [])
    feedback = f"READER'S NOTE (wording only): {said}" if said else ""
    about = "\n".join(f"[{i}] {x}" for i, x in enumerate(offered, 1)) or "(the ad has no About / role section: use its opening lines)\n" + ad_all[:1200]
    for attempt in range(P5_TRIES):
        user = P5_USER.format(company=job["company"], title=job["title"], about=about, feedback=feedback)
        info["tries"] = attempt + 1
        try:
            out = T._llm_json(llm, TASK, P5_SYSTEM, user, {"mode": "assembled_p5", "company": job["company"], "title": job["title"],
                                                            "about_sentences": offered, "temperature": 0.0 if attempt == 0 else 0.5})
        except TaskUnavailable:
            raise
        except Exception as e:  # noqa: BLE001  (no provider, a bad reply: the fallback P5 still makes a letter)
            info["problems"].append(f"P5 model call failed: {str(e)[:120]}")
            break
        text = str((out or {}).get("p5") or "").strip()
        if not text and isinstance((out or {}).get("paragraphs"), list) and out["paragraphs"]:
            text = str(out["paragraphs"][-1]).strip()
        info["tokens"] += (len(P5_SYSTEM) + len(user) + len(text)) // 4
        text = " ".join(text.split())
        probs = p5_problems(text, job, fb, kws, plan, team, ad_all, offered) if text else ["P5: empty reply"]
        if not probs:
            info.update(source="model", problems=[], tokens=_spent(llm, start, info["tokens"]))       # the router's own count when it has one
            return text, info
        info["problems"].append(probs)
        feedback = (f"READER'S NOTE (wording only): {said}\n" if said else "") + "Your last sentence was rejected: " + "; ".join(probs)[:400]
    info["tokens"] = _spent(llm, start, info["tokens"])
    fb_text = p5_fallback(offered, job["company"], lambda t: p5_problems(t, job, fb, kws, plan, team, ad_all, offered))
    notes.append(f"cover_letter: P5 is the fallback sentence ({info['tries']} model tr{'y' if info['tries'] == 1 else 'ies'} rejected or failed)")
    return fb_text, info


def _write_tied_p5(llm, job: dict, fb: dict, kws, plan, team: str, ad_all: str, offered: list[str], said: str, notes: list[str],
                   facts: list[dict]) -> tuple[str, dict]:
    """The model's ad clause tied to the first fact; else each of the ad's own clauses tied to each fact in turn; else the untied P5 of before.
    info["fact"] = the bullet id P5 is tied to ("" when untied); info["fallback_problems"] = why the first tied candidate was refused."""
    from ..llm.router import TaskUnavailable
    from . import tailor as T

    from .fluency import _spent

    fact = facts[0]
    clause = fact_clause(fact["text"])
    info: dict[str, Any] = {"source": "fallback", "tries": 0, "problems": [], "tokens": 0, "fact": fact["id"]}
    start = len(getattr(llm, "usage", []) or [])
    feedback = f"READER'S NOTE (wording only): {said}" if said else ""
    about = "\n".join(f"[{i}] {x}" for i, x in enumerate(offered, 1)) or "(the ad has no About / role section: use its opening lines)\n" + ad_all[:1200]

    def problems_of(p5: str, fact: dict = fact) -> list[str]:
        return p5_problems(p5, job, fb, kws, plan, team, ad_all, offered, fact)

    for attempt in range(P5_TRIES):
        user = P5_USER.format(company=job["company"], title=job["title"], about=about, feedback=feedback)
        info["tries"] = attempt + 1
        try:
            out = T._llm_json(llm, TASK, P5_CLAUSE_SYSTEM, user, {"mode": "assembled_p5", "company": job["company"], "title": job["title"],
                                                                   "about_sentences": offered, "temperature": 0.0 if attempt == 0 else 0.5})
        except TaskUnavailable:
            raise
        except Exception as e:  # noqa: BLE001  (no provider, a bad reply: the fallback P5 still makes a letter)
            info["problems"].append(f"P5 model call failed: {str(e)[:120]}")
            break
        text = " ".join(str((out or {}).get("p5") or "").split()).rstrip(".")
        info["tokens"] += (len(P5_CLAUSE_SYSTEM) + len(user) + len(text)) // 4
        p5 = tie_p5(text, fact["source"], clause) if text else ""
        probs = problems_of(p5) if text else ["P5: empty reply"]
        if not probs:
            info.update(source="model", problems=[], tokens=_spent(llm, start, info["tokens"]))
            return p5, info
        info["problems"].append(probs)
        feedback = (f"READER'S NOTE (wording only): {said}\n" if said else "") + "Your last clause was rejected: " + "; ".join(probs)[:400]
    info["tokens"] = _spent(llm, start, info["tokens"])
    for f in facts:
        for ad in ad_clauses(offered, job["company"]):
            p5 = tie_p5(ad, f["source"], fact_clause(f["text"]))
            probs = problems_of(p5, f)
            if not probs:
                info["fact"] = f["id"]
                notes.append(f"cover_letter: P5 is the ad's own clause tied to {f['id']} ({info['tries']} model tr{'y' if info['tries'] == 1 else 'ies'} "
                             "rejected or failed)")
                return p5, info
            info.setdefault("fallback_problems", probs)
    info["fact"] = ""
    notes.append(f"cover_letter: P5 could not be tied to a fact of this CV (no ad clause passed its guards); it is the untied sentence")
    return p5_fallback(offered, job["company"], lambda t: p5_problems(t, job, fb, kws, plan, team, ad_all, offered)), info


def body(plan, g3: list[dict], g4: list[dict], reqs: list[str], fb: dict, chosen: set[str] | None, st: Steer, settings,
         code_parts: dict[str, str], p5: str = "", moved: set[str] | frozenset[str] = frozenset(), kws=None) -> tuple[dict[str, str], set[str], list[str], list[dict]]:
    """(P3 and P4 text, the sentences assembled from bullets, notes, P3's bullets) with the length rule applied to the whole letter (`p5` included).
    `moved`: ids of P3 bullets that P4 left out (split_shared): P3 tells them with their employer ("At <employer>, I ...")."""
    from . import company_letter as CL

    from .faithfulness import numbers

    numbered = [e for e in g4 if numbers(e["text"])]
    if numbered and len(numbered) < len(g4):
        notes_pre = [f"cover_letter: P4 leaves out {', '.join(CL.employer(e) for e in g4 if e not in numbered)} (no bullet with a number on this CV)"]
        g4 = numbered
    else:
        notes_pre = []
    p3 = [at_role(e) if e["id"] in moved else first_person(e["text"]) for e in g3]
    p4 = [at_role(e) for e in g4]
    assembled = {s for s in p3 + p4}
    notes: list[str] = list(notes_pre)
    from . import fluency as FL

    lo, _ = CL.word_range(settings)
    cap = CL.word_cap(settings)
    goal = min(cap, lo + FL.GROW_MARGIN) if FL.enabled(settings) else lo        # room for the fluency rewrite to tighten the wording
    used = {e["id"] for e in g3 + g4} | ({plan.opening.bullet_id} if plan.opening is not None and plan.opening.bullet_id else set())

    def total(extra: list[str]) -> int:
        terms = _terms_sentence(g3, reqs, " ".join(extra))
        return CL.words([*code_parts.values(), " ".join(extra + ([terms] if terms else [])), " ".join(p4), p5])

    grown = 0
    from .letter_rules import term_dump_problems

    for b in _numbered_unused(fb, chosen, used, st.avoid, kws):
        if total(p3) >= goal or grown >= GROW_MAX:
            break
        if term_dump_problems([first_person(b["text"])], fb):                 # a bullet the letter guard reads as a list of terms: never added
            continue
        p3.append(first_person(b["text"]))
        assembled.add(p3[-1])
        g3 = g3 + [b]
        used.add(b["id"])
        grown += 1
        notes.append(f"cover_letter: P3 grew by bullet {b['id']} (under {goal} words)")
    terms = _terms_sentence(g3, reqs, " ".join(p3))
    parts = {"P3": " ".join(p3 + ([terms] if terms else [])), "P4": " ".join(p4)}
    if total(p3) > cap or st.shorter:
        if "P2" in code_parts:
            notes.append("cover_letter: P2 left out (" + ("you asked for shorter" if st.shorter else f"over {cap} words") + ")")
        if (total(p3) - (CL.words([code_parts.get("P2", "")]) if "P2" in code_parts else 0) > cap or st.shorter) and len(p3) > 1:
            p3 = p3[:1]
            terms = _terms_sentence(g3[:1], reqs, p3[0])
            parts["P3"] = " ".join(p3 + ([terms] if terms else []))
            notes.append("cover_letter: P3 kept to one bullet")
        parts["drop_p2"] = "1"
    parts["_g4"] = g4                                                           # type: ignore[assignment]  (the roles P4 kept)
    return parts, assembled, notes, g3[:len(p3)] if len(p3) < len(g3) else g3


# ====================================================================================================================================
# v3.5.2 (10 Nov 2026, after the 9 Nov RightShip letter): one paragraph per employer or project, every sentence names where the work was done,
# at most MAX_CLAIMS bullets told per paragraph, the plain-language jargon rule on the bullets' wording, and P5 ties the ad's own sentence to one
# evidence fact of this CV.
# ====================================================================================================================================
MAX_CLAIMS = 2                                   # bullets told in one paragraph (the "That work used X and Y." sentence counts as one)
P5_TIE = " — the same aim as "                   # P5 = <the ad's own words> + P5_TIE + <my work at X>, where <one fact of this CV>.
P5_FACT_MAX_WORDS = 16                           # the fact clause of P5
P5_WORDS_GUESS = 38                              # P5's length while P3 and P4 are sized (it is written after them)
# Names a letter reader never needs (gene and data standards, file formats, internal component names): a bullet that still says one after its
# brackets are taken out is not told, unless the ad names it (10 Nov 2026: "no Ensembl, HGNC, parquet unless the JD names them").
LETTER_NEVER = ("parquet", "hgnc", "ensembl", "leiden", "modularity", "identity-spine", "crosswalk", "crosswalks", "circuit breaker",
                "circuit breakers", "float16", "barge-in", "full-duplex", "silhouette", "skip-gram", "websocket", "json", "yaml", "csv", "regex",
                "cellosaurus", "lambda web adapter", "ecr", "iam", "least-privilege")
_NOT_A_VERB = frozenset("to and or for with in on as at by from of is's that which who".split())
_CUSTOMER = re.compile(r"\b(?:customers?|clients?|users?|act on|decisions?|patients?|researchers?|organi[sz]ations?)\b", re.I)


@dataclasses.dataclass(frozen=True)
class Source:
    """Where a bullet's work was done: an experience entry or a project, its short name and the phrases a letter sentence names it with."""
    id: str
    kind: str                     # experience | project
    name: str                     # "AstraZeneca", "CoverFit"
    phrase: str                   # "At 180 Degrees Consulting", "On my AstraZeneca dissertation project", "On my CoverFit hackathon project"
    mine: str                     # "my work at 180 Degrees Consulting", "my AstraZeneca dissertation project", "my CoverFit hackathon project"


def short_name(name: str) -> str:
    """'CoverFit — insurance benchmarking API (Bristol × UWE AI Hackathon)' -> 'CoverFit'; 'AstraZeneca, United Kingdom' -> 'AstraZeneca'."""
    n = " ".join(str(name or "").split())
    return re.split(r"\s+[—–-]{1,2}\s+|\s*\(|\s*,\s*", n, maxsplit=1)[0].strip()


def source_of(fb: dict, bullet_id: str) -> Source | None:
    """The Source of a bullet. The context word the fact bank gives the entry (doccheck.marks: dissertation, coursework, volunteer, hackathon)
    is said in the phrase: the letter never drops that context (10 Nov 2026)."""
    from .doccheck import marks

    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            if any(str(b.get("id")) == bullet_id for b in e.get("bullets") or []):
                word = (marks(e) or [("", "", "")])[0][0]
                if sec == "experience":
                    from .render import is_freelance

                    name = short_name(e.get("org") or e.get("display_org") or e.get("id"))
                    if is_freelance(e):                                     # "At Freelance, I ..." is not English
                        return Source(str(e.get("id")), "experience", "freelance", "In my freelance work", "my freelance work")
                    if word == "volunteer":
                        phrase, mine = f"As a volunteer at {name}", f"my volunteer work at {name}"
                    elif word:                                       # dissertation, hackathon, coursework: "On my AstraZeneca dissertation project"
                        phrase, mine = f"On my {name} {word} project", f"my {name} {word} project"
                    else:
                        phrase, mine = f"At {name}", f"my work at {name}"
                    return Source(str(e.get("id")), "experience", name, phrase, mine)
                name = short_name(e.get("name") or e.get("display_name") or e.get("id"))
                what = f"{name} {word} project" if word else f"{name} project"
                return Source(str(e.get("id")), "project", name, f"On my {what}", f"my {what}")
    return None


def strip_brackets(text: str) -> str:
    """The bullet without its bracketed asides ("(LangChain, Claude Haiku 4.5 on AWS Bedrock)"): words are only taken away, never added."""
    t = " ".join(str(text or "").split())
    while True:
        new = re.sub(r"\s*\([^()]*\)", "", t)
        if new == t:
            break
        t = new
    return re.sub(r"\s+([,.;:])", r"\1", t).strip()


def letter_text(bullet: dict, fb: dict, plain_rule: bool, allow: list[str]) -> tuple[str, list[str]]:
    """(the wording the letter tells a bullet with, the jargon that keeps it out). Plain style: the bullet's approved `plain:` when it has a usable
    one, else the bullet without its bracketed asides; it is left out when it still holds a LETTER_NEVER term (Ensembl, HGNC, parquet ...) or
    more than plain.MAX_JARGON (1) technical names off the allow-list, as a plain CV bullet may (the allow-list holds the ad's own keywords, so a
    term the ad names stays).
    Technical style: the bullet word for word."""
    from . import plain as PL

    if not plain_rule:
        return bullet["text"], []
    approved = PL.usable_plain(bullet)
    text = approved or strip_brackets(bullet["text"])
    owner = next((e for sec in ("experience", "projects") for e in fb.get(sec) or []
                  if any(str(b.get("id")) == bullet["id"] for b in e.get("bullets") or [])), None)
    js = PL.jargon(text, fb, owner, allow)
    never = {norm(x) for x in LETTER_NEVER}
    if any(norm(j) in never for j in js) or len(js) > PL.MAX_JARGON:
        return text, js
    return text, []


def norm(term: str) -> str:
    from .scorer import norm_literal

    return norm_literal(term)


def told(text: str, src: Source, second: bool = False, proper: frozenset[str] = frozenset()) -> str:
    """One letter sentence: the place, then the bullet in first person ("At AstraZeneca, I built ..."; the second of a paragraph "..., I also ...")."""
    body = first_person(text)
    if body.startswith("I "):
        return f"{src.phrase}, I {'also ' if second else ''}{body[2:]}"
    if body.startswith(("To deliver ", "For ")):
        return f"{src.phrase}, {body[0].lower()}{body[1:]}"
    first = body.split(" ", 1)[0].strip(",;:")
    if first[:1].isupper() and first[1:] == first[1:].lower() and first.lower() not in proper:
        return f"{src.phrase}, {body[0].lower()}{body[1:]}"
    return f"{src.phrase}: {body}"


def _proper(fb: dict) -> frozenset[str]:
    from . import letters as L

    words = {w for t in L._tools(fb) for w in t.split()}
    for sec in ("experience", "projects"):
        for e in fb.get(sec) or []:
            words |= {w.lower() for w in re.findall(r"[A-Za-z][\w'-]*", f"{e.get('org') or ''} {e.get('name') or ''}")}
    return frozenset(words)


def fact_clause(text: str) -> str:
    """P5's fact: the bullet's first-person action, cut at its first clause boundary after five words ("I wrote a three-tier test framework of
    ~65 tests across 8 files"); words are only cut away. "" when the wording has no "I <verb>" to start from or is longer than P5_FACT_MAX_WORDS."""
    body = first_person(text).rstrip(".")
    at = body.find("I ") if body.startswith("I ") else body.find(", I ")
    if at < 0:
        m = re.search(r"\bso I ", body)
        at = m.start() + 3 if m else -1
    elif not body.startswith("I "):
        at += 2
    if at < 0:
        return ""
    clause = body[at:]
    words = clause.split()
    for i in range(5, len(words)):
        if re.search(r"[,;:—]$", words[i - 1]) or words[i] in ("and", "then", "while", "so"):
            clause = " ".join(words[:i]).rstrip(",;:—")
            break
    return clause if 4 <= len(clause.split()) <= P5_FACT_MAX_WORDS else ""


def p5_split(p5: str) -> tuple[str, str]:
    """(the part in the ad's own words, the part about my work) of a v3.5.2 P5; ("whole P5", "") for an older one."""
    if P5_TIE in p5:
        ad, mine = p5.split(P5_TIE, 1)
        return ad.strip(), mine.strip().rstrip(".")
    return p5, ""


def tie_p5(ad_part: str, src: Source, fact: str) -> str:
    return f"{ad_part.strip().rstrip('.')}{P5_TIE}{src.mine}, where {fact}."


def ad_clauses(offered: list[str], company: str) -> list[str]:
    """The ad's own words for P5's first half, best first: a clause of an About sentence that starts with the company's name ("RightShip transforms
    complex safety and operational risk into intelligence its customers can act on with confidence"; "our" -> "its"), those about what the
    company's customers get first; then "<Company>'s focus on <a noun phrase of the ad>"."""
    out: list[str] = []
    for s in sorted(offered, key=lambda s: 0 if _CUSTOMER.search(s) else 1):
        s = " ".join(s.split()).rstrip(".")
        m = re.search(rf"\b{re.escape(company)}\b(?!['’]s)\s+(\w+)", s) if company.strip() else None
        if not m or m.group(1).lower() in _NOT_A_VERB:                    # "rely on RightShip to strengthen ...": the company is not the subject
            continue
        clause = re.sub(r"\bour\b", "its", s[m.start():], flags=re.I)
        clause = re.split(r"\s*;\s*|\.\s", clause, maxsplit=1)[0].strip().rstrip(",")
        if 6 <= len(clause.split()) <= 26 and not _WE.search(clause) and not _YOU.search(clause):
            out.append(clause)
    out += [f"{possessive(company)} focus on {p}" for p in noun_phrases(offered, company)]
    return list(dict.fromkeys(out))


def fact_problems(fact: str, evidence: dict, fb: dict) -> list[str]:
    """The P5 fact part says nothing its bullet (as the letter tells it) does not: numbers and tools from it only, and most of its words."""
    from . import letters as L
    from .faithfulness import numbers
    from .scorer import norm_literal

    out: list[str] = []
    extra = numbers(fact) - numbers(evidence["text"])
    if extra:
        out.append(f"P5: number(s) about you not in its fact ({evidence['id']}): {sorted(extra)}")
    lit, src = norm_literal(fact), norm_literal(evidence["text"])
    for tool in L._tools(fb):
        if len(tool) >= 3 and re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", lit) and not re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", src):
            out.append(f"P5: names '{tool}', which its fact ({evidence['id']}) does not")
    return out


@dataclasses.dataclass
class Built:
    parts: dict[str, str]
    g3: list[dict]
    g4: list[dict]
    reqs: list[str]
    assembled: set[str]
    notes: list[str]
    fact: dict | None                      # P5's evidence fact (a letter evidence dict), None when no fact fits
    left_out: dict[str, list[str]]         # bullet id -> the jargon that kept it out of the letter
    g5: list[dict] = dataclasses.field(default_factory=list)      # P4b's bullets (a third place), [] when the letter needs none
    facts: list[dict] = dataclasses.field(default_factory=list)   # P5's fact candidates, best first (`fact` is the first)


def compose(plan, fb: dict, kws, chosen: set[str] | None, jd: str, hk, st: Steer, settings, code_parts: dict[str, str], p5_len: int,
            plain_rule: bool, allow: list[str], avoid: set[str] = frozenset()) -> Built | None:
    """P3 and P4, one paragraph per place: P3 the place whose bullets on this CV carry most of the ad's skills (company_letter.skill_group), P4 the
    next place (an employer first, the hook's role first among them). Each paragraph tells one bullet, then grows to MAX_CLAIMS while the letter is
    under its length goal; every sentence opens with where the work was done. Over the cap: P2 goes, then each paragraph keeps one bullet. None when
    fewer than two places have a usable bullet."""
    from . import company_letter as CL
    from . import fluency as FL
    from . import tailor as T
    from .faithfulness import numbers
    from .letter_rules import term_dump_problems
    from .scorer import keyword_hits

    skip = {x for x in (getattr(plan.opening, "bullet_id", "") if plan.opening is not None else "",
                        getattr(hk, "proof_bullet_id", "") if hk is not None else "") if x} | {a.lower() for a in st.avoid} | set(avoid)
    ranked = T._with_orgs(fb, T.ranked_letter_bullets(fb, kws, set(chosen) if chosen else None, jd))
    ranked = [e for e in ranked if e["id"].lower() in st.prefer] + [e for e in ranked if e["id"].lower() not in st.prefer]
    proper = _proper(fb)
    usable: list[dict] = []
    left_out: dict[str, list[str]] = {}
    for e in ranked:
        if e["id"] in skip or e["id"].lower() in skip:
            continue
        src = source_of(fb, e["id"])
        if src is None:
            continue
        text, js = letter_text(e, fb, plain_rule, allow)
        if js:
            left_out[e["id"]] = js
            continue
        if term_dump_problems([told(text, src)], fb):                  # a bullet the letter guard reads as a list of terms is never told
            left_out[e["id"]] = ["a list of terms, not a sentence"]
            continue
        usable.append({**e, "text": text, "orig": e["text"], "org": src.name, "where": src.name, "source": src})
    places = list(dict.fromkeys(u["source"].id for u in usable))
    if len(places) < 2:
        return None
    by_id = {u["id"]: u for u in usable}
    skills, g = CL.skill_group(fb, kws, set(by_id), jd, set())
    first = next((u for u in usable if u["id"].lower() in st.prefer), None) or (by_id.get(g[0]["id"]) if g else None) or usable[0]
    a = first["source"]
    def long(u: dict) -> bool:                                           # a letter sentence over the fluency pass's limit (35 words) goes last
        return len(told(u["text"], u["source"]).split()) > FL.MAX_SENTENCE_WORDS

    a_items = [first] + [by_id[x["id"]] for x in g if x["id"] in by_id and by_id[x["id"]]["source"] == a and x["id"] != first["id"]]
    a_items = a_items[:1] + sorted(a_items[1:] + [u for u in usable if u["source"] == a and u not in a_items], key=lambda u: (long(u), usable.index(u)))
    others = [u for u in usable if u["source"] != a]
    role = getattr(hk, "relevant_role_id", "") if hk is not None else ""
    order = sorted(others, key=lambda u: (u["source"].kind != "experience", long(u), u["source"].id != role, not numbers(u["text"]), others.index(u)))
    b = order[0]["source"]
    b_items = sorted([u for u in usable if u["source"] == b], key=lambda u: (long(u), not numbers(u["text"]), usable.index(u)))
    notes: list[str] = []

    def reqs_of(items: list[dict]) -> list[str]:
        have: set[int] = set()
        for u in items:
            have |= set(keyword_hits(" ".join([u["text"], *u.get("terms", [])]), kws))
        return [kws[i].term for i in sorted(have, key=lambda i: (-kws[i].weight, i))][:3]

    def paragraph(items: list[dict], with_terms: bool) -> tuple[str, list[str], list[str]]:
        sents = [told(u["text"], u["source"], i > 0, proper) for i, u in enumerate(items)]
        reqs = reqs_of(items) if with_terms else []
        if with_terms and len(items) < MAX_CLAIMS:
            ts = _terms_sentence(items, reqs, " ".join(sents))
            if ts:
                sents.append(ts.replace("That work used", f"That {items[0]['source'].name} work used", 1))
        return " ".join(sents), sents, reqs

    lo, _ = CL.word_range(settings)
    cap = CL.word_cap(settings)
    goal = min(cap, lo + FL.GROW_MARGIN) if FL.enabled(settings) else lo
    n3, n4 = 1, 1

    def build(n3: int, n4: int) -> tuple[dict[str, str], set[str], list[str]]:
        p3, s3, reqs = paragraph(a_items[:n3], True)
        p4, s4, _ = paragraph(b_items[:n4], False)
        return {"P3": p3, "P4": p4}, set(s3 + s4), reqs

    def p3_names_its_skills(trial) -> bool:
        need = min(2, len(trial[2]))
        return len({r.lower() for r in trial[2]} & {kws[i].term.lower() for i in keyword_hits(trial[0]["P3"], kws)}) >= need

    def total(parts: dict[str, str], parts_code: dict[str, str]) -> int:
        return CL.words([*parts_code.values(), parts["P3"], parts["P4"], parts.get("P4b", "")]) + p5_len

    parts_code = dict(code_parts)
    parts, assembled, reqs = build(n3, n4)
    for which in ("a", "b"):
        if total(parts, parts_code) >= goal or st.shorter:
            break
        if which == "a" and len(a_items) > 1:
            trial = build(2, n4)
            if p3_names_its_skills(trial):                                   # the ad skills P3 must name are still named without the terms line
                n3, (parts, assembled, reqs) = 2, trial
                notes.append(f"cover_letter: P3 tells a second {a.name} bullet ({a_items[1]['id']}; under {goal} words)")
        if which == "b" and len(b_items) > 1:
            n4 = 2
            parts, assembled, reqs = build(n3, n4)
            notes.append(f"cover_letter: P4 tells a second {b.name} bullet ({b_items[1]['id']}; under {goal} words)")
    c_items: list[dict] = []
    if total(parts, parts_code) < goal and not st.shorter and not ("P2" in parts_code and "P6" in parts_code):
        # one paragraph per place: a third place gets its own paragraph (P4b) rather than a third bullet in P3 or P4 (at most seven paragraphs)
        third = [p for p in dict.fromkeys(u["source"].id for u in order) if p not in (a.id, b_items[0]["source"].id)]
        if third:
            c_items = sorted([u for u in usable if u["source"].id == third[0]], key=lambda u: (long(u), not numbers(u["text"]), usable.index(u)))[:MAX_CLAIMS]
            while c_items and total({**parts, "P4b": paragraph(c_items, False)[0]}, parts_code) > cap:
                c_items = c_items[:-1]
            if c_items:
                p4b, s5, _ = paragraph(c_items, False)
                parts = {**parts, "P4b": p4b}
                assembled = set(assembled) | set(s5)
                notes.append(f"cover_letter: under {goal} words, so a third place has its own paragraph (P4b): {c_items[0]['source'].name} "
                             f"({', '.join(u['id'] for u in c_items)})")
    if total(parts, parts_code) < lo and not st.shorter:
        # still under the floor: each paragraph's two longest bullets (their sentences may then pass 35 words), before the letter is given up
        keep = (a_items, b_items, n3, n4, parts, assembled, reqs)
        a_items, b_items = sorted(a_items, key=lambda u: -len(u["text"])), sorted(b_items, key=lambda u: -len(u["text"]))
        n3, n4 = min(2, len(a_items)), min(2, len(b_items))
        trial = build(n3, n4)
        if n3 == 2 and not p3_names_its_skills(trial):
            n3 = 1
            trial = build(n3, n4)
        trial_parts = {**trial[0], **({"P4b": parts["P4b"]} if parts.get("P4b") else {})}
        if total(trial_parts, parts_code) >= lo:
            parts, assembled, reqs = trial_parts, set(trial[1]) | (set(paragraph(c_items, False)[1]) if c_items else set()), trial[2]
            notes.append(f"cover_letter: under {lo} words, so P3 tells {', '.join(u['id'] for u in a_items[:n3])} and P4 "
                         f"{', '.join(u['id'] for u in b_items[:n4])}, the longest bullets of their places")
        else:
            a_items, b_items, n3, n4, parts, assembled, reqs = keep
    drop_p2 = False
    if total(parts, parts_code) > cap or st.shorter:
        if "P2" in parts_code:
            parts_code.pop("P2")
            drop_p2 = True
            notes.append("cover_letter: P2 left out (" + ("you asked for shorter" if st.shorter else f"over {cap} words") + ")")
        if total(parts, parts_code) > cap or st.shorter:
            n3, n4 = 1, 1
            p4b_now = parts.get("P4b")
            parts, assembled, reqs = build(n3, n4)
            if p4b_now and total({**parts, "P4b": p4b_now}, parts_code) <= cap and not st.shorter:
                parts = {**parts, "P4b": p4b_now}
            else:
                c_items = []
            notes.append("cover_letter: P3 and P4 kept to one bullet each")
    if left_out:
        notes.append("cover_letter: left out of the letter for jargon (plain style): " + "; ".join(f"{k} ({', '.join(v)})" for k, v in left_out.items()))
    used = {u["id"] for u in a_items[:n3] + b_items[:n4] + c_items}
    facts = [u for u in [*a_items, *b_items] if u["id"] not in used and fact_clause(u["text"])] + \
        [u for u in [*a_items[:n3], *b_items[:n4], *c_items] if fact_clause(u["text"])]
    facts = list({u["id"]: u for u in reversed(facts)}.values())[::-1]                  # each once, in order
    fact = facts[0] if facts else None
    if drop_p2:
        parts["drop_p2"] = "1"
    return Built(parts, a_items[:n3], b_items[:n4], reqs, assembled, notes, fact, left_out, c_items, facts)


def p5_with_fact(ad_candidates: list[str], fact: dict | None, check) -> tuple[str, str]:
    """(P5, its source): the first ad clause that, tied to the fact (`tie_p5`), passes `check` (the P5 guards); "" when none does."""
    if fact is None:
        return "", ""
    clause = fact_clause(fact["text"])
    for ad in ad_candidates:
        p5 = tie_p5(ad, fact["source"], clause)
        if not check(p5):
            return p5, fact["id"]
    return "", ""


def place_problems(parts: dict, g3: list[dict], g4: list[dict], g5: list[dict] | None = None) -> list[str]:
    """Every sentence of P3 names P3's place and every sentence of P4 names P4's (the fluency rewrite may not lose it); a paragraph tells one place."""
    from .grounding import sentences as _sentences

    out: list[str] = []
    for name, group in (("P3", g3), ("P4", g4), ("P4b", g5 or [])):
        src = group[0].get("source") if group else None
        if src is None:
            continue
        for s in _sentences(str(parts.get(name) or "")):
            if src.name.lower() not in s.lower():
                out.append(f"{name}: a sentence does not say where the work was done ({src.name}): '{s.strip()[:60]}'")
    return out
