"""The zero-token estimate of the honest ceiling, before any model call (v3.5.4). It feeds triage's E and the estimate F that orders the inbox.

The old estimate scanned the ad for taxonomy terms only. An ad whose requirements are its own phrases ("Applied statistics.", "Probabilistic reasoning.",
"Analytical ownership.") gave it two or three generic terms the fact bank has, so it read 100% while the ceiling after keyword extraction was 38.7%
(Iwoca, 10 Nov 2026). The estimate now reads the terms the way the gap report ranks them (review/tiers.py):

    terms   the taxonomy terms of the ad (scorer.taxonomy_keywords), plus the LABEL of each bullet in a requirements or nice-to-have section
            ("- Applied statistics. You have ..." -> "Applied statistics"; at most LABEL_WORDS words before a full stop, colon or dash)
    kept    the terms the ad STATES (must-have wording) or that are STRUCTURAL (in its requirements section, or repeated); an `inferred` one (a single
            mention in the about-us text) is dropped, unless nothing else is left
    score   the honest-ceiling rule itself (scorer.ceiling: weighted, stemmed, keyword-group synonyms) against the fact-bank text and its skills

No model and no network. `log` and `report` keep estimate vs final per tailored job, so `careerops calibrate --scores` prints the mean absolute error.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..review import tiers
from ..tailoring.scorer import Keyword, ceiling, classify_heading, merge_keywords, norm_literal, taxonomy_keywords

LABEL_WORDS = 4
LOG = "ceiling_estimates.jsonl"
LABEL = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+\**([A-Za-z][A-Za-z0-9+#/&' -]{1,48}?)\**\s*(?:[.:]|\s[–—-])\s+\S")
NOT_A_TERM = frozenset({"essential", "bonus", "desirable", "requirements", "required", "preferred", "nice to have", "you", "we", "our", "the role",
                        "e.g", "i.e", "etc"})


@dataclass
class Estimate:
    ceiling: float                                          # 0-1, the share the fact bank could honestly cover
    keywords: list[Keyword] = field(default_factory=list)   # the terms it was worked out on
    have: list[str] = field(default_factory=list)           # required terms the fact bank has, heaviest first
    lack: list[str] = field(default_factory=list)           # required terms it lacks
    gaps: list[str] = field(default_factory=list)           # every term it lacks (required or not)


def _section(head: str) -> str:
    if tiers.NICE_HEAD.search(head):
        return "nice"
    if tiers.REQUIREMENTS_HEAD.search(head):
        return "requirements"
    return "other"


def label_keywords(jd: str, req_w: float = 2.0, pref_w: float = 1.0) -> list[Keyword]:
    """The bullet labels of the ad's requirements (weight `req_w`) and nice-to-have (`pref_w`) sections. A label of more than LABEL_WORDS words, or a
    section word ("Essential"), is not a term."""
    section, out = "other", {}
    for line in (jd or "").splitlines():
        head = tiers._heading(line)
        if head is not None:
            section = "skip" if classify_heading(line) == "skip" else _section(head)
            continue
        if section not in ("requirements", "nice"):
            continue
        m = LABEL.match(line)
        if not m:
            continue
        term = m.group(1).strip(" -")
        if not term or len(term.split()) > LABEL_WORDS or term.lower() in NOT_A_TERM:
            continue
        w = req_w if section == "requirements" else pref_w
        key = norm_literal(term)
        if key not in out or w > out[key].weight:
            out[key] = Keyword(term, w, section == "requirements")
    return list(out.values())


def keywords(jd: str, settings) -> list[Keyword]:
    """The ad's terms (taxonomy + bullet labels), the inferred ones dropped while a stated or structural one is left."""
    sc = settings["scoring"]
    found = taxonomy_keywords(jd or "", sc["required_weight"], sc["preferred_weight"])
    have = {norm_literal(k.term) for k in found}
    found += [k for k in label_keywords(jd, sc["required_weight"], sc["preferred_weight"]) if norm_literal(k.term) not in have]
    found = merge_keywords(found)
    tier = tiers.classify(_once(jd or ""), found)          # a pasted-twice paragraph is not "repeated": each sentence counts once
    kept = [k for k in found if tier.get(k.term) in (tiers.STATED, tiers.STRUCTURAL)]
    return kept or found


def _once(jd: str) -> str:
    """The ad with each sentence kept at its first occurrence only (a pasted-twice paragraph is not emphasis)."""
    seen, out = set(), []
    for line in jd.splitlines():
        keep = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            key = sentence.strip().lower()
            if key and key in seen:
                continue
            seen.add(key)
            keep.append(sentence)
        out.append(" ".join(keep))
    return "\n".join(out)


def fact_terms(fb: Mapping[str, Any]) -> str:
    """The fact-bank text the ceiling is read against: every fact and the skills list (tailor.fact_text, as the final ceiling reads it)."""
    from ..tailoring.tailor import fact_text           # lazy: tailor imports the discovery package

    return fact_text(dict(fb))


def estimate(jd: str, fb: Mapping[str, Any], settings) -> Estimate:
    """The estimate for one ad. An ad with no term at all: ceiling 0 (nothing is known to be covered)."""
    kws = keywords(jd, settings)
    if not kws:
        return Estimate(0.0)
    text = fact_terms(fb)
    c, gaps = ceiling(text, kws)
    miss = set(gaps)
    req = sorted((k for k in kws if k.required), key=lambda k: -k.weight)
    return Estimate(c, kws, [k.term for k in req if k.term not in miss], [k.term for k in req if k.term in miss], list(gaps))


# ------------------------------------------------------------------ estimate vs final
def log_path(settings) -> Path:
    from . import evaluate as eval_mod

    return eval_mod.root(settings) / LOG


def log(settings, job: Mapping[str, Any], fb: Mapping[str, Any], final_ceiling: float | None, strict: float | None,
        *, now: datetime | None = None) -> dict | None:
    """Append one line: this job's estimate (worked out now, from its stored ad) next to the final ceiling and strict score. Never raises."""
    try:
        est = estimate(str(job["description"] or ""), fb, settings)
        row = {"ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "job_id": job["id"], "company": job["company"],
               "title": job["title"], "estimate": round(est.ceiling, 4), "final": None if final_ceiling is None else round(float(final_ceiling), 4),
               "strict": None if strict is None else round(float(strict), 4), "terms": len(est.keywords)}
        p = log_path(settings)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:  # noqa: BLE001 - the log is a measurement; it never stops a tailoring
        return None


def read(settings) -> list[dict]:
    """The logged lines, the newest one per job."""
    try:
        text = log_path(settings).read_text(encoding="utf-8")
    except OSError:
        return []
    out: dict[str, dict] = {}
    for raw in text.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("job_id") and e.get("estimate") is not None and e.get("final") is not None:
            out[e["job_id"]] = e
    return list(out.values())


def report(settings, *, worst: int = 5) -> list[str]:
    """Mean absolute error of the estimate against the final ceiling (and the strict score), in percentage points, with the worst misses."""
    rows = read(settings)
    if not rows:
        return ["Ceiling estimate: no tailored job logged yet (each tailoring adds a line to " + LOG + ")"]
    mae = sum(abs(r["estimate"] - r["final"]) for r in rows) / len(rows)
    bias = sum(r["estimate"] - r["final"] for r in rows) / len(rows)
    st = [r for r in rows if r.get("strict") is not None]
    out = [f"Ceiling estimate vs final ceiling ({len(rows)} tailored job(s)): mean absolute error {100 * mae:.1f} points, "
           f"bias {100 * bias:+.1f} (+ = the estimate is too high)"]
    if st:
        out.append(f"  vs the strict CV score ({len(st)}): mean absolute error {100 * sum(abs(r['estimate'] - r['strict']) for r in st) / len(st):.1f} points")
    for r in sorted(rows, key=lambda r: -abs(r["estimate"] - r["final"]))[:worst]:
        out.append(f"  {r['company']} | {r['title']}: estimate {100 * r['estimate']:.1f}% vs final {100 * r['final']:.1f}%")
    return out
