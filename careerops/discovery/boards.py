"""Finding a company's job board from its name: guess board tokens, ask the public Greenhouse / Lever / Ashby APIs, remember the answer.

A token counts for a provider only when its API answers 200 with a jobs list (ats.is_board): a 404, an error body or an empty Lever
list is "no board there". Every answer, hit or miss, is kept for `discover.ats.board_cache_days` in paths.discover/boards.json, so
a company is not asked about again for a month.

A token that spells the whole company name (ats.full_name_tokens) is taken at its word. A SHORTENED token (the first word, the first two
words, the name without Group / Holdings / Technologies: `octagon` for Octagon Group) often finds another company's board, so it is
accepted only when the board's own name (the Greenhouse API gives one; Lever and Ashby do not) matches the company at
`discover.ats.board_name_match` (0.9). Otherwise it is `board_unverified`: no ad is read from it and the report lists it. An answer that is not an answer (a blocked host, a 5xx, no connection) is NOT kept:
it is reported as an error and asked again next run. Requests go through discovery/http.py: robots.txt, one a second per host.

v2.8: a token that is not a company name is never asked (ats.slug_reject_reason: over 30 characters, a run of four digits, a postcode; ats.name_reject_reason: a
name holding a postcode or a salary; a name or token in discover.ignore_companies), and the report names it. At most `discover.ats.max_guesses_per_provider` (60)
tokens are asked of each provider in a run (the next run carries on: an answered token is cached). "No board" is kept 30 days for every provider, also when the host
does not exist (kept once the run has had any real answer, so an outage is not remembered) or its robots.txt only redirects (a company-per-host provider's answer for a
name nobody has: board none, not a refusal). The run reports guesses and requests sent per provider.
"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Callable

from ..apply.naming import folder_key
from . import ats as ats_mod
from . import more_ats
from .ats import Board, Posting
from .http import Blocked, FetchError, Fetcher
from .register import normalise
from .seeds import Seed

BOARD_NAME_MATCH = 0.9             # default of discover.ats.board_name_match
MAX_GUESSES = 60                   # default of discover.ats.max_guesses_per_provider
REJECT_EXAMPLES = 10               # rejected tokens named in the run record


def sent(fetcher) -> int:
    """The requests a fetcher has sent so far, robots.txt included (0 for a stand-in that does not count them)."""
    try:
        return int(sum(fetcher.by_host.values()))
    except (AttributeError, TypeError):
        return 0


class BoardIndex:
    """Which (provider, token) pairs are job boards and which are not, with the time each was asked: {"greenhouse:monzo": {...}}."""

    def __init__(self, settings, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path = settings.path("discover") / "boards.json"
        self.days = float(settings["discover"].get("ats", {}).get("board_cache_days", 30))
        self.name_match = float(settings["discover"].get("ats", {}).get("board_name_match", BOARD_NAME_MATCH))
        self.max_guesses = max(0, int(settings["discover"].get("ats", {}).get("max_guesses_per_provider", MAX_GUESSES)))
        self.ignored = {k for k in (folder_key(str(n)) for n in settings["discover"].get("ignore_companies") or []) if k}   # folders that are not companies
        self.clock = clock
        wanted = (settings["discover"].get("ats") or {}).get("providers")        # discover.ats.providers: the v2.6 providers asked when a slug is guessed
        self.more = tuple(p for p in (wanted if isinstance(wanted, list) else more_ats.NAME_CHECKED) if p in more_ats.NAME_CHECKED)
        self.stopped: dict[str, str] = {}               # provider -> why: it answered 401 / 403 / 429 this run, so it is not asked again (never worked around)
        self.refused: dict[str, list] = {}              # provider -> [count, first message]: robots.txt (or a session requirement) said no this run
        self.refusals: dict[str, int] = {}              # provider -> every request it refused this run, of either kind (the "refused requests per provider" report)
        self.guesses: Counter = Counter()               # provider -> (token) guesses asked this run: not in the cache, so a request each
        self.requests: Counter = Counter()              # provider -> requests those guesses sent (robots.txt included)
        self.cache_hits: Counter = Counter()            # provider -> guesses answered from boards.json (hit or miss), nothing sent
        self.over_cap: Counter = Counter()              # provider -> guesses left for the next run because max_guesses_per_provider was reached
        self.reject_count = 0                           # tokens never asked because they are not company names
        self.reject_examples: list[str] = []
        self.answered = False                           # has any provider given a real answer this run (so a host that does not resolve is not an outage)
        self.dns_missing: set[tuple[str, str]] = set()  # (provider, token) whose host did not resolve: kept as "no board" once the run has had a real answer
        self.dns_kept: set[tuple[str, str]] = set()     # ... of those, the ones already written to the cache
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
        self.answered = True
        self.data[f"{ats}:{token}"] = {"ts": self.clock().isoformat(timespec="seconds"), "found": found, **({"slug": slug} if slug else {})}

    def slug(self, ats: str, token: str) -> str:
        """The slug a provider answered for this token (Personio: the host that has the board), else the token."""
        return str((self.data.get(f"{ats}:{token}") or {}).get("slug") or token)

    def notes(self) -> list[str]:
        """One line per provider that was stopped or refused in this run (for the run's notes)."""
        out = [f"ats: {a} stopped for this run: {why}" for a, why in sorted(self.stopped.items())]
        out += [f"ats: {a} refused {n} request(s) (not asked again where it said no): {why}" for a, (n, why) in sorted(self.refused.items())]
        out += [f"ats: {a} guesses stopped at the limit of {self.max_guesses} a run (discover.ats.max_guesses_per_provider): {n} token(s) left for the next run"
                for a, n in sorted(self.over_cap.items()) if n]
        return out

    # ---- v2.8: what is not guessed, how much is guessed
    def reject_reason(self, token: str, name: str = "") -> str | None:
        """Why this token (made from `name`) is not asked: the name or the slug is a folder in discover.ignore_companies, or it is not a company name
        (ats.name_reject_reason / slug_reject_reason). None when it can be a board."""
        if name and folder_key(name) in self.ignored:
            return "the name is in discover.ignore_companies"
        if token and folder_key(token) in self.ignored:
            return "the slug is a folder named in discover.ignore_companies"
        return ats_mod.name_reject_reason(name) or ats_mod.slug_reject_reason(token)

    def note_reject(self, name: str, token: str, why: str) -> None:
        self.reject_count += 1
        if len(self.reject_examples) < REJECT_EXAMPLES:
            self.reject_examples.append(f"{token} (for '{name}'): {why}")

    def may_guess(self, ats: str) -> bool:
        """May one more token be asked of this provider in this run? False once max_guesses_per_provider are used: the guess is counted as left over."""
        if self.guesses[ats] >= self.max_guesses:
            self.over_cap[ats] += 1
            return False
        return True

    @contextmanager
    def counting(self, ats: str, fetcher):
        """Around the request(s) of one guess: it counts as a guess, and the requests the fetcher sent meanwhile (robots.txt included) are added to the provider's."""
        before = sent(fetcher)
        self.guesses[ats] += 1
        try:
            yield
        finally:
            self.requests[ats] += max(0, sent(fetcher) - before)

    def note_dns(self, ats: str, token: str) -> None:
        """A company-per-host provider whose host for this token does not resolve: no such board. Kept (30 days) once the run has had a real answer; if the
        whole run is offline nothing is remembered."""
        self.dns_missing.add((ats, token))

    def dns_missed(self, ats: str, token: str) -> bool:
        return (ats, token) in self.dns_missing

    def guess_counts(self) -> dict[str, dict[str, int]]:
        """provider -> {guesses, requests, cached, over_cap} of this run (the run record)."""
        names = sorted({*self.guesses, *self.requests, *self.cache_hits, *self.over_cap})
        return {a: {"guesses": self.guesses[a], "requests": self.requests[a], "cached": self.cache_hits[a], "over_cap": self.over_cap[a]} for a in names}

    def reject_summary(self) -> dict:
        return {"count": self.reject_count, "examples": list(self.reject_examples)}

    def refused_counts(self) -> dict[str, int]:
        """provider -> how many requests it refused this run (a 401 / 403 / 429, a robots.txt refusal, a session requirement), for the run record."""
        return dict(sorted(self.refusals.items()))

    def stop(self, ats: str, e: Blocked) -> None:
        """A provider said no: a 401 / 403 / 429 stops the provider for the run; a robots.txt refusal or a session requirement is counted."""
        self.refusals[ats] = self.refusals.get(ats, 0) + 1
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
        if self.answered:
            for ats, token in sorted(self.dns_missing - self.dns_kept):
                if self.get(ats, token) is None:
                    self.put(ats, token, False)
                self.dns_kept.add((ats, token))
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
    rejected: list[tuple[str, str]] = field(default_factory=list)       # (token, why) never asked: not a company name
    capped: set[str] = field(default_factory=set)         # providers a token was not asked of because the run's guess limit was reached
    providers: list[str] = field(default_factory=list)    # the providers a token is asked of

    def line(self) -> str:
        tried = ", ".join(t + (" (cached)" if t in self.cached else "") for t in self.tokens) or "-"
        board = ", ".join(f"{b.ats}/{b.slug}" for b in self.boards) or "none"
        extra = f" | board_unverified: {', '.join(f'{b.ats}/{b.slug}' for b, _ in self.unverified)}" if self.unverified else ""
        extra += f" | {len(self.errors)} error(s): {self.errors[0]}" if self.errors else ""
        extra += f" | rejected: {', '.join(f'{t} ({w})' for t, w in self.rejected)}" if self.rejected else ""
        extra += f" | not asked (guess limit): {', '.join(sorted(self.capped))}" if self.capped else ""
        reg = ", register" if self.seed.on_register else ""
        return f"  {self.seed.name} [{self.seed.origin}{reg}] | tried: {tried} | board: {board} | {self.matched} matched{extra}"

    def tried_text(self) -> str:
        """What was done to find this company's board, in a sentence (the trace of an alert that could not be resolved)."""
        bits = []
        if self.tokens:
            asked = ", ".join(t + (" (answered earlier: no board)" if t in self.cached else "") for t in self.tokens)
            bits.append(f"tokens {asked} asked of {', '.join(self.providers) or 'the providers'}: " + ("a board was found but not verified" if self.unverified else "none is a board"))
        if self.rejected:
            bits.append("not asked, not a company name: " + "; ".join(f"{t} ({w})" for t, w in self.rejected))
        if self.capped:
            bits.append("not asked, the run's guess limit was reached for " + ", ".join(sorted(self.capped)))
        if self.unverified:
            bits.append("board_unverified: " + "; ".join(f"{b.ats}/{b.slug}: {w}" for b, w in self.unverified))
        if self.errors:
            bits.append(f"{len(self.errors)} error(s), the first: {self.errors[0]}")
        return "; ".join(bits) or "no board token could be made from the name"


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
    f = Finding(seed, providers=[*ats_mod.PROVIDERS, *index.more])
    full = ats_mod.full_name_tokens(seed.name)
    for token in ats_mod.board_tokens(seed.name, seed.hint):
        why = None if token == seed.hint else index.reject_reason(token, seed.name)
        if why:                                           # not a company name: not asked, and the report says so
            f.rejected.append((token, why))
            index.note_reject(seed.name, token, why)
            continue
        f.tokens.append(token)
        asked = skipped = False
        hits: list[tuple[Board, list[Posting] | None]] = []
        for ats in ats_mod.PROVIDERS:
            known = index.get(ats, token)
            if known is False:
                index.cache_hits[ats] += 1
                continue
            board = Board(ats, token, seed.name, seed.origin)
            if known is True:
                index.cache_hits[ats] += 1
                hits.append((board, None))
                continue
            if not index.may_guess(ats):                  # the run's limit for this provider: left for the next run (nothing is cached for it)
                f.capped.add(ats)
                skipped = True
                continue
            asked = True
            url, params, what = ats_mod.board_request(ats, token)
            try:
                with index.counting(ats, fetcher):
                    data = fetcher.json(url, params=params, what=what)
            except FetchError as e:                       # Blocked is a FetchError: not an answer, so not remembered
                if isinstance(e, Blocked):
                    index.refusals[ats] = index.refusals.get(ats, 0) + 1
                f.errors.append(str(e))
                continue
            found = ats_mod.is_board(ats, data)
            index.put(ats, token, found)
            if found:
                hits.append((board, ats_mod.parse_postings(ats, data, seed.name)))
        for ats in index.more:                            # the v2.6 providers whose reply names the company (more_ats.NAME_CHECKED): every guess is name-checked
            if ats in index.stopped:
                continue
            known = index.get(ats, token)
            if known is False or (known is None and index.dns_missed(ats, token)):
                index.cache_hits[ats] += 1
                continue
            slug = index.slug(ats, token)
            if known is True:
                index.cache_hits[ats] += 1
                hits.append((Board(ats, slug, seed.name, seed.origin), None))
                continue
            if not index.may_guess(ats):
                f.capped.add(ats)
                skipped = True
                continue
            asked = True
            try:
                with index.counting(ats, fetcher):
                    reply = more_ats.fetch(ats, token, fetcher, seed.name)
            except Blocked as e:
                index.stop(ats, e)
                continue
            except FetchError as e:
                f.errors.append(str(e))
                continue
            if reply.dns:                                 # no such host: no such board (kept once the run has had a real answer)
                index.note_dns(ats, token)
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
        if not asked and not skipped:
            f.cached.add(token)
        if f.boards:
            break
    index.save()
    return f
