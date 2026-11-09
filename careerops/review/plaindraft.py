"""`careerops plain-draft`: two plain-language candidates per fact-bank bullet (and hook line) that has none, shown next to the original; the one
you pick is saved only on your `y` (tailoring/plain.py has the checks; factbank_append.add_field the one write, backup first).

    az10  AstraZeneca  (bullet)
      original: Needed an agent that researchers could trust ...: built a tool-calling agent (LangChain, ...) ...
      1.        Researchers needed answers they could trust, so I built an AI assistant on AWS, checked by ~65 automated tests.
                21 words, Flesch 52.3, jargon: none
      2.        ...
      [1]/[2] pick that one / [e] write your own / [n] skip / [q] quit > 1
      Save exactly this? [y/N] y

A candidate that fails a check is never shown as one (the problems are printed instead); when only one passes, one is shown. A bullet that
states no result is NEEDS RESULT and is not drafted. A saved `plain:` that breaks the ownership or credit rule (9 Nov 2026) is listed: the CV
does not use it, and you delete it yourself to draft a new one. Your edit is checked the same way, shown with its problems, and saved only after
a `y` for that exact text."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import factbank_append as F
from ..config import load_yaml
from ..tailoring import plain as P

CONFIRM = "    Save exactly this? [y/N] "


def menu(n: int) -> str:
    picks = "/".join(f"[{i}]" for i in range(1, n + 1))
    return f"  {picks} pick that one / [e] write your own / [n] skip / [q] quit > "


def _label(it: P.Item) -> str:
    o = it.owner
    where = str(o.get("org") or o.get("name") or o.get("id") or "")
    return f"{it.owner_id}  {where.split(',')[0][:50]}  ({it.kind if it.kind == 'bullet' else 'hook ' + it.kind})"


def _facts(text: str, fb: dict | None = None, owner: dict | None = None) -> str:
    j = P.jargon(text, fb or {}, owner)
    return f"{len(text.split())} words, Flesch {P.flesch(text)}, jargon: {', '.join(j) or 'none'}"


def run(fb_path: Path, llm, settings, *, ask: Callable[[str], str] = input, out: Callable[[str], None] = print, limit: int | None = None,
        ids: set[str] | None = None, hooks: bool = True, dry_run: bool = False, now: datetime | None = None) -> dict:
    """Draft, show and (on `y`) save. Returns {"saved": [(id, field)], "skipped", "rejected": {id: problems}, "needs_result", "tokens"}."""
    fb = load_yaml(fb_path)
    todo = [it for it in P.items(fb, hooks) if not ids or it.owner_id in ids]
    stats: dict = {"saved": [], "skipped": [], "rejected": {}, "needs_result": [], "refused": {}, "tokens": 0}
    out(f"plain-draft: {len(todo)} line(s) with no plain version" + (f"; drafting at most {limit}" if limit else "")
        + (" (dry run: nothing is saved)" if dry_run else ""))
    for sec in ("experience", "projects"):                      # 9 Nov 2026: saved lines the CV no longer uses
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                saved = " ".join(str(b.get("plain") or "").split())
                why = P.saved_problems(saved, b) if saved and (not ids or str(b["id"]) in ids) else []
                if why:
                    stats["refused"][str(b["id"])] = why
                    out(f"\n  {b['id']}: its saved plain version is NOT used on the CV ({'; '.join(why)}):\n    {saved}\n"
                        "    the original wording is used instead; delete that `plain:` line in fact_bank.yaml yourself to draft a new one")
    drafted = 0
    for it in todo:
        if it.needs_result:
            out(f"\n  {_label(it)}\n    NEEDS RESULT: the bullet states no number or outcome; add a `result:` to it first. Not drafted.")
            stats["needs_result"].append(it.owner_id)
            continue
        if limit is not None and drafted >= limit:
            break
        drafted += 1
        cands, probs, tokens = P.draft(llm, it, fb, settings)
        stats["tokens"] += tokens
        out(f"\n  {_label(it)}\n    original: {it.original}")
        if not cands:
            out(f"    no candidate passed the checks ({'; '.join(probs)[:300]}): nothing to approve")
            stats["rejected"][it.owner_id] = probs
            continue
        for i, cand in enumerate(cands, 1):
            out(f"    {i}.        {cand}\n              {_facts(cand, fb, it.owner)}")
        if len(cands) < P.CANDIDATES:
            out(f"    (only {len(cands)} of {P.CANDIDATES} candidates passed the checks)")
        if dry_run:
            continue
        reply = (ask(menu(len(cands))) or "").strip().lower()
        if reply == "q":
            break
        cand = cands[int(reply) - 1] if reply.isdigit() and 1 <= int(reply) <= len(cands) else ""
        if cand:
            out(f"    chosen:   {cand}")
            reply = (ask(CONFIRM) or "").strip().lower()
        elif reply == "e":
            typed = " ".join((ask("    Your plain version (blank = skip): ") or "").split())
            if not typed:
                stats["skipped"].append(it.owner_id)
                continue
            mine = P.problems(typed, it.bullet, it.owner, fb, settings, it.kind, None if it.kind == "bullet" else it.source)
            out(f"    yours:    {typed}\n              {_facts(typed, fb, it.owner)}"
                + (f"\n    checks:   {'; '.join(mine)}" if mine else "\n    checks:   all pass"))
            cand, reply = typed, (ask(CONFIRM) or "").strip().lower()
        else:
            reply = ""                                          # anything else, a bare `y` included: nothing is saved
        if reply != "y" or not cand:
            stats["skipped"].append(it.owner_id)
            continue
        try:
            done = F.add_field(Path(fb_path), it.owner_id, it.field, cand, confirmed="y", now=now)
        except F.AppendError as e:
            out(f"    {e}")
            continue
        out(f"    saved as `{it.field}:` on {it.owner_id} (backup {done.backup.name})")
        stats["saved"].append((it.owner_id, it.field))
        fb = load_yaml(fb_path)
    out(f"\nplain-draft: saved {len(stats['saved'])}, skipped {len(stats['skipped'])}, no candidate {len(stats['rejected'])}, "
        f"NEEDS RESULT {len(stats['needs_result'])}, saved lines not used {len(stats['refused'])}; model tokens ~{stats['tokens']}")
    return stats
