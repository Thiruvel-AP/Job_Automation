"""Review's `l` on a company letter: what the next draft is told.

After `l` the person is asked "What should change? (enter = just retry)". The model then gets, with every draft of that `l` (at most
company_letter.ATTEMPTS = 3):

  * the PREVIOUS DRAFT it is to revise (P3, P4, P5), named as in the letter's paragraph table (P1 hook ... P7 ask), so "shorten P4" works;
  * every guard rejection of that draft REWRITTEN AS AN INSTRUCTION ("remove 'robust'", "the letter is 288 words, 42 short of the 330 minimum:
    use more of the detail of bullet p1a");
  * the person's FEEDBACK, word for word.

What the person typed is kept in paths.letter_feedback (data/letters/feedback.jsonl: job id, text, date). Nothing here writes the fact bank, the
applicant file or the answers file.
"""
from __future__ import annotations

import json
import re
from datetime import date

from .grounding import sentences  # noqa: F401  (kept for callers that split a draft)

HEADER = "YOUR LAST DRAFT BROKE THESE RULES; fix every one"


# ------------------------------------------------------------------ rejection -> instruction
def _tag(problem: str) -> tuple[str, str]:
    """("P3", "rest") for "P3: rest"; ("", problem) for a problem about the letter as a whole."""
    m = re.match(r"^(P[1-7]):\s*(.*)$", problem, re.S)
    return (m.group(1), m.group(2)) if m else ("", problem)


def _para_words(paragraph: str) -> int:
    return len(paragraph.split())


def _shortest_bullet(paragraphs: list[str], plan) -> str:
    """The id of the first evidence bullet of the shorter of P3 and P4 (`paragraphs` = [P3, P4, P5]): where the missing words are best found."""
    groups = getattr(plan, "groups", None) or []
    if len(groups) < 2 or len(paragraphs) < 2:
        return ""
    n = 0 if _para_words(paragraphs[0]) <= _para_words(paragraphs[1]) else 1
    return groups[n][0]["id"] if groups[n] else ""


def instruction(problem: str, paragraphs: list[str], plan) -> str:
    """One guard rejection as an instruction to the writer."""
    tag, rest = _tag(problem)
    pre = f"{tag}: " if tag else ""
    m = re.match(r"^(\d+) words: the letter must be between (\d+) and (\d+)$", problem)
    if m:
        n, lo, hi = (int(x) for x in m.groups())
        if n < lo:
            bullet = _shortest_bullet(paragraphs, plan)
            return (f"the letter is {n} words, {lo - n} short of the {lo} minimum: use more of the detail of bullet {bullet}" if bullet
                    else f"the letter is {n} words, {lo - n} short of the {lo} minimum: use more of the detail of the evidence bullets")
        return f"the letter is {n} words, {n - hi} over the {hi} maximum: cut {n - hi} words"
    m = re.match(r"^vague filler '(.+?)' is not in the evidence", rest)
    if m:
        return f"remove '{m.group(1)}'"
    m = re.match(r"^deny-listed '(.+?)'", rest)
    if m:
        return f"{pre}remove '{m.group(1)}'"
    m = re.match(r"^number\(s\) about you not in the chosen bullets: (.*)$", rest)
    if m:
        return f"{pre}remove the number(s) {m.group(1)}: they are not in the bullets chosen for it"
    m = re.match(r"^number\(s\) about the company not in the ad: (.*)$", rest)
    if m:
        return f"{pre}remove the number(s) {m.group(1)}: the ad does not state them"
    m = re.match(r"^names '(.+?)', which is not in the chosen bullets", rest)
    if m:
        return f"{pre}remove '{m.group(1)}': its bullets do not name it"
    m = re.match(r"^names '(.+?)', which is not where the chosen bullets were done", rest)
    if m:
        return f"{pre}remove '{m.group(1)}': it is not where those bullets were done"
    m = re.match(r"^does not name an ad requirement its bullet evidences \(one of: (.+)\)$", rest)
    if m:
        return f"{pre}name {m.group(1)} in the paragraph"
    m = re.match(r"^names (\d+) of the ad skills its bullets evidence; it must name (\d+) \(from: (.+)\)$", rest)
    if m:
        return f"{pre}name at least {m.group(2)} of these ad skills: {m.group(3)}"
    m = re.match(r"^does not name the employer (.+)$", rest)
    if m:
        return f"P4: name the employer {m.group(1)} in its own sentence"
    m = re.match(r"^gives no number from the (.+) bullet \(one of: (.+)\)$", rest)
    if m:
        return f"P4: give one number of the {m.group(1)} bullet ({m.group(2)})"
    m = re.match(r"^P5 is (\d+) sentences", problem)
    if m or re.match(r"^P5 is \d+ words", problem):
        return "P5: write ONE short sentence"
    m = re.match(r"^(P\d): mentions memory / RAM / GB \('(.+?)'\)", problem)
    if m:
        return f"{m.group(1)}: remove '{m.group(2)}': the ad is not about memory or hardware"
    if problem.startswith("missing paragraph") or problem.startswith("expected 3 paragraphs"):
        return 'return JSON {"paragraphs": ["P3", "P4", "P5"]}: exactly three paragraphs'
    m = re.match(r"^claim about the company is not supported by the ad .*: '(.+)'$", rest)
    if m:
        return f"{pre}remove or rewrite '{m.group(1)}': the ad does not support it"
    if problem.startswith("P5 follows"):
        return "P5: quote or closely follow one of the numbered About / role sentences, in its own words"
    m = re.match(r"^'(.+?)' \((?:name|sector|client|capability|claim)\) is (?:in neither|not in)", rest)
    if m:
        return f"{pre}remove '{m.group(1)}': neither the evidence nor the ad says it"
    m = re.match(r"^'(.+?)' \(a name\) is not in the ad or the fact bank", problem)
    if m:
        return f"remove the name '{m.group(1)}': the ad and the fact bank do not give it"
    m = re.match(r"^uses '(.+?)': the letter speaks for one person", problem)
    if m:
        return f"write in the first person singular about your own work: no '{m.group(1)}'"
    m = re.search(r"do not reuse these sentences: (.*)$", problem)
    if m and problem.startswith("reuse:"):
        return f"do not reuse these sentences: {m.group(1)}"
    m = re.match(r"^body: (\d+) of (\d+) sentences cite", problem)
    if m:
        cited, total = int(m.group(1)), int(m.group(2))
        return "write P3 and P4 from their bullets: at least 60% of the sentences of P1 to P4 must be about your bullets"
    m = re.match(r"^P3/P4 draw on bullet (\S+), which is not printed", problem)
    if m:
        return f"write P3 and P4 only from the bullets given for them: {m.group(1)} is not on this CV"
    m = re.match(r"^names '(.+?)', which is not in this CV's Skills or bullets", rest)
    if m:
        return f"{pre}remove '{m.group(1)}': it is not in this CV's Skills or bullets"
    if problem.startswith("last paragraph is not a call-to-action question"):
        return "end with a question"
    return f"fix this: {problem}"


def instructions(problems: list[str], paragraphs: list[str], plan) -> list[str]:
    """Every rejection as an instruction, none twice, in the guard's order."""
    return list(dict.fromkeys(instruction(p, paragraphs, plan) for p in problems if p and p != "FALLBACK"))


# ------------------------------------------------------------------ the block appended to the prompt
def history_block(history: list[list[str]]) -> str:
    """Every rejection of every earlier draft, each guard message exactly as the guard wrote it ("" when there is none)."""
    rows = [f"Draft {n}:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(probs)) for n, probs in enumerate(history, 1) if probs]
    return ("EVERY REJECTION SO FAR, word for word (none of these may come back):\n" + "\n".join(rows)) if rows else ""


def revision_block(previous: list[str], lines: list[str], feedback: str, need: int | None = None, history: list[list[str]] | None = None) -> str:
    """The text added to the user message of a draft: the draft to revise, every earlier rejection word for word (`history`, one list per draft), what
    to change, the reader's feedback. "" when there is none of them."""
    out: list[str] = []
    if previous:
        tags = ("P3", "P4", "P5")
        out.append("YOUR PREVIOUS DRAFT (revise it; keep what already works):\n" + "\n".join(f"{t}: {p}" for t, p in zip(tags, previous)))
    if history_block(history or []):
        out.append(history_block(history or []))
    if lines:
        out.append(f"CHANGE THESE ({HEADER}):\n" + "\n".join(f"- {x}" for x in lines)
                   + (f"\nMake P3 + P4 + P5 about {need} words." if need else ""))
    if feedback.strip():
        out.append("THE READER'S FEEDBACK (follow it, word for word):\n" + feedback.strip())
    return ("\n\n" + "\n\n".join(out)) if out else ""


# ------------------------------------------------------------------ the feedback file
def save(settings, job_id: str, text: str):
    """Append what the person typed after `l`: {"job_id", "text", "date"} to paths.letter_feedback (made if needed). Returns the path."""
    path = settings.path("letter_feedback")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"job_id": job_id, "text": text, "date": date.today().isoformat()}, ensure_ascii=False) + "\n")
    return path


def last(settings, job_id: str) -> str:
    """The most recent feedback saved for this job ('' when there is none)."""
    path = settings.path("letter_feedback")
    if not path.exists():
        return ""
    found = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("job_id") == job_id:
            found = str(row.get("text") or "")
    return found
