"""Analyse → tailor → guard → render → score loop for one job. Writes a report.json the review step reads."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from dataclasses import asdict, replace as dc_replace
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .. import ats
from .. import hook as hooks
from .. import note as note_mod
from ..apply.naming import file_stem
from ..config import load_yaml
from ..discovery import gates
from ..llm.router import TaskUnavailable
from . import evidence_fields
from . import prompts as P
from . import layout as pdflayout
from .budget import entry_where, letter_scores, rank_projects, ranking_table, relevance_scores
from .faithfulness import check, deny_hits, numbers
from .grounding import (FILLER_WORDS, avoid_words, grounding_problems, hook_link_problems, opening_claim_problems,
                        rejected_words, repeated_evidence_problems, sentences)
from .context_terms import model_terms, split_context
from .families import possible_evidence
from . import freeze
from . import letters as letters_mod
from . import profile as profile_mod
from . import typography
from .jdtext import strip_benefits
from .rawlog import RawRecorder
from .profile import PROFILE_FACTS, pick_facts   # noqa: F401 — re-exported
from .qualifiers import prompt_list, repair
from .letter_rules import fallback_letter, gap_display, grammar_problems, term_dump_problems
from .rewrite_rules import (added_word_problems, bare_keyword_problems, canonical_case, canonical_forms, filler_problems, must_keep_problems,
                            qualifier_problems)
from . import selection as _selection
from .selection import (JACCARD, ONE_PAGE, REPLACE_RATIO, Layout, bullet_hits, chunks, fill_order, is_recent, layout_from, missing_keywords,
                        replacers, select_cv, selected_ids, settle_selection, situated_ids, skills_text, verbatim_ids)
from .skills import (CORE_LABEL, core_skills, cv_skill_groups, cv_skills, dedupe_skills, family_basics_missing, family_skill_lines,
                     is_database_row, pick_family, skill_group_config, wants_databases)   # noqa: F401 — dedupe_skills is re-exported
from .shapes import normalise_keywords, normalise_letter, normalise_rephrase
from .textnorm import normalise_json
from .render import (CV2_MARGIN_PT, DATE_SEPARATOR, last_line_fills, page2_fill, page_count, pdf_text, project_parts, render_cv,
                     render_letter, result_span)
from .scorer import (Keyword, _contains, cap_keywords, has_literal, ceiling, drop_contained, gap_terms, in_band, merge_keywords,
                     norm_literal, norm_stem, score, taxonomy_keywords)

USAGE_SUMS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")   # summed into report llm_usage
FALLBACK_MARK = "used fallback letter"
LETTER_MODES = ("off", "draft", "hook")     # settings letter.mode: no letter calls at all, the model drafts one, or hook.py's two variants A / B
LETTER_POLICIES = ("none", "template")     # settings letter.on_failure: what a letter that failed every draft becomes
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


def bullet_positions(fb: dict, first: str | None = None) -> dict[str, int]:
    """{bullet id: its place in the fact bank} (experience entries, then projects, each in its listed order): the order the CV
    prints the chosen bullets of one role or project in. `first` (the hook's proof bullet) goes before every other bullet."""
    pos = {b["id"]: n for n, b in enumerate(bullet_index(fb).values())}
    if first in pos:
        pos[first] = -1
    return pos


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60]


FOLDER_MAX = 60          # the company and role part of an output folder name, and the whole name when the id is short


def folder_name(job: dict) -> str:
    """`<Company>_<Role, truncated>_<job id>`: only the company and role are cut to fit, never the id, so two jobs of one company never
    share a folder (the old name cut the whole string at 60 characters and lost the id: "...Engineer_2027" became "...Engineer_2")."""
    tail = re.sub(r"[^A-Za-z0-9]+", "_", str(job["id"]).split(":")[-1]).strip("_") or "job"
    room = max(24, FOLDER_MAX - 1 - len(tail))
    head = re.sub(r"[^A-Za-z0-9]+", "_", f"{job['company']}_{job['title']}").strip("_")[:room].rstrip("_")
    return f"{head}_{tail}"


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
            frozen: dict | None = None, context: list[dict] | None = None) -> tuple[list[Keyword], str, str]:
    """(keywords, company_hook, team). `team` is "" when the JD names none. The model only suggests keywords; code
    enforces the rules: verbatim in the JD, short, nothing generic, near-duplicates merged (the shorter stays), at most
    scoring.max_keywords kept by weight then JD frequency. Shape problems in the model's answer and every drop are
    appended to `notes` when given. `frozen` ({"keywords": [Keyword], "company_hook", "team", "notes", "date"}, see
    reporting.calibrate.load_frozen) is the result of an earlier analysis: it is returned as it is, with no model call,
    so the keywords (and so the ceiling) are the same on every run and with every provider. Team, product-area and
    organisation names (Growth, Payments, "product squad") are not skills: they are taken out of the keywords before the
    cap (so they neither count in the score nor use up a place) and appended to `context` as
    {"term", "weight", "required", "why"}, for the review screen. This is the one place keywords are made, for live
    tailoring and calibration alike."""
    if frozen is not None:
        if context is not None:
            context.extend(frozen.get("context_terms") or [])
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
    model_ctx: set[str] = set()
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
        model_ctx = model_terms(out.get("context_terms"), job["description"] or "")
    except Exception as e:  # noqa: BLE001 — taxonomy keywords alone are a usable fallback
        hook = team = ""
        llm_kw = []
        print(f"  keyword LLM failed, using taxonomy only: {e}")
        if notes is not None:
            notes.append(f"extract_keywords: {LLM_FAILED} ({str(e)[:120]}); taxonomy keywords only")
    kws, merged = drop_contained(merge_keywords(tax, llm_kw))
    kws, ctx_terms = split_context(kws, job["description"] or "", model_ctx, team)
    kws, capped = cap_keywords(kws, jd, sc["max_keywords"])
    if context is not None:
        context.extend(ctx_terms)
    if notes is not None:
        notes.extend(f"keywords: {n}" for n in merged + capped)
        notes.extend(f"keywords: '{c['term']}' is context ({c['why']}), not scored" for c in ctx_terms)
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


LETTER_ATTEMPTS = 3           # the first draft plus up to two retries, each told every violation so far
REPHRASE_BATCH = 5            # bullets per rephrase_bullets call: small enough for a small model to follow every rule


def _deny_list(fb: dict) -> str:
    return ", ".join(fb.get("deny_terms", []))


# ---- 2a. rephrase: the model rewords bullets in batches, the guard decides what is kept
def award_of(fb: dict) -> dict[str, str]:
    """{project bullet id: the project's award}: the award is in the project's heading, so a bullet never repeats it."""
    return {b["id"]: str(p["award"]).strip() for p in fb.get("projects", []) if str(p.get("award") or "").strip()
            for b in p["bullets"]}


def unused_result(bullet: dict, award: str = "") -> str:
    """The bullet's optional `result:` when its text does not already say it, else ''. Never invented: it is only what
    the user wrote in the fact bank. A project's `award` is taken out of it ("3rd place; 200:1 class imbalance" ->
    "200:1 class imbalance"): the heading already shows the award."""
    res = " ".join(str(bullet.get("result") or "").split())
    res = typography.strip_award(res, award) if award else res
    return "" if not res or norm_literal(res).rstrip(".") in norm_literal(bullet["text"]) else res


MAX_OFFERS = 2        # keywords one bullet's rephrase may be asked to place


def placement_offers(fb: dict, sel: dict, kws: list[Keyword], other_text: str = "",
                     limit: int = MAX_OFFERS, introduce: bool = False) -> tuple[dict[str, list[str]], list[str]]:
    """Which missed keyword each selected bullet is asked to place. A keyword is missed when neither the selected bullets'
    canonical text nor `other_text` (profile, skills) has it literally. With `introduce` it is offered to the first selected
    bullet, in CV order, whose `terms` evidence it and which has fewer than `limit` offers (heaviest keyword first), so no keyword
    is asked for twice. Without it (the default since the round of 17 Oct) nothing is offered: a bullet's `terms:` list is not
    what the bullet says, and a rewording may not add a word the bullet lacks (rewrite_rules.added_word_problems), so every missed
    keyword is reported as unoffered. Returns ({bullet id: [words]}, [missed keywords no selected bullet could take])."""
    bidx = bullet_index(fb)
    ids = selected_ids(fb, sel)
    locked = verbatim_ids(fb)                                            # a verbatim bullet is never asked to take a keyword
    cv = norm_literal(other_text + " " + " ".join(bidx[i]["text"] for i in ids))
    offers: dict[str, list[str]] = {}
    unoffered: list[str] = []
    missed = [k for k in kws if not any(has_literal(cv, norm_literal(w)) for w in k.members())]
    for k in sorted(missed, key=lambda k: -k.weight):                    # stable: the JD's order within a weight
        for bid in ids:
            word = next(iter(missing_keywords(bidx[bid], [k])), None) if introduce and bid not in locked else None
            if word and len(offers.get(bid, [])) < limit:
                offers.setdefault(bid, []).append(word)
                break
        else:
            unoffered.append(k.term)
    return offers, unoffered


def propose_rephrases(job: dict, fb: dict, sel: dict, kws: list[Keyword], llm, cache: dict,
                      notes: list[str], other_text: str = "", introduce: bool = False) -> tuple[dict[str, str], dict]:
    """Ask for rewordings of the selected bullets that can carry a JD keyword the CV is missing (`placement_offers`: the
    keyword is in the bullet's `terms`, at most MAX_OFFERS per bullet, and only with `introduce`, which the pipeline never sets: a
    reworded bullet may not add a claim; `other_text` = the profile and skills text, which
    already count as having a keyword), in batches of REPHRASE_BATCH: each call gets the bullet text, its terms and the
    keywords it may introduce, and answers
    {bullet id: new text}. Returns ({bullet id: proposed text}, stats). `cache` maps (bullet id, missing words) to the
    answer, so a later attempt never pays twice for the same question; a failed batch is not cached and is noted."""
    bidx, awards = bullet_index(fb), award_of(fb)
    result = lambda bid: unused_result(bidx[bid], awards.get(bid, ""))      # noqa: E731
    todo = []
    offers, unoffered = placement_offers(fb, sel, kws, other_text, introduce=introduce)
    locked = verbatim_ids(fb)                         # `verbatim: true`: the user's own wording, never sent to the model
    for bid in selected_ids(fb, sel):
        if bid in locked:
            continue
        miss = tuple(offers.get(bid, ()))
        if miss or result(bid):                       # a result the user supplied may be worked in, even with no keyword
            todo.append((bid, miss))
    fresh = [t for t in todo if t not in cache]
    stats = {"eligible": len(todo), "asked": len(fresh), "cached": len(todo) - len(fresh), "calls": 0,
             "failed_calls": 0, "offered": {bid: list(m) for bid, m in offers.items()}, "unoffered": unoffered}
    system = P.REPHRASE_SYSTEM.replace("{deny}", _deny_list(fb))
    for batch in chunks(fresh, REPHRASE_BATCH):
        items = [{"id": bid, "text": bidx[bid]["text"], "terms": list(bidx[bid].get("terms", [])),
                  "introduce": list(miss), **({"result": result(bid)} if result(bid) else {})}
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


def guard_rephrases(fb: dict, proposed: dict[str, str], kws: list[Keyword], base: dict[str, str] | None = None,
                    offered: dict[str, list[str]] | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    """Run every proposed rewording through faithfulness.check against its own bullet (text + terms). Returns
    (accepted {bullet id: text}, violations, warnings); a rejected rewording simply keeps the bullet as it was.
    No-regression rule: a rewording that passes the guard is still refused when its literal keyword hits are not a superset
    of the hits of the text it replaces (`base[bid]`, default the canonical bullet; the wording pass replaces what is on
    the CV now), and a `no-regression` warning says which keywords it would lose. A rewording may not add a content word the bullet does
    not say (rewrite_rules.added_word_problems): `offered` ({bullet id: words}, empty in the pipeline) names the only words that may be new."""
    deny, bidx, vocab = fb.get("deny_terms", []), bullet_index(fb), fact_text(fb)
    forms, awards = canonical_forms(fb), award_of(fb)
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
                         + bare_keyword_problems(new, b["text"], kws)
                         + added_word_problems(new, b["text"], kws, str(b.get("result") or ""), (offered or {}).get(bid, ()))   # no new claim
                         + must_keep_problems(new, b["text"], str(b.get("result") or ""), awards.get(bid, "")))   # what it says must survive
        award = awards.get(bid, "")
        if award and typography.award_repeats(new, award) and not typography.award_repeats(b["text"], award):
            c.violations.append(f"the award '{award}' is in the project heading: not repeated in a bullet")
        warnings.extend(f"{bid}: {w}" for w in c.warnings)
        lost = lost_hits((base or {}).get(bid, b["text"]), new, kws) if c.ok else []
        if lost:
            warnings.append(f"{bid}: {NO_REGRESSION}: kept the earlier wording, the rewrite would lose {', '.join(lost)}")
        elif c.ok:
            accepted[bid] = canonical_case(new.strip(), forms)
        else:
            violations.extend(f"{bid}: {v}" for v in c.violations)
    return accepted, violations, warnings


def wording_targets(ctx: dict, kws: list[Keyword], wording_misses: list[str],
                    skip: frozenset[str] = frozenset()) -> tuple[dict[str, list[str]], list[str]]:
    """For each wording miss (a keyword the CV has in substance, stemmed, but not in the JD's own form), the ONE bullet on
    the CV, first in CV order, whose text carries the keyword's stem without its exact form (never one of `skip`, the verbatim
    bullets). Returns ({bullet id: [exact forms to write]}, [terms no bullet carries: the stem is only in the skills or the profile])."""
    by_term = {k.term: k for k in kws}
    plan: dict[str, list[str]] = {}
    unplaced: list[str] = []
    for term in wording_misses:
        k = by_term.get(term)
        hit = None
        for sec in ("experience", "projects"):
            for e in ctx[sec]:
                for bid, text in zip(e["bullet_ids"], e["bullets"]):
                    if bid in skip:
                        continue
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
    plan, unplaced = wording_targets(ctx, kws, wording_misses, verbatim_ids(fb))
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


def shorten_pass(job: dict, fb: dict, wanted: dict[str, tuple[str, int]], kws: list[Keyword], llm, cache: dict, notes: list[str],
                 hk: hooks.Hook | None = None) -> dict[str, str]:
    """Reword bullets that print on more lines than pdflayout.MAX_BULLET_LINES, shorter. `wanted` = {bullet id: (the bullet as it is on the
    CV, the most words it may have)}; the model gets the REPHRASE prompt with a SHORTEN line, in batches of REPHRASE_BATCH. Every
    answer goes through `guard_rephrases` against the bullet's fact-bank text (no new fact, no lost qualifier, no lost keyword) and must
    really be shorter and, for the hook's proof bullet, keep the hook's number. Returns {bullet id: the shorter text} for the accepted
    ones; a bullet the model could not shorten is simply not in it."""
    bidx = bullet_index(fb)
    system = P.REPHRASE_SYSTEM.replace("{deny}", _deny_list(fb))
    wanted = {bid: w for bid, w in wanted.items() if bid not in verbatim_ids(fb)}      # a verbatim bullet is never shortened
    todo = [(bid, text, mw) for bid, (text, mw) in wanted.items() if ("shorten", bid, text) not in cache]
    for batch in chunks(todo, REPHRASE_BATCH):
        items = [{"id": bid, "text": text, "terms": list(bidx[bid].get("terms", [])), "introduce": [], "shorten": mw}
                 for bid, text, mw in batch]
        lines = "\n".join(f"- id: {i['id']}\n  TEXT: {i['text']}\n  TERMS: {', '.join(i['terms']) or '(none)'}\n"
                          f"  MAY INTRODUCE: (none)\n  SHORTEN: at most {i['shorten']} words" for i in items)
        try:
            out = _llm_json(llm, "rephrase_bullets", system, P.REPHRASE_USER.format(title=job["title"], bullets=lines),
                            {"bullets": items, "title": job["title"]})
            got, shape_notes = normalise_rephrase(out, {i["id"] for i in items})
        except Exception as e:  # noqa: BLE001 — the bullet keeps its wording and is reported as too long
            notes.append(f"shorten: batch of {len(batch)} failed ({str(e)[:120]}); wording kept")
            continue
        notes.extend(f"shorten: {n}" for n in shape_notes)
        for bid, text, _ in batch:
            cache[("shorten", bid, text)] = got.get(bid, "")
    proposed = {bid: cache[("shorten", bid, text)] for bid, (text, _) in wanted.items()
                if cache.get(("shorten", bid, text)) and len(cache[("shorten", bid, text)].split()) < len(text.split())}
    accepted, _viol, warn = guard_rephrases(fb, proposed, kws, base={bid: wanted[bid][0] for bid in proposed})
    notes.extend(f"shorten: {w}" for w in warn if f": {NO_REGRESSION}: " in w)
    if hk is not None and hk.proof_bullet_id in accepted and not hooks.keeps_number(accepted[hk.proof_bullet_id], hk):
        del accepted[hk.proof_bullet_id]
    return accepted


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
    bidx, locked = bullet_index(fb), verbatim_ids(fb)
    eligible = rephrased = 0
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for bid, text in zip(e["bullet_ids"], e["bullets"]):
                if bid not in locked and missing_keywords(bidx[bid], kws):
                    eligible += 1
                    rephrased += text != bidx[bid]["text"]
    return {"eligible": eligible, "rephrased": rephrased, "ratio": rephrased / eligible if eligible else None}


# ---- 2b. profile: assembled in code (profile.py): the hook's profile line + one or two profile_facts. No model.
def assemble_profile(fb: dict, kws: list[Keyword], jd: str, hk: hooks.Hook | None) -> dict:
    """The CV profile (profile.assemble): `hk.profile_line` first, then the best one or two `profile_facts` as sentences. Returns
    {"text", "facts", "violations", "warnings", "retried", "fallback"}."""
    return profile_mod.assemble(fb, kws, jd, hk.profile_line if hk else "")


# ---- 2c. assemble and fit
TOP_PROJECTS = 3        # an award project this high in the JD ranking stays in Projects; lower, it moves to Achievements


def achievement_projects(fb: dict, kws: list[Keyword], jd: str = "", hk: hooks.Hook | None = None) -> list[str]:
    """The projects with an `award:` that go to Certifications & Achievements as a bold first line instead of the Projects section: all
    of them unless the project's keyword score puts it in the top TOP_PROJECTS projects for the job (budget.rank_projects), or it holds
    the hook's proof bullet (that bullet is first in its project, so the project stays one)."""
    keep = set(rank_projects(fb, kws, None, jd)[:TOP_PROJECTS]) | ({hk.entry_id} if hk and hk.entry_kind == "project" else set())
    return [p["id"] for p in fb.get("projects", []) if str(p.get("award") or "").strip() and p["id"] not in keep]


def achievement_entries(fb: dict, pids: list[str], kws: list[Keyword]) -> list[dict]:
    """The Achievements lines for these projects: {"id", "name", "award", "bullet_id", "text", "result"}, one per project, its bullet
    the one carrying the most JD keyword weight (the first on a tie). The text is the fact-bank wording with the award taken out
    (the bold lead already says it); nothing is reworded."""
    hits, weight = bullet_hits(fb, kws), [k.weight for k in kws]
    out = []
    for p in fb.get("projects", []):
        if p["id"] not in pids or not p["bullets"]:
            continue
        award = str(p["award"]).strip()
        b = max(p["bullets"], key=lambda b: sum(weight[i] for i in hits[b["id"]]))
        result = " ".join(str(b.get("result") or "").split())
        out.append({"id": p["id"], "name": p["name"], "award": award, "bullet_id": b["id"],
                    "text": typography.strip_award(" ".join(b["text"].split()), award),
                    "result": typography.strip_award(result, award) if result else ""})
    return out


def _context(fb: dict, sel: dict, texts: dict[str, str], profile: str, kws: list[Keyword], headline: str = "",
             skills: dict[str, list[str]] | None = None, hk: hooks.Hook | None = None,
             achievements: list[dict] | None = None) -> dict:
    """The render context for a selection: canonical bullet text unless the guard accepted a rewording (`texts`).
    `skills` = the rows to show (`cv_skills`); default every row, `core` first. `hk` (the hook): its headline goes under the
    name and its proof bullet comes first in its role (or project). `achievements` (achievement_entries): the award projects
    printed under Certifications & Achievements."""
    bidx = bullet_index(fb)
    pos = bullet_positions(fb, hk.proof_bullet_id if hk else None)

    def text(bid: str) -> str:
        return texts.get(bid) or bidx[bid]["text"]

    experience = []
    for e in fb["experience"]:
        ids = sorted(sel["experience"].get(e["id"]) or [e["bullets"][0]["id"]], key=pos.get)       # fact-bank order in the role
        experience.append({**e, "bullet_ids": list(ids), "bullets": [text(i) for i in ids]})
    by_id = {p["id"]: p for p in fb["projects"]}
    chosen = sel.get("project_bullets") or {}
    projects = []
    for p in sel["projects"]:
        if p in by_id:
            ids = sorted(chosen.get(p) or [b["id"] for b in by_id[p]["bullets"]], key=pos.get)
            projects.append({**by_id[p], "bullet_ids": ids, "bullets": [text(i) for i in ids]})
    if skills is None:
        skills = cv_skills(fb, kws)[0]
    ctx = {"id": {**fb["identity"], "headline": headline or fb["identity"].get("headline", "")}, "profile": profile,
            "skills": skills, "experience": experience, "projects": projects, "education": fb.get("education", []),
            "certifications": fb.get("certifications", []),
            # the user's measured outcome per bullet: the render prints it in bold wherever the bullet says it
            "results": {i: " ".join(str(b["result"]).split()) for i, b in bidx.items() if str(b.get("result") or "").strip()},
            # layout.with_openers: no situation text on a verbatim bullet, none next to a bullet that has its own
            "verbatim": sorted(verbatim_ids(fb)), "situated": sorted(situated_ids(fb))}
    if achievements:
        ctx["achievements"] = achievements
    if hk is not None:
        ctx["hook_headline"], ctx["hook_number"] = hk.headline, hk.number
        hooks.proof_first(ctx, hk)
    return ctx


def reconcile_proof(fb: dict, sel: dict, hk: hooks.Hook, kws: list[Keyword] | None = None, jd: str = "") -> tuple[dict, hooks.Hook, list[str]]:
    """`sel` with the hook's proof bullet on it and no chosen bullet next to the one it replaces (`replaces:`), the hook (a copy), and notes.
    A bullet that replaces the proof bullet and says the hook's number, in the same entry, and scores at least REPLACE_RATIO of it (the
    check is skipped without `kws`), takes over as the proof (the one already chosen first, else the best scoring): the user rewrote
    that bullet, the hook's strings are still true of it. Otherwise the proof stays and a chosen bullet that replaces it goes."""
    bidx, owner = bullet_index(fb), {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    notes: list[str] = []
    proof, reps = hk.proof_bullet_id, replacers(fb)
    sc = relevance_scores(fb, kws, jd) if kws else {}
    chosen = set(selected_ids(fb, sel))
    heirs = [r for r, olds in reps.items()
             if proof in olds and owner.get(r) == hk.entry_id and hooks.keeps_number(bidx[r]["text"], hk)
             and (not sc or sc.get(r, 0.0) >= REPLACE_RATIO * sc.get(proof, 0.0))]
    if heirs:
        heir = min(heirs, key=lambda r: (r not in chosen, -sc.get(r, 0.0)))
        hk = dc_replace(hk, proof_bullet_id=heir)
        notes.append(f"hook: {heir} replaces the proof bullet {proof} and says {hk.number or 'the same thing'}; it is the proof bullet now")
    out, dropped = settle_selection(fb, hooks.ensure_selected(sel, fb, hk), protect=hk.proof_bullet_id)
    notes.extend(f"replaces: {d} left off (it replaces the hook's proof bullet {hk.proof_bullet_id}, which stays)"
                 if hk.proof_bullet_id in reps.get(d, ()) else f"replaces: {d} left off (a chosen bullet replaces it)" for d in dropped)
    return out, hk, notes


def letter_summary(letter: dict) -> str:
    """"ok", "fallback (<first rejection reason>)" for a letter that fell back to the deterministic text, or
    "NO LETTER (<why>)" when letter.on_failure is none and every draft was rejected."""
    if letter.get("status") == "ok":
        return "ok"
    if letter.get("status") == "none":
        why = [p for p in letter.get("problems") or [] if p != FALLBACK_MARK]
        return f"NO LETTER ({letter.get('reason') or 'no letter'}" + (f"; first: {why[0]}" if why else "") + ")"
    if letter.get("status") == "off":
        return "off (letter.mode: off)"
    if letter.get("status") == "pending":
        return f"pending ({letter.get('reason') or 'no provider for it'})"
    why = [p for p in letter.get("problems") or [] if p != FALLBACK_MARK]
    return f"{letter.get('status')} ({why[0] if why else 'no reason recorded'})"


MIN_PROJECTS_KEPT = 2           # the shrink loop leaves whole projects alone down to this many until the roles have given up their extra bullets


def _shrink(ctx: dict, kws: list[Keyword], layout: Layout = ONE_PAGE, protect: str | None = None) -> bool:
    """Drop the least keyword-relevant bullet (or, at step 3, a whole project), in this order, each step only once the one before has
    nothing left to give:
      1. the layout's floors: the newest experience entry keeps `layout.first[0]` bullets, the other entries `layout.other[0]`, a project
         `layout.project_bullets[0]` (one page: 2 / 1 / 1), and a role dated within the last `layout.recent_years` years never fewer than
         `layout.recent_min` (two)
      2. the projects fall to one bullet each, the roles untouched
      3. whole projects go, the least relevant first, down to MIN_PROJECTS_KEPT
      4. the least relevant bullet anywhere, but a recent role keeps its two
      5. whole projects go down to the last one, then every entry may fall to its last bullet
    So a role that is recent is trimmed to two bullets only after the projects have given up theirs. `protect` (the hook's proof bullet)
    is never dropped, nor the project that holds it. False if nothing is left to drop."""
    terms = [[norm_literal(w) for w in k.members()] for k in kws]
    first = ctx["experience"][0] if ctx["experience"] else None
    floor_proj = layout.project_bullets[0] if layout.project_bullets else 1
    ref = _selection.today()
    recent = {id(e) for e in ctx["experience"] if layout.recent_min and is_recent(e, layout.recent_years, ref)}

    def relevance(b: str) -> int:
        return sum(any(t in norm_literal(b) for t in group) for group in terms)

    def strict(sec: str, e: dict) -> int:
        floor = floor_proj if sec == "projects" else layout.first[0] if e is first else layout.other[0]
        return max(floor, layout.recent_min if id(e) in recent else 0)

    def drop_project(keep: int) -> bool:
        gone = [(sum(relevance(b) for b in p["bullets"]), -i, p) for i, p in enumerate(ctx["projects"]) if protect not in p["bullet_ids"]]
        if len(ctx["projects"]) <= keep or not gone:
            return False
        worst = min(gone, key=lambda g: g[:2])[2]
        ctx["projects"][:] = [p for p in ctx["projects"] if p is not worst]
        return True

    steps = (strict,                                                            # a function: the floor of an entry for this step
             lambda sec, e: 1 if sec == "projects" else strict(sec, e),
             MIN_PROJECTS_KEPT,                                                 # a number: drop a whole project while more than this many remain
             lambda sec, e: layout.recent_min if id(e) in recent else 1,
             1,
             lambda sec, e: 1)
    for step in steps:
        if isinstance(step, int):
            if drop_project(step):
                return True
            continue
        worst = None
        for sec in ("projects", "experience"):
            for e in ctx[sec]:
                if len(e["bullets"]) <= max(1, step(sec, e)):
                    continue
                for i, b in enumerate(e["bullets"]):
                    if e["bullet_ids"][i] == protect:
                        continue
                    s = relevance(b)
                    if worst is None or s < worst[0]:
                        worst = (s, e, i)
        if worst:
            _, e, i = worst
            e["bullets"].pop(i)
            e["bullet_ids"].pop(i)
            return True
    return False




# ---- 2c'. length rules, measured on the PDF (layout.py)
LENGTH_ROUNDS = 5           # renders spent on one fit: each round fixes everything the last PDF showed too long


def fit_lengths(ctx: dict, render: Callable[[], tuple[Path, int]], fb: dict, kws: list[Keyword], job: dict, llm, cache: dict,
                notes: list[str], hk: hooks.Hook | None = None, state: dict | None = None) -> tuple[Path, int]:
    """Render `ctx` (`render()` -> (pdf, pages)) and bring the PDF within layout's length rules, re-rendering until it is or nothing is
    left to try: the hook headline on one line (shortened a clause at a time), the profile within MAX_PROFILE_LINES (whole sentences after
    the hook sentence dropped), and every bullet within MAX_BULLET_LINES: a bullet that prints longer goes back to its fact-bank
    wording, else is split in two at a sentence or semicolon boundary (`ctx["splits"]`), else is reworded shorter by the model
    (`shorten_pass`, guarded). One that is still too long is left as it is and named in `notes`. `state` ("done": the steps tried per
    bullet, "stats") carries across calls for one context, so a later render never repeats a step. Returns the last render."""
    st = state if state is not None else {}
    done: dict[str, set[str]] = st.setdefault("done", {})
    stats = st.setdefault("stats", {"rounds": 0, "headline": [], "profile_sentences_dropped": 0, "reverted": [], "split": [],
                                    "shortened": [], "unfit": [], "unmeasured": False})
    bidx, locked = bullet_index(fb), verbatim_ids(fb)
    pdf, pages = render()
    for _ in range(LENGTH_ROUNDS):
        m = pdflayout.measure(pdf)
        stats["rounds"] += 1
        changed = False
        if m.headline_lines > 1 and ctx.get("hook_headline"):
            opts = pdflayout.headline_options(ctx["hook_headline"], ctx.get("hook_number") or "")
            if opts:                 # the first version short enough for one line (a line holds about len / (lines - 0.5) characters)
                room = len(ctx["hook_headline"]) / (m.headline_lines - 0.5) * 0.98
                pick = next((o for o in opts if len(o) <= room), opts[-1])
                ctx["hook_headline"] = pick
                if hk is not None:
                    hk.headline = pick
                stats["headline"].append(pick)
                changed = True
        if m.profile_lines > pdflayout.MAX_PROFILE_LINES:
            opts = pdflayout.profile_options(ctx["profile"], hk.profile_line if hk is not None else "")
            if opts:
                room = len(ctx["profile"]) / (m.profile_lines - 0.5) * pdflayout.MAX_PROFILE_LINES * 0.98
                ctx["profile"] = next((o for o in opts if len(o) <= room), opts[-1])
                stats["profile_sentences_dropped"] += 1
                changed = True
        slots = pdflayout.slots(ctx)
        longest: dict[tuple[str, int, int], int] = {}
        if len(m.bullets) >= len(slots):
            for (sec, n, i, _part), lines in zip(slots, m.bullets):
                if lines > pdflayout.MAX_BULLET_LINES:
                    longest[(sec, n, i)] = max(lines, longest.get((sec, n, i), 0))
        else:
            stats["unmeasured"] = True                       # the PDF shows another number of bullets than the context has
        wanted: dict[str, tuple[str, int]] = {}
        where: dict[str, tuple[str, int, int]] = {}
        for (sec, n, i), lines in longest.items():
            e = ctx[sec][n]
            bid, text = e["bullet_ids"][i], e["bullets"][i]
            if bid in locked:                                # verbatim: never split, reworded or shortened; reported below
                continue
            tried = done.setdefault(bid, set())
            if "revert" not in tried and text != bidx[bid]["text"]:
                tried.add("revert")
                e["bullets"][i] = bidx[bid]["text"]
                stats["reverted"].append(bid)
                changed = True
            elif "split" not in tried:
                tried.add("split")
                opts = pdflayout.split_options(text)
                if opts:
                    ctx.setdefault("splits", {})[bid] = {"text": text, "parts": opts[0]}
                    stats["split"].append(bid)
                    changed = True
                else:                                        # nothing to cut at: straight on to a shorter wording
                    lead = len(pdflayout.opener(e.get("situation")).split()) if i == 0 and e.get("situation") else 0
                    tried.add("shorten")
                    wanted[bid] = (text, max(8, int((len(text.split()) + lead) * pdflayout.MAX_BULLET_LINES / lines * 0.92) - lead))
                    where[bid] = (sec, n, i)
            elif "shorten" not in tried:
                tried.add("shorten")
                lead = len(pdflayout.opener(e.get("situation")).split()) if i == 0 and e.get("situation") else 0
                wanted[bid] = (text, max(8, int((len(text.split()) + lead) * pdflayout.MAX_BULLET_LINES / lines * 0.92) - lead))
                where[bid] = (sec, n, i)
        if wanted:
            for bid, new in shorten_pass(job, fb, wanted, kws, llm, cache, notes, hk).items():
                sec, n, i = where[bid]
                ctx[sec][n]["bullets"][i] = new
                stats["shortened"].append(bid)
                changed = True
        if not changed:
            stats["unfit"] = [{"id": ctx[sec][n]["bullet_ids"][i], "lines": lines} for (sec, n, i), lines in longest.items()]
            for u in stats["unfit"]:
                msg = (f"length: {u['id']} prints {u['lines']} lines (limit {pdflayout.MAX_BULLET_LINES}); it is a verbatim bullet, so it is "
                       "never split or reworded and is left as the fact bank has it" if u["id"] in locked else
                       f"length: {u['id']} prints {u['lines']} lines (limit {pdflayout.MAX_BULLET_LINES}); it has no sentence or semicolon "
                       "to split at and the model could not shorten it without losing a fact, so it is left as the fact bank has it")
                if msg not in notes:
                    notes.append(msg)
            break
        pdf, pages = render()
    return pdf, pages


# ---- 2d. two pages: keep adding bullets until page 2 is full enough
LEADING_PT = 13.6 * 1.03                           # the 11pt body of cv2.tex.j2 (11pt article baselineskip x linespread 1.03)
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


def _insert(e: dict, bid: str, text: str, pos: dict[str, int]) -> None:
    """Put a bullet into an entry at its fact-bank place among the bullets already there."""
    at = next((i for i, x in enumerate(e["bullet_ids"]) if pos[x] > pos[bid]), len(e["bullet_ids"]))
    e["bullet_ids"].insert(at, bid)
    e["bullets"].insert(at, text)


def _remove(e: dict, bid: str) -> None:
    i = e["bullet_ids"].index(bid)
    e["bullet_ids"].pop(i)
    e["bullets"].pop(i)


def fill_page2(ctx: dict, fb: dict, order: list[str], texts: dict[str, str], rerender: Callable[[], tuple[Path, int]],
               min_fill: float, skip: set[str] | None = None, first: str | None = None) -> dict:
    """Add bullets from `order` (selection.fill_order) until page 2 is at least `min_fill` full or the bullets run out,
    never past two pages. Each round adds as many bullets as the size guess says it needs, renders, and if that tipped the
    CV onto a third page takes them back, last first (those bullets are not tried again). `rerender()` renders `ctx` and
    returns (pdf, pages). Returns {"added", "rejected_overflow", "renders", "reached"}."""
    bidx, pos = bullet_index(fb), bullet_positions(fb, first)           # `first`: the hook's proof bullet stays first in its entry
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
            _insert(entry(bid), bid, texts.get(bid) or bidx[bid]["text"], pos)
        pdf, pages = rerender()
        stats["renders"] += 1
        while pages > 2 and batch:                        # overshot onto a third page: take the last one back
            bid = batch.pop()
            e = entry(bid)
            _remove(e, bid)
            if not e["bullet_ids"]:                       # a project added only by this bullet goes with it
                ctx["projects"][:] = [p for p in ctx["projects"] if p is not e]
            stats["rejected_overflow"].append(bid)
            pdf, pages = rerender()
            stats["renders"] += 1
        stats["added"] += batch
        used = _used(pdf, pages) if pages <= 2 else 3.0
    stats["reached"] = pages == 2 and page2_fill(pdf, pages) >= min_fill
    return stats


OPTIONAL_SECTIONS = ("publications", "languages", "research_focus")   # fact-bank keys, in the order fill_tail tries them
LINESPREAD_STEP = 0.02


def fill_tail(ctx: dict, fb: dict, rerender: Callable[[], tuple[Path, int]], min_fill: float,
              linespread_max: float) -> dict:
    """What is left to do when `fill_page2` has used every bullet and page 2 is still short: add the optional fact-bank
    sections that exist (publications, languages, research_focus, in that order; one that would push the CV onto a third page
    is taken back), then raise the linespread from ctx["linespread"] in steps of 0.02 up to `linespread_max`, stopping at the
    first value that fills page 2 and keeping the last one that still fits on two pages. Text is never invented and the font
    is never shrunk. Returns {"sections", "linespread", "renders", "reached"}."""
    stats: dict = {"sections": [], "linespread": ctx["linespread"], "renders": 0, "reached": False}

    def render() -> tuple[Path, int, float]:
        pdf, pages = rerender()
        stats["renders"] += 1
        return pdf, pages, page2_fill(pdf, pages)

    pdf, pages, fill = render()
    for key in OPTIONAL_SECTIONS:
        if pages != 2 or fill >= min_fill:
            break
        value = fb.get(key)
        if not value or ctx.get(key):
            continue
        ctx[key] = value
        pdf, pages, fill = render()
        if pages > 2:
            del ctx[key]
            pdf, pages, fill = render()
        else:
            stats["sections"].append(key)
    ls = ctx["linespread"]
    while pages == 2 and fill < min_fill and round(ls + LINESPREAD_STEP, 2) <= linespread_max + 1e-9:
        ls = round(ls + LINESPREAD_STEP, 2)
        ctx["linespread"] = ls
        pdf, pages, fill = render()
        if pages > 2:                                     # one step too far: keep the last value that fit
            ctx["linespread"] = round(ls - LINESPREAD_STEP, 2)
            pdf, pages, fill = render()
            break
    stats["linespread"] = ctx["linespread"]
    stats["reached"] = pages == 2 and fill >= min_fill
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


def _lack_problems(text: str, gaps: list[str], bridge: list[str] | None = None) -> list[str]:
    """Sentences saying the candidate lacks something. Each must name a term in GAPS and no other tool, platform,
    language or framework; with GAPS empty every such sentence is a problem. `bridge` (a hook-mode gap sentence): the skill the
    candidate does have, which the same sentence may name ("I have not used dbt yet, but SQL is ...")."""
    allowed = [norm_literal(g) for g in gaps]
    named_elsewhere = gap_terms() - set(allowed) - {norm_literal(b) for b in bridge or []}
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


def letter_evidence(fb: dict, kws: list[Keyword], chosen_ids: set[str] | None = None, jd: str = "") -> list[dict]:
    """The two bullets the letter is written from, chosen in code from the bullets on the CV. EVERY bullet (experience and
    projects, verb-led or noun-led) is ranked by overlap with the JD keywords plus overlap with the JD's company domain
    (bank, payments, customers ...; budget.letter_scores): the best one, then the best of the rest from a different entry
    (two lines about one job read as one example). The text stays the canonical fact-bank text, as [{"id", "text",
    "terms"}]; fewer than two when the CV has fewer bullets."""
    sc = letter_scores(fb, kws, jd)
    bidx = bullet_index(fb)
    owner = {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    where = {b["id"]: entry_where(e) for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    order = {b: i for i, b in enumerate(sc)}
    ranked = sorted((b for b in sc if not chosen_ids or b in chosen_ids), key=lambda b: (-sc[b], order[b]))
    picked = ranked[:1]
    if len(ranked) > 1:
        picked.append(next((b for b in ranked[1:] if owner[b] != owner[ranked[0]]), ranked[1]))
    extra = lambda b: {k: bidx[b][k] for k in evidence_fields.OPTIONAL_KEYS if str(bidx[b].get(k) or "").strip()}   # noqa: E731
    return [{"id": b, "text": bidx[b]["text"], "terms": list(bidx[b].get("terms", [])), "where": where[b], **extra(b)}
            for b in picked]


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


def letter_mode(settings) -> str:
    """settings letter.mode: "off" = no letter is asked for and no letter call is made; "draft" = the model drafts one from the
    evidence fields (evidence_fields.py), under every letter check; "hook" = two letters, A (strengths) and B (A plus one honest gap),
    each opening with the hook's sentence (letters.py). "hook" needs the hook: with hook.enabled false a "draft" letter is made; with
    the hook on but none buildable both letters are "none"."""
    raw = settings["letter"].get("mode")
    mode = "off" if raw is None or raw is False else str(raw).strip().lower()          # YAML reads an unquoted `off` as False
    if mode not in LETTER_MODES:
        raise ValueError(f"letter.mode must be one of {', '.join(LETTER_MODES)}, got {mode!r}")
    return mode


def letter_policy(settings) -> str:
    """settings letter.on_failure: "none" (default) = a letter that fails every draft is not produced at all;
    "template" = the deterministic template letter is written instead (flagged as a fallback in review)."""
    policy = str((settings["letter"].get("on_failure") or "none")).strip().lower()
    if policy not in LETTER_POLICIES:
        raise ValueError(f"letter.on_failure must be one of {', '.join(LETTER_POLICIES)}, got {policy!r}")
    return policy


def _letter(job, fb, kws, hook, evidence: list[dict], llm, settings, gaps: list[str] | None = None,
            notes: list[str] | None = None, team: str = "",
            retry: dict | None = None, template: bool = True) -> tuple[list[str], list[str]]:
    """The cover letter from the company hook, the two evidence bullets and the optional GAPS. The model writes the body;
    the closing question is added here from settings (`letter_closing`). A draft is checked (length, banned phrases, deny
    list, claimed gaps, role upgrades, numbers, and every sentence against its own evidence bullet and the JD) and retried
    twice, naming the words it was rejected for; a third failure is the deterministic fallback, flagged, or with
    `template=False` (letter.on_failure: none) no letter at all: ([], problems + [FALLBACK_MARK]). The prompt lists
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
    retry.update({"count": 0, "rejected_words": [], "first_problems": [], "failures": [], "removed": [],
                  "outcome": "first draft"})
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
        out += repeated_evidence_problems(body, evidence)
        out += letters_mod.claim_problems(body, job["company"], fb)          # no "our" / "we", no "I help <Company>", no linked noun phrase
        out += term_dump_problems(body, fb)
        out += hook_link_problems(body[0], evidence) if body else []
        out += style + [p for p in g_first + g_rest + claim if p not in style]
        if extra:
            out.append(f"new numbers {sorted(extra)}")
        return list(dict.fromkeys(out))

    body: list[str] = []
    avoided: list[str] = []                 # every word rejected so far: a retry must not swap one unsupported word for another
    failures: list[dict] = []
    retry["failures"] = failures
    for attempt in range(LETTER_ATTEMPTS):
        try:
            out = _llm_json(
                llm, "cover_letter",
                P.LETTER_SYSTEM.replace("{max_words}", str(max_words))
                .replace("{deny}", ", ".join(d for d in deny if d not in named_ok))
                .replace("{qualifiers}", ", ".join(prompt_list(" ".join(texts)))) + P.LETTER_FIELDS_NOTE,
                P.LETTER_USER.format(title=job["title"], company=job["company"], company_hook=hook or "(none)",
                                     gaps=", ".join(gaps) or "(none)", avoid=", ".join(avoid) or "(none)",
                                     evidence=evidence_fields.block(evidence)) + feedback,
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
        fixed = repair(body, problems, draft_problems)       # only unsupported qualifiers wrong: delete them, re-check it all
        if fixed:
            body, removed = fixed
            retry["removed"] = removed
            retry["outcome"] = "qualifiers removed" + (" after retry" if attempt else "")
            if notes is not None:
                notes.append(f"cover_letter: removed unsupported qualifier(s) {', '.join(removed)}; the full check passed")
            return [*body, closing], []
        failures.append({"attempt": attempt + 1, "problems": list(problems)})       # the review screen shows every one
        avoided += [w for w in rejected_words(problems) if w not in avoided]
        if attempt == 0:
            retry["first_problems"] = list(problems)
        retry["rejected_words"] = list(avoided)
        if attempt + 1 < LETTER_ATTEMPTS:                      # another go, told exactly what the check rejected
            retry["count"] = attempt + 1
            if notes is not None:
                notes.append(f"cover_letter: draft {attempt + 1} rejected ({'; '.join(problems)[:200]}); "
                             f"retry {attempt + 1} of {LETTER_ATTEMPTS - 1}"
                             + (f", naming: {', '.join(avoided)}" if avoided else ""))
        feedback = (("\n\nThe check rejected these words as unsupported: " + ", ".join(f"'{w}'" for w in avoided) + ". "
                     + " ".join(f"Do not use '{w}' unless it appears in the evidence bullets." for w in avoided))
                    if avoided else "")
        feedback += "\n\nYOUR LAST DRAFT BROKE THESE RULES; fix every one:\n" + "\n".join(f"- {p}" for p in problems)
    retry["outcome"] = ("fallback" if template else "no letter") if retry["count"] else retry["outcome"]
    if not template:
        return [], problems + [FALLBACK_MARK]
    # deterministic fallback — always honest and first person; review will show why (letter_rules.fallback_letter)
    return (fallback_letter(job, hook, _with_orgs(fb, evidence), gaps, closing, deny, job["description"]),
            problems + [FALLBACK_MARK])


def _applicant(settings) -> dict:
    """applicant.yaml (the relocation line is read from it; nothing from it is sent to a model), {} when it cannot be read."""
    try:
        return load_yaml(settings.path("applicant")) or {}
    except (OSError, ValueError, KeyError):
        return {}


def _final_path(p, out_dir: Path, final_dir: Path) -> str | None:
    """A path under the staging folder as it will be once the folder is swapped in."""
    return str(final_dir / Path(p).relative_to(out_dir)) if p else None


def _letters(job, fb, kws, hook, hk, evidence: list[dict], llm, settings, out_dir: Path, letter_name: str, team: str,
             gaps: list[str], chosen_ids: set[str], claimable: list[str], possible: dict | None, hook_failed: bool = False):
    """(letter pdf or None, the letter dict `report["letter"]` shows, the two variants or None). In letter.mode `hook` with a hook:
    letters A and B (letters.py), the current choice is A when it passed its checks. In mode `hook` when the hook is on but none
    could be built: both letters are "none" (no old single letter takes their place, so the letter settings are never silently
    bypassed). With hook.enabled false, or in mode `draft`, the one letter of `_make_letter`."""
    if letter_mode(settings) == "hook" and hook_failed and hk is None:
        from . import letters as L

        letters = L.none_letters("no hook could be built for this job, and a hook-mode letter opens with the hook's sentence")
        return None, L.current(letters), letters
    if letter_mode(settings) == "hook" and hk is not None:
        from . import letters as L

        letters = L.make_letters(job, fb, kws, hook, hk, llm, settings, out_dir, letter_name, team, chosen_ids, claimable, possible,
                                 _applicant(settings))
        letter = L.current(letters)
        return (Path(letter["pdf"]) if letter.get("pdf") else None), letter, letters
    pdf, letter = _make_letter(job, fb, kws, hook, evidence, llm, settings, out_dir, letter_name, team, gaps)
    return pdf, letter, None


def _publish_letters(letters: dict | None, out_dir: Path, final_dir: Path) -> dict | None:
    """The variants with their file paths as they will be after the folder swap."""
    if not letters:
        return None
    out = {k: v for k, v in letters.items() if k not in ("A", "B")}
    for v in ("A", "B"):
        out[v] = {**letters[v], "pdf": _final_path(letters[v].get("pdf"), out_dir, final_dir),
                  "tex": _final_path(letters[v].get("tex"), out_dir, final_dir)}
    return out


def ats_by_variant(cv_text: str, kws: list[Keyword], claimable: list[str], letters: dict | None, band: tuple[float, float]) -> dict:
    """The CV + letter and letter-alone ATS numbers of each letter variant that exists ({} without variants)."""
    out = {}
    for v in ("A", "B"):
        let = (letters or {}).get(v) or {}
        if let.get("status") == "ok":
            s = ats.scores(cv_text, kws, claimable, "\n\n".join(let["paragraphs"]), band)
            out[v] = {"combined": s.combined, "letter": s.letter, "letter_negated": s.letter_negated}
    return out


def _make_letter(job, fb, kws, hook, evidence: list[dict], llm, settings, out_dir: Path, final_name: str,
                 team: str = "", gaps: list[str] | None = None):
    """Write the cover letter PDF. A failed or rule-breaking LLM draft becomes, by letter.on_failure, the deterministic
    fallback ("template": status "fallback") or no letter at all ("none", the default: status "none", no PDF, review
    says NO LETTER). report["letter"]["status"] says which of ok / fallback / none / pending the reader is looking at."""
    if letter_mode(settings) == "off":     # nothing was asked for: no model call, no PDF, no fallback
        return None, {"paragraphs": [], "problems": [], "score": None, "pages": 0, "status": "off",
                      "reason": "letter.mode: off", "notes": [],
                      "evidence": [{"id": e["id"], "text": e["text"]} for e in evidence]}
    notes: list[str] = []
    retry: dict = {}
    template = letter_policy(settings) == "template"
    try:
        paras, problems = _letter(job, fb, kws, hook, evidence, llm, settings, gaps, notes, team, retry, template)
    except TaskUnavailable as e:        # no provider may write it now: no letter, no fallback, the job waits
        return None, {"paragraphs": [], "problems": [], "score": None, "pages": 0, "status": "pending",
                      "reason": str(e)[:300], "notes": notes,
                      "evidence": [{"id": x["id"], "text": x["text"]} for x in evidence]}
    if not paras:                       # letter.on_failure: none, and every draft was rejected: no letter, nothing to render
        failed = len(retry.get("failures") or [])
        return None, {"paragraphs": [], "problems": [p for p in problems if p != FALLBACK_MARK], "score": None, "pages": 0,
                      "status": "none", "reason": (f"{failed} draft(s) rejected" if failed else "the letter model failed"),
                      "notes": notes, "retry": retry,
                      "evidence": [{"id": e["id"], "text": e["text"]} for e in evidence]}
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paras,
                         "date": date.today().strftime("%d %B %Y"),
                         "greeting": letter_greeting(settings, job["company"], team),
                         "signoff": settings["letter"]["signoff"]}, out_dir, final_name)
    return pdf, {"paragraphs": paras, "problems": problems, "score": ats.score(pdf_text(pdf), kws).literal,
                 "pages": page_count(pdf), "status": "fallback" if FALLBACK_MARK in problems else "ok",
                 "notes": notes, "retry": retry, "evidence": [{"id": e["id"], "text": e["text"]} for e in evidence]}


def date_separator(settings) -> str:
    """settings cv.date_separator: what stands between the two dates of a range, " – " (default) or " to ", always one space
    each side."""
    raw = str(((getattr(settings, "raw", None) or {}).get("cv") or {}).get("date_separator") or "").strip()
    return f" {raw} " if raw else DATE_SEPARATOR


def hyphen_keywords(kws: list[Keyword]) -> list[str]:
    """The JD keywords spelt with a hyphen, and the hyphenated words inside a longer keyword ("time-series" in "time-series forecasting"):
    the typography leaves those hyphens alone, because the ATS looks for the JD's own spelling."""
    return list(dict.fromkeys(x for k in kws for w in k.members() if "-" in w for x in (w, *(t for t in w.split() if "-" in t))))


def typography_report(ctx: dict, text: str, sep: str, keep: list[str]) -> dict:
    """The typography of a rendered CV for the review screen: the `typography.audit` of its text (em dashes, en dashes in prose,
    hyphenated words left), how many bullets show their `result:` in bold and which say it in other words (no bold), and any
    bullet that repeats its project's award."""
    t = typography.typeset(ctx, keep=keep)
    results = t.get("results") or {}
    bold, unmatched, repeats = 0, [], []
    for sec in ("experience", "projects"):
        for e in t[sec]:
            for i, bid in enumerate(e.get("bullet_ids") or []):
                if results.get(bid):
                    if result_span(e["bullets"][i], results[bid]):
                        bold += 1
                    else:
                        unmatched.append(bid)
                if e.get("award") and typography.award_repeats(e["bullets"][i], e["award"]):
                    repeats.append(bid)
    return {**typography.audit(text, keep=keep), "date_separator": sep, "keep": list(keep), "results_bold": bold,
            "results_unmatched": unmatched, "award_repeats": repeats}


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


def usage_by_model(calls: list[dict]) -> dict[str, dict]:
    """Per "<provider>/<model>" that answered: the number of calls and the summed token usage, so the review screen shows
    what the letter model (llm.models) cost next to the model that did everything else."""
    out: dict[str, dict] = {}
    for c in calls:
        m = out.setdefault(f"{c.get('provider') or '?'}/{c.get('model') or '?'}",
                           {"calls": 0, **{k: 0 for k in USAGE_SUMS}, "tasks": []})
        m["calls"] += 1
        for k in USAGE_SUMS:
            m[k] += c.get(k) or 0
        if (c.get("task") or "?") not in m["tasks"]:
            m["tasks"].append(c.get("task") or "?")
    return out


def usage_since(llm, start: int) -> dict:
    """Token usage of the LLM calls made since `start` (an index into llm.usage): the calls, their sums and the same per
    task and per model. Empty for a model that records none (MockLLM)."""
    calls = list(getattr(llm, "usage", [])[start:])
    total = {k: sum(c.get(k) or 0 for c in calls) for k in USAGE_SUMS}
    return {"calls": calls, **total, "by_task": usage_by_task(calls), "by_model": usage_by_model(calls)}


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
    evidence = letter_evidence(fb, kws, ids, job["description"])
    hk = hooks.Hook.from_dict(rep["hook"]) if rep.get("hook") else None
    pdf, letter, letters = _letters(job, fb, kws, rep.get("company_hook") or "", hk, evidence, llm, settings, out_dir, stem,
                                    rep.get("team") or "", required_gaps(rep.get("genuine_gaps") or [], kws), ids,
                                    rep.get("claimable") or [], rep.get("possible_evidence"), hooks.enabled(settings) and hk is None)
    rep["letter"], rep["letter_pdf"] = letter, str(pdf) if pdf else None
    rep["letters"], rep["letter_choice"] = letters, letters["choice"] if letters else None
    if rep.get("ats") is not None:
        cv_text = pdf_text(Path(rep["cv_pdf"]))
        band = tuple(rep.get("band_pct") or (70.0, 85.0))
        rep["ats"] = {**ats.scores(cv_text, kws, rep.get("claimable") or [], pdf_text(pdf) if pdf else None, band).to_dict(),
                      "variants": ats_by_variant(cv_text, kws, rep.get("claimable") or [], letters, band)}
    note_mod.write(job, rep, fb, out_dir)                                     # the note names letter B's gap
    new, old = usage_since(llm, usage_from), rep.get("llm_usage") or {}      # the re-draft adds to the job's total
    calls = (old.get("calls") or []) + new["calls"]
    rep["llm_usage"] = {"calls": calls, **{k: (old.get(k) or 0) + new[k] for k in USAGE_SUMS},
                        "by_task": usage_by_task(calls), "by_model": usage_by_model(calls)}
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


RENAME_RETRIES = 3       # a folder rename that Windows refuses (a viewer or the virus scanner holds a file in it) is tried again this often,
RENAME_WAIT_S = 1.0      # this many seconds apart, before the files are copied instead


def _rename(src: Path, dst: Path) -> bool:
    """os.replace, retried on PermissionError; False when it still fails."""
    for attempt in range(RENAME_RETRIES + 1):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            if attempt < RENAME_RETRIES:
                time.sleep(RENAME_WAIT_S)
    return False


def _files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file())


def _locked(folder: Path) -> list[str]:
    """The files of `folder` that Windows will not let this process open for writing."""
    out = []
    for f in _files(folder):
        try:
            open(f, "ab").close()
        except PermissionError:
            out.append(f.relative_to(folder).as_posix())
    return out


def _copy_over(src: Path, dst: Path) -> None:
    for f in _files(src):
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)


def swap_in(work: Path, final: Path, root: Path, out: Callable[[str], None] = print) -> None:
    """Put the finished staging folder `work` in place of `final` (an earlier output, moved aside to <final>.old first). A rename that
    Windows refuses is retried; if it still fails the files are copied into `final` and the staging folder is deleted, and the file
    Windows holds is printed. An earlier output that was moved aside is only deleted once the new one is complete."""
    old = final.with_name(final.name + ".old")
    _discard(old, root)
    aside = final.exists() and _rename(final, old)
    if final.exists() and not aside:
        out(f"  Windows will not move {final.name} aside (locked: {', '.join(_locked(final)) or 'a file in it'}); "
            "the new files are copied over it instead.")
    elif not _rename(work, final):
        held = _locked(work)
        out(f"  Windows will not rename {work.name} (locked: {', '.join(held) or 'a file in it'}); its files are copied into {final.name} instead.")
    else:
        _discard(old, root)
        return
    try:
        _copy_over(work, final)
    except Exception:
        if aside:                                               # the earlier output is still there, whole, as <job>.old
            out(f"  The copy failed; the earlier output is kept as {old.name}.")
        raise
    _discard(work, root)
    if work.exists():
        out(f"  Windows has locked {', '.join(p.relative_to(work).as_posix() for p in _files(work))} in {work.name}: the new folder "
            f"{final.name} is complete; delete {work.name} by hand when the file is free.")
    _discard(old, root)


def tailor_job(job: dict, fb: dict, llm, settings, out_root: Path, frozen: dict | None = None,
               one_page: bool = False, on_extracted: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Tailor one job into out_root/<company_title_id>/. Everything is built in a '.partial' sibling folder and swapped
    in only once the whole job has succeeded, so a run that fails part-way (LLM down, xelatex error) leaves an
    earlier successful output folder exactly as it was. `on_extracted(record)` is called with the job's fresh keyword
    analysis (freeze.record) as soon as the model has given it, before anything else can fail, unless `frozen` was used
    or the model call failed (a taxonomy-only list is never frozen)."""
    final = out_root / folder_name(job)
    work = final.with_name(final.name + ".partial")
    _discard(work, out_root)                                    # a leftover of a run that was killed
    try:
        report = _tailor(job, fb, llm, settings, work, final, frozen, one_page, on_extracted)
        swap_in(work, final, out_root)                          # the earlier output goes (.old), or is copied over when Windows holds it
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


def _keep_proof_number(ctx: dict, fb: dict, hk: hooks.Hook) -> None:
    """After the wording pass: a proof bullet whose new wording lost the hook's number goes back to its fact-bank text."""
    canonical = bullet_index(fb)[hk.proof_bullet_id]["text"]
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for i, bid in enumerate(e["bullet_ids"]):
                if bid == hk.proof_bullet_id and not hooks.keeps_number(e["bullets"][i], hk):
                    e["bullets"][i] = canonical


def refresh_ats(rep: dict, cv_text: str, letter_text: str | None = None) -> dict:
    """The three ATS numbers (ats.py) of a report after its CV text changed (review's edit): `rep["ats"]` is replaced, and returned.
    `letter_text` None = read the report's own letter PDF, when it has one."""
    kws = [Keyword(**k) for k in rep["keywords"]]
    if letter_text is None and rep.get("letter_pdf") and Path(rep["letter_pdf"]).exists():
        letter_text = pdf_text(Path(rep["letter_pdf"]))
    band = tuple(rep.get("band_pct") or (70.0, 85.0))
    rep["ats"] = ats.scores(cv_text, kws, rep.get("claimable") or [], letter_text, band).to_dict()
    if rep.get("letters"):
        rep["ats"]["variants"] = ats_by_variant(cv_text, kws, rep.get("claimable") or [], rep["letters"], band)
    rep["stretch"] = rep["ats"]["gate"] == "stretch"
    return rep["ats"]


def hook_placement(ctx: dict, hk: hooks.Hook | None, text: str) -> dict:
    """Where the hook ended up on the finished CV, for the review screen and the note: {"headline", "profile", "bullet",
    "number_in_pdf"} (each True/False), or {} without a hook. The headline and the profile line are in the context the PDF was
    rendered from; the proof bullet must be the first bullet of its entry."""
    if hk is None:
        return {}
    sec = "experience" if hk.entry_kind == "experience" else "projects"
    first = next((e["bullet_ids"][0] for e in ctx[sec] if e.get("id") == hk.entry_id and e["bullet_ids"]), None)
    return {"headline": ctx.get("hook_headline") == hk.headline, "profile": ctx["profile"].startswith(hk.profile_line),
            "bullet": first == hk.proof_bullet_id, "number_in_pdf": (not hk.number) or hk.number in text}


def _tailor(job: dict, fb: dict, llm, settings, out_dir: Path, final_dir: Path,
            frozen: dict | None = None, one_page: bool = False,
            on_extracted: Callable[[dict], None] | None = None) -> dict[str, Any]:
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
    context_terms: list[dict] = []
    kws, hook, team = analyse(job, llm, settings, analysis_warnings, frozen, context_terms)
    if on_extracted and frozen is None and not any(LLM_FAILED in n for n in analysis_warnings):
        on_extracted(freeze.record(kws, hook, team, context_terms, analysis_warnings, job["description"] or ""))
    ceil, all_gaps = ceiling(fact_text(fb), kws)
    # the claimable ceiling (ats.py): the keywords the fact bank backs, a deny-listed term never among them
    claimable = ats.claimable_terms(kws, all_gaps, fb.get("deny_terms", []))
    ceil = min(ceil, ats.claimable_ceiling(kws, claimable) / 100)
    # A gap with a neighbour term in the fact bank is possible evidence, for the user to judge: never counted, never claimed,
    # and not called a genuine gap either (so the letter cannot say "I have not used it").
    alts = {k.term: k.alts for k in kws if k.alts}
    possible = possible_evidence(fb, all_gaps, alts)
    gaps = [g for g in all_gaps if g not in possible]
    jd = job["description"] or ""
    groups_cfg = {} if one_page else skill_group_config(settings)
    from_family = gates.skills_from(settings, family)       # the bank family a role family without skill_lines of its own prints
    fam = None if one_page else pick_family(fb, kws, family, from_family)   # the bank family whose `match` terms carry the most weight for the ad
    if fam:                     # its skill_lines, word for word (the bank's own labels and items; nothing regrouped or reordered)
        skills, dropped_groups = family_skill_lines(fb, kws, family, from_family) or {}, []
        left = family_basics_missing(skills, fb, list((groups_cfg or {}).get("skills_always") or []))
        if left:
            analysis_warnings.append(f"skills: the '{fam['id']}' family's skill_lines do not mention {', '.join(left)}, which the fact bank "
                                     "lists (the lines are used as written; add them to the lines if you want them on the CV)")
    elif groups_cfg:            # the two-page CV: the four fixed Skills groups of settings cv.skill_groups
        skills, dropped_groups, empty_groups = cv_skill_groups(fb, kws, groups_cfg, jd)
        analysis_warnings.extend(f"skills: the fact bank has no skills for the group '{g}', so the CV leaves it off (add a skills group "
                                 "named like it to the fact bank)" for g in empty_groups)
    else:
        skills, dropped_groups = cv_skills(fb, kws, None if one_page else sc["max_skill_groups"], jd)
        if wants_databases(jd) and not any(is_database_row(g) for g in skills):
            analysis_warnings.append("skills: the JD mentions SQL or databases but the fact bank has no Databases skill row to show")
    sep, keep = date_separator(settings), hyphen_keywords(kws)
    cv_name, letter_name = _names(fb, settings)
    # the hook (hook.py): the fact bank's hook_library entry for this ad, word for word: in the headline, profile line 1, the first
    # bullet of its role and sentence 1 of the letter. No model and no template writes any of it; no qualifying entry = the family headline.
    hook_notes: list[str] = []
    hk = hooks.build_hook(job, kws, fb, llm, headline, team, hook, notes=hook_notes) if hooks.enabled(settings) else None
    hook_failed = hooks.enabled(settings) and hk is None          # on, but no entry qualified: say so, never drop it silently
    if hook_failed:
        analysis_warnings.append(f"hook: no library hook qualifies, so the CV headline is the role family's '{headline}' with no number, "
                                 "and there is no hook line, no proof bullet first and no hook letter (" + "; ".join(hook_notes)[:400] + ")")
    profile = assemble_profile(fb, kws, jd, hk)                  # the hook's profile line + profile_facts sentences, in code
    profile_text = profile["text"]
    # an award project moves to Certifications & Achievements unless the JD ranks it in the top three (or it holds the hook's proof):
    # the fact bank the selection sees then lacks it, so it neither takes a project slot nor is offered to the fill
    moved = [] if one_page else achievement_projects(fb, kws, jd, hk)
    fb_sel = {**fb, "projects": [p for p in fb["projects"] if p["id"] not in moved]} if moved else fb
    achievements = achievement_entries(fb, moved, kws)
    report: dict[str, Any] = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                              "family": family, "tier": tier, "headline": headline,
                              "keywords": [asdict(k) for k in kws], "company_hook": hook, "team": team,
                              "ceiling": ceil, "genuine_gaps": gaps, "possible_evidence": possible,
                              "context_terms": context_terms, "attempts": [], "out_dir": str(final_dir),
                              "analysis_warnings": analysis_warnings, "one_page": one_page,
                              "skills": {"core": core_skills(fb), "rows": list(skills), "dropped_groups": dropped_groups,
                                         "family": fam["id"] if fam else None},
                              "achievements": [a["id"] for a in achievements],
                              "profile": {k: profile[k] for k in ("facts", "retried", "fallback")},
                              "hook": hk.to_dict() if hk else None, "hook_notes": hook_notes,
                              "hook_family_headline": headline if hook_failed else ""}

    jac = sc["bullet_dedupe_jaccard"]
    ranking, best, cache = kws, None, {}
    for attempt in range(1, sc["max_attempts"] + 1):
        llm.attempt = attempt                                    # names this attempt's raw replies (tailor_profile-2.json)
        picked = select_cv(fb_sel, ranking, preferred, skills=skills, layout=layout, jaccard_limit=jac, jd=jd)
        rephrase_notes: list[str] = []
        if hk:           # the proof bullet is always on the CV (or the bullet that replaces it); no bullet next to the one it replaces
            sel, hk, proof_notes = reconcile_proof(fb, picked, hk, kws, jd)
            rephrase_notes.extend(proof_notes)
            report["hook"] = hk.to_dict()
        else:
            sel, _ = settle_selection(fb, picked)
        proposed, rstats = propose_rephrases(job, fb, sel, kws, llm, cache, rephrase_notes,
                                             other_text=profile_text + " " + skills_text(skills))
        accepted, viol, warn = guard_rephrases(fb, proposed, kws)
        if hk and hk.proof_bullet_id in accepted and not hooks.keeps_number(accepted[hk.proof_bullet_id], hk):
            del accepted[hk.proof_bullet_id]                     # a rewording that drops the hook's number is not the proof any more
            rephrase_notes.append(f"hook: the rewording of {hk.proof_bullet_id} lost {hk.number}; the fact-bank wording is kept")
        rstats.update(proposed=len(proposed), accepted=len(accepted), blocked=len(proposed) - len(accepted),
                      regressed=sum(f": {NO_REGRESSION}: " in w for w in warn))
        ctx = _context(fb, sel, accepted, profile_text, kws, headline, skills, hk, achievements)
        ctx["linespread"] = sc["cv_linespread"]
        fit_state: dict = {}

        def render_once() -> tuple[Path, int]:
            out = render_cv(ctx, out_dir, cv_name, one_page, sep, keep)
            return out, page_count(out)

        def rerender() -> tuple[Path, int]:
            """Every render of the two-page CV keeps to the length rules (fit_lengths); the one-page CV is rendered as it is."""
            if one_page:
                return render_once()
            return fit_lengths(ctx, render_once, fb, kws, job, llm, cache, rephrase_notes, hk, fit_state)

        pdf, pages = rerender()
        shrinks = 0
        while pages > max_pages and _shrink(ctx, kws, layout, hk.proof_bullet_id if hk else None):
            pdf, pages = rerender()
            shrinks += 1
        fill: dict = {"added": [], "rejected_overflow": [], "renders": 0, "reached": None, "sections": [],
                      "linespread": ctx["linespread"]}
        # two pages: when nothing had to be shrunk away, keep adding until page 2 is full enough (or nothing is left):
        # further projects, more experience bullets, optional fact-bank sections, then linespread (fill_tail)
        fill_on = not one_page and max_pages == 2 and shrinks == 0
        order = (fill_order(fb_sel, ranking, sel, skills, jac, sc["cv_projects_max"], jd) if fill_on else [])

        def fill_up(skip: set[str] | None = None) -> dict:
            more = fill_page2(ctx, fb, order, accepted, rerender, sc["min_fill_page2"], skip=skip,
                              first=hk.proof_bullet_id if hk else None)
            if not more["reached"]:
                tail = fill_tail(ctx, fb, rerender, sc["min_fill_page2"], sc["cv_linespread_max"])
                more.update(renders=more["renders"] + tail["renders"], reached=tail["reached"])
            return {**more, "sections": [k for k in OPTIONAL_SECTIONS if ctx.get(k)], "linespread": ctx["linespread"]}

        if fill_on:
            fill = fill_up()
            pdf, pages = rerender()
        s = ats.score(pdf_text(pdf), kws)

        # Wording pass: a keyword the CV has only in another form ("prototyped" for "prototyping") gets its exact form in
        # the one bullet that carries its stem. Kept only if the CV still fits and no keyword was lost.
        wording: dict = {"terms": list(s.wording_misses), "asked": 0, "calls": 0, "accepted": [], "blocked": [],
                         "unplaced": [], "kept": None}
        if s.wording_misses:
            before = [list(e["bullets"]) for sec in ("experience", "projects") for e in ctx[sec]]
            wording.update(wording_pass(job, fb, ctx, kws, s.wording_misses, llm, cache, rephrase_notes))
            if hk:
                _keep_proof_number(ctx, fb, hk)
            if wording["accepted"]:
                pdf, pages = rerender()
                s2 = ats.score(pdf_text(pdf), kws)
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
                            s = ats.score(pdf_text(pdf), kws)
            if reverted and fill_on:                           # shorter wording may have left page 2 short again
                more = fill_up(skip=set(fill["rejected_overflow"]))
                fill = {"added": fill["added"] + more["added"],
                        "rejected_overflow": fill["rejected_overflow"] + more["rejected_overflow"],
                        "renders": fill["renders"] + more["renders"], "reached": more["reached"],
                        "sections": more["sections"], "linespread": more["linespread"]}
                pdf, pages = rerender()
                s = ats.score(pdf_text(pdf), kws)

        rec = {"attempt": attempt, "score": s.literal, "score_stemmed": s.stemmed, "band": in_band(s.literal, band),
               "pages": pages, "page2_fill": page2_fill(pdf, pages), "fill": fill, "shrinks": shrinks,
               "reverted": reverted, "wording": wording, "lengths": (fit_state.get("stats") or {}),
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
        fixable = {m for m in s.wording_misses + s.misses if m not in all_gaps}
        ranking = _keyword_boost(ranking, fixable)
        again = select_cv(fb_sel, ranking, preferred, skills=skills, layout=layout, jaccard_limit=jac, jd=jd)
        if (reconcile_proof(fb, again, hk, kws, jd)[0] if hk else settle_selection(fb, again)[0]) == sel:   # the same selection (proof bullet included) is not asked again
            break

    rec, ctx = best
    pdf = render_cv(ctx, out_dir, cv_name, one_page, sep, keep)     # re-render the chosen attempt as the final file
    final_score = ats.score(pdf_text(pdf), kws)
    cv_pages = page_count(pdf)
    fill_final = page2_fill(pdf, cv_pages)
    chosen_ids = {i for e in ctx["experience"] + ctx["projects"] for i in e["bullet_ids"]}
    evidence = letter_evidence(fb, kws, chosen_ids, job["description"])
    letter_pdf, letter, letters = _letters(job, fb, kws, hook, hk, evidence, llm, settings, out_dir, letter_name, team,
                                           required_gaps(gaps, kws), chosen_ids, claimable, possible, hook_failed)
    cv_text = pdf_text(pdf)
    fm = None if one_page else pdflayout.measure(pdf)
    urls = [] if one_page else [p["github"] for p in ctx["projects"] if p.get("github") and not project_parts(p)[1]]
    link_issues = pdflayout.link_problems(pdf, urls) if urls else []            # checked on the PDF's word boxes
    report["analysis_warnings"].extend(f"layout: project link {x}" for x in link_issues)
    ats_now = ats.scores(cv_text, kws, claimable, pdf_text(letter_pdf) if letter_pdf else None, (lo * 100, hi * 100))
    report.update({
        "ats": {**ats_now.to_dict(), "variants": ats_by_variant(cv_text, kws, claimable, letters, (lo * 100, hi * 100))},
        "stretch": ats_now.gate == "stretch",                                # a ceiling under the band is labelled, never dropped
        "letters": _publish_letters(letters, out_dir, final_dir), "letter_choice": letters["choice"] if letters else None,
        "claimable": claimable, "band_pct": [lo * 100, hi * 100],
        "final": {"score": final_score.literal, "score_stemmed": final_score.stemmed,
                  "band": in_band(final_score.literal, band), "pages": cv_pages, "page2_fill": fill_final,
                  "hits": final_score.hits, "wording_misses": final_score.wording_misses,
                  "misses": final_score.misses, "chosen_attempt": rec["attempt"]},
        "cv_pages": cv_pages, "page2_fill": fill_final, "fill": rec["fill"],
        "needs_result": needs_result(fb, ctx),
        "lengths": {**rec["lengths"], "final": ({"headline_lines": fm.headline_lines, "profile_lines": fm.profile_lines,
                                                   "longest_bullet": max(fm.bullets, default=0)} if fm else None),
                    "links": link_issues},
        "hook_placed": hook_placement(ctx, hk, pdf_text(pdf)),
        "typography": typography_report(ctx, pdf_text(pdf), sep, keep),
        "ranking": ranking_table(fb, kws, jd, chosen_ids),
        "rephrase_coverage": rec["rephrase_coverage"], "rephrase": rec["rephrase"], "placement": rec["placement"],
        "letter": letter, "llm_usage": usage_since(llm, usage_from),
        "cv_pdf": str(final_dir / pdf.name), "letter_pdf": _final_path(letter_pdf, out_dir, final_dir),
        "context": ctx,
    })
    if report["letters"]:
        report["letter"] = {**letter, "pdf": report["letter_pdf"], "tex": _final_path(letter.get("tex"), out_dir, final_dir)}
    note_mod.write(job, report, fb, out_dir)                       # note.md: hook, ATS, learn list, contact, question, flags, gap
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
