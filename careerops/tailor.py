"""Analyse → tailor → guard → render → score loop for one job. Writes a report.json the review step reads."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from . import prompts as P
from .faithfulness import check, deny_hits, numbers
from .render import page_count, pdf_text, render_cv, render_letter
from .scorer import Keyword, ceiling, in_band, merge_keywords, norm_literal, score, taxonomy_keywords

BANNED_OPENERS = re.compile(r"^\s*(i am writing to|i'm writing to|i am excited|i am thrilled)", re.I)
BANNED_CLOSERS = re.compile(r"look forward to hearing from you", re.I)


# ------------------------------------------------------------------ fact bank helpers
def fact_text(fb: dict) -> str:
    parts = list(fb.get("profile_facts", []))
    for g, items in fb.get("skills", {}).items():
        parts.append(f"{g}: {', '.join(items)}")
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            parts.append(e.get("title", e.get("name", "")) + " " + e.get("stack", ""))
            for b in e["bullets"]:
                parts.append(b["text"] + " " + " ".join(b.get("terms", [])))
    return "\n".join(parts)


def bullet_index(fb: dict) -> dict[str, dict]:
    return {b["id"]: b for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60]


# ------------------------------------------------------------------ 1. JD analysis
def analyse(job: dict, llm, settings) -> tuple[list[Keyword], str]:
    sc = settings["scoring"]
    jd = job["description"] or ""
    tax = taxonomy_keywords(jd, sc["required_weight"], sc["preferred_weight"])
    hook = ""
    llm_kw: list[Keyword] = []
    try:
        out = llm.json("extract_keywords", P.KEYWORDS_SYSTEM,
                       P.KEYWORDS_USER.format(company=job["company"], title=job["title"], jd=jd[:12000]),
                       context={"fallback_required": [], "fallback_preferred": []})
        njd = norm_literal(jd)
        for req, key in ((True, "required"), (False, "preferred")):
            for t in out.get(key, []) or []:
                t = str(t).strip()
                # verbatim check — drops keywords the model invented
                if 1 < len(t) <= 40 and norm_literal(t) in njd:
                    llm_kw.append(Keyword(t, sc["required_weight"] if req else sc["preferred_weight"], req))
        hook = str(out.get("company_hook", ""))[:300]
    except Exception as e:  # noqa: BLE001 — taxonomy keywords alone are a usable fallback
        hook = ""
        llm_kw = []
        print(f"  keyword LLM failed, using taxonomy only: {e}")
    kws = merge_keywords(tax, llm_kw)[:70]
    return kws, hook


# ------------------------------------------------------------------ 2. one tailoring attempt
def _proposal(job, fb, kws, llm, feedback: str) -> dict:
    deny = ", ".join(fb.get("deny_terms", []))
    exp_txt = "\n".join(
        f"{e['id']}: {e['title']} @ {e['org']}\n" +
        "\n".join(f"  {b['id']}: {b['text']} [terms: {', '.join(b.get('terms', []))}]" for b in e["bullets"])
        for e in fb["experience"])
    prj_txt = "\n".join(
        f"{p['id']}: {p['name']} ({p.get('stack', '')})\n" +
        "\n".join(f"  {b['id']}: {b['text']} [terms: {', '.join(b.get('terms', []))}]" for b in p["bullets"])
        for p in fb["projects"])
    user = P.TAILOR_USER.format(
        title=job["title"], company=job["company"],
        keywords="\n".join(f"- {k.term} ({k.weight:g})" for k in kws),
        feedback=feedback, profile_facts="\n".join(f"- {f}" for f in fb["profile_facts"]),
        skills="; ".join(f"{g}: {', '.join(i)}" for g, i in fb["skills"].items()),
        experience=exp_txt, projects=prj_txt)
    ctx = {"keywords": [asdict(k) for k in kws], "experience": fb["experience"], "projects": fb["projects"],
           "profile_facts": fb["profile_facts"]}
    return llm.json("tailor_cv", P.TAILOR_SYSTEM.replace("{deny}", deny), user, context=ctx)


def _assemble(fb: dict, prop: dict, kws: list[Keyword]) -> tuple[dict, list[str], list[str]]:
    """Turn a proposal into a render context, applying the faithfulness guard.
    Returns (ctx, violations, warnings). Violating rephrases fall back to canonical text."""
    deny = fb.get("deny_terms", [])
    bidx = bullet_index(fb)
    vocab = fact_text(fb)
    violations, warnings = [], []
    rephrase = prop.get("rephrase") or {}

    def text_for(bid: str) -> str:
        b = bidx[bid]
        new = rephrase.get(bid)
        if not new or new.strip() == b["text"]:
            return b["text"]
        c = check(new, b["text"], b.get("terms", []), deny, kws, vocabulary=vocab)
        warnings.extend(f"{bid}: {w}" for w in c.warnings)
        if not c.ok:
            violations.extend(f"{bid}: {v}" for v in c.violations)
            return b["text"]
        return new.strip()

    exp_sel = prop.get("experience") or {}
    experience = []
    for e in fb["experience"]:
        own = {b["id"] for b in e["bullets"]}
        ids = list(dict.fromkeys(i for i in exp_sel.get(e["id"], []) if i in own))[:4] or [e["bullets"][0]["id"]]
        experience.append({**e, "bullet_ids": ids, "bullets": [text_for(i) for i in ids]})

    proj_by_id = {p["id"]: p for p in fb["projects"]}
    pids = [p for p in (prop.get("projects") or []) if p in proj_by_id][:4] or [p["id"] for p in fb["projects"][:3]]
    projects = [{**proj_by_id[p], "bullet_ids": [b["id"] for b in proj_by_id[p]["bullets"]],
                 "bullets": [text_for(b["id"]) for b in proj_by_id[p]["bullets"]]} for p in pids]

    profile = (prop.get("profile") or "").strip()
    src = " ".join(fb["profile_facts"]) + " " + " ".join(i for v in fb["skills"].values() for i in v)
    c = check(profile, src, [], deny, kws, vocabulary=vocab, max_growth=3.0) if profile else None
    if not profile or not c.ok or re.search(r"\b\d+\+?\s+years?\b", profile, re.I):
        if c and not c.ok:
            violations.extend(f"profile: {v}" for v in c.violations)
        profile = ". ".join(fb["profile_facts"][:3]) + "."

    hit_terms = {norm_literal(k.term) for k in kws}
    skills = {g: sorted(items, key=lambda s: norm_literal(s) not in hit_terms) for g, items in fb["skills"].items()}
    ctx = {"id": fb["identity"], "profile": profile, "skills": skills, "experience": experience,
           "projects": projects, "education": fb.get("education", []),
           "certifications": fb.get("certifications", [])}
    return ctx, violations, warnings


def _shrink(ctx: dict, kws: list[Keyword]) -> bool:
    """Drop the least keyword-relevant bullet (never an entry's last one). False if nothing left to drop."""
    terms = [norm_literal(k.term) for k in kws]
    worst = None
    for sec in ("projects", "experience"):
        for e in ctx[sec]:
            if len(e["bullets"]) <= 1:
                continue
            for i, b in enumerate(e["bullets"]):
                s = sum(t in norm_literal(b) for t in terms)
                if worst is None or s < worst[0]:
                    worst = (s, e, i)
    if worst is None:
        if len(ctx["projects"]) > 2:
            ctx["projects"].pop()
            return True
        return False
    _, e, i = worst
    e["bullets"].pop(i)
    e["bullet_ids"].pop(i)
    return True


# ------------------------------------------------------------------ 3. cover letter
def _letter(job, fb, kws, hook, chosen_bullets: list[str], llm, settings) -> tuple[list[str], list[str]]:
    deny = fb.get("deny_terms", [])
    max_words = settings["scoring"]["letter_max_words"]
    evidence = chosen_bullets[:5]
    source = " ".join(evidence + fb["profile_facts"]) + " " + hook + " " + job["description"]
    problems: list[str] = []
    for _ in range(2):
        try:
            out = llm.json(
                "cover_letter",
                P.LETTER_SYSTEM.replace("{max_words}", str(max_words)).replace("{deny}", ", ".join(deny)),
                P.LETTER_USER.format(title=job["title"], company=job["company"], company_hook=hook or "(none)",
                                     keywords=", ".join(k.term for k in kws[:20]),
                                     evidence="\n".join(f"- {e}" for e in evidence)),
                context={"company": job["company"], "title": job["title"], "evidence": evidence})
            paras = [p.strip() for p in out.get("paragraphs", []) if p and p.strip()]
        except Exception as e:  # noqa: BLE001
            problems.append(f"letter LLM failed: {e}")
            break
        text = " ".join(paras)
        problems = []
        if len(text.split()) > max_words:
            problems.append(f"{len(text.split())} words > {max_words}")
        if paras and BANNED_OPENERS.search(paras[0]):
            problems.append("banned opener")
        if BANNED_CLOSERS.search(text):
            problems.append("banned closer")
        problems += [f"deny-listed '{d}'" for d in deny_hits(text, deny)]
        extra = numbers(text) - numbers(source)
        if extra:
            problems.append(f"new numbers {sorted(extra)}")
        if not problems:
            return paras, []
    # deterministic fallback — always honest, never great; review will show why
    fallback = [
        f"{job['company']}'s {job['title']} role sits close to the work I have been doing most recently.",
        " ".join(evidence[:2]),
        f"I would welcome a short call to discuss where I could contribute first on the {job['title']} team.",
    ]
    return fallback, problems + ["used fallback letter"]


# ------------------------------------------------------------------ 4. the loop
def tailor_job(job: dict, fb: dict, llm, settings, out_root: Path) -> dict[str, Any]:
    sc = settings["scoring"]
    band = tuple(sc["band"])
    lo, hi = band
    kws, hook = analyse(job, llm, settings)
    ceil, gaps = ceiling(fact_text(fb), kws)
    out_dir = out_root / slug(f"{job['company']}_{job['title']}_{job['id'].split(':')[-1]}")
    report: dict[str, Any] = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                              "keywords": [asdict(k) for k in kws], "company_hook": hook,
                              "ceiling": ceil, "genuine_gaps": gaps, "attempts": [], "out_dir": str(out_dir)}

    feedback, best = "", None
    for attempt in range(1, sc["max_attempts"] + 1):
        prop = _proposal(job, fb, kws, llm, feedback)
        ctx, viol, warn = _assemble(fb, prop, kws)
        pdf = render_cv(ctx, out_dir)
        shrinks = 0
        while page_count(pdf) > sc["max_pages"] and _shrink(ctx, kws):
            pdf = render_cv(ctx, out_dir)
            shrinks += 1
        s = score(pdf_text(pdf), kws)

        # Above the band: revert rephrasings (most keyword-dense first) until back in band.
        reverted = []
        if in_band(s.literal, band) == "above":
            bidx = bullet_index(fb)
            for sec in ("experience", "projects"):
                for e in ctx[sec]:
                    for i, bid in enumerate(e["bullet_ids"]):
                        if e["bullets"][i] != bidx[bid]["text"] and s.literal > hi:
                            e["bullets"][i] = bidx[bid]["text"]
                            reverted.append(bid)
                            pdf = render_cv(ctx, out_dir)
                            s = score(pdf_text(pdf), kws)

        rec = {"attempt": attempt, "score": s.literal, "score_stemmed": s.stemmed, "band": in_band(s.literal, band),
               "pages": page_count(pdf), "shrinks": shrinks, "reverted": reverted, "violations": viol,
               "warnings": warn, "wording_misses": s.wording_misses, "misses": s.misses,
               "proposal": prop, "selection": {e["id"]: e["bullet_ids"] for e in ctx["experience"] + ctx["projects"]},
               "profile": ctx["profile"]}
        report["attempts"].append(rec)
        if best is None or abs(s.literal - (lo + hi) / 2) < abs(best[0]["score"] - (lo + hi) / 2):
            best = (rec, ctx)
        if rec["band"] == "in":
            break
        if rec["band"] == "below" and ceil < lo:
            break   # can't be fixed honestly; don't burn calls
        if len(report["attempts"]) > 1 and rec["selection"] == report["attempts"][-2]["selection"] \
                and not prop.get("rephrase") and not report["attempts"][-2]["proposal"].get("rephrase"):
            break   # model ignored the feedback — another identical attempt is wasted quota
        if rec["band"] == "below":
            fixable = [m for m in s.wording_misses + s.misses if m not in gaps]
            feedback = P.FEEDBACK_BELOW.format(score=s.literal, lo=lo, fixable=", ".join(fixable) or "(none)",
                                               gaps=", ".join(gaps) or "(none)")

    rec, ctx = best
    pdf = render_cv(ctx, out_dir)                      # re-render the chosen attempt as the final file
    final_score = score(pdf_text(pdf), kws)
    chosen = [b for e in ctx["experience"] + ctx["projects"] for b in e["bullets"]]
    paras, letter_problems = _letter(job, fb, kws, hook, chosen, llm, settings)
    letter_pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paras,
                                "date": date.today().strftime("%d %B %Y")}, out_dir)
    report.update({
        "final": {"score": final_score.literal, "score_stemmed": final_score.stemmed,
                  "band": in_band(final_score.literal, band), "pages": page_count(pdf),
                  "hits": final_score.hits, "wording_misses": final_score.wording_misses,
                  "misses": final_score.misses, "chosen_attempt": rec["attempt"]},
        "letter": {"paragraphs": paras, "problems": letter_problems,
                   "score": score(pdf_text(letter_pdf), kws).literal},
        "cv_pdf": str(pdf), "letter_pdf": str(letter_pdf), "context": ctx,
    })
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
