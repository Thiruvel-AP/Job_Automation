"""The gate in front of tailoring: `cli.tailor_jobs` asks it about every job before a single token is spent. In this order:

  1. blacklist  (config/profile/blacklist.yaml, discovery/blacklist.py)  a hit asks "<company> is on your blacklist since <date>: <reason>. Apply anyway? [y/N]"
  2. liveness   (discovery/liveness.py)  a closed or expired posting is filed in jds_done/closed/ and never tailored; an uncertain one goes on with a flag
  3. channel    (discovery/channel.py)   the same role already on record under another channel (agency vs direct, two agencies, the archive): which one owns it?
                                         An agency ad that hides the employer: go on anyway?
  4. repost     (discovery/repost.py)    `possible repost (n listings since <date>)` and `evergreen?`, flags only
Every question defaults to no: silence (Enter, EOF) never lets a job through. `offline` (the --mock run) skips the network and the archive.
A stop leaves a status behind (`closed`, or `skipped` with the reason) and moves this job's own inbox file only (jds_done/closed or jds_done/skipped, with a
<name>.reason.txt beside it): never a copy of the job on another channel, never anything in the application archive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..apply import decided
from ..db import DB
from . import applykind, blacklist, channel, liveness, manual, repost
from .blocklog import FILE as BLOCKLOG_FILE, BlockLog
from .http import Fetcher
from .robots import RobotsCache

OWN_FLAGS = ("liveness uncertain", "possible repost", "evergreen?", "blacklisted (you chose", "same role via another channel", "agency ad that hides")
REPORT_MAX = 150                 # ads whose liveness `discover --report` checks in one go (1 request a second per host)


@dataclass
class Gate:
    stop: bool = False
    reason: str = ""
    decision: str = ""                                   # closed | skipped | "" (stopped without a verdict: an unreadable blacklist)
    flags: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)


def default_fetcher(settings) -> Fetcher:
    return Fetcher(RobotsCache(settings.path("robots")), never=applykind.NEVER_FETCH, blocklog=BlockLog(settings.path("discover") / BLOCKLOG_FILE),
                   allow_paths=("/wday/cxs/",))                # a Workday posting's public API is asked for liveness; its careers pages stay refused


def _get(job: Mapping[str, Any], key: str) -> Any:
    try:
        return job[key]
    except (KeyError, IndexError, TypeError):
        return None


def _flags_of(job: Mapping[str, Any]) -> list[str]:
    try:
        v = json.loads(_get(job, "flags") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _own_files(settings, job: Mapping[str, Any]) -> list[Path]:
    """This job's own JD files in the inbox: by job id, or by the address in the header. Never the file of another job."""
    urls = {repost.url_key(u) for u in (_get(job, "url"), _get(job, "apply_url")) if u}
    out = []
    for p in decided.txt_files(decided.inbox_dir(settings)):
        hdr = decided.header_of(p)
        if decided.file_job_id(p) == _get(job, "id") or (hdr.get("url") and repost.url_key(hdr["url"]) in urls):
            out.append(p)
    return out


def _drop(db: DB, settings, job: Mapping[str, Any], status: str, reason: str, folder: str) -> None:
    """Mark this job `status` with the reason and file its own inbox file(s) in jds_done/<folder>/ beside a .reason.txt. Nothing is deleted."""
    db.update_job(job["id"], status=status, reason=reason[:500])
    db.log(job["id"], "pretailor", {"status": status, "reason": reason})
    for f in _own_files(settings, job):
        try:
            dst = decided.move_file(settings, f, decided.decision_dir(settings, folder))
            dst.with_name(dst.stem + ".reason.txt").write_text(f"{status}: {reason}\n", encoding="utf-8")
        except (OSError, decided.DecidedError):
            pass                                          # the status is what matters; the file stays where it is


def _ask(ask: Callable[[str], str], prompt: str) -> str:
    try:
        return ask(prompt) or ""
    except EOFError:
        return ""


def _listings(db: DB) -> list[repost.Listing]:
    return [repost.Listing(r["company"], r["title"], r["url"] or r["apply_url"] or "", str(r["first_seen"] or ""), str(r["posted_at"] or ""), r["id"])
            for r in db.jobs()]


def run(db: DB, settings, job: Mapping[str, Any], *, fetcher: Fetcher | None = None, cache: liveness.Cache | None = None,
        ask: Callable[[str], str] = input, out: Callable[[str], None] = print, now: datetime | None = None, offline: bool = False,
        archive: list[tuple[str, str]] | None = None) -> Gate:
    """Decide whether this job may be tailored. Returns a Gate: `stop` with the reason, or the flags to show (they are also saved on the job)."""
    now = now or datetime.now(timezone.utc)
    g = Gate()
    company, title = str(_get(job, "company") or ""), str(_get(job, "title") or "")
    url, apply_url = str(_get(job, "url") or ""), str(_get(job, "apply_url") or "")

    def asked(prompt: str) -> str:
        g.asked.append(prompt)
        return _ask(ask, prompt)

    # 1. the blacklist
    try:
        hit = blacklist.load(blacklist.path_for(settings)).hit(company, url, apply_url)
    except blacklist.BlacklistError as e:
        g.stop, g.reason = True, f"{e}: fix or remove config/profile/blacklist.yaml; nothing was tailored"
        return g
    if hit is not None:
        if not channel.agrees(asked(blacklist.question(hit, company))):
            g.stop, g.decision, g.reason = True, "skipped", f"{company} is on your blacklist" + (f" since {hit.since}" if hit.since else "") + (f": {hit.reason}" if hit.reason else "")
            _drop(db, settings, job, "skipped", "blacklisted: " + g.reason, "skipped")
            return g
        g.flags.append("blacklisted (you chose to go on)")

    # 2. is it still open?
    if not offline and (url or apply_url):
        result = liveness.check(url or apply_url, fetcher or default_fetcher(settings), cache if cache is not None else liveness.cache_for(settings), now)
        if result.status == liveness.CLOSED:
            g.stop, g.decision, g.reason = True, "closed", f"the posting is closed ({result.reason})"
            _drop(db, settings, job, "closed", result.reason, "closed")
            return g
        if result.status == liveness.UNCERTAIN:
            g.flags.append(f"liveness uncertain: {result.reason}")

    # 3. the same role under another channel
    if channel.hidden_employer(job) and not channel.agrees(asked(channel.hidden_question(job))):
        g.stop, g.decision, g.reason = True, "skipped", "an agency ad that does not name the employer: you did not go on"
        _drop(db, settings, job, "skipped", g.reason, "skipped")
        return g
    if channel.hidden_employer(job):
        g.flags.append("agency ad that hides the employer (you chose to go on)")
    pairs = [] if offline else (archive if archive is not None else _archive_pairs(settings))
    matches = channel.find(job, [dict(r) for r in db.jobs() if r["id"] != _get(job, "id")], pairs)
    if matches:
        if not channel.proceeds(asked(channel.channel_question(job, matches))):
            g.stop, g.decision, g.reason = True, "skipped", f"the same role is already on record under another channel ({matches[0].label}); you did not go on"
            _drop(db, settings, job, "skipped", g.reason, "skipped")
            return g
        g.flags.append("same role via another channel (you chose this one)")

    # 4. repost and evergreen: information only
    key = _get(job, "id")
    listings = _listings(db)
    mine = next((l for l in listings if l.id == key), None)
    if mine is not None:
        g.flags += repost.analyse(listings, now.date()).get(mine.key, [])

    saved = [f for f in _flags_of(job) if not f.startswith(OWN_FLAGS)] + g.flags
    if saved != _flags_of(job):
        db.update_job(job["id"], flags=saved)
    for f in g.flags:
        out(f"  flag: {f}")
    return g


def _archive_pairs(settings) -> list[tuple[str, str]]:
    from . import discover                                # a big module: imported when the archive is actually listed

    return discover.archive_pairs(settings.archive_roots())


# ------------------------------------------------------------------ discover --report
def _inbox(settings) -> list[dict]:
    rows = []
    for p in sorted(decided.inbox_dir(settings).glob("*.txt")):
        hdr = decided.header_of(p)
        try:
            written = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).date().isoformat()
        except OSError:
            written = ""
        rows.append({"file": p.name, "company": hdr.get("company", ""), "title": hdr.get("title", ""), "url": hdr.get("url", ""),
                     "first_seen": hdr.get("first_seen") or written, "posted": hdr.get("posted", "")})
    return rows


def report_lines(settings, db: DB, fetcher: Fetcher | None, *, cache: liveness.Cache | None = None, now: datetime | None = None,
                 max_check: int = REPORT_MAX) -> list[str]:
    """The inbox in numbers: liveness (open / closed / uncertain) and the repost and evergreen signals. Information only: nothing is moved or changed.
    Without a `fetcher` the liveness check is skipped and says so."""
    now = now or datetime.now(timezone.utc)
    ads = _inbox(settings)
    lines: list[str] = []
    if fetcher is None:
        lines.append(f"Liveness: not checked ({len(ads)} ad(s) in the inbox; --no-liveness, or no network in this mode).")
    else:
        cache = cache if cache is not None else liveness.cache_for(settings)
        counts = {liveness.OPEN: 0, liveness.CLOSED: 0, liveness.UNCERTAIN: 0}
        closed, checked = [], 0
        for ad in ads:
            if not ad["url"] or checked >= max_check:
                continue
            checked += 1
            r = liveness.check(ad["url"], fetcher, cache, now)
            counts[r.status] += 1
            if r.status == liveness.CLOSED:
                closed.append(f"  closed: {ad['company']} | {ad['title']}: {r.reason}  [{ad['file']}]")
        left = sum(1 for ad in ads if ad["url"]) - checked
        lines.append(f"Liveness of the inbox ({len(ads)} ad(s); answers kept {liveness.CACHE_HOURS} h; LinkedIn, Indeed and CV-Library are never fetched): "
                     f"open {counts[liveness.OPEN]} · closed {counts[liveness.CLOSED]} · uncertain {counts[liveness.UNCERTAIN]}"
                     + (f" · {left} not checked (the cap is {max_check} a run)" if left else "")
                     + (f" · {sum(1 for ad in ads if not ad['url'])} without an address" if any(not ad["url"] for ad in ads) else ""))
        lines += closed[:15]
        if len(closed) > 15:
            lines.append(f"  ... and {len(closed) - 15} more closed")
        if closed:
            lines.append("  (closed ads are filed in jds_done/closed/ when `live` or `tailor` reaches them; this report moves nothing)")
    listings = [repost.Listing(a["company"], a["title"], a["url"], a["first_seen"], a["posted"], id=f"inbox:{a['file']}") for a in ads]
    seen = {repost.url_key(l.url) for l in listings if l.url}
    listings += [l for l in _listings(db) if not l.url or repost.url_key(l.url) not in seen]
    today = now.date()
    s = repost.summary(listings, today)
    lines.append("Repost / ghost signals (information only, never a score): " + s.line())
    flagged = repost.analyse(listings, today)
    for l in listings:
        if l.id.startswith("inbox:") and flagged.get(l.key):
            lines.append(f"  {l.company} | {l.title}: {'; '.join(flagged[l.key])}")
    return lines
