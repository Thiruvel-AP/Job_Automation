"""The hard stops again, just before tailoring (v3.5.4). Zero tokens, no network.

An ad approved on an earlier day was approved under that day's rules. Before `careerops live` spends a model call on it, every source file is
checked once more against the rules that need nothing but the file itself:

  * a fee-charging training scheme sold as a job (discovery/blacklist.training_scheme),
  * an excluded level in the title (gates.level_excluded: roles.levels.exclude plus chief / head of / vp / director / principal / staff),
  * your blacklist (config/profile/blacklist.yaml).

A file that fails moves to `paths.jds_done/skipped_agent/` with a `<name>.reason.txt` beside it, and the agent's decision log
(data/evals/decisions.jsonl) records AGENT-SKIP with the reason, so the shortlist holds it. `careerops unskip --shortlist --match ...` brings it
back to the inbox. A job you promoted (`careerops promote`) is your override and is never stopped here.

The region is NOT one of these stops: an ad outside the chosen region is left alone (parked), never skipped (live's region gate does that first).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..discovery import blacklist, gates, manual
from . import decided

FOLDER = "skipped_agent"
REASON_SUFFIX = ".reason.txt"


@dataclass(frozen=True)
class Stop:
    path: Path
    company: str
    title: str
    reason: str

    @property
    def key(self) -> str:
        return "|".join(decided.key_of(self.company, self.title))


def folder(settings) -> Path:
    """Where a stopped JD file goes: paths.jds_done/skipped_agent/."""
    return decided.decision_dir(settings, FOLDER)


def reason_of(path: Path, settings) -> Stop | None:
    """The first hard stop the file states, else None. A file that cannot be read as a JD is not stopped here (live rejects it with its own reason)."""
    try:
        text, _ = manual.read_jd(path)
    except manual.JDError:
        return None
    hdr = manual.header_fields(text)
    company, title = hdr.get("company") or "", hdr.get("title") or ""
    if (scheme := blacklist.training_scheme(company, text)):
        return Stop(path, company, title, f"{blacklist.TRAINING_SCHEME_LABEL}: {scheme}")
    if (lvl := gates.level_excluded(title, settings)):
        return Stop(path, company, title, f"level excluded ({lvl})")
    try:
        hit = blacklist.load(blacklist.path_for(settings)).hit(company, hdr.get("url") or "", hdr.get("apply_url") or "")
    except blacklist.BlacklistError:
        hit = None                           # a broken blacklist file is reported by the shortlist; it never stops a job by itself
    if hit is not None:
        return Stop(path, company, title, f"{company} is on your blacklist" + (f": {hit.reason}" if hit.reason else ""))
    return None


def promoted_keys(settings) -> set[str]:
    """The jobs whose latest agent decision is your `promote` override: never stopped by this check."""
    from ..discovery import decide

    return {k for k, e in decide.latest(settings).items() if e.get("decision") == decide.PROMOTED}


def find(files: Iterable[Path], settings) -> list[Stop]:
    """Every file of `files` a hard stop applies to (your promoted jobs aside)."""
    exempt = promoted_keys(settings)
    out = []
    for p in files:
        st = reason_of(p, settings)
        if st is not None and st.key not in exempt:
            out.append(st)
    return out


def file_away(settings, stops: Sequence[Stop], *, now: datetime | None = None) -> list[Path]:
    """Move each stopped file to skipped_agent/, write its reason beside it, and log AGENT-SKIP for it (append-only). Returns the new paths."""
    from ..discovery import decide

    now = now or datetime.now(timezone.utc)
    out, events = [], []
    for st in stops:
        if not st.path.is_file():
            continue
        dst = decided.move_file(settings, st.path, folder(settings))
        (dst.parent / (dst.stem + REASON_SUFFIX)).write_text(f"{st.reason}\n(from {st.path.parent.name}/, {now:%Y-%m-%d %H:%M} UTC; "
                                                             "`careerops unskip --shortlist --match ...` brings it back)\n", encoding="utf-8")
        out.append(dst)
        events.append({"key": st.key, "job_id": decided.file_job_id(dst) or "", "company": st.company, "title": st.title, "decision": decide.SKIP,
                       "by": "agent", "reason": f"hard stop before tailoring: {st.reason}", "file": str(dst)})
    decide.log(settings, events, now)
    return out


def jd_files(settings) -> list[Path]:
    """The JD files in skipped_agent/ (their .reason.txt notes aside)."""
    return [p for p in decided.txt_files(folder(settings)) if not p.name.endswith(REASON_SUFFIX)]


def restore(settings, keys: set[str]) -> list[tuple[Path, Path]]:
    """`unskip --shortlist`: the stopped files of `keys` go back to the inbox (their reason note stays in skipped_agent/ as the record). Returns (from, to) pairs."""
    moved = []
    for p in jd_files(settings):
        hdr = decided.header_of(p)
        if "|".join(decided.key_of(hdr.get("company"), hdr.get("title"))) not in keys:
            continue
        moved.append((p, decided.move_file(settings, p, decided.inbox_dir(settings))))      # its reason note stays as the record (nothing is deleted)
    return moved


def lines(stops: Sequence[Stop], *, applied: bool) -> list[str]:
    if not stops:
        return ["  hard stops before tailoring: none"]
    verb = "moved to" if applied else "would move to"
    return [f"  hard stop: {st.path.parent.name}/{st.path.name} ({st.company} | {st.title}): {st.reason} -> {verb} jds_done/{FOLDER}/" for st in stops]
