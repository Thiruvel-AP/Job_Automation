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

from ..db import DB, now
from ..tailoring.faithfulness import deny_hits
from ..tailoring.queue import order
from ..tailoring.render import page2_fill, page_count, pdf_text, render_cv
from ..tailoring.scorer import Keyword, score
from ..tailoring.tailor import FALLBACK_MARK, regenerate_letter as _regenerate_letter

CONFIRM_FALLBACK = "send fallback"


class FallbackLetterError(RuntimeError):
    """Approving a job whose cover letter is the deterministic fallback needs a deliberate `send fallback`."""


def letter_status(rep: dict) -> str:
    """'ok' or 'fallback'. Reports written before A1 have no status: judge them by their recorded problems."""
    letter = rep.get("letter") or {}
    if letter.get("status"):
        return letter["status"]
    return "fallback" if FALLBACK_MARK in (letter.get("problems") or []) else "ok"


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
        (f"T{rep['tier']} · {rep['headline']}" if rep.get("tier") else "Tier: out of target families"),
        f"{job['url']}",
        f"Sponsor: {job['sponsor_name'] or 'NOT MATCHED'} ({(job['sponsor_score'] or 0):.2f})",
        f"ATS literal {f['score']:.0%} | stemmed {f['score_stemmed']:.0%} | honest ceiling {rep['ceiling']:.0%} "
        f"| band: {f['band']} | pages {f['pages']}"
        + (f" (page 2 {f['page2_fill']:.0%} full)" if f.get("page2_fill") else "") + f" | attempts {len(rep['attempts'])}",
        f"Letter score {rep['letter']['score']:.0%}" + (f" | letter issues: {rep['letter']['problems']}" if rep['letter']['problems'] else ""),
    ]
    failures = (rep["letter"].get("retry") or {}).get("failures") or []
    for fail in failures:
        lines.append(f"Letter draft {fail['attempt']} rejected: " + "; ".join(fail["problems"])[:400])
    removed = (rep["letter"].get("retry") or {}).get("removed") or []
    if removed:
        lines.append("Letter removed: " + ", ".join(removed) + " (unsupported qualifiers deleted; the full check then passed)")
    if failures and letter_status(rep) == "ok":
        lines.append(f"Letter accepted on draft {len(failures) + 1}")
    if letter_status(rep) == "fallback":
        lines.append("LETTER IS A FALLBACK — regenerate (l) before approving")
    if flags:
        lines.append("FLAGS: " + " | ".join(flags))
    if rep.get("needs_result"):
        lines.append(f"Needs a result (yours to supply in fact_bank.yaml, never invented): "
                     f"{', '.join(n['id'] for n in rep['needs_result'])}")
    if rep.get("context_terms"):
        lines.append("Context terms (team / product-area / org names; not scored, not in the ceiling): "
                     + ", ".join(c["term"] for c in rep["context_terms"]))
    if rep.get("possible_evidence"):
        lines.append("Possible evidence — you decide (nothing is claimed; add the word to the fact bank if it is true):")
        for term, via in rep["possible_evidence"].items():
            lines.append(f"  {term} <- " + "; ".join(f"{v['via']} ({', '.join(v['where'][:2])})" for v in via))
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
def approve(db: DB, job, rep: dict, prefs: Path, confirm: str | None = None) -> None:
    """A fallback letter is approved only with confirm == 'send fallback' (logged as an event)."""
    if letter_status(rep) == "fallback":
        if (confirm or "").strip() != CONFIRM_FALLBACK:
            raise FallbackLetterError(f"the cover letter is a fallback; regenerate it, or type '{CONFIRM_FALLBACK}'")
        db.log(job["id"], "review", {"approved_fallback_letter": True, "problems": rep["letter"].get("problems", [])})
    _log(prefs, {"kind": "approve", "job_id": job["id"], "keywords": rep["keywords"],
                 "selection": rep["attempts"][rep["final"]["chosen_attempt"] - 1]["selection"],
                 "score": rep["final"]["score"]})
    db.update_job(job["id"], status="approved")
    db.log(job["id"], "review", "approved")


def regenerate_letter(db: DB, job, rep: dict, fb: dict, llm, settings) -> dict:
    """Re-draft the cover letter (review's 'l'); updates report.json, the letter score and the event log."""
    rep = _regenerate_letter(job, fb, llm, settings, rep)
    db.update_job(job["id"], letter_score=rep["letter"]["score"])
    db.log(job["id"], "review", {"letter_regenerated": rep["letter"]["status"]})
    return rep


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
    pdf = render_cv(ctx, Path(rep["out_dir"]), Path(rep["cv_pdf"]).stem,     # keep the CV's final file name and layout
                    bool(rep.get("one_page")))
    s = score(pdf_text(pdf), kws)
    pages = page_count(pdf)
    rep["final"].update({"score": s.literal, "score_stemmed": s.stemmed, "pages": pages,
                         "page2_fill": page2_fill(pdf, pages), "edited": True})
    rep["cv_pages"], rep["page2_fill"] = pages, rep["final"]["page2_fill"]
    (Path(rep["out_dir"]) / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    db.update_job(job["id"], score=s.literal, score_stemmed=s.stemmed)
    return warnings


# ---------------------------------------------------------------- interactive loop
MENU = {"a": "[a]pprove", "e": "[e]dit", "r": "[r]eject", "l": "[l]etter regenerate", "o": "[o]pen PDFs", "s": "[s]kip",
        "q": "[q]uit"}


def review_job(db: DB, job, settings, fb: dict, make_llm=None, ask=None, allow: str = "aerlosq") -> str:
    """Review ONE job at the prompt. Returns 'approved' | 'rejected' | 'skipped' | 'quit'. `allow` names the choices
    offered (a approve, e edit, r reject, l regenerate the letter, o open the PDFs, s skip, q quit); `ask` answers the
    prompts (default: input). A skip changes nothing in the database: what a skip means is the caller's business."""
    ask = ask or input
    prefs = settings.path("preferences")
    rep = load_report(job)
    print(summary(job, rep))
    menu = "  ".join(MENU[c] for c in "aerlosq" if c in allow)
    while True:
        c = ask(f"{menu} > ").strip().lower()
        if c not in allow:
            continue
        if c == "o":
            open_file(rep["cv_pdf"]); open_file(rep["letter_pdf"])
        elif c == "a":
            confirm = None
            if letter_status(rep) == "fallback":
                confirm = ask(f"Letter is a FALLBACK. Type '{CONFIRM_FALLBACK}' to approve anyway: ")
            try:
                approve(db, job, rep, prefs, confirm=confirm)
            except FallbackLetterError as e:
                print(f"  NOT approved: {e}")
                continue
            return "approved"
        elif c == "l":
            if make_llm is None:
                print("  no LLM available for this run")
                continue
            try:
                rep = regenerate_letter(db, job, rep, fb, make_llm(), settings)
            except Exception as e:  # noqa: BLE001  (provider errors, xelatex errors: report, keep the old letter)
                print(f"  letter NOT regenerated: {e}")
                continue
            job = db.job(job["id"])
            print(summary(job, rep))
        elif c == "r":
            reject(db, job, rep, prefs, ask("reason: ").strip() or "unspecified")
            return "rejected"
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
            return "skipped"
        elif c == "q":
            return "quit"


def run(db: DB, settings, fb: dict, limit: int | None = None, make_llm=None) -> None:
    queue = order(db.jobs("needs_review"), settings, tailored=True)[:limit]
    print(f"{len(queue)} job(s) waiting for review.")
    for job in queue:
        if review_job(db, job, settings, fb, make_llm) == "quit":
            return
