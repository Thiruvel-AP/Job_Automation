"""`careerops patterns` (weekly, information only): what happened to the applications you made, by role family, by ATS kind and by triage band.

Source: the tracker Sheet's Status column (you own it: "Applied", "Interview", "Rejected" ...), read with the Sheets API; with no Sheet, or `--db`, the outcome log in
the database (the same facts, written by `outcome`, `mark-applied` and the reply watch). Job ID joins each Sheet row to the database for its triage score T.

  applications   rows with a Job ID
  replies        a Status other than blank / "Applied" / "Withdrawn" (a withdrawal is your move, not theirs)
  interviews     a Status that names an interview, a screen, an assessment or an offer
  rejections     a Status that names a rejection
  reply rate     replies / applications

It NEVER changes a setting or a weight. It may SUGGEST, for example "T >= 4.0 had 3/9 replies; T 3.5-4.0 had 0/7: consider `min_triage_score: 4.0`", and only
when both bands have at least MIN_BAND applications; changing settings is yours.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .. import targeting as targeting_mod
from ..apply import decided
from ..db import DB
from ..discovery import applykind

MIN_BAND = 5                    # applications a band needs before it is compared
GAP = 0.20                      # the better band's reply rate must lead by this much
BANDS = (("T >= 4.0", 4.0, 5.01), ("T 3.5-4.0", 3.5, 4.0), ("T < 3.5", 0.0, 3.5))
NOT_A_REPLY = ("", "applied", "withdrawn", "no response")
INTERVIEWISH = re.compile(r"interview|screen|assessment|offer", re.I)
REJECTISH = re.compile(r"reject", re.I)


@dataclass(frozen=True)
class Row:
    job_id: str
    status: str
    family: str = ""
    kind: str = ""
    t: float | None = None

    @property
    def replied(self) -> bool:
        return self.status.strip().lower() not in NOT_A_REPLY

    @property
    def interviewed(self) -> bool:
        return bool(INTERVIEWISH.search(self.status))

    @property
    def rejected(self) -> bool:
        return bool(REJECTISH.search(self.status))


@dataclass
class Tally:
    n: int = 0
    replies: int = 0
    interviews: int = 0
    rejections: int = 0

    def add(self, r: Row) -> None:
        self.n += 1
        self.replies += r.replied
        self.interviews += r.interviewed
        self.rejections += r.rejected

    @property
    def rate(self) -> float:
        return self.replies / self.n if self.n else 0.0

    def text(self) -> str:
        return f"{self.n} applied · {self.replies} replies ({100 * self.rate:.0f}%) · {self.interviews} interviews · {self.rejections} rejections"


def band_of(t: float | None) -> str:
    if t is None:
        return "T unknown"
    return next(name for name, lo, hi in BANDS if lo <= t < hi)


def score_of(db: DB, company: str, title: str) -> float | None:
    r = db.conn.execute("SELECT t FROM triage_scores WHERE key=?", ("|".join(decided.key_of(company, title)),)).fetchone()
    return float(r[0]) if r else None


def _enrich(db: DB, job_id: str, status: str, family: str = "", url: str = "") -> Row:
    j = db.job(job_id)
    kind = (j["apply_kind"] if j is not None and "apply_kind" in j.keys() and j["apply_kind"] else "") or applykind.kind_of(url or (j["url"] if j is not None else ""))
    return Row(job_id, status, family or (j["family"] if j is not None and j["family"] else ""),
               kind or "unknown", score_of(db, j["company"], j["title"]) if j is not None else None)


def rows_from_sheet(sheet, tab: str, db: DB) -> list[Row]:
    """The tracker rows: Job ID, Status, Role family, Posting URL (found by header text), joined to the database for T. Raises tracker.TrackerError when the
    tab has no Job ID or Status column."""
    from .. import tracker

    header = [str(h).strip() for h in ((sheet.get_values(f"{tracker._q(tab)}!1:1") or [[]])[0])]
    if tracker.KEY not in header or tracker.STATUS not in header:
        raise tracker.TrackerError(f"the '{tab}' tab has no Job ID or Status column")

    def column(name: str) -> list[str]:
        if name not in header:
            return []
        col = tracker.column_letter(header.index(name))
        return [(r[0] if r else "") for r in (sheet.get_values(f"{tracker._q(tab)}!{col}2:{col}") or [])]
    ids, status, family, url = (column(n) for n in (tracker.KEY, tracker.STATUS, "Role family", "Posting URL"))
    pad = lambda xs, n: xs + [""] * (n - len(xs))        # noqa: E731  (trailing empty cells are not returned by the API)
    return [_enrich(db, jid.strip(), st.strip(), fam.strip(), u.strip())
            for jid, st, fam, u in zip(ids, pad(status, len(ids)), pad(family, len(ids)), pad(url, len(ids))) if jid.strip()]


def rows_from_db(db: DB) -> list[Row]:
    """The applied jobs with the status their outcome log implies (offer > interview > screen > rejected > acknowledged > Applied)."""
    order = ["offer", "interview", "screen", "rejected", "acknowledged", "withdrawn"]
    label = {"offer": "Offer", "interview": "Interview", "screen": "Screen", "rejected": "Rejected", "acknowledged": "Acknowledged", "withdrawn": "Withdrawn"}
    out = []
    for j in db.jobs():
        if not j["applied_at"]:
            continue
        have = {r[0] for r in db.conn.execute("SELECT event FROM outcomes WHERE job_id=?", (j["id"],))}
        status = next((label[e] for e in order if e in have), "Applied")
        out.append(_enrich(db, j["id"], status))
    return out


def tally(rows: list[Row], key) -> dict[str, Tally]:
    out: dict[str, Tally] = {}
    for r in rows:
        out.setdefault(key(r) or "unknown", Tally()).add(r)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1].n, kv[0])))


def suggestions(rows: list[Row], settings) -> list[str]:
    """Suggestions only, from the triage bands. Empty when the bands are too small to compare."""
    by = tally(rows, lambda r: band_of(r.t))
    cfg = targeting_mod.config(settings)
    out = []
    for hi_name, lo_name, cut in (("T >= 4.0", "T 3.5-4.0", 4.0), ("T 3.5-4.0", "T < 3.5", 3.5)):
        hi, lo = by.get(hi_name), by.get(lo_name)
        if hi and lo and hi.n >= MIN_BAND and lo.n >= MIN_BAND and hi.rate - lo.rate >= GAP and cut > cfg.min_triage_score:
            out.append(f"{hi_name} had {hi.replies}/{hi.n} replies; {lo_name} had {lo.replies}/{lo.n}: consider `min_triage_score: {cut:.1f}` "
                       "(your call: nothing was changed)")
    return out


def report(rows: list[Row], settings, source: str, today: date | None = None) -> list[str]:
    today = today or date.today()
    if not rows:
        return [f"Patterns ({source}, {today.isoformat()}): no applications on record yet."]
    out = [f"Patterns ({source}, {today.isoformat()}); information only, no setting or weight is ever changed.", f"All: {tally(rows, lambda r: 'all')['all'].text()}"]
    for title, key in (("By role family", lambda r: r.family), ("By ATS kind", lambda r: r.kind), ("By triage score", lambda r: band_of(r.t))):
        out.append(title + ":")
        out += [f"  {name}: {t.text()}" for name, t in tally(rows, key).items()]
    s = suggestions(rows, settings)
    out += ["Suggestions:"] + [f"  {x}" for x in s] if s else [f"Suggestions: none (a band needs {MIN_BAND} applications and a {int(GAP * 100)}-point lead before it is compared)."]
    return out
