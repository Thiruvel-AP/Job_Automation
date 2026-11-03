"""Finding a company's job board from its name: guess board tokens, ask the public Greenhouse / Lever / Ashby APIs, remember the answer.

A token counts for a provider only when its API answers 200 with a jobs list (ats.is_board): a 404, an error body or an empty Lever
list is "no board there". Every answer, hit or miss, is kept for `discover.ats.board_cache_days` in paths.discover/boards.json, so
a company is not asked about again for a month.

A token that spells the whole company name (ats.full_name_tokens) is taken at its word. A SHORTENED token (the first word, the first two
words, the name without Group / Holdings / Technologies: `octagon` for Octagon Group) often finds another company's board, so it is
accepted only when the board's own name (the Greenhouse API gives one; Lever and Ashby do not) matches the company at
`discover.ats.board_name_match` (0.9). Otherwise it is `board_unverified`: no ad is read from it and the report lists it. An answer that is not an answer (a blocked host, a 5xx, no connection) is NOT kept:
it is reported as an error and asked again next run. Requests go through discovery/http.py: robots.txt, one a second per host.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Callable

from . import ats as ats_mod
from . import more_ats
from .ats import Board, Posting
from .http import Blocked, FetchError, Fetcher
from .register import normalise
from .seeds import Seed

BOARD_NAME_MATCH = 0.9             # default of discover.ats.board_name_match


class BoardIndex:
    """Which (provider, token) pairs are job boards and which are not, with the time each was asked: {"greenhouse:monzo": {...}}."""

    def __init__(self, settings, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path = settings.path("discover") / "boards.json"
        self.days = float(settings["discover"].get("ats", {}).get("board_cache_days", 30))
        self.name_match = float(settings["discover"].get("ats", {}).get("board_name_match", BOARD_NAME_MATCH))
        self.clock = clock
        wanted = (settings["discover"].get("ats") or {}).get("providers")        # discover.ats.providers: the v2.6 providers asked when a slug is guessed
        self.more = tuple(p for p in (wanted if isinstance(wanted, list) else more_ats.NAME_CHECKED) if p in more_ats.NAME_CHECKED)
        self.stopped: dict[str, str] = {}               # provider -> why: it answered 401 / 403 / 429 this run, so it is not asked again (never worked around)
        self.refused: dict[str, list] = {}              # provider -> [count, first message]: robots.txt (or a session requirement) said no this run
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

    def put(self, ats: str, token: str, found: bool, slug: str = "") -> None:
        self.data[f"{ats}:{token}"] = {"ts": self.clock().isoformat(timespec="seconds"), "found": found, **({"slug": slug} if slug else {})}

    def slug(self, ats: str, token: str) -> str:
        """The slug a provider answered for this token (Personio: the host that has the board), else the token."""
        return str((self.data.get(f"{ats}:{token}") or {}).get("slug") or token)

    def notes(self) -> list[str]:
        """One line per provider that was stopped or refused in this run (for the run's notes)."""
        out = [f"ats: {a} stopped for this run: {why}" for a, why in sorted(self.stopped.items())]
        out += [f"ats: {a} refused {n} request(s) (not asked again where it said no): {why}" for a, (n, why) in sorted(self.refused.items())]
        return out

    def stop(self, ats: str, e: Blocked) -> None:
        """A provider said no: a 401 / 403 / 429 stops the provider for the run; a robots.txt refusal or a session requirement is counted."""
        if getattr(e, "status", None):
            self.stopped.setdefault(ats, str(e))
        else:
            row = self.refused.setdefault(ats, [0, str(e)])
            row[0] += 1

    def name(self, ats: str, token: str) -> str | None:
        """The company name the board gave itself ('' = it has none), kept with the answer; None when it was never asked."""
        e = self.data.get(f"{ats}:{token}")
        return e.get("name") if e and self.get(ats, token) is not None else None

    def set_name(self, ats: str, token: str, name: str) -> None:
        if f"{ats}:{token}" in self.data:
            self.data[f"{ats}:{token}"]["name"] = name

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
    unverified: list[tuple[Board, str]] = field(default_factory=list)   # boards a shortened slug found whose name did not match: (board, why)
    errors: list[str] = field(default_factory=list)
    matched: int = 0                                      # postings in a target role family and a UK / remote-UK place

    def line(self) -> str:
        tried = ", ".join(t + (" (cached)" if t in self.cached else "") for t in self.tokens) or "-"
        board = ", ".join(f"{b.ats}/{b.slug}" for b in self.boards) or "none"
        extra = f" | board_unverified: {', '.join(f'{b.ats}/{b.slug}' for b, _ in self.unverified)}" if self.unverified else ""
        extra += f" | {len(self.errors)} error(s): {self.errors[0]}" if self.errors else ""
        reg = ", register" if self.seed.on_register else ""
        return f"  {self.seed.name} [{self.seed.origin}{reg}] | tried: {tried} | board: {board} | {self.matched} matched{extra}"


def names_match(board_name: str, company: str, threshold: float = BOARD_NAME_MATCH) -> bool:
    """Is this board's own company name the company's? Normalised (case, punctuation, Ltd / plc) and compared at `threshold`."""
    a, b = normalise(board_name or ""), normalise(company or "")
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= threshold


def verify_named(ats: str, token: str, company: str, index: BoardIndex, hint: str | None = None) -> tuple[bool, str]:
    """(accepted, why not) for a board of a v2.6 provider found by GUESSING a slug: the company name its own reply gives must match the company's at
    discover.ats.board_name_match (0.90), even for a slug that spells the whole name (a Workable `monzo` is not necessarily Monzo). A slug already verified
    for this company (`hint`) is taken at its word."""
    if hint and token == hint:
        return True, ""
    name = index.name(ats, token)
    if not name:
        return False, f"{ats}/{token} gives no company name, so the guessed slug '{token}' cannot be checked against '{company}'"
    if names_match(more_ats.clean_name(name), company, index.name_match):
        return True, ""
    return False, f"the {ats} board '{token}' is called '{name}', not '{company}' (needs a {index.name_match:.2f} match)"


def board_name(ats: str, token: str, fetcher: Fetcher, index: BoardIndex) -> str | None:
    """The company name a board gives itself: Greenhouse's API has one (one request, then kept with the cached answer); Lever's and
    Ashby's do not. None when there is none or it could not be read."""
    if ats != "greenhouse":
        return None
    cached = index.name(ats, token)
    if cached is not None:
        return cached or None
    try:
        data = fetcher.json(f"{ats_mod.GH}/{token}", what=f"Greenhouse board {token}")
    except FetchError:                                       # not an answer: not remembered
        return None
    name = str(data["name"]) if isinstance(data, dict) and data.get("name") else ""
    index.set_name(ats, token, name)
    return name or None


def verify_shortened(board: Board, company: str, fetcher: Fetcher, index: BoardIndex) -> tuple[bool, str]:
    """(accepted, why not) for a board found through a shortened slug: its own name must match the company's."""
    name = board_name(board.ats, board.slug, fetcher, index)
    if not name:
        if board.ats != "greenhouse":
            return False, (f"{board.ats}/{board.slug} gives no company name, so the shortened slug '{board.slug}' cannot be checked "
                           f"against '{company}'")
        return False, f"{board.ats}/{board.slug}: the board's name could not be read to check the shortened slug against '{company}'"
    if names_match(name, company, index.name_match):
        return True, ""
    return False, f"the {board.ats} board '{board.slug}' is called '{name}', not '{company}' (needs a {index.name_match:.2f} match)"


def find(seed: Seed, fetcher: Fetcher, index: BoardIndex, on_postings: Callable[[Board, list[Posting]], None] | None = None) -> Finding:
    """Try the company's tokens in order; the first token that is a board for any provider ends the search (all four providers
    are asked for that token). `on_postings(board, postings)` gets the list a fresh request already returned. A board found through a
    shortened token goes to `unverified` unless its name matches (see the top of this file)."""
    f = Finding(seed)
    full = ats_mod.full_name_tokens(seed.name)
    for token in ats_mod.board_tokens(seed.name, seed.hint):
        f.tokens.append(token)
        asked = False
        hits: list[tuple[Board, list[Posting] | None]] = []
        for ats in ats_mod.PROVIDERS:
            known = index.get(ats, token)
            if known is False:
                continue
            board = Board(ats, token, seed.name, seed.origin)
            if known is True:
                hits.append((board, None))
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
                hits.append((board, ats_mod.parse_postings(ats, data, seed.name)))
        for ats in index.more:                            # the v2.6 providers: their reply names the company, so every guess is name-checked
            if ats in index.stopped:
                continue
            known = index.get(ats, token)
            if known is False:
                continue
            slug = index.slug(ats, token)
            if known is True:
                hits.append((Board(ats, slug, seed.name, seed.origin), None))
                continue
            asked = True
            try:
                reply = more_ats.fetch(ats, token, fetcher, seed.name)
            except Blocked as e:
                index.stop(ats, e)
                continue
            except FetchError as e:
                f.errors.append(str(e))
                continue
            if reply.dns:
                continue
            index.put(ats, token, reply.found, reply.slug if reply.slug != token else "")
            if reply.found:
                index.set_name(ats, token, reply.name)
                hits.append((Board(ats, reply.slug or token, seed.name, seed.origin), None if reply.detail else reply.postings))
        for board, posts in hits:
            if board.ats in more_ats.NAME_CHECKED:
                ok, why = verify_named(board.ats, token, seed.name, index, seed.hint)
            else:
                ok, why = (True, "") if token in full or token == seed.hint else verify_shortened(board, seed.name, fetcher, index)
            if not ok:
                f.unverified.append((board, why))
                continue
            f.boards.append(board)
            if posts is not None and on_postings:
                on_postings(board, posts)
        if not asked:
            f.cached.add(token)
        if f.boards:
            break
    index.save()
    return f
