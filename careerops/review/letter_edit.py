"""Review's two letter keys for `letter.mode: company`.

`l`  asks "What should change? (enter = just retry)"; what the person types is saved (paths.letter_feedback) and goes to the next draft, word
     for word, with the previous draft and the guard rejections rewritten as instructions (tailoring/letter_feedback.py). At most three drafts.
`e`  opens the letter text in $EDITOR (nano unless EDITOR says otherwise). On save the same guards run (company_letter.edit_problems). A letter
     that passes is used. One that fails shows its reasons and is used only if `use anyway` is typed in full; anything else edits again, and
     `discard` keeps the old letter. A text that is not five paragraphs can never be installed.

Nothing here writes the fact bank, the applicant file or the answers file; the edited text is the person's own.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from ..tailoring import company_letter as CL
from ..tailoring import letter_feedback as LF
from ..tailoring import tailor

CONFIRM_ANYWAY = "use anyway"
FEEDBACK_PROMPT = "What should change? (enter = just retry) "
FILE_NAME = "edit_letter.txt"


def ask_feedback(ask: Callable[[str], str], settings, job_id: str) -> str:
    """The `l` question. A non-empty answer is saved in paths.letter_feedback and returned word for word; enter returns ''."""
    text = ask(FEEDBACK_PROMPT).strip()
    if text:
        LF.save(settings, job_id, text)
    return text


def editor_command() -> list[str]:
    """$EDITOR (with its arguments, e.g. `code -w`), nano when it is not set."""
    return shlex.split(os.environ.get("EDITOR") or "nano") or ["nano"]


def run_editor(path: Path) -> None:
    subprocess.call([*editor_command(), str(path)])


def starting_text(rep: dict) -> list[str]:
    """The five paragraphs the editor opens with: the letter, else the last rejected draft between the opening and closing, else only those two."""
    old = rep.get("letter") or {}
    paras = list(old.get("paragraphs") or [])
    if len(paras) == 5:
        return paras
    middle = list(old.get("draft_body") or [])
    middle = middle if len(middle) == 3 else ["", "", ""]
    return [old.get("opening") or "", *middle, old.get("closing") or ""]


def split(text: str) -> list[str]:
    """Paragraphs separated by one or more blank lines, each on one line."""
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def edit_letter(db, job, rep: dict, fb: dict, settings, ask: Callable[[str], str], edit: Callable[[Path], None] | None = None,
                out: Callable[[str], None] = print) -> dict:
    """Run review's `e` on the letter of this job; returns the report (the same one when nothing was installed)."""
    edit = edit or run_editor
    job = dict(job)
    job["description"] = job.get("description") or ""
    start = starting_text(rep)
    path = Path(rep["out_dir"]) / FILE_NAME
    path.write_text("\n\n".join(start), encoding="utf-8", newline="\n")
    out("  Edit the letter: five paragraphs separated by a blank line (P1 opening, P2 why this company, P3 and P4 your work, P5 closing).")
    while True:
        edit(path)
        paragraphs = split(path.read_text(encoding="utf-8"))
        if paragraphs == [" ".join(p.split()) for p in start]:
            out("  The letter is unchanged.")
            return rep
        problems = CL.edit_problems(job, fb, rep, settings, paragraphs)
        if not problems:
            out("  The edited letter passed every guard.")
            return _install(db, job, rep, fb, settings, paragraphs, [], out)
        out("  The edited letter breaks these rules:" + "".join(f"\n    - {p}" for p in problems))
        if len(paragraphs) != 5:
            answer = ask("  Press Enter to edit again, or type 'discard' to keep the old letter: ").strip().lower()
        else:
            answer = ask(f"  Type '{CONFIRM_ANYWAY}' to use it as written, press Enter to edit again, or type 'discard' to keep the old letter: ").strip()
            if answer == CONFIRM_ANYWAY:
                return _install(db, job, rep, fb, settings, paragraphs, problems, out)
            answer = answer.lower()
        if answer == "discard":
            out("  Edit discarded: the old letter stays.")
            return rep


def _install(db, job: dict, rep: dict, fb: dict, settings, paragraphs: list[str], overridden: list[str], out) -> dict:
    pdf, letter = CL.install_edited(job, fb, rep, settings, paragraphs, overridden)
    tailor.install_letter(job, fb, rep, pdf, letter)
    tailor.save_report(rep)
    db.update_job(job["id"], letter_score=letter["score"])
    db.log(job["id"], "review", {"letter_edited": True, "overridden": len(overridden)})
    out(f"  Letter installed ({letter['words']} words" + (f"; {len(overridden)} guard rule(s) overridden with '{CONFIRM_ANYWAY}'" if overridden else "") + ").")
    return rep
