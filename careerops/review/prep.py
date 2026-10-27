"""Interview prep pack (Phase D3): `prep.md` in the job's archive folder, written when you `mark-applied`.

  Questions       8-10 the ad is likely to lead to: its keywords, required first and heaviest first (at most 8), then the standard ones (why this company, the
                  project you are proudest of ...). For each, up to two fact-bank entries to answer with, in situation / task + action / result order,
                  the bullet quoted word for word with its id.
  Gaps            the gap report's gap rows (keywords the fact bank cannot back), each with a one-line learn task; a term you told the fact bank you lack
                  is marked do not claim.
  Questions to ask them   three, built from the ad.

TRUTH RULE. Every statement about you comes from the fact bank and nothing else: a bullet is quoted, never reworded, a result is the bullet's own `result:`
field or the numbers in its text, a situation is the entry's own `situation:`. A question with no supporting bullet (or on a deny-listed term, whatever a
stray tag says) is marked `no evidence: don't claim`. What the pack says about the company comes from the ad. No model, no network. The file is created
with safe_io (never over an existing file) and never inside paths.applications, your own archive.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import bands as bands_mod
from .. import note as note_mod
from ..apply.safe_io import safe_write_text
from ..config import load_yaml
from ..db import DB
from ..tailoring.faithfulness import deny_hits
from ..tailoring.scorer import Keyword
from . import gaps as gaps_mod

NO_EVIDENCE = "no evidence: don't claim"
NO_STORY = "no story needed: answer from the ad and from the questions you ask below"
MAX_KEYWORD_QUESTIONS, MIN_QUESTIONS, MAX_QUESTIONS = 8, 8, 10
MAX_ENTRIES, MAX_BULLETS, MAX_GAPS = 2, 2, 8
LETTERS = "AB"
ASK = {
    "team": "How does the team decide what to build next, and who does this role work with day to day?",
    "quarter": "What does a strong first quarter look like in this role?",
    "future": "Where is {term} heading in the team's work over the next year, and what is hard about it today?",
}
ASK_COUNT = 3
KEYWORD_QUESTIONS = (
    "Tell me about a project where you used {term}. What was your part in it?",
    "Describe a problem you solved with {term} and how you knew it worked.",
    "What have you built with {term}, and what would you do differently now?",
)
PREFERRED_QUESTION = "The ad lists {term} as a plus. What is your experience with it?"
# (question, how its story is found): "hook" = the proof bullet of this job's hook, "projects" = the projects on this CV, a list = fact-bank terms, None = none
STANDARD = (
    ("Why {company}, and why this {title} role?", "hook"),
    ("Walk me through the project you are proudest of, end to end.", "projects"),
    ("Tell me about a time you explained technical results to someone non-technical.", ["communication", "stakeholders", "presentation", "reporting"]),
    ("Tell me about something you built that failed or gave a wrong answer, and what you did.",
     ["reliability", "testing", "evaluation", "guardrails", "resilience", "debugging", "data quality"]),
    ("What do you want to learn first in this role?", None),
)


class PrepError(RuntimeError):
    """The pack cannot be written (one line, fit to print)."""


# ------------------------------------------------------------------ the fact bank
@dataclass
class Story:
    label: str                                     # "Data Scientist, Northwind Research Lab, May 2026 – Sep 2026"
    entry_id: str
    situation: str
    bullets: list[dict] = field(default_factory=list)


def _label(section: str, e: Mapping[str, Any]) -> str:
    if section == "experience":
        return ", ".join(str(x) for x in (e.get("title"), e.get("org"), e.get("dates")) if x)
    return str(e.get("name") or e.get("id")) + (f" ({e['stack']})" if e.get("stack") else "")


def _entries(fb: Mapping[str, Any]):
    for section in ("experience", "projects"):
        for e in fb.get(section) or []:
            yield section, e


def _chosen(rep: Mapping[str, Any]) -> list[str]:
    return gaps_mod._chosen(dict(rep))


def stories(has: Callable[[str], bool], fb: Mapping[str, Any], rep: Mapping[str, Any], limit: int = MAX_ENTRIES) -> list[Story]:
    """Up to `limit` entries with a bullet that `has` accepts (text + tags), those on this CV first, then those that say it in the bullet's own words.
    Each carries its best bullets (at most MAX_BULLETS), the ones that say it in their own words and the ones on the CV first."""
    on_cv = set(_chosen(rep))
    found = []
    for n, (section, e) in enumerate(_entries(fb)):
        hits = [b for b in e.get("bullets") or [] if has(f"{b['text']} {' '.join(str(t) for t in b.get('terms') or [])}")]
        if not hits:
            continue
        hits.sort(key=lambda b: (b["id"] not in on_cv, not has(b["text"])))
        key = (not any(b["id"] in on_cv for b in hits), not any(has(b["text"]) for b in hits), n)
        found.append((key, Story(_label(section, e), str(e.get("id") or ""), str(e.get("situation") or "").strip(), hits[:MAX_BULLETS])))
    return [s for _, s in sorted(found, key=lambda t: t[0])][:limit]


def _story_by_bullet(fb: Mapping[str, Any], bullet_ids: list[str]) -> list[Story]:
    out: list[Story] = []
    for section, e in _entries(fb):
        hit = [b for b in e.get("bullets") or [] if b["id"] in bullet_ids]
        if hit:
            out.append(Story(_label(section, e), str(e.get("id") or ""), str(e.get("situation") or "").strip(), hit[:MAX_BULLETS]))
    return out


def _result(story: Story) -> str:
    for b in story.bullets:
        if b.get("result"):
            return str(b["result"]).strip()
    if any(re.search(r"\d", b["text"]) for b in story.bullets):
        return "the numbers in the bullet above are the result; add no other"
    return "none recorded in the fact bank: do not state one"


def _story_lines(stories_: list[Story]) -> list[str]:
    out: list[str] = []
    for i, s in enumerate(stories_[:len(LETTERS)]):
        out.append(f"   Story {LETTERS[i]}: {s.label}")
        out.append("   - Situation: " + (s.situation or "not written in the fact bank; set the scene from the bullet below and say no more"))
        for b in s.bullets:
            out.append(f"   - Task and action [{b['id']}]" + (" (provisional)" if b.get("provisional") else "") + f": {' '.join(str(b['text']).split())}")
        out.append(f"   - Result: {_result(s)}")
    return out


# ------------------------------------------------------------------ the questions
def _ordered(keywords: list[Keyword]) -> list[Keyword]:
    return [k for _, k in sorted(enumerate(keywords), key=lambda t: (not t[1].required, -t[1].weight, t[0]))]


def _evidence_for(k: Keyword, fb: Mapping[str, Any], rep: Mapping[str, Any]) -> list[Story]:
    if deny_hits(k.term, list(fb.get("deny_terms") or [])):
        return []
    return stories(gaps_mod._matcher(k), fb, rep)


def _standard(kind, fb: Mapping[str, Any], rep: Mapping[str, Any]) -> tuple[list[Story], list[str]]:
    """(stories, extra lines) for a standard question."""
    if kind is None:
        return [], []
    if kind == "hook":
        proof = (rep.get("hook") or {}).get("proof_bullet_id")
        extra = [f"   - From the ad (about the company, not about you): {rep['company_hook']}"] if rep.get("company_hook") else []
        return (_story_by_bullet(fb, [proof]) if proof else []), extra
    if kind == "projects":
        ids = [i for p in (rep.get("context") or {}).get("projects") or [] for i in p.get("bullet_ids") or []]
        return [s for s in _story_by_bullet(fb, ids)][:MAX_ENTRIES], []
    return stories(gaps_mod._matcher(Keyword(kind[0], 1, False, list(kind[1:]))), fb, rep), []


def questions(job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any]) -> list[str]:
    kws = _ordered([Keyword(**k) for k in rep.get("keywords") or []])[:MAX_KEYWORD_QUESTIONS]
    standard = min(len(STANDARD), max(MIN_QUESTIONS - len(kws), 2), MAX_QUESTIONS - len(kws))
    blocks: list[tuple[str, list[Story], list[str], bool]] = []          # (question, stories, extra lines, needs a story)
    for i, k in enumerate(kws):
        q = (KEYWORD_QUESTIONS[i % len(KEYWORD_QUESTIONS)] if k.required else PREFERRED_QUESTION).format(term=k.term)
        blocks.append((q + f" ({'required' if k.required else 'preferred'} in the ad)", _evidence_for(k, fb, rep), [], True))
    for q, kind in STANDARD[:standard]:
        found, extra = _standard(kind, fb, rep)
        blocks.append((q.format(company=job["company"], title=job["title"]), found, extra, kind is not None))
    out: list[str] = []
    for n, (q, found, extra, needs) in enumerate(blocks, 1):
        lines = [f"{n}. {q}"]
        if found:
            lines += _story_lines(found)
        elif needs:
            lines.append(f"   - {NO_EVIDENCE}")
        else:
            lines.append(f"   - {NO_STORY}")
        out += lines + extra
    return out


def gap_lines(rep: Mapping[str, Any], fb: Mapping[str, Any], band: bands_mod.Bands) -> list[str]:
    kws = [Keyword(**k) for k in rep.get("keywords") or []]
    if rep.get("ats") and kws:
        rows = [(r.term, r.required, r.denied) for r in gaps_mod.build(dict(rep), dict(fb), band).rows if r.kind == "gap"]
    else:
        from ..tailoring.letters import learn_list
        rows = [(r["term"], r["required"], r["confirmed_absent"]) for r in learn_list(kws, rep.get("claimable") or [], list(fb.get("deny_terms") or []))]
    if not rows:
        return ["- None: the fact bank backs every term of the ad."]
    out = []
    for term, required, denied in rows[:MAX_GAPS]:
        head = f"- {term} ({'required' if required else 'preferred'}): "
        task = f"learn: build one small working example with {term} and read its core documentation; add a fact to your fact bank only once it is true."
        out.append(head + ("you told the fact bank you lack it, so do not claim it. " if denied else "") + task[0].upper() + task[1:])
    return out


def ask_lines(job: Mapping[str, Any], rep: Mapping[str, Any]) -> list[str]:
    first = note_mod.question(dict(rep), rep.get("team") or "", job["company"])
    kws = _ordered([Keyword(**k) for k in rep.get("keywords") or []])
    mine = {c.lower() for c in rep.get("claimable") or []}
    rest = [k for k in kws if k.term.lower() not in first.lower()]
    spare = next((k for k in rest if k.term.lower() in mine), rest[0] if rest else None)     # one you can talk about, else the heaviest
    asks = [first, ASK["team"], ASK["future"].format(term=spare.term) if spare else ASK["quarter"]]
    return [f"- {a}" for a in asks[:ASK_COUNT]]


def build(job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], band: bands_mod.Bands) -> str:
    job = {k: job[k] for k in job.keys()} if not isinstance(job, dict) else job
    lines = [f"# Interview prep: {job['company']} | {job['title']}", "",
             f"Job {job['id']}. Everything about YOU in this pack is a bullet from your fact bank (ids in square brackets), quoted word for word; "
             "everything about the company is from the ad.",
             f"A question marked `{NO_EVIDENCE}` has no supporting bullet: do not tell a story for it.", "",
             "## Questions", "", *questions(job, rep, fb), "", "## Gaps", "",
             "From the gap report: the ad's terms your fact bank cannot back.", "", *gap_lines(rep, fb, band), "",
             "## Questions to ask them", "", *ask_lines(job, rep)]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ the file
def _report(job: Mapping[str, Any], folder: Path) -> dict:
    for base in (job["out_dir"], folder):
        if not base:
            continue
        try:
            return json.loads((Path(base) / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    raise PrepError("the job has no report.json (neither in its output folder nor in its archive folder)")


def write_pack(db: DB, settings, job_id: str, fb: Mapping[str, Any] | None = None) -> Path:
    """Write prep.md into the job's archive folder. Raises PrepError (one line) when there is no archive folder, no report, the folder is your own
    (paths.applications) or prep.md already exists: an existing file is left exactly as it is."""
    job = db.job(job_id)
    if job is None:
        raise PrepError(f"no such job: {job_id}")
    if not job["application_dir"]:
        raise PrepError("the job has no archive folder yet (the archive step did not finish)")
    folder = Path(job["application_dir"])
    if not folder.is_dir():
        raise PrepError(f"the archive folder {folder} does not exist")
    try:
        if folder.resolve().is_relative_to(settings.path("applications").resolve()):
            raise PrepError(f"{folder} is inside your own archive folder (paths.applications), which this tool never writes to")
    except (KeyError, OSError):
        pass
    rep = _report(job, folder)
    fb = fb if fb is not None else load_yaml(settings.path("fact_bank"))
    dst = folder / "prep.md"
    try:
        safe_write_text(dst, build(job, rep, fb, bands_mod.config(settings)))
    except FileExistsError:
        raise PrepError(f"{dst} already exists; it is left as it is") from None
    return dst
