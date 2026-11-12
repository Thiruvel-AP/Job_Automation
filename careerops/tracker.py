"""The application tracker: one row per applied job in the `Applications` tab of your Google Sheet (tracker.sheet_id in settings.yaml).

  * `careerops mark-applied` (and `apply` / `live`, which end the same way) upserts the row keyed by Job ID; `careerops tracker sync`
    re-sends every applied job from the database. Both are idempotent: a row is updated, never duplicated, and nothing ever deletes one.
  * Columns are found by their header text in row 1, never by position, so you can add, move or rename-around columns. A header this
    module writes that row 1 lacks is appended at the end; nothing is reordered.
  * You own Status, Interview date and Notes. Status gets "Applied" only while its cell is empty; the other two are never written. The one
    exception is `set_status`, which the reply watch (reporting/replies.py) calls after you typed `y` to a printed proposal: that cell alone.
    On an update only cells that would change are written, one range per cell, so formulas, formatting and the dropdown on other cells
    are left alone.
  * Values go in with valueInputOption=RAW: a job id or URL is never turned into a number or a date. Dates are YYYY-MM-DD. The JD text
    is cut at JD_MAX characters, under the 50,000 a Sheets cell can hold.

The Sheets calls are `SheetsClient` (googleapi.py does the HTTP); tests pass a fake with the same four methods.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from . import contacts as contacts_mod
from . import pathstore
from . import outreach
from .config import load_yaml
from .db import DB
from .discovery import freshness, gmail, region as region_mod, size as size_mod, workcountry
from .googleapi import GoogleAPI

DEFAULT_TAB = "Applications"
JD_MAX = 45_000
BASE = "https://sheets.googleapis.com/v4/spreadsheets"
HEADERS = [
    "Job ID", "Date applied", "Company", "Role", "Location", "Work mode", "Posting URL", "Posting age (h)", "Sponsor match",
    "ATS strict (%)", "ATS ceiling (%)", "Stretch", "Role family", "Hook ID", "Hook headline", "CV file", "Letter",
    "Projects on CV", "Bullets on CV", "Top JD keywords", "Learn list", "Question to ask", "JD text",
    "Contact 1 name", "Contact 1 role", "Contact 1 email", "Contact 1 source",
    "Contact 2 name", "Contact 2 role", "Contact 2 email", "Contact 2 source",
    "Outreach status", "Follow-up due", "Status", "Interview date", "Notes",
    "Region", "Size",                       # v2.4, appended on the right: an older Sheet gets them added after its last column (ensure_headers)
]
KEY = "Job ID"
STATUS = "Status"
USER_OWNED = frozenset({"Status", "Interview date", "Notes"})
TOP_KEYWORDS = 8
_HYBRID = re.compile(r"\bhybrid\b", re.I)


class TrackerError(RuntimeError):
    """The tracker cannot be written (not configured, not signed in, Google refused). One line, fit to print."""


class Sheet(Protocol):
    def get_values(self, a1: str) -> list[list[str]]: ...
    def update_cells(self, cells: Mapping[str, Any]) -> None: ...
    def append_row(self, tab: str, row: list[Any]) -> None: ...
    def ensure_columns(self, tab: str, count: int) -> None: ...


# ------------------------------------------------------------------ the Sheets API
class SheetsClient:
    def __init__(self, api: GoogleAPI, sheet_id: str):
        self.api, self.sheet_id = api, sheet_id

    def _url(self, tail: str) -> str:
        return f"{BASE}/{quote(self.sheet_id, safe='')}/{tail}"

    def get_values(self, a1: str) -> list[list[str]]:
        data = self.api.call("GET", self._url("values/" + quote(a1, safe="")),
                             params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}, what=f"Sheets read {a1}")
        return [[str(c) for c in row] for row in data.get("values", [])]

    def update_cells(self, cells: Mapping[str, Any]) -> None:
        """One request writing every `a1 -> value` of `cells` as RAW."""
        if cells:
            self.api.call("POST", self._url("values:batchUpdate"), what="Sheets write",
                          body={"valueInputOption": "RAW", "data": [{"range": a1, "values": [[v]]} for a1, v in cells.items()]})

    def append_row(self, tab: str, row: list[Any]) -> None:
        self.api.call("POST", self._url("values/" + quote(f"{_q(tab)}!A1", safe="") + ":append"), what="Sheets append",
                      params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, body={"values": [row]})

    def ensure_columns(self, tab: str, count: int) -> None:
        """Grow the tab's grid to `count` columns when it is narrower (only needed when a header has to be appended)."""
        meta = self.api.call("GET", self._url(""), params={"fields": "sheets.properties(sheetId,title,gridProperties.columnCount)"},
                             what="Sheets metadata")
        for s in meta.get("sheets", []):
            p = s.get("properties", {})
            if p.get("title") == tab:
                have = int((p.get("gridProperties") or {}).get("columnCount", 0))
                if have < count:
                    self.api.call("POST", self._url(":batchUpdate"), what="Sheets add column",
                                  body={"requests": [{"appendDimension": {"sheetId": p["sheetId"], "dimension": "COLUMNS",
                                                                          "length": count - have}}]})
                return


def _q(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, 35 -> AJ."""
    s, n = "", index + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def settings_for(settings) -> tuple[str, str]:
    """(sheet id, tab) from settings.yaml `tracker`."""
    t = (settings.raw.get("tracker") or {}) if hasattr(settings, "raw") else {}
    sheet_id = str(t.get("sheet_id") or "").strip()
    if not sheet_id:
        raise TrackerError("tracker.sheet_id is not set in config/settings.yaml")
    return sheet_id, str(t.get("tab") or DEFAULT_TAB)


def configured(settings) -> bool:
    return bool(str(((settings.raw.get("tracker") or {}) if hasattr(settings, "raw") else {}).get("sheet_id") or "").strip())


def scope_problem(settings) -> str | None:
    """None when the Google sign-in on disk may edit the tracker Sheet; else one line saying what is wrong and how to fix it. Reads the
    token file only (no network): the scope list Google granted is kept in it."""
    token = gmail._read_token(settings)
    if not token or not token.get("refresh_token"):
        return "not signed in to Google: run `careerops discover --gmail-auth` (tick every box)"
    if gmail.SCOPE_SHEETS not in str(token.get("scope") or "").split():
        return f"the Google sign-in lacks {gmail.SCOPE_SHEETS}: run `careerops discover --gmail-auth` (tick every box)"
    return None


def client(settings, api: GoogleAPI | None = None) -> tuple[SheetsClient, str]:
    sheet_id, tab = settings_for(settings)
    return SheetsClient(api or GoogleAPI(settings, need=(gmail.SCOPE_SHEETS,)), sheet_id), tab


# ------------------------------------------------------------------ one job -> one record
def _job(row: Any) -> dict:
    return {k: row[k] for k in row.keys()} if not isinstance(row, dict) else row


def _load_report(job: Mapping[str, Any]) -> dict:
    try:
        return pathstore.read_report(Path(pathstore.resolve(job["out_dir"])) / "report.json")
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _applied(job: Mapping[str, Any]) -> datetime | None:
    return freshness.parse(job.get("applied_at"))


def work_mode(job: Mapping[str, Any], applicant: Mapping[str, Any] | None) -> str:
    """Remote | Hybrid | On-site, "" when the ad does not say (workcountry.py decides remote and on-site; "hybrid" is the ad's word)."""
    text = f"{job.get('location') or ''} {job.get('description') or ''}"
    try:
        mode = workcountry.for_job(job, applicant).mode
    except Exception:  # noqa: BLE001  (an applicant file without visa.home: the mode is simply not known)
        mode = "unknown"
    if mode == "remote" and not _HYBRID.search(str(job.get("location") or "")):
        return "Remote"
    if _HYBRID.search(text):
        return "Hybrid"
    return "On-site" if mode == "onsite" else ""


def _num(x: Any) -> float | str:
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return ""


def record(db: DB, settings, job_row: Any, fb: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The Sheet row of one applied job as {header: value}. Everything comes from the database, the job's report.json and the fact bank;
    a missing report leaves its columns empty."""
    from .note import question
    from .tailoring.letters import learn_list
    from .tailoring.scorer import Keyword

    job = _job(job_row)
    rep = _load_report(job)
    fb = fb if fb is not None else load_yaml(settings.path("fact_bank"))
    applicant = workcountry.applicant_visa(settings)          # applicant.yaml, read only
    hk = rep.get("hook") or {}
    ats = rep.get("ats") or {}
    ctx = rep.get("context") or {}
    kws = [Keyword(**k) for k in rep.get("keywords") or []]
    applied_at = _applied(job)
    age = freshness.age_hours(job, applied_at) if applied_at else None
    route = " ".join(str(x) for x in (job.get("sponsor_route"), job.get("sponsor_rating")) if x)
    match = str(job.get("sponsor_match") or "")
    ids = [i for sec in ("experience", "projects") for e in ctx.get(sec, []) or [] for i in e.get("bullet_ids", [])]
    people = contacts_mod.stored(db, job["id"])[:contacts_mod.MAX_CONTACTS]
    out: dict[str, Any] = {
        "Job ID": job["id"], "Date applied": str(job.get("applied_at") or "")[:10], "Company": job["company"], "Role": job["title"],
        "Location": _shown(job, applicant), "Work mode": work_mode(job, applicant), "Posting URL": job.get("url") or job.get("apply_url") or "",
        "Posting age (h)": _num(age) if age is not None else "", "Sponsor match": match + (f" ({route})" if match and route else ""),
        "ATS strict (%)": _num(ats.get("strict")), "ATS ceiling (%)": _num(ats.get("ceiling")),
        "Stretch": ("Yes" if rep.get("stretch") else "No") if rep else "", "Role family": job.get("family") or "",
        "Hook ID": (hk.get("hook_id") or "template") if hk else "", "Hook headline": hk.get("headline") or "",
        "CV file": Path(rep["cv_pdf"]).name if rep.get("cv_pdf") else "",
        "Letter": (Path(rep["letter_pdf"]).name if rep.get("letter_pdf") else "none") if rep else "",
        "Projects on CV": ", ".join(str(p.get("name") or "") for p in ctx.get("projects", []) or [] if p.get("name")),
        "Bullets on CV": ", ".join(ids),
        "Top JD keywords": ", ".join(k.term for k in sorted(kws, key=lambda k: -k.weight)[:TOP_KEYWORDS]),
        "Learn list": ", ".join(r["term"] for r in learn_list(kws, rep.get("claimable") or [], fb.get("deny_terms", []))) if kws else "",
        "Question to ask": question(rep, rep.get("team") or "", job["company"]) if rep else "",
        "JD text": str(job.get("description") or "")[:JD_MAX],
        "Outreach status": outreach.status(db, job["id"]), "Follow-up due": outreach.follow_up_due(db, settings, job),
        STATUS: "Applied",
        "Region": region_mod.region_of(job), "Size": size_mod.stored(db, job).cell,
    }
    for n, p in enumerate(people, 1):
        out.update({f"Contact {n} name": p.get("name") or "", f"Contact {n} role": p.get("role") or "",
                    f"Contact {n} email": p.get("email") or "", f"Contact {n} source": p.get("source") or ""})
    return out


def _shown(job: Mapping[str, Any], applicant: Mapping[str, Any]) -> str:
    """The location as it may be shown: the remote label for a remote role at home, never the home country."""
    from .apply import visa

    try:
        return str(visa.displayed_location(job, applicant) or job.get("location") or "")
    except Exception:  # noqa: BLE001
        return str(job.get("location") or "")


# ------------------------------------------------------------------ the upsert
def _same(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def ensure_headers(sheet: Sheet, tab: str) -> list[str]:
    """Row 1 as it is, plus any header of HEADERS it lacks appended at the end (never reordered, never renamed)."""
    rows = sheet.get_values(f"{_q(tab)}!1:1")
    header = [str(h).strip() for h in (rows[0] if rows else [])]
    missing = [h for h in HEADERS if h not in header]
    if missing:
        sheet.ensure_columns(tab, len(header) + len(missing))
        sheet.update_cells({f"{_q(tab)}!{column_letter(len(header) + i)}1": h for i, h in enumerate(missing)})
        header += missing
    return header


def upsert(sheet: Sheet, tab: str, rec: Mapping[str, Any]) -> str:
    """Write one record: a new row (`added`), the changed cells of the existing row for its Job ID (`updated`), or nothing (`unchanged`)."""
    header = ensure_headers(sheet, tab)
    key_col = column_letter(header.index(KEY))
    ids = sheet.get_values(f"{_q(tab)}!{key_col}2:{key_col}")
    rownum = next((i + 2 for i, r in enumerate(ids) if r and r[0].strip() == str(rec[KEY]).strip()), None)
    if rownum is None:
        sheet.append_row(tab, ["" if h in USER_OWNED - {STATUS} else _cell(rec.get(h)) for h in header])
        return "added"
    cur = (sheet.get_values(f"{_q(tab)}!{rownum}:{rownum}") or [[]])[0]
    cur += [""] * (len(header) - len(cur))
    cells: dict[str, Any] = {}
    for i, h in enumerate(header):
        v = rec.get(h)
        if h not in rec or v in (None, "") or (h in USER_OWNED and (h != STATUS or str(cur[i]).strip())):
            continue
        if not _same(cur[i], v):
            cells[f"{_q(tab)}!{column_letter(i)}{rownum}"] = v
    sheet.update_cells(cells)
    return "updated" if cells else "unchanged"


def _cell(v: Any) -> Any:
    return "" if v is None else v


def set_status(sheet: Sheet, tab: str, job_id: str, status: str) -> str:
    """Write the Status cell of the row for `job_id` and no other cell (the reply watch, after a `y`). -> `updated` | `unchanged`. A tab without a
    Job ID or Status column, or a job with no row, is a TrackerError: nothing is appended or created here (`careerops tracker sync` adds rows)."""
    header = [str(h).strip() for h in ((sheet.get_values(f"{_q(tab)}!1:1") or [[]])[0])]
    missing = [h for h in (KEY, STATUS) if h not in header]
    if missing:
        raise TrackerError(f"the '{tab}' tab has no {' or '.join(missing)} column")
    key_col, status_col = column_letter(header.index(KEY)), column_letter(header.index(STATUS))
    ids = sheet.get_values(f"{_q(tab)}!{key_col}2:{key_col}")
    rownum = next((i + 2 for i, r in enumerate(ids) if r and r[0].strip() == str(job_id).strip()), None)
    if rownum is None:
        raise TrackerError(f"{job_id} has no row in the Sheet: run `careerops tracker sync --id {job_id}` first")
    cell = f"{_q(tab)}!{status_col}{rownum}"
    cur = (sheet.get_values(cell) or [[""]])[0]
    if (cur[0] if cur else "").strip() == status:
        return "unchanged"
    sheet.update_cells({cell: status})
    return "updated"


def sheet_job_ids(settings, *, sheet: Sheet | None = None, tab: str | None = None) -> set[str]:
    """The Job IDs already in the tracker Sheet (its `Job ID` column), for the auto-apply duplicate gate. Reads only. Raises TrackerError for a
    missing sheet id, no sign-in, or a Google refusal."""
    from .discovery.gmail import GmailAuthError
    from .googleapi import GoogleError

    try:
        if sheet is None:
            sheet, tab = client(settings)
        tab = tab or settings_for(settings)[1]
        header = [str(h).strip() for h in ((sheet.get_values(f"{_q(tab)}!1:1") or [[]])[0])]
        if KEY not in header:
            return set()
        col = column_letter(header.index(KEY))
        return {str(r[0]).strip() for r in (sheet.get_values(f"{_q(tab)}!{col}2:{col}") or []) if r and str(r[0]).strip()}
    except (GmailAuthError, GoogleError) as e:
        raise TrackerError(str(e)) from None


def sync(db: DB, settings, job_ids: list[str] | None = None, *, sheet: Sheet | None = None, tab: str | None = None,
         say=print) -> dict[str, str]:
    """Upsert the given applied jobs (default: every job the database holds as applied). -> {job id: added | updated | unchanged}.
    Raises TrackerError for a missing sheet id, no sign-in, or a Google refusal."""
    from .discovery.gmail import GmailAuthError
    from .googleapi import GoogleError

    rows = [db.job(j) for j in job_ids] if job_ids else db.jobs("applied")
    rows = [r for r in rows if r is not None and r["applied_at"]]
    fb = load_yaml(settings.path("fact_bank"))
    try:
        if sheet is None:
            sheet, tab = client(settings)
        tab = tab or settings_for(settings)[1]
        result: dict[str, str] = {}
        for r in rows:
            result[r["id"]] = upsert(sheet, tab, record(db, settings, r, fb))
            db.log(r["id"], "tracker", result[r["id"]])
        return result
    except (GmailAuthError, GoogleError) as e:
        raise TrackerError(str(e)) from None
