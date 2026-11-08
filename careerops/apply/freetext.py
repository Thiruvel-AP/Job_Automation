"""Free-text answers to application-form questions ("Describe an LLM feature you've built", "Why {company}?").

The form loop (apply/formloop.py) hands every free-text field, required or not, that answers.yaml does not answer to `FreeText.propose` / `review` (`answer` = both):

  1. read the question and its limit ("1-2 sentences", "max 200 words", the field's maxlength; nothing stated = 120 words) and classify it
     (project | challenge | why_company | why_role | other);
  2. an answer YOU approved or wrote for a similar question (same class, similarity >= 0.85, still inside this question's limit) is reused as it
     is. "Why {company}?" is only ever reused for the same company; and an answer is not reused when a fact-bank entry (or motivation)
     it was built from has changed since you approved it;
  3. otherwise PLAN which fact-bank entries it may draw on (the ones whose terms best overlap the question and the ad's keywords; the hook proof
     of this job's CV first, so the answer and the CV agree), DRAFT it with one model call, and GUARD the draft as strictly as the CV: every
     number, tool, organisation and name must be in the chosen entries (for a "why" answer: the ad, your `motivations`, the CV's hook), no
     deny-listed term, no word of `style.avoid`, first person, the limit, the letter guard (no "we/our", no pasted list of terms), and a claim
     about the company must be a run of words the ad itself contains. A draft that fails is redrafted once with the problems named; a second
     failure goes to you with no draft;
  4. show it (question, limit, sources, guard result) and ask `[y] use · [e] edit · [r] redraft · [s] I'll write it`. Only `y`, a text you
     typed, or a text you edited is used, and is stored in the answer library (paths.answers_library) with approved: true.

"Why this role / company" questions (v2.4.1: "What motivated you to apply for this position?", "Why do you want to work here?", "What excites you
...", "Why are you applying?", "Why {company}?") are always drafted, 60-150 words (or the field's smaller limit), first person, no greeting: from the
letter's P1 (the company's problem plus the proof) and P5 (why this team) when this job has a letter, else from the job ad, your `motivations` and one
CV bullet with a number. The job ad is required: under 150 words it is fetched once more, and still short = hand-off with the reason "no JD" and
nothing drafted. The draft must name the company, hold a run of words of the ad, pass the claim guard, and not reuse an earlier motivation answer
(5-word shingles, overlap under 0.30).

v3.4, with a letter: the answer reuses no sentence of this job's letter (5-word shingles: Jaccard under 0.30 against the letter, and no sentence
with half of its shingles in it). It is a FLUENCY rewrite first (one model call, two tries): the model gets the facts (the code-built answer below and
nothing else) and the ad snippets, never the letter; the rewrite must pass tailoring/fluency.py's checks (every
number, name and tool in the facts or the ad, no sentence over 35 words, no repeated sentence, no banned opening such as "I am applying") and every
check below. Else the code-built answer: "The <role> role at <Company> stands out to me for its focus on <a noun phrase of the ad>.", the hook's
proof bullet in first person (when the letter does not print it) and your `motivations`, 60-150 words. Else the model draft as before.

A new draft is therefore never submitted without your `y` (or an edit you saved). Nothing here writes to answers.yaml, fact_bank.yaml
or applicant.yaml: the library is runtime data. "Other" questions (hobbies, "anything else?") are never drafted: they are asked.

Two entry points. `answer` is the old one-call form (propose, then ask). The form loop (apply/formloop.py) calls `propose` for every free-text field
of a form, required or not, puts the result in the field at once (a NEW DRAFT), and calls `review` for each new draft before anything is submitted.
`propose` also reads what the question ASKS for (a compound such as "non-engineering", a name such as "Salesforce", the words after "with" / "for" /
"in") and checks each against the whole fact bank: a thing the bank never mentions is NOT claimed (the guard rejects a draft that mentions it) and the
proposal carries `WEAK EVIDENCE: <what was asked> vs <entry ids>`; a question that overlaps no entry at all is answered from the closest true entry.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..tailoring import faithfulness, fluency as fluency_mod, letter_reuse, letters as letters_mod
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
MENU = "  [y] use · [l] regenerate with feedback · [e] edit · [r] redraft · [s] I'll write it  > "
JOB_SOURCES = ("ad", "hook", "letter")      # sources of a plan that belong to the job, not to you: only the rest are checked for change
WHY = ("why_company", "why_role")
MOTIVATION_WORDS = (60, 150)      # a motivation answer: at least / at most this many words (a smaller limit on the field wins at the top)
MIN_JD_WORDS = 150                # the ad is a required input of a motivation answer
REUSE_LIMIT = letter_reuse.LIMIT  # 5-word-shingle overlap with an earlier motivation answer must stay under this
_GREETING = re.compile(r"^\s*(?:hi|hello|dear|hey|greetings)\b|\bi(?:'m| am) writing\b|\bi(?: would|'d) like to apply\b", re.I)


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
    why = re.search(r"\bwhy\b|what (?:attracts|draws|drew|interests|interested|excites|excited|motivates|motivated|inspires|inspired|prompted|made) you|"
                    r"what is it about|what are your reasons|how did you become interested", q)
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
more most very part way powered based driven enabled""".split())
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


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jaccard, SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() if ta and tb and jaccard >= 0.5 else 0.0)


def _head(question: str) -> str:
    """The first sentence of a question: what is asked, before the form elaborates ("... shipped. What did it do and how did you know?")."""
    return re.split(r"(?<=[.?!\u2026])\s+", " ".join(question.split()), maxsplit=1)[0].rstrip(".\u2026 ")


def similarity(a: str, b: str) -> float:
    """0..1: the share of meaningful words two questions have in common once synonyms ("shipped", "built", "deployed") are one word. A question
    that goes on after its first sentence is also compared by that sentence alone (at least 3 meaningful words), the better of the two counts."""
    whole = _similar(a, b)
    ha, hb = _head(a), _head(b)
    if (ha, hb) != (a.strip(), b.strip()) and len(_tokens(ha)) >= 3 and len(_tokens(hb)) >= 3:
        return max(whole, _similar(ha, hb))
    return whole


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
    digests: dict[str, str] = field(default_factory=dict)       # sha256 of each of your sources (fact-bank entry, motivation) when approved

    @classmethod
    def new(cls, question: str, answer: str, kind: str, company: str, job_id: str, source_ids: list[str], day: date,
            origin: str = "draft", digests: Mapping[str, str] | None = None) -> "Record":
        return cls(question, normalise(question), answer, company, job_id, list(source_ids), day.isoformat(), True, kind, origin,
                   dict(digests or {}))

    def stale(self, current: Mapping[str, str]) -> bool:
        """Built from a fact-bank entry (or motivation) that has changed since approval, is gone, or whose state was never recorded?
        `current` = id -> digest of every source of yours as it is now. An answer with no such source (a seeded one) is never stale."""
        return any(i not in self.digests or current.get(i) != self.digests[i] for i in self.source_ids if i not in JOB_SOURCES)


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

    def find(self, question: str, cls: str, company: str, limit: Limit, threshold: float = SIMILAR, job_id: str = "",
             current: Mapping[str, str] | None = None) -> Record | None:
        """The approved answer for a question like this one: same class, similarity >= threshold, inside `limit`; the best, the latest on a tie.
        A why_company answer is only found for the same company (and then any "why this company" wording is the same question), a why_role
        answer only for the same job: both quote that employer's ad. An answer built from a source of yours that has changed since approval
        is skipped (`current`: id -> digest now; none given = nothing is known to be unchanged)."""
        best: tuple[float, int, Record] | None = None
        for n, r in enumerate(self.records()):
            if r.cls != cls or (cls == "why_company" and answers_mod.norm(r.company) != answers_mod.norm(company)) or \
                    (cls == "why_role" and (not job_id or r.job_id != job_id)):
                continue
            if r.stale(current or {}):
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

    fallback: bool = False                 # no entry overlapped the question: the sources are the closest true entry, not a match
    motivation: bool = False               # a "why this role / company" answer: the motivation rules (length, company name, no greeting) apply

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.sources]

    def allowed(self) -> str:
        return " ".join(s.text + " " + " ".join(s.terms) for s in self.sources)

    def digests(self) -> dict[str, str]:
        """Digest of each source of yours this answer is built from (the ad and the hook are this job's: not kept)."""
        return {s.id: source_digest(s) for s in self.sources if s.id not in JOB_SOURCES}


def source_digest(s: Source) -> str:
    return hashlib.sha256("\n".join([s.id, s.text, *map(str, s.terms)]).encode("utf-8")).hexdigest()


def bullet_source(entry: Mapping[str, Any], b: Mapping[str, Any]) -> Source:
    """A fact-bank bullet with the entry it belongs to: title, employer or project name, situation, stack, the bullet and its result."""
    head = str(entry.get("title") or entry.get("name") or "")
    org = str(entry.get("org") or "")
    parts = [head + (f", {org}" if org and org != head else "") + ".", str(entry.get("situation") or ""), str(b["text"]), str(b.get("result") or ""),
             f"Stack: {entry['stack']}." if entry.get("stack") else ""]
    return Source(str(b["id"]), " ".join(p for p in parts if p.strip()), [str(t) for t in b.get("terms", [])])


def _fact_corpus(fb: Mapping[str, Any]) -> str:
    from ..tailoring.profile import bank_sentences

    parts = list(bank_sentences(fb))
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
# A sentence that says what the user did or used ("I used X", "I've built Y", "my experience with Z"): a claim about them.
_CLAIM = re.compile(r"\bI(?:'ve|'d| have| had)?\s+(?:\w+\s+){0,2}?(?:used|built|worked|deployed|shipped|developed|wrote|written|trained|ran|run|"
                    r"managed|led|designed|implemented|created|delivered|maintained|configured|tested|migrated|measured|achieved|reduced|"
                    r"improved|applied|owned|launched|automated|analysed|analyzed)\b|\bmy\s+(?:experience|work|background|projects?)\b", re.I)


def _runs(text: str, ad: str, n: int = AD_RUN_WORDS) -> bool:
    """Does `text` hold n words in a row that the ad holds too?"""
    words, hay = norm_literal(re.sub(r"[^\w\s'-]", " ", text)).split(), " " + " ".join(norm_literal(re.sub(r"[^\w\s'-]", " ", ad)).split()) + " "
    return any(" " + " ".join(words[i:i + n]) + " " in hay for i in range(len(words) - n + 1))


# ------------------------------------------------------------------ what a question asks for that the bank may not have
ASK_PREPS = frozenset("with for using in within across alongside between among from on around".split())
FRAMING = frozenset("""tell describe walk talk give share example examples proud recent time times took take taken lead led setting context environment
situation team role company organisation organization work worked working experience project projects thing things way ways one""".split())
MODIFIERS = ("powered", "based", "driven", "enabled", "led", "facing", "related", "oriented", "native")      # "LLM-powered" is a kind of system, not an ask
_GENERIC = frozenset(w for w, canon in _SAME.items() if canon in ("build", "system", "hard"))      # agent, tool, feature, project, shipped, built ...
_ALPHA = r"[A-Za-z][A-Za-z']*"


def _prefix(term: str) -> str:
    t = norm_literal(term)
    return t if len(t) <= 4 else t[:max(4, len(t) - 3)]


def mentions(text: str, term: str) -> bool:
    """Does the text say `term` (a compound, or a word with its endings: "stakeholder" ~ "stakeholders")?"""
    t, hay = norm_literal(term), norm_literal(text)
    if "-" in t:
        return _contains(hay, t) or _contains(hay, t.replace("-", " "))
    return re.search(r"(?<![\w])" + re.escape(_prefix(t)), hay) is not None


def asks(question: str, company: str = "") -> list[tuple[str, list[str]]]:
    """(phrase, terms) of what a question asks about beyond its kind: a hyphenated compound ("non-engineering"), a name written with a capital
    inside the sentence ("Salesforce"), and the words after with / for / using / in / from ... up to the next small word ("sales stakeholders").
    The framing words of a question (describe, tell us, project, built, shipped, agent ...) are not asks."""
    q = re.sub(r"\([^)]*\)", " ", question.replace("\u2019", "'"))
    skip = {w for w in _tokens(company)} | _GENERIC | FRAMING
    found: list[tuple[str, list[str]]] = []

    def add(phrase: str, terms: list[str]) -> None:
        terms = [t for t in terms if t and t not in skip and t not in _STOP and len(t) >= 3]
        if terms and phrase.lower() not in [p.lower() for p, _ in found]:
            found.append((phrase, list(dict.fromkeys(terms))))

    words = re.findall(_ALPHA + r"(?:-" + _ALPHA + r")*|[.?!;:,]", q)
    for i, w in enumerate(words):                                                     # 1. the words after with / for / in ... up to the next small word
        if w.lower() in ASK_PREPS:
            run: list[str] = []
            for nxt in words[i + 1:i + 5]:
                ln = nxt.lower()
                if not nxt[0].isalpha():
                    break
                if ln in ASK_PREPS or ln in FRAMING or ln in _GENERIC or ln in _STOP:
                    if run:
                        break
                    continue
                run.append(ln)
            if run:
                add(" ".join(run), run)
    for i, w in enumerate(words):                                                     # 2. a compound or a name that is not in one of those phrases
        lw = w.lower()
        if any(lw in terms for _, terms in found):
            continue
        if "-" in lw:
            if not lw.endswith(tuple("-" + m for m in MODIFIERS)):
                add(lw, [lw])
        elif i and words[i - 1] not in (".", "?", "!") and w[0].isupper() and w != "I" and not w.startswith("I'"):
            add(w, [lw])                                                              # a name inside the sentence
    return found


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

    def problems(self, text: str, plan: Plan, limit: Limit, missing: Sequence[str] = ()) -> list[str]:
        t = (text or "").strip()
        if not t:
            return ["the draft is empty"]
        out = list(limit.problems(t))
        out += [f"claims '{m}', which no fact-bank entry mentions" for m in missing if mentions(t, m)]
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
        mine = ""                                                                  # what the user may be said to have done or used
        if why:                                                                    # the ad is allowed for the company and the role, never for the user
            mine = " ".join([_fact_corpus(self.fb), *(f"{s.text} {' '.join(s.terms)}" for s in plan.sources if s.id != "ad"), self.company, self.title])
            for sent in split_sentences(t):
                if _CLAIM.search(sent):
                    out += faithfulness.check(sent, mine, [], self.deny, self.kws, vocabulary="", max_growth=1000.0).violations
        mine_lit = norm_literal(mine)
        for sent in split_sentences(t):
            pool = mine_lit if why and _CLAIM.search(sent) else allowed_lit
            names = _NAME.findall(sent)
            for w in names[1:]:                                                   # the first word of a sentence is capitalised anyway
                if not w[0].isupper() or w == "I" or w.startswith(("I'", "I’")):
                    continue
                base = re.sub(r"['’]s$", "", w)
                if not all(_contains(pool, norm_literal(p)) or (p.endswith("s") and _contains(pool, norm_literal(p[:-1])))
                           for p in re.split(r"[-'’]", base) if p):
                    out.append(f"names '{w}', which is not in the chosen entries")
        if plan.motivation:
            n_words, lo, hi = len(t.split()), MOTIVATION_WORDS[0], MOTIVATION_WORDS[1]
            if limit.words is not None:
                hi = min(hi, limit.words)
            lo = min(lo, hi)
            if n_words < lo:
                out.append(f"{n_words} words: a motivation answer is at least {lo} words")
            if n_words > hi:
                out.append(f"{n_words} words: a motivation answer is at most {hi} words")
            if self.company and answers_mod.norm(self.company) not in answers_mod.norm(t):
                out.append(f"does not name the company ({self.company})")
            if _GREETING.search(t) or fluency_mod.BANNED_OPENINGS.search(t):
                out.append("opens with a greeting, 'I am writing' or 'I am applying': it starts with the reason")
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
    "AD_SNIPPETS (the same words, in a row), and say why the applicant cares using the MOTIVATIONS or the SOURCES. If NOT_IN_BANK is given, the "
    "applicant has no recorded experience of those things: do not mention or imply them; describe the closest true thing in the SOURCES instead. "
    "If PROBLEMS is given, your last draft was rejected for them: write it again without them. Return JSON only: {\"answer\": \"...\", \"ad_phrases\": [\"exact words you "
    "quoted from AD_SNIPPETS\"]}. If WORDS is given, write between those two numbers of words, name the company once, quote one phrase of the ad, "
    "start with the reason (no greeting, never 'I am writing').")


FLUENCY_SYSTEM = (
    "You write the applicant's answer to a job application question about why they want this role, in the first person, as natural, fluent "
    "prose. FACTS are the only source of what the applicant did, used, built or measured; AD_SNIPPETS are the only source of what is said about "
    "the company or the role (never a source for what the applicant did or knows). Rewrite the FACTS into a fluent answer, in your own words. "
    "Rules, all checked by code: between WORDS[0] and WORDS[1] words; name the company once; copy a run of at "
    "least 4 consecutive words from AD_SNIPPETS; no sentence over 35 words; say each point once; every number, name and tool must be in FACTS "
    "(or, for the company, in AD_SNIPPETS); never 'we' or 'our'; start with the reason: never a greeting, 'I am writing', 'I am applying' or "
    "'I am excited'; no word listed under style.avoid. If PROBLEMS is given, LAST_ANSWER was rejected for them: fix exactly those problems in "
    "LAST_ANSWER and change nothing else. Return JSON only: {\"answer\": \"...\"}.")


def _user_prompt(ctx: Mapping[str, Any]) -> str:
    return json.dumps({k: ctx[k] for k in ("question", "class", "limit", "style", "company", "title", "sources", "motivations", "ad_snippets", "not_in_bank",
                                           "problems", "words")
                       if ctx.get(k)}, ensure_ascii=False, indent=1)


@dataclass
class Proposal:
    """What `FreeText.propose` found for one question: an approved answer to reuse (`origin == "reused"`), a guarded draft (`"draft"`), or
    nothing (`origin == ""`, `why_not` says why and who writes it)."""
    question: str
    cls: str
    limit: Limit
    plan: Plan
    text: str = ""
    origin: str = ""
    problems: list[str] = field(default_factory=list)
    why_not: str = ""
    weak: str = ""                                      # "WEAK EVIDENCE: <what was asked> vs <entry ids>", or ""
    missing: list[str] = field(default_factory=list)    # the terms the bank never mentions: the draft may not claim them
    reused: Record | None = None
    handoff: str = ""                                   # set when the job must be handed off (no JD to draft a motivation answer from): the reason


# ------------------------------------------------------------------ one job's free-text answers
class FreeText:
    def __init__(self, llm, fb: Mapping[str, Any], library: Library, job: Mapping[str, Any], rep: Mapping[str, Any], book: answers_mod.AnswerBook,
                 ask: Callable[[str], str] = input, out: Callable[[str], None] = print, today: date | None = None,
                 refetch_jd: Callable[[], str] | None = None, settings=None):
        job = dict(job)                                  # a database row has no .get
        self.llm, self.fb, self.lib, self.job, self.book = llm, fb, library, job, book
        self.ask, self.out, self.today = ask, out, today or date.today()
        self.company, self.title, self.ad = str(job.get("company") or ""), str(job.get("title") or ""), str(job.get("description") or "")
        self.kws = [Keyword(**{k: v for k, v in kw.items() if k in Keyword.__dataclass_fields__}) for kw in (rep.get("keywords") or [])]
        self.hook = rep.get("hook") or {}
        self.style = dict(book.style or {})
        self.guard = Guard.build(fb, self.kws, self.style.get("avoid") or [], self.company, self.title, self.ad)
        self.last = ""                                  # how the last answer came about: reused | approved | edited | written | ""
        self.refetch_jd, self._refetched = refetch_jd, False       # one more fetch of the ad when it is too short for a motivation answer
        self.motivation_log: list[dict] = []            # every motivation draft: question, text, words, guard result, sources, overlap (the dry-run record)
        self.letter_parts: dict[str, str] = {}
        self.letter_text = ""
        self.letter_body = ""                           # the whole letter as the reader sees it: the motivation answer may not copy it (v3.4)
        self.letter_ids: set[str] = set()               # the bullets the letter prints
        self._settings = settings                       # letter.fluency decides whether the motivation answer is rewritten for fluency
        self.fluency_info: dict = {"source": "off"}
        self._rep, self._current_q = rep, ""
        self.use_letter(rep)

    def use_letter(self, rep: Mapping[str, Any]) -> None:
        """This job's letter (also one written after the helper was made, v2.5: before the browser opens): the motivation answer draws on its P1, P5 and
        the first sentence of P4 (one role, one number)."""
        letter = rep.get("letter") or {}
        final = letter.get("parts") or {}
        parts = letter.get("assembled_parts") or final                          # the fact source: the assembled draft (v3.4), else the letter
        if letter.get("status") == "ok":
            self.letter_body = " ".join(str(final.get(n) or "") for n in ("P1", "P2", "P3", "P4", "P5", "P6", "P7") if final.get(n)).strip()
            self.letter_ids = {str(e.get("id")) for e in letter.get("evidence") or [] if isinstance(e, dict)}
            p4 = split_sentences(str(parts.get("P4") or ""))
            self.letter_parts = {"P1": str(parts.get("P1") or "").strip(), "P5": str(parts.get("P5") or "").strip(), "P4": (p4[0].strip() if p4 else "")}
            self.letter_text = " ".join(v for v in (self.letter_parts["P1"], self.letter_parts["P5"], self.letter_parts["P4"]) if v).strip()
            self._rep = rep

    def _proof(self) -> Source | None:
        """The bullet the code-built answer tells: the hook's proof bullet, else a numbered bullet, that this job's letter does not print."""
        proof = str(self.hook.get("proof_bullet_id") or "")
        pool = [(e, b) for sec in ("experience", "projects") for e in self.fb.get(sec, []) for b in e["bullets"] if str(b["id"]) not in self.letter_ids]
        order = sorted(pool, key=lambda eb: (str(eb[1]["id"]) != proof, not re.search(r"\d", eb[1]["text"])))
        return bullet_source(*order[0]) if order and (str(order[0][1]["id"]) == proof or re.search(r"\d", order[0][1]["text"])) else None

    def assembled_motivations(self, limit: Limit) -> list[str]:
        """Every code-built motivation answer that fits, best first (v3.4: none takes a sentence from the letter): "The <role> role at <Company>
        stands out to me for its focus on <a noun phrase of the ad>." (one per noun phrase, those the letter does not use first), the proof
        bullet in first person, then your `motivations`, each added while the answer stays inside the limit; 60-150 words."""
        if not self.letter_parts:
            return []
        from ..tailoring import assembled_letter as AL
        from ..tailoring.company_letter import about_sentences

        cap = min(MOTIVATION_WORDS[1], limit.words or MOTIVATION_WORDS[1])
        said = norm_literal(self.letter_body)
        phrases = AL.noun_phrases(about_sentences(self.ad, self.company, self.kws), self.company)
        phrases = sorted(phrases, key=lambda p: norm_literal(p) in said)
        proof = self._proof()
        told = [AL.first_person(b["text"]) for sec in ("experience", "projects") for e in self.fb.get(sec, []) for b in e["bullets"]
                if proof is not None and str(b["id"]) == proof.id][:1]
        rest = told + list(self.book.motivations)
        out = []
        for phrase in phrases:
            text = f"The {self.title} role at {self.company} stands out to me for its focus on {phrase}."
            for bit in rest:
                if len(f"{text} {bit}".split()) <= cap:
                    text = f"{text} {bit}"
            if MOTIVATION_WORDS[0] <= len(text.split()) <= cap and text not in out:
                out.append(text)
        return out

    def assembled_motivation(self, limit: Limit) -> str:
        """The first code-built motivation answer (assembled_motivations), '' when there is no letter or none fits."""
        return next(iter(self.assembled_motivations(limit)), "")

    def _current(self) -> dict[str, str]:
        """id -> digest of every fact-bank bullet (with the entry it sits in) and motivation of yours as they are now."""
        now = {str(b["id"]): source_digest(bullet_source(e, b)) for sec in ("experience", "projects") for e in self.fb.get(sec, [])
               for b in e["bullets"]}
        now.update({f"m{i}": source_digest(Source(f"m{i}", m)) for i, m in enumerate(self.book.motivations, 1)})
        return now

    # ---- the plan
    def plan(self, cls: str, question: str) -> Plan:
        if cls in ("project", "challenge"):
            return self._plan_bullets(cls, question)
        snippets = ad_snippets(self.ad, self.kws, question, self.company)
        ad_src = Source("ad", " ".join(snippets))
        if cls in WHY:
            return self._plan_motivation(cls, ad_src, snippets)
        hook_text = " ".join(str(self.hook.get(k) or "") for k in ("problem", "profile_line", "headline"))
        srcs = [ad_src, Source("hook", hook_text)] if hook_text.strip() else [ad_src]
        proof = self.hook.get("proof_bullet_id")
        for sec in ("experience", "projects"):
            for e in self.fb.get(sec, []):
                srcs += [bullet_source(e, b) for b in e["bullets"] if b["id"] == proof]
        return Plan(cls, srcs, "the job ad and the hook used on this job's CV" + (f" (proof {proof})" if proof else ""), snippets)

    def _numbered_bullet(self) -> Source | None:
        """One CV bullet with a number: the hook's proof bullet when it has one, else the first numbered bullet of this job's CV, else of the bank."""
        proof = str(self.hook.get("proof_bullet_id") or "")
        chosen = {i for ids in (self._rep.get("selection") or {}).values() for i in ids} if isinstance(self._rep.get("selection"), dict) else set()
        pool = [(e, b) for sec in ("experience", "projects") for e in self.fb.get(sec, []) for b in e["bullets"] if re.search(r"\d", b["text"])]
        order = sorted(pool, key=lambda eb: (eb[1]["id"] != proof, eb[1]["id"] not in chosen))
        return bullet_source(*order[0]) if order else None

    def _plan_motivation(self, cls: str, ad_src: Source, snippets: list[str]) -> Plan:
        """The sources of a "why this role / company" answer, in this order: the letter's P1 (the company's problem plus the proof) and P5 (why this team)
        when this job has a letter; otherwise the job ad, your `motivations` and one CV bullet with a number."""
        mot = [Source(f"m{i}", m) for i, m in enumerate(self.book.motivations, 1)]
        if self.letter_text:
            proof = self._proof()
            return Plan(cls, [ad_src, Source("letter", self.letter_text), *([proof] if proof else []), *mot[:MAX_PLAN + 1]],
                        "the job ad, this job's letter (P1, P5, one P4 sentence)" + (f", bullet {proof.id}" if proof else "")
                        + f" and {len(mot[:MAX_PLAN + 1])} motivation(s) of yours", snippets, motivation=True)
        bullet = self._numbered_bullet()
        hook_text = " ".join(str(self.hook.get(k) or "") for k in ("problem", "profile_line", "headline")).strip()      # what this job's CV says first
        why = f"the job ad, {len(mot)} motivation(s) of yours" + (f" and CV bullet {bullet.id}" if bullet else "")
        return Plan(cls, [ad_src, *([Source("hook", hook_text)] if hook_text else []), *mot[:MAX_PLAN + 1], *([bullet] if bullet else [])], why, snippets,
                    motivation=True)

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
            closest = [eb for eb in pool if eb[1]["id"] == proof][:1] or order[:1]          # the hook proof, else the best of a zero-score pool
            if not closest:
                return Plan(cls, [], "the fact bank has no entry to draw on")
            return Plan(cls, [bullet_source(e, b) for e, b in closest],
                        f"no fact-bank entry overlaps this question: the closest true entry is {closest[0][1]['id']}", fallback=True)
        why = f"entries {', '.join(b['id'] for _, b in chosen)} overlap the question and the ad's keywords" + (
            f"; the hook proof {proof} of this job's CV goes first" if proof and chosen[0][1]["id"] == proof else "")
        return Plan(cls, [bullet_source(e, b) for e, b in chosen], why)

    # ---- the draft
    def _context(self, question: str, plan: Plan, limit: Limit, problems: list[str], missing: Sequence[str] = ()) -> dict:
        return {"question": question, "class": plan.cls, "limit": limit.as_dict(), "company": self.company, "title": self.title,
                "style": {"avoid": list(self.style.get("avoid") or []), "person": self.style.get("person", "first"), "voice": self.style.get("voice", "")},
                "sources": [{"id": s.id, "text": s.text} for s in plan.sources], "ad_snippets": plan.ad_snippets,
                "motivations": list(self.book.motivations) if plan.cls in WHY and not self.letter_text else [], "not_in_bank": list(missing),
                "problems": problems, "words": list(MOTIVATION_WORDS) if plan.motivation else []}

    def _draft(self, question: str, plan: Plan, limit: Limit, missing: Sequence[str] = (), problems: list[str] | None = None,
               tries: int = 2) -> tuple[str, list[str]]:
        """(the first draft that passes the guard, []) or ("", the problems of the last try): `tries` model calls, each told the problems of the
        one before (and `problems`, those of an earlier pass)."""
        problems = list(problems or [])
        for _ in range(tries):
            ctx = self._context(question, plan, limit, problems, missing)
            try:
                reply = normalise_json(self.llm.json(TASK, SYSTEM, _user_prompt(ctx), context=ctx))
            except Exception as e:  # noqa: BLE001  (no budget, no network, a cut-off reply: the person writes it)
                self.out(f"  free-text draft unavailable: {e}")
                return "", [f"the model could not draft it: {e}"]
            text = str((reply or {}).get("answer") or "").strip()
            problems = self.guard.problems(text, plan, limit, missing)
            for ph in (reply or {}).get("ad_phrases") or []:
                if plan.cls in ("why_company", "why_role") and norm_literal(str(ph)) not in " ".join(norm_literal(self.ad).split()):
                    problems.append(f"quotes '{str(ph)[:60]}' as a phrase of the ad, which the ad does not contain")
            if not problems:
                return text, []
        return "", problems

    # ---- what the question asks for that the bank never mentions
    def _bank_text(self) -> str:
        if not hasattr(self, "_bank"):
            parts = [_fact_corpus(self.fb)]
            for sec in ("experience", "projects"):
                for e in self.fb.get(sec, []):
                    parts.append(" ".join(str(e.get(k) or "") for k in ("title", "name", "org", "situation", "stack")))
                    parts += [str(b.get("result") or "") for b in e["bullets"]]
            self._bank = norm_literal("\n".join(parts))
        return self._bank

    def unsupported(self, question: str) -> list[tuple[str, list[str]]]:
        """(phrase, terms) of what the question asks for that NO fact-bank entry mentions (the terms; the phrase is shown as asked)."""
        bank, out = self._bank_text(), []
        for phrase, terms in asks(question, self.company):
            gone = [t for t in terms if not mentions(bank, t)]
            if gone:
                out.append((phrase, gone))
        return out

    # ---- one pass: reuse, or plan + draft + check
    def propose(self, question: str, label: str = "", maxlength: Any = None, *, problems: list[str] | None = None, tries: int = 2) -> Proposal:
        """Retrieve and draft the answer to one free-text question, asking nobody. An approved answer for a similar question (same class, similarity
        >= 0.85) is `reused`; else the draft that passes the guard (`origin == "draft"`, to be approved); else no text and `why_not`."""
        question = " ".join(question.split())
        cls, limit = classify(question, self.company), read_limits(f"{question} {label}", maxlength)
        if cls in WHY and not limit.stated:
            limit = Limit(words=MOTIVATION_WORDS[1])                               # a motivation answer without a stated limit: 150 words, not the default 120
        hit = self.lib.find(question, cls, self.company, limit, job_id=str(self.job.get("id") or ""), current=self._current())
        empty = Plan(cls, [], "")
        if hit is not None:
            return Proposal(question, cls, limit, empty, hit.answer, "reused", reused=hit)
        if cls == "other":
            return Proposal(question, cls, limit, empty, why_not=f"free-text question of no kind a draft can answer ({question[:80]!r}): it is yours")
        if cls in WHY:
            gone = self._jd_problem()
            if gone:
                return Proposal(question, cls, limit, empty, why_not=gone, handoff=gone)
        if cls in WHY and not self.letter_text and not self.book.motivations:
            return Proposal(question, cls, limit, empty, why_not="answers.yaml has no `motivations` and this job has no letter, so nothing may be said "
                                                                 "about why you want this role: add them, or write it yourself")
        plan = self.plan(cls, question)
        if not plan.sources:
            return Proposal(question, cls, limit, plan, why_not=f"free text [{cls}]: {plan.why}")
        if cls in WHY and plan.motivation and self.letter_parts:                 # v3.4: the fluency rewrite, then the code-built answer
            cands = self.assembled_motivations(limit)
            if self._settings is not None and fluency_mod.enabled(self._settings):
                text, probs, info = self._fluent_motivation(question, plan, limit, cands[0] if cands else "")
                if text:
                    text, probs = self._reuse_checked(question, plan, limit, [], text, [], 0)
                    if text:
                        self.motivation_log[-1]["fluency"] = info
                        return Proposal(question, cls, limit, plan, text, "draft", [])
                self.fluency_info = info
            text, probs = "", ["no code-built answer fits the motivation limit"]
            for cand in cands:
                probs = self.guard.problems(cand, plan, limit) + self._text_problems(cand)
                if not probs:
                    text = cand
                    break
            if text:
                text, probs = self._reuse_checked(question, plan, limit, [], text, [], 0)
                if text:
                    self.motivation_log[-1].update(assembled=True, fluency=self.fluency_info)
                    return Proposal(question, cls, limit, plan, text, "draft", [])
        gaps = self.unsupported(question) if cls in ("project", "challenge") else []
        missing = [t for _, terms in gaps for t in terms]
        asked = "; ".join(p for p, _ in gaps) or (question[:80] if plan.fallback else "")
        weak = f"WEAK EVIDENCE: {asked} vs {', '.join(plan.ids)}" if gaps or plan.fallback else ""
        text, probs = self._draft(question, plan, limit, missing, problems, tries)
        if plan.motivation:
            text, probs = self._reuse_checked(question, plan, limit, missing, text, probs, tries)
        return Proposal(question, cls, limit, plan, text, "draft" if text else "", probs, weak=weak, missing=missing,
                        why_not="" if text else f"free text [{cls}]: no draft passed the guard ({'; '.join(probs[:4])}). Nothing is drafted for you.")

    # ---- a motivation answer: the ad is required, and the answer is its own
    def _jd_problem(self) -> str:
        """"" when the ad has MIN_JD_WORDS words or more; else it is fetched once more, and if it is still short the reason (hand-off: no JD)."""
        if len(self.ad.split()) >= MIN_JD_WORDS:
            return ""
        if self.refetch_jd is not None and not self._refetched:
            self._refetched = True
            try:
                fresh = str(self.refetch_jd() or "")
            except Exception as e:  # noqa: BLE001  (no network, a closed posting, a blocked host: the job is handed off)
                self.out(f"  the job ad could not be fetched again: {str(e)[:120]}")
                fresh = ""
            if len(fresh.split()) > len(self.ad.split()):
                self.ad = fresh
                self.guard.ad = fresh
        if len(self.ad.split()) >= MIN_JD_WORDS:
            return ""
        return (f"no JD: the job ad has {len(self.ad.split())} words (a motivation answer needs {MIN_JD_WORDS}+), even after fetching it again; "
                f"nothing is drafted from the title alone")

    def _text_problems(self, text: str) -> list[str]:
        """tailoring/fluency.py's (b) to (d) for a motivation answer, and its overlap with this job's letter."""
        one = {"answer": text}
        return (fluency_mod.length_problems(one) + fluency_mod.repeat_problems(one) + fluency_mod.opening_problems(text)
                + fluency_mod.unfinished_problems(one) + fluency_mod.grammar_problems(one)
                + fluency_mod.ad_paste_problems(one, self.ad) + fluency_mod.letter_overlap_problems(text, self.letter_body))

    def _fluent_motivation(self, question: str, plan: Plan, limit: Limit, built: str) -> tuple[str, list[str], dict]:
        """(text, problems, info): the model rewrites the facts (the code-built answer) into a motivation answer, fluency_mod.TRIES tries, each
        checked by (a) every number, name and tool in the facts or the ad, (b) to (d), the letter overlap and the guard (e). The letter is not
        sent (8 Nov 2026: the model took AstraZeneca and AWS Lambda from it); the overlap check reads it. info = {"source", "tries", "problems",
        "tokens"}."""
        facts = list(dict.fromkeys(f.strip() for f in split_sentences(built) if f.strip()))       # the code-built answer is the fact source
        info: dict = {"source": "fallback", "tries": 0, "problems": [], "tokens": 0}
        if not facts:
            return "", ["no facts to write from"], info
        lo, hi = MOTIVATION_WORDS[0], min(MOTIVATION_WORDS[1], limit.words or MOTIVATION_WORDS[1])
        problems: list[str] = []
        last = ""
        start = len(getattr(self.llm, "usage", []) or [])
        estimate = 0
        for attempt in range(fluency_mod.TRIES):
            ctx = {"mode": "fluency_motivation", "question": question, "company": self.company, "title": self.title, "facts": facts,
                   "ad_snippets": plan.ad_snippets, "words": [min(lo, hi), hi], "draft": built,          # not the letter: it lent the model facts
                   "style": {"avoid": list(self.style.get("avoid") or []), "voice": self.style.get("voice", "")}, "problems": problems,
                   "last_answer": last, "temperature": 0.3 if attempt == 0 else 0.5}
            user = json.dumps({k: ctx[k] for k in ("question", "company", "title", "facts", "ad_snippets", "words", "style", "last_answer",
                                                   "problems") if ctx.get(k)}, ensure_ascii=False, indent=1)
            info["tries"] = attempt + 1
            try:
                reply = normalise_json(self.llm.json(TASK, FLUENCY_SYSTEM, user, context=ctx))
            except Exception as e:  # noqa: BLE001  (no budget, no network: the code-built answer)
                info["problems"].append([f"the rewrite failed: {str(e)[:120]}"])
                break
            text = " ".join(str((reply or {}).get("answer") or "").split())
            estimate += (len(FLUENCY_SYSTEM) + len(user) + len(text)) // 4
            problems = (fluency_mod.fact_problems(text, "\n".join(facts), self.ad, self.guard.tools) + self._text_problems(text)
                        + self.guard.problems(text, plan, limit)) if text else ["the answer is empty"]
            if not problems:
                info.update(source="model", tokens=fluency_mod._spent(self.llm, start, estimate))
                return text, [], info
            info["problems"].append(list(problems))
            last = text
        info["tokens"] = fluency_mod._spent(self.llm, start, estimate)
        return "", problems, info

    def _earlier_motivations(self) -> list[tuple[str, str]]:
        """(label, text) of every earlier motivation answer: the approved ones in the library (other jobs) and this run's own earlier drafts."""
        mine = str(self.job.get("id") or "")
        out = [(f"{r.company} ({r.date})", r.answer) for r in self.lib.records() if r.cls in WHY and r.job_id != mine]
        return out + [(f"an earlier draft this run ({m['question'][:40]})", m["text"]) for m in self.motivation_log if m["text"] and m["question"] != self._current_q]

    def _overlap(self, text: str) -> tuple[float, str]:
        sh = letter_reuse.shingles(text)
        best = (0.0, "")
        for label, other in self._earlier_motivations():
            score = letter_reuse.jaccard(sh, letter_reuse.shingles(other))
            if score > best[0]:
                best = (score, label)
        return best

    def _reuse_checked(self, question: str, plan: Plan, limit: Limit, missing: Sequence[str], text: str, probs: list[str], tries: int) -> tuple[str, list[str]]:
        """A motivation draft must not reuse an earlier motivation answer (5-word shingles, overlap under REUSE_LIMIT): one redraft is asked, then none.
        Every draft goes to `motivation_log` with its guard result."""
        self._current_q = question
        overlap, who = self._overlap(text) if text else (0.0, "")
        mine = fluency_mod.letter_overlap_problems(text, self.letter_body) if text else []
        if text and (overlap >= REUSE_LIMIT or mine):                           # another motivation answer, or this job's letter (v3.4)
            asked = ([f"it reuses {who}: overlap {overlap:.2f} of 5-word runs (limit {REUSE_LIMIT}); say it in new words"] if overlap >= REUSE_LIMIT
                     else []) + [f"{m}; say it in new words" for m in mine]
            again, probs2 = self._draft(question, plan, limit, missing, asked, tries) if tries else ("", [])
            overlap2, who2 = self._overlap(again) if again else (1.0, who)
            mine2 = fluency_mod.letter_overlap_problems(again, self.letter_body) if again else mine
            text, probs, overlap, who = (again, [], overlap2, who2) if again and overlap2 < REUSE_LIMIT and not mine2 else (
                "", probs2 or mine2 or [f"reuses {who}: overlap {overlap:.2f} (limit {REUSE_LIMIT})"], overlap2, who2)
        self.motivation_log.append({"question": question, "text": text, "words": len(text.split()), "sources": plan.ids,
                                    "guard": "passed" if text else "FAILED: " + "; ".join(probs[:4]),
                                    "overlap": f"{overlap:.2f}" + (f" (closest: {who})" if who else " (no earlier motivation answer)")})
        return text, probs

    # ---- the person
    def _show(self, question: str, cls: str, limit: Limit, plan: Plan, text: str, weak: str = "") -> None:
        self.out(f"\n  FREE-TEXT QUESTION [{cls}]: {question}\n  Limit: {limit.describe()}\n  Plan: {plan.why}\n  Sources: {', '.join(plan.ids)}\n"
                 f"  Guard: passed (numbers, tools, organisations and names are all in the sources; limit, style and letter rules kept)\n"
                 + (f"  {weak}: the answer uses the closest true entry and does not claim the missing part\n" if weak else "")
                 + f"  Draft:\n    {text}")

    def _store(self, question: str, text: str, cls: str, plan: Plan, origin: str) -> None:
        self.lib.add(Record.new(question, text, cls, self.company, str(self.job.get("id") or ""), plan.ids, self.today, origin,
                                plan.digests()))
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

    def review(self, p: Proposal, strict: bool = True, on_text: Callable[[str], None] | None = None) -> str:
        """Show a draft and ask `[y] use · [e] edit · [r] redraft · [s] I'll write it`. Returns the text the person approved or wrote ('' = none),
        and stores it in the library. `on_text(text)` is called with each redraft so that the form can show it."""
        text, problems, redrafts = p.text, p.problems, 0
        while True:
            if not text:
                return self._write(p.question, p.cls, p.plan, p.limit, strict,
                                   f"free text [{p.cls}]: no draft passed the guard ({'; '.join(problems[:4])}). Nothing is drafted for you.")
            self._show(p.question, p.cls, p.limit, p.plan, text, p.weak)
            reply = (self.ask(MENU) or "").strip().lower()
            if reply in ("y", "yes"):
                self._store(p.question, text, p.cls, p.plan, "draft")
                return text
            if reply in ("r", "l") and redrafts < MAX_REDRAFTS:
                said = (self.ask("  What should change? ") or "").strip() if reply == "l" else ""
                redrafts += 1
                text, problems = self._draft(p.question, p.plan, p.limit, p.missing, [f"the reader asks: {said}"] if said else None)
                if text and on_text is not None:
                    on_text(text)
                continue
            if reply == "e":
                typed = (self.ask("  Your edited answer (replaces the draft; blank = back to the menu): ") or "").strip()
                if not typed:
                    continue
                over = p.limit.problems(typed)
                if over:
                    self.out(f"  too long for the form ({'; '.join(over)}): shorten it")
                    continue
                self._store(p.question, typed, p.cls, p.plan, "edited")
                return typed
            if reply == "s":
                return self._write(p.question, p.cls, p.plan, p.limit, strict, "")
            return ""                                                  # anything else is not an approval

    def answer(self, question: str, label: str = "", maxlength: Any = None, *, strict: bool = True) -> str:
        """The text for a free-text field, '' when there is none (the form then needs you). `strict`: the run can ask you to type an answer
        (auto-apply, or the assist when you press s); not strict: the assist leaves the field for you to type in the browser."""
        self.last = ""
        p = self.propose(question, label, maxlength)
        if p.origin == "reused":
            self.last = "reused"
            self.out(f"  free text: reused the answer you approved on {p.reused.date} for \"{p.reused.question[:70]}\" ({p.cls})")
            return p.text
        if not p.text:
            return self._write(p.question, p.cls, p.plan, p.limit, strict, p.why_not)
        return self.review(p, strict)


def for_job(job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], llm, settings, book: answers_mod.AnswerBook, *,
            ask: Callable[[str], str] = input, out: Callable[[str], None] = print, today: date | None = None,
            refetch_jd: Callable[[], str] | None = None) -> FreeText:
    """The free-text helper of one job: its ad, its CV report (keywords, hook), the fact bank, the model and the answer library. `refetch_jd` fetches
    the ad once more (the board API) when a motivation answer finds it too short; none = the ad as the database has it."""
    return FreeText(llm, fb, Library(settings.path("answers_library")), job, rep, book, ask=ask, out=out, today=today, refetch_jd=refetch_jd,
                    settings=settings)
