"""`careerops search-plan` (v3.0): the companies that still have no careers address, and a way to check addresses you are shown before they go into careers_urls.yaml.

    careerops search-plan [--limit 20] [--all]      the companies with no board and no entry in careers_urls.yaml: sponsor-register companies first, what each was asked
                                                    (its tokens and how many separate runs found nothing), and the board names the public list holds for it. Offline.
    careerops search-plan --check FILE              FILE is a CSV with the columns company,url[,evidence]: which reader each address would get, the reason for one that
                                                    gets none, and the lines to paste into careers_urls.yaml. Offline; nothing is fetched and nothing is written.

The search itself is not done by this tool. In an interactive Claude Code session, one WebSearch is run for each company in the list ("<company> careers jobs",
the employer's own page, not an aggregator), the address found and where it was seen (`evidence`) are written to a CSV (data/discover/careers_proposed.csv), and you read
that file, run `--check`, and paste the lines you accept. Nothing a search returned reaches careers_urls.yaml without you.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..apply.naming import folder_key
from . import ats as ats_mod
from . import boards as boards_mod
from . import careers, seeds as seeds_mod
from .dataset import Dataset


@dataclass
class Item:
    name: str
    on_register: bool = False
    tokens: list[str] = field(default_factory=list)
    misses: int = 0                                    # the most separate runs that found no board for any of its tokens at one provider
    listed: dict[str, dict[str, str]] = field(default_factory=dict)       # token -> {provider: slug} in the public board list
    workday: list[str] = field(default_factory=list)                    # Workday addresses the list holds (a suggestion, never read)


def board_names(settings) -> dict[str, list[str]]:
    """token -> the names of the boards already read for it (paths.discover/boards/<provider>-<token>.json)."""
    out: dict[str, list[str]] = {}
    d = settings.path("discover") / "boards"
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        prov, _, token = p.stem.partition("-")
        try:
            name = str(json.loads(p.read_text(encoding="utf-8")).get("name") or "")
        except (OSError, ValueError, AttributeError):
            continue
        out.setdefault(token, []).append(name)
    return out


def has_board(name: str, known: dict[str, list[str]]) -> bool:
    return any(boards_mod.names_match(n, name) for t in ats_mod.board_tokens(name) for n in known.get(t, []))


def pending(settings, db, *, limit: int | None = 20, dataset: Dataset | None = None) -> tuple[list[Item], int]:
    """(the first `limit` companies without an address and without a board, how many there are in all)."""
    known, have = board_names(settings), careers.keys(settings)
    index = boards_mod.BoardIndex(settings)
    ds = dataset if dataset is not None else Dataset(settings, None)           # no fetcher: the copy on disk or nothing; this command never asks the network
    items: list[Item] = []
    for seed in seeds_mod.seeds(settings, db):
        if folder_key(seed.name) in have or has_board(seed.name, known):
            continue
        it = Item(seed.name, seed.on_register)
        for token in ats_mod.board_tokens(seed.name, seed.hint):
            if index.reject_reason(token, seed.name):
                continue
            it.tokens.append(token)
            it.misses = max([it.misses, *(index.misses(a, token) for a in (*ats_mod.PROVIDERS, *index.more))])
            hit = ds.hits(token)
            if hit:
                it.listed[token] = hit
            it.workday += [u for u in ds.workday_urls(token) if u not in it.workday]
        items.append(it)
    items.sort(key=lambda i: (not i.on_register, i.name.lower()))
    return (items if limit is None else items[:limit]), len(items)


def plan_lines(items: list[Item], total: int, settings) -> list[str]:
    path = careers.path(settings)
    out = [f"search-plan: {total} compan(ies) with no board and no entry in {path.name}; showing {len(items)} (sponsor-register companies first)"]
    for n, it in enumerate(items, 1):
        bits = [f"tried: {', '.join(it.tokens) or 'nothing (no usable name)'}"]
        if it.misses:
            bits.append(f"no board found in {it.misses} run(s)")
        if it.listed:
            bits.append("in the public board list: " + "; ".join(f"{t} -> " + ", ".join(f"{p}/{s}" for p, s in h.items()) for t, h in it.listed.items()))
        if it.workday:
            bits.append("Workday site in the list (check it is theirs): " + ", ".join(it.workday))
        out.append(f"{n:3d}. {it.name}{'  [sponsor register]' if it.on_register else ''} | " + " | ".join(bits))
    out += ["",
            "Next, in an interactive Claude Code session: one WebSearch per company above (\"<company> careers jobs\"), take the employer's OWN careers address (not an aggregator),",
            "and save company,url,evidence to data/discover/careers_proposed.csv. Then:  careerops search-plan --check data/discover/careers_proposed.csv",
            f"Review the lines it prints and paste the ones you accept into {path} (this tool never writes that file)."]
    return out


def read_proposals(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def check_lines(settings, rows: list[dict]) -> list[str]:
    """For each proposed (company, url): the reader it would get, or why none; then the lines to paste. Offline."""
    have = careers.keys(settings)
    out, paste = [], []
    for r in rows:
        company, url, ev = r.get("company", ""), r.get("url", ""), r.get("evidence", "")
        if not company or not url:
            out.append(f"  skipped (needs company and url): {r}")
            continue
        board, why = careers.board_of(url, company, r.get("provider", "").lower())
        dup = " (already in careers_urls.yaml)" if folder_key(company) in have else ""
        if board is None:
            out.append(f"  NO READER  {company}: {url} | {why}{dup}")
            continue
        out.append(f"  {board.ats:<15} {company}: {url} | board {board.slug}{dup}" + (f" | seen: {ev[:90]}" if ev else ""))
        if not dup:
            paste.append(careers.yaml_line(company, url, "eploy" if board.ats == "eploy" else ""))
    head = [f"search-plan --check: {len(rows)} proposed address(es), {len(paste)} ready to paste"]
    tail = ["", f"Paste under `companies:` in {careers.path(settings)}:", *paste] if paste else []
    return head + out + tail
