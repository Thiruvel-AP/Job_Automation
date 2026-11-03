"""A job named by its id, its link or the name of its JD file (v2.5 fix round 2), and the decision log's backfill.

`careerops why` and `careerops skip` used to take a database id only, but a job you skipped or approved at the shortlist is often only a JD file (it
never entered the database). `resolve` turns any of these into one `Target`: the database rows of the listing (if any), the JD files of it in the
inbox, approved/<date>/ and jds_done/, and its canonical keys (apply/listing_keys.py).

  skip_files   a skip with no database row: the JD files of the listing move to jds_done/skipped/ (with a `.reason.txt`) AND the decision log gets a
               line with all its keys. A bare link with no file is recorded by its keys alone.
  restore_files   the same for `unskip`: the files go back to the inbox and the log gets an `unskipped` line.
  record_file  one decision-log line for a JD file (the shortlist's [s] and [x] write it).
  backfill     the log's missing lines: every skipped / applied job of the database (dated by its real event, not by the last update) and every JD
               file in jds_done/skipped and jds_done/applied that the log does not know, dated by the shortlist log, else by the `.reason.txt`'s time.

Nothing here deletes or rewrites a log line, and nothing is ever deleted.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..db import DB
from . import decided
from . import decisions as decisions_mod
from . import listing_keys as LK
from .decided import APPLIED, SKIPPED, DecidedError


@dataclass
class Target:
    what: str
    rows: list = field(default_factory=list)                    # database rows of the listing (same id, ATS key or URL key)
    files: list[Path] = field(default_factory=list)             # JD files of the listing: inbox, approved/<date>/, jds_done/skipped|applied
    job_id: str = ""                                            # the database id, else the id `live` would give the file's job, else ""
    company: str = ""
    title: str = ""
    url: str = ""
    apply_url: str = ""
    location: str = ""
    keys: LK.Keys = field(default_factory=LK.Keys)
    logged: list[dict] = field(default_factory=list)            # decision-log records that name the id (an old id the database and the folders no longer hold)
    events: list = field(default_factory=list)                  # the database's events for the id
    more_keys: list[str] = field(default_factory=list)          # the other keys those records carry (other addresses, the fingerprint)

    @property
    def job(self):
        return self.rows[0] if self.rows else None

    def live_files(self, settings) -> list[Path]:
        """The files still in play (inbox, approved/): the ones a skip moves."""
        done = {decided.decision_dir(settings, SKIPPED).resolve(), decided.decision_dir(settings, APPLIED).resolve()}
        return [p for p in self.files if p.parent.resolve() not in done]

    def done_files(self, settings, decision: str = SKIPPED) -> list[Path]:
        return [p for p in self.files if p.parent.resolve() == decided.decision_dir(settings, decision).resolve()]


def all_folders(settings) -> list[Path]:
    return [decided.inbox_dir(settings), *decided.approved_dirs(settings), decided.decision_dir(settings, SKIPPED), decided.decision_dir(settings, APPLIED)]


def find_file(settings, what: str) -> Path | None:
    """A JD file by path, or by its name (with or without `.txt`) in the inbox, approved/<date>/ or jds_done/."""
    p = Path(what)
    if p.is_file():
        return p
    name = p.name if p.suffix.lower() == ".txt" else p.name + ".txt"
    for folder in all_folders(settings):
        if (folder / name).is_file():
            return folder / name
    return None


def resolve(settings, db: DB, what: str) -> Target:
    """The job `what` names: a database id, a link (or `ats:<vendor>:<board>:<id>`), or a JD file name / path. Raises DecidedError for anything else."""
    q = (what or "").strip()
    if not q:
        raise DecidedError("name a job: its id, its link, or the name of its JD file")
    t = Target(q)
    row = db.job(q)
    named: Path | None = None
    if row is not None:
        t.rows, t.job_id = [row], row["id"]
        t.company, t.title, t.url, t.apply_url, t.location = (str(row[k] or "") for k in ("company", "title", "url", "apply_url", "location"))
        t.keys = LK.keys_of_row(row)
    elif q.startswith(("http://", "https://", "ats:")):
        t.keys = LK.parse_query(q)
        if not t.keys.all():
            raise DecidedError(f"{q!r} is not a link this tool can read")
        t.url = q if q.startswith("http") else ""
    else:
        named = find_file(settings, q)
        if named is None:
            old = _old_id(settings, db, t, q)
            if old:
                return t
            raise DecidedError(f"no job, link or JD file matches {q!r} (a job id such as manual:ab12..., a link, or a file name in the inbox, approved/ or jds_done/)")
        hdr = decided.header_of(named)
        t.company, t.title, t.url, t.apply_url, t.location = (hdr.get(k, "") for k in ("company", "title", "url", "apply_url", "location"))
        t.keys = decided.file_keys(named)
        t.job_id = decided.file_job_id(named) or ""
        row = db.job(t.job_id) if t.job_id else None
        if row is not None:
            t.rows = [row]
    for p in ([named] if named is not None else []):
        t.files.append(p)
    for folder in all_folders(settings):
        for p in decided.txt_files(folder):
            if p in t.files:
                continue
            if (t.job_id and decided.file_job_id(p) == t.job_id) or decided.same_listing(t.keys, decided.file_keys(p)):
                t.files.append(p)
    if row is None and t.keys.strong():
        t.rows = [r for r in db.jobs() if decided.same_listing(t.keys, LK.keys_of_row(r))]
    if not t.job_id and t.rows:
        t.job_id = t.rows[0]["id"]
    if not t.company and t.files:                                # a link whose file we have: the file says who it is
        hdr = decided.header_of(t.files[0])
        t.company, t.title, t.url, t.apply_url, t.location = (hdr.get(k, "") for k in ("company", "title", "url", "apply_url", "location"))
        fk = decided.file_keys(t.files[0])
        t.keys = LK.Keys(t.keys.ats or fk.ats, t.keys.url or fk.url, fk.fingerprint)
        t.job_id = t.job_id or decided.file_job_id(t.files[0]) or ""
    return t


def _old_id(settings, db: DB, t: Target, q: str) -> bool:
    """An id the database has no row for and no folder holds a file of, but the decision log or the events table still remembers: fill `t` from them."""
    recs = decisions_mod.find_id(settings, q)
    evs = db.conn.execute("SELECT ts, stage, detail FROM events WHERE job_id = ? ORDER BY ts", (q,)).fetchall()
    if not recs and not evs:
        return False
    t.job_id, t.logged, t.events = q, recs, evs
    ks = [k for r in recs for k in r["keys"]]
    ats = next((k for k in ks if k.startswith("ats:")), "")
    url = next((k for k in ks if k.startswith("https://")), "")
    fp = next((k for k in ks if "|" in k and not k.startswith(("id:", "ats:", "https://"))), "")
    t.keys = LK.Keys(ats, url, fp)
    t.url = url
    t.more_keys = [k for k in dict.fromkeys(ks) if not k.startswith("id:")]
    if fp:
        parts = fp.split("|")
        t.company, t.title = parts[0], parts[1] if len(parts) > 1 else ""
    for p in all_files_of(settings, t):
        if p not in t.files:
            t.files.append(p)
    return True


def all_files_of(settings, t: Target) -> list[Path]:
    out = []
    for folder in all_folders(settings):
        for p in decided.txt_files(folder):
            if (t.job_id and decided.file_job_id(p) == t.job_id) or decided.same_listing(t.keys, decided.file_keys(p)):
                out.append(p)
    return out


def sources_of(settings, db: DB, t: Target) -> list[str]:
    """Where the listing came from, as far as anything records it: the database row's source, the `Source:` header of its JD files (the board or
    feed that gave the ad), the Gmail-alert queue (manual_queue.csv: LinkedIn / Indeed / ... alert links), the shortlist log."""
    out: list[str] = []
    for r in t.rows:
        if r["source"]:
            out.append(f"database row {r['id']}: source '{r['source']}'")
    for p in t.files:
        h = decided.header_of(p)
        if h.get("source"):
            out.append(f"{p.parent.parent.name}/{p.parent.name}/{p.name}: Source '{h['source']}'" + (f", apply kind '{h['apply_kind']}'" if h.get("apply_kind") else "")
                       + (f", first seen {h['first_seen']}" if h.get("first_seen") else ""))
    for name in ("manual_queue.csv", "manual_queue_overflow.csv"):
        q = settings.path("jds") / name
        try:
            lines = q.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for row in csv.DictReader(lines):
            link, co, ti = row.get("link", ""), (row.get("company") or "").lower(), (row.get("title") or "").lower()
            same = (link and (LK.url_key(link) in (t.keys.url, *t.more_keys))) or (co and ti and co == t.company.lower() and ti == t.title.lower())
            if same:
                out.append(f"{name}: alert from {row.get('source') or '?'} on {str(row.get('ts') or '')[:10]} ({link})")
    sk = shortlist_events(settings, t)
    out += sk
    return list(dict.fromkeys(out)) or ["(no source recorded)"]


def shortlist_events(settings, t: Target) -> list[str]:
    from ..discovery import shortlist

    if not (t.company and t.title):
        return []
    key = shortlist._key(t.company, t.title)
    try:
        text = shortlist.log_path(settings).read_text(encoding="utf-8")
    except OSError:
        return []
    got = []
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("key") == key and e.get("event") in ("skipped", "approved", "parked", "reset"):
            got.append(f"shortlist log (any posting with this company + title): {e['event']} on {str(e.get('ts') or '')[:10]}")
    return got[-3:]


# ------------------------------------------------------------------ one line per file
def record_file(settings, path: Path, decision: str, reason: str, ts: str | None = None, *, user: bool = True) -> dict:
    """The decision-log line of a JD file (its id as `live` gives it, its ATS key, URL key and fingerprint)."""
    kind = decisions_mod.APPLIED if decision == APPLIED else (decisions_mod.SKIPPED_USER if user else decisions_mod.SKIPPED)
    return decisions_mod.append(settings, decided.file_keys(path), kind, reason, decided.file_job_id(path), ts=ts)


def skip_files(settings, t: Target, reason: str = "skipped (user)") -> list[str]:
    """Skip a job that has no database row (or whose row the caller handled): its live JD files move to jds_done/skipped/ with a `.reason.txt`, and the
    decision log gets a line with every key of the listing. Returns the lines to print. Idempotent: a job already skipped is not written twice."""
    book = decisions_mod.Book.load(settings)
    prior = book.decided(t.keys, t.job_id or None)
    if prior is not None and prior.decision == decisions_mod.APPLIED:
        raise DecidedError("this job is one you applied to: an applied decision is final")
    if any(r["status"] == APPLIED or str(r["status"]).startswith("applied") for r in t.rows):
        raise DecidedError(f"{t.job_id} is applied: an applied decision is final")
    keys, out = record_keys(t), []                              # before the files move: their own addresses are keys too
    for p in t.live_files(settings):
        dst = decided.move_file(settings, p, decided.decision_dir(settings, SKIPPED))
        dst.with_name(dst.stem + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
        out.append(f"moved {p.parent.name}/{p.name} -> jds_done/skipped/{dst.name}")
    if prior is None:
        rec = decisions_mod.append(settings, keys, decisions_mod.SKIPPED_USER, reason, t.job_id or None)
        out.append(f"decision log: {rec['decision']} with {len(rec['keys'])} key(s): {', '.join(rec['keys'])}")
    else:
        out.append(f"already skipped on {prior.date or '(date not recorded)'}: no second line written")
    return out


def _keys_with_fingerprint(t: Target) -> LK.Keys:
    fp = t.keys.fingerprint or LK.fingerprint(t.company, t.title, t.location)
    return LK.Keys(t.keys.ats, t.keys.url, fp)


def record_keys(t: Target) -> list[str]:
    """Every key of the listing for a log line: its id, ATS key, the link as given AND the address of each of its JD files, the fingerprint."""
    k = _keys_with_fingerprint(t)
    urls = [k.url, *(decided.file_keys(p).url for p in t.files)]
    ordered = [decisions_mod.id_key(t.job_id), k.ats, *urls, *t.more_keys, k.fingerprint]
    return list(dict.fromkeys(x for x in ordered if x))


def restore_files(settings, t: Target) -> list[str]:
    """Undo a skip of a job with no database row: its JD files go from jds_done/skipped/ back to the inbox and the log gets an `unskipped` line."""
    if any(r["status"] == APPLIED for r in t.rows):
        raise DecidedError(f"{t.job_id} is applied: an applied decision is final")
    book = decisions_mod.Book.load(settings)
    files = t.done_files(settings, SKIPPED)
    if book.decided(t.keys, t.job_id or None) is None and not files:
        raise DecidedError(f"{t.what!r} is not skipped")
    keys, out = record_keys(t), []
    for p in files:
        dst = decided.move_file(settings, p, decided.inbox_dir(settings))
        out.append(f"moved jds_done/skipped/{p.name} -> inbox/{dst.name}")
    decisions_mod.append(settings, keys, decisions_mod.UNSKIPPED, f"careerops unskip {t.what}", t.job_id or None)
    out.append("decision log: unskipped")
    return out


# ------------------------------------------------------------------ backfill
def _local(ts: str) -> str:
    """An ISO time as local `YYYY-MM-DDTHH:MM:SS` (the log's own format); a naive time is taken as local already."""
    try:
        d = datetime.fromisoformat(str(ts))
    except ValueError:
        return ""
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d.isoformat(timespec="seconds")


def shortlist_skips(settings) -> dict[str, str]:
    """`company family|title` -> local time of the LAST `skipped` event in shortlist_log.jsonl after the last `reset` for it."""
    from ..discovery import shortlist

    out: dict[str, str] = {}
    try:
        text = shortlist.log_path(settings).read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or not e.get("key"):
            continue
        if e.get("event") == "skipped" and _local(e.get("ts", "")):
            out[e["key"]] = _local(e["ts"])
        elif e.get("event") == "reset":
            out.pop(e["key"], None)
    return out


def filed_at(path: Path) -> str:
    """Local time a JD file was filed in jds_done/: its `.reason.txt`'s, else the JD's own. "" if neither can be read."""
    for p in (path.with_name(path.stem + ".reason.txt"), path):
        try:
            return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            continue
    return ""


def _file_decision(folder_decision: str, reason: str) -> str:
    low = reason.lower()
    if folder_decision == APPLIED:
        return decisions_mod.APPLIED
    if "ats s" in low:
        return decisions_mod.SKIPPED_ATS
    if "(user" in low or "you chose" in low or "skipped (user" in low:
        return decisions_mod.SKIPPED_USER
    return decisions_mod.SKIPPED


def plan_files(settings, have: set[str]) -> list[dict]:
    """The log lines the JD files in jds_done/skipped and jds_done/applied lack (their id, ATS key and URL key are all unknown to the log)."""
    from ..discovery import shortlist

    skips, out = shortlist_skips(settings), []
    for folder_decision in (SKIPPED, APPLIED):
        for p in decided.txt_files(decided.decision_dir(settings, folder_decision)):
            jid, lk = decided.file_job_id(p), decided.file_keys(p)
            mine = [k for k in (decisions_mod.id_key(jid), *lk.strong()) if k]
            if not mine or any(k in have for k in mine):
                continue
            try:
                reason = p.with_name(p.stem + ".reason.txt").read_text(encoding="utf-8").strip().splitlines()[0]
            except (OSError, IndexError):
                reason = f"file in jds_done/{folder_decision}"
            hdr = decided.header_of(p)
            ts = (skips.get(shortlist._key(hdr.get("company", ""), hdr.get("title", ""))) if folder_decision == SKIPPED else None) or filed_at(p)
            out.append({"ts": ts, "job_id": jid or "", "keys": decisions_mod.keys_list(lk, jid), "decision": _file_decision(folder_decision, reason),
                        "reason": f"backfill: {reason[:160]}", "source": f"file jds_done/{folder_decision}/{p.name}"})
            have.update(mine)
    return out


def plan(db: DB, settings) -> list[dict]:
    """Every line the log lacks: the database's skipped / applied jobs (dated by their real event), then the JD files the database never held."""
    have = {k for r in decisions_mod.records(settings) for k in r["keys"]}
    rows = decisions_mod.plan_db(db, have)
    return rows + plan_files(settings, have)


def backfill(db: DB, settings, *, apply: bool = True) -> list[dict]:
    """Write (or, with apply False, only list) the missing lines. Returns them. Idempotent: a second run finds nothing to add."""
    lines = plan(db, settings)
    if apply:
        for r in lines:
            decisions_mod.append(settings, r["keys"], r["decision"], r["reason"], r["job_id"] or None, ts=r["ts"] or None)
    return lines
