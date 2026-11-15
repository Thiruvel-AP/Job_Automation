"""Tailoring attempts of one job: nothing is ever deleted, moved or overwritten, the best one is just pointed at.

Layout under `paths.outputs`:
    <job>/                  attempt 0: the first tailoring (and every folder made before attempts existed)
    <job>/attempts/<n>/     attempt n >= 1: each later tailoring (review's [r] / [f], `tailor --id`); n only increases, a folder is never reused

Every attempt's report.json carries "attempt": n and "current_attempt": c, c = the attempt with the highest strict ATS among all attempts up to and
including n (a tie keeps the earlier one). The newest attempt's report therefore names the current attempt. `jobs.out_dir` in the database always
points at the current attempt's folder, so the review screen, [o], the form upload, the archive and the judge all read it without knowing about attempts.
"""
from __future__ import annotations

from pathlib import Path

from .. import pathstore

SUB = "attempts"
EPS = 1e-9


def job_folder(path: Path | str) -> Path:
    """The job's own folder for either an attempt folder (`<job>/attempts/<n>`) or the job folder itself."""
    p = Path(path)
    return p.parent.parent if p.parent.name == SUB and p.name.isdigit() else p


def job_name(path: Path | str) -> str:
    return job_folder(path).name


def folder_of(job_dir: Path, n: int) -> Path:
    return job_dir if n == 0 else job_dir / SUB / str(n)


def numbers(job_dir: Path) -> list[int]:
    """Every attempt number that has a folder (0 = the job folder itself when it holds a report), ascending."""
    found = [int(p.name) for p in (job_dir / SUB).iterdir() if p.is_dir() and p.name.isdigit()] if (job_dir / SUB).is_dir() else []
    if (job_dir / "report.json").is_file():
        found.append(0)
    return sorted(found)


def next_number(job_dir: Path) -> int:
    """The number the next attempt gets: one above every attempt folder that exists, with or without a report. Never reuses a number."""
    return max([0, *numbers(job_dir)]) + 1


def strict_of_report(rep: dict) -> float | None:
    value = (rep.get("ats") or {}).get("strict") if isinstance(rep, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def strict_of(folder: Path) -> float | None:
    try:
        return strict_of_report(pathstore.read_report(folder / "report.json"))
    except (OSError, ValueError, TypeError):
        return None


def choose(scores: dict[int, float | None]) -> int:
    """The attempt with the highest strict ATS; a tie (or no score at all) keeps the earlier one. An attempt without a score loses to any with one."""
    best, best_score = min(scores), None
    for n in sorted(scores):
        s = scores[n]
        if s is not None and (best_score is None or s > best_score + EPS):
            best, best_score = n, s
    return best


def current_after(job_dir: Path, n: int, strict: float | None) -> int:
    """`current_attempt` for a new attempt n (not yet placed) that scored `strict`, given the attempts already on disk."""
    scores: dict[int, float | None] = {k: strict_of(folder_of(job_dir, k)) for k in numbers(job_dir) if k != n}
    scores[n] = strict
    return choose(scores)


def current_number(job_dir: Path) -> int | None:
    """The current attempt as the job's newest report names it; the best by score when that report predates attempts; None with no attempt."""
    nums = numbers(job_dir)
    if not nums:
        return None
    try:
        named = pathstore.read_report(folder_of(job_dir, nums[-1]) / "report.json").get("current_attempt")
    except (OSError, ValueError):
        named = None
    if isinstance(named, int) and named in nums:
        return named
    return choose({k: strict_of(folder_of(job_dir, k)) for k in nums})


def current_dir(job_dir: Path) -> Path:
    """The folder of the current attempt (the job folder itself when it has no attempts or nothing can be read)."""
    n = current_number(job_dir)
    return folder_of(job_dir, n) if n is not None else job_dir
