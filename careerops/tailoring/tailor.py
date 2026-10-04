"""Analyse → tailor → guard → render → score loop for one job. Writes a report.json the review step reads."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..apply.naming import file_stem
from ..discovery import gates
from . import prompts as P
from .budget import bullet_scores
from .faithfulness import check, deny_hits, numbers
from .grounding import grounding_problems
from .rawlog import RawRecorder
from .selection import MOST_RECENT_BULLETS, chunks, missing_keywords, select_cv, selected_ids
from .shapes import normalise_keywords, normalise_letter, normalise_profile, normalise_rephrase
from .textnorm import normalise_json
from .render import page_count, pdf_text, render_cv, render_letter
from .scorer import (Keyword, _contains, cap_keywords, ceiling, drop_contained, gap_terms, in_band, merge_keywords,
                     norm_literal, norm_stem, score, taxonomy_keywords)

USAGE_SUMS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")   # summed into report llm_usage
FALLBACK_MARK = "used fallback letter"
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


def clean_team(raw, job: dict) -> str:
    """The team name the model found, only if the JD really contains it (a model may not invent one)."""
    t = re.sub(r"\s+", " ", str(raw or "")).strip(" .,:;")
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


def analyse(job: dict, llm, settings, notes: list[str] | None = None) -> tuple[list[Keyword], str, str]:
    """(keywords, company_hook, team). `team` is "" when the JD names none. The model only suggests keywords; code
    enforces the rules: verbatim in the JD, short, nothing generic, near-duplicates merged (the shorter stays), at most
    scoring.max_keywords kept by weight then JD frequency. Shape problems in the model's answer and every drop are
    appended to `notes` when given."""
    sc = settings["scoring"]
    jd = job["description"] or ""
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
def propose_rephrases(job: dict, fb: dict, sel: dict, kws: list[Keyword], llm, cache: dict,
                      notes: list[str]) -> tuple[dict[str, str], dict]:
    """Ask for rewordings of the selected bullets that can carry a JD keyword their text lacks (missing_keywords), in
    batches of REPHRASE_BATCH: each call gets the bullet text, its terms and the keywords it may introduce, and answers
    {bullet id: new text}. Returns ({bullet id: proposed text}, stats). `cache` maps (bullet id, missing words) to the
    answer, so a later attempt never pays twice for the same question; a failed batch is not cached and is noted."""
    bidx = bullet_index(fb)
    todo = []
    for bid in selected_ids(fb, sel):
        miss = tuple(missing_keywords(bidx[bid], kws))
        if miss:
            todo.append((bid, miss))
    fresh = [t for t in todo if t not in cache]
    stats = {"eligible": len(todo), "asked": len(fresh), "cached": len(todo) - len(fresh), "calls": 0,
             "failed_calls": 0}
    system = P.REPHRASE_SYSTEM.replace("{deny}", _deny_list(fb))
    for batch in chunks(fresh, REPHRASE_BATCH):
        items = [{"id": bid, "text": bidx[bid]["text"], "terms": list(bidx[bid].get("terms", [])),
                  "introduce": list(miss)} for bid, miss in batch]
        lines = "\n".join(f"- id: {i['id']}\n  TEXT: {i['text']}\n  TERMS: {', '.join(i['terms']) or '(none)'}\n"
                          f"  MAY INTRODUCE: {', '.join(i['introduce'])}" for i in items)
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


def guard_rephrases(fb: dict, proposed: dict[str, str], kws: list[Keyword]) -> tuple[dict[str, str], list[str],
                                                                                      list[str]]:
    """Run every proposed rewording through faithfulness.check against its own bullet (text + terms). Returns
    (accepted {bullet id: text}, violations, warnings); a rejected rewording simply keeps the canonical bullet."""
    deny, bidx, vocab = fb.get("deny_terms", []), bullet_index(fb), fact_text(fb)
    accepted: dict[str, str] = {}
    violations: list[str] = []
    warnings: list[str] = []
    for bid, new in proposed.items():
        b = bidx[bid]
        c = check(new, b["text"], b.get("terms", []), deny, kws, vocabulary=vocab)
        warnings.extend(f"{bid}: {w}" for w in c.warnings)
        if c.ok:
            accepted[bid] = new.strip()
        else:
            violations.extend(f"{bid}: {v}" for v in c.violations)
    return accepted, violations, warnings


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
def _context(fb: dict, sel: dict, texts: dict[str, str], profile: str, kws: list[Keyword], headline: str = "") -> dict:
    """The render context for a selection: canonical bullet text unless the guard accepted a rewording (`texts`)."""
    bidx = bullet_index(fb)

    def text(bid: str) -> str:
        return texts.get(bid) or bidx[bid]["text"]

    experience = []
    for e in fb["experience"]:
        ids = sel["experience"].get(e["id"]) or [e["bullets"][0]["id"]]
        experience.append({**e, "bullet_ids": list(ids), "bullets": [text(i) for i in ids]})
    by_id = {p["id"]: p for p in fb["projects"]}
    projects = [{**by_id[p], "bullet_ids": [b["id"] for b in by_id[p]["bullets"]],
                 "bullets": [text(b["id"]) for b in by_id[p]["bullets"]]} for p in sel["projects"] if p in by_id]
    hit_terms = {norm_literal(w) for k in kws for w in k.members()}
    skills = {g: sorted(items, key=lambda s: norm_literal(s) not in hit_terms) for g, items in fb["skills"].items()}
    return {"id": {**fb["identity"], "headline": headline or fb["identity"].get("headline", "")}, "profile": profile,
            "skills": skills, "experience": experience, "projects": projects, "education": fb.get("education", []),
            "certifications": fb.get("certifications", [])}


def _shrink(ctx: dict, kws: list[Keyword]) -> bool:
    """Drop the least keyword-relevant bullet. An entry keeps its last bullet, and the most recent experience entry
    keeps two until nothing else can go. False if nothing is left to drop."""
    terms = [[norm_literal(w) for w in k.members()] for k in kws]
    first = ctx["experience"][0] if ctx["experience"] else None
    for strict in (True, False):
        worst = None
        for sec in ("projects", "experience"):
            for e in ctx[sec]:
                if len(e["bullets"]) <= (MOST_RECENT_BULLETS[0] if strict and e is first else 1):
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
    team = team.strip()
    if team:
        return cfg["greeting"].format(company=company, team=f" {team}")
    return cfg["greeting_fallback"].format(company=company)


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


VAGUE_WORDS = ("scalable", "robust", "seamless", "cutting-edge")      # filler: only if the evidence itself says it
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
    most, then the best of the rest from a different entry (two lines about one job read as one example). Canonical
    fact-bank text, as [{"id", "text", "terms"}]; fewer than two when the CV has fewer bullets."""
    sc = bullet_scores(fb, kws)
    bidx = bullet_index(fb)
    owner = {b["id"]: e["id"] for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"]}
    order = {b: i for i, b in enumerate(sc)}
    pool = [b for b in sc if not chosen_ids or b in chosen_ids]
    ranked = sorted(pool, key=lambda b: (-sc[b], order[b]))
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
        if _contains(norm_literal(text), w) and not _contains(ev_all, w):
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


def _letter(job, fb, kws, hook, evidence: list[dict], llm, settings, gaps: list[str] | None = None,
            notes: list[str] | None = None) -> tuple[list[str], list[str]]:
    """The cover letter from the company hook, the two evidence bullets and the optional GAPS. A draft is checked (length,
    banned phrases, deny list, claimed gaps, role upgrades, numbers, and every sentence against its own evidence bullet
    and the JD) and retried once with its problems listed; a second failure is the deterministic fallback, flagged."""
    deny = fb.get("deny_terms", [])
    gaps = (gaps or [])[:6]
    # a deny-listed tool that is a gap is nameable, so the prompt must not also forbid it
    named_ok = {d for g in gaps for d in deny_hits(g, deny)}
    max_words = settings["scoring"]["letter_max_words"]
    texts = [e["text"] for e in evidence]
    source = " ".join(texts) + " " + hook + " " + job["description"]
    named = f"{job['company']} {job['title']}"
    problems: list[str] = []
    feedback = ""
    for _ in range(2):
        try:
            out = _llm_json(
                llm, "cover_letter",
                P.LETTER_SYSTEM.replace("{max_words}", str(max_words))
                .replace("{deny}", ", ".join(d for d in deny if d not in named_ok)),
                P.LETTER_USER.format(title=job["title"], company=job["company"], company_hook=hook or "(none)",
                                     gaps=", ".join(gaps) or "(none)",
                                     evidence="\n".join(f"- {e}" for e in texts)) + feedback,
                {"company": job["company"], "title": job["title"], "company_hook": hook, "evidence": texts,
                 "gaps": gaps})
            paras, letter_notes = normalise_letter(out)
            if notes is not None:
                notes.extend(m for m in (f"cover_letter: {n}" for n in letter_notes) if m not in notes)
        except Exception as e:  # noqa: BLE001
            problems.append(f"letter LLM failed: {e}")
            break
        text = " ".join(paras)
        problems = []
        if not text.strip():
            problems.append("empty letter")
        if len(text.split()) > max_words:
            problems.append(f"{len(text.split())} words > {max_words}")
        if paras and BANNED_OPENERS.search(paras[0]):
            problems.append("banned opener")
        if BANNED_CLOSERS.search(text):
            problems.append("banned closer")
        problems += _deny_problems(text, deny, gaps)
        problems += _lack_problems(text, gaps)
        problems += _style_problems(paras, texts)
        problems += grounding_problems(paras, evidence, job["description"], hook, named, gaps)
        extra = numbers(text) - numbers(source)
        if extra:
            problems.append(f"new numbers {sorted(extra)}")
        if not problems:
            return paras, []
        feedback = "\n\nYOUR LAST DRAFT BROKE THESE RULES; fix every one:\n" + "\n".join(f"- {p}" for p in problems)
    # deterministic fallback — always honest, never great; review will show why
    fallback = [
        f"{job['company']}'s {job['title']} role sits close to the work I have been doing most recently.",
        " ".join(texts[:2]),
        f"Could we find twenty minutes to talk about where I could contribute first on the {job['title']} team?",
    ]
    return [p for p in fallback if p.strip()], problems + [FALLBACK_MARK]


def _make_letter(job, fb, kws, hook, evidence: list[dict], llm, settings, out_dir: Path, final_name: str,
                 team: str = "", gaps: list[str] | None = None):
    """Write the cover letter PDF. Always produces one: a failed or rule-breaking LLM draft becomes the
    deterministic fallback, and report["letter"]["status"] says which of the two the reader is looking at."""
    notes: list[str] = []
    paras, problems = _letter(job, fb, kws, hook, evidence, llm, settings, gaps, notes)
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paras,
                         "date": date.today().strftime("%d %B %Y"),
                         "greeting": letter_greeting(settings, job["company"], team),
                         "signoff": settings["letter"]["signoff"]}, out_dir, final_name)
    return pdf, {"paragraphs": paras, "problems": problems, "score": score(pdf_text(pdf), kws).literal,
                 "pages": page_count(pdf), "status": "fallback" if FALLBACK_MARK in problems else "ok",
                 "notes": notes, "evidence": [{"id": e["id"], "text": e["text"]} for e in evidence]}


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
    pdf, letter = _make_letter(job, fb, kws, rep.get("company_hook") or "", letter_evidence(fb, kws, ids), llm,
                               settings, out_dir, Path(rep["letter_pdf"]).stem, rep.get("team") or "",
                               required_gaps(rep.get("genuine_gaps") or [], kws))
    rep["letter"], rep["letter_pdf"] = letter, str(pdf)
    new, old = usage_since(llm, usage_from), rep.get("llm_usage") or {}      # the re-draft adds to the job's total
    calls = (old.get("calls") or []) + new["calls"]
    rep["llm_usage"] = {"calls": calls, **{k: (old.get(k) or 0) + new[k] for k in USAGE_SUMS},
                        "by_task": usage_by_task(calls)}
    (out_dir / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return rep


# ------------------------------------------------------------------ 4. the loop
def tailor_job(job: dict, fb: dict, llm, settings, out_root: Path) -> dict[str, Any]:
    """Tailor one job into out_root/<company_title_id>/. Everything is built in a '.partial' sibling folder and swapped
    in only once the whole job has succeeded, so a run that fails part-way (LLM down, xelatex error) leaves an
    earlier successful output folder exactly as it was."""
    final = out_root / slug(f"{job['company']}_{job['title']}_{job['id'].split(':')[-1]}")
    work = final.with_name(final.name + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    try:
        report = _tailor(job, fb, llm, settings, work, final)
        old = final.with_name(final.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if final.exists():
            os.replace(final, old)
        os.replace(work, final)
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(final.with_name(final.name + ".failed"), ignore_errors=True)    # superseded by this success
        return report
    except BaseException as exc:
        # The staging folder goes, the earlier successful folder stays. But the model's raw answers are what you need
        # to see why a job failed, so they are kept in <job>.failed/llm_raw and the error says where.
        raw = work / "llm_raw"
        if raw.is_dir():
            failed = final.with_name(final.name + ".failed")
            shutil.rmtree(failed, ignore_errors=True)
            shutil.copytree(raw, failed / "llm_raw")
            if isinstance(exc, Exception) and exc.args and isinstance(exc.args[0], str):
                exc.args = (f"{exc.args[0]} [raw model answers: {failed / 'llm_raw'}]", *exc.args[1:])
        shutil.rmtree(work, ignore_errors=True)
        raise


def _tailor(job: dict, fb: dict, llm, settings, out_dir: Path, final_dir: Path) -> dict[str, Any]:
    """The loop proper. Writes into out_dir (the staging folder); the report names final_dir's paths.
    Code selects the bullets, the model rewords them (batches) and writes the profile, the guard checks both."""
    sc = settings["scoring"]
    band = tuple(sc["band"])
    lo, hi = band
    llm = RawRecorder(llm, out_dir / "llm_raw")                 # every raw model reply is kept for inspection
    usage_from = len(getattr(llm, "usage", []))
    family, tier, headline, preferred = role_prior(job, fb, settings)
    analysis_warnings: list[str] = []
    kws, hook, team = analyse(job, llm, settings, analysis_warnings)
    ceil, gaps = ceiling(fact_text(fb), kws)
    cv_name, letter_name = _names(fb, settings)
    profile = make_profile(job, fb, kws, headline, llm)         # depends on the facts only, not on the selection
    report: dict[str, Any] = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                              "family": family, "tier": tier, "headline": headline,
                              "keywords": [asdict(k) for k in kws], "company_hook": hook, "team": team,
                              "ceiling": ceil, "genuine_gaps": gaps, "attempts": [], "out_dir": str(final_dir),
                              "analysis_warnings": analysis_warnings,
                              "profile": {k: profile[k] for k in ("facts", "retried", "fallback")}}

    ranking, best, cache = kws, None, {}
    for attempt in range(1, sc["max_attempts"] + 1):
        llm.attempt = attempt                                    # names this attempt's raw replies (tailor_profile-2.json)
        sel = select_cv(fb, ranking, preferred)
        rephrase_notes: list[str] = []
        proposed, rstats = propose_rephrases(job, fb, sel, kws, llm, cache, rephrase_notes)
        accepted, viol, warn = guard_rephrases(fb, proposed, kws)
        rstats.update(proposed=len(proposed), accepted=len(accepted), blocked=len(proposed) - len(accepted))
        ctx = _context(fb, sel, accepted, profile["text"], kws, headline)
        pdf = render_cv(ctx, out_dir, cv_name)
        shrinks = 0
        while page_count(pdf) > sc["max_pages"] and _shrink(ctx, kws):
            pdf = render_cv(ctx, out_dir, cv_name)
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
                            pdf = render_cv(ctx, out_dir, cv_name)
                            s = score(pdf_text(pdf), kws)

        rec = {"attempt": attempt, "score": s.literal, "score_stemmed": s.stemmed, "band": in_band(s.literal, band),
               "pages": page_count(pdf), "shrinks": shrinks, "reverted": reverted,
               "violations": profile["violations"] + viol, "warnings": profile["warnings"] + rephrase_notes + warn,
               "wording_misses": s.wording_misses, "misses": s.misses, "rephrase": rstats,
               "proposal": {"selection": sel, "rephrase": proposed, "profile": profile["text"]},
               "selection": {e["id"]: e["bullet_ids"] for e in ctx["experience"] + ctx["projects"]},
               "profile": ctx["profile"], "rephrase_coverage": rephrase_coverage(ctx, fb, kws)}
        report["attempts"].append(rec)
        if best is None or abs(s.literal - (lo + hi) / 2) < abs(best[0]["score"] - (lo + hi) / 2):
            best = (rec, ctx)
        if rec["band"] != "below" or ceil < lo:
            break   # in band, or (above: rephrasings already reverted) nothing left to try, or no honest way up
        # Below the band: count the keywords the fact bank evidences but the CV missed double when ranking, so the next
        # attempt picks the bullets that carry them. Selection is deterministic; an unchanged one would be wasted quota.
        fixable = {m for m in s.wording_misses + s.misses if m not in gaps}
        ranking = _keyword_boost(ranking, fixable)
        if select_cv(fb, ranking, preferred) == sel:
            break

    rec, ctx = best
    pdf = render_cv(ctx, out_dir, cv_name)                      # re-render the chosen attempt as the final file
    final_score = score(pdf_text(pdf), kws)
    chosen_ids = {i for e in ctx["experience"] + ctx["projects"] for i in e["bullet_ids"]}
    letter_pdf, letter = _make_letter(job, fb, kws, hook, letter_evidence(fb, kws, chosen_ids), llm, settings, out_dir,
                                      letter_name, team, required_gaps(gaps, kws))
    report.update({
        "final": {"score": final_score.literal, "score_stemmed": final_score.stemmed,
                  "band": in_band(final_score.literal, band), "pages": page_count(pdf),
                  "hits": final_score.hits, "wording_misses": final_score.wording_misses,
                  "misses": final_score.misses, "chosen_attempt": rec["attempt"]},
        "rephrase_coverage": rec["rephrase_coverage"], "rephrase": rec["rephrase"],
        "letter": letter, "llm_usage": usage_since(llm, usage_from),
        "cv_pdf": str(final_dir / pdf.name), "letter_pdf": str(final_dir / letter_pdf.name), "context": ctx,
    })
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
