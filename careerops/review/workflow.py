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
from urllib.parse import urlparse

import yaml

from ..db import DB, now
from ..tailoring.faithfulness import deny_hits
from ..tailoring.queue import order
from ..tailoring.render import page2_fill, page_count, pdf_text, render_cv
from ..tailoring.scorer import Keyword, score
from ..tailoring.tailor import FALLBACK_MARK, regenerate_letter as _regenerate_letter
from . import judge

CONFIRM_FALLBACK = "send fallback"
CONFIRM_NO_LETTER = "no letter"
NO_LETTER_BANNER = "NO LETTER — write your own or apply without"
JOB_BOARDS = ("cvlibrary", "reed", "totaljobs", "indeed", "linkedin")          # a host label or source name, hyphens ignored
BOARD_BANNER = "agency/job board — sponsor check may refer to the agency, apply by hand on the site"


def job_board(job) -> str | None:
    """The job board / agency site this job came from (cv-library, reed, ...), judged by its URLs and its source name; None
    when it is not one. The sponsor register match on such a job can be the agency's, not the employer's."""
    keys = job.keys()
    names = [str(job["source"] or "")] if "source" in keys else []
    for k in ("url", "apply_url"):
        host = (urlparse(str(job[k] or "")).hostname or "") if k in keys else ""
        names += host.split(".")
    for n in names:
        n = n.lower().replace("-", "").replace("_", "")
        if n in JOB_BOARDS:
            return n
    return None


class FallbackLetterError(RuntimeError):
    """Approving a job whose cover letter is the deterministic fallback needs a deliberate `send fallback`."""


class NoLetterError(RuntimeError):
    """Approving a job that has no cover letter needs a deliberate `no letter`."""


def letter_status(rep: dict) -> str:
    """'ok', 'fallback', 'none' (no letter was produced: letter.on_failure) or 'off' (letter.mode: off, none was asked for).
    Reports written before A1 have no status: judge them by their recorded problems."""
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


def usage_lines(rep: dict) -> list[str]:
    """Tokens per model for this job: "groq/openai/gpt-oss-20b 6,911 tokens in 9 calls (extract_keywords, ...)". Reports
    written before per-model accounting have the calls but not the totals: they are worked out from the calls."""
    from ..tailoring.tailor import usage_by_model

    use = rep.get("llm_usage") or {}
    by_model = use.get("by_model") or usage_by_model(use.get("calls") or [])
    return [f"  {name}: {m['total_tokens']:,} tokens (prompt {m['prompt_tokens']:,}, completion {m['completion_tokens']:,}"
            f", reasoning {m['reasoning_tokens']:,}) in {m['calls']} call(s): {', '.join(m['tasks'])}"
            for name, m in by_model.items()]


def summary(job, rep: dict) -> str:
    f = rep["final"]
    flags = json.loads(job["flags"] or "[]")
    last = rep["attempts"][-1]
    letter = rep["letter"]
    lines = [
        "=" * 78,
        f"{job['company']} — {job['title']}  [{job['location'] or '?'}]",
        (f"T{rep['tier']} · {rep['headline']}" if rep.get("tier") else "Tier: out of target families"),
        f"{job['url']}",
        f"Sponsor: {job['sponsor_name'] or 'NOT MATCHED'} ({(job['sponsor_score'] or 0):.2f})",
        f"ATS literal {f['score']:.0%} | stemmed {f['score_stemmed']:.0%} | honest ceiling {rep['ceiling']:.0%} "
        f"| band: {f['band']} | pages {f['pages']}"
        + (f" (page 2 {f['page2_fill']:.0%} full)" if f.get("page2_fill") else "") + f" | attempts {len(rep['attempts'])}",
        (f"Letter score {letter['score']:.0%}" if letter.get("score") is not None
         else "Letter: off (letter.mode: off; write your own, or apply without)" if letter.get("status") == "off"
         else "Letter: none")
        + (f" | letter issues: {letter['problems']}" if letter['problems'] else ""),
    ]
    if letter.get("evidence"):
        lines.append("Letter evidence: " + ", ".join(e["id"] for e in letter["evidence"]))
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
    if letter_status(rep) == "none":
        lines.append(NO_LETTER_BANNER + f"  ({letter.get('reason') or 'no letter'}; regenerate (l) to try again)")
    if job_board(job):
        lines.append("BOARD: " + BOARD_BANNER)
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
    lines += [f"Profile: {rep['context']['profile']}", f"CV: {rep['cv_pdf']}", f"Letter: {rep['letter_pdf'] or '(none)'}"]
    used = usage_lines(rep)
    if used:
        lines += ["LLM tokens by model:", *used]
    return "\n".join(lines)


EXPLORER_FALLBACK = "/mnt/c/Windows/explorer.exe"      # where explorer.exe is when WSL's interop leaves it off PATH


def in_wsl() -> bool:
    """True inside WSL (Windows Subsystem for Linux), where a PDF is best opened by Windows' own viewer."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def wsl_open(p: str) -> bool:
    """Open `p` with Windows' explorer.exe, which hands a .pdf to the default PDF app. `wslpath -w` turns the Linux path
    into the Windows one (D:\\... for /mnt/d/..., \\\\wsl.localhost\\... for a path inside the distro). False when not in WSL
    or either tool is missing, so the caller can try something else. explorer.exe exits 1 even on success: its code is
    not read."""
    explorer = shutil.which("explorer.exe") or (EXPLORER_FALLBACK if Path(EXPLORER_FALLBACK).exists() else None)
    if not (in_wsl() and explorer and shutil.which("wslpath")):
        return False
    try:
        win = subprocess.run(["wslpath", "-w", p], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not win:
        return False
    subprocess.Popen([explorer, win])
    return True


def open_file(p: str) -> None:
    if wsl_open(p):
        return
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
    """A fallback letter is approved only with confirm == 'send fallback', a job with no letter only with confirm ==
    'no letter' (each logged as an event)."""
    if letter_status(rep) == "none":
        if (confirm or "").strip() != CONFIRM_NO_LETTER:
            raise NoLetterError(f"there is no cover letter; regenerate it, or type '{CONFIRM_NO_LETTER}' to approve "
                                "without one")
        db.log(job["id"], "review", {"approved_without_letter": True, "problems": rep["letter"].get("problems", [])})
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


def _ask_judge(settings, job, rep: dict, fb: dict, make_llm, mode: str) -> judge.Verdict | None:
    """The judge's verdict on this version of the job, printed. None = the judge was not asked (mode off, or no LLM for this
    run); a Verdict with `error` = it was asked and could not answer."""
    if mode == "off":
        return None
    if make_llm is None:
        print("  Judge: not run (no LLM for this run)")
        return None
    try:
        llm = make_llm()
    except Exception as e:  # noqa: BLE001 — a missing key must not stop a review
        print(f"  Judge: not run ({e})")
        return None
    v = judge.run_judge(llm, settings, job, rep, fb)
    print(f"  {v.line()} [{mode}]")
    if v.input_line():
        print(f"  {v.input_line()}")
    for issue in v.issues:
        print(f"    - {issue}")
    return v


def review_job(db: DB, job, settings, fb: dict, make_llm=None, ask=None, allow: str = "aerlosq",
               skip_means_no: bool = False) -> str:
    """Review ONE job at the prompt. Returns 'approved' | 'rejected' | 'skipped' | 'quit'. `allow` names the choices
    offered (a approve, e edit, r reject, l regenerate the letter, o open the PDFs, s skip, q quit); `ask` answers the
    prompts (default: input). A skip changes nothing in the database: what a skip means is the caller's business; with
    `skip_means_no` (live: a skipped job is finished) the judge log records it as a rejection.

    review.judge (judge.py): the verdict is printed under the summary and logged beside your decision. In `gate` mode a
    judge yes approves the job here without the prompt, unless it has FLAGS or its letter is a fallback or was lost to
    letter.on_failure (those keep their deliberate confirmation); raises judge.GateRefused while the log has not earned gate."""
    ask = ask or input
    prefs = settings.path("preferences")
    mode = judge.judge_mode(settings)
    if mode == "gate":
        judge.require_gate(settings)
    rep = load_report(job)
    print(summary(job, rep))
    verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)
    if (mode == "gate" and verdict is not None and verdict.yes and not json.loads(job["flags"] or "[]")
            and letter_status(rep) in ("ok", "off")):
        approve(db, job, rep, prefs)
        judge.log_decision(settings, job, rep, verdict, "auto", "gate: judge yes, no flags")
        db.log(job["id"], "review", {"auto_approved_by_judge": verdict.model, "score": verdict.score})
        print("  Judge gate: approved without the prompt (judge yes, no flags). Whether to open the form is still yours.")
        return "approved"
    menu = "  ".join(MENU[c] for c in "aerlosq" if c in allow)
    while True:
        c = ask(f"{menu} > ").strip().lower()
        if c not in allow:
            continue
        if c == "o":
            open_file(rep["cv_pdf"])
            if rep.get("letter_pdf"):
                open_file(rep["letter_pdf"])
        elif c == "a":
            confirm = None
            if letter_status(rep) == "fallback":
                confirm = ask(f"Letter is a FALLBACK. Type '{CONFIRM_FALLBACK}' to approve anyway: ")
            elif letter_status(rep) == "none":
                confirm = ask(f"There is NO LETTER. Type '{CONFIRM_NO_LETTER}' to approve without one: ")
            try:
                approve(db, job, rep, prefs, confirm=confirm)
            except (FallbackLetterError, NoLetterError) as e:
                print(f"  NOT approved: {e}")
                continue
            judge.log_decision(settings, job, rep, verdict, "approve")
            return "approved"
        elif c == "l":
            if letter_status(rep) == "off":
                print("  letter.mode is off: set letter.mode: draft in config/settings.yaml to have a letter drafted")
                continue
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
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the letter changed: judge this version
        elif c == "r":
            reason = ask("reason: ").strip() or "unspecified"
            reject(db, job, rep, prefs, reason)
            judge.log_decision(settings, job, rep, verdict, "reject", reason)
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
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the CV changed: judge this version
        elif c == "s":
            if skip_means_no:
                judge.log_decision(settings, job, rep, verdict, "reject", "skipped in live review")
            return "skipped"
        elif c == "q":
            return "quit"


def run(db: DB, settings, fb: dict, limit: int | None = None, make_llm=None) -> None:
    queue = order(db.jobs("needs_review"), settings, tailored=True)[:limit]
    print(f"{len(queue)} job(s) waiting for review.")
    for job in queue:
        if review_job(db, job, settings, fb, make_llm) == "quit":
            return
