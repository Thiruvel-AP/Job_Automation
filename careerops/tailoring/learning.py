"""Learning list (M4). Spec: docs/IMPLEMENTATION.md §5.

Skills a JD asks for that the fact bank has no evidence for are never put on the CV; they are ranked here:

    priority(k) = Σ_{j ∈ jobs} w_{k,j} · 1[k is a genuine gap in j]      (w = 2 required, 1 preferred)

The top of the list is the skill that would lift the most applications above the 70 % floor.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import DB


@dataclass
class Gap:
    term: str
    priority: float
    jobs: int
    required_in: int


def per_job(report: dict) -> list[dict]:
    """Genuine gaps for one tailored job, with weight and required flag."""
    raise NotImplementedError("M4 — learning.per_job")


def aggregate(db: DB) -> list[Gap]:
    """Weighted gap frequency across every tailored job, highest priority first."""
    raise NotImplementedError("M4 — learning.aggregate")
