"""The cover letter's grammar and its deterministic fallback.

A model draft is checked for the ungrammatical shapes small models produce from a bullet ("my work on built ...",
"mirrors my work a tool-calling agent", a paragraph that starts with a bare past-tense verb or is an evidence bullet pasted
verbatim). When a draft fails, the letter is built here, all in the first person, from text that is already checked: the
company hook's first sentence and the two evidence bullets, each turned into a clause ("I built ...").
"""
from __future__ import annotations

import re

from .faithfulness import deny_hits
from .grounding import sentences
from .rewrite_rules import CANONICAL
from .scorer import norm_literal

_IRREGULAR_PAST = frozenset("""built wrote led ran made drove won took gave kept held found taught sold sent spent understood
    chose began set put cut fit shipped""".split())


def starts_with_past_verb(text: str) -> bool:
    """Does the text begin with a past-tense verb ("Built ...", "Deployed ...", "Wrote ...")? Evidence is such bullets only."""
    m = re.match(r"[A-Za-z]+", text.strip())
    if not m:
        return False
    w = m.group(0).lower()
    return w in _IRREGULAR_PAST or (w.endswith("ed") and len(w) > 4)


# ------------------------------------------------------------------ gaps
_GAP_CASE = {k: v for k, v in {**{w.lower(): w for w in CANONICAL}, **{w.lower(): w for w in (
    "Azure", "Power BI", "AutoGen", "DSPy", "dbt", "Java", "JavaScript", "TypeScript", "Kubernetes", "Terraform", "Snowflake",
    "Databricks", "Tableau", "Looker", "Spark", "PySpark", "Kafka", "Airflow", "Redis", "MongoDB", "Docker", "Git", "Go",
    "Scala", "C++", "C#", "Rust", "Excel", "Alteryx", "Qlik", "SAS", "SPSS", "BigQuery", "Redshift", "OpenAI", "Hugging Face")}
}.items()}


def gap_display(gap: str, jd: str = "") -> str:
    """A gap as it should be written: the job description's own casing when it names it, else the canonical spelling
    (Azure, Power BI, AutoGen, DSPy), else the gap as given."""
    g = gap.strip()
    m = re.search(r"(?<![\w])" + re.escape(g) + r"(?![\w])", jd or "", re.I)
    if m and (m.group(0) != m.group(0).lower() or m.group(0).lower() not in _GAP_CASE):
        return m.group(0)
    return _GAP_CASE.get(g.lower(), m.group(0) if m else g)


# ------------------------------------------------------------------ grammar of a model draft
_PREP = r"(?:on|in|at|with|as|for|across|from|during|to|of|through|using|around|that|which)"
_MIRRORS_NOUN = re.compile(r"\b(?:mirror|reflect|match|echo)s?\s+my\s+(?:work|experience)\s+(?!" + _PREP + r"\b)(?!\w+ing\b)\w+",
                           re.I)


def grammar_problems(body: list[str], evidence: list[str]) -> list[str]:
    """Ungrammatical or pasted shapes in the model's paragraphs: "my work on built ...", "mirrors my work <noun>", a
    paragraph that starts with a bare past-tense verb (it has no subject), a sentence that is an evidence bullet verbatim."""
    out: list[str] = []
    text = " ".join(body)
    # the verbs a bullet starts with are the ones a model copies after "my work on": "my work on built scikit-learn pipelines"
    verbs = {re.match(r"[A-Za-z]+", e.strip()).group(0).lower() for e in evidence if starts_with_past_verb(e)}
    work_on_verb = re.compile(r"\bmy\s+(?:work|experience)\s+(?:on|in|with)\s+(?:" + "|".join(sorted(verbs | _IRREGULAR_PAST))
                              + r")\b", re.I)
    for rx, what in ((work_on_verb, "'my work on <past-tense verb>'"), (_MIRRORS_NOUN, "'mirrors my work <noun>'")):
        m = rx.search(text)
        if m:
            out.append(f"ungrammatical {what}: '{m.group(0)[:50]}'")
    for p in body:
        if starts_with_past_verb(p):
            out.append(f"paragraph starts with a bare past-tense verb (write 'I ...'): '{p.strip()[:50]}'")
    ev = {norm_literal(e).rstrip(". ") for e in evidence}
    for s in sentences(text):
        if norm_literal(s).rstrip(". ") in ev:
            out.append(f"pastes an evidence bullet verbatim: '{s.strip()[:50]}'")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the fallback letter
def clause(bullet: str) -> str:
    """A bullet as a clause that follows "I": first letter lower-cased (past-tense verbs only reach here), no final period."""
    b = bullet.strip().rstrip(".").rstrip()
    return b[:1].lower() + b[1:]


def org_label(org: str) -> str:
    """Where a job's work was done, as the phrase that follows a comma: "in my freelance AI/ML work" for self-employment,
    "at AstraZeneca" for "AstraZeneca · GeneTraceAI, University of Bristol" (the short name before the "·"), else "at <org>"."""
    short = org.split("·")[0].strip()
    if short.lower().startswith("self-employed"):
        return "in my freelance AI/ML work"
    return f"at {short}"


def where(org: str, project: bool, first: bool) -> str:
    """The place of a bullet: org_label for a job, "in my <project> project" for a project; capitalised when it starts a
    sentence."""
    s = ("in my recent work" if not org else f"in my {org} project" if project else org_label(org))
    return s[:1].upper() + s[1:] if first else s


def fallback_letter(job: dict, hook: str, evidence: list[dict], gaps: list[str], closing: str, deny: list[str],
                    jd: str = "") -> list[str]:
    """The letter when the model's draft fails the guard. Every paragraph is first person and no bullet appears twice:
      P1  <company hook sentence> I'd like to bring my experience to that work: at <org 1>, I <bullet 1 as a clause>.
      P2  At <org 2>, I <bullet 2 as a clause>.                      (a different bullet; left out when there is only one)
      P3  I have not used <Gap> yet.                                 (only when the job has a gap)
      P4  the closing question
    `evidence` = [{"id", "text", "org", "project"}]; the hook sentence is replaced by the role sentence when it is empty or
    names a deny-listed term."""
    sents = sentences(hook or "")
    lead = sents[0].strip() if sents else ""
    if not lead or deny_hits(lead, deny):
        lead = f"{job['company']}'s {job['title']} role sits close to the work I have been doing most recently."
    if not lead.endswith((".", "!", "?")):
        lead += "."
    paras: list[str] = []
    first, *others = evidence
    second = next((e for e in others if e["id"] != first["id"] and norm_literal(e["text"]) != norm_literal(first["text"])),
                  None) if evidence else None
    if evidence:
        paras.append(f"{lead} I'd like to bring my experience to that work: "
                     f"{where(first['org'], first.get('project', False), False)}, I {clause(first['text'])}.")
    else:
        paras.append(lead)
    if second:
        paras.append(f"{where(second['org'], second.get('project', False), True)}, I {clause(second['text'])}.")
    if gaps:
        paras.append(f"I have not used {gap_display(gaps[0], jd)} yet.")
    paras.append(closing)
    return [p for p in paras if p.strip()]
