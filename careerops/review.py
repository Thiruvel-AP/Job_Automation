"""Human-in-the-loop review queue. Every decision is logged as preference data.

preferences.jsonl rows:
  {"kind": "approve", "job_id", "keywords", "selection", ...}
  {"kind": "reject",  "job_id", "reason", "proposal"}
  {"kind": "edit_pair", "job_id", "bullet_id", "source", "keywords", "chosen": <your text>, "rejected": <model text>}
edit_pair rows are DPO-ready (prompt = source bullet + JD keywords; chosen/rejected = texts).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

import yaml

from .db import DB, now
from .faithfulness import deny_hits
from .render import page_count, pdf_text, render_cv
from .scorer import Keyword, score


def _log(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now(), **row}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_report(job) -> dict:
    return json.loads((Path(job["out_dir"]) / "report.json").read_text(encoding="utf-8"))


def summary(job, rep: dict) -> str:
    f = rep["final"]
    flags = json.loads(job["flags"] or "[]")
    last = rep["attempts"][-1]
    lines = [
        "=" * 78,
        f"{job['company']} — {job['title']}  [{job['location'] or '?'}]",
        f"{job['url']}",
        f"Sponsor: {job['sponsor_name'] or 'NOT MATCHED'} ({(job['sponsor_score'] or 0):.2f})",
        f"ATS literal {f['score']:.0%} | stemmed {f['score_stemmed']:.0%} | honest ceiling {rep['ceiling']:.0%} "
        f"| band: {f['band']} | pages {f['pages']} | attempts {len(rep['attempts'])}",
        f"Letter score {rep['letter']['score']:.0%}" + (f" | letter issues: {rep['letter']['problems']}" if rep['letter']['problems'] else ""),
    ]
    if flags:
        lines.append("FLAGS: " + " | ".join(flags))
    if rep["genuine_gaps"]:
        lines.append("Genuine gaps (no evidence, left out): " + ", ".join(rep["genuine_gaps"][:25]))
    if f["wording_misses"]:
        lines.append("Wording-only misses (substance present): " + ", ".join(f["wording_misses"][:20]))
    viol = [v for a in rep["attempts"] for v in a["violations"]]
    if viol:
        lines.append(f"Guard blocked {len(viol)} model claims, e.g.: " + "; ".join(viol[:4]))
    warns = [w for a in rep["attempts"] for w in a["warnings"]]
    if warns:
        lines.append("Warnings: " + "; ".join(warns[:4]))
    lines += [f"Profile: {rep['context']['profile']}", f"CV: {rep['cv_pdf']}", f"Letter: {rep['letter_pdf']}"]
    return "\n".join(lines)


def open_file(p: str) -> None:
    if shutil.which("wslview"):
        subprocess.Popen(["wslview", p])
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", p])
    elif platform.system() == "Windows":
        os.startfile(p)  # type: ignore[attr-defined]
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", p])
    else:
        print(f"  open manually: {p}")


# ---------------------------------------------------------------- decisions (also used by tests)
def approve(db: DB, job, rep: dict, prefs: Path) -> None:
    _log(prefs, {"kind": "approve", "job_id": job["id"], "keywords": rep["keywords"],
                 "selection": rep["attempts"][rep["final"]["chosen_attempt"] - 1]["selection"],
                 "score": rep["final"]["score"]})
    db.update_job(job["id"], status="approved")
    db.log(job["id"], "review", "approved")


def reject(db: DB, job, rep: dict, prefs: Path, reason: str) -> None:
    _log(prefs, {"kind": "reject", "job_id": job["id"], "reason": reason,
                 "proposal": rep["attempts"][-1]["proposal"], "keywords": rep["keywords"]})
    db.update_job(job["id"], status="rejected", reason=reason)
    db.log(job["id"], "review", {"rejected": reason})


def edit_file_for(rep: dict) -> dict:
    ctx = rep["context"]
    return {"profile": ctx["profile"],
            "bullets": {bid: txt for sec in ("experience", "projects") for e in ctx[sec]
                        for bid, txt in zip(e["bullet_ids"], e["bullets"])}}


def apply_edits(db: DB, job, rep: dict, edited: dict, fb: dict, prefs: Path) -> list[str]:
    """Write your edits into the CV, re-render, rescore, and log (chosen=yours, rejected=model) pairs."""
    ctx, kws = rep["context"], [Keyword(**k) for k in rep["keywords"]]
    src_by_id = {b["id"]: b for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    warnings = []
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for i, bid in enumerate(e["bullet_ids"]):
                new = (edited.get("bullets") or {}).get(bid)
                if new and new.strip() != e["bullets"][i]:
                    _log(prefs, {"kind": "edit_pair", "job_id": job["id"], "bullet_id": bid,
                                 "source": src_by_id.get(bid, {}).get("text"), "keywords": rep["keywords"],
                                 "chosen": new.strip(), "rejected": e["bullets"][i]})
                    warnings += [f"{bid}: deny-listed '{d}'" for d in deny_hits(new, fb.get("deny_terms", []))]
                    e["bullets"][i] = new.strip()
    if edited.get("profile") and edited["profile"].strip() != ctx["profile"]:
        _log(prefs, {"kind": "edit_pair", "job_id": job["id"], "bullet_id": "profile", "source": fb["profile_facts"],
                     "keywords": rep["keywords"], "chosen": edited["profile"].strip(), "rejected": ctx["profile"]})
        ctx["profile"] = edited["profile"].strip()
    pdf = render_cv(ctx, Path(rep["out_dir"]))
    s = score(pdf_text(pdf), kws)
    rep["final"].update({"score": s.literal, "score_stemmed": s.stemmed, "pages": page_count(pdf), "edited": True})
    (Path(rep["out_dir"]) / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    db.update_job(job["id"], score=s.literal, score_stemmed=s.stemmed)
    return warnings


# ---------------------------------------------------------------- interactive loop
def run(db: DB, settings, fb: dict, limit: int | None = None) -> None:
    prefs = settings.path("preferences")
    queue = db.jobs("needs_review", limit=limit)
    print(f"{len(queue)} job(s) waiting for review.")
    for job in queue:
        rep = load_report(job)
        print(summary(job, rep))
        while True:
            c = input("[a]pprove  [e]dit  [r]eject  [o]pen PDFs  [s]kip  [q]uit > ").strip().lower()
            if c == "o":
                open_file(rep["cv_pdf"]); open_file(rep["letter_pdf"])
            elif c == "a":
                approve(db, job, rep, prefs); break
            elif c == "r":
                reject(db, job, rep, prefs, input("reason: ").strip() or "unspecified"); break
            elif c == "e":
                path = Path(rep["out_dir"]) / "edit.yaml"
                path.write_text(yaml.safe_dump(edit_file_for(rep), sort_keys=False, allow_unicode=True, width=120),
                                encoding="utf-8")
                editor = os.environ.get("EDITOR", "notepad" if platform.system() == "Windows" else "nano")
                subprocess.call([editor, str(path)])
                warns = apply_edits(db, job, rep, yaml.safe_load(path.read_text(encoding="utf-8")), fb, prefs)
                for w in warns:
                    print("  WARNING:", w)
                print(f"  re-scored: {rep['final']['score']:.0%} literal, {rep['final']['pages']} page(s)")
            elif c == "s":
                break
            elif c == "q":
                return
