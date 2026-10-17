"""The cover letter's grammar and its deterministic fallback.

A model draft is checked for the ungrammatical shapes small models produce from a bullet ("my work on built ...",
"mirrors my work a tool-calling agent", a paragraph that starts with a bare past-tense verb or is an evidence bullet pasted
verbatim). When a draft fails, the letter is built here, all in the first person, from text that is already checked: the
company hook's first sentence and the two evidence bullets, each turned into a clause ("I built ...", "I worked on ...").
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


def _display_word(word: str, jd: str) -> str:
    low = word.casefold()
    if low in CANONICAL:
        return CANONICAL[low]
    if _GAP_CASE.get(low, low) != low:
        return _GAP_CASE[low]
    if low in _TECH:                                            # Python, Docker, Keras ...: the names the term-dump check wants capitalised
        return _TECH[low]
    m = re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", jd or "", re.I)
    return m.group(0) if m and any(c.isupper() for c in m.group(0)[1:]) else low


def _display(term: str, jd: str) -> str:
    """A term as it is written inside a sentence, word by word: a name keeps its spelling (AWS, MLOps, CI/CD, Docker), the ad's own
    camel-case or capitals are kept (XGBoost, ETL), anything else is lower case ("aws lambda" -> "AWS lambda")."""
    return " ".join(_display_word(w, jd) for w in term.split())


def bank_names(fb: dict) -> list[str]:
    """Every skill and project-stack name of the fact bank, in the user's own spelling (for `noun_phrase`'s `known`)."""
    names: list[str] = []
    for items in ((fb or {}).get("skills") or {}).values():
        names += [str(s) for s in items] if isinstance(items, list) else []
    for sec in ("experience", "projects"):
        for e in (fb or {}).get(sec, []) or []:
            names += [x.strip() for x in str(e.get("stack") or "").split(",") if x.strip()]
    return names


def noun_phrase(terms, jd: str = "", known=()) -> str:
    """`terms` as a lower-case noun phrase for the middle of a sentence: "production, deployment and testing". Never title case:
    only a name keeps its capitals (AWS, MLOps, CI/CD; and, from `known` -- the bank's own skill names -- a spelling like "AWS Lambda"
    that is more than a first capital letter)."""
    spelled = {" ".join(n.split()).casefold(): " ".join(n.split()) for n in known
               if " ".join(n.split()) not in (" ".join(n.split()).capitalize(), " ".join(n.split()).lower())}
    out: list[str] = []
    for t in terms:
        t = " ".join(str(t).split())
        d = spelled.get(t.casefold()) or (_display(t, jd) if t else "")
        if d and d.casefold() not in {x.casefold() for x in out}:
            out.append(d)
    return out[0] if len(out) == 1 else (", ".join(out[:-1]) + " and " + out[-1]) if out else ""


# ------------------------------------------------------------------ grammar of a model draft
_PREP = r"(?:on|in|at|with|as|for|across|from|during|to|of|through|using|around|that|which)"
_MIRRORS_NOUN = re.compile(r"\b(?:mirror|reflect|match|echo)s?\s+my\s+(?:work|experience)\s+(?!" + _PREP + r"\b)(?!\w+ing\b)\w+",
                           re.I)


PASTE_OVERLAP = 0.7        # a sentence with at least this share of its words in one evidence bullet is that bullet pasted
PASTE_MIN_WORDS = 5        # shorter sentences share words by chance ("I used Python.")
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_LEADING_I = re.compile(r"^\s*I\s+")


def _paste_words(text: str) -> set[str]:
    """The words compared by the paste check: lower-case alphanumeric tokens, bracketed text and a leading "I" left out."""
    return set(re.findall(r"[a-z0-9]+", _LEADING_I.sub("", _BRACKETED.sub(" ", text)).lower()))


def paste_overlap(sentence: str, bullet: str) -> float:
    """The share of the sentence's words (see `_paste_words`) that the bullet also has; 0.0 for a sentence of fewer than
    PASTE_MIN_WORDS words."""
    s = _paste_words(sentence)
    return len(s & _paste_words(bullet)) / len(s) if len(s) >= PASTE_MIN_WORDS else 0.0


def grammar_problems(body: list[str], evidence: list[str]) -> list[str]:
    """Ungrammatical or pasted shapes in the model's paragraphs: "my work on built ...", "mirrors my work <noun>", a
    paragraph that starts with a bare past-tense verb (it has no subject), a sentence that is an evidence bullet verbatim
    or nearly so (PASTE_OVERLAP of its words are the bullet's, "I" and bracketed text left out)."""
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
            continue
        share = max((paste_overlap(s, e) for e in evidence), default=0.0)
        if share >= PASTE_OVERLAP:
            out.append(f"pastes an evidence bullet ({share:.0%} of its words are the bullet's): '{s.strip()[:50]}'")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the fallback letter
def clause(bullet: str) -> str:
    """A bullet as a clause that follows "I": first letter lower-cased (a plain capitalised first word only: "XGBoost",
    "LSTM" and "MCP" keep their case), no final period."""
    b = bullet.strip().rstrip(".").rstrip()
    first = re.match(r"[A-Za-z][A-Za-z-]*", b)
    plain = bool(first) and not any(c.isupper() for c in first.group(0)[1:])
    return b[:1].lower() + b[1:] if plain else b


def i_clause(bullet: str) -> str:
    """The bullet as a first-person clause: "I built a tool-calling agent ..." for a verb-led bullet, "I worked on XGBoost
    churn classifier in R ..." for a noun-led one (it cannot follow "I" as it stands). "worked on" says no more than the
    bullet does: no built / led / owned."""
    return f"I {clause(bullet)}" if starts_with_past_verb(bullet) else f"I worked on {clause(bullet)}"


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
                     f"{where(first['org'], first.get('project', False), False)}, {i_clause(first['text'])}.")
    else:
        paras.append(lead)
    if second:
        paras.append(f"{where(second['org'], second.get('project', False), True)}, {i_clause(second['text'])}.")
    if gaps:
        paras.append(f"I have not used {gap_display(gaps[0], jd)} yet.")
    paras.append(closing)
    return [p for p in paras if p.strip()]


# ------------------------------------------------------------------ a pasted `terms:` list, and technologies in lower case
# Fix round 2: a draft that reads "...mirrors my work on docker, containerised, containerized, aws lambda, serverless, mlops, cloud, iam,
# security, production." is a fact-bank bullet's `terms:` list pasted into prose. Prose says what was done, not which words index it.
TERM_RUN = 3                # this many comma-separated items in a row that are all some bullet's `terms` make a pasted list
_TECH = {**{k: v for k, v in CANONICAL.items() if k != v}, **{w.lower(): w for w in (
    "Docker", "Python", "Keras", "Kubernetes", "Terraform", "Streamlit", "Tableau", "Snowflake", "Databricks", "PySpark", "Airflow",
    "LightGBM", "CatBoost", "Optuna", "Pydantic", "Flask", "Django", "Kafka", "MongoDB", "DynamoDB", "OpenCV", "Plotly", "Seaborn",
    "Matplotlib")}}
_TECH.pop("ai", None)       # "ai" is a word in other languages and in names; the other acronyms are not
_COMMA = re.compile(r",(?!\d)|;")


def bullet_terms(fb: dict) -> set[str]:
    """Every `terms` entry of every bullet of the fact bank (experience and projects), as norm_literal text."""
    return {norm_literal(str(t)).strip() for sec in ("experience", "projects") for e in (fb or {}).get(sec, []) or []
            for b in e.get("bullets", []) for t in b.get("terms", []) or [] if str(t).strip()}


def _segment(seg: str) -> str:
    s = norm_literal(seg).strip(" .;:!?()\"'")
    return re.sub(r"^(?:and|or|&)\s+", "", s).strip()


def _kind(seg: str, terms: set[str]) -> str:
    """exact: the segment is a term; suffix: it ends with one (the start of a list, "...my work on docker"); prefix: it starts with
    one (the end of a list, "iam and then ..."); "" otherwise."""
    if seg in terms:
        return "exact"
    words = seg.split()
    for n in range(1, min(4, len(words)) + 1):
        if " ".join(words[-n:]) in terms:
            return "suffix"
    for n in range(1, min(4, len(words)) + 1):
        if " ".join(words[:n]) in terms:
            return "prefix"
    return ""


def _sub_run(a: list[str], b: list[str]) -> bool:
    return len(a) <= len(b) and any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


def term_dump_problems(paragraphs: list[str], fb: dict) -> list[str]:
    """Problems that mark a letter as written from the `terms:` lists rather than from the work: a run of TERM_RUN or more
    comma-separated items that are all some bullet's terms ("a list of terms"), an item repeated inside such a list or a short list
    ("repeated"), and a known technology in lower case ("lower case 'docker'"). Empty for ordinary prose."""
    terms = bullet_terms(fb)
    text = " ".join(p for p in paragraphs if p)
    out: list[str] = []
    for sent in sentences(text):
        segs = [_segment(x) for x in _COMMA.split(sent)]
        kinds = [_kind(x, terms) if x else "" for x in segs]
        i = 0
        while i < len(segs):
            if kinds[i] not in ("exact", "suffix"):
                i += 1
                continue
            j = i + 1
            while j < len(segs) and kinds[j] == "exact":
                j += 1
            end = j + 1 if j < len(segs) and kinds[j] == "prefix" else j
            if end - i >= TERM_RUN:
                out.append(f"a list of terms, not a sentence: '{sent.strip()[:70]}'")
            exact = [segs[k].split() for k in range(i, j) if kinds[k] == "exact"]
            if any(_sub_run(a, b) for x, a in enumerate(exact) for y, b in enumerate(exact) if x != y):
                out.append(f"an item repeated inside one list: '{sent.strip()[:70]}'")
            i = max(end, i + 1)
        short = [s for s in segs if s and len(s.split()) <= 3]
        if len(segs) >= 3 and len(set(short)) < len(short):
            out.append(f"an item repeated inside one list: '{sent.strip()[:70]}'")
        for a, b in zip(segs, segs[1:]):
            if len(segs) >= 3 and b and len(b.split()) <= 3 and a.endswith(" " + b):
                out.append(f"an item repeated inside one list: '{sent.strip()[:70]}'")
    for low, canon in _TECH.items():
        if re.search(r"(?<![\w/.-])" + re.escape(low) + r"(?![\w/-])", text):
            out.append(f"lower case '{low}' (write {canon})")
    return list(dict.fromkeys(out))
