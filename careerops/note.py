"""The post-CV note: data/outputs/<job_id>/note.md, one page for you to read before you apply and to send to a person.

Sections:
  Hook      the problem the ad hires for, the proof bullet and its number
  ATS       strict CV, claimable ceiling, CV + letter (per variant), letter alone (never gated) and the gate's label
  Learn list  the ad's terms the fact bank cannot claim; a deny-listed term is marked (confirmed absent)
  Contact   a TODO for you: find the person on LinkedIn (this code never fetches linkedin.com)
  Question  one question that names a specific term from the ad
  Flags     visa or sponsorship wording, location or office policy, seniority or years asked, security clearance, IR35, travel,
            and the posting age; plus anything the pipeline flagged (hook has no number, stretch)
  Gap       the honest gap used in letter B, with its bridge

The note only reads the report, the job and the fact bank; it writes nothing but note.md and sends nothing anywhere.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .discovery import freshness, priority
from .hook import NO_NUMBER_FLAG
from .tailoring.letters import learn_list
from .tailoring.scorer import Keyword

SNIPPET = 170
FLAG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Location / office policy", re.compile(
        r"\b(hybrid|on[- ]?site|in[- ]?office|office[- ]based|(?:\d|one|two|three|four|five)\s+days?\s+(?:a|per|each)\s+(?:week|wk)|"
        r"remote(?:ly)?|work(?:ing)? from home|relocat\w+)\b", re.I)),
    ("Seniority / years asked", re.compile(
        r"\b(\d+\s*\+\s*years?|\d+\s*(?:-|to)\s*\d+\s*years?|at least \d+ years?|minimum (?:of )?\d+ years?|\d+ years?(?:'|’)? experience|"
        r"senior|principal|staff (?:engineer|scientist)|team lead|lead (?:data|ml|engineer|scientist)|head of)\b", re.I)),
    ("Security clearance", re.compile(r"\b(?:sc|dv|ctc|bpss|npvv)\s+clear\w*|security[- ]clear\w*|security clearance|eligible for clearance", re.I)),
    ("IR35", re.compile(r"\bIR35\b", re.I)),
    ("Travel", re.compile(r"\b(?:travel(?:l?ing)?|\d+\s*%\s*travel|on[- ]site visits?)\b", re.I)),
]


def _one_line(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= SNIPPET else t[: SNIPPET - 1] + "…"


def _sentences(jd: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", jd or "") if s.strip()]


def jd_flags(jd: str) -> dict[str, list[str]]:
    """{flag name: up to two sentences of the ad that raise it}, in the note's order; a flag with no sentence has an empty list."""
    sents = _sentences(jd)
    out: dict[str, list[str]] = {"Visa / sponsorship": [f"\"{w.text}\" ({w.kind.replace('_', ' ')})" for w in priority.sponsor_wording(jd)[:2]]}
    for name, rx in FLAG_PATTERNS:
        out[name] = [_one_line(s) for s in sents if rx.search(s)][:2]
    return out


def question(rep: dict, team: str, company: str) -> str:
    """One question that names a specific term from the ad: the heaviest keyword (required first), preferring one the fact bank can
    claim (the question is about the work, not about a gap)."""
    kws = [Keyword(**k) for k in rep.get("keywords") or []]
    if not kws:
        return f"What does a first month on the {team or company} team look like?"
    have = {c.lower() for c in rep.get("claimable") or []}
    best = max(kws, key=lambda k: (k.term.lower() in have, k.weight, -kws.index(k)))
    where = f"the {team} team" if team else company
    return f"How does {where} handle {best.term} today, and what would you want a new data scientist to change first?"


def _why_no_letter(rep: dict) -> str:
    """The guards' own words for a job without a letter (tailoring/letter_reason.py), on one line."""
    from .tailoring.letter_reason import letter_reason

    return " ".join(letter_reason(rep.get("letter")).split())[:400] or "see report.json"


def contact_line(rep: dict, job: dict) -> str:
    team = (rep.get("team") or "").strip()
    return f"TODO: you find on LinkedIn: hiring manager, {team}" if team else \
        f"TODO: you find on LinkedIn: hiring manager, {rep.get('headline') or job['title']} at {job['company']}"


def build(job, rep: dict, fb: dict, now: datetime | None = None) -> str:
    """The note as Markdown. `job` is a job row or dict (company, title, location, description, posted_at, ...), `rep` the tailoring
    report."""
    job = {k: job[k] for k in job.keys()} if not isinstance(job, dict) else job
    jd = job.get("description") or ""
    hk = rep.get("hook") or {}
    a = rep.get("ats") or {}
    lines = [f"# {job['company']} | {job['title']}", "", f"{job.get('location') or ''}  ", f"{job.get('url') or ''}", ""]

    lines += ["## Hook", ""]
    if hk:
        bullet = next((b for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]
                       if b["id"] == hk["proof_bullet_id"]), {})
        lines += [f"- **Hook id:** {hk.get('hook_id') or 'none (no hook_library entry matched: the code template was used)'}",
                  f"- **Problem:** {hk['problem']}",
                  f"- **Proof:** {hk['proof_bullet_id']} ({hk['org']}): {bullet.get('text', '')}",
                  f"- **Number:** {hk['number'] or 'none: no bullet in the fact bank has one'}",
                  f"- **Headline:** {hk['headline']}",
                  f"- **Profile sentence 1:** {hk.get('profile_line', '')}",
                  f"- **Letter sentence 1:** {hk['letter_sentence']}"]
    else:
        lines.append("- No hook for this job (hook.enabled is off, or no bullet passed its checks).")

    lines += ["", "## ATS", ""]
    if a:
        lines += [f"- **Strict CV:** {a['strict']:.1f}%",
                  f"- **Claimable ceiling:** {a['ceiling']:.1f}%",
                  f"- **CV + letter:** " + (f"{a['combined']:.1f}%" if a.get("combined") is not None else f"no letter ({_why_no_letter(rep)})"),
                  f"- **Letter alone:** " + (f"{a['letter']:.1f}% (reported, never gated)" if a.get("letter") is not None else f"no letter ({_why_no_letter(rep)})"),
                  f"- **Gate:** {a['gate']}" + (" (STRETCH: kept, never dropped)" if rep.get("stretch") else "")]
        for v, nums in (a.get("variants") or {}).items():
            lines.append(f"- Letter {v}: CV + letter {nums['combined']:.1f}%, letter alone {nums['letter']:.1f}%")
        if a.get("negated"):
            lines.append("- In the CV only as \"not ...\" (not counted): " + ", ".join(a["negated"]))
    else:
        lines.append("- Not scored.")

    kws = [Keyword(**k) for k in rep.get("keywords") or []]
    learn = learn_list(kws, rep.get("claimable") or [], fb.get("deny_terms", []))
    lines += ["", "## Learn list", ""]
    lines += [f"- {r['term']}" + (" (confirmed absent)" if r["confirmed_absent"] else "") + ("" if r["required"] else " (nice to have)")
              for r in learn] or ["- Nothing: the fact bank backs every term of the ad."]

    lines += ["", "## Contact", "", contact_line(rep, job)]

    lines += ["", "## Question", "", question(rep, rep.get("team") or "", job["company"])]

    lines += ["", "## Flags", ""]
    for name, found in jd_flags(jd).items():
        lines.append(f"- **{name}:** " + ("; ".join(found) if found else "none found"))
    lines.append(f"- **Posting age:** {freshness.describe(job, now)}")
    extra = list(hk.get("flags") or [])
    if hk and not hk.get("number") and NO_NUMBER_FLAG not in extra:
        extra.append(NO_NUMBER_FLAG)
    if rep.get("stretch"):
        extra.append(f"stretch: the honest ceiling is {a.get('ceiling', 0):.0f}%, under the band")
    lines += [f"- **Pipeline:** {x}" for x in extra]

    lines += ["", "## Gap", ""]
    letters = rep.get("letters") or {}
    b = letters.get("B") or {}
    if b.get("status") == "ok" and b.get("gap"):
        lines += [f"- **Gap used in letter B:** {b['gap']}" + (" (confirmed absent)" if any(
            r["term"].lower() == b["gap"].lower() and r["confirmed_absent"] for r in learn) else ""),
                  f"- **Bridge:** {b['bridge']}",
                  f"- **Sentence:** {b['paragraphs'][-2] if len(b['paragraphs']) >= 2 else ''}"]
    else:
        lines.append(f"- None: {b.get('reason') or 'letter B was not made (letter.mode is not hook)'}")
    return "\n".join(lines).rstrip() + "\n"


def write(job, rep: dict, fb: dict, out_dir: Path, now: datetime | None = None) -> Path:
    """Write note.md into the job's output folder (the staging folder during tailoring)."""
    path = Path(out_dir) / "note.md"
    path.write_text(build(job, rep, fb, now), encoding="utf-8")
    return path
