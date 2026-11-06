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

v3.0: guessing is the last resort, not the first. (1) A company with an entry in careers_urls.yaml is not guessed at: its address is the answer (careers.py). (2) A name
that is a slug in the public board list (dataset.py) is checked with ONE request to the provider that lists it, and nothing else is guessed for that name (a separate
limit, `discover.ats.dataset_checks_per_provider`, 60). (3) What is left is guessed only on Greenhouse, Lever (both hosts), Ashby, Workable and SmartRecruiters, at most 20
tokens per provider and run (`max_guesses_per_provider`, which cannot be set higher), and never on a provider with one host per company (Teamtailor, Recruitee, Personio,
Pinpoint): an unknown company there is a host that does not exist. (4) "No board" is believed only after `discover.ats.misses_needed` (3) separate runs each found none;
until then the name is asked again next run (an answered token is not asked twice in one run). A board found resets the count.

v3.1: no blind guesses. A company is asked of a provider ONLY when its normalised name matches a board in the public list (dataset.match: the same name in letters
and digits, or a 0.90 match), with one verifying request; a Workday tenant the list matches is verified through its CXS endpoint (one POST) and then read like any
board. A name the list does not hold is not asked anywhere (a board found earlier, still in boards.json, is still read). The old guessing comes back only with
`discover.ats.blind_guesses: true`, and then within max_guesses_per_provider (default now 0, never above 20). The run reports, per provider, the names matched from
the list, verified, and failed.
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
from . import careers as careers_mod
from . import more_ats
from .ats import Board, Posting
from .http import Blocked, FetchError, Fetcher
from .register import normalise
from .seeds import Seed

BOARD_NAME_MATCH = 0.9             # default of discover.ats.board_name_match
MAX_GUESSES = 0                    # default of discover.ats.max_guesses_per_provider (v3.1: no blind guesses by default; v3.0: 20; before: 60)
GUESS_CEILING = 20                 # ... and the most it can be set to (v3.0: "guess only what is left, at most 20 per provider per run")
DIRECTED_MAX = 60                  # default of discover.ats.dataset_checks_per_provider: names the public list holds, checked once each
MISSES_NEEDED = 3                  # default of discover.ats.misses_needed: separate runs that must each find no board before "no board" is believed
REJECT_EXAMPLES = 10               # rejected tokens named in the run record
SEARCH_FIELDS = ("source", "query_id", "date", "company", "name")        # what an agent-search board carries in boards.json (agentsearch.py)


def search_boards(settings) -> list[Board]:
    """v3.1: the boards the agent search added to paths.discover/boards.json (`source: search`, still a board), under the company name it gave them (origin
    `search`). Read by ats.configured_boards, so `discover --ats` reads them every run and an alert of the company resolves on them (board_known). The cache
    window does not apply: a board stays until a check finds it gone."""
    try:
        data = json.loads((settings.path("discover") / "boards.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for key, e in (data.items() if isinstance(data, dict) else []):
        if not isinstance(e, dict) or e.get("source") != "search" or not e.get("found"):
            continue
        ats, _, token = key.partition(":")
        if ats and token:
            slug = str(e.get("slug") or token)
            out.append(Board(ats, slug, str(e.get("company") or e.get("name") or slug), "search"))
    return out


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
        self.blind = (settings["discover"].get("ats") or {}).get("blind_guesses") is True       # v3.1: guessing slugs is opt-in
        self.max_guesses = min(GUESS_CEILING, max(0, int(settings["discover"].get("ats", {}).get("max_guesses_per_provider", MAX_GUESSES)))) if self.blind else 0
        self.list_matched: Counter = Counter()          # provider -> companies whose name matched a board in the public list this run (v3.1)
        self.list_verified: Counter = Counter()         # ... whose verifying request found the board
        self.list_failed: Counter = Counter()           # ... whose verifying request found none, or failed
        self.directed_max = max(0, int(settings["discover"].get("ats", {}).get("dataset_checks_per_provider", DIRECTED_MAX)))
        self.misses_needed = max(1, int(settings["discover"].get("ats", {}).get("misses_needed", MISSES_NEEDED)))
        self.run_id = clock().isoformat(timespec="microseconds")          # a "separate run" is a different BoardIndex
        self.ignored = {k for k in (folder_key(str(n)) for n in settings["discover"].get("ignore_companies") or []) if k}   # folders that are not companies
        self.clock = clock
        wanted = (settings["discover"].get("ats") or {}).get("providers")        # discover.ats.providers: the v2.6 providers asked when a slug is guessed
        asked_for = [p for p in (wanted if isinstance(wanted, list) else more_ats.NAME_CHECKED) if p in more_ats.NAME_CHECKED]
        self.more = tuple(p for p in asked_for if p not in more_ats.SUBDOMAIN)         # v3.0: never guessed on a provider with one host per company
        self.not_guessed = [p for p in asked_for if p in more_ats.SUBDOMAIN]
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
        self.directed: Counter = Counter()              # provider -> names the public list holds, checked with one request each this run
        self.over_directed: Counter = Counter()         # provider -> such checks left for the next run because dataset_checks_per_provider was reached
        self.dataset = None                             # dataset.Dataset (set by the run); None = the public list is not used
        self.careers = careers_mod.keys(settings)       # folder_key of every company with an entry in careers_urls.yaml: its name is not guessed
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data: dict[str, dict] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, ats: str, token: str) -> bool | None:
        """True when a board was found inside the cache window. False when it is believed there is none: it was asked in THIS run and found none, or `misses_needed`
        separate runs each found none (an old entry with no count is one miss). None when it is unknown, stale, or has been missed fewer times than that: ask again."""
        e = self.data.get(f"{ats}:{token}")
        try:
            if e and self.clock() - datetime.fromisoformat(e["ts"]) < timedelta(days=self.days):
                if e["found"]:
                    return True
                if e.get("run") == self.run_id or int(e.get("misses", 1)) >= self.misses_needed:
                    return False
        except (KeyError, ValueError, TypeError):
            pass
        return None

    def misses(self, ats: str, token: str) -> int:
        """How many separate runs have found no board for this pair (0 when it is unknown or was found)."""
        e = self.data.get(f"{ats}:{token}")
        return 0 if not e or e.get("found") else int(e.get("misses", 1))

    def put(self, ats: str, token: str, found: bool, slug: str = "") -> None:
        self.answered = True
        key = f"{ats}:{token}"
        prev = self.data.get(key) or {}
        entry: dict = {"ts": self.clock().isoformat(timespec="seconds"), "found": found, "run": self.run_id, **({"slug": slug} if slug else {})}
        if found and prev.get("found"):                   # v3.1: a board the agent search added keeps where it came from (search_boards reads it)
            entry.update({k: prev[k] for k in SEARCH_FIELDS if k in prev})
            if prev.get("slug"):
                entry.setdefault("slug", prev["slug"])
        if not found:
            before = 0 if (not prev or prev.get("found")) else int(prev.get("misses", 1))
            entry["misses"] = min(self.misses_needed, max(1, before) if prev.get("run") == self.run_id else before + 1)
        self.data[key] = entry

    def slug(self, ats: str, token: str) -> str:
        """The slug a provider answered for this token (Personio: the host that has the board), else the token."""
        return str((self.data.get(f"{ats}:{token}") or {}).get("slug") or token)

    def notes(self) -> list[str]:
        """One line per provider that was stopped or refused in this run (for the run's notes)."""
        out = [f"ats: {a} stopped for this run: {why}" for a, why in sorted(self.stopped.items())]
        out += [f"ats: {a} refused {n} request(s) (not asked again where it said no): {why}" for a, (n, why) in sorted(self.refused.items())]
        out += [f"ats: {a} guesses stopped at the limit of {self.max_guesses} a run (discover.ats.max_guesses_per_provider): {n} token(s) left for the next run"
                for a, n in sorted(self.over_cap.items()) if n]
        out += [f"ats: {a} checks of names in the public board list stopped at {self.directed_max} a run (discover.ats.dataset_checks_per_provider): {n} left for the next run"
                for a, n in sorted(self.over_directed.items()) if n]
        if self.not_guessed:
            out.append(f"ats: {', '.join(self.not_guessed)} in discover.ats.providers are no longer guessed (one host per company: an unknown name is a host that does not exist); "
                       "a board you know goes in careers_urls.yaml or under sources:, and one an alert links to is found from the link")
        if self.list_matched:
            out.append("ats: from the public board list: " + " · ".join(
                f"{a} {self.list_matched[a]} matched, {self.list_verified[a]} verified, {self.list_failed[a]} failed" for a in sorted(self.list_matched))
                + ("" if self.blind else "; nothing was guessed (discover.ats.blind_guesses is off)"))
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

    def may_check(self, ats: str) -> bool:
        """May one more name that the public board list holds be checked with this provider in this run (dataset_checks_per_provider)?"""
        if self.directed[ats] >= self.directed_max:
            self.over_directed[ats] += 1
            return False
        return True

    @contextmanager
    def counting(self, ats: str, fetcher, directed: bool = False):
        """Around the request(s) of one guess: it counts as a guess (or, when `directed`, as a check of a name the public list holds), and the requests the fetcher
        sent meanwhile (robots.txt included) are added to the provider's."""
        before = sent(fetcher)
        (self.directed if directed else self.guesses)[ats] += 1
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
        names = sorted({*self.guesses, *self.requests, *self.cache_hits, *self.over_cap, *self.directed, *self.list_matched})
        return {a: {"guesses": self.guesses[a], "requests": self.requests[a], "cached": self.cache_hits[a], "over_cap": self.over_cap[a],
                    **({"checked_from_list": self.directed[a]} if self.directed[a] else {}),
                    **({"list_matched": self.list_matched[a], "list_verified": self.list_verified[a], "list_failed": self.list_failed[a]}
                       if self.list_matched[a] else {})} for a in names}

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
    skipped: str = ""                                     # why nothing was guessed (it has a careers address)
    listed: set[str] = field(default_factory=set)         # providers whose public board list holds one of the tokens (asked instead of guessing)
    suggested: list[str] = field(default_factory=list)    # Workday addresses the public list holds for the name: NOT read, for you to confirm in careers_urls.yaml
    not_listed: bool = False                              # v3.1: the public list holds no board named like the company, so nothing was asked (no blind guesses)

    def line(self) -> str:
        tried = ", ".join(t + (" (cached)" if t in self.cached else "") for t in self.tokens) or "-"
        board = ", ".join(f"{b.ats}/{b.slug}" for b in self.boards) or "none"
        extra = f" | board_unverified: {', '.join(f'{b.ats}/{b.slug}' for b, _ in self.unverified)}" if self.unverified else ""
        extra += f" | {len(self.errors)} error(s): {self.errors[0]}" if self.errors else ""
        extra += f" | rejected: {', '.join(f'{t} ({w})' for t, w in self.rejected)}" if self.rejected else ""
        extra += f" | not asked (guess limit): {', '.join(sorted(self.capped))}" if self.capped else ""
        extra += f" | {self.skipped}" if self.skipped else ""
        extra += f" | in the public board list: {', '.join(sorted(self.listed))}" if self.listed else ""
        extra += f" | the list suggests (not read; check, then add to careers_urls.yaml): {', '.join(self.suggested)}" if self.suggested else ""
        extra += " | not in the public board list: nothing guessed" if self.not_listed else ""
        reg = ", register" if self.seed.on_register else ""
        return f"  {self.seed.name} [{self.seed.origin}{reg}] | tried: {tried} | board: {board} | {self.matched} matched{extra}"

    def tried_text(self) -> str:
        """What was done to find this company's board, in a sentence (the trace of an alert that could not be resolved)."""
        bits = []
        if self.skipped:
            return self.skipped
        if self.not_listed:
            return ("not in the public board list (Greenhouse, Lever, Ashby, Workday) under this name, so no provider was asked: nothing is guessed "
                    "(discover.ats.blind_guesses is off); a careers address you find goes in careers_urls.yaml")
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
    if folder_key(seed.name) in index.careers:
        f.skipped = "has an entry in careers_urls.yaml: its board is read from that address, not guessed"
        return f
    full = ats_mod.full_name_tokens(seed.name)
    ds = index.dataset if index.dataset is not None and index.dataset.enabled else None
    name_hits = ds.match(seed.name) if ds is not None and not index.blind else {}         # v3.1: {provider: (slug, ratio)}: the list's boards named like the company
    list_tokens = {s.lower() for s, _ in name_hits.values()}
    for ats in name_hits:
        index.list_matched[ats] += 1
    order = [*([seed.hint] if seed.hint else []), *sorted(list_tokens), *ats_mod.board_tokens(seed.name, seed.hint)]
    for token in dict.fromkeys(order):
        why = None if token == seed.hint or token in list_tokens else index.reject_reason(token, seed.name)
        if why:                                           # not a company name: not asked, and the report says so
            f.rejected.append((token, why))
            index.note_reject(seed.name, token, why)
            continue
        f.tokens.append(token)
        asked = skipped = False
        hits: list[tuple[Board, list[Posting] | None]] = []
        if index.blind:                                   # v3.0 behaviour (blind guessing on): the token itself is looked up in the list
            listed = index.dataset.hits(token) if index.dataset is not None else {}      # {provider: slug}: where the public board list says this name's board is
        else:                                             # v3.1: only the list's boards named like the company
            listed = {a: s for a, (s, _) in name_hits.items() if s.lower() == token}
        f.listed |= set(listed)
        for ats in ats_mod.PROVIDERS:
            known = index.get(ats, token)
            if listed and ats not in listed and known is not True:      # the list says where the board is: nothing else is guessed for this name (a board already found stays)
                continue
            if known is False:
                index.cache_hits[ats] += 1
                continue
            slug = listed.get(ats) or index.slug(ats, token)
            board = Board(ats, slug, seed.name, seed.origin)
            if known is True:
                index.cache_hits[ats] += 1
                hits.append((board, None))
                continue
            directed = ats in listed
            if not directed and not index.blind:          # v3.1: a name the list does not place on this provider is not asked (no blind guess)
                continue
            if not (index.may_check(ats) if directed else index.may_guess(ats)):    # the run's limit for this provider: left for the next run (nothing is cached for it)
                f.capped.add(ats)
                skipped = True
                continue
            asked = True
            url, params, what = ats_mod.board_request(ats, slug)
            soft = {"soft_401": "guess"} if ats == "ashby" else {}     # v3.1: Ashby answers 401 for a board name that does not exist: "no board", not a refusal
            try:
                with index.counting(ats, fetcher, directed):
                    data = fetcher.json(url, params=params, what=what, **soft)
            except FetchError as e:                       # Blocked is a FetchError: not an answer, so not remembered
                if isinstance(e, Blocked):
                    index.refusals[ats] = index.refusals.get(ats, 0) + 1
                f.errors.append(str(e))
                if directed and not index.blind:
                    index.list_failed[ats] += 1
                continue
            found = ats_mod.is_board(ats, data)
            index.put(ats, token, found, slug if slug != token else "")
            if directed and not index.blind:
                (index.list_verified if found else index.list_failed)[ats] += 1
            if found:
                hits.append((board, ats_mod.parse_postings(ats, data, seed.name)))
        for ats in (*index.more, *index.not_guessed):     # the v2.6 providers whose reply names the company (more_ats.NAME_CHECKED): every guess is name-checked
            known = index.get(ats, token)
            if ats in index.stopped and known is not True:        # a provider that said no is not asked; a board it was already known to have still counts (reading it is refused by read_board)
                continue
            if known is not True and (listed or ats in index.not_guessed or not index.blind):     # one host per company, a name the list places elsewhere, or no blind guessing
                continue                                                                          # (v3.1): never guessed, but a board found before is read
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
            if not ok and token in list_tokens:           # v3.1: the list's board is named like the company (exact or 0.90) and the request found it: verified
                ok = True
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
    sites = ds.workday_match(seed.name) if ds is not None and not index.blind and not f.boards else []
    if sites:                                             # v3.1: a Workday tenant the list names like the company: verified through its CXS endpoint, then read
        index.list_matched["workday"] += 1
        for site in sites:
            board = Board("workday", site.slug, seed.name, seed.origin)
            known = index.get("workday", site.slug)
            if known is True:
                index.cache_hits["workday"] += 1
                f.boards.append(board)
                break
            if known is False:
                index.cache_hits["workday"] += 1
                continue
            if not index.may_check("workday"):
                f.capped.add("workday")
                break
            ok, why = verify_workday(site, fetcher, index)
            if ok is None:
                f.errors.append(why)
                index.list_failed["workday"] += 1
                continue
            index.put("workday", site.slug, ok)
            (index.list_verified if ok else index.list_failed)["workday"] += 1
            if ok:
                f.boards.append(board)
                break
    f.not_listed = ds is not None and not index.blind and not name_hits and not sites and not f.boards
    if not f.boards and index.dataset is not None and index.blind:
        for t in f.tokens:                                # a Workday site the list holds for this name: shown, never read
            f.suggested += [u for u in index.dataset.workday_urls(t) if u not in f.suggested]
    index.save()
    return f


def verify_workday(site, fetcher: Fetcher, index: BoardIndex) -> tuple[bool | None, str]:
    """(is it a Workday site, why not) from ONE list request to its CXS endpoint (the first page). None when Workday could not say (an outage, a refusal):
    nothing is kept and the next run asks again."""
    from . import workday
    try:
        with index.counting("workday", fetcher, directed=True):
            workday._list_page(fetcher, site, "", 0, f"Workday {site.tenant}/{site.site}")
    except workday.Transient as e:
        return None, str(e)
    except Blocked as e:
        index.stop("workday", e)
        return None, str(e)
    except workday.WorkdayError as e:
        return False, str(e)
    except FetchError as e:
        return None, str(e)
    return True, ""
