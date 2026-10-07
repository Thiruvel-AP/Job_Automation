"""Analyse → tailor → guard → render → score loop for one job. Writes a report.json the review step reads."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..apply.naming import file_stem
from ..discovery import gates
from ..llm.router import TaskUnavailable
from . import prompts as P
from .budget import bullet_scores
from .faithfulness import check, deny_hits, numbers
from .grounding import (FILLER_WORDS, avoid_words, grounding_problems, opening_claim_problems, rejected_words,
                        sentences)
from .jdtext import strip_benefits
from .rawlog import RawRecorder
from .letter_rules import fallback_letter, gap_display, grammar_problems, starts_with_past_verb
from .rewrite_rules import bare_keyword_problems, canonical_case, canonical_forms, filler_problems, qualifier_problems
from .selection import (JACCARD, ONE_PAGE, Layout, chunks, fill_order, layout_from, missing_keywords, select_cv,
                        selected_ids, skills_text)
from .skills import CORE_LABEL, core_skills, cv_skills, dedupe_skills   # noqa: F401 — dedupe_skills is re-exported
from .shapes import normalise_keywords, normalise_letter, normalise_profile, normalise_rephrase
from .textnorm import normalise_json
from .render import CV2_MARGIN_PT, last_line_fills, page2_fill, page_count, pdf_text, render_cv, render_letter
from .scorer import (Keyword, _contains, cap_keywords, has_literal, ceiling, drop_contained, gap_terms, in_band, merge_keywords,
                     norm_literal, norm_stem, score, taxonomy_keywords)

USAGE_SUMS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")   # summed into report llm_usage
FALLBACK_MARK = "used fallback letter"
LLM_FAILED = "LLM call failed"             # in analyse's notes when only the taxonomy keywords could be used
BANNED_OPENERS = re.compile(r"^\s*(i am writing to|i'm writing to|i am excited|i am thrilled)", re.I)
BANNED_CLOSERS = re.compile(r"look forward to hearing from you", re.I)
# a deny-listed tool may be named in the letter only to say the candidate lacks it
LACK_WORDS = re.compile(r"\b(not|no|never|yet|without|new to|limited)\b|n't", re.I)
# the letter may say the candidate lacks something only if it is in GAPS (matched on norm_literal text)
LACK_CLAIM = re.compile(r"\b(have not|haven't|never used|no experience)\b")


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
def _llm_json(llm, task: str, system: str, user: str, context: dict | None = None) -> dict:
    """llm.json with the typographic characters models like (non-breaking hyphens, curly quotes, NBSP) made plain."""
    return normalise_json(llm.json(task, system, user, context=context))


def strip_team_word(team: str) -> str:
    """The team name without a trailing "team" ("Data Science team" -> "Data Science"): the greeting and the closing
    question both add the word themselves."""
    t = re.sub(r"\s+", " ", team).strip(" .,:;")
    return re.sub(r"(?:^|\s)team$", "", t, flags=re.I).strip(" .,:;")


def clean_team(raw, job: dict) -> str:
    """The team name the model found, only if the JD really contains it (a model may not invent one)."""
    t = strip_team_word(str(raw or ""))
    if not t or len(t) > 40 or norm_literal(t) == norm_literal(job["company"]):
        return ""
    return t if norm_literal(t) in norm_literal(job["description"] or "") else ""


# Words that name nothing a recruiter's ATS would search for. A phrase containing any of GENERIC_ANYWHERE is not a
# keyword ("ML systems", "data quality", "coding tasks"); GENERIC_ALONE only disqualifies a phrase made of nothing else
# ("data" is out, "data pipelines" stays). Degree requirements are never keywords either.
GENERIC_ANYWHERE = frozenset({"tasks", "workflows", "features", "approaches", "systems", "frameworks", "patterns",
                              "skills", "sets", "quality", "generation"})
GENERIC_ALONE = frozenset({"task", "workflow", "pattern", "skill", "data", "techniques", "technique", "experience",
                           "ability", "knowledge", "understanding"}) | GENERIC_ANYWHERE
DEGREE = re.compile(r"\b(degree|bachelor'?s?|master'?s?|msc|bsc|ph\.?d|doctorate|diploma|undergraduate|postgraduate)\b",
                    re.I)
MAX_KEYWORD_WORDS = 3
_ALTERNATIVES = re.compile(r"\s+or\s+|\s*,\s*(?:or\s+)?|\s+/\s+", re.I)


def _words(phrase: str) -> list[str]:
    return [w.strip(".,;:()\"'") for w in norm_literal(phrase).split()]


def llm_keyword(raw, njd: str, weight: float, required: bool) -> Keyword | None:
    """One keyword from the model's output. "A or B or C" is one any-of keyword; every alternative must appear
    verbatim in the JD (`njd`, norm_literal'd), be 1-3 words, contain no generic word (GENERIC_ANYWHERE), not be made of
    generic words only (GENERIC_ALONE) and not be a degree requirement. None if nothing usable is left."""
    t = str(raw).strip()
    parts = [p.strip(" .;:()\"'") for p in (_ALTERNATIVES.split(t) if re.search(r"\bor\b|,|\s/\s", t, re.I) else [t])]
    ok = []
    for p in parts:
        words = _words(p)
        if (1 < len(p) <= 40 and 1 <= len(words) <= MAX_KEYWORD_WORDS and _contains(njd, norm_literal(p))
                and not GENERIC_ANYWHERE & set(words) and not all(w in GENERIC_ALONE for w in words)
                and not DEGREE.search(p)):
            ok.append(p)
    return Keyword(ok[0], weight, required, ok[1:]) if ok else None


def analyse(job: dict, llm, settings, notes: list[str] | None = None,
            frozen: dict | None = None) -> tuple[list[Keyword], str, str]:
    """(keywords, company_hook, team). `team` is "" when the JD names none. The model only suggests keywords; code
    enforces the rules: verbatim in the JD, short, nothing generic, near-duplicates merged (the shorter stays), at most
    scoring.max_keywords kept by weight then JD frequency. Shape problems in the model's answer and every drop are
    appended to `notes` when given. `frozen` ({"keywords": [Keyword], "company_hook", "team", "notes", "date"}, see
    reporting.calibrate.load_frozen) is the result of an earlier analysis: it is returned as it is, with no model call,
    so the keywords (and so the ceiling) are the same on every run and with every provider."""
    if frozen is not None:
        if notes is not None:
            notes.append(f"keywords: frozen ({frozen.get('date')}), extract_keywords not called")
            notes.extend(frozen.get("notes") or [])
        return list(frozen["keywords"]), frozen.get("company_hook") or "", frozen.get("team") or ""
    sc = settings["scoring"]
    jd, cut = strip_benefits(job["description"] or "")           # the employer's perks are not the role's keywords
    if cut and notes is not None:
        notes.append(f"keywords: {cut}")
    tax = taxonomy_keywords(jd, sc["required_weight"], sc["preferred_weight"])
    hook = team = ""
    llm_kw: list[Keyword] = []
    try:
        out = _llm_json(llm, "extract_keywords", P.KEYWORDS_SYSTEM,
                        P.KEYWORDS_USER.format(company=job["company"], title=job["title"], jd=jd[:12000]),
                        context={"fallback_required": [], "fallback_preferred": []})
        njd = norm_literal(jd)
        required, preferred, kw_notes = normalise_keywords(out)
        if notes is not None:
            notes.extend(f"extract_keywords: {n}" for n in kw_notes)
        for req, terms in ((True, required), (False, preferred)):
            for t in terms:
                k = llm_keyword(t, njd, sc["required_weight"] if req else sc["preferred_weight"], req)
                if k:                                      # verbatim, short, specific: nothing invented survives
                    llm_kw.append(k)
                elif notes is not None:
                    notes.append(f"extract_keywords: dropped keyword '{str(t)[:40]}' (not verbatim, too long, generic "
                                 "or a degree)")
        out = out if isinstance(out, dict) else {}
        hook = str(out.get("company_hook") or "")[:300]
        team = clean_team(out.get("team"), job)
    except Exception as e:  # noqa: BLE001 — taxonomy keywords alone are a usable fallback
        hook = team = ""
        llm_kw = []
        print(f"  keyword LLM failed, using taxonomy only: {e}")
        if notes is not None:
            notes.append(f"extract_keywords: {LLM_FAILED} ({str(e)[:120]}); taxonomy keywords only")
    kws, merged = drop_contained(merge_keywords(tax, llm_kw))
    kws, capped = cap_keywords(kws, jd, sc["max_keywords"])
    if notes is not None:
        notes.extend(f"keywords: {n}" for n in merged + capped)
    return kws, hook, team


# ------------------------------------------------------------------ 2. one tailoring attempt
def role_prior(job: dict, fb: dict, settings) -> tuple[str | None, int | None, str, list[str]]:
    """(family, tier, CV headline, preferred project ids) for this job. Falls back to classifying the title,
    then to identity.headline with no prior when the title is out of scope."""
    fam = gates.family(settings, job.get("family"))
    if fam is None:
        role = gates.classify_role(job.get("title", ""), settings)
        fam = gates.family(settings, role[0]) if role else None
    if fam is None:
        return None, None, fb["identity"].get("headline", ""), []
    return fam.name, fam.tier, fam.headline, list(fam.projects)


REPHRASE_BATCH = 5            # bullets per rephrase_bullets call: small enough for a small model to follow every rule
PROFILE_MIN_WORDS, PROFILE_MAX_WORDS = 6, 45     # a profile shorter than a sentence is junk, not a profile
PROFILE_FACTS = 2


def _deny_list(fb: dict) -> str:
    return ", ".join(fb.get("deny_terms", []))


# ---- 2a. rephrase: the model rewords bullets in batches, the guard decides what is kept
def unused_result(bullet: dict) -> str:
    """The bullet's optional `result:` when its text does not already say it, else ''. Never invented: it is only what
    the user wrote in the fact bank."""
    res = " ".join(str(bullet.get("result") or "").split())
    return "" if not res or norm_literal(res).rstrip(".") in norm_literal(bullet["text"]) else res


MAX_OFFERS = 2        # keywords one bullet's rephrase may be asked to place


def placement_offers(fb: dict, sel: dict, kws: list[Keyword], other_text: str = "",
                     limit: int = MAX_OFFERS) -> tuple[dict[str, list[str]], list[str]]:
    """Which missed keyword each selected bullet is asked to place. A keyword is missed when neither the selected bullets'
    canonical text nor `other_text` (profile, skills) has it literally; it is offered to the first selected bullet, in CV
    order, whose `terms` evidence it and which has fewer than `limit` offers (heaviest keyword first), so no keyword is
    asked for twice. Returns ({bullet id: [words]}, [missed keywords no selected bullet could take: no evidence or no room])."""
    bidx = bullet_index(fb)
    ids = selected_ids(fb, sel)
    cv = norm_literal(other_text + " " + " ".join(bidx[i]["text"] for i in ids))
    offers: dict[str, list[str]] = {}
    unoffered: list[str] = []
    missed = [k for k in kws if not any(has_literal(cv, norm_literal(w)) for w in k.members())]
    for k in sorted(missed, key=lambda k: -k.weight):                    # stable: the JD's order within a weight
        for bid in ids:
            word = next(iter(missing_keywords(bidx[bid], [k])), None)
            if word and len(offers.get(bid, [])) < limit:
                offers.setdefault(bid, []).append(word)
                break
        else:
            unoffered.append(k.term)
    return offers, unoffered


def propose_rephrases(job: dict, fb: dict, sel: dict, kws: list[Keyword], llm, cache: dict,
                      notes: list[str], other_text: str = "") -> tuple[dict[str, str], dict]:
    """Ask for rewordings of the selected bullets that can carry a JD keyword the CV is missing (`placement_offers`: the
    keyword is in the bullet's `terms`, at most MAX_OFFERS per bullet; `other_text` = the profile and skills text, which
    already count as having a keyword), in batches of REPHRASE_BATCH: each call gets the bullet text, its terms and the
    keywords it may introduce, and answers
    {bullet id: new text}. Returns ({bullet id: proposed text}, stats). `cache` maps (bullet id, missing words) to the
    answer, so a later attempt never pays twice for the same question; a failed batch is not cached and is noted."""
    bidx = bullet_index(fb)
    todo = []
    offers, unoffered = placement_offers(fb, sel, kws, other_text)
    for bid in selected_ids(fb, sel):
        miss = tuple(offers.get(bid, ()))
        if miss or unused_result(bidx[bid]):          # a result the user supplied may be worked in, even with no keyword
            todo.append((bid, miss))
    fresh = [t for t in todo if t not in cache]
    stats = {"eligible": len(todo), "asked": len(fresh), "cached": len(todo) - len(fresh), "calls": 0,
             "failed_calls": 0, "offered": {bid: list(m) for bid, m in offers.items()}, "unoffered": unoffered}
    system = P.REPHRASE_SYSTEM.replace("{deny}", _deny_list(fb))
    for batch in chunks(fresh, REPHRASE_BATCH):
        items = [{"id": bid, "text": bidx[bid]["text"], "terms": list(bidx[bid].get("terms", [])),
                  "introduce": list(miss), **({"result": unused_result(bidx[bid])} if unused_result(bidx[bid]) else {})}
                 for bid, miss in batch]
        lines = "\n".join(f"- id: {i['id']}\n  TEXT: {i['text']}\n  TERMS: {', '.join(i['terms']) or '(none)'}\n"
                          f"  MAY INTRODUCE: {', '.join(i['introduce']) or '(none)'}"
                          + (f"\n  RESULT: {i['result']}" if i.get("result") else "") for i in items)
        stats["calls"] += 1
        try:
            out = _llm_json(llm, "rephrase_bullets", system, P.REPHRASE_USER.format(title=job["title"], bullets=lines),
                            {"bullets": items, "title": job["title"]})
            got, shape_notes = normalise_rephrase(out, {i["id"] for i in items})
        except Exception as e:  # noqa: BLE001 — the bullets keep their canonical wording
            stats["failed_calls"] += 1
            notes.append(f"rephrase_bullets: batch of {len(batch)} failed ({str(e)[:120]}); canonical wording kept")
            continue
        notes.extend(f"rephrase_bullets: {n}" for n in shape_notes)
        for bid, miss in batch:
            cache[(bid, miss)] = got.get(bid, "")
    proposed = {bid: cache[(bid, miss)] for bid, miss in todo
                if cache.get((bid, miss)) and cache[(bid, miss)].strip() != bidx[bid]["text"]}
    return proposed, stats


NO_REGRESSION = "no-regression"       # marks the warning of a rewording refused for losing a keyword the bullet had


def lost_hits(old: str, new: str, kws: list[Keyword]) -> list[str]:
    """The keywords `old` hits literally (the ATS view, singular/plural alike) that `new` does not."""
    kept = set(score(new, kws).hits)
    return [t for t in score(old, kws).hits if t not in kept]


def guard_rephrases(fb: dict, proposed: dict[str, str], kws: list[Keyword],
                    base: dict[str, str] | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    """Run every proposed rewording through faithfulness.check against its own bullet (text + terms). Returns
    (accepted {bullet id: text}, violations, warnings); a rejected rewording simply keeps the bullet as it was.
    No-regression rule: a rewording that passes the guard is still refused when its literal keyword hits are not a superset
    of the hits of the text it replaces (`base[bid]`, default the canonical bullet; the wording pass replaces what is on
    the CV now), and a `no-regression` warning says which keywords it would lose."""
    deny, bidx, vocab = fb.get("deny_terms", []), bullet_index(fb), fact_text(fb)
    forms = canonical_forms(fb)
    accepted: dict[str, str] = {}
    violations: list[str] = []
    warnings: list[str] = []
    for bid, new in proposed.items():
        b = bidx[bid]
        # the bullet's own `result:` (the user's measured outcome) is evidence for this bullet only: its numbers are not
        # new numbers here, and no other bullet's result counts
        c = check(new, (b["text"] + " " + str(b.get("result") or "")).strip(), b.get("terms", []), deny, kws,
                  vocabulary=vocab)
        # rules that only reject more: the bullet's qualifiers survive, no filler pattern is added (see rewrite_rules)
        c.violations += (qualifier_problems(new, b["text"]) + filler_problems(new, b["text"])
                         + bare_keyword_problems(new, b["text"], kws))
        warnings.extend(f"{bid}: {w}" for w in c.warnings)
        lost = lost_hits((base or {}).get(bid, b["text"]), new, kws) if c.ok else []
        if lost:
            warnings.append(f"{bid}: {NO_REGRESSION}: kept the earlier wording, the rewrite would lose {', '.join(lost)}")
        elif c.ok:
            accepted[bid] = canonical_case(new.strip(), forms)
        else:
            violations.extend(f"{bid}: {v}" for v in c.violations)
    return accepted, violations, warnings


def wording_targets(ctx: dict, kws: list[Keyword], wording_misses: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """For each wording miss (a keyword the CV has in substance, stemmed, but not in the JD's own form), the ONE bullet on
    the CV, first in CV order, whose text carries the keyword's stem without its exact form. Returns
    ({bullet id: [exact forms to write]}, [terms no bullet carries: the stem is only in the skills or the profile])."""
    by_term = {k.term: k for k in kws}
    plan: dict[str, list[str]] = {}
    unplaced: list[str] = []
    for term in wording_misses:
        k = by_term.get(term)
        hit = None
        for sec in ("experience", "projects"):
            for e in ctx[sec]:
                for bid, text in zip(e["bullet_ids"], e["bullets"]):
                    lit, stm = norm_literal(text), norm_stem(text)
                    for w in (k.members() if k else [term]):
                        if not _contains(lit, norm_literal(w)) and _contains(stm, norm_stem(w)):
                            hit = hit or (bid, w)
        if hit:
            plan.setdefault(hit[0], []).append(hit[1])
        else:
            unplaced.append(term)
    return plan, unplaced


def wording_pass(job: dict, fb: dict, ctx: dict, kws: list[Keyword], wording_misses: list[str], llm, cache: dict,
                 notes: list[str]) -> dict:
    """After rephrasing: for each wording miss, ask the model to rewrite the one bullet that carries the keyword's stem so
    it uses the JD's exact form ("prompts", "prototyping"), in batches of REPHRASE_BATCH like any rephrase. Every rewrite
    goes through `guard_rephrases` against its canonical bullet; an accepted one replaces the bullet's text in `ctx`.
    Returns {"terms", "asked", "calls", "accepted": [ids], "blocked": [ids], "unplaced": [terms]}."""
    plan, unplaced = wording_targets(ctx, kws, wording_misses)
    stats: dict = {"terms": list(wording_misses), "asked": len(plan), "calls": 0, "accepted": [], "blocked": [],
                   "unplaced": unplaced}
    if unplaced:
        notes.append("wording: no bullet carries the stem of " + ", ".join(unplaced) + " (skills or profile only)")
    if not plan:
        return stats
    bidx = bullet_index(fb)
    current = {bid: text for sec in ("experience", "projects") for e in ctx[sec] for bid, text in
               zip(e["bullet_ids"], e["bullets"])}
    system = P.REPHRASE_SYSTEM.replace("{deny}", _deny_list(fb))
    proposed: dict[str, str] = {}
    todo = [(bid, tuple(forms)) for bid, forms in plan.items()]
    for batch in chunks([t for t in todo if ("wording", t[0], t[1], current[t[0]]) not in cache], REPHRASE_BATCH):
        items = [{"id": bid, "text": current[bid], "terms": list(bidx[bid].get("terms", [])), "introduce": list(forms),
                  "exact_form": list(forms)} for bid, forms in batch]
        lines = "\n".join(f"- id: {i['id']}\n  TEXT: {i['text']}\n  TERMS: {', '.join(i['terms']) or '(none)'}\n"
                          f"  MAY INTRODUCE: {', '.join(i['introduce'])}\n"
                          f"  EXACT FORM: {', '.join(i['exact_form'])}" for i in items)
        stats["calls"] += 1
        try:
            out = _llm_json(llm, "rephrase_bullets", system, P.REPHRASE_USER.format(title=job["title"], bullets=lines),
                            {"bullets": items, "title": job["title"]})
            got, shape_notes = normalise_rephrase(out, {i["id"] for i in items})
        except Exception as e:  # noqa: BLE001 — the bullets keep their wording
            notes.append(f"wording: batch of {len(batch)} failed ({str(e)[:120]}); wording kept")
            continue
        notes.extend(f"wording: {n}" for n in shape_notes)
        for bid, forms in batch:
            cache[("wording", bid, forms, current[bid])] = got.get(bid, "")
    for bid, forms in todo:
        new = cache.get(("wording", bid, forms, current[bid]), "")
        if new and new.strip() != current[bid]:
            proposed[bid] = new
    accepted, _viol, warn = guard_rephrases(fb, proposed, kws, base=current)
    notes.extend(f"wording: {w}" for w in warn if f": {NO_REGRESSION}: " in w)
    stats["accepted"] = sorted(accepted)
    stats["blocked"] = sorted(set(proposed) - set(accepted))
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for i, bid in enumerate(e["bullet_ids"]):
                if bid in accepted:
                    e["bullets"][i] = accepted[bid]
    return stats


def placement_report(offered: dict[str, list[str]], unoffered: list[str], proposed: dict[str, str],
                     accepted: dict[str, str], ctx: dict) -> dict:
    """What became of each keyword a bullet was asked to place: `placed` (the bullet on the CV now has it literally),
    `blocked` (the model's rewording was refused by the guard or the no-regression rule) or `not_placed` (the model left
    it out, or it was reverted later). `unoffered`: missed keywords no selected bullet evidences or has room for."""
    cur = {bid: text for sec in ("experience", "projects") for e in ctx[sec] for bid, text in
           zip(e["bullet_ids"], e["bullets"])}
    out: dict = {"placed": [], "not_placed": [], "blocked": [], "unoffered": list(unoffered)}
    for bid, words in offered.items():
        for w in words:
            if has_literal(norm_literal(cur.get(bid, "")), norm_literal(w)):
                out["placed"].append(w)
            elif bid in proposed and bid not in accepted:
                out["blocked"].append(w)
            else:
                out["not_placed"].append(w)
    return out


def rephrase_coverage(ctx: dict, fb: dict, kws: list[Keyword]) -> dict:
    """Of the selected bullets that could carry a JD keyword's wording (the bullet's terms evidence it, its text does
    not say it), how many were reworded? {"eligible", "rephrased", "ratio"}; ratio is None with nothing eligible."""
    bidx = bullet_index(fb)
    eligible = rephrased = 0
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for bid, text in zip(e["bullet_ids"], e["bullets"]):
                if missing_keywords(bidx[bid], kws):
                    eligible += 1
                    rephrased += text != bidx[bid]["text"]
    return {"eligible": eligible, "rephrased": rephrased, "ratio": rephrased / eligible if eligible else None}


# ---- 2b. profile: the model picks two profile_facts and words them; the guard sees those two facts only
def pick_facts(facts: list[str], kws: list[Keyword], have: list[int], n: int = PROFILE_FACTS) -> list[int]:
    """`have` (fact indexes the model picked) topped up to `n` with the facts that overlap the JD keywords most
    (fact-bank order on ties). Code, not model: used when the model picked too few valid facts."""
    sc = {i: score(f, kws).stemmed for i, f in enumerate(facts)}
    rest = sorted((i for i in range(len(facts)) if i not in have), key=lambda i: (-sc[i], i))
    return (list(have) + rest)[:n]


def _profile_retrier(llm, fb: dict, headline: str) -> Callable[[str, list[str], str], str]:
    """The one retry a profile that fails the guard gets: the model sees what was wrong and the facts it may use."""
    deny = _deny_list(fb)

    def retry(previous: str, problems: list[str], source: str) -> str:
        out = _llm_json(llm, "tailor_profile", P.PROFILE_RETRY_SYSTEM.replace("{deny}", deny),
                        P.PROFILE_RETRY_USER.format(headline=headline or fb["identity"].get("headline", ""),
                                                    previous=previous or "(none)", problems="; ".join(problems),
                                                    source=source),
                        {"profile_facts": fb["profile_facts"], "previous": previous, "problems": problems,
                         "headline": headline})
        return str(out.get("profile") or "")

    return retry


def make_profile(job: dict, fb: dict, kws: list[Keyword], headline: str, llm,
                 retry: Callable[[str, list[str], str], str] | None = None) -> dict:
    """The CV profile: the model picks two of `profile_facts` and writes <= 45 words from them; the text is checked
    against those two facts only (no skills, no bullets). A draft that fails the guard is rewritten once by `retry`,
    then the two facts themselves are used. Returns {"text", "facts", "violations", "warnings", "retried", "fallback"}."""
    facts = list(fb["profile_facts"])
    deny = fb.get("deny_terms", [])
    vocab = fact_text(fb)
    headline = headline or fb["identity"].get("headline", "")
    retry = retry or _profile_retrier(llm, fb, headline)
    warnings: list[str] = []
    picked: list[int] = []
    text = ""
    try:
        out = _llm_json(llm, "tailor_profile", P.PROFILE_SYSTEM.replace("{deny}", _deny_list(fb)),
                        P.PROFILE_USER.format(title=job["title"], company=job["company"], headline=headline,
                                              facts="\n".join(f"F{i}: {f}" for i, f in enumerate(facts, 1))),
                        {"facts": [{"id": f"F{i}", "text": f} for i, f in enumerate(facts, 1)],
                         "headline": headline, "title": job["title"],
                         "keywords": [k.term for k in kws]})
        picked, text, notes = normalise_profile(out, facts)
        warnings.extend(f"tailor_profile: {n}" for n in notes)
    except Exception as e:  # noqa: BLE001 — the code-picked facts are a usable profile
        warnings.append(f"profile: LLM failed ({str(e)[:120]})")
    if len(picked) < min(PROFILE_FACTS, len(facts)):
        picked = pick_facts(facts, kws, picked)
        warnings.append("profile: facts picked by JD overlap (the model named fewer than two valid ones)")
    source = headline + ". " + " ".join(facts[i] for i in picked)     # the headline is the user's own role title

    def problems(t: str) -> list[str]:
        if not t:
            return ["no profile"]
        probs = check(t, source, [], deny, kws, vocabulary=vocab, max_growth=3.0).violations
        if len(t.split()) > PROFILE_MAX_WORDS:
            probs.append(f"{len(t.split())} words > {PROFILE_MAX_WORDS}")
        if len(t.split()) < PROFILE_MIN_WORDS:
            probs.append(f"{len(t.split())} words < {PROFILE_MIN_WORDS}")
        if re.search(r"\b\d+\+?\s+years?\b", t, re.I):
            probs.append("years-of-experience claim")
        return probs

    violations: list[str] = []
    retried = fallback = False
    probs = problems(text)
    if probs:
        violations.extend(f"profile: {p}" for p in probs if p != "no profile")
        newer = ""
        try:
            newer = retry(text, probs, "\n".join(facts[i] for i in picked)).strip()
            retried = True
        except Exception as e:  # noqa: BLE001 — a failed retry just means the fallback
            warnings.append(f"profile: retry failed ({str(e)[:120]})")
        again = problems(newer) if newer else []
        violations.extend(f"profile (retry): {p}" for p in again if newer)
        if newer and not again:
            text = newer
            warnings.append("profile: rewritten once after the guard rejected the first draft")
        else:
            text = ". ".join(f.rstrip(".") for f in (facts[i] for i in picked)) + "."
            fallback = True
            warnings.append("profile: fell back to the picked profile_facts")
    return {"text": text, "facts": [facts[i] for i in picked], "violations": violations, "warnings": warnings,
            "retried": retried, "fallback": fallback}


# ---- 2c. assemble and fit
def _context(fb: dict, sel: dict, texts: dict[str, str], profile: str, kws: list[Keyword], headline: str = "",
             skills: dict[str, list[str]] | None = None) -> dict:
    """The render context for a selection: canonical bullet text unless the guard accepted a rewording (`texts`).
    `skills` = the rows to show (`cv_skills`); default every row, `core` first."""
    bidx = bullet_index(fb)

    def text(bid: str) -> str:
        return texts.get(bid) or bidx[bid]["text"]

    experience = []
    for e in fb["experience"]:
        ids = sel["experience"].get(e["id"]) or [e["bullets"][0]["id"]]
        experience.append({**e, "bullet_ids": list(ids), "bullets": [text(i) for i in ids]})
    by_id = {p["id"]: p for p in fb["projects"]}
    chosen = sel.get("project_bullets") or {}
    projects = []
    for p in sel["projects"]:
        if p in by_id:
            ids = list(chosen.get(p) or [b["id"] for b in by_id[p]["bullets"]])
            projects.append({**by_id[p], "bullet_ids": ids, "bullets": [text(i) for i in ids]})
    if skills is None:
        skills = cv_skills(fb, kws)[0]
    return {"id": {**fb["identity"], "headline": headline or fb["identity"].get("headline", "")}, "profile": profile,
            "skills": skills, "experience": experience, "projects": projects, "education": fb.get("education", []),
            "certifications": fb.get("certifications", [])}


def letter_summary(letter: dict) -> str:
    """"ok", or "fallback (<first rejection reason>)" for a letter that fell back to the deterministic text."""
    if letter.get("status") == "ok":
        return "ok"
    if letter.get("status") == "pending":
        return f"pending ({letter.get('reason') or 'no provider for it'})"
    why = [p for p in letter.get("problems") or [] if p != FALLBACK_MARK]
    return f"{letter.get('status')} ({why[0] if why else 'no reason recorded'})"


def _shrink(ctx: dict, kws: list[Keyword], layout: Layout = ONE_PAGE) -> bool:
    """Drop the least keyword-relevant bullet. While anything else can go, the newest experience entry keeps
    `layout.first[0]` bullets, the other entries `layout.other[0]` and a project `layout.project_bullets[0]` (one page:
    2 / 1 / 1); then every entry may fall to its last bullet. False if nothing is left to drop."""
    terms = [[norm_literal(w) for w in k.members()] for k in kws]
    first = ctx["experience"][0] if ctx["experience"] else None
    floor_proj = layout.project_bullets[0] if layout.project_bullets else 1
    for strict in (True, False):
        worst = None
        for sec in ("projects", "experience"):
            for e in ctx[sec]:
                floor = 1
                if strict:
                    floor = floor_proj if sec == "projects" else layout.first[0] if e is first else layout.other[0]
                if len(e["bullets"]) <= max(1, floor):
                    continue
                for i, b in enumerate(e["bullets"]):
                    s = sum(any(t in norm_literal(b) for t in group) for group in terms)
                    if worst is None or s < worst[0]:
                        worst = (s, e, i)
        if worst:
            _, e, i = worst
            e["bullets"].pop(i)
            e["bullet_ids"].pop(i)
            return True
    return False


# ---- 2d. two pages: keep adding bullets until page 2 is full enough
LEADING_PT = 13.2                                  # the 11pt body of cv2.tex.j2
LINE_SHARE = LEADING_PT / (841.89 - 2 * CV2_MARGIN_PT)     # one text line as a share of the text area
CHARS_PER_LINE = 92                                # a bullet line of the 11pt body, to guess how big a bullet is


def _bullet_share(text: str) -> float:
    """A guess of the share of a page's text area one bullet takes. It only sizes the batch to render; the rendered PDF
    decides whether the page is full."""
    return max(1, math.ceil(len(text) / CHARS_PER_LINE)) * LINE_SHARE


def _used(pdf: Path, pages: int) -> float:
    """Pages used so far: whole pages before the last, plus how far down the last one the text reaches."""
    fills = last_line_fills(pdf)
    return (pages - 1) + (fills[pages - 1] if len(fills) >= pages else 0.0)


def fill_page2(ctx: dict, fb: dict, order: list[str], texts: dict[str, str], rerender: Callable[[], tuple[Path, int]],
               min_fill: float, skip: set[str] | None = None) -> dict:
    """Add bullets from `order` (selection.fill_order) until page 2 is at least `min_fill` full or the bullets run out,
    never past two pages. Each round adds as many bullets as the size guess says it needs, renders, and if that tipped the
    CV onto a third page takes them back, last first (those bullets are not tried again). `rerender()` renders `ctx` and
    returns (pdf, pages). Returns {"added", "rejected_overflow", "renders", "reached"}."""
    bidx = bullet_index(fb)
    owner = {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    present = {i for sec in ("experience", "projects") for e in ctx[sec] for i in e["bullet_ids"]}
    queue = [b for b in order if b not in present and b not in (skip or set())]
    stats = {"added": [], "rejected_overflow": [], "renders": 0, "reached": False}
    target = 1.0 + min_fill

    projects_by_id = {p["id"]: p for p in fb["projects"]}

    def entry(bid: str) -> dict:
        found = next((e for sec in ("experience", "projects") for e in ctx[sec] if e.get("id") == owner[bid]), None)
        if found is None:                                 # a bullet of a project not on the CV yet brings its heading
            found = {**projects_by_id[owner[bid]], "bullet_ids": [], "bullets": []}
            ctx["projects"].append(found)
        return found

    pdf, pages = rerender()
    stats["renders"] += 1
    used = _used(pdf, pages) if pages <= 2 else 3.0
    while queue and used < target and pages <= 2:
        batch, est = [], used
        for bid in list(queue):
            share = _bullet_share(texts.get(bid) or bidx[bid]["text"])
            if est + share > 2.0:
                continue                                  # would not fit even by the guess: a shorter one may
            batch.append(bid)
            est += share
            if est >= target:
                break
        if not batch:
            break
        queue = [q for q in queue if q not in batch]
        for bid in batch:
            e = entry(bid)
            e["bullet_ids"].append(bid)
            e["bullets"].append(texts.get(bid) or bidx[bid]["text"])
        pdf, pages = rerender()
        stats["renders"] += 1
        while pages > 2 and batch:                        # overshot onto a third page: take the last one back
            bid = batch.pop()
            e = entry(bid)
            e["bullet_ids"].pop()
            e["bullets"].pop()
            if not e["bullet_ids"]:                       # a project added only by this bullet goes with it
                ctx["projects"][:] = [p for p in ctx["projects"] if p is not e]
            stats["rejected_overflow"].append(bid)
            pdf, pages = rerender()
            stats["renders"] += 1
        stats["added"] += batch
        used = _used(pdf, pages) if pages <= 2 else 3.0
    stats["reached"] = pages == 2 and page2_fill(pdf, pages) >= min_fill
    return stats


def _keyword_boost(kws: list[Keyword], fixable: set[str]) -> list[Keyword]:
    """The keywords with the ones the fact bank evidences but the CV missed counted double when ranking bullets: how a
    below-band attempt differs from the last (selection is deterministic, so an identical retry would be wasted)."""
    return [Keyword(k.term, k.weight * 2, k.required, list(k.alts)) if k.term in fixable else k for k in kws]


# ------------------------------------------------------------------ 3. cover letter
def required_gaps(gaps: list[str], kws: list[Keyword]) -> list[str]:
    """GAPS for the letter: required JD keywords the fact bank has no evidence for, and only tools, platforms,
    languages and frameworks (taxonomy category). A broad skill (statistics, communication, research) is never a gap:
    its literal term being absent from the fact bank says nothing about whether the candidate has the skill."""
    required = {k.term for k in kws if k.required}
    eligible = gap_terms()
    return [g for g in gaps if g in required and norm_literal(g) in eligible]


def letter_greeting(settings, company: str, team: str = "") -> str:
    """'Hi <company> <team> team' when the JD names a team, else 'Dear <company> hiring team' (settings → letter)."""
    cfg = settings["letter"]
    team = strip_team_word(team)
    if team:
        return cfg["greeting"].format(company=company, team=f" {team}")
    return cfg["greeting_fallback"].format(company=company)


def letter_closing(settings, company: str, team: str = "") -> str:
    """The closing question, written from the settings template and never by the model (settings -> letter)."""
    cfg, team = settings["letter"], strip_team_word(team)
    return cfg["closing"].format(company=company, team=team) if team else cfg["closing_fallback"].format(company=company)


def _deny_problems(text: str, deny: list[str], gaps: list[str]) -> list[str]:
    """Deny-listed terms in the letter. One that is also a required gap may appear, but only in sentences that say
    the candidate lacks it ('I have not used X yet'); any other mention, or any other deny-listed term, is a problem."""
    allowed = {d for g in gaps for d in deny_hits(g, deny)}
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for d in deny_hits(text, deny):
        if d in allowed and all(LACK_WORDS.search(s) for s in sentences if deny_hits(s, [d])):
            continue
        out.append(f"deny-listed '{d}'")
    return out


def _lack_problems(text: str, gaps: list[str]) -> list[str]:
    """Sentences saying the candidate lacks something. Each must name a term in GAPS and no other tool, platform,
    language or framework; with GAPS empty every such sentence is a problem."""
    allowed = [norm_literal(g) for g in gaps]
    named_elsewhere = gap_terms() - set(allowed)
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        n = norm_literal(s)
        if not LACK_CLAIM.search(n):
            continue
        if any(_contains(n, g) for g in allowed) and not any(_contains(n, t) for t in named_elsewhere):
            continue
        out.append(f"claims a gap not in GAPS: '{s.strip()[:60]}'")
    return out


VAGUE_WORDS = FILLER_WORDS      # filler: only if the evidence itself says it
# evidence that does not say "I built / led it": contributed, shipped, or work done as part of a team
WEAK_ROLE = re.compile(r"\b(contribut\w*|shipped|team (?:project|of \w+|effort)|(?:as )?part of (?:a|the) team|in a team)\b",
                       re.I)
UPGRADE_VERBS = re.compile(r"\b(deliver\w*|built|build\w*|led|leads|leading|owned|owns|owning)\b")
APPROX = re.compile(r"(?:~|\b(?:about|around|approximately|roughly|circa|nearly|almost)\s+)\s*(\d[\d,]*(?:\.\d+)?)", re.I)
APPROX_MARK = re.compile(r"(?:~|\b(?:about|around|approximately|roughly|circa|nearly|almost|over|more than|some)\s*)$", re.I)
_STOP = frozenset("with that this from have been into their over using used which what when where while also about "
                  "than then them they were will would could should your more most such each some team".split())


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text)


def _content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#-]{4,}", norm_literal(s)) if w not in _STOP}


def letter_evidence(fb: dict, kws: list[Keyword], chosen_ids: set[str] | None = None) -> list[dict]:
    """The two bullets the letter is written from, chosen in code from the bullets on the CV: the one overlapping the JD
    most, then the best of the rest from a different entry (two lines about one job read as one example). Only bullets
    that start with a past-tense verb ("Built ...", "Wrote ...") are used: the letter says "I built ...", and a noun-led
    bullet ("XGBoost churn classifier in R ...") cannot follow "I". When the CV has fewer than two of them, the best such
    bullets of the whole bank fill up. Canonical fact-bank text, as [{"id", "text", "terms"}]; fewer than two when the
    CV has fewer bullets."""
    sc = bullet_scores(fb, kws)
    bidx = bullet_index(fb)
    owner = {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    order = {b: i for i, b in enumerate(sc)}
    on_cv = [b for b in sc if not chosen_ids or b in chosen_ids]
    pool = [b for b in on_cv if starts_with_past_verb(bidx[b]["text"])]
    ranked = sorted(pool, key=lambda b: (-sc[b], order[b]))
    if len(ranked) < 2 and len(on_cv) >= 2:        # noun-led bullets were left out: the bank's best verb-led ones fill up
        rest = sorted((b for b in sc if b not in ranked and starts_with_past_verb(bidx[b]["text"])),
                      key=lambda b: (-sc[b], order[b]))
        ranked += rest
    picked = ranked[:1]
    if len(ranked) > 1:
        picked.append(next((b for b in ranked[1:] if owner[b] != owner[ranked[0]]), ranked[1]))
    return [{"id": b, "text": bidx[b]["text"], "terms": list(bidx[b].get("terms", []))} for b in picked]


def _style_problems(paras: list[str], evidence: list[str]) -> list[str]:
    """Checks that keep the letter faithful in tone: it ends on a question, never promotes the candidate's role,
    keeps approximations approximate and uses no filler word the evidence does not use."""
    out = []
    text = " ".join(paras)
    if paras and not paras[-1].rstrip().endswith("?"):
        out.append("last paragraph is not a call-to-action question")
    ev_all = norm_literal("\n".join(evidence))
    for w in VAGUE_WORDS:
        nw = norm_literal(w)
        if has_literal(norm_literal(text), nw) and not has_literal(ev_all, nw):
            out.append(f"vague filler '{w}' is not in the evidence")
    sents = _sentences(text)
    for ev in evidence:
        cue = WEAK_ROLE.search(ev)
        if not cue:
            continue
        words, nev = _content_words(ev), norm_literal(ev)
        for s in sents:
            if len(_content_words(s) & words) < 3:
                continue                                     # this sentence is not about that evidence line
            for m in UPGRADE_VERBS.finditer(norm_literal(s)):
                if not _contains(nev, m.group(0)):
                    out.append(f"upgrades the role: '{m.group(0)}' where the evidence says '{cue.group(0)}'")
    for m in APPROX.finditer("\n".join(evidence)):
        n = m.group(1)
        for hit in re.finditer(r"(?<![\w.])" + re.escape(n) + r"(?![\w.]*\d)", text):
            if not APPROX_MARK.search(text[max(0, hit.start() - 16):hit.start()]):
                out.append(f"drops the approximation '~{n}'")
                break
    return list(dict.fromkeys(out))


def _with_orgs(fb: dict, evidence: list[dict]) -> list[dict]:
    """The evidence with the entry each bullet belongs to: {"org"} = the employer (or the project's name when "project")."""
    owner = {b["id"]: (e.get("org") or e.get("name") or "", sec == "projects")
             for sec in ("experience", "projects") for e in fb.get(sec, []) for b in e["bullets"]}
    return [{**e, "org": owner.get(e["id"], ("", False))[0], "project": owner.get(e["id"], ("", False))[1]} for e in evidence]


def _letter(job, fb, kws, hook, evidence: list[dict], llm, settings, gaps: list[str] | None = None,
            notes: list[str] | None = None, team: str = "",
            retry: dict | None = None) -> tuple[list[str], list[str]]:
    """The cover letter from the company hook, the two evidence bullets and the optional GAPS. The model writes the body;
    the closing question is added here from settings (`letter_closing`). A draft is checked (length, banned phrases, deny
    list, claimed gaps, role upgrades, numbers, and every sentence against its own evidence bullet and the JD) and retried
    once, naming the words it was rejected for; a second failure is the deterministic fallback, flagged. The prompt lists
    the filler and capability words nothing supports under AVOID. `retry` (when given) is filled in: {"count",
    "rejected_words", "first_problems", "outcome"}, and `notes` says the retry happened. Raises TaskUnavailable when no
    provider the task is restricted to can take it: the caller keeps the job waiting."""
    deny = fb.get("deny_terms", [])
    gaps = [gap_display(g, job["description"]) for g in (gaps or [])[:6]]       # "Azure", "Power BI", "DSPy": the JD's casing
    # a deny-listed tool that is a gap is nameable, so the prompt must not also forbid it
    named_ok = {d for g in gaps for d in deny_hits(g, deny)}
    max_words = settings["scoring"]["letter_max_words"]
    texts = [e["text"] for e in evidence]
    source = " ".join(texts) + " " + hook + " " + job["description"]
    named = f"{job['company']} {job['title']}"
    closing = letter_closing(settings, job["company"], team)
    problems: list[str] = []
    feedback = ""
    retry = retry if retry is not None else {}
    retry.update({"count": 0, "rejected_words": [], "first_problems": [], "outcome": "first draft"})
    avoid = avoid_words(evidence, job["description"], hook, f"{named} {' '.join(gaps)}")

    def draft_problems(body: list[str]) -> list[str]:
        """Every problem with this body. The opening sentence's claim half may use the two evidence bullets alone, not the
        JD or the company hook; the paragraphs must be first-person prose, not pasted bullets."""
        paras = [*body, closing]
        text = " ".join(paras)
        out: list[str] = []
        if not " ".join(body).strip():
            out.append("empty letter")
        if len(text.split()) > max_words:
            out.append(f"{len(text.split())} words > {max_words}")
        if paras and BANNED_OPENERS.search(paras[0]):
            out.append("banned opener")
        if BANNED_CLOSERS.search(text):
            out.append("banned closer")
        out += _deny_problems(text, deny, gaps)
        out += _lack_problems(text, gaps)
        style = _style_problems(paras, texts) + grammar_problems(body, texts)
        first = (sentences(body[0]) or [""])[0] if body else ""
        rest = [s for s in sentences(" ".join(body)) if s != first] if body else []
        g_first = grounding_problems([first], evidence, job["description"], hook, named, gaps) if first else []
        g_rest = grounding_problems(rest, evidence, job["description"], hook, named, gaps) if rest else []
        claim = opening_claim_problems(first, evidence, gaps) if first else []                 # the model's words only
        extra = numbers(text) - numbers(source)
        out += style + [p for p in g_first + g_rest + claim if p not in style]
        if extra:
            out.append(f"new numbers {sorted(extra)}")
        return list(dict.fromkeys(out))

    body: list[str] = []
    for attempt in range(2):
        try:
            out = _llm_json(
                llm, "cover_letter",
                P.LETTER_SYSTEM.replace("{max_words}", str(max_words))
                .replace("{deny}", ", ".join(d for d in deny if d not in named_ok)),
                P.LETTER_USER.format(title=job["title"], company=job["company"], company_hook=hook or "(none)",
                                     gaps=", ".join(gaps) or "(none)", avoid=", ".join(avoid) or "(none)",
                                     evidence="\n".join(f"- {e}" for e in texts)) + feedback,
                {"company": job["company"], "title": job["title"], "company_hook": hook, "evidence": texts,
                 "gaps": gaps, "avoid": avoid})
            body, letter_notes = normalise_letter(out)
            if body and body[-1].rstrip().endswith("?"):       # the model wrote its own closing question: ours replaces it
                body = body[:-1]
                letter_notes.append("dropped the model's own closing question; the template closing is used")
            if notes is not None:
                notes.extend(m for m in (f"cover_letter: {n}" for n in letter_notes) if m not in notes)
        except TaskUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            problems.append(f"letter LLM failed: {e}")
            break
        problems = draft_problems(body)
        if not problems:
            retry["outcome"] = "ok after retry" if attempt else "first draft"
            return [*body, closing], []
        words = rejected_words(problems)
        if attempt == 0:                                       # the one retry: say which words the check rejected
            retry.update({"count": 1, "rejected_words": words, "first_problems": list(problems)})
            if notes is not None:
                notes.append("cover_letter: first draft rejected (" + "; ".join(problems)[:200] + "); retried once"
                             + (f", naming: {', '.join(words)}" if words else ""))
        feedback = ((f"\n\nThe check rejected these words as unsupported: {', '.join(words)}. Do NOT use them." if words else "")
                    + "\n\nYOUR LAST DRAFT BROKE THESE RULES; fix every one:\n" + "\n".join(f"- {p}" for p in problems))
    retry["outcome"] = "fallback" if retry["count"] else retry["outcome"]
    # deterministic fallback — always honest and first person; review will show why (letter_rules.fallback_letter)
    return (fallback_letter(job, hook, _with_orgs(fb, evidence), gaps, closing, deny, job["description"]),
            problems + [FALLBACK_MARK])


def _make_letter(job, fb, kws, hook, evidence: list[dict], llm, settings, out_dir: Path, final_name: str,
                 team: str = "", gaps: list[str] | None = None):
    """Write the cover letter PDF. Always produces one: a failed or rule-breaking LLM draft becomes the
    deterministic fallback, and report["letter"]["status"] says which of the two the reader is looking at."""
    notes: list[str] = []
    retry: dict = {}
    try:
        paras, problems = _letter(job, fb, kws, hook, evidence, llm, settings, gaps, notes, team, retry)
    except TaskUnavailable as e:        # no provider may write it now: no letter, no fallback, the job waits
        return None, {"paragraphs": [], "problems": [], "score": None, "pages": 0, "status": "pending",
                      "reason": str(e)[:300], "notes": notes,
                      "evidence": [{"id": x["id"], "text": x["text"]} for x in evidence]}
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paras,
                         "date": date.today().strftime("%d %B %Y"),
                         "greeting": letter_greeting(settings, job["company"], team),
                         "signoff": settings["letter"]["signoff"]}, out_dir, final_name)
    return pdf, {"paragraphs": paras, "problems": problems, "score": score(pdf_text(pdf), kws).literal,
                 "pages": page_count(pdf), "status": "fallback" if FALLBACK_MARK in problems else "ok",
                 "notes": notes, "retry": retry, "evidence": [{"id": e["id"], "text": e["text"]} for e in evidence]}


def needs_result(fb: dict, ctx: dict) -> list[dict]:
    """The bullets on this CV that have no `result:` in the fact bank, for the user to fill in (never invented):
    [{"id", "entry", "text"}], in CV order."""
    bidx = bullet_index(fb)
    return [{"id": bid, "entry": e.get("org") or e.get("name"), "text": bidx[bid]["text"]}
            for sec in ("experience", "projects") for e in ctx[sec] for bid in e["bullet_ids"]
            if not str(bidx[bid].get("result") or "").strip()]


def _names(fb: dict, settings) -> tuple[str, str]:
    """Final file stems for the CV and the letter: '<name> CV', '<name> Cover Letter' (files.name_style)."""
    style = settings["files"]["name_style"]
    return (file_stem(fb["identity"]["name"], "CV", style), file_stem(fb["identity"]["name"], "Cover Letter", style))


def usage_by_task(calls: list[dict]) -> dict[str, dict]:
    """Per task: the number of calls, the summed token usage and each call's prompt tokens in call order (what the
    provider reported, else our estimate), so a prompt that has grown too big for the per-minute budget shows up."""
    out: dict[str, dict] = {}
    for c in calls:
        t = out.setdefault(c.get("task") or "?", {"calls": 0, **{k: 0 for k in USAGE_SUMS}, "prompt_tokens_per_call": []})
        t["calls"] += 1
        for k in USAGE_SUMS:
            t[k] += c.get(k) or 0
        t["prompt_tokens_per_call"].append(c["prompt_tokens"] if c.get("prompt_tokens") is not None
                                           else c.get("est_input_tokens"))
    return out


def usage_since(llm, start: int) -> dict:
    """Token usage of the LLM calls made since `start` (an index into llm.usage): the calls, their sums and the same per
    task. Empty for a model that records none (MockLLM)."""
    calls = list(getattr(llm, "usage", [])[start:])
    total = {k: sum(c.get(k) or 0 for c in calls) for k in USAGE_SUMS}
    return {"calls": calls, **total, "by_task": usage_by_task(calls)}


def regenerate_letter(job, fb: dict, llm, settings, rep: dict) -> dict:
    """Re-draft the letter for an already tailored job (review's 'l'). Keeps the file name; rewrites report.json."""
    job = dict(job)
    job["description"] = job.get("description") or ""
    kws = [Keyword(**k) for k in rep["keywords"]]
    out_dir = Path(rep["out_dir"])
    llm = RawRecorder(llm, out_dir / "llm_raw")                 # keeps the model's raw reply next to the first draft's
    usage_from = len(getattr(llm, "usage", []))
    ids = {i for sec in ("experience", "projects") for e in rep["context"][sec] for i in e.get("bullet_ids", [])}
    stem = Path(rep["letter_pdf"]).stem if rep.get("letter_pdf") else _names(fb, settings)[1]    # none while pending
    pdf, letter = _make_letter(job, fb, kws, rep.get("company_hook") or "", letter_evidence(fb, kws, ids), llm,
                               settings, out_dir, stem, rep.get("team") or "",
                               required_gaps(rep.get("genuine_gaps") or [], kws))
    rep["letter"], rep["letter_pdf"] = letter, str(pdf) if pdf else None
    new, old = usage_since(llm, usage_from), rep.get("llm_usage") or {}      # the re-draft adds to the job's total
    calls = (old.get("calls") or []) + new["calls"]
    rep["llm_usage"] = {"calls": calls, **{k: (old.get(k) or 0) + new[k] for k in USAGE_SUMS},
                        "by_task": usage_by_task(calls)}
    (out_dir / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return rep


# ------------------------------------------------------------------ 4. the loop
STAGING_SUFFIXES = (".partial", ".old", ".failed")


def _discard(path: Path, root: Path) -> None:
    """Delete a folder this module owns: a sibling of a job folder named <job>.partial / .old / .failed, directly inside
    `root` (the outputs folder). It is the only deletion in the tailoring package besides xelatex's own aux files, and it
    refuses anything else (a symlink, a folder outside `root`, a name that is not a staging name), so no other folder
    can ever be removed through it."""
    p, r = path.absolute(), root.absolute()
    if p.parent != r or p.suffix not in STAGING_SUFFIXES or p.is_symlink():
        raise RuntimeError(f"refusing to delete {path}: not a staging folder directly inside {root}")
    shutil.rmtree(p, ignore_errors=True)


def tailor_job(job: dict, fb: dict, llm, settings, out_root: Path, frozen: dict | None = None,
               one_page: bool = False) -> dict[str, Any]:
    """Tailor one job into out_root/<company_title_id>/. Everything is built in a '.partial' sibling folder and swapped
    in only once the whole job has succeeded, so a run that fails part-way (LLM down, xelatex error) leaves an
    earlier successful output folder exactly as it was."""
    final = out_root / slug(f"{job['company']}_{job['title']}_{job['id'].split(':')[-1]}")
    work = final.with_name(final.name + ".partial")
    _discard(work, out_root)                                    # a leftover of a run that was killed
    try:
        report = _tailor(job, fb, llm, settings, work, final, frozen, one_page)
        old = final.with_name(final.name + ".old")
        _discard(old, out_root)
        if final.exists():
            os.replace(final, old)
        os.replace(work, final)
        _discard(old, out_root)                                 # the output this one replaces
        _discard(final.with_name(final.name + ".failed"), out_root)    # superseded by this success
        return report
    except BaseException as exc:
        # The staging folder goes, the earlier successful folder stays. But the model's raw answers are what you need
        # to see why a job failed, so they are kept in <job>.failed/llm_raw and the error says where.
        raw = work / "llm_raw"
        if raw.is_dir():
            failed = final.with_name(final.name + ".failed")
            _discard(failed, out_root)
            shutil.copytree(raw, failed / "llm_raw")
            if isinstance(exc, Exception) and exc.args and isinstance(exc.args[0], str):
                exc.args = (f"{exc.args[0]} [raw model answers: {failed / 'llm_raw'}]", *exc.args[1:])
        _discard(work, out_root)
        raise


def _tailor(job: dict, fb: dict, llm, settings, out_dir: Path, final_dir: Path,
            frozen: dict | None = None, one_page: bool = False) -> dict[str, Any]:
    """The loop proper. Writes into out_dir (the staging folder); the report names final_dir's paths.
    Code selects the bullets, the model rewords them (batches) and writes the profile, the guard checks both."""
    sc = settings["scoring"]
    band = tuple(sc["band"])
    lo, hi = band
    one_page = one_page or sc["max_pages"] <= 1                  # --one-page, or a settings file that asks for one page
    max_pages = 1 if one_page else sc["max_pages"]
    layout = ONE_PAGE if one_page else layout_from(settings)
    llm = RawRecorder(llm, out_dir / "llm_raw")                 # every raw model reply is kept for inspection
    usage_from = len(getattr(llm, "usage", []))
    family, tier, headline, preferred = role_prior(job, fb, settings)
    analysis_warnings: list[str] = []
    kws, hook, team = analyse(job, llm, settings, analysis_warnings, frozen)
    ceil, gaps = ceiling(fact_text(fb), kws)
    skills, dropped_groups = cv_skills(fb, kws, None if one_page else sc["max_skill_groups"])
    cv_name, letter_name = _names(fb, settings)
    profile = make_profile(job, fb, kws, headline, llm)         # depends on the facts only, not on the selection
    report: dict[str, Any] = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                              "family": family, "tier": tier, "headline": headline,
                              "keywords": [asdict(k) for k in kws], "company_hook": hook, "team": team,
                              "ceiling": ceil, "genuine_gaps": gaps, "attempts": [], "out_dir": str(final_dir),
                              "analysis_warnings": analysis_warnings, "one_page": one_page,
                              "skills": {"core": core_skills(fb), "rows": list(skills), "dropped_groups": dropped_groups},
                              "profile": {k: profile[k] for k in ("facts", "retried", "fallback")}}

    jac = sc["bullet_dedupe_jaccard"]
    ranking, best, cache = kws, None, {}
    for attempt in range(1, sc["max_attempts"] + 1):
        llm.attempt = attempt                                    # names this attempt's raw replies (tailor_profile-2.json)
        sel = select_cv(fb, ranking, preferred, skills=skills, layout=layout, jaccard_limit=jac)
        rephrase_notes: list[str] = []
        proposed, rstats = propose_rephrases(job, fb, sel, kws, llm, cache, rephrase_notes,
                                             other_text=profile["text"] + " " + skills_text(skills))
        accepted, viol, warn = guard_rephrases(fb, proposed, kws)
        rstats.update(proposed=len(proposed), accepted=len(accepted), blocked=len(proposed) - len(accepted),
                      regressed=sum(f": {NO_REGRESSION}: " in w for w in warn))
        ctx = _context(fb, sel, accepted, profile["text"], kws, headline, skills)

        def rerender() -> tuple[Path, int]:
            out = render_cv(ctx, out_dir, cv_name, one_page)
            return out, page_count(out)

        pdf, pages = rerender()
        shrinks = 0
        while pages > max_pages and _shrink(ctx, kws, layout):
            pdf, pages = rerender()
            shrinks += 1
        fill: dict = {"added": [], "rejected_overflow": [], "renders": 0, "reached": None}
        # two pages: when nothing had to be shrunk away, keep adding bullets until page 2 is full enough (or none are left)
        fill_on = not one_page and max_pages == 2 and shrinks == 0
        order = (fill_order(fb, ranking, sel, skills, jac, bool(sc.get("cv_fill_extra_projects")))
                 if fill_on else [])
        if fill_on:
            fill = fill_page2(ctx, fb, order, accepted, rerender, sc["min_fill_page2"])
            pdf, pages = rerender()
        s = score(pdf_text(pdf), kws)

        # Wording pass: a keyword the CV has only in another form ("prototyped" for "prototyping") gets its exact form in
        # the one bullet that carries its stem. Kept only if the CV still fits and no keyword was lost.
        wording: dict = {"terms": list(s.wording_misses), "asked": 0, "calls": 0, "accepted": [], "blocked": [],
                         "unplaced": [], "kept": None}
        if s.wording_misses:
            before = [list(e["bullets"]) for sec in ("experience", "projects") for e in ctx[sec]]
            wording.update(wording_pass(job, fb, ctx, kws, s.wording_misses, llm, cache, rephrase_notes))
            if wording["accepted"]:
                pdf, pages = rerender()
                s2 = score(pdf_text(pdf), kws)
                wording["kept"] = s2.literal >= s.literal and pages <= max_pages
                if wording["kept"]:
                    s = s2
                else:
                    for e, old in zip([e for sec in ("experience", "projects") for e in ctx[sec]], before):
                        e["bullets"][:] = old
                    pdf, pages = rerender()
                    rephrase_notes.append("wording: the exact-form rewrites lowered the score or added a page; reverted")

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
                            pdf, pages = rerender()
                            s = score(pdf_text(pdf), kws)
            if reverted and fill_on:                           # shorter wording may have left page 2 short again
                more = fill_page2(ctx, fb, order, accepted, rerender, sc["min_fill_page2"],
                                  skip=set(fill["rejected_overflow"]))
                fill = {"added": fill["added"] + more["added"],
                        "rejected_overflow": fill["rejected_overflow"] + more["rejected_overflow"],
                        "renders": fill["renders"] + more["renders"], "reached": more["reached"]}
                pdf, pages = rerender()
                s = score(pdf_text(pdf), kws)

        rec = {"attempt": attempt, "score": s.literal, "score_stemmed": s.stemmed, "band": in_band(s.literal, band),
               "pages": pages, "page2_fill": page2_fill(pdf, pages), "fill": fill, "shrinks": shrinks,
               "reverted": reverted, "wording": wording,
               "violations": profile["violations"] + viol, "warnings": profile["warnings"] + rephrase_notes + warn,
               "wording_misses": s.wording_misses, "misses": s.misses, "rephrase": rstats,
               "proposal": {"selection": sel, "rephrase": proposed, "profile": profile["text"]},
               "selection": {e["id"]: e["bullet_ids"] for e in ctx["experience"] + ctx["projects"]},
               "profile": ctx["profile"], "rephrase_coverage": rephrase_coverage(ctx, fb, kws),
               "placement": placement_report(rstats["offered"], rstats["unoffered"], proposed, accepted, ctx)}
        report["attempts"].append(rec)
        if best is None or abs(s.literal - (lo + hi) / 2) < abs(best[0]["score"] - (lo + hi) / 2):
            best = (rec, ctx)
        if rec["band"] != "below" or ceil < lo:
            break   # in band, or (above: rephrasings already reverted) nothing left to try, or no honest way up
        # Below the band: count the keywords the fact bank evidences but the CV missed double when ranking, so the next
        # attempt picks the bullets that carry them. Selection is deterministic; an unchanged one would be wasted quota.
        fixable = {m for m in s.wording_misses + s.misses if m not in gaps}
        ranking = _keyword_boost(ranking, fixable)
        if select_cv(fb, ranking, preferred, skills=skills, layout=layout, jaccard_limit=jac) == sel:
            break

    rec, ctx = best
    pdf = render_cv(ctx, out_dir, cv_name, one_page)            # re-render the chosen attempt as the final file
    final_score = score(pdf_text(pdf), kws)
    cv_pages = page_count(pdf)
    fill_final = page2_fill(pdf, cv_pages)
    chosen_ids = {i for e in ctx["experience"] + ctx["projects"] for i in e["bullet_ids"]}
    letter_pdf, letter = _make_letter(job, fb, kws, hook, letter_evidence(fb, kws, chosen_ids), llm, settings, out_dir,
                                      letter_name, team, required_gaps(gaps, kws))
    report.update({
        "final": {"score": final_score.literal, "score_stemmed": final_score.stemmed,
                  "band": in_band(final_score.literal, band), "pages": cv_pages, "page2_fill": fill_final,
                  "hits": final_score.hits, "wording_misses": final_score.wording_misses,
                  "misses": final_score.misses, "chosen_attempt": rec["attempt"]},
        "cv_pages": cv_pages, "page2_fill": fill_final, "fill": rec["fill"],
        "needs_result": needs_result(fb, ctx),
        "rephrase_coverage": rec["rephrase_coverage"], "rephrase": rec["rephrase"], "placement": rec["placement"],
        "letter": letter, "llm_usage": usage_since(llm, usage_from),
        "cv_pdf": str(final_dir / pdf.name), "letter_pdf": str(final_dir / letter_pdf.name) if letter_pdf else None,
        "context": ctx,
    })
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
