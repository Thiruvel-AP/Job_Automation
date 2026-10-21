"""Free-text answers to application-form questions ("Describe an LLM feature you've built", "Why {company}?").

The form filler (apply/filler.py) hands every REQUIRED free-text field that answers.yaml does not answer to `FreeText.answer`:

  1. read the question and its limit ("1-2 sentences", "max 200 words", the field's maxlength; nothing stated = 120 words) and classify it
     (project | challenge | why_company | why_role | other);
  2. an answer YOU approved or wrote for a similar question (same class, similarity >= 0.85, still inside this question's limit) is reused as it
     is. "Why {company}?" is only ever reused for the same company;
  3. otherwise PLAN which fact-bank entries it may draw on (the ones whose terms best overlap the question and the ad's keywords; the hook proof
     of this job's CV first, so the answer and the CV agree), DRAFT it with one model call, and GUARD the draft as strictly as the CV: every
     number, tool, organisation and name must be in the chosen entries (for a "why" answer: the ad, your `motivations`, the CV's hook), no
     deny-listed term, no word of `style.avoid`, first person, the limit, the letter guard (no "we/our", no pasted list of terms), and a claim
     about the company must be a run of words the ad itself contains. A draft that fails is redrafted once with the problems named; a second
     failure goes to you with no draft;
  4. show it (question, limit, sources, guard result) and ask `[y] use · [e] edit · [r] redraft · [s] I'll write it`. Only `y`, a text you
     typed, or a text you edited is used, and is stored in the answer library (paths.answers_library) with approved: true.

A new draft is therefore never used without your `y`, so it can never be submitted without it. Nothing here writes to answers.yaml, fact_bank.yaml
or applicant.yaml: the library is runtime data. "Other" questions (hobbies, "anything else?") are never drafted: they are asked.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping

from ..tailoring import faithfulness, letters as letters_mod
from ..tailoring.grounding import sentences as split_sentences
from ..tailoring.letter_rules import term_dump_problems
from ..tailoring.scorer import Keyword, _contains, gap_terms, keyword_hits, norm_literal
from ..tailoring.textnorm import normalise_json
from . import answers as answers_mod

TASK = "free_text_answer"
CLASSES = ("project", "challenge", "why_company", "why_role", "other")
DEFAULT_WORDS = 120               # the limit when the question and the field state none
LONG_TEXT_CHARS = 100             # a one-line field with room for more than this is a free-text field
SIMILAR = 0.85                    # an approved answer is reused for a question at least this similar (same class)
MAX_PLAN = 3                      # fact-bank entries one answer may draw on
MAX_REDRAFTS = 3                  # redrafts the person may ask for with `r`
AD_RUN_WORDS = 4                  # a claim about the company must contain this many words in a row that the ad contains
MENU = "  [y] use · [e] edit · [r] redraft · [s] I'll write it  > "


# ------------------------------------------------------------------ the limit of a question
@dataclass(frozen=True)
class Limit:
    words: int | None = None
    sentences: int | None = None
    chars: int | None = None
    stated: bool = False                                   # read from the label or the field (else the default words)

    def problems(self, text: str) -> list[str]:
        out = []
        n_words, n_sent = len(text.split()), len(split_sentences(text.strip()))
        if self.words is not None and n_words > self.words:
            out.append(f"{n_words} words: the limit is {self.words} words")
        if self.sentences is not None and n_sent > self.sentences:
            out.append(f"{n_sent} sentences: the limit is {self.sentences} sentence(s)")
        if self.chars is not None and len(text) > self.chars:
            out.append(f"{len(text)} characters: the limit is {self.chars} characters")
        return out

    def describe(self) -> str:
        parts = [f"at most {self.words} words" if self.words else "", f"at most {self.sentences} sentence(s)" if self.sentences else "",
                 f"at most {self.chars} characters" if self.chars else ""]
        return ", ".join(p for p in parts if p) + ("" if self.stated else " (none stated: the default)")

    def as_dict(self) -> dict:
        return {"words": self.words, "sentences": self.sentences, "chars": self.chars}


_MINIMUM = re.compile(r"(?:at\s+least|minimum(?:\s+of)?|min\.?|no\s+fewer\s+than)\s*$", re.I)


def _count(text: str, unit: str) -> int | None:
    """The largest number the text gives for `unit` ("1-2 sentences" -> 2, "max 200 words" -> 200); a minimum ("at least 50 words") is ignored."""
    found = []
    for m in re.finditer(r"(?:(\d+)\s*(?:-|to)\s*)?(\d+)\s*" + unit, text, re.I):
        if not _MINIMUM.search(text[:m.start()]):
            found.append(int(m.group(2)))
    return max(found) if found else None


def read_limits(text: str, maxlength: Any = None) -> Limit:
    """The limit of a question: from its wording (the label, the placeholder) and the field's `maxlength`. Nothing stated = DEFAULT_WORDS."""
    t = " ".join(str(text or "").replace("–", "-").replace("—", "-").split())
    words, sents, chars = _count(t, r"words?\b"), _count(t, r"sentences?\b"), _count(t, r"(?:characters?|chars?)\b")
    try:
        if int(maxlength) > 0:
            chars = min(chars, int(maxlength)) if chars else int(maxlength)
    except (TypeError, ValueError):
        pass
    if words is None and sents is None and chars is None:
        return Limit(words=DEFAULT_WORDS)
    return Limit(words, sents, chars, stated=True)


def is_free_text(typ: str, maxlength: Any = None) -> bool:
    """A text area, or a one-line text box with room for more than LONG_TEXT_CHARS characters."""
    if typ == "textarea":
        return True
    try:
        return typ in ("text", "") and int(maxlength) > LONG_TEXT_CHARS
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ the class of a question
def classify(question: str, company: str = "") -> str:
    q = " ".join(question.lower().replace("’", "'").split())
    named = bool(company) and answers_mod.norm(company) in answers_mod.norm(question)
    why = re.search(r"\bwhy\b|what (?:attracts|draws|interests|excites|motivates) you|what is it about", q)
    if why:
        role = re.search(r"\b(?:role|position|job|vacancy|opportunity|applying|apply|applied)\b", q)
        us = re.search(r"\b(?:us|our company|this company|the company|our team|join us|working here|work here|work for|work at|join)\b", q)
        if named or (us and not role):
            return "why_company"
        return "why_role" if role else "why_company"
    if re.search(r"\b(?:hardest|toughest|most difficult|biggest|greatest|main|key)\s+(?:challenge|part|obstacle|problem|difficulty)|"
                 r"\bchalleng(?:e|es|ing)\b|\bobstacles?\b|\bdifficult(?:y|ies)?\b|what went wrong|lessons? (?:you )?learn", q):
        return "challenge"
    if re.search(r"\b(?:describe|tell us about|tell me about|walk us through|walk me through|talk about|give an example|share an example|"
                 r"example of)\b.*\b(?:project|feature|system|model|pipeline|application|tool|product|agent|work|experience)\b", q) or \
            re.search(r"\b(?:built|build|shipped|deployed|developed|created|implemented|maintain\w*|designed)\b.*\b(?:llm|ml|model|system|project|"
                      r"feature|pipeline|product|agent|application)\b", q):
        return "project"
    return "other"


# ------------------------------------------------------------------ is this the same question?
_STOP = frozenset("""a an the you your ve we us me my i it its this that these those of to in on at for with and or but is are was were be been do did does
have has had please describe tell about give share example examples what which who how when where can could would should will any one some
more most very part way""".split())
_SAME = {"shipped": "build", "built": "build", "build": "build", "building": "build", "deployed": "build", "deploy": "build", "developed": "build",
         "develop": "build", "created": "build", "launched": "build", "delivered": "build", "maintain": "build", "maintained": "build",
         "implemented": "build", "designed": "build", "feature": "system", "tool": "system", "application": "system", "product": "system",
         "service": "system", "agent": "system", "pipeline": "system", "project": "system", "hardest": "hard", "toughest": "hard",
         "difficult": "hard", "challenge": "hard", "challenging": "hard", "biggest": "hard"}


def _tokens(question: str) -> frozenset[str]:
    q = re.sub(r"\([^)]*\)", " ", question.lower().replace("’", "'"))                # "(1-2 sentences)" is the limit, not the question
    words = re.findall(r"[a-z0-9]+", q.replace("'", " "))
    return frozenset(_SAME.get(w, w) for w in words if w not in _STOP and len(w) > 1)


def normalise(question: str) -> str:
    return " ".join(sorted(_tokens(question)))


def similarity(a: str, b: str) -> float:
    """0..1: the share of meaningful words two questions have in common once synonyms ("shipped", "built", "deployed") are one word."""
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jaccard, SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() if ta and tb and jaccard >= 0.5 else 0.0)


# ------------------------------------------------------------------ the answer library
@dataclass
class Record:
    question: str
    normalised: str
    answer: str
    company: str
    job_id: str
    source_ids: list[str]
    date: str
    approved: bool
    cls: str = "other"
    origin: str = "draft"                  # draft (approved as drafted) | edited | written | seed

    @classmethod
    def new(cls, question: str, answer: str, kind: str, company: str, job_id: str, source_ids: list[str], day: date,
            origin: str = "draft") -> "Record":
        return cls(question, normalise(question), answer, company, job_id, list(source_ids), day.isoformat(), True, kind, origin)


SEED_JOB = "manual:177a9a1afcc1"
SEEDS = [
    ("Describe an LLM feature you've built, deployed, and maintain", "project",
     "At AstraZeneca I built a tool-calling LLM agent (LangChain, Claude Haiku 4.5 on AWS Bedrock) for a cancer research platform and "
     "deployed it as a Docker image on AWS Lambda with least-privilege IAM. I measured it with a three-tier, ~65-test evaluation harness "
     "with hallucination-rate analysis, and reused the same harness to validate the migration from Groq to Bedrock after a rate-limit "
     "evaluation. It fails safe with circuit breakers and a 19,213-gene local parquet fallback when the external APIs are unavailable."),
    ("What was the hardest part of building that feature (1-2 sentences)", "challenge",
     "Making it trustworthy rather than just functional: failures only became visible once I tested layer by layer (tool, API, then live), "
     "and each one needed a deterministic guard or fallback instead of a prompt tweak. Balancing that against the rate limits and outages "
     "of the external APIs it depends on took the most iteration."),
]


class Library:
    """paths.answers_library: one JSON object per line, only answers the person approved or wrote. A file that does not exist yet starts with the
    two answers approved for Limbic on 2026-10-21. Append-only: a line is never changed."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _seed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "x", encoding="utf-8", newline="\n") as f:
            for q, kind, a in SEEDS:
                f.write(json.dumps(asdict(Record.new(q, a, kind, "Limbic", SEED_JOB, [], date(2026, 10, 21), "seed")), ensure_ascii=False) + "\n")

    def records(self) -> list[Record]:
        if not self.path.exists():
            try:
                self._seed()
            except FileExistsError:
                pass
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("approved") is True:
                    out.append(Record(**{k: d[k] for k in Record.__dataclass_fields__ if k in d}))
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def add(self, rec: Record) -> None:
        self.records()                                                    # a first answer does not hide the seeds
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def find(self, question: str, cls: str, company: str, limit: Limit, threshold: float = SIMILAR, job_id: str = "") -> Record | None:
        """The approved answer for a question like this one: same class, similarity >= threshold, inside `limit`; the best, the latest on a tie.
        A why_company answer is only found for the same company (and then any "why this company" wording is the same question), a why_role
        answer only for the same job: both quote that employer's ad."""
        best: tuple[float, int, Record] | None = None
        for n, r in enumerate(self.records()):
            if r.cls != cls or (cls == "why_company" and answers_mod.norm(r.company) != answers_mod.norm(company)) or \
                    (cls == "why_role" and (not job_id or r.job_id != job_id)):
                continue
            sim = 1.0 if cls == "why_company" else similarity(question, r.question)
            if sim >= threshold and not limit.problems(r.answer) and (best is None or (sim, n) >= best[:2]):
                best = (sim, n, r)
        return best[2] if best else None


# ------------------------------------------------------------------ what an answer may draw on
@dataclass
class Source:
    id: str
    text: str
    terms: list[str] = field(default_factory=list)


@dataclass
class Plan:
    cls: str
    sources: list[Source]
    why: str
    ad_snippets: list[str] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.sources]

    def allowed(self) -> str:
        return " ".join(s.text + " " + " ".join(s.terms) for s in self.sources)


def bullet_source(entry: Mapping[str, Any], b: Mapping[str, Any]) -> Source:
    """A fact-bank bullet with the entry it belongs to: title, employer or project name, situation, stack, the bullet and its result."""
    head = str(entry.get("title") or entry.get("name") or "")
    org = str(entry.get("org") or "")
    parts = [head + (f", {org}" if org and org != head else "") + ".", str(entry.get("situation") or ""), str(b["text"]), str(b.get("result") or ""),
             f"Stack: {entry['stack']}." if entry.get("stack") else ""]
    return Source(str(b["id"]), " ".join(p for p in parts if p.strip()), [str(t) for t in b.get("terms", [])])


def _fact_corpus(fb: Mapping[str, Any]) -> str:
    parts = list(fb.get("profile_facts", []))
    for g, items in (fb.get("skills") or {}).items():
        parts.append(f"{g}: {', '.join(map(str, items))}" if isinstance(items, list) else str(items))
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            parts.append(f"{e.get('title', e.get('name', ''))} {e.get('org', '')} {e.get('stack', '')}")
            parts += [b["text"] + " " + " ".join(b.get("terms", [])) for b in e["bullets"]]
    return "\n".join(map(str, parts))


def ad_snippets(ad: str, kws: list[Keyword], question: str, company: str, n: int = 8) -> list[str]:
    """The sentences of the job ad a "why" answer may quote, the ones that name the company or share words with the question and the keywords."""
    pieces = [re.sub(r"^[\s\-*•]+", "", p).strip() for p in re.split(r"\n+|(?<=[.!?])\s+", ad or "")]
    qwords, name = _tokens(question), answers_mod.norm(company)
    scored = []
    for i, p in enumerate(pieces):
        if len(p) < 20:
            continue
        score = len(keyword_hits(p, kws)) + len(qwords & _tokens(p)) + (2 if name and name in answers_mod.norm(p) else 0)
        scored.append((score, i, p[:260]))
    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:n]
    return [p for _, _, p in sorted(top, key=lambda t: t[1])]


# ------------------------------------------------------------------ the guard
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[-'’][A-Za-z0-9+#]+)*")
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|my|me|mine)\b")


def _runs(text: str, ad: str, n: int = AD_RUN_WORDS) -> bool:
    """Does `text` hold n words in a row that the ad holds too?"""
    words, hay = norm_literal(re.sub(r"[^\w\s'-]", " ", text)).split(), " " + " ".join(norm_literal(re.sub(r"[^\w\s'-]", " ", ad)).split()) + " "
    return any(" " + " ".join(words[i:i + n]) + " " in hay for i in range(len(words) - n + 1))


@dataclass
class Guard:
    fb: Mapping[str, Any]
    kws: list[Keyword]
    deny: list[str]
    avoid: list[str]
    company: str
    title: str
    ad: str
    tools: list[str]

    @classmethod
    def build(cls, fb, kws, avoid, company, title, ad) -> "Guard":
        tools = set(gap_terms()) | set(n for n in letters_mod._tools(fb))            # type: ignore[attr-defined]  (the bank's own skill and stack names)
        return cls(fb, kws, list(fb.get("deny_terms", [])), list(avoid), company, title, ad, sorted((t for t in tools if len(t) >= 3), key=len, reverse=True))

    def problems(self, text: str, plan: Plan, limit: Limit) -> list[str]:
        t = (text or "").strip()
        if not t:
            return ["the draft is empty"]
        out = list(limit.problems(t))
        why = plan.cls in ("why_company", "why_role")
        allowed = plan.allowed() + (f" {self.ad} {self.company} {self.title}" if why else "")
        out += faithfulness.check(t, allowed, [], self.deny, self.kws, vocabulary="", max_growth=1000.0).violations
        for w in self.avoid:
            if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", t, re.I):
                out.append(f"uses '{w}', a word the style forbids")
        if not _FIRST_PERSON.search(t):
            out.append("is not in the first person (no I / my / me)")
        lit, allowed_lit, corpus = norm_literal(t), norm_literal(allowed), norm_literal(_fact_corpus(self.fb))
        for tool in self.tools:
            if _contains(lit, tool):
                if not _contains(allowed_lit, tool):
                    out.append(f"mentions '{tool}', which the chosen entries do not")
                elif why and not _contains(corpus, tool) and not _contains(norm_literal(f"{self.company} {self.title}"), tool):
                    out.append(f"mentions '{tool}', which the fact bank does not")
        for sent in split_sentences(t):
            names = _NAME.findall(sent)
            for w in names[1:]:                                                   # the first word of a sentence is capitalised anyway
                if not w[0].isupper() or w == "I" or w.startswith(("I'", "I’")):
                    continue
                base = re.sub(r"['’]s$", "", w)
                if not all(_contains(allowed_lit, norm_literal(p)) or (p.endswith("s") and _contains(allowed_lit, norm_literal(p[:-1])))
                           for p in re.split(r"[-'’]", base) if p):
                    out.append(f"names '{w}', which is not in the chosen entries")
        if plan.cls in ("why_company", "why_role"):
            if not _runs(t, self.ad):
                out.append(f"claims nothing from the ad: a claim about the company or the role must be {AD_RUN_WORDS}+ words the ad contains")
            elif plan.cls == "why_company" and self.company:
                for sent in split_sentences(t):
                    if answers_mod.norm(self.company) in answers_mod.norm(sent) and not _runs(sent, self.ad):
                        out.append(f"says something about {self.company} that is not a phrase of the ad: '{sent[:70]}'")
        out += letters_mod.claim_problems([t], self.company, self.fb)             # no "we" / "our", no "I help <company>"
        out += term_dump_problems([t], self.fb)                                    # no pasted list of terms
        return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the model call
SYSTEM = (
    "You write ONE answer to a question on a job application form, for the applicant, in the first person. Use ONLY the SOURCES given: never "
    "name a tool, number, employer, organisation, project or outcome that is not in them, and add no adjective the sources do not earn. Past "
    "tense for past work. Plain prose, no list, no headings. Never write 'we' or 'our'. Do not use any word listed under style.avoid. Stay inside "
    "the limit. For a question about the company or the role, say something about the company or role ONLY by quoting words that are in "
    "AD_SNIPPETS (the same words, in a row), and say why the applicant cares using the MOTIVATIONS or the SOURCES. If PROBLEMS is given, your "
    "last draft was rejected for them: write it again without them. Return JSON only: {\"answer\": \"...\", \"ad_phrases\": [\"exact words you "
    "quoted from AD_SNIPPETS\"]}.")


def _user_prompt(ctx: Mapping[str, Any]) -> str:
    return json.dumps({k: ctx[k] for k in ("question", "class", "limit", "style", "company", "title", "sources", "motivations", "ad_snippets", "problems")
                       if ctx.get(k)}, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ one job's free-text answers
class FreeText:
    def __init__(self, llm, fb: Mapping[str, Any], library: Library, job: Mapping[str, Any], rep: Mapping[str, Any], book: answers_mod.AnswerBook,
                 ask: Callable[[str], str] = input, out: Callable[[str], None] = print, today: date | None = None):
        job = dict(job)                                  # a database row has no .get
        self.llm, self.fb, self.lib, self.job, self.book = llm, fb, library, job, book
        self.ask, self.out, self.today = ask, out, today or date.today()
        self.company, self.title, self.ad = str(job.get("company") or ""), str(job.get("title") or ""), str(job.get("description") or "")
        self.kws = [Keyword(**{k: v for k, v in kw.items() if k in Keyword.__dataclass_fields__}) for kw in (rep.get("keywords") or [])]
        self.hook = rep.get("hook") or {}
        self.style = dict(book.style or {})
        self.guard = Guard.build(fb, self.kws, self.style.get("avoid") or [], self.company, self.title, self.ad)
        self.last = ""                                  # how the last answer came about: reused | approved | edited | written | ""

    # ---- the plan
    def plan(self, cls: str, question: str) -> Plan:
        if cls in ("project", "challenge"):
            return self._plan_bullets(cls, question)
        snippets = ad_snippets(self.ad, self.kws, question, self.company)
        ad_src = Source("ad", " ".join(snippets))
        if cls == "why_company":
            mot = [Source(f"m{i}", m) for i, m in enumerate(self.book.motivations, 1)]
            return Plan(cls, [ad_src, *mot[:MAX_PLAN + 1]], f"the job ad and {len(mot)} motivation(s) of yours", snippets)
        hook_text = " ".join(str(self.hook.get(k) or "") for k in ("problem", "profile_line", "headline"))
        srcs = [ad_src, Source("hook", hook_text)] if hook_text.strip() else [ad_src]
        proof = self.hook.get("proof_bullet_id")
        for sec in ("experience", "projects"):
            for e in self.fb.get(sec, []):
                srcs += [bullet_source(e, b) for b in e["bullets"] if b["id"] == proof]
        return Plan(cls, srcs, "the job ad and the hook used on this job's CV" + (f" (proof {proof})" if proof else ""), snippets)

    def _plan_bullets(self, cls: str, question: str) -> Plan:
        entries = [(e, b) for sec in ("experience", "projects") for e in self.fb.get(sec, []) for b in e["bullets"]]
        pool = [(e, b) for e, b in entries if e.get("situation") or b.get("result")] if cls == "challenge" else entries
        pool = pool or entries
        kws = self.kws + [Keyword(w, 2.0, False) for w in sorted(_tokens(question)) if len(w) >= 3]
        score = {b["id"]: sum(kws[i].weight for i in keyword_hits(b["text"] + " " + " ".join(b.get("terms", [])), kws)) for _, b in pool}
        proof = str(self.hook.get("proof_bullet_id") or "")
        order = sorted(pool, key=lambda eb: -score[eb[1]["id"]])
        top = score[order[0][1]["id"]] if order else 0
        chosen = [eb for eb in pool if eb[1]["id"] == proof][:1]
        for eb in order:
            if len(chosen) >= MAX_PLAN:
                break
            if eb not in chosen and score[eb[1]["id"]] > 0 and score[eb[1]["id"]] >= 0.4 * top:
                chosen.append(eb)
        if not chosen:
            return Plan(cls, [], "no fact-bank entry overlaps this question")
        why = f"entries {', '.join(b['id'] for _, b in chosen)} overlap the question and the ad's keywords" + (
            f"; the hook proof {proof} of this job's CV goes first" if proof and chosen[0][1]["id"] == proof else "")
        return Plan(cls, [bullet_source(e, b) for e, b in chosen], why)

    # ---- the draft
    def _context(self, question: str, plan: Plan, limit: Limit, problems: list[str]) -> dict:
        return {"question": question, "class": plan.cls, "limit": limit.as_dict(), "company": self.company, "title": self.title,
                "style": {"avoid": list(self.style.get("avoid") or []), "person": self.style.get("person", "first"), "voice": self.style.get("voice", "")},
                "sources": [{"id": s.id, "text": s.text} for s in plan.sources], "ad_snippets": plan.ad_snippets,
                "motivations": list(self.book.motivations) if plan.cls == "why_company" else [], "problems": problems}

    def _draft(self, question: str, plan: Plan, limit: Limit) -> tuple[str, list[str]]:
        """(the first draft that passes the guard, []) or ("", the problems of the second try): one redraft, with the problems named."""
        problems: list[str] = []
        for _ in range(2):
            ctx = self._context(question, plan, limit, problems)
            try:
                reply = normalise_json(self.llm.json(TASK, SYSTEM, _user_prompt(ctx), context=ctx))
            except Exception as e:  # noqa: BLE001  (no budget, no network, a cut-off reply: the person writes it)
                self.out(f"  free-text draft unavailable: {e}")
                return "", [f"the model could not draft it: {e}"]
            text = str((reply or {}).get("answer") or "").strip()
            problems = self.guard.problems(text, plan, limit)
            for ph in (reply or {}).get("ad_phrases") or []:
                if plan.cls in ("why_company", "why_role") and norm_literal(str(ph)) not in " ".join(norm_literal(self.ad).split()):
                    problems.append(f"quotes '{str(ph)[:60]}' as a phrase of the ad, which the ad does not contain")
            if not problems:
                return text, []
        return "", problems

    # ---- the person
    def _show(self, question: str, cls: str, limit: Limit, plan: Plan, text: str) -> None:
        self.out(f"\n  FREE-TEXT QUESTION [{cls}]: {question}\n  Limit: {limit.describe()}\n  Plan: {plan.why}\n  Sources: {', '.join(plan.ids)}\n"
                 f"  Guard: passed (numbers, tools, organisations and names are all in the sources; limit, style and letter rules kept)\n"
                 f"  Draft:\n    {text}")

    def _store(self, question: str, text: str, cls: str, plan: Plan, origin: str) -> None:
        self.lib.add(Record.new(question, text, cls, self.company, str(self.job.get("id") or ""), plan.ids, self.today, origin))
        self.last = {"draft": "approved"}.get(origin, origin)

    def _write(self, question: str, cls: str, plan: Plan, limit: Limit, strict: bool, why: str) -> str:
        if why:
            self.out("  " + why)
        if not strict:
            self.out("  Left for you to write in the browser.")
            return ""
        typed = (self.ask("  Your answer for this form (blank = stop here; nothing is guessed): ") or "").strip()
        if not typed:
            return ""
        for p in limit.problems(typed):
            self.out(f"  note: your text is over the limit ({p})")
        self._store(question, typed, cls, plan, "written")
        return typed

    def answer(self, question: str, label: str = "", maxlength: Any = None, *, strict: bool = True) -> str:
        """The text for a free-text field, '' when there is none (the form then needs you). `strict`: the run can ask you to type an answer
        (auto-apply, or the assist when you press s); not strict: the assist leaves the field for you to type in the browser."""
        self.last = ""
        question = " ".join(question.split())
        cls, limit = classify(question, self.company), read_limits(f"{question} {label}", maxlength)
        hit = self.lib.find(question, cls, self.company, limit, job_id=str(self.job.get("id") or ""))
        if hit is not None:
            self.last = "reused"
            self.out(f"  free text: reused the answer you approved on {hit.date} for \"{hit.question[:70]}\" ({cls})")
            return hit.answer
        empty = Plan(cls, [], "")
        if cls == "other":
            return self._write(question, cls, empty, limit, strict, f"free-text question of no kind a draft can answer ({question[:80]!r}): it is yours")
        if cls == "why_company" and not self.book.motivations:
            return self._write(question, cls, empty, limit, strict,
                               "answers.yaml has no `motivations`, so nothing may be said about why you want this company: add them, or write it yourself")
        plan = self.plan(cls, question)
        if not plan.sources:
            return self._write(question, cls, plan, limit, strict, f"free text [{cls}]: {plan.why}")
        text, problems = self._draft(question, plan, limit)
        redrafts = 0
        while True:
            if not text:
                return self._write(question, cls, plan, limit, strict,
                                   f"free text [{cls}]: no draft passed the guard ({'; '.join(problems[:4])}). Nothing is drafted for you.")
            self._show(question, cls, limit, plan, text)
            reply = (self.ask(MENU) or "").strip().lower()
            if reply in ("y", "yes"):
                self._store(question, text, cls, plan, "draft")
                return text
            if reply == "r" and redrafts < MAX_REDRAFTS:
                redrafts += 1
                text, problems = self._draft(question, plan, limit)
                continue
            if reply == "e":
                typed = (self.ask("  Your edited answer (replaces the draft; blank = back to the menu): ") or "").strip()
                if not typed:
                    continue
                over = limit.problems(typed)
                if over:
                    self.out(f"  too long for the form ({'; '.join(over)}): shorten it")
                    continue
                self._store(question, typed, cls, plan, "edited")
                return typed
            if reply == "s":
                return self._write(question, cls, plan, limit, strict, "")
            return ""                                                  # anything else is not an approval


def for_job(job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], llm, settings, book: answers_mod.AnswerBook, *,
            ask: Callable[[str], str] = input, out: Callable[[str], None] = print, today: date | None = None) -> FreeText:
    """The free-text helper of one job: its ad, its CV report (keywords, hook), the fact bank, the model and the answer library."""
    return FreeText(llm, fb, Library(settings.path("answers_library")), job, rep, book, ask=ask, out=out, today=today)
