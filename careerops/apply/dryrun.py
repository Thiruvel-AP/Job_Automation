"""The record of a filled-but-not-submitted form (`careerops live ... --record`, v2.4.1), kept under data/dryruns/<date>_<company>_<role>_<ad id>/:

  fields.json / fields.md   one row per field: label, type, required, value (120 characters), source (answers:<id> | freetext | letter | cv | applicant |
                            profile | job), and its status (FILLED / VERIFIED / LOST / EMPTY / NEEDS USER, apply/requiredgate.py)
  form_full.png             a full-page screenshot of the form after filling
  cv.pdf, letter.pdf, letter.txt   copies (never moves) of the files the form was given
  motivation.txt            the drafted "what motivated you ..." answer, its word count and the guard results
  run.json                  ATS strict / claimable scores, the hook, the required fields still empty: what report.md is written from

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
    (folder / "run.json").write_text(json.dumps({
        "job": {"id": job.get("id"), "company": job.get("company"), "title": job.get("title"), "url": job.get("url")},
        "ats": {"strict": ats.get("strict"), "ceiling": ats.get("ceiling"), "claimable_only": ats.get("ceiling")},
        "hook": {"id": hook.get("hook_id") or hook.get("entry_id"), "headline": hook.get("headline"), "proof": hook.get("proof_bullet_id")},
        "required_blocking": [r.as_dict() for r in requiredgate.blocking([r for r in rows if r.required])],
        "letter_status": (rep.get("letter") or {}).get("status"), "motivation": log}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    out(f"  dry-run record: {folder}")
    return folder
