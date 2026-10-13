"""The health check of every source in config/sources.yaml (`careerops sources check`, and the end of every `careerops discover` run).

Per source, by its method:
  greenhouse / ashby / lever + token   robots.txt allows the API URL? the board answers? how many open postings?
  greenhouse / ashby / lever, no token the API host answers (any HTTP answer under 500); postings = what this run's `discover --ats` looked at
  reed                                 REED_API_KEY present? one search request (resultsToTake=1): does the key work, how many matches?
  gmail_alerts                         signed in to Gmail (read-only)? alert emails of this source in the last discover.health.alert_days?
  jobbank_csv                          robots.txt allows the catalogue? the newest English file found? (the 46 MB file is not downloaded)
  dedupe_only                          the export file is there? how many rows?
A source with sender domains also gets its alert count, whatever its method. Every check is isolated: a source that raises, is blocked
or is unreachable shows that in its row and the others are still checked. Nothing here writes to a job source or to your mailbox; the
only request that is not a plain GET of a public/official API is Gmail's read-only message list.

status: ok | quiet (works, but found nothing) | blocked (robots.txt, 401 / 403 / 429) | unreachable | no_credentials | not_found |
        no_file | error | disabled
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ats as ats_mod
from . import discover as disc
from . import gmail, jobbank, reed
from .http import Blocked, FetchError, Fetcher
from .registry import ATS_METHODS, Registry, Source

COLUMNS = ["ts", "source", "name", "method", "country", "status", "new_jobs", "reachable", "robots", "credentials", "alerts",
           "postings", "detail"]
TABLE = ("source", "method", "status", "new_jobs")
API_PROBE = {"greenhouse": ats_mod.GH, "lever": ats_mod.LEVER["lever"], "ashby": ats_mod.ASHBY}


@dataclass
class Row:
    source: Source
    status: str = "ok"
    reachable: str = "n/a"           # yes | no | n/a
    robots: str = "n/a"              # allowed | disallowed | unchecked | n/a (an official API with its own terms)
    credentials: str = "n/a"         # present | missing | n/a
    alerts: int | None = None        # alert emails in the last discover.health.alert_days
    postings: int | None = None      # open postings found
    new_jobs: int = 0                # ads written (or rows listed) for this source in this run
    detail: str = ""

    def record(self, ts: str) -> dict:
        s = self.source
        return {"ts": ts, "source": s.id, "name": s.name, "method": s.method, "country": s.country, "status": self.status,
                "new_jobs": self.new_jobs, "reachable": self.reachable, "robots": self.robots, "credentials": self.credentials,
                "alerts": "" if self.alerts is None else self.alerts, "postings": "" if self.postings is None else self.postings,
                "detail": self.detail}


@dataclass
class Context:
    settings: object
    fetcher: Fetcher
    cache: disc.BoardCache
    run: disc.Run | None = None
    days: int = 7
    _token: str | None = field(default=None, repr=False)
    _token_error: str = ""

    def gmail_token(self) -> str | None:
        """A read-only Gmail access token, found once per check; None (with the reason) when not signed in."""
        if self._token is None and not self._token_error:
            try:
                self._token = gmail.access_token(self.settings)
            except gmail.GmailAuthError as e:
                self._token_error = str(e)
            except (FetchError, Blocked, OSError) as e:
                self._token_error = f"{type(e).__name__}: {e}"
        return self._token


def _blocked(row: Row, e: Blocked) -> None:
    row.status, row.reachable = "blocked", "no"
    row.detail = str(e)
    if "robots.txt" in str(e):
        row.robots = "disallowed" if "disallows" in str(e) else "unchecked"


def _alerts(row: Row, ctx: Context) -> None:
    """Count the source's alert emails of the last N days (a read-only message list); sets status when it is the source's job."""
    src = row.source
    token = ctx.gmail_token()
    if token is None:
        if src.method == "gmail_alerts":
            row.status, row.credentials, row.detail = "no_credentials", "missing", ctx._token_error
        return
    q = f"{gmail.source_clause(src)} newer_than:{ctx.days}d"
    try:
        row.alerts = len(gmail.list_message_ids(ctx.fetcher, token, q, 50))
    except Blocked as e:
        if src.method == "gmail_alerts":
            _blocked(row, e)
        return
    except FetchError as e:
        if src.method == "gmail_alerts":
            row.status, row.reachable, row.detail = "unreachable", "no", str(e)
        return
    if src.method == "gmail_alerts":
        row.credentials, row.reachable = "present", "yes"
        if row.alerts == 0:
            row.status = "quiet"
            row.detail = (f"no alert email from {', '.join(src.domains)} in the last {ctx.days} days: set up an alert on the site, "
                          "or this sender domain is not the one it uses (config/sources.yaml)")


def _check_ats(row: Row, ctx: Context) -> None:
    src = row.source
    if src.token:
        board = ats_mod.Board(src.method, src.token, src.name, "registry")
        url = ats_mod.board_request(board.ats, board.slug)[0]
        row.robots = ctx.fetcher.robots_state(url)
        try:
            posts = ctx.cache.postings(board)
        except Blocked as e:
            return _blocked(row, e)
        except FetchError as e:
            row.status, row.reachable, row.detail = "unreachable", "no", str(e)
            return
        row.reachable = "yes"
        row.postings = sum(1 for p in posts if p.listed)
        if not row.postings:
            row.status = "not_found"
            row.detail = f"{src.method} board '{src.token}' answered but lists no open postings (no such board, or none open)"
        return
    url = API_PROBE[src.method]
    row.robots = ctx.fetcher.robots_state(url)
    try:
        ctx.fetcher.request(url, what=f"{src.method} API")           # any answer under 500 (a 404 for the bare path) = the host is up
    except Blocked as e:
        return _blocked(row, e)
    except FetchError as e:
        row.status, row.reachable, row.detail = "unreachable", "no", str(e)
        return
    row.reachable = "yes"
    if ctx.run is not None and "ats" in ctx.run.sources:
        row.postings = int(ctx.run.sources["ats"].get("seen", 0))
        row.detail = "postings = those `discover --ats` looked at in this run, across all boards found"
    else:
        row.detail = "the boards are found per company by `discover --ats`; this checks the API host only"


def _check_reed(row: Row, ctx: Context) -> None:
    if not os.environ.get("REED_API_KEY", "").strip():
        row.status, row.credentials = "no_credentials", "missing"
        row.detail = "REED_API_KEY is not set in .env"
        return
    row.credentials = "present"
    try:
        data = ctx.fetcher.json(f"{reed.BASE}/search", params={"keywords": "data scientist", "resultsToTake": 1},
                                auth=(reed.api_key(), ""), robots=False, what="Reed search")
    except Blocked as e:
        return _blocked(row, e)
    except FetchError as e:
        row.status, row.reachable, row.detail = "unreachable", "no", str(e)
        return
    row.reachable = "yes"
    total = (data or {}).get("totalResults") if isinstance(data, dict) else None
    row.postings = int(total) if isinstance(total, int) else None
    if not row.postings:
        row.status, row.detail = "quiet", "the key works but a test search for 'data scientist' found nothing"


def _check_jobbank(row: Row, ctx: Context) -> None:
    row.robots = ctx.fetcher.robots_state(jobbank.CKAN)
    try:
        res = jobbank.latest_csv(ctx.fetcher, row.source.dataset)
    except Blocked as e:
        return _blocked(row, e)
    except (FetchError, jobbank.JobBankError) as e:
        row.status, row.reachable, row.detail = "unreachable", "no", str(e)
        return
    row.reachable = "yes"
    row.detail = f"newest file: {res['name']} (not downloaded by the check)"
    if ctx.run is not None and "jobbank" in ctx.run.sources:
        row.postings = int(ctx.run.sources["jobbank"].get("seen", 0))


def _check_dedupe(row: Row, ctx: Context) -> None:
    path: Path = ctx.settings.path("aiapply_applied")
    if not path.exists():
        row.status, row.detail = "no_file", f"{path} not found: nothing to skip (export your applications there as a CSV with company and title columns)"
        return
    pairs, note = disc.read_aiapply(path)
    row.postings = len(pairs)
    if note:
        row.status, row.detail = "error", note
    else:
        row.detail = f"{len(pairs)} applied job(s) are skipped as duplicates"


def check_source(src: Source, ctx: Context) -> Row:
    row = Row(src, new_jobs=ctx.run.new_jobs(src.id) if ctx.run is not None else 0)
    if not src.enabled:
        row.status, row.detail = "disabled", "enabled: false in config/sources.yaml"
        return row
    try:
        if src.method in ATS_METHODS:
            _check_ats(row, ctx)
        elif src.method == "reed":
            _check_reed(row, ctx)
        elif src.method == "jobbank_csv":
            _check_jobbank(row, ctx)
        elif src.method == "dedupe_only":
            _check_dedupe(row, ctx)
        if src.domains and (src.method == "gmail_alerts" or row.status in ("ok", "quiet")):
            _alerts(row, ctx)
    except Blocked as e:
        _blocked(row, e)
    except Exception as e:  # noqa: BLE001 — one source's failure is its row, never the end of the check
        row.status, row.detail = "error", f"{type(e).__name__}: {e}"
    return row


def check_all(settings, registry: Registry, fetcher: Fetcher, *, run: disc.Run | None = None, cache: disc.BoardCache | None = None,
              say: Callable[[str], None] = lambda s: None) -> list[Row]:
    """A Row per source, in the order of sources.yaml. `run` (a discover run) supplies the new-jobs column; `cache` is that run's
    board cache, so a board it just read is not fetched twice."""
    ctx = Context(settings, fetcher, cache or disc.BoardCache(settings, fetcher), run,
                  days=int(((settings["discover"].get("health") or {}).get("alert_days", 7))))
    rows = []
    for i, src in enumerate(registry, 1):
        if i % 8 == 0:
            say(f"health: {i}/{len(registry)} sources checked ...")
        rows.append(check_source(src, ctx))
    return rows


# ------------------------------------------------------------------ output
def format_table(rows: list[Row]) -> str:
    cells = [[r.source.name, r.source.method, r.status, str(r.new_jobs)] for r in rows]
    heads = ["source", "method", "status", "new jobs"]
    widths = [max(len(h), *(len(c[i]) for c in cells)) if cells else len(h) for i, h in enumerate(heads)]
    line = lambda parts: "  ".join(p.ljust(w) if i < 3 else p.rjust(w) for i, (p, w) in enumerate(zip(parts, widths))).rstrip()  # noqa: E731
    out = [line(heads), line(["-" * w for w in widths])] + [line(c) for c in cells]
    bad = [r for r in rows if r.status not in ("ok", "disabled") and r.detail]
    if bad:
        out.append("")
        out += [f"  {r.source.name}: {r.status}: {r.detail}" for r in bad]
    return "\n".join(out)


def csv_path(settings, today: datetime) -> Path:
    return settings.path("discover") / "health" / f"{today:%Y-%m-%d}.csv"


def save(settings, rows: list[Row], now: datetime | None = None) -> Path:
    """Append this check to paths.discover/health/<date>.csv (a header once per file; a second check the same day adds its rows)."""
    now = now or datetime.now(timezone.utc)
    path = csv_path(settings, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    ts = now.isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerows(r.record(ts) for r in rows)
    return path
