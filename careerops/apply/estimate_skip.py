"""The estimate stop before tailoring (v3.5.5). Zero tokens, no network.

`careerops live` ranks the approved files by the estimate of P x W (discovery/priority.py) before it spends a model call on any of them. An ad whose estimate F
(the zero-token ceiling, discovery/ceiling_estimate.py) is under `priority.min_estimate` (0.45) is not tailored: tailoring would skip it anyway (a CV scored under
apply.bands.review is skipped unless the honest ceiling is at rescue_ceiling or more, and the strict score cannot pass the ceiling), so the model call is saved.

The file moves to `paths.jds_done/skipped_estimate/` with a `<name>.reason.txt` beside it, and the decision log (data/evals/decisions.jsonl) records
`skipped_estimate` for it, so the shortlist holds it. It is reversible: `careerops unskip --shortlist --match ...` brings it back to the inbox, and the ad is then never
estimate-skipped again (your override). A job you promoted (`careerops promote`) is never stopped here either.

Not stopped: an ad with too few readable terms for F to mean anything (ceiling_estimate.MIN_TERMS: its F is shown, not acted on), and a file `live` was given
outside its approved list (`--include-unapproved`, `--url`, a file you name: your own choice stands). `priority.min_estimate: 0` switches the stop off. The estimate is
not exact (the first calibration's mean absolute error was 13 points): `calibrate --scores` prints how many tailored jobs the line would have held wrongly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import decided
from .hardstop import REASON_SUFFIX, Stop

FOLDER = "skipped_estimate"
DEFAULT_MIN = 0.45


def threshold(settings) -> float:
    """`priority.min_estimate` of settings.yaml (0.45 when the key is missing); 0 or less = the stop is off."""
    return float(((settings.raw.get("priority") or {}).get("min_estimate", DEFAULT_MIN)))


def folder(settings) -> Path:
    """Where a stopped JD file goes: paths.jds_done/skipped_estimate/."""
    return decided.decision_dir(settings, FOLDER)


def exempt_keys(settings) -> set[str]:
    """The jobs never stopped here: the latest decision is your `promote`, or you once `unskip`ped it after an estimate stop (the log holds a reset of a
    `skipped_estimate` for that key)."""
    from ..discovery import decide

    out = {k for k, e in decide.latest(settings).items() if e.get("decision") == decide.PROMOTED}
    out |= {e["key"] for e in decide.read_log(settings) if e.get("decision") == decide.RESET and e.get("was") == decide.ESTIMATE_SKIP}
    return out


def find(ranked: Iterable[tuple[Path, Any]], settings) -> list[Stop]:
    """The (path, Priority) pairs of `ranked` the stop applies to: F known and under the line, the job not exempt. The reason carries F and the line."""
    line = threshold(settings)
    if line <= 0:
        return []
    exempt = exempt_keys(settings)
    out = []
    for path, pr in ranked:
        if pr is None or not pr.f_known or pr.f >= line:
            continue
        hdr = decided.header_of(path)
        company, title = hdr.get("company") or "", hdr.get("title") or ""
        st = Stop(path, company, title, f"estimate F {pr.f:.2f} is under the {line:.2f} line (priority.min_estimate): the ad's terms the fact bank backs, plus the terms the "
                                        "model is expected to add, do not reach it, so tailoring would end under the review band")
        if st.key not in exempt:
            out.append(st)
    return out


def file_away(settings, stops: Sequence[Stop], *, now: datetime | None = None) -> list[Path]:
    """Move each stopped file to skipped_estimate/, write its reason beside it, and log `skipped_estimate` for it (append-only). Returns the new paths."""
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
        events.append({"key": st.key, "job_id": decided.file_job_id(dst) or "", "company": st.company, "title": st.title, "decision": decide.ESTIMATE_SKIP,
                       "by": "agent", "reason": f"estimate stop before tailoring: {st.reason}", "file": str(dst)})
    decide.log(settings, events, now)
    return out


def jd_files(settings) -> list[Path]:
    """The JD files in skipped_estimate/ (their .reason.txt notes aside)."""
    return [p for p in decided.txt_files(folder(settings)) if not p.name.endswith(REASON_SUFFIX)]


def restore(settings, keys: set[str]) -> list[tuple[Path, Path]]:
    """`unskip --shortlist`: the stopped files of `keys` go back to the inbox (their reason note stays as the record: nothing is deleted). Returns (from, to) pairs."""
    moved = []
    for p in jd_files(settings):
        hdr = decided.header_of(p)
        if "|".join(decided.key_of(hdr.get("company"), hdr.get("title"))) not in keys:
            continue
        moved.append((p, decided.move_file(settings, p, decided.inbox_dir(settings))))
    return moved


def lines(stops: Sequence[Stop], *, applied: bool) -> list[str]:
    verb = "moved to" if applied else "would move to"
    return [f"  estimate stop: {st.path.parent.name}/{st.path.name} ({st.company} | {st.title}): {st.reason} -> {verb} jds_done/{FOLDER}/" for st in stops]
