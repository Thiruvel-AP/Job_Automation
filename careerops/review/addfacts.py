"""Review's `f`: ask the user about the top gap keywords, one by one, and keep what they TYPE.

    Airflow (+2.5): have you used it? Describe it in one line (where, what, result), or press enter to skip.

An answer goes to data/facts/proposed.yaml (settings.path("facts_proposed")) with the keyword, the text exactly as typed, the role or project
the user names for it, the job id and the date (tailoring/provisional.py). Never to the fact bank: that is review/factsave.py, after the job,
with a `y` for each entry. An empty answer stores nothing, so a skipped question can never become a claim; a sentence that names a tool on
the deny list is refused; no keyword is asked twice for one job. The model is not involved: the text is the user's.
"""
from __future__ import annotations

from typing import Callable

from ..tailoring import provisional as prov
from ..tailoring.scorer import has_literal, norm_literal
from .gaps import GapReport, Row, gain_text

ASK_MAX = 5
PROMPT = "{term} (+{gain}): have you used it? Describe it in one line (where, what, result), or press enter to skip. "
TARGET_TRIES = 3


def targets(fb: dict) -> list[tuple[str, str]]:
    """(entry id, how it is shown) for every role and project of the fact bank, in the bank's order."""
    out = []
    for e in fb.get("experience") or []:
        out.append((str(e["id"]), f"{e['id']}: {e.get('title') or ''}, {e.get('org') or ''}".rstrip(", ")))
    for e in fb.get("projects") or []:
        out.append((str(e["id"]), f"{e['id']}: {e.get('display_name') or e.get('name') or ''}"))
    return out


def label_for(fb: dict, target: str) -> str:
    return next((label for tid, label in targets(fb) if tid == target), target)


def choose_target(ask: Callable[[str], str], fb: dict, out: Callable[[str], None] = print) -> str | None:
    """Which role or project the fact belongs to: a number from the list or an id; enter = drop the fact. None after TARGET_TRIES bad answers."""
    options = targets(fb)
    out("  Which role or project is it from?")
    out("\n".join(f"   {n}. {label}" for n, (_, label) in enumerate(options, 1)))
    for _ in range(TARGET_TRIES):
        answer = ask("  number or id (enter = drop this fact): ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        if answer in dict(options):
            return answer
        out(f"  '{answer}' is not in the list.")
    return None


def asked_rows(gap: GapReport, store: prov.Store, job_id: str) -> list[Row]:
    """The gap keywords to ask about: the top ASK_MAX by gain, none deny-listed, none that already has a fact (any status) for this job."""
    done = {f.keyword.lower() for f in store.facts() if f.job_id == job_id}
    return [r for r in gap.askable(10_000) if r.term.lower() not in done][:ASK_MAX]


def collect(ask: Callable[[str], str], gap: GapReport, fb: dict, store: prov.Store, job_id: str,
            out: Callable[[str], None] = print) -> list[prov.Fact]:
    """Put each question to the user; return the facts stored. `fb` is the real fact bank (its deny list and its roles)."""
    stored: list[prov.Fact] = []
    for row in asked_rows(gap, store, job_id):
        text = ask(PROMPT.format(term=row.term, gain=gain_text(row.gain))).strip()
        if not text:
            continue                                                 # skipped: nothing is stored, nothing is claimed
        if "\n" in text:
            out(f"  not stored: one line, please ({row.term})")
            continue
        denied = prov.deny_problem(fb, text)
        if denied:
            out(f"  not stored: '{denied}' is on your deny list (you said you lack it), so no fact may say it.")
            continue
        target = choose_target(ask, fb, out)
        if target is None:
            out(f"  not stored: no role or project for the {row.term} fact.")
            continue
        stored.append(store.add(row.term, text, target, job_id))
        if not any(has_literal(norm_literal(text), norm_literal(w)) for w in [row.term]):
            out(f"  note: your sentence does not say '{row.term}', so the ATS would not count it; it is still used as you wrote it.")
    return stored
