"""Finding a company's job board from its name: guess board tokens, ask the public Greenhouse / Lever / Ashby APIs, remember the answer.

A token counts for a provider only when its API answers 200 with a jobs list (ats.is_board): a 404, an error body or an empty Lever
list is "no board there". Every answer, hit or miss, is kept for `discover.ats.board_cache_days` in paths.discover/boards.json, so
a company is not asked about again for a month. An answer that is not an answer (a blocked host, a 5xx, no connection) is NOT kept:
it is reported as an error and asked again next run. Requests go through discovery/http.py: robots.txt, one a second per host.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import ats as ats_mod
from .ats import Board, Posting
from .http import FetchError, Fetcher
from .seeds import Seed


class BoardIndex:
    """Which (provider, token) pairs are job boards and which are not, with the time each was asked: {"greenhouse:monzo": {...}}."""

    def __init__(self, settings, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path = settings.path("discover") / "boards.json"
        self.days = float(settings["discover"].get("ats", {}).get("board_cache_days", 30))
        self.clock = clock
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data: dict[str, dict] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, ats: str, token: str) -> bool | None:
        """True / False when the pair was asked inside the cache window, None when it is unknown or the answer is stale."""
        e = self.data.get(f"{ats}:{token}")
        try:
            if e and self.clock() - datetime.fromisoformat(e["ts"]) < timedelta(days=self.days):
                return bool(e["found"])
        except (KeyError, ValueError, TypeError):
            pass
        return None

    def put(self, ats: str, token: str, found: bool) -> None:
        self.data[f"{ats}:{token}"] = {"ts": self.clock().isoformat(timespec="seconds"), "found": found}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        except OSError:
            pass


@dataclass
class Finding:
    """What the run did for one company: the tokens tried, the board(s) found and the jobs that matched."""
    seed: Seed
    tokens: list[str] = field(default_factory=list)
    cached: set[str] = field(default_factory=set)         # tokens answered from boards.json, no request sent
    boards: list[Board] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    matched: int = 0                                      # postings in a target role family and a UK / remote-UK place

    def line(self) -> str:
        tried = ", ".join(t + (" (cached)" if t in self.cached else "") for t in self.tokens) or "-"
        board = ", ".join(f"{b.ats}/{b.slug}" for b in self.boards) or "none"
        extra = f" | {len(self.errors)} error(s): {self.errors[0]}" if self.errors else ""
        reg = ", register" if self.seed.on_register else ""
        return f"  {self.seed.name} [{self.seed.origin}{reg}] | tried: {tried} | board: {board} | {self.matched} matched{extra}"


def find(seed: Seed, fetcher: Fetcher, index: BoardIndex, on_postings: Callable[[Board, list[Posting]], None] | None = None) -> Finding:
    """Try the company's tokens in order; the first token that is a board for any provider ends the search (all four providers
    are asked for that token). `on_postings(board, postings)` gets the list a fresh request already returned."""
    f = Finding(seed)
    for token in ats_mod.board_tokens(seed.name, seed.hint):
        f.tokens.append(token)
        asked = False
        for ats in ats_mod.PROVIDERS:
            known = index.get(ats, token)
            if known is False:
                continue
            board = Board(ats, token, seed.name, seed.origin)
            if known is True:
                f.boards.append(board)
                continue
            asked = True
            url, params, what = ats_mod.board_request(ats, token)
            try:
                data = fetcher.json(url, params=params, what=what)
            except FetchError as e:                       # Blocked is a FetchError: not an answer, so not remembered
                f.errors.append(str(e))
                continue
            found = ats_mod.is_board(ats, data)
            index.put(ats, token, found)
            if found:
                f.boards.append(board)
                if on_postings:
                    on_postings(board, ats_mod.parse_postings(ats, data, seed.name))
        if not asked:
            f.cached.add(token)
        if f.boards:
            break
    index.save()
    return f
