"""The agent decides, you veto (v3.2; settings agent.decide, code default "veto"). Ported from career-ops: triage -> full evaluation -> decision.

    1. every inbox ad that passes the hard stops gets the quick score (agent_triage.py, zero tokens): PASS / MARGINAL / FAIL;
    2. a PASS ad gets the full evaluation (evaluate.py, one model call, at most agent.max_evals_per_day a day; a stored one costs nothing);
    3. the decision:
         global >= 4.5, confidence Medium or High, apply = employer form  AUTO-APPROVED (priority)  -> data/jds/approved/<date>/
         4.0 <= global < 4.5, confidence Medium or High, employer form     AUTO-APPROVED             -> data/jds/approved/<date>/
         global >= 4.0, apply = Reed page or unknown                       READY FOR YOU             -> approved/ too: `live` builds the pack, you apply
         3.5 <= global < 4.0, or confidence Low                            MARGINAL                  held; listed in the weekly block; never approved by the agent
         global < 3.5, or quick score FAIL                                 AGENT-SKIP                held (the file stays in the inbox); `unskip` reverses it
         blacklist, closed posting, unreadable ad, agency with no employer NEEDS CONFIRMATION        held with the reason (career-ops "unattended runs")
       a possible repost of a job you skipped is MARGINAL (it needs you, as at [a]).
    4. caps: at most targeting.daily_max_applications approvals a day, at most targeting.per_company_max_open at one company (counting its open applications
       and the approved files still waiting), the cooldown; the highest global x freshness wins the slots, the rest WAIT (their evaluation is kept, so
       tomorrow costs no tokens).

Approval needs agent.approve_from (default 4.0; a value below 4.0 is not used). The decisions are logged in data/evals/decisions.jsonl (append-only) and
the numbered list in data/evals/digest-<date>.json, so `careerops veto 2` and `careerops promote 5` name a line of today's list.

Nothing here tailors, opens a form or submits: approval only moves a JD file to approved/, exactly as [a] did. Only autoapply.py submits.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import targeting as targeting_mod
from ..apply import decided, volume
from ..apply.naming import folder_key
from ..db import DB
from . import agent_triage as quick_mod
from . import evaluate as eval_mod
from . import gates, repost, shortlist, triage
from . import size as size_mod
from .http import Fetcher

MODES = ("veto", "ask", "auto")
DEFAULT_MODE = "veto"
APPROVE_FROM, PRIORITY_FROM, MARGINAL_FROM = 4.0, 4.5, 3.5
AUTO_PRIORITY, AUTO, READY = "auto_approved_priority", "auto_approved", "ready"
MARGINAL, SKIP, CONFIRM, WAITING = "marginal", "skipped_agent", "needs_confirmation", "waiting"
VETOED, PROMOTED, RESET = "vetoed", "promoted", "reset"
ESTIMATE_SKIP = "skipped_estimate"            # v3.5.5: live did not tailor an approved ad whose estimate F is under priority.min_estimate (apply/estimate_skip.py)
APPROVING = (AUTO_PRIORITY, AUTO, READY)
HELD = (MARGINAL, SKIP, CONFIRM, ESTIMATE_SKIP)  # a logged decision that keeps the ad out of the agent's pool until `unskip`
LABEL = {AUTO_PRIORITY: "AUTO-APPROVED", AUTO: "AUTO-APPROVED", READY: "READY FOR YOU", MARGINAL: "MARGINAL", SKIP: "AGENT-SKIP",
         CONFIRM: "NEEDS CONFIRMATION", WAITING: "WAITING", ESTIMATE_SKIP: "ESTIMATE-SKIP"}
ORDER = (AUTO_PRIORITY, AUTO, READY, WAITING, MARGINAL, CONFIRM, SKIP)
RESUME = {"blacklist": "`careerops promote N` to evaluate it anyway", "posting_closed": "share a live posting (`careerops live --url LINK`)",
          "jd_unavailable": "paste the ad (`careerops live --clipboard`)", "agency": "`careerops promote N` once you know the employer"}
LOG = "decisions.jsonl"
VETO_REASON = f"skipped {shortlist.SKIP_MARK}: vetoed after the agent approved it, not offered again"
WEEK = 7
WORKDAY_HANDOFF = "hand-off: Workday needs a candidate account (never signed in by code)"
LIST_MAX = 15                                    # lines printed per block of the digest (the counts are always complete)


class DecideError(RuntimeError):
    """A veto / promote that cannot be done (one line, fit to print)."""


# ------------------------------------------------------------------ settings
def _agent(settings) -> dict:
    raw = (getattr(settings, "raw", None) or {}).get("agent") or {}
    return raw if isinstance(raw, dict) else {}


def mode(settings) -> str:
    v = str(_agent(settings).get("decide", DEFAULT_MODE)).strip().lower()
    return v if v in MODES else DEFAULT_MODE


def approve_from(settings) -> float:
    v = _agent(settings).get("approve_from", APPROVE_FROM)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and APPROVE_FROM <= v <= 5 else APPROVE_FROM


# ------------------------------------------------------------------ one job
@dataclass
class Item:
    key: str
    company: str
    title: str
    decision: str
    reason: str = ""
    path: Path | None = None
    job: dict | None = None
    job_id: str = ""
    route: str = ""
    quick: quick_mod.Quick | None = None
    ev: eval_mod.Evaluation | None = None
    age_days: float | None = None
    n: int = 0
    rank: float = 0.0
    dups: list[Path] = field(default_factory=list)
    freed_by: str = ""                           # v3.2.1: the veto whose freed slot this approval took ("<key>@<ts>")

    @property
    def name(self) -> str:
        return f"{self.company} | {self.title}"

    def score_text(self) -> str:
        if self.ev is not None:
            return f"{self.ev.global_score:.1f} ({self.ev.confidence})"
        return f"quick {self.quick.score:.1f}" if self.quick is not None else "-"

    def route_text(self) -> str:
        """` | apply: employer form (greenhouse)`; a Workday form adds the hand-off (v3.2.1). Only on an approved line."""
        if self.decision not in APPROVING:
            return ""
        out = f" | apply: {self.route or 'unknown'}"
        return out + (f" | {WORKDAY_HANDOFF}" if "workday" in (self.route or "").lower() else "")

    def line(self) -> str:
        head = f"{self.n:>2}. {LABEL[self.decision]} {self.score_text()} | {self.name}{self.route_text()}"
        if self.ev is not None and self.decision in (*APPROVING, WAITING, MARGINAL):
            why = "; ".join(self.ev.why()) or "-"
            return f"{head} | why: {why} | risk: {self.ev.risk()} | legitimacy {self.ev.legitimacy}" + (f" | {self.reason}" if self.reason else "")
        return f"{head} | {self.reason}" if self.reason else head


def freshness(age_days: float | None) -> float:
    """The multiplier the slots are ranked by: 1.0 up to 7 days old, down to 0.8 at 60 days and after; 0.9 when the date is not known."""
    if age_days is None:
        return 0.9
    if age_days <= 7:
        return 1.0
    return max(0.8, 1.0 - 0.2 * (age_days - 7) / 53)


def classify(ev: eval_mod.Evaluation, route: str, settings, *, repost_note: str = "") -> tuple[str, str]:
    """(decision, reason) of an evaluated job, before the caps. Pure: no file is read or moved."""
    g, conf = ev.global_score, ev.confidence
    if g < MARGINAL_FROM:
        return SKIP, f"global {g:.1f} < {MARGINAL_FROM}"
    if conf == "Low":
        return MARGINAL, "confidence Low: " + ("; ".join(ev.adjustments[-1:]) or "the evidence is thin") + " (the agent never approves on Low)"
    bar = approve_from(settings)
    if g < bar:
        return MARGINAL, f"global {g:.1f} < {bar:g}: worth it only for a specific reason"
    if repost_note:
        return MARGINAL, f"{repost_note}: needs you"
    if route.startswith("employer form"):
        return (AUTO_PRIORITY if g >= PRIORITY_FROM else AUTO), ""
    return READY, f"apply: {route}: the pack is built, you apply"


# ------------------------------------------------------------------ the log
def log_path(settings) -> Path:
    return eval_mod.root(settings) / LOG


def read_log(settings) -> list[dict]:
    try:
        text = log_path(settings).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("key"):
            out.append(e)
    return out


def log(settings, events: Sequence[dict], now: datetime) -> None:
    if not events:
        return
    p = log_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), **e}, ensure_ascii=False) + "\n")


def latest(settings) -> dict[str, dict]:
    """job key -> its latest decision line."""
    out: dict[str, dict] = {}
    for e in read_log(settings):
        out[e["key"]] = e
    return out


def held_keys(settings) -> set[str]:
    """The keys the agent holds (MARGINAL, AGENT-SKIP, NEEDS CONFIRMATION) and nobody has reset or promoted since."""
    return {k for k, e in latest(settings).items() if e.get("decision") in HELD}


def _event(it: Item, decision: str, by: str = "agent", **extra) -> dict:
    return {"key": it.key, "job_id": it.job_id, "company": it.company, "title": it.title, "decision": decision, "by": by,
            "global": it.ev.global_score if it.ev else None, "confidence": it.ev.confidence if it.ev else None,
            "quick": it.quick.score if it.quick else None, "reason": it.reason, "file": str(it.path) if it.path else "", **extra}


def approvals_on(settings, day: date) -> int:
    iso = day.isoformat()
    return sum(1 for e in read_log(settings) if e.get("decision") in APPROVING and e.get("by") == "agent" and str(e.get("ts") or "")[:10] == iso)


# ------------------------------------------------------------------ the run
@dataclass
class Digest:
    day: date
    items: list[Item] = field(default_factory=list)
    plan: shortlist.Plan | None = None
    tokens: int = 0
    tokens_today: int = 0
    evals_new: int = 0
    evals_cached: int = 0
    eval_waiting: int = 0
    errors: list[str] = field(default_factory=list)
    triage_fail: int = 0
    triage_marginal: int = 0
    week_marginal: list[dict] = field(default_factory=list)
    mode: str = DEFAULT_MODE
    dry_run: bool = False
    levels: list[str] = field(default_factory=list)       # v3.2.1: the effective roles.levels.exclude (header)
    freed: list[str] = field(default_factory=list)        # v3.2.1: what happened to the slots your vetoes freed

    def low_confidence_high(self) -> list[Item]:
        return [i for i in self.items if i.ev is not None and i.ev.global_score >= PRIORITY_FROM and i.ev.confidence == "Low"]


class _View:
    """What shortlist.build needs in agent mode: the held keys, the row limit and the quick score (with repost flags worked out once)."""

    def __init__(self, settings, db: DB, held: set[str], limit: int, now: datetime):
        self.settings, self.held, self.limit, self.now = settings, held, limit, now
        self._flags: dict[str, list[str]] | None = None
        self._db = db

    def _repost_flags(self, job: dict) -> list[str]:
        if self._flags is None:
            from .pretailor import _listings
            inbox = []
            for p in decided.txt_files(decided.inbox_dir(self.settings)):
                h = decided.header_of(p)
                if h.get("company") and h.get("title"):
                    inbox.append(repost.Listing(h["company"], h["title"], h.get("url") or "", (h.get("first seen") or "")[:10], h.get("posted") or "",
                                                id=f"inbox:{p.name}"))
            seen = {repost.url_key(x.url) for x in inbox if x.url}
            listings = inbox + [x for x in _listings(self._db) if not x.url or repost.url_key(x.url) not in seen]
            self._flags = repost.analyse(listings, self.now.date())
        name = Path(str(job.get("file") or "")).name
        return self._flags.get(f"inbox:{name}", [])

    def quick(self, job: dict, t) -> quick_mod.Quick:
        return quick_mod.quick(job, t, self.settings, now=self.now, repost_flags=self._repost_flags(job))


def _strict_and_ceiling(db: DB, job_id: str, jd: str, fb: dict, settings) -> tuple[float | None, float | None]:
    row = db.job(job_id) if job_id else None
    strict = 100.0 * row["score"] if row is not None and row["score"] is not None and row["out_dir"] else None
    ceiling = row["ceiling"] if row is not None and row["ceiling"] is not None and row["out_dir"] else None
    if ceiling is None:
        try:
            ceiling = gates.pre_ceiling(jd, fb, settings)
        except Exception:                                  # noqa: BLE001 - a ceiling that cannot be worked out is "not known", never a crash
            ceiling = None
    return strict, (100.0 * ceiling if ceiling is not None else None)


def _inputs(row: shortlist.Row, db: DB, fb: dict, settings, live: str) -> eval_mod.Inputs:
    job = row.job
    jid = decided.file_job_id(row.path) or str(job.get("id") or row.path.name)
    jd = str(job.get("description") or "")
    strict, ceiling = _strict_and_ceiling(db, jid, jd, fb, settings)
    fam = gates.match_family(str(job.get("title") or ""), settings)
    legit = {"age_days": "unknown" if row.t.age_days is None else f"{row.t.age_days:.0f}", "liveness": live}
    flags = [f for f in (row.quick.flags if row.quick else []) if f.startswith(("evergreen?", "possible repost"))]
    if flags:
        legit["repost"] = flags[0]
    if row.repost:
        legit["skipped before"] = row.repost
    return eval_mod.Inputs(jid, str(job["company"]), str(job["title"]), jd, row.route, str(job.get("location") or ""), fam.name if fam else "",
                           list(row.t.have), list(row.t.lack), row.t.e, ceiling, strict, str(job.get("sponsor_match") or ""),
                           (f"{row.size.tag}" if row.size is not None else ""), (f"top £{row.t.pay_top:,}" if row.t.pay_top else ""), legit,
                           list(row.quick.hard if row.quick else []), row.quick.score if row.quick else None)


def _item(row: shortlist.Row, decision: str, reason: str = "") -> Item:
    return Item(row.key, str(row.job["company"]), str(row.job["title"]), decision, reason, row.path, row.job,
                decided.file_job_id(row.path) or str(row.job.get("id") or ""), row.route, row.quick, None, row.t.age_days,
                dups=[d.path for d in row.dups])


def run(settings, db: DB, fb: dict, llm, *, now: datetime | None = None, fetcher: Fetcher | None = None, archive: list[tuple[str, str]] | None = None,
        regions: Sequence[str] | None = None, size_mode: str | None = None, size_resolver: size_mod.Resolver | None = None,
        choose_size: Callable[[Counter], str] | None = None, dry_run: bool = False) -> Digest:
    """Build the pool, evaluate, decide, apply the caps and (unless `dry_run`) move the approved files and log every decision."""
    now = now or datetime.now(timezone.utc)
    day = now.date()
    cfg = targeting_mod.config(settings)
    held = held_keys(settings)
    limit = eval_mod.max_evals_per_day(settings) + cfg.daily_max_applications
    view = _View(settings, db, held, limit, now)
    plan = shortlist.build(settings, db, fb, fetcher=fetcher, now=now, archive=archive, regions=regions, size_mode=size_mode,
                           size_resolver=size_resolver, choose_size=choose_size, agent=view)
    if not dry_run:
        shortlist.apply_drops(settings, plan)
    d = Digest(day, plan=plan, mode=mode(settings), dry_run=dry_run)
    d.triage_fail, d.triage_marginal = len(plan.failed), len(plan.marginal)
    items: list[Item] = []
    for path, job, code, detail in plan.confirm:
        if job is not None:
            key, company, title = shortlist._key(job["company"], job["title"]), str(job["company"]), str(job["title"])
        else:
            key, company, title = f"file:{path.name}", path.name, "(unreadable)"
        it = Item(key, company, title, CONFIRM, f"needs_confirmation: {code} {detail} - {RESUME.get(code, 'tell the agent')}", path, job,
                  (decided.file_job_id(path) or "") if job is not None else "")
        items.append(it)
    left = eval_mod.evals_left(settings, day)
    resets = {k: str(e.get("ts") or "") for k, e in latest(settings).items() if e.get("decision") == RESET}
    for row in plan.rows:
        live = "open or not checked"
        it = _item(row, WAITING)
        inp = _inputs(row, db, fb, settings, live)
        it.job_id = inp.job_id
        ev = eval_mod.cached(settings, inp.job_id, inp.jd)
        if ev is not None and resets.get(row.key, "") > ev.ts:              # you reset it after this evaluation: evaluate afresh
            ev = None
        if ev is not None:
            d.evals_cached += 1
        elif left <= 0:
            it.reason = f"waiting: agent.max_evals_per_day ({eval_mod.max_evals_per_day(settings)}) evaluations made today"
            d.eval_waiting += 1
            items.append(it)
            continue
        else:
            try:
                ev = eval_mod.evaluate(llm, inp, settings, now=now)
            except Exception as e:                       # noqa: BLE001 - budget spent, no provider, a reply that is not JSON: the job waits, the run goes on
                it.reason = f"waiting: not evaluated ({type(e).__name__}: {str(e)[:160]})"
                d.errors.append(f"{it.name}: {it.reason}")
                items.append(it)
                continue
            left -= 1
            d.evals_new += 1
            d.tokens += ev.tokens
        it.ev = ev
        it.decision, it.reason = classify(ev, row.route, settings, repost_note=row.repost)
        it.rank = ev.global_score * freshness(row.t.age_days)
        items.append(it)
    for row in plan.marginal:
        items.append(_item(row, MARGINAL, f"quick score {row.quick.score:.1f} MARGINAL ({row.quick.text()}): no full evaluation"))
    for row in plan.failed:
        items.append(_item(row, SKIP, f"quick score FAIL: {row.quick.text()}"))
    slots = freed_slots(settings, day)
    prev = prev_waiting(settings, day)
    caps(items, settings, db, day, freed=slots, prev=prev)
    d.freed = freed_lines(settings, items, slots, day)
    d.levels = level_lines(settings)
    order = {k: i for i, k in enumerate(ORDER)}
    items.sort(key=lambda i: (order[i.decision], -(i.ev.global_score if i.ev else (i.quick.score if i.quick else 0)), i.name))
    for n, it in enumerate(items, 1):
        it.n = n
    d.items = items
    if not dry_run:
        commit(settings, items, now)
    d.tokens_today = _tokens_today(db, day)
    d.week_marginal = [e for e in latest(settings).values() if e.get("decision") == MARGINAL and str(e.get("ts") or "")[:10] >= (day - timedelta(days=WEEK)).isoformat()]
    if not dry_run:
        save_digest(settings, d)
    return d


def caps(items: list[Item], settings, db: DB, day: date, *, freed: Sequence[dict] = (), prev: set[str] | frozenset = frozenset()) -> None:
    """Demote the approvals over the caps to WAITING: the daily approvals, the per-company limit (open applications + approved files waiting + this run's),
    the cooldown. The highest global x freshness is kept first, except that a slot your veto freed (`freed`, freed_slots) goes first to the best job that was
    WAITING in an earlier digest (`prev`). A freed slot is priority inside the daily cap, never an extra approval: code never raises a cap."""
    cfg = targeting_mod.config(settings)
    room = cfg.daily_max_applications - approvals_on(settings, day)
    apps = volume.applications(db, settings)
    waiting = Counter(folder_key(decided.header_of(p).get("company") or "") for p in shortlist.approved_files(settings, db))
    taken: Counter = Counter()
    slots = list(freed)
    pending = sorted((i for i in items if i.decision in APPROVING), key=lambda i: -i.rank)
    while pending:
        it = next((i for i in pending if slots and i.key in prev), pending[0])
        pending = [i for i in pending if i is not it]
        fam = folder_key(it.company)
        if (why := volume.company_block(db, settings, it.company, day, apps=apps)):
            it.decision, it.reason = WAITING, f"waiting: {why}"
            continue
        have = volume.open_count(db, settings, it.company, day, apps=apps) + waiting[fam] + taken[fam]
        if have >= cfg.per_company_max_open:
            it.decision, it.reason = WAITING, (f"waiting: {it.company} already has {have} (open applications + approved and waiting); "
                                              f"targeting.per_company_max_open is {cfg.per_company_max_open}")
            continue
        if room <= 0:
            it.decision, it.reason = WAITING, f"waiting: {cfg.daily_max_applications} approvals today (targeting.daily_max_applications); next run"
            continue
        room -= 1
        taken[fam] += 1
        if slots and it.key in prev:
            v = slots.pop(0)
            it.freed_by = v["id"]
            it.reason = f"the slot your veto of {v['company']} | {v['title']} ({str(v['ts'])[:10]}) freed"


def freed_slots(settings, day: date) -> list[dict]:
    """The slots your vetoes freed that a WAITING job may take on `day`: a veto frees one from the day after it (never the same day: that day's approvals
    are already counted), until an approval takes it. Oldest first."""
    rows = read_log(settings)
    used = {e.get("freed_by") for e in rows if e.get("freed_by")}
    out = []
    for e in rows:
        vid = f"{e['key']}@{e.get('ts')}"
        if e.get("decision") == VETOED and str(e.get("frees_slot_on") or "9999") <= day.isoformat() and vid not in used:
            out.append({"id": vid, "company": e.get("company"), "title": e.get("title"), "ts": e.get("ts")})
    return out


def pending_slots(settings, day: date) -> list[dict]:
    """Vetoes whose freed slot only opens after `day` (made today): said in the header, used at the next run."""
    return [e for e in read_log(settings) if e.get("decision") == VETOED and str(e.get("frees_slot_on") or "") > day.isoformat()]


def prev_waiting(settings, day: date) -> set[str]:
    """The keys that were WAITING in the latest digest of a day before `day`."""
    for d, rows in reversed(_digests(settings)):
        if d < day.isoformat():
            return {r["key"] for r in rows if r.get("decision") == WAITING and r.get("key")}
    return set()


def freed_lines(settings, items: list[Item], slots: list[dict], day: date) -> list[str]:
    out = [f"slot freed by your veto of {i.reason.split('veto of ', 1)[-1].removesuffix(' freed')} -> approved {i.name} (it was WAITING)"
           for i in items if i.freed_by]
    left = len(slots) - len(out)
    if left > 0:
        out.append(f"{left} slot(s) freed by your vetoes: no earlier WAITING job could take one this run; the next run tries again")
    later = pending_slots(settings, day)
    if later:
        names = "; ".join(f"{e.get('company')} | {e.get('title')}" for e in later)
        out.append(f"{len(later)} slot(s) freed by your veto today ({names}): the best WAITING job takes it at the next run, not today")
    return out


def level_lines(settings) -> list[str]:
    """The effective roles.levels.exclude, as the quick score's hard stop reads it, and whether a Senior / Sr. title is excluded."""
    def plain(p: str) -> str:
        return p.replace("\\b", "").replace("\\.?", ".").replace("\\s", " ")
    mine = [plain(p) for p in settings["roles"]["levels"]["exclude"]]
    always = [plain(p) for p in gates.ALWAYS_EXCLUDED]
    out = [f"levels.exclude (effective): {', '.join(mine) or '(none)'} + always: {', '.join(always)}"]
    if gates.level_excluded("Senior Data Scientist", settings) is None:
        out.append("  senior: NOT excluded: a Senior or Sr. title passes (Sr. / Snr are read as Senior, so a `sr` pattern alone excludes nothing); "
                   "add '\\bsenior\\b' to roles.levels.exclude to exclude both (only you edit settings.yaml)")
    else:
        out.append("  senior: excluded (Senior, Sr. and Snr titles alike)")
    return out


def commit(settings, items: list[Item], now: datetime) -> None:
    """Move the approved files to approved/<date>/ (their merged copies to jds_done/duplicate/) and log every decision but WAITING."""
    events = []
    folder = shortlist.approved_root(settings) / now.date().isoformat()
    for it in items:
        if it.decision in APPROVING and it.path is not None:
            try:
                dst = decided.move_file(settings, it.path, folder)
            except (OSError, decided.DecidedError) as e:
                it.decision, it.reason = WAITING, f"waiting: the file could not be moved ({e})"
                continue
            for dp in it.dups:
                shortlist.file_away(settings, dp, shortlist.DUPLICATE, f"duplicate of {dst.name} (same company, title and location), approved by the agent on {now.date()}")
            it.path = dst
            events.append(_event(it, it.decision, **({"freed_by": it.freed_by} if it.freed_by else {})))
        elif it.decision == CONFIRM or (it.decision in (MARGINAL, SKIP) and it.ev is not None):      # a quick-score verdict is worked out again each run
            events.append(_event(it, it.decision))
    log(settings, events, now)
    shortlist.log_events(settings, [{"event": "approved", "key": it.key, "company": it.company, "title": it.title, "by": "agent"}
                                    for it in items if it.decision in APPROVING], now)


def _tokens_today(db: DB, day: date) -> int:
    try:
        row = db.conn.execute("SELECT COALESCE(SUM(tokens), 0) FROM llm_task_usage WHERE day=? AND task=?", (day.isoformat(), eval_mod.TASK)).fetchone()
    except Exception:                                        # noqa: BLE001 - an old database without the table: no count
        return 0
    return int(row[0] or 0)


# ------------------------------------------------------------------ what is printed
def lines(d: Digest) -> list[str]:
    c = Counter(i.decision for i in d.items)
    approved = c[AUTO_PRIORITY] + c[AUTO]
    out = [f"Agent decisions ({d.day.isoformat()}, agent.decide: {d.mode}" + (", dry run: nothing moved or logged" if d.dry_run else "") + "): "
           f"{approved} auto-approved ({c[AUTO_PRIORITY]} priority), {c[READY]} ready for you, {c[WAITING]} waiting, {c[MARGINAL]} marginal, "
           f"{c[SKIP]} agent-skip, {c[CONFIRM]} need confirmation.",
           f"evaluations: {d.evals_new} new, {d.evals_cached} read back" + (f", {d.eval_waiting} waiting for tomorrow's allowance" if d.eval_waiting else "")
           + f" · {eval_mod.TASK} tokens this run: {d.tokens:,} (today: {d.tokens_today:,})"
           + f" · quick score: {d.triage_marginal} marginal and {d.triage_fail} fail without a model call", *d.levels, *d.freed]
    by: dict[str, list[Item]] = {}
    for it in d.items:
        by.setdefault(it.decision, []).append(it)
    for dec in ORDER:
        rows = by.get(dec, [])
        if not rows:
            continue
        out.append("")
        out += [it.line() for it in rows[:LIST_MAX]]
        if len(rows) > LIST_MAX:
            out.append(f"    ... and {len(rows) - LIST_MAX} more {LABEL[dec]}")
    if (low := d.low_confidence_high()):
        out += ["", "4.5+ but Low confidence (never approved by the agent):"]
        out += [f"  {i.n}. {i.name} {i.ev.global_score:.1f}: " + ("; ".join(i.ev.adjustments) or "unknown: " + ", ".join(
            k for k, v in i.ev.dimensions.items() if v.get("status") == "unknown")) for i in low]
    if d.week_marginal:
        out += ["", f"Marginal in the last {WEEK} days ({len(d.week_marginal)}): " + "; ".join(
            f"{e['company']} | {e['title']} {e.get('global') or e.get('quick') or ''}" for e in d.week_marginal[:LIST_MAX])]
    if d.plan is not None and d.plan.held:
        out += ["", f"held back at a company limit before any evaluation ({len(d.plan.held)}): " + "; ".join(f"{n}: {w}" for n, w in d.plan.held[:5])]
    out += [f"  error: {e}" for e in d.errors[:5]]
    if any(i.decision in APPROVING for i in d.items):
        out += ["", "Veto before `careerops live`: careerops veto N (skipped for good) · careerops promote N (a MARGINAL / AGENT-SKIP / WAITING line, "
                "approved as your override) · careerops unskip --shortlist (brings back what the agent held)"]
    return out


def digest_path(settings, day: date) -> Path:
    return eval_mod.root(settings) / f"digest-{day.isoformat()}.json"


def save_digest(settings, d: Digest) -> None:
    rows = [{"n": i.n, "key": i.key, "job_id": i.job_id, "company": i.company, "title": i.title, "decision": i.decision,
             "global": i.ev.global_score if i.ev else None, "confidence": i.ev.confidence if i.ev else None, "quick": i.quick.score if i.quick else None,
             "file": str(i.path) if i.path else "", "reason": i.reason, "route": i.route} for i in d.items]
    write_digest(settings, d.day.isoformat(), rows)


def write_digest(settings, day: str, rows: list[dict]) -> None:
    p = eval_mod.root(settings) / f"digest-{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": day, "items": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _digests(settings) -> list[tuple[str, list[dict]]]:
    """(day, lines) of every readable digest, oldest first."""
    try:
        files = sorted(eval_mod.root(settings).glob("digest-*.json"))
    except OSError:
        return []
    out = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append((str(d.get("day") or p.stem.removeprefix("digest-")), list(d.get("items") or [])))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def last_digest(settings) -> list[dict]:
    got = _digests(settings)
    return got[-1][1] if got else []


def find(settings, what: str) -> dict:
    """The line of the latest digest that `what` names: its number, its job id, its JD file name, or a key. Raises DecideError."""
    q = (what or "").strip()
    rows = last_digest(settings)
    if not rows:
        raise DecideError("the latest agent digest has no lines (or there is none yet): run `careerops shortlist` (or `daily`) first")
    if q.isdigit():
        hit = next((r for r in rows if r.get("n") == int(q)), None)
    else:
        name = q if q.endswith(".txt") else q + ".txt"
        hit = next((r for r in rows if q in (r.get("job_id"), r.get("key")) or Path(str(r.get("file") or "")).name in (q, name)), None)
    if hit is None:
        raise DecideError(f"{q!r} is not a line of the latest agent digest (numbers 1-{len(rows)}, a job id or a JD file name)")
    return hit


def _current_file(settings, row: dict) -> Path | None:
    p = Path(str(row.get("file") or ""))
    if p.is_file():
        return p
    from ..apply import targets
    return targets.find_file(settings, p.name) if p.name else None


def veto(settings, db: DB, what: str, *, now: datetime | None = None) -> list[str]:
    """An auto-approved (or ready) job becomes your permanent skip: its file goes to jds_done/skipped/ with the shortlist skip mark, the decision log says
    skipped (user), and `live` never sees it. Raises DecideError for a line the agent did not approve."""
    from ..apply import targets
    from .. import review

    now = now or datetime.now(timezone.utc)
    row = find(settings, what)
    if row.get("decision") not in APPROVING:
        raise DecideError(f"line {row.get('n')} ({row['company']} | {row['title']}) is {LABEL.get(row.get('decision'), row.get('decision'))}, not approved by "
                          "the agent: nothing to veto (`careerops skip` skips any job)")
    p = _current_file(settings, row)
    if p is None:
        raise DecideError(f"the JD file of {row['company']} | {row['title']} is no longer in the inbox or approved/ (applied or skipped already?)")
    try:
        t = targets.resolve(settings, db, str(p))
        job = t.job
        if job is not None and str(job["status"] or "").startswith("applied"):
            raise DecideError(f"{job['id']} is applied: an applied decision is final")
        out = []
        if job is not None and str(job["status"] or "") != "skipped":
            review.skip_user(db, settings, job["id"], where="veto")
            out.append(f"database row {job['id']} set to skipped")
        out += targets.skip_files(settings, targets.resolve(settings, db, str(p)), VETO_REASON)
    except decided.DecidedError as e:
        raise DecideError(str(e)) from e
    opens = (now.date() + timedelta(days=1)).isoformat()
    log(settings, [{"key": row["key"], "job_id": row.get("job_id"), "company": row["company"], "title": row["title"], "decision": VETOED, "by": "user",
                    "global": row.get("global"), "reason": "careerops veto", "frees_slot_on": opens}], now)
    shortlist.log_events(settings, [{"event": "skipped", "key": row["key"], "company": row["company"], "title": row["title"], "by": "user veto"}], now)
    return [f"vetoed {row['company']} | {row['title']}: skipped for good (as [s]); `careerops unskip --shortlist --match ...` is the only way back",
            f"the slot it held goes to the best WAITING job at the next run from {opens} (not today: today's approvals are already counted)", *out]


# ------------------------------------------------------------------ recheck (v3.2.1)
def recheck(settings, db: DB, fb: dict, *, now: datetime | None = None, apply: bool = False) -> list[str]:
    """The latest digest's lines under today's rules, zero tokens: no evaluation is run again. Reports how the quick-score verdicts move, and the WAITING /
    MARGINAL / approved lines a hard disqualifier now stops. With `apply`, a WAITING or MARGINAL line that is now hard-disqualified becomes AGENT-SKIP (held,
    logged; `unskip --shortlist` reverses it) and the digest line says so; an approved line is only named (a veto is yours). v3.5.4: first, the approved
    files are checked against the hard stops `live` now runs before tailoring (apply/hardstop.py); with `apply` they move to jds_done/skipped_agent/."""
    from . import register as sponsors

    from ..apply import hardstop

    now = now or datetime.now(timezone.utc)
    stops = hardstop.find(shortlist.approved_files(settings, db), settings)       # v3.5.4: what `live` would now stop before tailoring
    head = [f"Approved files (live's source) under today's hard stops: {len(stops)} stopped" + ("" if apply or not stops else " (dry run: --apply moves them)"),
            *hardstop.lines(stops, applied=apply)]
    if apply and stops:
        hardstop.file_away(settings, stops, now=now)
    got = _digests(settings)
    if not got:
        return ["recheck: no agent digest yet: run `careerops shortlist` (or `daily`) first", *head]
    day, rows = got[-1]
    idx = sponsors.load_index(db)
    floor = triage.salary_floor(settings)
    view = _View(settings, db, set(), 0, now)
    moves: dict[str, Counter] = {"FAIL": Counter(), "MARGINAL": Counter()}
    stopped, approved_hit, events = [], [], []
    for r in rows:
        p = _current_file(settings, r)
        job = triage.inbox_job(p, settings, fb, idx) if p is not None else None
        if job is None:
            continue
        q = view.quick(job, triage.score(job, settings, fb, floor=floor, now=now))
        was, quick_only = r.get("decision"), r.get("global") is None
        if quick_only and was in (SKIP, MARGINAL):
            moves["FAIL" if was == SKIP else "MARGINAL"][q.verdict] += 1
        if not q.hard:
            continue
        name = f"{r.get('n')}. {r['company']} | {r['title']} ({LABEL.get(was, was)})"
        if was in APPROVING:
            approved_hit.append(f"  {name}: {q.hard[0]} -> approved before this rule: `careerops veto {r.get('n')}` if you agree")
        elif was in (WAITING, MARGINAL):
            stopped.append(f"  {name}: {q.hard[0]} -> AGENT-SKIP" + ("" if apply else " (dry run: --apply records it)"))
            if apply:
                r["decision"], r["reason"] = SKIP, f"recheck {now.date().isoformat()}: hard disqualifier {q.hard[0]}"
                events.append({"key": r["key"], "job_id": r.get("job_id"), "company": r["company"], "title": r["title"], "decision": SKIP, "by": "agent",
                               "global": r.get("global"), "quick": q.score, "reason": r["reason"], "file": str(p)})
    out = [f"Recheck of the agent digest of {day} under today's rules (zero tokens: no evaluation was run again):", *level_lines(settings)]
    for old, c in moves.items():
        n = sum(c.values())
        if n:
            out.append(f"  quick-score {old} lines ({n}): -> PASS {c['PASS']}, -> MARGINAL {c['MARGINAL']}, still FAIL {c['FAIL']}"
                       if old == "FAIL" else f"  quick-score {old} lines ({n}): -> PASS {c['PASS']}, still MARGINAL {c['MARGINAL']}, -> FAIL {c['FAIL']}")
    out += ["  newly hard-disqualified:"] + stopped if stopped else ["  newly hard-disqualified (waiting / marginal): none"]
    out += ["  approved but now hard-disqualified:"] + approved_hit if approved_hit else []
    out.append("  a line that moves to PASS is evaluated at the next run (agent.max_evals_per_day); a quick-score verdict is never logged, so nothing else changes")
    if apply and events:
        log(settings, events, now)
        write_digest(settings, day, rows)
    return out + head


def promote(settings, db: DB, what: str, *, now: datetime | None = None) -> list[str]:
    """A MARGINAL, AGENT-SKIP, WAITING or NEEDS CONFIRMATION line becomes approved: its file moves to approved/<today>/ and the log records your override."""
    now = now or datetime.now(timezone.utc)
    row = find(settings, what)
    if row.get("decision") in APPROVING:
        raise DecideError(f"line {row.get('n')} ({row['company']} | {row['title']}) is already approved")
    p = _current_file(settings, row)
    if p is None or p.parent.resolve() != decided.inbox_dir(settings).resolve():
        raise DecideError(f"the JD file of {row['company']} | {row['title']} is not in the inbox (" + (f"it is in {p.parent.name}/" if p else "not found")
                          + "); a closed posting needs a live one: careerops live --url LINK")
    dst = decided.move_file(settings, p, shortlist.approved_root(settings) / now.date().isoformat())
    log(settings, [{"key": row["key"], "job_id": row.get("job_id"), "company": row["company"], "title": row["title"], "decision": PROMOTED, "by": "user",
                    "global": row.get("global"), "was": row.get("decision"), "reason": f"user override of {LABEL.get(row.get('decision'), row.get('decision'))}",
                    "file": str(dst)}], now)
    shortlist.log_events(settings, [{"event": "approved", "key": row["key"], "company": row["company"], "title": row["title"], "by": "user promote"}], now)
    return [f"override: promoted {row['company']} | {row['title']} ({LABEL.get(row.get('decision'), row.get('decision'))} {row.get('global') or row.get('quick') or ''})"
            f" -> approved/{now.date().isoformat()}/{dst.name} (logged as your override in {log_path(settings).name})"]


def reset(settings, *, match: str | None = None, now: datetime | None = None) -> list[str]:
    """`careerops unskip --shortlist`: the ads the agent holds (MARGINAL, AGENT-SKIP, NEEDS CONFIRMATION) go back into its pool. The reset is a log line:
    a stored evaluation older than it is not read back, so the job is evaluated afresh (no file is renamed or deleted). Your own skips are not touched here."""
    now = now or datetime.now(timezone.utc)
    want = (lambda n: True) if match is None else (lambda n: match.lower() in n.lower())
    events, out = [], []
    for key, e in latest(settings).items():
        name = f"{e.get('company', '')} | {e.get('title', '')}"
        if e.get("decision") in HELD and want(name):
            events.append({"key": key, "job_id": e.get("job_id"), "company": e.get("company"), "title": e.get("title"), "decision": RESET, "by": "user",
                           "was": e.get("decision")})
            out.append(f"back in the agent's pool: {name} (was {LABEL.get(e['decision'], e['decision'])})")
    log(settings, events, now)
    from ..apply import estimate_skip, hardstop

    keys = {e["key"] for e in events}
    for src, dst in hardstop.restore(settings, keys):        # v3.5.4: a file the hard stop filed away comes back to the inbox
        out.append(f"  moved {hardstop.FOLDER}/{src.name} -> inbox/{dst.name}")
    for src, dst in estimate_skip.restore(settings, keys):   # v3.5.5: so does one the estimate stop held back (and it is not estimate-skipped again)
        out.append(f"  moved {estimate_skip.FOLDER}/{src.name} -> inbox/{dst.name}")
    return out


def held_lines(settings) -> list[str]:
    return [f"  {e.get('company')} | {e.get('title')} ({LABEL.get(e['decision'], e['decision'])})" for e in latest(settings).values() if e.get("decision") in HELD]


# ------------------------------------------------------------------ calibrate
BANDS = (("4.5+", 4.5, 9.0), ("4.0-4.4", 4.0, 4.5), ("3.5-3.9", 3.5, 4.0), ("<3.5", 0.0, 3.5))
MIN_OUTCOMES = 20


def calibrate(settings, db: DB, *, today: date | None = None) -> list[str]:
    """Reply rate per score band (career-ops /calibrate). An outcome is a reply (acknowledged, screen, interview, offer, rejected) or no reply 28 days
    after applying. Suggests only: nothing here writes settings."""
    from ..reporting import outcomes as outcomes_mod

    today = today or date.today()
    scores = eval_mod.scores_by_job(settings)
    replied: dict[str, bool] = {}
    for r in db.conn.execute("SELECT job_id, event FROM outcomes"):
        if r["event"] in outcomes_mod.REPLIES:
            replied[r["job_id"]] = True
    rows: list[tuple[float, bool]] = []
    for j in db.jobs():
        if j["id"] not in scores or not (j["applied_at"] or j["status"] == "applied"):
            continue
        if replied.get(j["id"]):
            rows.append((scores[j["id"]], True))
            continue
        try:
            on = date.fromisoformat(str(j["applied_at"] or "")[:10])
        except ValueError:
            continue
        if (today - on).days >= outcomes_mod.NO_RESPONSE_DAYS:
            rows.append((scores[j["id"]], False))
    if len(rows) < MIN_OUTCOMES:
        return [f"calibrate: {len(rows)} scored application(s) with an outcome; at least {MIN_OUTCOMES} are needed before the bands say anything "
                "(an outcome: a reply, or 28 days with none)"]
    out = [f"Reply rate by agent score band ({len(rows)} scored applications with an outcome):"]
    rate: dict[str, float | None] = {}
    for label, lo, hi in BANDS:
        got = [ok for g, ok in rows if lo <= g < hi]
        k, n = sum(got), len(got)
        rate[label] = k / n if n else None
        ci = outcomes_mod.wilson(k, n)
        out.append(f"  {label:>7}: {k}/{n} replied" + (f" = {100 * k / n:.0f}%" if n else "") + (f" (95% {100 * ci[0]:.0f}-{100 * ci[1]:.0f}%)" if ci else ""))
    a, b = rate["4.0-4.4"], rate["3.5-3.9"]
    if a is not None and b is not None and a <= b:
        out.append(f"Suggestion: the 4.0-4.4 band replies no better than 3.5-3.9 ({100 * a:.0f}% vs {100 * b:.0f}%): consider `agent: approve_from: 4.5` in "
                   "settings.yaml (only you edit it; nothing was changed)")
    else:
        out.append("No change suggested: the approval line at 4.0 separates the bands (or a band has no data yet).")
    return out
