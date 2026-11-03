"""The decision log (v2.5): every skip and application, with the job's canonical keys (apply/listing_keys.py), in `<paths.state>/decisions.jsonl`
(data/state/decisions.jsonl; git-ignored with the rest of data/; the parallel test copy never writes back into data/).

One JSON object per line, appended and never rewritten: {"ts", "job_id", "keys": [...], "decision", "reason"}.

  decision   skipped_user (your [s], `careerops skip`), skipped_ats (under the ATS band), skipped (any other skip the database holds), applied, and
             unskipped (`careerops unskip`: a later line that lifts the earlier ones for the same keys).
  keys       `id:<job id>`, the ATS key, the URL key and the fingerprint. The id, the ATS key and the URL key decide on their own (discover, the inbox
             intake, shortlist and live check them before they write or show a job); the fingerprint alone is only `possible repost of a job you
             skipped on <date>`.

`backfill(db, settings)` writes a line for every skipped / applied job of the database that has none yet (the migration: `careerops retriage
--decided --apply` runs it first). Nothing here deletes or rewrites a line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import listing_keys as LK

FILE = "decisions.jsonl"
SKIPPED_USER, SKIPPED_ATS, SKIPPED, APPLIED, UNSKIPPED = "skipped_user", "skipped_ats", "skipped", "applied", "unskipped"
BLOCKING = (SKIPPED_USER, SKIPPED_ATS, SKIPPED, APPLIED)


def path(settings) -> Path:
    return settings.path("state") / FILE


def id_key(job_id: str | None) -> str:
    return f"id:{job_id}" if job_id else ""


def keys_list(keys: LK.Keys, job_id: str | None = None) -> list[str]:
    return [k for k in (id_key(job_id), keys.ats, keys.url, keys.fingerprint) if k]


def append(settings, keys: LK.Keys | Iterable[str], decision: str, reason: str = "", job_id: str | None = None, ts: str | None = None) -> dict:
    """Append one line; returns it."""
    ks = keys_list(keys, job_id) if isinstance(keys, LK.Keys) else [k for k in keys if k]
    if job_id and id_key(job_id) not in ks:
        ks.insert(0, id_key(job_id))
    rec = {"ts": ts or datetime.now().isoformat(timespec="seconds"), "job_id": job_id or "", "keys": ks, "decision": decision, "reason": reason}
    p = path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def records(settings) -> list[dict]:
    p = path(settings)
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("keys"), list):
            out.append(r)
    return out


def _kind(key: str) -> str:
    return "id" if key.startswith("id:") else "ats" if key.startswith("ats:") else "url" if key.startswith("https://") else "fingerprint"


def repost_label(applied: bool, date: str, job_id: str = "") -> str:
    """`possible repost of a job you skipped on 2026-10-20 (manual:abc)`. Never a `?`: a date that was not recorded is said so, a missing id is left out."""
    what = "applied to" if applied else "skipped"
    return f"possible repost of a job you {what} " + (f"on {date}" if date else "(date not recorded)") + (f" ({job_id})" if job_id else "")


@dataclass
class Match:
    record: dict
    key: str
    kind: str                      # id | ats | url | fingerprint

    @property
    def decision(self) -> str:
        return self.record["decision"]

    @property
    def date(self) -> str:
        return str(self.record.get("ts") or "")[:10]

    def label(self) -> str:
        return repost_label(self.decision == APPLIED, self.date, self.record.get("job_id") or "")


@dataclass
class Book:
    latest: dict[str, dict] = field(default_factory=dict)          # key -> the last record that names it

    @classmethod
    def load(cls, settings) -> "Book":
        b = cls()
        for r in records(settings):
            for k in r["keys"]:
                b.latest[k] = r
        return b

    def _hit(self, keys: Iterable[str], kinds: tuple[str, ...]) -> Match | None:
        for k in keys:
            if not k or _kind(k) not in kinds:
                continue
            r = self.latest.get(k)
            if r is not None and r.get("decision") in BLOCKING:
                return Match(r, k, _kind(k))
        return None

    def decided(self, keys: LK.Keys, job_id: str | None = None) -> Match | None:
        """The decision this job is covered by: its id, ATS key or URL key (never the fingerprint alone)."""
        return self._hit([id_key(job_id), keys.ats, keys.url], ("id", "ats", "url"))

    def repost(self, keys: LK.Keys, job_id: str | None = None) -> Match | None:
        """A fingerprint-only match (and no strong one): `possible repost of a job you skipped on <date>`."""
        if self.decided(keys, job_id) is not None:
            return None
        fp = keys.fingerprint
        return self._hit([fp, fp.rsplit("|", 1)[0] + "|" if fp else ""], ("fingerprint",))      # a decided job with no place recorded still counts


def why(settings, keys: LK.Keys, job_id: str | None = None, extra: Iterable[str] = ()) -> list[tuple[dict, list[str]]]:
    """Every record that names one of these keys (`extra`: more keys of the same listing), oldest first, with the keys that matched."""
    mine = set(keys_list(keys, job_id)) | {k for k in extra if k}
    out = []
    for r in records(settings):
        hit = [k for k in r["keys"] if k in mine]
        if hit:
            out.append((r, hit))
    out.sort(key=lambda x: str(x[0].get("ts") or ""))
    return out


def find_id(settings, job_id: str) -> list[dict]:
    """The records of an old job id (it may no longer be in the database or in any folder): by their `id:` key or their `job_id`, oldest first."""
    want = id_key(job_id)
    out = [r for r in records(settings) if want in r["keys"] or r.get("job_id") == job_id]
    return sorted(out, key=lambda r: str(r.get("ts") or ""))


def decision_of_row(row) -> str | None:
    """The decision a database row stands for (None when it is not skipped / applied)."""
    status, reason = str(row["status"] or ""), str(row["reason"] or "").lower()
    if status.startswith("applied"):
        return APPLIED
    if status == "skipped (ats)":
        return SKIPPED_ATS
    if status in ("skipped", "skipped (user)") and ("user" in reason or "review" in reason or status == "skipped (user)"):
        return SKIPPED_USER
    if status.startswith("skipped"):
        return SKIPPED
    return None


def event_ts(db, row, decision: str) -> str:
    """When a skip / application really happened, from the events table: the application's `applied_at`; a skip of yours (`skip`, `live` \"skipped in
    review\", review's skip); an ATS-band skip's tailoring; else the row's `updated_at`; else its newest event; else when it was first seen."""
    jid, upd = row["id"], str(row["updated_at"] or "")[:19]
    evs = db.conn.execute("SELECT ts, stage, detail FROM events WHERE job_id = ? ORDER BY ts", (jid,)).fetchall()

    def last(pred) -> str:
        got = [str(e["ts"])[:19] for e in evs if pred(str(e["stage"] or ""), str(e["detail"] or ""))]
        return got[-1] if got else ""
    when = ""
    if decision == APPLIED and row["applied_at"]:
        a = str(row["applied_at"])[:19]
        when = a if len(a) > 10 else a + "T00:00:00"
    elif decision == SKIPPED_USER:
        when = last(lambda s, d: s == "skip" or (s == "live" and d.startswith("skipped")) or (s == "review" and "skip" in d.lower()))
    elif decision == SKIPPED_ATS:
        when = last(lambda s, d: s == "tailor")
    return when or upd or (str(evs[-1]["ts"])[:19] if evs else "") or str(row["first_seen"] or "")[:19]


def plan_db(db, have: set[str]) -> list[dict]:
    """The lines the log lacks for the database's skipped / applied jobs (those whose `id:` key it does not hold), each dated by its real event."""
    out = []
    for row in db.jobs():
        d = decision_of_row(row)
        if d is None or id_key(row["id"]) in have:
            continue
        out.append({"ts": event_ts(db, row, d), "job_id": row["id"], "keys": keys_list(LK.keys_of_row(row), row["id"]), "decision": d,
                    "reason": f"backfill: status '{row['status']}'" + (f", {row['reason']}" if row["reason"] else ""), "source": f"database row {row['id']}"})
        have.add(id_key(row["id"]))
    return out


def backfill(db, settings) -> list[dict]:
    """A line for every skipped / applied job of the database that has none yet (by its id), dated by its real event. Returns the lines written.
    (`apply/targets.backfill` also covers the JD files the database never held.)"""
    have = {k for r in records(settings) for k in r["keys"] if k.startswith("id:")}
    return [append(settings, r["keys"], r["decision"], r["reason"], r["job_id"], ts=r["ts"] or None) for r in plan_db(db, have)]
