"""The template letter (v3.5.3, 10 Nov 2026): the user writes the cover letter ONCE per role family; the model fills three slots and writes nothing else.

Where the template lives (fact_bank.yaml, the user's own words): `letter_templates: {<family>: |...}` at the top level, or `letter_template: |...` on a
`families:` entry. The family is looked up in this order: the job's role family (settings roles.families, "forward_deployed_engineer"), then the bank
family the CV used ("ai_engineer"). A template is the whole letter: the greeting line ("Hi {company} team,"), the body paragraphs (blank lines between)
and the sign-off ("Regards," then the name on the next line).

The slots (anything else in braces is an error in the template):
  {company}                   code: the job's company.
  {company_problem_sentence}  model: ONE sentence, at most PROBLEM_MAX_WORDS (30) words, from the ad, on what the team must achieve: it starts "Your team ..." or
                              "<Company> needs ...", never describes the company, no "I" / "my" / "we" / "our", every content word the ad's.
  {ad_requirement}            model: a noun phrase of the ad, at most REQUIREMENT_MAX_WORDS (15) words, that reads inside the template's frame "Your ad asks
                              for {ad_requirement}, which ...": no leading verb (a copied imperative), no "to ...", no end punctuation.
  {question}                  model: a noun phrase starting how / what / which, at most QUESTION_MAX_WORDS (15) words, that finishes "Could we spend 20
                              minutes on ___?"; no "I", every name and number from the ad.
The model is asked up to TRIES times (each time told what was wrong); a slot that never passes is filled in code instead, with a
FIXED fallback (FALLBACKS) and the letter is marked DRAFT with the slot named (v3.5.6: a slot that does not fit its frame is never patched up).

Then the WHOLE letter goes through the fact check (`fact_problems`): every number, name and JD keyword in the template's own text must be in the fact
bank (it is what the letter says about you); every number and name in a slot must be in the ad (it is what the letter says about the company); no
"we" / "our", no "I help <Company>", no keyword dump, no memory / GB figure for an ad that is not about hardware, and the body inside
letter.company_words. A letter that fails is still written but marked DRAFT (review shows why; approval needs [e] or a fact-bank fix).

Paragraph names (so that review, [e] and the free-text answers read it like any company letter): the paragraph with {company_problem_sentence} is
P1, the one with {ad_requirement} P5, the one with {question} P7; the paragraphs between P1 and P5 are P3, P4, P4b; one between P5 and P7 is P6.
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
SLOTS = ("company_problem_sentence", "ad_requirement", "question")
CODE_SLOTS = ("company",)
SHORTEN_MIN = 0.6                    # a shortened ad sentence keeps at least this share of its words
PROBLEM_MIN_WORDS = 6
PROBLEM_MAX_WORDS = 30
REQUIREMENT_MAX_WORDS = 15
QUESTION_MAX_WORDS = 15
MIDDLE = ("P3", "P4", "P4b")
_SLOT = re.compile(r"\{([A-Za-z_]+)\}")
_TOKEN = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")
_PERSON = re.compile(r"\b(?:I|me|my|mine|you|your|yours)\b", re.I)
_ME = re.compile(r"\b(?:I|me|my|mine)\b")
_WE = re.compile(r"^(?:we|our|ours|us)$")
_SIGNOFF = re.compile(r"^(?:regards|kind regards|best regards|best|thanks|thank you|sincerely|yours sincerely|yours faithfully)\s*,?$", re.I)
_GREETING = re.compile(r"^(?:hi|hello|dear)\b", re.I)
# the call's own frame ("Could we spend 20 minutes on ...?") is the ask, not a claim: its "we" and its number are not checked
_ASK = re.compile(r"\b(?:could|can|shall|may|might)\s+we\s+(?:spend|find|take|have|book)\s+(?:\d+|ten|fifteen|twenty|thirty)\s+minutes\b", re.I)

SYSTEM = """You fill exactly three slots of a cover letter that the applicant wrote. You write nothing else and change nothing else.
Return JSON only: {"company_problem_sentence": "...", "ad_requirement": "...", "question": "..."}.
- company_problem_sentence: ONE sentence, at most 30 words, on what the team must achieve, taken from the ad. It starts "Your team ..." or
  "<Company> needs ...". Never a description of the company ("X is a leading ..."), never "I", "my", "we" or "our"; every content word is the ad's.
- ad_requirement: a NOUN PHRASE of the ad, at most 15 words, lower case, no full stop, that reads correctly in "Your ad asks for ___, which is the
  work I want to do next." Never start with a verb or an instruction ("Build ...", "Design ...", "to ..."); e.g. "production LLM agents with tool
  calling on AWS". Every content word is the ad's; no number or name the ad does not give.
- question: a noun phrase starting with how, what or which, at most 15 words, lower case, no question mark, that finishes "Could we spend 20 minutes
  on ___?", e.g. "how the team evaluates agent failures in production". The ad's words; never "I".
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
        p1, p5, p7 = at["company_problem_sentence"], at["ad_requirement"], at["question"]
        if len({p1, p5, p7}) < 3 or not p1 < p5 < p7:
            t.errors.append("the body must hold {company_problem_sentence}, then {ad_requirement}, then {question}, each in a paragraph of its own")
        elif p1 != 0:
            t.errors.append("{company_problem_sentence} must be in the first body paragraph (the letter opens with the company's problem)")
        elif p5 - p1 - 1 > len(MIDDLE) or p7 - p5 - 1 > 1 or p7 != len(t.paragraphs) - 1:
            t.errors.append("at most three paragraphs between the problem and the ad requirement, at most one between the ad requirement and the "
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


_STOP = frozenset("a an the of to for and or in on at by with from as is are be been it its this that these those which who their them they your team must need needs how what when where why would will can could should may might shall do does did has have had not no than then so such into more most very also first next own".split())
_END_BAD = _STOP | frozenset("into over under about across through per via".split())
_DESCRIBES = re.compile(r"\b(?:is|are|was)\s+(?:a|an|the|one of)\b|\bfounded\b|\bheadquartered\b|\bbased (?:in|out of)\b|\bleading\b|\bworld[- ]class\b", re.I)
# a base-form verb that opens an instruction in an ad ("Build ...", "Design ..."): never the start of a noun phrase after "asks for"
IMPERATIVES = frozenset("""build design develop deploy lead own drive work collaborate create write ship run manage implement maintain
    partner support help define deliver evaluate train analyse analyze architect improve optimise optimize monitor integrate prototype
    research identify ensure contribute communicate mentor test debug scale operate apply use make take join""".split())
FALLBACKS = {
    "company_problem_sentence": "{company} needs the {title} to deliver the work described in this ad.",
    "ad_requirement": "the work this role would own",
    "question": "what the first problem in this role would be",
}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def _not_in_ad(slot: str, ad_all: str, company: str, title: str) -> list[str]:
    """The content words of `slot` (stop words out) that neither the ad, the company nor the title has, up to a plural / -ing / -ed ending."""
    have = {_stem(t) for t in _tokens(f"{ad_all} {company} {title}")}
    return [w for w in dict.fromkeys(_tokens(slot, company)) if w != CO and w not in _STOP and _stem(w) not in have and not w.isdigit()]


def _tail_problems(key: str, s: str) -> list[str]:
    """The ending of a noun-phrase slot: a letter or digit (no stop, colon or full stop), and not a joining word."""
    out = []
    if not re.search(r"[A-Za-z0-9)]$", s):
        out.append(f"{key} must not end with punctuation (it sits inside a sentence of the template)")
    last = (s.split() or [""])[-1].lower().strip(".,;:!?)")
    if last in _END_BAD:
        out.append(f"{key} ends with '{last}': it must end on the noun")
    return out


def problem_sentence_problems(slot: str, ad: list[str], company: str, ad_all: str = "", title: str = "") -> list[str]:
    """company_problem_sentence: ONE sentence of 6-30 words that says what the team must achieve ("Your team ..." / "<Company> needs ..."),
    no "I" / "my" / "we" / "our", no company description, every content word the ad's (a light shortening of an ad sentence passes too)."""
    s = " ".join(str(slot or "").split())
    if not s:
        return ["company_problem_sentence is empty"]
    out = []
    if len(sentences(s)) != 1:
        out.append("company_problem_sentence must be ONE sentence")
    n = len(s.split())
    if not PROBLEM_MIN_WORDS <= n <= PROBLEM_MAX_WORDS:
        out.append(f"company_problem_sentence has {n} words (between {PROBLEM_MIN_WORDS} and {PROBLEM_MAX_WORDS})")
    if _ME.search(s):
        out.append(f"company_problem_sentence says '{_ME.search(s).group(0)}': it is about the team, not you")
    if re.search(r"\byour\b", s, re.I) and not re.match(r"your team\b", s, re.I):
        out.append("company_problem_sentence says 'your' outside \"Your team ...\"")
    if re.search(r"\b(?:we|our|ours|us)\b", s, re.I):
        out.append("company_problem_sentence says 'we' / 'our': use the company's name")
    if not (re.match(r"your team\b", s, re.I) or (company.strip() and re.match(re.escape(company.strip()) + r"\s+needs\b", s, re.I))):
        out.append(f"company_problem_sentence must start \"Your team ...\" or \"{company} needs ...\" (what the team must achieve)")
    if _DESCRIBES.search(s):
        out.append("company_problem_sentence describes the company; say what the team must achieve")
    if not s.endswith((".", "!")) or s.endswith(".."):
        out.append("company_problem_sentence must end with a full stop")
    if ad_all and shortened_from(s, ad, company) is None:
        miss = _not_in_ad(s, ad_all, company, title)
        if miss:
            out.append(f"company_problem_sentence uses words the ad does not: {', '.join(miss[:6])}")
    return out


def requirement_problems(slot: str, ad_all: str, company: str, title: str) -> list[str]:
    """ad_requirement: a noun phrase of at most 15 words that reads inside "Your ad asks for ___, which ...": no imperative or "to ..." start, no
    "I" / "my", no punctuation inside or after, ends on the noun, every content word, number and name the ad's."""
    s = " ".join(str(slot or "").split())
    if not s:
        return ["ad_requirement is empty"]
    out = []
    words = s.split()
    if len(words) > REQUIREMENT_MAX_WORDS:
        out.append(f"ad_requirement has {len(words)} words (at most {REQUIREMENT_MAX_WORDS})")
    first = words[0].lower().strip(",")
    if first in IMPERATIVES or first in ("to", "we", "you", "your", "our", "i", "who", "which", "that", "how", "what", "if", "when"):
        out.append(f"ad_requirement starts with '{words[0]}': it must be a noun phrase, not an instruction or a clause (\"Your ad asks for ___\")")
    if _ME.search(s) or re.search(r"\b(?:we|our|you|your)\b", s, re.I):
        out.append("ad_requirement must not speak about you, the reader or the company in the first person")
    if re.search(r"[.;:!?]", s):
        out.append("ad_requirement must be one phrase: no full stop, colon, semicolon or question mark")
    out += _tail_problems("ad_requirement", s)
    out += _content_problems("ad_requirement", s, ad_all, company, title)
    return out


def _content_problems(key: str, s: str, ad_all: str, company: str, title: str) -> list[str]:
    out = []
    allowed = f"{ad_all} {company} {title}"
    if numbers(s) - numbers(allowed):
        out.append(f"{key} gives a number the ad does not: {', '.join(sorted(numbers(s) - numbers(allowed)))}")
    lit = norm_literal(allowed)
    out += [f"{key} names '{n}', which the ad does not" for n in _names("x " + s) if not name_allowed(lit, n)]
    miss = _not_in_ad(s, ad_all, company, title)
    if miss:
        out.append(f"{key} uses words the ad does not: {', '.join(miss[:6])}")
    return out


def question_problems(slot: str, ad_all: str, company: str, title: str) -> list[str]:
    """question: a noun phrase of at most 15 words starting how / what / which, one question's end, no "I" / "my", the ad's words."""
    s = " ".join(str(slot or "").split()).rstrip(" ?.")
    if not s:
        return ["question is empty"]
    out = []
    n = len(s.split())
    if n > QUESTION_MAX_WORDS:
        out.append(f"question has {n} words (at most {QUESTION_MAX_WORDS})")
    if "?" in s or len(sentences(s)) > 1:
        out.append("question must be the end of ONE question, with no question mark of its own")
    if s.split()[0].lower() not in ("how", "what", "which"):
        out.append(f"question starts with '{s.split()[0]}': it must start with how, what or which")
    if _ME.search(s):
        out.append("question speaks about you: it asks about the role")
    out += _tail_problems("question", s)
    out += _content_problems("question", s, ad_all, company, title)
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
    if key in ("question", "ad_requirement"):          # a phrase inside a sentence of the template: lower case, no end punctuation
        if key == "question":
            s = re.sub(r"^could we spend 20 minutes on\s+", "", s, flags=re.I)
        else:
            s = re.sub(r"^your ad asks for\s+", "", s, flags=re.I)
        s = s.rstrip(" ?.!,;:")
        first = s.split()[0] if s else ""
        return s[:1].lower() + s[1:] if s[:1].isupper() and not first.isupper() and not first[1:2].isupper() else s
    if s and s[-1] not in ".!?":
        s += "."
    return s[:1].upper() + s[1:]


def slot_problems(key: str, v: str, job: dict, fb: dict, kws: list[Keyword], team: str, ad: list[str], ad_all: str, offered: list[str]) -> list[str]:
    if key == "company_problem_sentence":
        return problem_sentence_problems(v, ad, job["company"], ad_all, job["title"])
    if key == "ad_requirement":
        return requirement_problems(v, ad_all, job["company"], job["title"])
    return question_problems(v, ad_all, job["company"], job["title"])


def fallback(key: str, job: dict) -> str:
    """The FIXED value (FALLBACKS) of a slot the model never filled so that it fits its frame. Not patched from the ad: the letter is marked DRAFT."""
    return FALLBACKS[key].format(company=job["company"], title=job["title"])


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
            v = fallback(k, job)
            values[k], source[k] = v, "fallback"
            if k in rejected:
                notes.append(f"cover_letter (template): {k} filled in code after {TRIES} rejected answer(s); last: {'; '.join(rejected[k])[:200]}")
    paragraphs = fill(t, job["company"], values)
    parts = dict(zip(t.names, paragraphs))
    problems = fact_problems(paragraphs, values, fb, kws, ad_all, job["company"], job["title"])
    problems += [f"slot {k}: the model's answers did not fit its frame, so the fixed fallback is used ({'; '.join(rejected.get(k) or ['no answer'])[:200]})"
                 for k in SLOTS if source[k] == "fallback"]
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
