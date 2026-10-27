"""Agency or direct, for the same role: before tailoring, the tracker (the jobs table) and the archive are checked for the SAME company and role under ANOTHER
channel, a recruitment agency vs direct, or two agencies. If one is found, the job stops and the person says which channel owns it. If an agency hides the
employer, the person is asked before going on. Silence is never a yes.

The employer behind an agency ad is read from its text ("Our client, Acme Ltd, ...", "on behalf of Acme", "Client: Acme"); an ad that names nobody hides it.
Two ads of one company on one channel are a duplicate (apply/decided.py), not another channel. Jobs you already dropped (skipped, rejected, closed, error)
do not count. Pure functions: rows in, matches out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..apply.naming import folder_key
from . import candidate as cand
from .repost import title_identity

DROPPED = ("skipped", "skipped (rules)", "skipped (user)", "rejected", "closed", "error")
_NAME = r"([A-Z][\w&'’.-]*(?:[ \t]+[A-Z][\w&'’.-]*){0,3})"
EMPLOYER = re.compile(
    r"(?:[Oo]ur [Cc]lient[ \t]*(?:,|:|[ \t]+is|[ \t]+are)|[Oo]n [Bb]ehalf [Oo]f|[Cc]lient[ \t]*:)[ \t]*(?:the[ \t]+|The[ \t]+)?" + _NAME)
NOT_NAMES = frozenset({"A", "An", "We", "They", "It", "This", "Our", "Confidential", "Leading"})


@dataclass(frozen=True)
class Match:
    where: str            # tracker | archive
    label: str            # "Hays | Data Scientist [agency, triaged]" or "Acme Ltd/Data Scientist"
    channel: str          # agency | direct | unknown
    other_id: str = ""


def employer_of(text: str) -> str | None:
    """The employer an agency ad names, or None when it names nobody."""
    for m in EMPLOYER.finditer(str(text or "")):
        name = m.group(1).strip(" .,;:-")
        if name and name.split()[0] not in NOT_NAMES:
            return name
    return None


def _get(job: Mapping[str, Any], key: str) -> Any:
    try:
        return job[key]
    except (KeyError, IndexError, TypeError):
        return None


def channel_of(job: Mapping[str, Any]) -> str:
    return "agency" if _get(job, "agency") else "direct"


def hidden_employer(job: Mapping[str, Any]) -> bool:
    """An agency ad that does not say who the employer is."""
    return channel_of(job) == "agency" and employer_of(str(_get(job, "description") or "")) is None


def _names(job: Mapping[str, Any]) -> set[str]:
    """The employer names a job answers to: its company, and (for an agency ad) the employer the ad names."""
    out = {folder_key(str(_get(job, "company") or ""))}
    if channel_of(job) == "agency":
        out.add(folder_key(employer_of(str(_get(job, "description") or "")) or ""))
    return {n for n in out if n}


def find(job: Mapping[str, Any], others: list[Mapping[str, Any]], archive: list[tuple[str, str]]) -> list[Match]:
    """The same role in `others` (other jobs of the tracker) or the archive ((company folder, role folder) pairs) under another channel."""
    ident, mine = title_identity(str(_get(job, "title") or "")), _names(job)
    here = channel_of(job)
    out: list[Match] = []
    for r in others:
        if _get(r, "id") == _get(job, "id") or str(_get(r, "status") or "") in DROPPED or title_identity(str(_get(r, "title") or "")) != ident:
            continue
        theirs, there = _names(r), channel_of(r)
        same_firm = folder_key(str(_get(job, "company") or "")) == folder_key(str(_get(r, "company") or ""))
        if same_firm and here == there:
            continue                                                       # one company, one channel: a duplicate, not another channel
        if mine & theirs:
            pass
        elif (here == there == "agency" and hidden_employer(job) and hidden_employer(r)
              and cand.places_agree(str(_get(job, "location") or ""), str(_get(r, "location") or ""))
              and cand.norm_place(str(_get(job, "location") or ""))):
            pass                                                           # two agencies that name nobody: the title and the town are all there is
        else:
            continue
        out.append(Match("tracker", f"{_get(r, 'company')} | {_get(r, 'title')} [{there}, {_get(r, 'status')}]", there, str(_get(r, "id") or "")))
    for company, role in archive:
        if folder_key(company) in mine and title_identity(role) == ident:
            out.append(Match("archive", f"{company}/{role}", "unknown"))
    return out


def channel_question(job: Mapping[str, Any], matches: list[Match]) -> str:
    lines = [f"  {_get(job, 'company')} | {_get(job, 'title')} comes through the {channel_of(job)} channel, and the same role is already on record:"]
    lines += [f"    - {m.label}" + (f" ({m.channel} channel)" if m.channel != "unknown" else " (in your archive; channel not recorded)") for m in matches[:5]]
    lines.append("  Which channel owns it? Go on with this one, or skip it (the other one owns it): [c]ontinue / [s]kip > ")
    return "\n".join(lines)


def hidden_question(job: Mapping[str, Any]) -> str:
    return (f"  {_get(job, 'company')} is an agency that does not name the employer for \"{_get(job, 'title')}\". "
            "Go on with this ad anyway? [y/N] ")


def proceeds(reply: object) -> bool:
    """Only c / continue goes on; silence, Enter, s or anything else does not."""
    return str(reply or "").strip().lower() in ("c", "continue")


def agrees(reply: object) -> bool:
    return str(reply or "").strip().lower() in ("y", "yes")
