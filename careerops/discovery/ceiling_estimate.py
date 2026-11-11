"""The zero-token estimate of the honest ceiling, before any model call (v3.5.4, calibrated v3.5.5). It is the estimate F that orders live's inbox and decides the estimate stop; triage's E reads only its
`stated_share` (the first part below), so the T thresholds keep their scale.

What the final ceiling is (tailor._tailor): the weight share of the ad's keywords the CV-able fact bank backs, leaving out deny-listed terms (ats.claimable_*),
over a list of up to scoring.max_keywords terms. The model writes most of that list: the taxonomy terms of the ad plus ~15 phrases of its own ("procurement",
"lab robots", "dynamic pricing"). The fact bank backs 92% of the taxonomy terms but only 18% of the model's own phrases (47 tailored jobs, 11 Nov 2026), so an
estimate that reads the taxonomy terms alone said 100% where the final was 39% (Iwoca 57%, Scale AI 100% vs 42%): MAE 40 points, bias +37.

The estimate now works in two parts, with the same normaliser, weights and claimability as ats.py:

    known terms   (F reads all of them; triage's E and the have / lack lists read only the ones the ad STATES or that are STRUCTURAL, tiers.py, as v3.5.4 did)
                  the taxonomy terms of the ad (scorer.taxonomy_keywords, on the ad without its benefits section and with its all-caps / emoji headings closing the
                  section before them) plus the LABEL of each bullet in a requirements or nice-to-have section ("- Applied statistics. You have ..." -> "Applied
                  statistics"; at most LABEL_WORDS words), weighted required / neutral / preferred as the final ceiling weighs them, and counted as the fact bank
                  backs them (scorer.ceiling, stemmed, keyword-group synonyms; ats.claimable_terms: a gap, a neighbour-term-only match and a deny-listed term are
                  not backed; the bank is the CV-able one, tailor.cv_bank)
    other terms   the model's own phrases cannot be known without the model. The list is expected to hold `expected_terms` terms in all, so
                  max(0, expected_terms - known) more of them, each at the required weight, and the bank backs `other_coverage` of them

    F = (backed known weight + other_coverage x other weight) / (known weight + other weight)

`expected_terms` and `other_coverage` are `priority.estimate` in settings.yaml (34 and 0.30, fitted to the 46 real tailored jobs: mean absolute error 12.9
points, bias +0.6; 13.9 and -0.7 with the one test entry "o" the database also holds; the 25-term cap with 0.25 gives 15.3). They are a calibration, not the cap of
25: the fit shrinks every estimate toward the typical 0.3-0.5. `calibrate --scores` prints the table of estimate vs final for every tailored job, the error, the worst
five and the best-fit pair (it suggests; nothing writes the settings). The 10-point target is NOT reached, and not for want of a better reading of the ad: what stays
unexplained is the model's own choice. Its list was 7 terms for one ad and 38 for another, and its phrases were backed 0% to 100% of the time; a noun-phrase scan of
the ad (the nearest thing to the model without the model) correlated 0.04 with that, and reading the bullet labels or closing the headings changes the error by less
than a point. The jobs that miss are the ones where the model's list is short and covered (Wayve 7 terms, 84%; ElevenLabs, Epic Games) or long and uncovered (Valarian).

An ad with fewer than MIN_TERMS known terms has no reliable estimate (`Estimate.reliable`): live neither skips it on F nor reads its F as zero.
No model and no network. `log` and `report` keep estimate vs final per tailored job.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..review import tiers
from ..tailoring.scorer import Keyword, ceiling, classify_heading, drop_contained, merge_keywords, norm_literal, taxonomy_keywords

LABEL_WORDS = 4
MIN_TERMS = 3                       # known terms an ad needs before its estimate is more than a prior
DEFAULT_EXPECTED_TERMS = 34         # see the module text: fitted, 11 Nov 2026
DEFAULT_OTHER_COVERAGE = 0.30
LOG = "ceiling_estimates.jsonl"
LABEL = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+\**([A-Za-z][A-Za-z0-9+#/&' -]{1,48}?)\**\s*(?:[.:]|\s[–—-])\s+\S")
NOT_A_TERM = frozenset({"essential", "bonus", "desirable", "requirements", "required", "preferred", "nice to have", "you", "we", "our", "the role",
                        "e.g", "i.e", "etc"})
_HEADER_LINE = re.compile(r"^\s*([A-Za-z ]{3,12}?)\s*:\s*\S")


@dataclass
class Estimate:
    ceiling: float                                          # 0-1, the expected share of the final keyword weight the fact bank backs
    keywords: list[Keyword] = field(default_factory=list)   # the terms read from the ad
    have: list[str] = field(default_factory=list)           # required terms the fact bank has, heaviest first
    lack: list[str] = field(default_factory=list)           # required terms it lacks
    gaps: list[str] = field(default_factory=list)           # every term it lacks (required or not)
    known_weight: float = 0.0                               # weight of the terms read
    backed_weight: float = 0.0                              # ... of which the fact bank backs (claimable)
    other_weight: float = 0.0                               # weight of the terms the model is expected to add
    stated_share: float = 0.0                               # triage's E: the share of the weight of the STATED / STRUCTURAL terms the bank backs (v3.5.4 reading, no terms added)
    stated: list[str] = field(default_factory=list)         # those terms

    @property
    def known(self) -> int:
        return len(self.keywords)

    @property
    def reliable(self) -> bool:
        """Were enough terms read for the number to mean something? (An ad whose body the reader could not find has none.)"""
        return self.known >= MIN_TERMS


def params(settings) -> tuple[int, float]:
    """(expected_terms, other_coverage): settings.yaml priority.estimate, the defaults above where a key is missing."""
    given = ((getattr(settings, "raw", None) or {}).get("priority") or {}).get("estimate") or {}
    return int(given.get("expected_terms", DEFAULT_EXPECTED_TERMS)), float(given.get("other_coverage", DEFAULT_OTHER_COVERAGE))


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


def body(jd: str) -> str:
    """The ad without the `Company:` / `Title:` / `URL:` ... header lines an inbox file starts with (their words are not the ad's)."""
    from .manual import HEADER_KEYS

    lines = (jd or "").splitlines()
    i = 0
    while i < len(lines):
        m = _HEADER_LINE.match(lines[i])
        if lines[i].strip() and not (m and m.group(1).strip().lower() in HEADER_KEYS):
            break
        i += 1
    return "\n".join(lines[i:])


def close_sections(jd: str) -> str:
    """The ad with every heading the section scanner of scorer.taxonomy_keywords misses written as one it reads. That scanner knows a heading by a colon or a
    known opening phrase, so "INTRODUCING DATA SCIENCE & ANALYTICS AT TRAINLINE 👋" and "WE'D LOVE TO HEAR FROM YOU IF YOU HAVE... 🔍" never closed the "About us"
    section before them: the whole ad read as company blurb and its terms were lost (Trainline's Senior Data Scientist: F 0.00, 11 Nov 2026). A line
    tiers._heading sees as a heading (two words or more) and the scanner does not becomes "Requirements:", "Nice to have:" or "Details:"."""
    out = []
    for line in (jd or "").splitlines():
        head = tiers._heading(line)
        if head is not None and len(head.split()) >= 2 and classify_heading(line) is None:
            sec = _section(head)
            out.append("Requirements:" if sec == "requirements" else "Nice to have:" if sec == "nice" else "Details:")
        else:
            out.append(line)
    return "\n".join(out)


def keywords(jd: str, settings) -> list[Keyword]:
    """The terms the ad states, as the final keyword list would hold them before the model adds its own: taxonomy terms and requirement-bullet labels, the benefits
    section cut first (tailor.analyse does the same), near-duplicates merged and a term contained in a longer one dropped."""
    from ..tailoring.jdtext import strip_benefits

    sc = settings["scoring"]
    text = close_sections(strip_benefits(body(jd or ""))[0])
    found = taxonomy_keywords(text, sc["required_weight"], sc["preferred_weight"])
    have = {norm_literal(k.term) for k in found}
    found += [k for k in label_keywords(text, sc["required_weight"], sc["preferred_weight"]) if norm_literal(k.term) not in have]
    return drop_contained(merge_keywords(found))[0]


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


def stated_terms(jd: str, kws: list[Keyword]) -> list[Keyword]:
    """The terms the ad STATES (must-have wording) or that are STRUCTURAL (in its requirements section, or repeated); one mentioned once in the about-us text
    (`inferred`) is left out unless nothing else is left (review/tiers.py). This is the v3.5.4 reading, which triage's E and the have / lack lists keep."""
    tier = tiers.classify(_once(body(jd or "")), kws)
    return [k for k in kws if tier.get(k.term) in (tiers.STATED, tiers.STRUCTURAL)] or list(kws)


def fact_terms(fb: Mapping[str, Any], settings=None) -> str:
    """The fact-bank text the ceiling is read against: the bank as the CV may draw on it (tailor.cv_bank: a project without a public URL is left off) and its
    skills, as tailor._tailor reads it for the final ceiling."""
    from ..tailoring.tailor import cv_bank, fact_text            # lazy: tailor imports the discovery package

    bank = dict(fb)
    return fact_text(cv_bank(bank, settings["scoring"]) if settings is not None else bank)


def combine(known_weight: float, backed_weight: float, known: int, expected_terms: int, other_coverage: float, other_each: float = 2.0) -> float:
    """F from the weights: the known terms plus the terms the model is expected to add (see the module text)."""
    other = max(0, expected_terms - known) * other_each
    total = known_weight + other
    return (backed_weight + other_coverage * other) / total if total > 0 else 0.0


def estimate(jd: str, fb: Mapping[str, Any], settings, *, terms: int | None = None, coverage: float | None = None) -> Estimate:
    """The estimate for one ad. `terms` / `coverage` override the calibration (`fit` tries them)."""
    from .. import ats

    kws = keywords(jd, settings)
    exp, cov = params(settings)
    exp, cov = exp if terms is None else terms, cov if coverage is None else coverage
    each = float(settings["scoring"]["required_weight"])
    other = max(0, exp - len(kws)) * each
    if not kws:
        return Estimate(combine(0.0, 0.0, 0, exp, cov, each), other_weight=other)
    c, gaps = ceiling(fact_terms(fb, settings), kws)
    claim = ats.claimable_terms(kws, gaps, dict(fb).get("deny_terms", []))
    known_w = sum(k.weight for k in kws)
    backed_w = known_w * ats.claimable_ceiling(kws, claim) / 100.0
    miss = {k.term.lower() for k in kws} - {t.lower() for t in claim}          # a gap, a neighbour-term-only match or a deny-listed term
    stated = stated_terms(jd, kws)
    stated_w = sum(k.weight for k in stated)
    share = sum(k.weight for k in stated if k.term.lower() not in miss) / stated_w if stated_w > 0 else 0.0
    req = sorted((k for k in stated if k.required), key=lambda k: -k.weight)
    return Estimate(combine(known_w, backed_w, len(kws), exp, cov, each), kws,
                    [k.term for k in req if k.term.lower() not in miss], [k.term for k in req if k.term.lower() in miss],
                    [k.term for k in kws if k.term.lower() in miss], known_w, backed_w, other, share, [k.term for k in stated])


# ------------------------------------------------------------------ the table: estimate vs the final ceiling of every tailored job
@dataclass
class Row:
    job_id: str
    company: str
    title: str
    final: float
    estimate: float
    known: int
    known_weight: float
    backed_weight: float
    each: float

    @property
    def error(self) -> float:
        return self.estimate - self.final


def table(settings, jobs: Iterable[Mapping[str, Any]], fb: Mapping[str, Any]) -> list[Row]:
    """A row per tailored job (a stored final ceiling and an ad): the estimate worked out now from the stored ad. A job whose ad is a stub (under
    manual.MIN_JD_BYTES, a test entry) is left out."""
    from .manual import MIN_JD_BYTES

    rows = []
    for j in jobs:
        jd = str(j["description"] or "")
        if j["ceiling"] is None or len(jd) < MIN_JD_BYTES:
            continue
        e = estimate(jd, fb, settings)
        rows.append(Row(j["id"], j["company"], j["title"], float(j["ceiling"]), e.ceiling, e.known, e.known_weight, e.backed_weight,
                        float(settings["scoring"]["required_weight"])))
    return rows


def errors(rows: list[Row]) -> tuple[float, float]:
    """(mean absolute error, bias) in 0-1; bias > 0 = the estimate is too high."""
    return (statistics.mean(abs(r.error) for r in rows), statistics.mean(r.error for r in rows)) if rows else (0.0, 0.0)


def fit(rows: list[Row], *, terms: Iterable[int] = range(14, 41, 2), coverage: Iterable[float] = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4)) -> tuple[int, float, float]:
    """(expected_terms, other_coverage, mean absolute error) that fit `rows` best (a grid; the known terms and their weights are as the rows hold them)."""
    best = None
    for n in terms:
        for p in coverage:
            mae = statistics.mean(abs(combine(r.known_weight, r.backed_weight, r.known, n, p, r.each) - r.final) for r in rows)
            if best is None or mae < best[2] - 1e-12:
                best = (n, p, mae)
    return best or (DEFAULT_EXPECTED_TERMS, DEFAULT_OTHER_COVERAGE, 0.0)


def skip_check(rows: list[Row], threshold: float, review: float = 0.50, rescue: float = 0.60) -> list[str]:
    """What skipping at F < `threshold` would have done to these jobs. Tailoring skips a job whose strict score is under `review` (apply.bands.review) unless its
    ceiling is at `rescue` or more, and the strict score cannot pass the ceiling, so a job with a final ceiling under `review` was going to be skipped anyway: skipping
    it before tailoring only saves the tokens. A held job whose final ceiling is `rescue` or more was a wrong skip."""
    held = [r for r in rows if r.known >= MIN_TERMS and r.estimate < threshold]
    wrong = [r for r in held if r.final >= rescue]
    saved = [r for r in held if r.final < review]
    waste = [r for r in rows if r not in held and r.final < review]
    out = [f"  at F < {threshold:.2f}: {len(held)} of {len(rows)} tailored jobs would have been skipped before tailoring; {len(saved)} of them ended under a {100 * review:.0f}% "
           f"ceiling (the bands skip them anyway: tokens saved), {len(wrong)} reached {100 * rescue:.0f}% or more (wrongly skipped"
           + (f": {', '.join(f'{r.company} {100 * r.final:.0f}%' for r in wrong[:4])}" if wrong else "") + ")",
           f"  {len(waste)} jobs that passed F still ended under {100 * review:.0f}% (tailored for nothing)"]
    return out


def calibration(settings, jobs: Iterable[Mapping[str, Any]], fb: Mapping[str, Any], *, worst: int = 5, threshold: float | None = None) -> list[str]:
    """The lines `careerops calibrate --scores` prints: n, mean absolute error, bias, the worst `worst` jobs, the best-fit calibration and what the skip line does."""
    rows = table(settings, jobs, fb)
    if not rows:
        return ["Ceiling estimate: no tailored job with a stored ceiling and ad yet"]
    mae, bias = errors(rows)
    exp, cov = params(settings)
    out = [f"Ceiling estimate F vs the final ceiling ({len(rows)} tailored jobs, estimate worked out now from each stored ad): mean absolute error {100 * mae:.1f} "
           f"points, bias {100 * bias:+.1f} (+ = F too high)",
           f"  calibration in use: expected_terms {exp}, other_coverage {cov:.2f}"]
    n, p, best = fit(rows)
    if (n, round(p, 2)) != (exp, round(cov, 2)):
        out.append(f"  best fit on these jobs: expected_terms {n}, other_coverage {p:.2f} (mean absolute error {100 * best:.1f} points); "
                   "priority.estimate in settings.yaml is yours to change")
    for r in sorted(rows, key=lambda r: -abs(r.error))[:worst]:
        out.append(f"  {r.company} | {r.title}: F {r.estimate:.2f} vs final {r.final:.2f} ({100 * r.error:+.0f} points, {r.known} terms read)")
    if threshold is not None:
        out += skip_check(rows, threshold)
    return out


# ------------------------------------------------------------------ estimate vs final, logged at each tailoring
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
    """Mean absolute error of the logged estimates against the final ceiling (and the strict score), in percentage points, with the worst misses."""
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
