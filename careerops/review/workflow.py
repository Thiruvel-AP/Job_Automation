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
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml

from .. import ats, bands
from .. import note as note_mod
from ..apply import decided
from ..db import DB, now
from ..tailoring.faithfulness import deny_hits
from ..discovery import applykind, freshness, priority as prio
from ..discovery.gates import is_sponsor_flag
from ..hook import headline_lint
from ..llm import usage
from ..tailoring import doccheck
from ..tailoring.letter_reason import letter_reason
from ..tailoring.profile import bank_sentences as profile_sentences
from ..tailoring.queue import order
from ..tailoring.render import DATE_SEPARATOR, page2_fill, page_count, pdf_text, render_cv
from ..tailoring.scorer import Keyword, score
from ..tailoring import company_letter
from ..tailoring import letters as letters_mod
from ..tailoring import tailor as tailor_mod
from ..tailoring.tailor import FALLBACK_MARK, refresh_ats, regenerate_letter as _regenerate_letter, typography_report
from ..tailoring import provisional as prov
from . import addfacts, claude_review, gaps, judge, letter_edit
from . import decision_queue as dq

CONFIRM_FALLBACK = "send fallback"
CONFIRM_NO_LETTER = "no letter"
CONFIRM_DOC_CHECK = doccheck.OVERRIDE                       # "override doc check", typed in full to approve a CV that failed the document check
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
    """'ok', 'fallback', 'none' (no letter was produced: letter.on_failure), 'declined' (you chose none in review: hook mode) or 'off'
    (letter.mode: off, none was asked for).
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


def fill_lines(rep: dict, settings=None) -> list[str]:
    """The review screen's page-2 fill: how full page 2 is (from the rendered PDF) against the target, and what filled it
    (projects shown, optional fact-bank sections, linespread above the reference's). Nothing for a one-page CV."""
    f = rep["final"]
    if rep.get("one_page") or f.get("pages", 0) < 2 or f.get("page2_fill") is None:      # (a report from before the fill was measured)
        return []
    target = settings["scoring"].get("min_fill_page2") if settings is not None else None
    fill = rep.get("fill") or {}
    ctx = rep.get("context") or {}
    bits = [f"page 2 {f['page2_fill']:.0%} full" + (f" (target {target:.0%})" if target else "")]
    if ctx.get("projects") is not None:
        bits.append(f"{len(ctx['projects'])} projects")
    if fill.get("sections"):
        bits.append("optional sections: " + ", ".join(fill["sections"]))
    ls = ctx.get("linespread") or fill.get("linespread")
    base = float(settings["scoring"].get("cv_linespread", 1.02)) if settings is not None else 1.02     # the reference CV's
    if ls and float(ls) > base + 1e-9:
        bits.append(f"linespread {float(ls):.2f}")
    short = target is not None and f["page2_fill"] < target
    return ["Fill: " + " | ".join(bits) + ("  !! below target: the fact bank has no more content to add" if short else "")]


SHOWN_HYPHENS = 20                  # hyphenated words the review screen lists
RANK_TEXT = 56                      # characters of a bullet shown in the ranking


def typography_lines(rep: dict) -> list[str]:
    """The review screen's typography: em dashes left in the PDF (there must be none), en dashes used as prose dashes, the hyphenated
    words still on the page (add one to config/typography.yaml `rewrite` to close it up), the hyphenated JD keywords kept on
    purpose, the bullets whose `result:` is not in bold because the bullet words it differently, and an award repeated in a bullet."""
    t = rep.get("typography")
    if not t:
        return []
    em = t.get("em_dashes", 0)
    bits = [f"!! {em} EM DASH(ES) LEFT" if em else "no em dashes", f"{t.get('results_bold', 0)} result(s) in bold"]
    if t.get("prose_en_dashes"):
        bits.append(f"{len(t['prose_en_dashes'])} en dash(es) used as a prose dash")
    lines = ["Typography: " + " | ".join(bits)]
    if t.get("hyphens"):
        shown = [f"{w} x{n}" if n > 1 else w for w, n in list(t["hyphens"].items())[:SHOWN_HYPHENS]]
        more = len(t["hyphens"]) - SHOWN_HYPHENS
        lines.append("  hyphens left: " + ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
                     + "  [config/typography.yaml closes one up]")
    if t.get("kept"):
        lines.append("  hyphen kept (the JD spells the keyword with it): " + ", ".join(t["kept"]))
    if t.get("prose_en_dashes"):
        lines.append("  en dash in prose: " + "; ".join(f"'{c}'" for c in t["prose_en_dashes"][:3]))
    if t.get("results_unmatched"):
        lines.append("  result not in bold (the bullet words it differently): " + ", ".join(t["results_unmatched"]))
    if t.get("award_repeats"):
        lines.append("  !! the award is repeated inside the bullet of: " + ", ".join(t["award_repeats"]))
    return lines


def ranking_lines(rep: dict) -> list[str]:
    """The top-10 bullets and projects by relevance to the JD, with their scores (keyword + domain overlap; `*` = on the CV)."""
    r = rep.get("ranking")
    if not r:
        return []
    lines = ["Ranking, top bullets (score = keyword + domain; * = on the CV):"]
    for n, b in enumerate(r.get("bullets") or [], 1):
        text = b["text"] if len(b["text"]) <= RANK_TEXT else b["text"][:RANK_TEXT - 1] + "…"
        lines.append(f"  {n:>2}. {b['score']:>5.1f} ({b['keywords']:.1f}+{b['domain']:.1f}) {'*' if b['chosen'] else ' '} "
                     f"{b['id']:<10} {text}")
    lines.append("Ranking, projects:")
    for n, p in enumerate(r.get("projects") or [], 1):
        lines.append(f"  {n:>2}. {p['score']:>5.1f} (all bullets {p['sum']:.1f}) {'*' if p['chosen'] else ' '} {p['name']}")
    return lines


def letter_lines(rep: dict) -> list[str]:
    """Hook mode: the two letter variants and which one is chosen. [] for a report with no variants."""
    letters = rep.get("letters")
    if not letters:
        return []
    choice = letters.get("choice", "none")
    variants = (rep.get("ats") or {}).get("variants") or {}
    out = [f"Letters (hook mode): chosen {choice if not letters.get('declined') else 'none (you chose to go without)'}"
           "   [l] cycles A -> B -> none, [g] writes them again; the chosen one is uploaded"]
    for v in ("A", "B"):
        let = letters.get(v) or {}
        if let.get("status") != "ok":
            out.append(f"  {v}: {let.get('status', '?')} ({let.get('reason') or 'no reason recorded'})")
            continue
        nums = variants.get(v) or {}
        bits = [f"{let['words']} words"]
        if let.get("gap"):
            bits.append(f"gap: {let['gap']} (bridge to {let['bridge']}; sentence by the {let.get('gap_source')})")
        if nums.get("combined") is not None:
            bits.append(f"CV + letter {nums['combined']:.1f}%")
        if nums.get("letter") is not None:
            bits.append(f"letter alone {nums['letter']:.1f}% (not gated)")
        out.append(f"  {v}: ok | " + " | ".join(bits))
    return out


def company_letter_lines(letter: dict) -> list[str]:
    """letter.mode: company: the word count against the accepted range, the paragraphs it has (P1 to P7), and, for P5 (why this team), each sentence
    with the ad sentences it drew from, so that the claim about the employer can be read against the ad; the honest-gap paragraph (P6) is shown on its
    own with whether it is in the letter. [] for a letter of another mode."""
    if letter.get("mode") != "company" or letter.get("status") != "ok":
        return []
    head: list[str] = []
    if letter.get("draft"):                                          # v3.5.3
        head.append(f"!! LETTER DRAFT (needs [e]): {letter.get('draft_reason') or 'not checked'}")
    if letter.get("compose") == "template":                          # v3.5.3: the user's template, three slots filled
        src = letter.get("slot_source") or {}
        head.append(f"Letter (template '{letter.get('template_family')}'): {letter.get('words', 0)} words | paragraphs: "
                    + " ".join(letter.get("names") or []))
        head += [f"  {{{k}}} ({src.get(k, '?')}): {v}" for k, v in (letter.get("slots") or {}).items()]
        head += [f"  !! fact check: {p}" for p in letter.get("template_problems") or []]
        return head
    out = head + [f"Letter (company mode): {letter.get('words', 0)} words (asked for {letter.get('target_words')}; accepted {'-'.join(str(n) for n in letter.get('accepted') or company_letter.DEFAULT_RANGE)}) | opening: {letter.get('hook_source')} | requirements named: "
           + "; ".join(", ".join(r) for r in letter.get("requirements") or [])]
    out.append("  paragraphs: " + " ".join(letter.get("names") or []) + ("  (P2 left out: the fact bank has no limits or result for the hook)"
                                                                          if "P2" not in (letter.get("names") or []) else ""))
    out.append("  P5 (why this team) drew from the job ad:")
    for row in letter.get("p5_sources") or []:
        out.append(f"    - {row['claim']}")
        out += [f"        ad: {s}" for s in row["ad"]] or ["        ad: (no single ad sentence; the check passed on a run of them)"]
    if letter.get("p5_used"):
        out.append("  P5 quotes or closely follows these About / role sentences of the ad:")
        out += [f"    [{i}] {s}" for i, s in enumerate(letter["p5_used"], 1)]
    return out + honest_gap_lines(letter)


def honest_gap_lines(letter: dict) -> list[str]:
    """The honest-gap paragraph (P6) on its own: its text, whether it is in the letter, and the keys that change it. [] when the letter has none."""
    gap = letter.get("gap") or {}
    if letter.get("mode") != "company" or letter.get("status") != "ok" or not gap.get("paragraph"):
        return []
    state = "IN the letter" if "P6" in (letter.get("names") or []) else "NOT in the letter"
    return [f"  P6 (honest gap, {state}): {gap['paragraph']}",
            "    [k]eep it in the letter   [d]rop it from the letter"]


def ats_lines(rep: dict) -> list[str]:
    """The three ATS numbers (ats.py: strict CV, claimable ceiling, CV + letter; the letter alone is shown, never gated), and the
    STRETCH label for a job whose honest ceiling is under the band."""
    a = rep.get("ats")
    if not a:
        return []
    line = ("ATS strict CV {strict:.1f}% | claimable ceiling {ceiling:.1f}%".format(**a)
            + (f" | CV + letter {a['combined']:.1f}%" if a.get("combined") is not None else "")
            + (f" | letter alone {a['letter']:.1f}% (not gated)" if a.get("letter") is not None else ""))
    out = [line]
    if a.get("negated"):
        out.append("  in the CV only as 'not ...' (not counted): " + ", ".join(a["negated"]))
    if rep.get("stretch"):
        lo = (rep.get("band_pct") or [70.0])[0]
        out.append(f"STRETCH: the honest ceiling ({a['ceiling']:.0f}%) is under the {lo:.0f}% band. Kept, never dropped: "
                   "expect a lower interview rate; the fact bank, not the CV, is what stops it.")
    return out


def _jd(job) -> str | None:
    """The ad's text of a job row or dict, None when it has none (the gap report then calls every keyword inferred)."""
    try:
        return job["description"]
    except (KeyError, IndexError, TypeError):
        return None


def gap_lines(rep: dict, gap: gaps.GapReport) -> list[str]:
    """The gap report (review/gaps.py) in place of the plain ATS lines, for a job in the review band: its header first, then the CV + letter
    numbers and any keyword the CV only mentions as 'not ...', then the missing keywords and the two sums."""
    a = rep.get("ats") or {}
    lines = gap.lines()
    mid = []
    nums = [f"CV + letter {a['combined']:.1f}%" if a.get("combined") is not None else "",
            f"letter alone {a['letter']:.1f}% (not gated)" if a.get("letter") is not None else ""]
    if any(nums):
        mid.append(" | ".join(n for n in nums if n))
    if a.get("negated"):
        mid.append("  in the CV only as 'not ...' (not counted): " + ", ".join(a["negated"]))
    return [lines[0], *mid, *lines[1:]]


def provisional_lines(rep: dict) -> list[str]:
    """The facts the user typed for this job (review's `f`) that this tailoring was given: the ones the CV uses are labelled [provisional]."""
    facts = rep.get("provisional") or []
    if not facts:
        return []
    out = ["Facts you typed for this job (not in the fact bank; they count for this job only until you save them):"]
    for f in facts:
        out.append(f"  [provisional] {f['text']}  (counts for {f['keyword']})" if f.get("on_cv")
                   else f"  {f['text']}  (typed for {f['keyword']}; not on the CV)")
    return out


def ats_check_lines(rep: dict, settings, fb: dict | None, job) -> list[str]:
    """v3.3: `ATS structure 94/A (0 critical) | lints: ... | title match: yes | years: ... | degree met | skills-only required: 2` and its details
    (review/screeners.py). Red, with the reason, when the CV may not go out. Nothing without settings (a bare summary)."""
    if settings is None or fb is None:
        return []
    from . import screeners

    try:
        return screeners.check(rep, settings, fb, job).lines()
    except Exception as e:                                      # noqa: BLE001 - a check that cannot run says so; it is never a pass
        return [f"{screeners.RED}ATS structure: the check could not run ({type(e).__name__}: {str(e)[:80]}){screeners.RESET}"]


def doc_check_lines(rep: dict, settings, fb: dict | None, job) -> list[str]:
    """`DOC CHECK: pass`, or the list of failures, on the review screen. Nothing without settings (a bare summary)."""
    if settings is None or fb is None:
        return []
    return doc_check(rep, settings, fb, job).text().splitlines()


NO_HOOK_WARNING = "no hook: the headline has no number"
RED, RESET = "\033[31m", "\033[0m"


def hook_lines(rep: dict, color: bool | None = None) -> list[str]:
    """The hook (hook.py) as the review screen shows it: the problem, the proof bullet and its number, the headline, and anything
    that did not land on the CV. v3.5.3: with no hook the first line is the warning NO_HOOK_WARNING, in red on a terminal (`color` None: when
    stdout is one)."""
    if "hook" not in rep:
        return []
    hk = rep["hook"]
    if not hk:
        import sys

        paint = sys.stdout.isatty() if color is None else color
        warn = f"!! {NO_HOOK_WARNING}" + (f" (the CV headline is the role family's '{rep['hook_family_headline']}' alone)"
                                         if rep.get("hook_family_headline") else "")
        first = f"{RED}{warn}{RESET}" if paint else warn
        why = "; ".join(rep.get("hook_notes") or [])[:300]
        family = rep.get("hook_family_headline")
        if family:
            return [first, f"Hook: none qualifies, so the CV headline is the role family's '{family}' with no number (no hook line, no hook letter)",
                    *([f"  why: {why}"] if why else [])]
        return [first, "Hook: none (hook.enabled is off): the CV and letter carry no hook", *([f"  !! why: {why}"] if why else [])]
    out = [f"Hook {hk.get('hook_id') or '(code template)'}: {hk['problem']} | proof {hk['proof_bullet_id']} ({hk['org']}) | number "
           f"{hk['number'] or 'NONE'} [{hk['source']}]",
           *([f"  why it won: {hk['reason']}"] if hk.get("reason") else []),
           f"  headline: {hk['headline']}",
           *[f"  LINT WARNING (headline): {w}" for w in headline_lint(hk["headline"])],
           f"  profile sentence 1: {hk.get('profile_line', '')}",
           f"  letter sentence 1: {hk['letter_sentence']}"]
    placed = rep.get("hook_placed") or {}
    missing = [k for k in ("headline", "profile", "bullet") if placed and not placed.get(k)]
    if missing:
        out.append("  !! hook missing from the CV: " + ", ".join(missing))
    out += [f"  FLAG: {f}" for f in hk.get("flags") or []]
    return out


def work_country_lines(job, settings=None) -> list[str]:
    """W (the country the work is done in) with the evidence sentence, and the visa answers that country gets (apply/visa.py)."""
    if settings is None:
        return []
    from ..apply import visa
    from ..discovery import workcountry

    return visa.review_lines(job, workcountry.applicant_visa(settings))


def shown_location(job, settings=None) -> str:
    """The job's location as the review screen prints it: the remote label for a remote role at home (never the home country), else
    the location the ad gave."""
    from ..apply import visa
    from ..discovery import workcountry

    if settings is None:
        return job["location"] or "?"
    return visa.displayed_location(job, workcountry.applicant_visa(settings)) or "?"


def pdf_created(path) -> str:
    """" (created 2026-10-17 16:03:12)" for a PDF that exists (its file time), else "": the review screen shows when the CV was made."""
    try:
        return " (created " + datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") + ")"
    except (OSError, TypeError, ValueError):
        return ""


def summary(job, rep: dict, settings=None, fb: dict | None = None, bank: dict | None = None) -> str:
    """The review screen. With `fb` (the fact bank, plus the job's provisional facts) a job in the review band gets the gap report in place of
    the plain ATS header. `bank` (v3.5.2) is the fact bank for the v3.3 ATS structure line when `fb` is not given: that line (score, grade, lints,
    title / years / degree) is on every review screen, not only in the review band (it was missing from the 9 Nov RightShip run)."""
    f = rep["final"]
    gap = (gaps.build(rep, fb, bands.config(settings), jd=_jd(job)) if settings is not None and fb is not None and rep.get("ats")
           and gaps.applies(rep, bands.config(settings)) else None)
    all_flags = json.loads(job["flags"] or "[]")
    flags = [x for x in all_flags if not is_sponsor_flag(x)]          # sponsor-register notes are information, shown on the Sponsor line
    sponsor_notes = [x for x in all_flags if is_sponsor_flag(x)]
    pr = prio.priority(job, settings, tailored=True)
    last = rep["attempts"][-1]
    letter = rep["letter"]
    lines = [
        "=" * 78,
        f"{job['company']} — {job['title']}  [{shown_location(job, settings)}]",
        (f"T{rep['tier']} · {rep['headline']}" if rep.get("tier") else "Tier: out of target families"),
        f"{job['url']}",
        applykind.line(prio._get(job, "apply_url") or job["url"], prio._get(job, "apply_kind")),
        f"Sponsor: {job['sponsor_name'] or 'NOT MATCHED'} ({(job['sponsor_score'] or 0):.2f})"
        + (f" - {'; '.join(sponsor_notes)} (information only; a weight on priority, not a reason to skip)" if sponsor_notes else ""),
        pr.line(),
        *([f"{pr.flag()} (L {pr.l:.2f}: P is multiplied by it, the ATS score cannot see seniority; edit priority.level_factor in settings.yaml)"]
          if pr.flag() else []),
        f"Posted: {freshness.describe(job)}",
        *work_country_lines(job, settings),
        f"ATS literal {f['score']:.0%} | stemmed {f['score_stemmed']:.0%} | honest ceiling {rep['ceiling']:.0%} "
        f"| band: {f['band']} | pages {f['pages']}"
        + f" | attempts {len(rep['attempts'])}",
        *(gap_lines(rep, gap) if gap is not None else ats_lines(rep)),
        *ats_check_lines(rep, settings, fb if fb is not None else bank, job),
        *provisional_lines(rep),
        *hook_lines(rep),
        *letter_lines(rep),
        *fill_lines(rep, settings),
        *typography_lines(rep),
        *ranking_lines(rep),
        (f"Letter score {letter['score']:.0%}" if letter.get("score") is not None
         else f"Letter: off ({letter.get('reason') or 'letter.mode: off'}; write your own, or apply without)" if letter.get("status") == "off"
         else "Letter: none (you chose to go without one)" if letter.get("status") == "declined"
         else "Letter: none")
        + (f" | letter issues: {letter['problems']}" if letter['problems'] else ""),
    ]
    if letter.get("evidence"):
        lines.append("Letter evidence: " + ", ".join(e["id"] for e in letter["evidence"]))
    lines += company_letter_lines(letter)
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
        lines.append(NO_LETTER_BANNER + f"  ({letter_reason(letter)}; regenerate (l) to try again)")
    wording = prio.wording_line(str(prio._get(job, "description") or ""))     # a job row, or a dict without the JD
    if wording:
        lines.append(wording)
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
    if rep.get("possible_evidence") and gap is None:                 # the gap report lists them itself
        lines.append("Possible evidence — you decide (nothing is claimed; add the word to the fact bank if it is true):")
        for term, via in rep["possible_evidence"].items():
            lines.append(f"  {term} <- " + "; ".join(f"{v['via']} ({', '.join(v['where'][:2])})" for v in via))
    if rep["genuine_gaps"] and gap is None:
        lines.append("Genuine gaps (no evidence, left out): " + ", ".join(rep["genuine_gaps"][:25]))
    if f["wording_misses"]:
        lines.append("Wording-only misses (substance present): " + ", ".join(f["wording_misses"][:20]))
    viol = [v for a in rep["attempts"] for v in a["violations"]]
    if viol:
        lines.append(f"Guard blocked {len(viol)} model claims, e.g.: " + "; ".join(viol[:4]))
    warns = [w for a in rep["attempts"] for w in a["warnings"]]
    if warns:
        lines.append("Warnings: " + "; ".join(warns[:4]))
    lines += [f"Profile: {rep['context']['profile']}", f"CV: {rep['cv_pdf']}{pdf_created(rep['cv_pdf'])}", f"Letter: {rep['letter_pdf'] or '(none)'}"]
    if letter.get("status") == "off":
        lines.append("letter: off")
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


def open_url(url: str) -> bool:
    """Review's `w`: open an apply page in the default Windows browser, through wslview, else explorer.exe. It only opens the page: nothing
    is clicked, filled or submitted, and this tool sends no request to it. A link that is not http(s) is never handed to a program. False
    (with the link printed) when there is no program to open it with."""
    if urlparse(url or "").scheme not in ("http", "https") or not urlparse(url).netloc:
        return False
    if shutil.which("wslview"):
        subprocess.Popen(["wslview", url])
        return True
    explorer = shutil.which("explorer.exe") or (EXPLORER_FALLBACK if Path(EXPLORER_FALLBACK).exists() else None)
    if explorer:
        subprocess.Popen([explorer, url])
        return True
    print(f"  open manually: {url}")
    return False


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
def doc_check(rep: dict, settings, fb: dict | None, job) -> doccheck.DocCheck:
    """The document check of a tailored job (tailoring/doccheck.py), read from its files. A check that cannot run is a failure."""
    try:
        return doccheck.for_report(rep, settings, fb, job)
    except Exception as e:  # noqa: BLE001
        return doccheck.DocCheck([f"the check could not run ({type(e).__name__}: {str(e)[:80]})"])


def approve(db: DB, job, rep: dict, prefs: Path, confirm: str | None = None, doc: doccheck.DocCheck | None = None,
            override: str | None = None) -> None:
    """A fallback letter is approved only with confirm == 'send fallback', a job with no letter only with confirm ==
    'no letter' (each logged as an event). A CV that failed the document check (`doc`, tailoring/doccheck.py) is approved only with
    override == 'override doc check', typed in full; the failures are logged with the approval."""
    if doc is not None and not doc.ok:
        if (override or "").strip() != CONFIRM_DOC_CHECK:
            raise doccheck.DocCheckError(f"the document check failed ({len(doc.failures)}); fix it, or type '{CONFIRM_DOC_CHECK}' to approve anyway")
        db.log(job["id"], "review", {"approved_over_doc_check": True, "failures": doc.failures})
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


def contacts_step(db: DB, job, settings, ask) -> None:
    """After [a]pprove, with outreach.mode: drafts: the contacts found for the job (source, domain check) and `[c]ontact add`, before
    mark-applied. Needs the network (the company site, DNS): never on the mock root, and a failure is printed, never raised."""
    from .. import mockroot, outreach

    if mockroot.is_mock(settings) or not outreach.drafts_on(settings):
        return
    try:
        from .. import contacts
        contacts.review_step(db, settings, db.job(job["id"]), ask)
    except Exception as e:  # noqa: BLE001  (a missing dnspython, a network error: the review goes on)
        print(f"  contacts: skipped ({e})")


def regenerate_letter(db: DB, job, rep: dict, fb: dict, llm, settings, feedback: str = "") -> dict:
    """Re-draft the cover letter (review's 'l'); updates report.json, the letter score and the event log. `feedback` = what the person said
    should change (company mode): it goes to the next draft word for word."""
    rep = _regenerate_letter(db.job_with_contact(job), fb, llm, settings, rep, feedback=feedback)
    db.update_job(job["id"], letter_score=rep["letter"]["score"])
    db.log(job["id"], "review", {"letter_regenerated": rep["letter"]["status"], **({"choice": rep["letter_choice"]} if rep.get("letters") else {}),
                                 **({"feedback": feedback} if feedback else {})})
    return rep


def cycle_letter(db: DB, job, rep: dict) -> dict:
    """Review's `l` in hook mode: the chosen letter goes A -> B -> none -> A (a variant that was not produced is skipped). The choice
    is what `report["letter"]` / `letter_pdf` show and so what the apply assist uploads and the archive keeps; no model is called.
    Choosing none is yours: approving then needs no extra words."""
    letters = rep.get("letters")
    if not letters:
        raise ValueError("this job has no letter variants (letter.mode is not hook)")
    choice = letters_mod.next_choice(letters, letters.get("choice", "none"))
    any_ok = any((letters.get(v) or {}).get("status") == "ok" for v in letters_mod.VARIANTS)
    letters["choice"], letters["declined"] = choice, choice == "none" and any_ok
    rep["letter_choice"], rep["letter"] = choice, letters_mod.current(letters)
    rep["letter_pdf"] = rep["letter"].get("pdf")
    cv = Path(rep["cv_pdf"])
    if cv.exists():
        refresh_ats(rep, pdf_text(cv))
    (Path(rep["out_dir"]) / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    db.update_job(job["id"], letter_score=rep["letter"].get("score"))
    db.log(job["id"], "review", {"letter_choice": choice})
    return rep


def note_path(rep: dict) -> Path:
    return Path(rep["out_dir"]) / "note.md"


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


def read_edit_file(path: Path, ask: Callable[[str], str], open_editor: Callable[[Path], object]) -> dict | None:
    """The parsed `edit.yaml` once the editor has closed, or None when the person discards it. A file that does not parse (a stray `x` line, a
    tab, an unclosed quote) is never passed on: the line and column are printed and the person chooses [r]eopen (the editor opens again, then the
    file is read again) or [d]iscard (the CV stays as it was). Anything else asks again. A file that parses but is not a mapping is refused the same way."""
    while True:
        problem = ""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            problem = f"the file must be a mapping with `profile:` and `bullets:` (it holds {type(data).__name__})"
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
            problem = f"{where}{getattr(e, 'problem', None) or e}"
        except (OSError, UnicodeDecodeError) as e:
            problem = f"cannot read the file: {e}"
        print(f"  edit.yaml was not accepted, so nothing was changed: {problem}")
        while True:
            choice = (ask("  [r]eopen the file / [d]iscard your edits > ") or "").strip().lower()
            if choice in ("r", "reopen"):
                open_editor(path)
                break
            if choice in ("d", "discard"):
                print("  edits discarded; the CV is as it was")
                return None


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
        _log(prefs, {"kind": "edit_pair", "job_id": job["id"], "bullet_id": "profile", "source": profile_sentences(fb),
                     "keywords": rep["keywords"], "chosen": edited["profile"].strip(), "rejected": ctx["profile"]})
        ctx["profile"] = edited["profile"].strip()
    typo = rep.get("typography") or {}
    sep, keep = typo.get("date_separator") or DATE_SEPARATOR, typo.get("keep") or []
    pdf = render_cv(ctx, Path(rep["out_dir"]), Path(rep["cv_pdf"]).stem,     # keep the CV's final file name and layout
                    bool(rep.get("one_page")), sep, keep)
    rep["typography"] = typography_report(ctx, pdf_text(pdf), sep, keep)
    s = ats.score(pdf_text(pdf), kws)
    refresh_ats(rep, pdf_text(pdf))
    note_mod.write(job, rep, fb, Path(rep["out_dir"]))                 # the note carries the ATS numbers: keep it current
    pages = page_count(pdf)
    rep["final"].update({"score": s.literal, "score_stemmed": s.stemmed, "pages": pages,
                         "page2_fill": page2_fill(pdf, pages), "edited": True})
    rep["cv_pages"], rep["page2_fill"] = pages, rep["final"]["page2_fill"]
    (Path(rep["out_dir"]) / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    db.update_job(job["id"], score=s.literal, score_stemmed=s.stemmed)
    return warnings


# ---------------------------------------------------------------- interactive loop
MENU = {"a": "[a]pprove", "e": "[e]dit", "r": "[r]eject", "l": "[l]etter regenerate", "g": "[g]enerate letters again",
        "n": "[n]ote", "o": "[o]pen PDFs", "w": "[w] open apply page", "s": "[s]kip", "q": "[q]uit"}
MENU_ORDER = "aerlgnowsq"
HOOK_L = "[l]etter A/B/none"              # `l` in hook mode: the label shown instead of MENU["l"]
GAP_MENU = ("[a] apply as is   [s] skip   [r] re-tailor with the claimable points forced in",
            "[f] add facts     [o] open PDF   [q] quit")      # the choices for a job in the review band (the gap report); `r` is not reject here
GAP_ALSO = "elngw"                                          # the other keys still work in that prompt; listed on a line of their own
SKIPPED_USER = "skipped (user)"                             # review_job's answer to `s` at the gap report; live files the JD under jds_done/skipped/
PARKED = "parked"                                           # v3.5a: a park decided in the queue (live): the job stays 'needs_review', its JD in the inbox


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


def review_job(db: DB, job, settings, fb: dict, make_llm=None, ask=None, allow: str = "aerlgnowsq",
               skip_means_no: bool = False, retailor=None, claude=None, queue_apply: bool = False) -> str:
    """Review ONE job at the prompt. Returns 'approved' | 'rejected' | 'skipped' | 'skipped (user)' | 'quit'. `allow` names the choices
    offered (a approve, e edit, r reject, l regenerate the letter, o open the PDFs, w open the apply page in the browser, s skip, q quit); `ask` answers the
    prompts (default: input). This function changes nothing in the database on a skip: the caller stores it (`careerops review`: skip_user,
    v2.5; live: its own end of the job); with `skip_means_no` (live: a skipped job is finished) the judge log records it as a rejection.

    review.judge (judge.py): the verdict is printed under the summary and logged beside your decision. In `gate` mode a
    judge yes approves the job here without the prompt, unless it has FLAGS or its letter is a fallback or was lost to
    letter.on_failure (those keep their deliberate confirmation); raises judge.GateRefused while the log has not earned gate.

    A job in the review band (50 <= strict S < 70, or the "CV under-uses the fact bank" rescue) gets the gap report (review/gaps.py) and its own
    choices (GAP_MENU): `r` re-tailors once with the claimable keywords ranked first, `f` asks about the top gap keywords, keeps what the user
    types as provisional facts (data/facts/proposed.yaml, never the fact bank) and re-tailors with them, `s` answers 'skipped (user)'. Both
    re-tailorings go through `retailor(job id, fact bank for this job, keywords to rank first)`, which tailors and records the job (cli.py).

    v3.5a / v3.5 (decision_queue.py, claude_review.py): every version on screen is a review row of the decision queue. `claude` (a judge.Session)
    is asked once per row unless judge.mode is human; the screen says who decided. Your answer at the prompt is recorded on the row (approve =
    send, reject / skip = park, r / f = retailor). With `queue_apply` (live), a final decision is carried out without the prompt: yours from
    `careerops decide --by user`, or Claude's in judge.mode live, unless it must come to you (claude_review.escalation, a send that needs a typed
    confirmation). A park returns PARKED."""
    ask = ask or input
    prefs = settings.path("preferences")
    mode = judge.judge_mode(settings)
    if mode == "gate":
        judge.require_gate(settings)
    rep = load_report(job)
    store, band = prov.store(settings), bands.config(settings)

    def view() -> dict:
        """The fact bank as this job sees it: the real one plus the facts typed for this job, marked provisional."""
        return prov.augment(fb, store.for_job(job["id"]))[0]

    def show() -> str:
        """The review screen of this version of the job: with the gap report in the review band, the plain header elsewhere."""
        if rep.get("ats") and gaps.applies(rep, band):
            screen = summary(job, rep, settings, fb=view())
        else:
            screen = summary(job, rep, settings, bank=view())
        return "\n".join([screen, *doc_check_lines(rep, settings, view(), job)])          # the document check is on every review screen

    print(show())
    verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)
    if (mode == "gate" and verdict is not None and verdict.yes
            and not [x for x in json.loads(job["flags"] or "[]") if not is_sponsor_flag(x)]
            and letter_status(rep) in ("ok", "off", "declined") and doc_check(rep, settings, view(), job).ok):
        approve(db, job, rep, prefs)
        judge.log_decision(settings, job, rep, verdict, "auto", "gate: judge yes, no flags")
        db.log(job["id"], "review", {"auto_approved_by_judge": verdict.model, "score": verdict.score})
        print("  Judge gate: approved without the prompt (judge yes, no flags). Whether to open the form is still yours.")
        contacts_step(db, job, settings, ask)
        return "approved"

    def re_tailor(boost: list[str]) -> bool | None:
        """One re-tailoring of this job through the `retailor` hook (the bank given is this job's: the real one plus its provisional facts).
        True = the new tailoring is shown; False = nothing changed; None = the job cannot go on now (its letter is pending: quit)."""
        nonlocal job, rep, verdict
        if retailor is None:
            print("  no re-tailoring is available in this run")
            return False
        bank, notes = prov.augment(fb, store.for_job(job["id"]))
        for n in notes:
            print("  " + n)
        old = rep.get("ats") or {}
        retailor(job["id"], bank, list(boost))
        job = db.job(job["id"])
        status = job["status"]
        if status == "letter_pending":
            print("  The CV is re-tailored but its letter is pending (no provider has budget now); run live again later to finish it.")
            return None
        if status == "error":
            why = job["reason"] or "unknown error"
            db.update_job(job["id"], status="needs_review", reason=None)
            job = db.job(job["id"])
            print(f"  re-tailoring failed ({why}); the earlier tailoring stays.")
            return False
        if status == bands.ATS_SKIP:                                  # under the bands after all: this is the user's call, not an automatic skip
            db.update_job(job["id"], status="needs_review", reason=None)
            job = db.job(job["id"])
            print("  The re-tailored CV is under the ATS band; it is still yours to decide.")
        rep = load_report(job)
        new = rep.get("ats") or {}
        print(f"  re-tailored: ATS strict {bands.num(old.get('strict', 0))} -> {bands.num(new.get('strict', 0))}, "
              f"ceiling {bands.num(old.get('ceiling', 0))} -> {bands.num(new.get('ceiling', 0))}")
        print(show())
        verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the CV changed: judge this version
        return True

    qrow: list[dq.Row] = []                                                  # the queue row of the version on screen

    def queue_step(apply: bool) -> str | None:
        """Queue this version's row, ask Claude about it once, print who decided; with `apply`, carry out a final decision. Returns review_job's
        answer when the job is done here, None to go on to the prompt."""
        nonlocal job, rep
        for _ in range(dq.RETAILOR_MAX + 1):
            row = dq.review_row(settings, job, rep)
            qrow[:] = [row]
            why_none = ""
            if dq.state(settings, row).latest("claude") is None and dq.mode(settings) != "human" and claude is not None:
                _, why_none = claude_review.ask(settings, claude, job, rep, fb, row)
            st =dq.state(settings, row)
            for line in claude_review.screen_lines(st, why_none):
                print(line)
            d = st.final
            if not apply or d is None:
                return None
            why = claude_review.escalation(d, verdict)
            if why:
                print(f"  escalated to you: {why}")
                return None
            who = "you (careerops decide)" if d["by"] == "user" else f"claude ({d['confidence']})"
            if d["option"] == "park":
                print(f"  parked by {who}: the job stays waiting for review; its JD stays in the inbox")
                db.log(job["id"], "review", {"queue": row.qid, "parked_by": d["by"]})
                return PARKED
            if d["option"] == "send":
                if letter_status(rep) in ("ok", "off", "declined") and doc_check(rep, settings, view(), job).ok:
                    approve(db, job, rep, prefs)
                    judge.log_decision(settings, job, rep, verdict, "approve" if d["by"] == "user" else "auto", f"queue {row.qid}: {who}")
                    db.log(job["id"], "review", {"queue": row.qid, "approved_by": d["by"], "confidence": d.get("confidence")})
                    print(f"  approved by {who}. Whether a form is submitted is still decided by the apply gates and by you.")
                    contacts_step(db, job, settings, ask)
                    return "approved"
                print(f"  {who} said send, but this letter or document check needs your typed confirmation: the prompt follows")
                return None
            if d["option"] == "retailor":
                gap_now = gaps.build(rep, view(), band, jd=_jd(job)) if rep.get("ats") and gaps.applies(rep, band) else None
                print(f"  re-tailoring, as {who} decided")
                done = re_tailor(gap_now.claimable_terms() if gap_now is not None else [])
                if done is None:
                    return "quit"
                if not done:
                    return None
                continue                                                     # a new version: its own row, its own decision
        return None

    def record(choice: str, reason: str = "") -> None:
        if qrow:
            dq.record_user(settings, qrow[0], choice, reason)

    done = queue_step(queue_apply)
    if done is not None:
        return done

    while True:
        hook_mode = bool(rep.get("letters"))
        gap_keys = "kd" if ((rep.get("letter") or {}).get("gap") or {}).get("paragraph") and (rep.get("letter") or {}).get("status") == "ok" else ""
        gap = gaps.build(rep, view(), band, jd=_jd(job)) if rep.get("ats") and gaps.applies(rep, band) else None
        if gap is None:
            allowed = set(allow) | set(gap_keys)
            menu = "  ".join((HOOK_L if hook_mode and k == "l" else MENU[k]) for k in MENU_ORDER if k in allow and (k != "g" or hook_mode))
            c = ask(f"{menu} > ").strip().lower()
        else:
            allowed = (set(allow) - {"r"}) | {"r", "f"} | set(gap_keys)
            print("\n".join(GAP_MENU))
            more = [(HOOK_L if hook_mode and k == "l" else MENU[k]) for k in GAP_ALSO if k in allow and (k != "g" or hook_mode)]
            if more:
                print("also: " + "  ".join(more))
            c = ask("> ").strip().lower()
        if c not in allowed:
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
            found, override = doc_check(rep, settings, view(), job), None
            if not found.ok:
                print("  " + found.text().replace("\n", "\n  "))
                override = ask(f"The document check failed. Type '{CONFIRM_DOC_CHECK}' to approve anyway: ")
            try:
                if found.ok:
                    approve(db, job, rep, prefs, confirm=confirm)
                else:
                    approve(db, job, rep, prefs, confirm=confirm, doc=found, override=override)
            except (FallbackLetterError, NoLetterError, doccheck.DocCheckError) as e:
                print(f"  NOT approved: {e}")
                continue
            judge.log_decision(settings, job, rep, verdict, "approve")
            record("approve")
            contacts_step(db, job, settings, ask)
            return "approved"
        elif c == "w":
            url = prio._get(job, "apply_url") or job["url"]
            if not url:
                print("  no application page for this job: apply by hand")
            elif not open_url(url):
                print(f"  could not open {url}" if urlparse(url).scheme in ("http", "https") else f"  not a web link, not opened: {url}")
        elif c == "n":
            if note_path(rep).exists():
                open_file(str(note_path(rep)))
                print(f"  note: {note_path(rep)}")
            else:
                print("  no note.md for this job (it is written when the job is tailored)")
        elif c in ("k", "d"):
            pdf, letter, problems = company_letter.set_gap(dict(job), fb, rep, settings, keep=(c == "k"))
            if problems:
                print("  P6 not changed: " + "; ".join(problems))
                continue
            tailor_mod.install_letter(dict(job), fb, rep, pdf, letter)
            tailor_mod.save_report(rep)
            db.update_job(job["id"], letter_score=letter["score"])
            db.log(job["id"], "review", {"honest_gap": "kept" if c == "k" else "dropped"})
            job = db.job(job["id"])
            print(f"  P6 {'kept' if c == 'k' else 'dropped'}: the letter is {letter['words']} words.")
            print(show())
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the letter changed: judge this version
        elif c == "g" and not hook_mode:
            print("  [g] writes the A / B letters again: it is for letter.mode: hook")
        elif c == "l" and hook_mode:
            rep = cycle_letter(db, job, rep)
            job = db.job(job["id"])
            print("\n".join(letter_lines(rep) + ats_lines(rep)[:1]))
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # another letter: judge this version
        elif c in ("l", "g"):
            if letter_status(rep) == "off":
                print("  letter.mode is off: set letter.mode: draft (or hook) in config/settings.yaml to have a letter drafted")
                continue
            if make_llm is None:
                print("  no LLM available for this run")
                continue
            feedback = ""
            if c == "l" and (rep.get("letter") or {}).get("mode") == "company":       # company letters: what should change?
                feedback = letter_edit.ask_feedback(ask, settings, job["id"])
            try:
                rep = regenerate_letter(db, job, rep, fb, make_llm(), settings, feedback=feedback)
            except Exception as e:  # noqa: BLE001  (provider errors, xelatex errors: report, keep the old letter)
                print(f"  letter NOT regenerated: {e}")
                continue
            job = db.job(job["id"])
            print(show())
            usage.report(db, settings)
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the letter changed: judge this version
        elif c == "r" and gap is not None:
            if gap.claimable_terms():
                record("retailor")
                if re_tailor(gap.claimable_terms()) is None:
                    return "quit"
                queue_step(False)                                            # the new version's row: what Claude says about it
            else:
                print("  nothing claimable is missing: only a new fact (f) can add points")
        elif c == "f" and gap is not None:
            facts = addfacts.collect(ask, gap, fb, store, job["id"])
            if not facts:
                print("  nothing typed: nothing changed")
                continue
            record("facts")
            if re_tailor(gap.claimable_terms() + [f.keyword for f in facts]) is None:
                return "quit"
            queue_step(False)
        elif c == "r":
            reason = ask("reason: ").strip() or "unspecified"
            reject(db, job, rep, prefs, reason)
            judge.log_decision(settings, job, rep, verdict, "reject", reason)
            record("reject", reason)
            return "rejected"
        elif c == "e" and (rep.get("letter") or {}).get("mode") == "company" and \
                ask("Edit the [c]v or the [l]etter? (enter = cv) ").strip().lower() in ("l", "letter"):
            rep = letter_edit.edit_letter(db, job, rep, fb, settings, ask)
            job = db.job(job["id"])
            print(show())
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the letter changed: judge this version
        elif c == "e":
            path = Path(rep["out_dir"]) / "edit.yaml"
            path.write_text(yaml.safe_dump(edit_file_for(rep), sort_keys=False, allow_unicode=True, width=120),
                            encoding="utf-8")
            editor = os.environ.get("EDITOR", "notepad" if platform.system() == "Windows" else "nano")
            subprocess.call([editor, str(path)])
            edited = read_edit_file(path, ask, lambda p: subprocess.call([editor, str(p)]))
            if edited is None:
                continue
            warns = apply_edits(db, job, rep, edited, fb, prefs)
            for w in warns:
                print("  WARNING:", w)
            print(f"  re-scored: {rep['final']['score']:.0%} literal, {rep['final']['pages']} page(s)")
            verdict = _ask_judge(settings, job, rep, fb, make_llm, mode)       # the CV changed: judge this version
        elif c == "s":
            if skip_means_no:
                judge.log_decision(settings, job, rep, verdict, "reject", "skipped in live review")
            record("skip")
            return SKIPPED_USER if gap is not None else "skipped"
        elif c == "q":
            return "quit"


def skip_user(db: DB, settings, job_id: str, where: str = "review") -> None:
    """`s` in `careerops review` (any skip there, v2.5: before, a skip without a gap report stored nothing and the job came back) and `careerops skip
    --id`: the job is skipped (reason 'skipped (user)'), the decision log gets a line with its keys, every copy of the same listing is skipped, and
    their JD files leave the inbox and any jds/approved/<date>/ folder for jds_done/skipped/ (apply/decided.py): a skip overrides an approval."""
    db.update_job(job_id, status=decided.SKIPPED, reason=SKIPPED_USER)
    db.log(job_id, where, SKIPPED_USER)
    try:
        for line in decided.settle(db, settings, job_id, decided.SKIPPED, reason=SKIPPED_USER).lines():
            print("  " + line)
    except decided.DecidedError as e:
        print(f"  JD files not moved: {e}")


def run(db: DB, settings, fb: dict, limit: int | None = None, make_llm=None, retailor=None, claude=None) -> None:
    queue = order(db.jobs("needs_review"), settings, tailored=True)[:limit]
    print(f"{len(queue)} job(s) waiting for review.")
    for job in queue:
        result = review_job(db, job, settings, fb, make_llm, retailor=retailor, claude=claude)
        if result in (SKIPPED_USER, "skipped"):
            skip_user(db, settings, job["id"])
        if result == "quit":
            return
