"""Companies House bulk join + sector filter (M2). Spec: docs/IMPLEMENTATION.md §3.

Joins A-rated Skilled Worker sponsors to the monthly Free Company Data Product in one streaming pass:
O(N_companies_house) time, O(N_register) memory. Adds company number, status, SIC codes and a sector.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..db import DB

SIC_CODE = r"^\s*(\d{5})"          # SIC text looks like "62012 - Business and domestic software development"


@dataclass
class JoinStats:
    register_rows: int = 0
    exact: int = 0
    by_town: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    public_sector: int = 0
    in_scope: int = 0


def download_bulk(settings) -> list[Path]:
    """Fetch this month's zip parts into data/companies_house/YYYY-MM/; no-op if already present."""
    raise NotImplementedError("M2 — companies_house.download_bulk")


def stream_rows(paths: list[Path]) -> Iterator[dict]:
    """Yield one dict per company row across all parts; header names read at runtime, fail loudly if missing."""
    raise NotImplementedError("M2 — companies_house.stream_rows")


def classify(sic_codes: list[str], allow: dict[str, list[int]]) -> str | None:
    """First sector whose allow-list contains any of the company's SIC codes, else None."""
    raise NotImplementedError("M2 — companies_house.classify")


def join(db: DB, settings, paths: list[Path]) -> JoinStats:
    """Match register rows to Companies House rows (exact → town tie-break → ambiguous/none) and update `companies`."""
    raise NotImplementedError("M2 — companies_house.join")
