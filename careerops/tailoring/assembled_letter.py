"""The assembled company letter (v2.5, `letter.compose: assembled`, the code default): a letter that always exists.

Free drafts failed because every sentence that makes a claim about you is a chance to fail a guard: with k ≈ 12 claim sentences and a pass rate of
p ≈ 0.92 per sentence, P(pass) = p^k ≈ 0.37, and three retries near temperature 0 are almost the same draft. Here the sentences about you are
ASSEMBLED from verified text and the model writes only the sentence about the company:

  P1  the plan's opening (opening.py, code): the ad's problem phrase, a semicolon, the hook's letter line (verbatim, verified).
  P2  the hook fact's `limits` / `result` (code; left out when there is neither).
  P3  the one or two bullets of this CV that evidence the ad's required skills (skill_group), each turned into first person by ONE fixed rule:
      a bullet that opens with a past-tense verb gets "I " before it ("Built X" -> "I built X"); any other wording stays word for word. When those
      bullets do not say two of the ad skills they evidence, one sentence names them from the bullets' own `terms`: "That work used X and Y."
  P4  one sentence per role (role_group: the most relevant roles), "At <employer>, " + that role's bullet on this CV that holds a number, first person.
  P5  the model: ONE sentence on why this team, holding a run of at least 4 words of the ad; two tries, each checked by the letter's own P5 guards;
      then the fallback "What draws me to this role is <a clause of the ad>."
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
AD_RUN = 4
PAST_IRREGULAR = frozenset("""built led ran wrote made set drove took cut grew won taught brought found gave kept held sold spent shipped rebuilt broke chose
got put read saw sent told thought understood began became did drew fed hid hung knew lit met paid rode rose shot showed sang sat slept spoke stood stole
swam threw wore woke""".split())
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")
_WE = re.compile(r"\b(?:we|our|ours|us|we're|we've|we'll)\b", re.I)

P5_SYSTEM = """You write ONE sentence (at most 40 words) for a cover letter: why this team and role appeal, in the first person.
It must contain, word for word, a run of at least 4 consecutive words copied from one of the AD SENTENCES. Make no claim about the candidate's
experience or skills, give no number, never write "we", "our" or "us", invent no name. Return JSON only: {"p5": "<the sentence>"}."""
P5_USER = """COMPANY: {company}
ROLE: {title}
AD SENTENCES:
{about}
{feedback}"""


def compose_mode(settings) -> str:
    raw = getattr(settings, "raw", settings) or {}
    v = str(((raw.get("letter") or {}) if isinstance(raw, dict) else {}).get("compose") or "assembled").strip().lower()
    return v if v in ("assembled", "free") else "assembled"


def first_person(text: str) -> str:
    """The fixed rule: a bullet that opens with a past-tense verb gets "I " ("Built X" -> "I built X"); anything else stays word for word."""
    t = _BULLET.sub("", " ".join(str(text or "").split())).strip()
    if not t:
        return ""
    head, _, rest = t.partition(" ")
    word = head.strip(",;:")
    low = word.lower()
    verb = word[:1].isupper() and not word.isupper() and (low in PAST_IRREGULAR or re.fullmatch(r"[a-z-]+ed", low) is not None)
    out = f"I {low}{head[len(word):]} {rest}".strip() if verb else t
    return out if out.endswith((".", "!", "?")) else out + "."


def at_role(e: dict) -> str:
    """P4's sentence for one role: "At <employer>, " + the bullet in first person."""
    from .company_letter import employer

    who = employer(e)
    body = first_person(e["text"])
    if body.startswith("I "):
        return f"At {who}, {body}" if who else body
    return f"At {who}: {body}" if who else body


def _terms_sentence(group: list[dict], reqs: list[str], said: str) -> str:
    """'That work used X and Y.' with the ad skills these bullets evidence (their own terms) that the sentences do not say yet."""
    from ..tailoring.scorer import norm_literal

    lit = norm_literal(said)
    have = {t.lower() for e in group for t in e.get("terms", [])}
    missing = [r for r in reqs if r.lower() in have and norm_literal(r) not in lit]
    named = [r for r in reqs if norm_literal(r) in lit]
    need = max(0, min(2, len(reqs)) - len(named))
    pick = missing[:need]
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


def _numbered_unused(fb: dict, chosen: set[str] | None, used: set[str], avoid: set[str]) -> list[dict]:
    """The bullets of this CV not used yet: the numbered ones first (the length rule's "next-ranked numbered bullet"), then the rest."""
    from . import tailor as T

    out = []
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                if (chosen is None or b["id"] in chosen) and b["id"] not in used and b["id"].lower() not in avoid:
                    out.append(b)
    out.sort(key=lambda b: 0 if re.search(r"\d", b["text"]) else 1)
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


def p5_candidates(offered: list[str], company: str) -> list[str]:
    """Fallback P5 sentences, best first: an ad sentence's "you will <do X>" or "... is to <do X>" as "the chance to <do X>", else a clause of the ad
    (no "we" / "our" / "you"), each 6-30 words, in the fixed frame "What draws me to this role is <phrase>."."""
    from . import tailor as T

    vague = [*T.VAGUE_WORDS, *__import__("careerops.tailoring.company_letter", fromlist=["FILLER"]).FILLER]
    out: list[str] = []
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


def p5_problems(p5: str, job: dict, fb: dict, kws, plan, team: str, ad_all: str, offered: list[str]) -> list[str]:
    """The letter's own P5 guards (company_letter), plus the run of ad words this mode asks for."""
    from . import company_letter as CL
    from . import letters as L

    company, title = job["company"], job["title"]
    skip = frozenset(CL.stems(f"{company} {title} {team}"))
    out = [f"P5: {p}" for p in CL.company_problems(p5, ad_all, skip)] + CL.one_sentence_problems(p5) + CL.extractive_problems(p5, offered, skip)
    allowed = " ".join([ad_all, company, title, team, *CL.bank_names(fb), *CL._labels(fb).values()])
    out += CL.name_problems([p5], allowed)
    out += L.claim_problems([p5], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", ad_all)))
    if re.search(r"\d", p5):
        out.append("P5 gives a number")
    if not _ad_run(p5, ad_all):
        out.append(f"P5 holds no run of {AD_RUN} words of the ad")
    return out


def write_p5(llm, job: dict, fb: dict, kws, plan, team: str, ad_all: str, offered: list[str], said: str, notes: list[str]) -> tuple[str, dict]:
    """P5 from the model (P5_TRIES tries), else the fallback; (text, info: source, tries, problems, tokens)."""
    from ..llm.router import TaskUnavailable
    from . import tailor as T

    info: dict[str, Any] = {"source": "fallback", "tries": 0, "problems": [], "tokens": 0}
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
            info.update(source="model", problems=[])
            return text, info
        info["problems"].append(probs)
        feedback = (f"READER'S NOTE (wording only): {said}\n" if said else "") + "Your last sentence was rejected: " + "; ".join(probs)[:400]
    fb_text = p5_fallback(offered, job["company"], lambda t: p5_problems(t, job, fb, kws, plan, team, ad_all, offered))
    notes.append(f"cover_letter: P5 is the fallback sentence ({info['tries']} model tr{'y' if info['tries'] == 1 else 'ies'} rejected or failed)")
    return fb_text, info


def body(plan, g3: list[dict], g4: list[dict], reqs: list[str], fb: dict, chosen: set[str] | None, st: Steer, settings,
         code_parts: dict[str, str], p5: str = "") -> tuple[dict[str, str], set[str], list[str], list[dict]]:
    """(P3 and P4 text, the sentences assembled from bullets, notes, P3's bullets) with the length rule applied to the whole letter (`p5` included)."""
    from . import company_letter as CL

    from .faithfulness import numbers

    numbered = [e for e in g4 if numbers(e["text"])]
    if numbered and len(numbered) < len(g4):
        notes_pre = [f"cover_letter: P4 leaves out {', '.join(CL.employer(e) for e in g4 if e not in numbered)} (no bullet with a number on this CV)"]
        g4 = numbered
    else:
        notes_pre = []
    p3 = [first_person(e["text"]) for e in g3]
    p4 = [at_role(e) for e in g4]
    assembled = {s for s in p3 + p4}
    notes: list[str] = list(notes_pre)
    lo, _ = CL.word_range(settings)
    cap = CL.word_cap(settings)
    used = {e["id"] for e in g3 + g4} | ({plan.opening.bullet_id} if plan.opening is not None and plan.opening.bullet_id else set())

    def total(extra: list[str]) -> int:
        terms = _terms_sentence(g3, reqs, " ".join(extra))
        return CL.words([*code_parts.values(), " ".join(extra + ([terms] if terms else [])), " ".join(p4), p5])

    grown = 0
    for b in _numbered_unused(fb, chosen, used, st.avoid):
        if total(p3) >= lo or grown >= GROW_MAX:
            break
        p3.append(first_person(b["text"]))
        assembled.add(p3[-1])
        g3 = g3 + [b]
        used.add(b["id"])
        grown += 1
        notes.append(f"cover_letter: P3 grew by bullet {b['id']} (under {lo} words)")
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
