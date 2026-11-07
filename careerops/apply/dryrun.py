"""The record of a filled-but-not-submitted form (`careerops live ... --record`, v2.4.1), kept under data/dryruns/<date>_<company>_<role>_<ad id>/:

  fields.json / fields.md   one row per field: label, type, required, value (120 characters), source (answers:<id> | freetext | letter | cv | applicant |
                            profile | job), and its status (FILLED / VERIFIED / LOST / EMPTY / NEEDS USER, apply/requiredgate.py)
  form_full.png             a full-page screenshot of the form after filling
  cv.pdf, letter.pdf, letter.txt   copies (never moves) of the files the form was given
  motivation.txt            the drafted "what motivated you ..." answer, its word count and the guard results
  run.json                  ATS strict / claimable scores, the hook, the required fields still empty, v3.3 the ATS structure check
  report.md                 v3.3: the same as one page to read: the ATS numbers, the `ATS structure ...` line with its issues, lints and signals
                            (review/screeners.py; rep["ats_check"] when live put it there, else the PDF-only structural check), the empty required fields

data/dryruns/ holds personal data and is git-ignored. Nothing here clicks, submits, or writes outside that folder.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from . import requiredgate

VALUE_CHARS = 120


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_")


def ad_id(job: Mapping[str, Any]) -> str:
    m = re.search(r"/(\d{5,})(?:[/?#]|$)", str(job.get("url") or job.get("apply_url") or ""))
    return m.group(1) if m else slug(str(job.get("id") or "job").split(":")[-1])


def folder_for(rep: Mapping[str, Any], job: Mapping[str, Any], today: date | None = None) -> Path:
    """data/dryruns/<date>_<company>_<role>_<ad id>, a sibling of the outputs folder (data/outputs/<job>/ -> data/dryruns/...)."""
    root = Path(str(rep["out_dir"])).parent.parent / "dryruns"
    return root / f"{(today or date.today()).isoformat()}_{slug(job.get('company'))}_{slug(job.get('title'))}_{ad_id(job)}"


def _short(v: str) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= VALUE_CHARS else v[: VALUE_CHARS - 1] + "…"


def fields_md(rows: list[requiredgate.Row]) -> str:
    out = ["# Fields of the filled form", "", "| # | label | type | required | value | source | status |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        cell = lambda t: str(t).replace("|", "\\|").replace("\n", " ")        # noqa: E731
        out.append(f"| {i} | {cell(r.label[:120])} | {r.kind} | {'yes' if r.required else 'no'} | {cell(_short(r.value))} | {cell(r.source)} | "
                   f"{r.status}{(' — ' + cell(r.reason[:120])) if r.reason else ''} |")
    return "\n".join(out) + "\n"


def record(folder: Path, page, job: Mapping[str, Any], rep: Mapping[str, Any], rows: list[requiredgate.Row], *, cv: str | None, letter: str | None,
           letter_text: str | None, freetext=None, out=print) -> Path:
    """Write the record into `folder` (created). Every file is a copy; a missing source is simply not copied."""
    folder.mkdir(parents=True, exist_ok=True)
    data = [{**r.as_dict(), "value": _short(r.value)} for r in rows]
    (folder / "fields.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "fields.md").write_text(fields_md(rows), encoding="utf-8")
    try:
        page.screenshot(path=str(folder / "form_full.png"), full_page=True)
    except Exception as e:  # noqa: BLE001  (a closed page: the rest of the record is still written)
        out(f"  dry-run record: no screenshot ({str(e)[:80]})")
    for src, name in ((cv, "cv.pdf"), (letter, "letter.pdf")):
        if src and Path(src).is_file():
            (folder / name).write_bytes(Path(src).read_bytes())
    if letter and (Path(letter).with_name("letter.txt")).is_file():
        (folder / "letter.txt").write_bytes(Path(letter).with_name("letter.txt").read_bytes())
    elif letter_text:
        (folder / "letter.txt").write_text(letter_text, encoding="utf-8")
    log = list(getattr(freetext, "motivation_log", []) or [])
    if log:
        (folder / "motivation.txt").write_text("\n\n".join(
            f"QUESTION: {m['question']}\nWORDS: {m['words']}\nGUARD: {m['guard']}\nSOURCES: {', '.join(m.get('sources') or [])}\n"
            f"REUSE OVERLAP (5-word shingles, limit 0.30): {m.get('overlap', 'n/a')}\n\n{m['text'] or '(no draft)'}" for m in log) + "\n", encoding="utf-8")
    ats = rep.get("ats") or {}
    hook = rep.get("hook") or {}
    check = ats_check_of(rep)
    (folder / "run.json").write_text(json.dumps({
        "job": {"id": job.get("id"), "company": job.get("company"), "title": job.get("title"), "url": job.get("url")},
        "ats": {"strict": ats.get("strict"), "ceiling": ats.get("ceiling"), "claimable_only": ats.get("ceiling")},
        "hook": {"id": hook.get("hook_id") or hook.get("entry_id"), "headline": hook.get("headline"), "proof": hook.get("proof_bullet_id")},
        "required_blocking": [r.as_dict() for r in requiredgate.blocking([r for r in rows if r.required])],
        "letter_status": (rep.get("letter") or {}).get("status"), "motivation": log, "ats_structure": check}, indent=2, ensure_ascii=False,
        default=str), encoding="utf-8")
    (folder / "report.md").write_text(report_md(job, rep, rows, check), encoding="utf-8")
    out(f"  dry-run record: {folder}")
    return folder


def ats_check_of(rep: Mapping[str, Any]) -> dict:
    """The ATS check live attached to the job (rep["ats_check"], review/screeners.py), else the structural check of the PDFs alone (no fact bank,
    no ad: no role lints, no signals). A check that cannot run says so."""
    if isinstance(rep.get("ats_check"), Mapping):
        return dict(rep["ats_check"])
    from .. import ats_structure

    st = ats_structure.for_report(rep)
    if st.cv is None:
        return {"line": f"ATS structure: not checked ({st.error or 'no CV'})", "lines": [], "ok": False}
    lines = [st.cv.line()] + [f"  {i.line()}" for i in st.cv.issues] + [f"  {l.line()}" for l in st.lints]
    return {"line": st.cv.line(), "lines": lines, "ok": st.ok, "cv": st.cv.as_dict()}


def report_md(job: Mapping[str, Any], rep: Mapping[str, Any], rows: list[requiredgate.Row], check: Mapping[str, Any]) -> str:
    ats = rep.get("ats") or {}
    nums = (f"ATS strict CV {ats['strict']:.1f}% | claimable ceiling {ats['ceiling']:.1f}%" if ats.get("strict") is not None and ats.get("ceiling") is not None
            else "ATS: not scored")
    blocking = requiredgate.blocking([r for r in rows if r.required])
    out = [f"# Dry run: {job.get('company')} — {job.get('title')}", "", f"{job.get('url') or ''}", "", "## ATS", "", nums, "",
           str(check.get("line") or ""), ""]
    detail = [l for l in (check.get("lines") or [])[1:]]
    if detail:
        out += ["```", *detail, "```", ""]
    out += ["## Required fields still empty", ""] + ([f"- {r.label}" for r in blocking] or ["none"]) + [""]
    out += [f"Letter: {(rep.get('letter') or {}).get('status') or 'none'}", ""]
    return "\n".join(out)
