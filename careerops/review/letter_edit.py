"""Review's two letter keys for `letter.mode: company`.

`l`  asks "What should change? (enter = just retry)"; what the person types is saved (paths.letter_feedback) and goes to the next draft, word
     for word, with the previous draft and the guard rejections rewritten as instructions (tailoring/letter_feedback.py). At most three drafts.
`e`  opens the letter text in $EDITOR (nano unless EDITOR says otherwise). On save the same guards run (company_letter.edit_problems). A letter
     that passes is used. One that fails shows its reasons and is used only if `use anyway` is typed in full; anything else edits again, and
     `discard` keeps the old letter. The text is one paragraph per name, `[P1]` ... `[P7]` on a line of its own (P2 and P6 may be left out); a text
     that lacks P1, P3, P4, P5 or P7, or names anything else, can never be installed.

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
_LABEL = re.compile(r"^\[(P\d)\]\s*$", re.M)
REQUIRED = ("P1", "P3", "P4", "P5", "P7")


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


def starting_text(rep: dict) -> dict[str, str]:
    """The paragraphs the editor opens with, by name: the letter's, else the last rejected draft (P3 to P5) between the opening (P1) and the ask (P7)."""
    old = rep.get("letter") or {}
    parts = {n: p for n, p in (old.get("parts") or {}).items() if p}
    if old.get("status") == "ok" and parts:
        return parts
    draft = list(old.get("draft_body") or [])
    out = {"P1": old.get("opening") or "", "P7": old.get("closing") or ""}
    out.update(dict(zip(("P3", "P4", "P5"), draft)) if len(draft) == 3 else {"P3": "", "P4": "", "P5": ""})
    return out


def render_text(parts: dict[str, str]) -> str:
    """The editor's text: `[P1]` on a line, the paragraph, a blank line, `[P2]` ..."""
    return "\n\n".join(f"[{n}]\n{parts[n]}" for n in CL.NAMES if n in parts)


def split(text: str) -> dict[str, str]:
    """{"P1": ..., ...} from the editor's text: each paragraph on one line under its `[Pn]` label (labels may come in any order)."""
    pieces = _LABEL.split(text.strip())
    return {pieces[i]: " ".join(pieces[i + 1].split()) for i in range(1, len(pieces) - 1, 2) if pieces[i + 1].strip()}


def edit_letter(db, job, rep: dict, fb: dict, settings, ask: Callable[[str], str], edit: Callable[[Path], None] | None = None,
                out: Callable[[str], None] = print) -> dict:
    """Run review's `e` on the letter of this job; returns the report (the same one when nothing was installed)."""
    edit = edit or run_editor
    job = dict(job)
    job["description"] = job.get("description") or ""
    start = starting_text(rep)
    path = Path(rep["out_dir"]) / FILE_NAME
    path.write_text(render_text(start), encoding="utf-8", newline="\n")
    out("  Edit the letter: one paragraph under each [Pn] label (P1 hook, P2 what it shows, P3 skills, P4 roll-up, P5 why this team, P6 honest gap, "
        "P7 the ask). P2 and P6 may be left out.")
    while True:
        edit(path)
        paragraphs = split(path.read_text(encoding="utf-8"))
        if paragraphs == {n: " ".join(p.split()) for n, p in start.items() if p}:
            out("  The letter is unchanged.")
            return rep
        required = CL.required_names(rep.get("letter") or {})          # v3.5.3: a template letter keeps its own paragraphs
        problems = CL.edit_problems(job, fb, rep, settings, paragraphs) if all(n in paragraphs for n in required) else \
            [f"missing paragraph(s): {', '.join(n for n in required if n not in paragraphs)}"]
        if not problems:
            out("  The edited letter passed every guard.")
            return _install(db, job, rep, fb, settings, paragraphs, [], out)
        out("  The edited letter breaks these rules:" + "".join(f"\n    - {p}" for p in problems))
        if not all(n in paragraphs for n in required) or any(n not in CL.NAMES for n in paragraphs):
            answer = ask("  Press Enter to edit again, or type 'discard' to keep the old letter: ").strip().lower()
        else:
            answer = ask(f"  Type '{CONFIRM_ANYWAY}' to use it as written, press Enter to edit again, or type 'discard' to keep the old letter: ").strip()
            if answer == CONFIRM_ANYWAY:
                return _install(db, job, rep, fb, settings, paragraphs, problems, out)
            answer = answer.lower()
        if answer == "discard":
            out("  Edit discarded: the old letter stays.")
            return rep


def _install(db, job: dict, rep: dict, fb: dict, settings, paragraphs: dict[str, str], overridden: list[str], out) -> dict:
    pdf, letter = CL.install_edited(job, fb, rep, settings, paragraphs, overridden)
    tailor.install_letter(job, fb, rep, pdf, letter)
    tailor.save_report(rep)
    db.update_job(job["id"], letter_score=letter["score"])
    db.log(job["id"], "review", {"letter_edited": True, "overridden": len(overridden)})
    out(f"  Letter installed ({letter['words']} words" + (f"; {len(overridden)} guard rule(s) overridden with '{CONFIRM_ANYWAY}'" if overridden else "") + ").")
    return rep
