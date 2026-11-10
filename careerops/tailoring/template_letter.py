"""The template letter (v3.5.3, 10 Nov 2026): the user writes the cover letter ONCE per role family; the model fills three slots and writes nothing else.

Where the template lives (fact_bank.yaml, the user's own words): `letter_templates: {<family>: |...}` at the top level, or `letter_template: |...` on a
`families:` entry. The family is looked up in this order: the job's role family (settings roles.families, "forward_deployed_engineer"), then the bank
family the CV used ("ai_engineer"). A template is the whole letter: the greeting line ("Hi {company} team,"), the body paragraphs (blank lines between)
and the sign-off ("Regards," then the name on the next line).

The slots (anything else in braces is an error in the template):
  {company}                   code: the job's company.
  {company_problem_sentence}  model: ONE sentence of the job ad, word for word, lightly shortened (words may be dropped, at least SHORTEN_MIN of
                              them kept, in order; "we" / "our" / "us" may become the company's name; the capital and the full stop may change).
                              No "I", "my", "you", "your". Checked in code (`problem_sentence_problems`).
  {why_sentence}              model: ONE first-person sentence on why this team, with a run of 4+ words of the ad; the assembled letter's P5 guards
                              (assembled_letter.p5_problems: every claim about the company in the ad, no number, no "we", no name the ad or the
                              bank does not give).
  {question}                  model: what the 20 minutes would be about, the words that finish "Could we spend 20 minutes on ___?" (at most
                              QUESTION_MAX_WORDS, no "I", every name and number from the ad).
The model is asked up to TRIES times (each time told what was wrong); a slot that never passes is filled in code instead: the first About / role
sentence of the ad that passes, the P5 fallback ("<Company>'s focus on <a phrase of the ad> is why I applied."), the first problem the role would own.

Then the WHOLE letter goes through the fact check (`fact_problems`): every number, name and JD keyword in the template's own text must be in the fact
bank (it is what the letter says about you); every number and name in a slot must be in the ad (it is what the letter says about the company); no
"we" / "our", no "I help <Company>", no keyword dump, no memory / GB figure for an ad that is not about hardware, and the body inside
letter.company_words. A letter that fails is still written but marked DRAFT (review shows why; approval needs [e] or a fact-bank fix).

Paragraph names (so that review, [e] and the free-text answers read it like any company letter): the paragraph with {company_problem_sentence} is
P1, the one with {why_sentence} P5, the one with {question} P7; the paragraphs between P1 and P5 are P3, P4, P4b; one between P5 and P7 is P6.
The 5-word-shingle reuse rule is not applied to the template's fixed text: it is the user's own, the same in every letter on purpose.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..llm.router import TaskUnavailable
from .faithfulness import check as fact_check, numbers
from .grounding import _names, bank_text, name_allowed, sentences
from .scorer import Keyword, norm_literal

TASK = "cover_letter"
MODE = "template_slots"
TRIES = 3
SLOTS = ("company_problem_sentence", "why_sentence", "question")
CODE_SLOTS = ("company",)
SHORTEN_MIN = 0.6                    # a shortened ad sentence keeps at least this share of its words
PROBLEM_MIN_WORDS = 6
PROBLEM_MAX_WORDS = 40
QUESTION_MAX_WORDS = 20
MIDDLE = ("P3", "P4", "P4b")
_SLOT = re.compile(r"\{([A-Za-z_]+)\}")
_TOKEN = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")
_PERSON = re.compile(r"\b(?:I|me|my|mine|you|your|yours)\b", re.I)
_WE = re.compile(r"^(?:we|our|ours|us)$")
_SIGNOFF = re.compile(r"^(?:regards|kind regards|best regards|best|thanks|thank you|sincerely|yours sincerely|yours faithfully)\s*,?$", re.I)
_GREETING = re.compile(r"^(?:hi|hello|dear)\b", re.I)
# the call's own frame ("Could we spend 20 minutes on ...?") is the ask, not a claim: its "we" and its number are not checked
_ASK = re.compile(r"\b(?:could|can|shall|may|might)\s+we\s+(?:spend|find|take|have|book)\s+(?:\d+|ten|fifteen|twenty|thirty)\s+minutes\b", re.I)

SYSTEM = """You fill exactly three slots of a cover letter that the applicant wrote. You write nothing else and change nothing else.
Return JSON only: {"company_problem_sentence": "...", "why_sentence": "...", "question": "..."}.
- company_problem_sentence: ONE sentence copied from AD_SENTENCES, about what the company or the team needs, builds or is solving. You may only
  delete words to shorten it (keep at least 60% of them, in the same order), replace "we" / "our" / "us" with the company's name, and fix the first
  capital letter and the final full stop. Never "I", "my", "you" or "your".
- why_sentence: ONE sentence (at most 40 words) in the first person on why this team and role appeal. It must contain, word for word, a run of at
  least 4 consecutive words copied from AD_SENTENCES, and every other content word must be the ad's too (code checks it): the safe shape is
  "<the ad's own words about the company or the role> is why I applied." Make no claim about the applicant's experience; no number; never "we" or
  "our"; never "I am excited", "I am writing" or "I am applying".
- question: the words that finish "Could we spend 20 minutes on ___?": the first problem the role would own, in the ad's words (at most 20 words,
  starting in lower case, no question mark, never "I").
If PROBLEMS is given, your last answer was rejected for them: fix exactly those slots."""


@dataclass
class Template:
    family: str
    greeting: str                            # with {company}; no trailing comma
    paragraphs: list[str]                    # body paragraphs, slots still in braces
    names: list[str]                         # P1 ... P7, one per paragraph
    signoff: str = "Regards"
    name: str = ""
    errors: list[str] = field(default_factory=list)


def raw_template(fb: Mapping[str, Any], families: list[str]) -> tuple[str, str]:
    """(family, template text) of the first family in `families` that has one (top-level `letter_templates.<family>`, else `families[*].letter_template`),
    or ("", "")."""
    top = fb.get("letter_templates") or {}
    by_id = {str(f.get("id")): f for f in fb.get("families") or [] if isinstance(f, Mapping)}
    for fam in [f for f in families if f]:
        text = (top.get(fam) if isinstance(top, Mapping) else None) or (by_id.get(fam) or {}).get("letter_template")
        if isinstance(text, str) and text.strip():
            return fam, text
    return "", ""


def parse(family: str, text: str) -> Template:
    """The template's greeting, body paragraphs (with their P-names), sign-off and name. `errors` lists what makes it unusable."""
    blocks = [[l.strip() for l in b.strip().splitlines() if l.strip()] for b in re.split(r"\n\s*\n", text.strip())]
    blocks = [b for b in blocks if b]
    t = Template(family, "", [], [])
    if blocks and _GREETING.match(blocks[0][0]):
        t.greeting = blocks[0][0].rstrip(" ,")
        rest = blocks[0][1:]
        blocks = ([rest] if rest else []) + blocks[1:]
    else:
        t.errors.append("it has no greeting line ('Hi {company} team,') first")
    if blocks and _SIGNOFF.match(blocks[-1][0]):
        t.signoff, t.name = blocks[-1][0].rstrip(" ,"), " ".join(blocks[-1][1:])
        blocks = blocks[:-1]
    elif len(blocks) >= 2 and _SIGNOFF.match(blocks[-2][-1]) and len(blocks[-1]) == 1:
        t.signoff, t.name = blocks[-2][-1].rstrip(" ,"), blocks[-1][0]
        blocks = blocks[:-2] + ([blocks[-2][:-1]] if blocks[-2][:-1] else [])
    else:
        t.errors.append("it has no sign-off ('Regards,' then your name) at the end")
    t.paragraphs = [" ".join(b) for b in blocks]
    used = [m.group(1) for p in [t.greeting, *t.paragraphs] for m in _SLOT.finditer(p)]
    unknown = sorted({s for s in used if s not in SLOTS + CODE_SLOTS})
    if unknown:
        t.errors.append(f"unknown slot(s) {', '.join('{' + s + '}' for s in unknown)} (the slots are {{company}} and {{{'}, {'.join(SLOTS)}}})")
    at: dict[str, int] = {}
    for s in SLOTS:
        where = [i for i, p in enumerate(t.paragraphs) if "{" + s + "}" in p]
        n = sum(p.count("{" + s + "}") for p in t.paragraphs)
        if n != 1:
            t.errors.append(f"{{{s}}} must appear exactly once in the body (found {n})")
        elif where:
            at[s] = where[0]
    if len(at) == len(SLOTS):
        p1, p5, p7 = at["company_problem_sentence"], at["why_sentence"], at["question"]
        if len({p1, p5, p7}) < 3 or not p1 < p5 < p7:
            t.errors.append("the body must hold {company_problem_sentence}, then {why_sentence}, then {question}, each in a paragraph of its own")
        elif p1 != 0:
            t.errors.append("{company_problem_sentence} must be in the first body paragraph (the letter opens with the company's problem)")
        elif p5 - p1 - 1 > len(MIDDLE) or p7 - p5 - 1 > 1 or p7 != len(t.paragraphs) - 1:
            t.errors.append("at most three paragraphs between the problem and the why sentence, at most one between the why sentence and the "
                            "question, and nothing after the question")
        else:
            t.names = ["P1", *MIDDLE[:p5 - p1 - 1], "P5", *(["P6"] if p7 - p5 == 2 else []), "P7"]
    return t


def find(fb: Mapping[str, Any], families: list[str]) -> Template | None:
    family, text = raw_template(fb, families)
    return parse(family, text) if text else None


# ------------------------------------------------------------------ the slot checks
CO = "zzcompanyzz"                     # the company's name (or its possessive) as one token: it may stand for the ad's "we" / "our" / "us"


def _tokens(text: str, company: str = "") -> list[str]:
    low = " ".join(str(text).split()).lower().replace("’", "'")
    if company.strip():
        low = re.sub(r"(?<![\w])" + re.escape(company.strip().lower()) + r"(?:'s|')?(?![\w])", f" {CO} ", low)
    return _TOKEN.findall(norm_literal(low).replace("’", "'"))


def shortened_from(slot: str, ad: list[str], company: str) -> str | None:
    """The ad sentence `slot` is a light shortening of (its words in order, at least SHORTEN_MIN of the sentence kept; the company's name may stand
    for "we" / "our" / "us"), or None."""
    want = _tokens(slot, company)
    if not want:
        return None
    for sent in ad:
        have, i = _tokens(sent, company), 0
        for w in want:
            while i < len(have) and not (have[i] == w or (w == CO and _WE.match(have[i]))):
                i += 1
            if i == len(have):
                break
            i += 1
        else:
            if have and len(want) >= len(have) * SHORTEN_MIN - 1e-9:
                return sent
    return None


def problem_sentence_problems(slot: str, ad: list[str], company: str) -> list[str]:
    s = " ".join(str(slot or "").split())
    if not s:
        return ["company_problem_sentence is empty"]
    out = []
    if len(sentences(s)) != 1:
        out.append("company_problem_sentence must be ONE sentence")
    n = len(s.split())
    if not PROBLEM_MIN_WORDS <= n <= PROBLEM_MAX_WORDS:
        out.append(f"company_problem_sentence has {n} words (between {PROBLEM_MIN_WORDS} and {PROBLEM_MAX_WORDS})")
    if _PERSON.search(s):
        out.append(f"company_problem_sentence says '{_PERSON.search(s).group(0)}': it is about the company, not you or the reader")
    if re.search(r"\b(?:we|our|ours|us)\b", s, re.I):
        out.append("company_problem_sentence says 'we' / 'our': use the company's name")
    if shortened_from(s, ad, company) is None:
        out.append(f"company_problem_sentence is not a sentence of the ad, word for word (words may only be dropped, at least {SHORTEN_MIN:.0%} kept)")
    return out


def question_problems(slot: str, ad_all: str, company: str, title: str) -> list[str]:
    s = " ".join(str(slot or "").split()).rstrip(" ?.")
    if not s:
        return ["question is empty"]
    out = []
    if len(s.split()) > QUESTION_MAX_WORDS:
        out.append(f"question has {len(s.split())} words (at most {QUESTION_MAX_WORDS})")
    if "?" in s or len(sentences(s)) > 1:
        out.append("question must be the end of ONE question, with no question mark of its own")
    if re.search(r"\bI\b|\bmy\b", s):
        out.append("question speaks about you: it asks about the role")
    allowed = f"{ad_all} {company} {title}"
    if numbers(s) - numbers(allowed):
        out.append(f"question gives a number the ad does not: {', '.join(sorted(numbers(s) - numbers(allowed)))}")
    lit = norm_literal(allowed)
    out += [f"question names '{n}', which the ad does not" for n in _names("x " + s) if not name_allowed(lit, n)]
    return out


# ------------------------------------------------------------------ the fact check on the whole letter
def _raw_bank(fb: Mapping[str, Any]) -> str:
    parts: list[str] = []

    def walk(x) -> None:
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, Mapping):
            for k, v in x.items():
                if k not in ("deny_terms", "letter_template", "letter_templates"):
                    walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(fb)
    return " ".join(parts)


def _without(text: str, pieces: list[str]) -> str:
    for p in sorted((p for p in pieces if p), key=len, reverse=True):
        text = text.replace(p, " ")
    return " ".join(text.split())


def fact_problems(paragraphs: list[str], slots: Mapping[str, str], fb: Mapping[str, Any], kws: list[Keyword], ad_all: str, company: str,
                  title: str = "") -> list[str]:
    """The fact check on a filled template letter. The template's own text (the paragraphs without the slot values) speaks about you: its numbers,
    names and JD keywords must be in the fact bank. The slot values speak about the company: their numbers and names must be in the ad. Then the
    rules every letter keeps (no "we" / "our", no "I help <Company>", no keyword dump)."""
    from . import letters as L
    from .letter_rules import term_dump_problems

    values = [str(v) for v in slots.values() if v]
    paragraphs = [_ASK.sub(" ", p) for p in paragraphs]
    mine = _without(" ".join(paragraphs), [*values, company])
    bank_raw, bank_lit = _raw_bank(fb), bank_text(dict(fb))
    out = [f"template text: {v}" for v in fact_check(mine, bank_raw, [], list(fb.get("deny_terms") or []), kws, vocabulary="",
                                                     max_growth=1e9).violations]
    for s in sentences(mine):
        for n in _names(s):
            if not name_allowed(bank_lit, n) and not name_allowed(norm_literal(f"{company} {title}"), n):
                out.append(f"template text names '{n}', which the fact bank does not")
    allowed = f"{ad_all} {company} {title}"
    for key, v in slots.items():
        extra = numbers(v) - numbers(allowed)
        if extra:
            out.append(f"{key} gives the number(s) {', '.join(sorted(extra))}, which the ad does not")
    text = " ".join(paragraphs)
    out += L.claim_problems(paragraphs, company, dict(fb), set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", allowed)))
    out += term_dump_problems(paragraphs, dict(fb))
    if not text.strip():
        out.append("the letter is empty")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ filling it
def fill(t: Template, company: str, values: Mapping[str, str]) -> list[str]:
    """The body paragraphs with every slot filled."""
    out = []
    for p in t.paragraphs:
        for s in (*CODE_SLOTS, *SLOTS):
            v = company if s == "company" else str(values.get(s) or "")
            p = p.replace("{" + s + "}", v)
        out.append(" ".join(p.split()))
    return out


def _clean_slot(key: str, v: Any) -> str:
    s = " ".join(str(v or "").split()).strip().strip('"“”')
    if key == "question":
        s = re.sub(r"^could we spend 20 minutes on\s+", "", s, flags=re.I).rstrip(" ?.")
        return s[:1].lower() + s[1:] if s[:1].isupper() and not s.split()[0].isupper() and not s.split()[0][1:2].isupper() else s
    if s and s[-1] not in ".!?":
        s += "."
    return s[:1].upper() + s[1:]


def slot_problems(key: str, v: str, job: dict, fb: dict, kws: list[Keyword], team: str, ad: list[str], ad_all: str, offered: list[str]) -> list[str]:
    from . import assembled_letter as AL

    if key == "company_problem_sentence":
        return problem_sentence_problems(v, ad, job["company"])
    if key == "why_sentence":
        return [p if p.startswith("why") else f"why_sentence: {p}" for p in AL.p5_problems(v, job, fb, kws, None, team, ad_all, offered)] if v else \
            ["why_sentence is empty"]
    return question_problems(v, ad_all, job["company"], job["title"])


def fallback(key: str, job: dict, fb: dict, kws: list[Keyword], team: str, ad: list[str], ad_all: str, offered: list[str], problem: str) -> str:
    """The code's value for a slot the model never filled well: an About / role sentence of the ad (then any ad sentence) that passes; the P5
    fallback; the first problem the role would own."""
    from . import assembled_letter as AL

    company = job["company"]
    if key == "company_problem_sentence":
        own = AL.possessive(company)
        for s in [*offered, *ad]:
            v = re.sub(r"\b[Oo]ur\b", own, re.sub(r"\b(?:[Ww]e|[Uu]s)\b", company, s))
            v = _clean_slot(key, v)
            if not problem_sentence_problems(v, ad, company):
                return v
        return ""
    if key == "why_sentence":
        return AL.p5_fallback(offered, company, lambda c: AL.p5_problems(c, job, fb, kws, None, team, ad_all, offered))
    return f"the first problem the {job['title']} would own" + (f", around {problem}" if problem else "")


def make(t: Template, job: dict, fb: dict, kws: list[Keyword], llm, settings, out_dir: Path, final_name: str, team: str, header: dict | None,
         problem: str, page_text: str = "", feedback: str = "", notes: list[str] | None = None) -> tuple[Path | None, dict]:
    """(pdf, letter dict) of a template letter: the three slots from the model (checked; up to TRIES), else from code; the whole letter through
    `fact_problems` and the length range; written with company_letter.write_letter. A letter that fails a check is written and marked DRAFT."""
    from . import company_letter as CL
    from . import letters as L
    from . import tailor as T

    notes = notes if notes is not None else []
    jd = job.get("description") or ""
    ad_all = jd + ("\n" + page_text if page_text else "")
    ad, offered = CL.ad_sentences(jd), CL.about_sentences(ad_all, job["company"], kws)
    if t.errors:
        return None, CL._letter_dict("none", reason=f"letter_template of '{t.family}': " + "; ".join(t.errors), notes=notes, compose="template",
                                     template_family=t.family)
    values: dict[str, str] = {}
    source: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    for attempt in range(TRIES):
        todo = [k for k in SLOTS if k not in values]
        if not todo:
            break
        user = json.dumps({"company": job["company"], "title": job["title"], "team": team, "slots": todo,
                           "letter": "\n\n".join(t.paragraphs), "ad_sentences": ad[:80], "about_sentences": offered,
                           "problems": {k: rejected[k] for k in todo if k in rejected}, "feedback": feedback}, ensure_ascii=False)
        try:
            out = T._llm_json(llm, TASK, SYSTEM, user, {"mode": MODE, "company": job["company"], "title": job["title"], "slots": todo,
                                                       "ad_sentences": ad, "about_sentences": offered, "attempt": attempt + 1})
        except TaskUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            notes.append(f"cover_letter (template): the model failed ({str(e)[:120]}); the slots left are filled in code")
            break
        for k in todo:
            v = _clean_slot(k, (out or {}).get(k))
            bad = slot_problems(k, v, job, fb, kws, team, ad, ad_all, offered)
            if bad:
                rejected[k] = bad
            else:
                values[k], source[k] = v, "model"
    for k in SLOTS:
        if k not in values:
            v = fallback(k, job, fb, kws, team, ad, ad_all, offered, problem)
            values[k], source[k] = v, "code"
            if k in rejected:
                notes.append(f"cover_letter (template): {k} filled in code after {TRIES} rejected answer(s); last: {'; '.join(rejected[k])[:200]}")
    if not values.get("company_problem_sentence"):
        return None, CL._letter_dict("none", reason="no sentence of the ad can open the template letter (the model's were rejected and none of the "
                                     "ad's own passes the check)", notes=notes, compose="template", template_family=t.family, retry={"rejected": rejected})
    paragraphs = fill(t, job["company"], values)
    parts = dict(zip(t.names, paragraphs))
    problems = fact_problems(paragraphs, values, fb, kws, ad_all, job["company"], job["title"])
    problems += [f"slot {p}" for k in SLOTS for p in (slot_problems(k, values[k], job, fb, kws, team, ad, ad_all, offered) if source[k] == "code" else [])]
    problems += CL.hardware_problems(parts, jd)
    lo, cap = CL.word_range(settings)[0], CL.word_cap(settings)
    n = CL.words(paragraphs)
    if not lo <= n <= cap:
        problems.append(f"{n} body words: the letter must be between {lo} and {cap} (change the template in fact_bank.yaml)")
    greeting = L.greeting(job, team, settings) if L.contact_first_name(job) else t.greeting.replace("{company}", job["company"])
    draft = bool(problems)
    pdf, letter = CL.write_letter(job, kws, parts, team, fb, settings, out_dir, final_name, header, None, greeting=greeting, signoff=t.signoff,
                                  notes=notes, compose="template", template_family=t.family, slots=dict(values), slot_source=dict(source),
                                  retry={"rejected": rejected}, hook_source=f"letter_template '{t.family}'", target_words=CL.target_words(settings),
                                  template_problems=list(problems), draft=draft,
                                  draft_reason=("the fact check failed: " + "; ".join(problems)[:300]) if draft else "")
    return pdf, letter
