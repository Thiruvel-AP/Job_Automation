"""`careerops calibrate`: run the real tailoring pipeline over the calibration JDs and measure it (M5 part 2).

Input is `<calibration>/manifest.yaml`, one entry per JD (id, company, title, jd_file; optionally manual_ats,
reference_cv, outcome). Per JD it triages (verdict recorded, tailoring happens regardless), tailors with the supplied LLM
(the Router under pacing: its per-minute token window and its `begin_job` daily-budget check both apply, and the run
stops early, leaving the rest unmeasured, if the day's tokens cannot cover another job), and measures the result:
keywords, ceiling, final score, rephrase coverage and guard blocks, the selection, violations, letter status, tokens,
seconds. It writes `report-<stamp>.md` (+ a `.json` sidecar that `--compare` reads) and a `rubric.csv` of empty rating
columns for the human review.

Nothing here touches the jobs table or `data/outputs/`: jobs are plain dicts and the CVs go under
`<calibration>/runs/<stamp>/outputs/`. The DB is used only for the sponsor index (read) and the router's token counters.
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, load_yaml
from ..discovery import gates
from ..discovery import register as sponsors
from ..llm import BudgetExhausted
from ..tailoring.render import pdf_text
from ..tailoring.scorer import Keyword, score
from ..tailoring.tailor import tailor_job

REQUIRED = ("id", "company", "title", "jd_file")
OUTCOMES = ("rejected", "no_response", "screen_passed", "interview", "offer")
GUARD_INVESTIGATE = 0.30             # blocked ÷ proposed rephrases above this deserves a look
RUBRIC_COLUMNS = ["id", "cv_false_claim(Y/N)", "cv_relevance(1-5)", "cv_readability(1-5)", "letter_false_claim(Y/N)",
                  "letter_tone(1-5)", "would_send(Y/N)", "notes"]
RUBRIC_MARK = "<!-- rubric -->"
HEADER_RULE = re.compile(r"^=+$")


class ManifestError(ValueError):
    """The manifest cannot be used; the message names the entry and what is wrong."""


# ------------------------------------------------------------------ manifest
def resolve(root: Path, p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else root / p


def jd_text(path: Path) -> str:
    """The JD in a calibration file. Files saved from a job board start with a header block (name, `Source:` and `Fetched`
    lines) ending in a line of `=` signs; that block is not part of the JD. Files without one, and JDs that merely use an
    `=====` heading underline, are used as they are."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines[:12]):
        if len(line.strip()) >= 10 and HEADER_RULE.match(line.strip()):
            if any(l.strip().startswith(("Source:", "Fetched")) for l in lines[:i]):
                return "\n".join(lines[i + 1:]).strip() + "\n"
            break
    return text


def load_manifest(path: Path, root: Path) -> list[dict]:
    """The manifest's entries, each with `_jd_path` resolved. Raises ManifestError for a missing field, a duplicate id,
    an unreadable JD file or an unknown outcome label."""
    raw = load_yaml(path)
    if not isinstance(raw, list) or not raw:
        raise ManifestError(f"{path}: expected a list of entries (id, company, title, jd_file, ...)")
    seen: set[str] = set()
    entries = []
    for n, e in enumerate(raw, 1):
        if not isinstance(e, dict):
            raise ManifestError(f"{path}: entry {n} is not a mapping")
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            raise ManifestError(f"{path}: entry {n} ({e.get('id', '?')}) is missing {', '.join(missing)}")
        if e["id"] in seen:
            raise ManifestError(f"{path}: duplicate id {e['id']}")
        seen.add(e["id"])
        jd = resolve(root, e["jd_file"])
        if not jd.is_file():
            raise ManifestError(f"{path}: {e['id']}: jd_file not found: {jd}")
        if e.get("outcome") and e["outcome"] not in OUTCOMES:
            raise ManifestError(f"{path}: {e['id']}: outcome must be one of {', '.join(OUTCOMES)}")
        entries.append({**e, "_jd_path": jd})
    return entries


# ------------------------------------------------------------------ one JD
def job_for(entry: dict) -> dict:
    jd = jd_text(entry["_jd_path"])
    return {"id": f"calibration:{entry['id']}", "source": "calibration", "company": entry["company"],
            "title": entry["title"], "location": entry.get("location", "United Kingdom"),
            "url": entry.get("url", ""), "apply_url": entry.get("url", ""), "description": jd}


def _usage(rep: dict) -> dict:
    u = rep.get("llm_usage") or {}
    by = {t: {"calls": v["calls"], "prompt_tokens": v["prompt_tokens"], "total_tokens": v["total_tokens"],
              "max_prompt": max([n for n in v["prompt_tokens_per_call"] if n is not None], default=None)}
          for t, v in (u.get("by_task") or {}).items()}
    return {"calls": len(u.get("calls") or []), "total_tokens": u.get("total_tokens", 0),
            "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0),
            "max_prompt": max([t["max_prompt"] for t in by.values() if t["max_prompt"] is not None], default=None),
            "by_task": by}


def measure(entry: dict, verdict: gates.Verdict, rep: dict, seconds: float, settings: Settings) -> dict:
    """One report row from a tailored job's report.json and its triage verdict."""
    final = rep["final"]
    chosen = rep["attempts"][final["chosen_attempt"] - 1]
    kws = [Keyword(**k) for k in rep["keywords"]]
    reference = None
    if entry.get("reference_cv"):
        ref = resolve(settings.root, entry["reference_cv"])
        if ref.is_file():                                       # scored with the SAME keywords as the pipeline's CV
            reference = score(pdf_text(ref), kws).literal
    return {
        "id": entry["id"], "company": entry["company"], "title": entry["title"], "status": "ok",
        "triage": {"passed": verdict.passed, "reasons": verdict.reasons, "flags": verdict.flags,
                   "family": verdict.family, "tier": verdict.tier, "ceiling_pre": verdict.ceiling_pre},
        "keywords": {"n": len(kws), "required": sum(k.required for k in kws), "terms": [k.term for k in kws]},
        "ceiling": rep["ceiling"], "gaps": rep["genuine_gaps"],
        "final": {"literal": final["score"], "stemmed": final["score_stemmed"], "band": final["band"],
                  "pages": final["pages"], "misses": final["misses"], "wording_misses": final["wording_misses"]},
        "attempts": len(rep["attempts"]),
        "rephrase": {**{k: chosen["rephrase"].get(k, 0) for k in ("proposed", "accepted", "blocked", "calls")},
                     "coverage": rep["rephrase_coverage"]},
        "selection": chosen["selection"], "violations": chosen["violations"],
        "profile": rep.get("profile") or {},
        "letter": {"status": rep["letter"]["status"], "problems": rep["letter"]["problems"],
                   "score": rep["letter"]["score"], "pages": rep["letter"]["pages"],
                   "evidence": [e["id"] for e in rep["letter"].get("evidence", [])]},
        "usage": _usage(rep), "seconds": round(seconds, 1),
        "manual_ats": entry.get("manual_ats"), "reference": reference, "outcome": entry.get("outcome"),
        "out_dir": rep["out_dir"],
    }


def stub(entry: dict, status: str, why: str, seconds: float = 0.0) -> dict:
    return {"id": entry["id"], "company": entry["company"], "title": entry["title"], "status": status, "error": why,
            "seconds": round(seconds, 1), "manual_ats": entry.get("manual_ats"), "outcome": entry.get("outcome")}


# ------------------------------------------------------------------ aggregates
def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1                     # ties share their average rank
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho (Pearson on average ranks); None with fewer than 3 pairs or no variation on either side."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sx = sum((a - mx) ** 2 for a in rx) ** 0.5
    sy = sum((b - my) ** 2 for b in ry) ** 0.5
    if not sx or not sy:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def aggregates(rows: list[dict], settings: Settings) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    lo = settings["scoring"]["band"][0]
    scoped = [r for r in ok if r["ceiling"] >= lo]
    proposed = sum(r["rephrase"]["proposed"] for r in ok)
    blocked = sum(r["rephrase"]["blocked"] for r in ok)
    eligible = sum(r["rephrase"]["coverage"]["eligible"] for r in ok)
    rephrased = sum(r["rephrase"]["coverage"]["rephrased"] for r in ok)
    manual = [r for r in ok if r.get("manual_ats") is not None]
    refs = [r for r in ok if r.get("reference") is not None]
    basis, base_rows = (("reference", refs) if len(refs) >= 4 else ("manual", manual) if len(manual) >= 3 else (None, []))
    key = {"reference": "reference", "manual": "manual_ats"}.get(basis)
    rate = blocked / proposed if proposed else None
    return {
        "jobs": len(rows), "ok": len(ok), "errors": sum(r["status"] == "error" for r in rows),
        "not_run": sum(r["status"] == "skipped_budget" for r in rows),
        "guard": {"proposed": proposed, "blocked": blocked, "rate": rate,
                  "investigate": rate is not None and rate > GUARD_INVESTIGATE},
        "band": {"in_scope": len(scoped), "in_band": sum(r["final"]["band"] == "in" for r in scoped),
                 "rate": (sum(r["final"]["band"] == "in" for r in scoped) / len(scoped)) if scoped else None},
        "headroom": _mean(r["ceiling"] - r["final"]["literal"] for r in scoped),
        "manual": {"n": len(manual), "mean_abs_diff": _mean(abs(r["final"]["literal"] - r["manual_ats"]) for r in manual),
                   "rho": spearman([r["final"]["literal"] for r in manual], [r["manual_ats"] for r in manual])},
        "reference": {"n": len(refs), "mean_delta": _mean(r["final"]["literal"] - r["reference"] for r in refs)},
        "rank": {"basis": basis, "n": len(base_rows),
                 "rho": spearman([r["final"]["literal"] for r in base_rows], [r[key] for r in base_rows])
                 if basis else None},
        "cost": {"calls_per_job": _mean(r["usage"]["calls"] for r in ok), "seconds_per_job": _mean(r["seconds"] for r in ok),
                 "tokens_per_job": _mean(r["usage"]["total_tokens"] for r in ok),
                 "tokens_total": sum(r["usage"]["total_tokens"] for r in ok),
                 "max_prompt": max([r["usage"]["max_prompt"] for r in ok if r["usage"]["max_prompt"] is not None],
                                   default=None)},
        "rephrase": {"eligible": eligible, "rephrased": rephrased, "ratio": rephrased / eligible if eligible else None},
        "letters": {"ok": sum(r["letter"]["status"] == "ok" for r in ok),
                    "fallback": sum(r["letter"]["status"] != "ok" for r in ok)},
        "violations": sum(len(r["violations"]) for r in ok),
        "keywords_per_job": _mean(r["keywords"]["n"] for r in ok),
        "mean_final": _mean(r["final"]["literal"] for r in ok), "mean_ceiling": _mean(r["ceiling"] for r in ok),
    }


# ------------------------------------------------------------------ the report
def pct(x: float | None, digits: int = 0) -> str:
    return "–" if x is None else f"{x:.{digits}%}"


def num(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{x:,.{digits}f}"


def _selection(sel: dict) -> str:
    return " ".join(f"{k}:{len(v)}" for k, v in sel.items())


def _row_line(r: dict) -> str:
    if r["status"] != "ok":
        return f"| {r['id']} | {r['status']} | | | | | | | | | | {r.get('error', '')[:60]} |"
    cov = r["rephrase"]["coverage"]
    return (f"| {r['id']} | T{r['triage']['tier'] or '-'} | {r['keywords']['n']} | {pct(r['ceiling'])} | "
            f"{pct(r['final']['literal'])} ({r['final']['band']}) | "
            f"{cov['rephrased']}/{cov['eligible']} | {r['rephrase']['blocked']}/{r['rephrase']['proposed']} | "
            f"{len(r['violations'])} | {r['letter']['status']} | {r['usage']['calls']} | "
            f"{r['usage']['total_tokens']:,} | {r['seconds']:.0f} |")


def render_report(meta: dict, rows: list[dict], agg: dict, compare: list[str] | None = None) -> str:
    g, b, c = agg["guard"], agg["band"], agg["cost"]
    out = [f"# Calibration report {meta['stamp']}", "",
           f"Model(s): {', '.join(meta['models'])} · temperature {meta['temperature']} · seed {meta['seed']} · "
           f"band {meta['band'][0]:.0%}-{meta['band'][1]:.0%} · {agg['ok']} of {agg['jobs']} JD(s) measured"
           + (f" · {agg['not_run']} not run (daily token budget)" if agg["not_run"] else "")
           + (f" · {agg['errors']} error(s)" if agg["errors"] else ""), "",
           "## Per JD", "",
           "| id | tier | kw | ceiling | final (band) | rephrased/eligible | blocked/proposed | violations | letter | "
           "calls | tokens | s |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    out += [_row_line(r) for r in rows]
    rho = agg["rank"]
    out += ["", "## Aggregates", "", "| Metric | Value | Target |", "|---|---|---|",
            f"| Guard block rate | {pct(g['rate'])} ({g['blocked']}/{g['proposed']} rephrases)"
            f"{' ← investigate' if g['investigate'] else ''} | report; investigate if > {GUARD_INVESTIGATE:.0%} |",
            f"| Band hit rate | {b['in_band']}/{b['in_scope']} ({pct(b['rate'])}) | ≥ 3 of 4 (JDs whose ceiling ≥ band floor) |",
            f"| Headroom left | {pct(agg['headroom'], 1)} (mean ceiling − final, same JDs) | report |",
            f"| Rephrase coverage | {agg['rephrase']['rephrased']}/{agg['rephrase']['eligible']} "
            f"({pct(agg['rephrase']['ratio'])}) | report |",
            f"| Keywords per JD | {num(agg['keywords_per_job'])} | ≤ {meta['max_keywords']} |",
            f"| Letters | {agg['letters']['ok']} ok, {agg['letters']['fallback']} fallback | all ok |",
            f"| Violations (chosen attempts) | {agg['violations']} | report |",
            f"| Mean final / ceiling | {pct(agg['mean_final'])} / {pct(agg['mean_ceiling'])} | |",
            f"| Manual agreement | mean absolute difference to manual {pct(agg['manual']['mean_abs_diff'], 1)} over "
            f"{agg['manual']['n']} JD(s); Spearman ρ {num(agg['manual']['rho'], 2)} | report (see note) |",
            f"| Reference CV | mean pipeline − reference {pct(agg['reference']['mean_delta'], 1)} over "
            f"{agg['reference']['n']} JD(s) | report |",
            f"| Rank agreement | ρ {num(rho['rho'], 2)} against {rho['basis'] or 'nothing (too few scores)'} "
            f"(n = {rho['n']}) | sanity check, not proof |",
            f"| Cost | {num(c['calls_per_job'])} calls, {num(c['seconds_per_job'], 0)} s, "
            f"{num(c['tokens_per_job'], 0)} tokens per JD; {c['tokens_total']:,} tokens in all; largest prompt "
            f"{c['max_prompt'] or '–'} tokens | report |", "",
            "In-scope = the honest ceiling reaches the band floor. Manual scores came from per-role keyword lists, so "
            "absolute differences are not like-for-like; rank order is the meaningful comparison. The reference CV is "
            "scored with the pipeline's keywords. With n = 5, ρ is only significant (one-tailed α = 0.05) at ρ ≥ 0.90."]
    out += ["", "## Detail", ""]
    for r in rows:
        out += [f"### {r['id']} — {r['company']}, {r['title']}", ""]
        if r["status"] != "ok":
            out += [f"{r['status']}: {r.get('error', '')}", ""]
            continue
        t = r["triage"]
        out += [f"- triage: {'passed' if t['passed'] else 'would be skipped'}"
                + (f" ({'; '.join(t['reasons'])})" if t["reasons"] else "")
                + (f", flags: {'; '.join(t['flags'])}" if t["flags"] else "")
                + f" · family {t['family'] or '-'} · pre-ceiling {pct(t['ceiling_pre'])}",
                f"- keywords ({r['keywords']['n']}, {r['keywords']['required']} required): "
                + ", ".join(r["keywords"]["terms"]),
                f"- score: ceiling {pct(r['ceiling'])}, final {pct(r['final']['literal'])} "
                f"(stemmed {pct(r['final']['stemmed'])}), {r['attempts']} attempt(s), {r['final']['pages']} page(s); "
                f"missed: {', '.join(r['final']['misses']) or '–'}; wording only: "
                f"{', '.join(r['final']['wording_misses']) or '–'}; genuine gaps: {', '.join(r['gaps']) or '–'}",
                f"- selection: {json.dumps(r['selection'])}",
                f"- profile facts: {len(r['profile'].get('facts', []))} picked"
                + (", retried" if r["profile"].get("retried") else "")
                + (", fell back to the facts" if r["profile"].get("fallback") else ""),
                f"- violations ({len(r['violations'])}): " + (" | ".join(r["violations"]) or "none"),
                f"- letter: {r['letter']['status']}, score {pct(r['letter']['score'])}, evidence "
                f"{', '.join(r['letter']['evidence']) or '–'}; problems: " + (" | ".join(r["letter"]["problems"]) or "none"),
                f"- tokens: {r['usage']['total_tokens']:,} in {r['usage']['calls']} calls; "
                + ", ".join(f"{t} {v['calls']}× (largest prompt {v['max_prompt']})"
                            for t, v in r["usage"]["by_task"].items())]
        extras = [f"manual {pct(r['manual_ats'], 1)}" if r.get("manual_ats") is not None else "",
                  f"reference {pct(r['reference'], 1)}" if r.get("reference") is not None else "",
                  f"outcome {r['outcome']}" if r.get("outcome") else ""]
        if any(extras):
            out.append("- " + ", ".join(e for e in extras if e))
        out.append("")
    if compare:
        out += compare
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ comparing two runs
def _delta(a, b, fmt: Callable[[float], str]) -> str:
    if a is None or b is None:
        return "–"
    d = b - a
    return ("+" if d > 0 else "") + fmt(d) if abs(d) > 1e-9 else "="


def compare_lines(prev: dict, rows: list[dict], agg: dict) -> list[str]:
    """A markdown section setting this run beside a previous report's JSON: aggregates, then every JD both ran."""
    pa, na = prev["aggregates"], agg
    metrics = [
        ("Mean final", pa["mean_final"], na["mean_final"], pct), ("Mean ceiling", pa["mean_ceiling"], na["mean_ceiling"], pct),
        ("Band hit rate", pa["band"]["rate"], na["band"]["rate"], pct),
        ("Headroom left", pa["headroom"], na["headroom"], lambda x: pct(x, 1)),
        ("Guard block rate", pa["guard"]["rate"], na["guard"]["rate"], pct),
        ("Rephrase coverage", pa["rephrase"]["ratio"], na["rephrase"]["ratio"], pct),
        ("Keywords per JD", pa["keywords_per_job"], na["keywords_per_job"], num),
        ("Letters ok", pa["letters"]["ok"], na["letters"]["ok"], lambda x: f"{x:g}"),
        ("Violations", pa["violations"], na["violations"], lambda x: f"{x:g}"),
        ("Calls per JD", pa["cost"]["calls_per_job"], na["cost"]["calls_per_job"], num),
        ("Tokens per JD", pa["cost"]["tokens_per_job"], na["cost"]["tokens_per_job"], lambda x: num(x, 0)),
        ("Seconds per JD", pa["cost"]["seconds_per_job"], na["cost"]["seconds_per_job"], lambda x: num(x, 0)),
        ("Spearman ρ (rank)", pa["rank"]["rho"], na["rank"]["rho"], lambda x: num(x, 2))]
    out = [f"## Compared with {prev['meta']['stamp']}", "", "| Metric | Before | Now | Δ |", "|---|---|---|---|"]
    for name, a, b, fmt in metrics:
        out.append(f"| {name} | {fmt(a) if a is not None else '–'} | {fmt(b) if b is not None else '–'} | "
                   f"{_delta(a, b, fmt)} |")
    before = {r["id"]: r for r in prev["rows"] if r["status"] == "ok"}
    out += ["", "| id | final | ceiling | rephrased | violations | letter | tokens |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        p = before.get(r["id"])
        if r["status"] != "ok" or p is None:
            out.append(f"| {r['id']} | {'not in the earlier run' if p is None else r['status']} | | | | | |")
            continue
        out.append(
            f"| {r['id']} | {pct(p['final']['literal'])} → {pct(r['final']['literal'])} | "
            f"{pct(p['ceiling'])} → {pct(r['ceiling'])} | "
            f"{p['rephrase']['coverage']['rephrased']}/{p['rephrase']['coverage']['eligible']} → "
            f"{r['rephrase']['coverage']['rephrased']}/{r['rephrase']['coverage']['eligible']} | "
            f"{len(p['violations'])} → {len(r['violations'])} | {p['letter']['status']} → {r['letter']['status']} | "
            f"{p['usage']['total_tokens']:,} → {r['usage']['total_tokens']:,} |")
    gone = [i for i in before if i not in {r["id"] for r in rows}]
    if gone:
        out += ["", "Only in the earlier run: " + ", ".join(gone)]
    return out + [""]


def load_previous(path: Path) -> dict:
    """A previous report's JSON sidecar, given the .md or the .json path."""
    p = path.with_suffix(".json")
    if not p.is_file():
        raise ManifestError(f"no JSON sidecar for {path} (looked for {p}); --compare needs a report written by "
                            "`careerops calibrate`")
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ rubric
def write_rubric(path: Path, ids: list[str]) -> int:
    """Create the rubric, or add rows for ids it lacks; ratings already filled in are never touched. Returns rows added."""
    rows: list[dict] = []
    columns = RUBRIC_COLUMNS
    if path.is_file():
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or RUBRIC_COLUMNS
            rows = list(reader)
    have = {r.get("id") for r in rows}
    new = [i for i in ids if i not in have]
    rows += [{c: (i if c == "id" else "") for c in columns} for i in new]
    if new or not path.is_file():
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)
    return len(new)


def latest_report(cal_dir: Path) -> Path | None:
    reports = sorted(cal_dir.glob("report-*.md"))
    return reports[-1] if reports else None


def _yn(rows: list[dict], col: str, want: str) -> int:
    return sum((r.get(col) or "").strip().upper() == want for r in rows)


def summarise(cal_dir: Path) -> Path:
    """Merge the filled-in rubric into the latest report (replacing an earlier merge) and return that report's path."""
    report = latest_report(cal_dir)
    if report is None:
        raise ManifestError(f"no report-*.md in {cal_dir}; run `careerops calibrate` first")
    rubric = cal_dir / "rubric.csv"
    if not rubric.is_file():
        raise ManifestError(f"{rubric} does not exist; run `careerops calibrate` first")
    with open(rubric, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if any((v or "").strip() for k, v in r.items() if k != "id")]
    data = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in data["rows"]}
    rated = [r for r in rows if r["id"] in by_id]
    lines = [RUBRIC_MARK, "", f"## Rubric ({len(rated)} of {len(by_id)} JD(s) rated)", "",
             "| id | final | letter | CV false claim | CV relevance | CV readability | letter false claim | "
             "letter tone | would send | notes |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rated:
        m = by_id[r["id"]]
        lines.append(f"| {r['id']} | {pct(m['final']['literal']) if m['status'] == 'ok' else '–'} | "
                     f"{m['letter']['status'] if m['status'] == 'ok' else '–'} | "
                     + " | ".join((r.get(c) or "").strip() or "–" for c in RUBRIC_COLUMNS[1:]) + " |")
    a = data["aggregates"]
    checks = [
        ("zero false claims (CV and letter)", bool(rated) and not _yn(rated, RUBRIC_COLUMNS[1], "Y")
         and not _yn(rated, RUBRIC_COLUMNS[4], "Y")),
        ("band hit rate ≥ 3 of 4 in-scope JDs", a["band"]["rate"] is not None and a["band"]["rate"] >= 0.75),
        ("no letter with rule problems", a["letters"]["fallback"] == 0 and a["ok"] > 0),
        ("would_send = Y for at least 3 JDs", _yn(rated, RUBRIC_COLUMNS[6], "Y") >= 3)]
    lines += ["", "Exit criteria (M5): " + "; ".join(f"{'✓' if ok else '✗'} {name}" for name, ok in checks), ""]
    text = report.read_text(encoding="utf-8")
    text = text.split(RUBRIC_MARK)[0].rstrip() + "\n\n" + "\n".join(lines)
    report.write_text(text, encoding="utf-8")
    return report


# ------------------------------------------------------------------ the run
def calibrate(settings: Settings, db, llm, fb: dict, manifest: Path, out_dir: Path, *, only: list[str] | None = None,
              limit: int | None = None, compare_to: Path | None = None, stamp: str | None = None,
              log: Callable[[str], None] = print) -> dict:
    """Run the manifest and write the report. `llm` is the Router (or a MockLLM); `db` is read for the sponsor index
    and, through the Router, holds the daily token counters. Returns {"report", "json", "rubric", "rows",
    "aggregates"}. The run stops early, marking the rest `skipped_budget`, when no provider has the daily tokens for
    another job."""
    entries = load_manifest(manifest, settings.root)
    if only:
        unknown = sorted(set(only) - {e["id"] for e in entries})
        if unknown:
            raise ManifestError(f"--only names ids that are not in the manifest: {', '.join(unknown)}")
        entries = [e for e in entries if e["id"] in set(only)]
    if limit:
        entries = entries[:limit]
    previous = load_previous(compare_to) if compare_to else None
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = out_dir / "runs" / stamp / "outputs"
    gates.load_roles(settings, fb)                                  # a renamed project id fails here, not per JD
    idx = sponsors.SponsorIndex(db) if db.sponsor_count() else None
    est = settings["llm"].get("job_token_estimate", 20000)
    left = [p.tokens_left() for p in getattr(llm, "providers", []) if p.tokens_left() is not None]
    log(f"Calibrating {len(entries)} JD(s), about {len(entries) * est:,} tokens "
        f"(llm.job_token_estimate {est:,} each)" + (f"; {max(left):,} left today on the provider with most room"
                                                    if left else "") + ".")
    if left and max(left) < len(entries) * est:
        log("  that is more than today's budget: the run will stop when a job no longer fits; use --limit to choose.")
    rows: list[dict] = []
    stopped = ""
    for n, entry in enumerate(entries):
        if stopped:
            rows.append(stub(entry, "skipped_budget", stopped))
            continue
        job = job_for(entry)
        verdict = gates.triage(job, settings, idx, fb)
        log(f"- {entry['id']} ({entry['company']} | {entry['title']}): triage "
            f"{'passed' if verdict.passed else 'would skip: ' + '; '.join(verdict.reasons)}")
        try:
            llm.begin_job(est)
        except BudgetExhausted as e:
            stopped = f"daily LLM token budget: {e}"
            log(f"  STOP: {stopped}\n  {len(entries) - n} JD(s) not run; re-run after the budget resets (00:00 UTC).")
            rows.append(stub(entry, "skipped_budget", stopped))
            continue
        t0 = time.monotonic()
        try:
            rep = tailor_job(job, fb, llm, settings, outputs)
            row = measure(entry, verdict, rep, time.monotonic() - t0, settings)
            f = row["final"]
            log(f"  ceiling {pct(row['ceiling'])} final {pct(f['literal'])} ({f['band']}), letter "
                f"{row['letter']['status']}, {row['usage']['calls']} calls, {row['usage']['total_tokens']:,} tokens, "
                f"{row['seconds']:.0f} s")
        except Exception as e:  # noqa: BLE001 — one JD failing must not lose the others' measurements
            row = stub(entry, "error", f"{type(e).__name__}: {e}"[:400], time.monotonic() - t0)
            log(f"  ERROR: {row['error']}")
        finally:
            llm.end_job()
        rows.append(row)
    agg = aggregates(rows, settings)
    providers = getattr(llm, "providers", None)
    meta = {"stamp": stamp, "band": settings["scoring"]["band"], "temperature": settings["llm"].get("temperature"),
            "seed": settings["llm"].get("seed"), "max_keywords": settings["scoring"].get("max_keywords"),
            "models": [p.cfg.get("model", p.name) for p in providers] if providers else ["MockLLM"],
            "manifest": str(manifest)}
    md = render_report(meta, rows, agg, compare_lines(previous, rows, agg) if previous else None)
    report = out_dir / f"report-{stamp}.md"
    report.write_text(md, encoding="utf-8")
    sidecar = report.with_suffix(".json")
    sidecar.write_text(json.dumps({"meta": meta, "rows": rows, "aggregates": agg}, indent=2, default=str),
                       encoding="utf-8")
    rubric = out_dir / "rubric.csv"
    write_rubric(rubric, [e["id"] for e in load_manifest(manifest, settings.root)])
    return {"report": report, "json": sidecar, "rubric": rubric, "rows": rows, "aggregates": agg}
