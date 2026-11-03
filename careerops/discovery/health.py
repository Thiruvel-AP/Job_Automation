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

A gmail_alerts source shows three numbers: `alerts` (its mails in the last discover.health.alert_days), `jobs parsed` (jobs read out of its
mails in that time, from the saved discover runs plus the run being checked) and `new` (ads written). It is `ok` only when jobs were parsed;
mails that held no job (an account mail, a welcome) give `parsed_none`, with the first subject line in the detail column.

status: ok | quiet (works, but found nothing) | parsed_none (mails, but no job read from any) | blocked (robots.txt, 401 / 403 / 429) |
        unreachable | no_credentials | not_found | no_file | error | disabled
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import ats as ats_mod
from . import discover as disc
from . import gmail, jobbank, reed
from .http import Blocked, FetchError, Fetcher
from . import more_ats
from .registry import ATS_METHODS, MORE_METHODS, VC_METHODS, Registry, Source
from .robots import host_of

COLUMNS = ["ts", "source", "name", "method", "country", "status", "new_jobs", "reachable", "robots", "credentials", "alerts",
           "postings", "detail", "jobs_parsed"]
API_PROBE = {"greenhouse": ats_mod.GH, "lever": ats_mod.LEVER["lever"], "ashby": ats_mod.ASHBY,
             "workable": "https://apply.workable.com/api/v1/widget/accounts", "smartrecruiters": "https://api.smartrecruiters.com/v1/companies"}
VC_API = {"getro": "https://api.getro.com/api/v2/collections/", "consider": "/api-boards/search-jobs"}


@dataclass
class Row:
    source: Source
    status: str = "ok"
    reachable: str = "n/a"           # yes | no | n/a
    robots: str = "n/a"              # allowed | disallowed | unchecked | n/a (an official API with its own terms)
    credentials: str = "n/a"         # present | missing | n/a
    alerts: int | None = None        # alert emails in the last discover.health.alert_days
    postings: int | None = None      # open postings found
    parsed: int | None = None        # (gmail_alerts) jobs read out of its mails in the last alert_days; None = not known
    new_jobs: int = 0                # ads written (or rows listed) for this source in this run
    detail: str = ""

    def record(self, ts: str) -> dict:
        s = self.source
        return {"ts": ts, "source": s.id, "name": s.name, "method": s.method, "country": s.country, "status": self.status,
                "new_jobs": self.new_jobs, "reachable": self.reachable, "robots": self.robots, "credentials": self.credentials,
                "alerts": "" if self.alerts is None else self.alerts, "postings": "" if self.postings is None else self.postings,
                "detail": self.detail, "jobs_parsed": "" if self.parsed is None else self.parsed}


@dataclass
class Context:
    settings: object
    fetcher: Fetcher
    cache: disc.BoardCache
    run: disc.Run | None = None
    days: int = 7
    now: datetime | None = None
    _token: str | None = field(default=None, repr=False)
    _token_error: str = ""
    _runs: list | None = field(default=None, repr=False)

    def parsed(self, source_id: str) -> tuple[int, str]:
        """(jobs parsed from this source's mails in the last `days`, the first subject of a mail it read no job from): the saved discover
        runs of that time plus the run being checked (not saved yet when the check is the end of a discover run)."""
        if self._runs is None:
            cutoff = (self.now or datetime.now(timezone.utc)) - timedelta(days=self.days)
            self._runs = [r for r in disc.read_runs(self.settings) if _after(r.get("ts"), cutoff)]
        runs = [*self._runs, *([self.run.as_json()] if self.run is not None else [])]
        parsed = sum(int(((r.get("by_source") or {}).get(source_id) or {}).get("parsed", 0)) for r in runs)
        subject = next((r["no_job_subjects"][source_id] for r in reversed(runs) if (r.get("no_job_subjects") or {}).get(source_id)), "")
        return parsed, subject

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


def _after(ts: str | None, cutoff: datetime) -> bool:
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) >= cutoff


def _parsed(row: Row, ctx: Context) -> None:
    """A gmail_alerts source is `ok` only if jobs were read out of its mails: mails with none are `parsed_none`, with the first subject."""
    if row.alerts is None:                                          # no sign-in / unreachable: nothing to say about parsing
        return
    row.parsed, subject = ctx.parsed(row.source.id)
    if row.status == "ok" and row.parsed == 0:
        row.status = "parsed_none"
        row.detail = (f'{row.alerts} mail(s) in the last {ctx.days} days but no job was read from any; first subject: "{subject}"' if subject else
                      f"{row.alerts} mail(s) in the last {ctx.days} days but no job was read from any (no discover run has read them: "
                      "run `careerops discover --gmail-alerts`)")


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


def _check_more(row: Row, ctx: Context) -> None:
    """The v2.6 single-company providers. Workable and SmartRecruiters have one API host: robots.txt and the host answering are checked. Teamtailor, Recruitee,
    Personio and Pinpoint have one host per company, so there is nothing to probe in general: each board's robots.txt is read before its first request.
    Workday boards are the sites under `sources.workday`."""
    src = row.source
    if src.method == "workday":
        from . import workday
        n, bad = len(workday.entries(ctx.settings)), workday.config_problems(ctx.settings)
        row.detail = f"{n} Workday site(s) in settings sources.workday" + (f"; {bad[0]}" if bad else "")
        if ctx.run is not None and "ats" in ctx.run.sources:
            row.postings = int(ctx.run.sources["ats"].get("seen", 0))
        return
    if src.method in API_PROBE:
        return _check_ats(row, ctx)
    row.detail = ("one host per company: each board's robots.txt is read before its first request; "
                  + ("found from a link or sources:, never guessed" if src.method in more_ats.LINK_ONLY else "found per company by `discover --ats`"))
    if ctx.run is not None and "ats" in ctx.run.sources:
        row.postings = int(ctx.run.sources["ats"].get("seen", 0))


def _check_vc(row: Row, ctx: Context) -> None:
    """A VC portfolio board: robots.txt of its search API, then (when allowed) one read of the board."""
    src = row.source
    api = VC_API[src.method] if src.method == "getro" else f"https://{host_of(src.url)}{VC_API['consider']}"
    row.robots = ctx.fetcher.robots_state(api)
    if row.robots == "disallowed":
        row.status, row.reachable = "blocked", "no"
        row.detail = f"robots.txt of {host_of(api)} disallows its search API; the board is not read"
        return
    try:
        posts = more_ats.vc_read(src.method, src.url, src.token, ctx.fetcher)
    except Blocked as e:
        return _blocked(row, e)
    except FetchError as e:
        row.status, row.reachable, row.detail = "unreachable", "no", str(e)
        return
    row.reachable, row.postings = "yes", len(posts)
    if not posts:
        row.status, row.detail = "quiet", "the board answered but lists no job"


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
        elif src.method in MORE_METHODS:
            _check_more(row, ctx)
        elif src.method in VC_METHODS:
            _check_vc(row, ctx)
        elif src.method == "reed":
            _check_reed(row, ctx)
        elif src.method == "jobbank_csv":
            _check_jobbank(row, ctx)
        elif src.method == "dedupe_only":
            _check_dedupe(row, ctx)
        if src.domains and (src.method == "gmail_alerts" or row.status in ("ok", "quiet")):
            _alerts(row, ctx)
            if src.method == "gmail_alerts":
                _parsed(row, ctx)
    except Blocked as e:
        _blocked(row, e)
    except Exception as e:  # noqa: BLE001 — one source's failure is its row, never the end of the check
        row.status, row.detail = "error", f"{type(e).__name__}: {e}"
    log = getattr(ctx.fetcher, "blocklog", None)
    if log is not None and src.method in API_PROBE:               # a host that refused a script lately: when, and the slower pace that follows
        note = log.describe(host_of(API_PROBE[src.method]))
        if note:
            row.detail = f"{row.detail}; {note}" if row.detail else note
    return row


def check_all(settings, registry: Registry, fetcher: Fetcher, *, run: disc.Run | None = None, cache: disc.BoardCache | None = None,
              say: Callable[[str], None] = lambda s: None, now: datetime | None = None) -> list[Row]:
    """A Row per source, in the order of sources.yaml. `run` (a discover run) supplies the new-jobs column and the jobs it parsed; `cache`
    is that run's board cache, so a board it just read is not fetched twice."""
    ctx = Context(settings, fetcher, cache or disc.BoardCache(settings, fetcher), run,
                  days=int(((settings["discover"].get("health") or {}).get("alert_days", 7))), now=now)
    rows = []
    for i, src in enumerate(registry, 1):
        if i % 8 == 0:
            say(f"health: {i}/{len(registry)} sources checked ...")
        rows.append(check_source(src, ctx))
    return rows


# ------------------------------------------------------------------ output
def format_table(rows: list[Row]) -> str:
    dash = lambda n: "-" if n is None else str(n)  # noqa: E731 — a number that does not apply to the source's method
    cells = [[r.source.name, r.source.method, r.status, dash(r.alerts), dash(r.parsed), str(r.new_jobs)] for r in rows]
    heads = ["source", "method", "status", "alerts", "jobs parsed", "new jobs"]
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
    ts = now.isoformat(timespec="seconds")
    disc.append_rows(path, COLUMNS, [r.record(ts) for r in rows])        # a file of the same day from before a column existed gets it added
    return path
