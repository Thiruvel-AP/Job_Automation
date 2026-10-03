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
from ..llm.pacing import estimate_tokens
from .budget import bullet_scores, call_limits, fit, rank_projects
from .faithfulness import check, deny_hits, numbers
from .textnorm import normalise_json
from .render import page_count, pdf_text, render_cv, render_letter
from .scorer import (Keyword, _contains, ceiling, gap_terms, in_band, merge_keywords, norm_literal, norm_stem, score,
                     taxonomy_keywords)

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


# words that name nothing a recruiter's ATS would search for, and degree requirements: never keywords
GENERIC_WORDS = frozenset({"tasks", "task", "workflows", "workflow", "patterns", "pattern", "skills", "skill", "data",
                           "techniques", "technique", "experience", "ability", "knowledge", "understanding"})
DEGREE = re.compile(r"\b(degree|bachelor'?s?|master'?s?|msc|bsc|ph\.?d|doctorate|diploma|undergraduate|postgraduate)\b",
                    re.I)
MAX_KEYWORD_WORDS, MAX_LLM_KEYWORDS = 3, 25
_ALTERNATIVES = re.compile(r"\s+or\s+|\s*,\s*(?:or\s+)?|\s+/\s+", re.I)


def llm_keyword(raw, njd: str, weight: float, required: bool) -> Keyword | None:
    """One keyword from the model's output. "A or B or C" is one any-of keyword; every alternative must appear
    verbatim in the JD (`njd`, norm_literal'd), be 1-3 words, not be generic ("data", "skills") and not be a
    degree requirement. None if nothing usable is left."""
    t = str(raw).strip()
    parts = [p.strip(" .;:()\"'") for p in (_ALTERNATIVES.split(t) if re.search(r"\bor\b|,|\s/\s", t, re.I) else [t])]
    ok = []
    for p in parts:
        words = p.split()
        if (1 < len(p) <= 40 and 1 <= len(words) <= MAX_KEYWORD_WORDS and norm_literal(p) in njd
                and not all(w.lower() in GENERIC_WORDS for w in words) and not DEGREE.search(p)):
            ok.append(p)
    return Keyword(ok[0], weight, required, ok[1:]) if ok else None


def analyse(job: dict, llm, settings) -> tuple[list[Keyword], str, str]:
    """(keywords, company_hook, team). `team` is "" when the JD names none."""
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
        for req, key in ((True, "required"), (False, "preferred")):
            for t in out.get(key, []) or []:
                k = llm_keyword(t, njd, sc["required_weight"] if req else sc["preferred_weight"], req)
                if k:                                      # verbatim, short, specific: nothing invented survives
                    llm_kw.append(k)
        llm_kw = llm_kw[:MAX_LLM_KEYWORDS]
        hook = str(out.get("company_hook", ""))[:300]
        team = clean_team(out.get("team"), job)
    except Exception as e:  # noqa: BLE001 — taxonomy keywords alone are a usable fallback
        hook = team = ""
        llm_kw = []
        print(f"  keyword LLM failed, using taxonomy only: {e}")
    kws = merge_keywords(tax, llm_kw)[:70]
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


def _tailor_prompt(job, fb: dict, kws: list[Keyword], headline: str, preferred: list[str] | None,
                   feedback: str) -> tuple[str, str]:
    """(system, user) for tailor_cv. `fb` may be a reduced view of the fact bank (see budget.py)."""
    exp_txt = "\n".join(
        f"{e['id']}: {e['title']} @ {e['org']}\n" +
        "\n".join(f"  {b['id']}: {b['text']} [terms: {', '.join(b.get('terms', []))}]" for b in e["bullets"])
        for e in fb["experience"])
    by_id = {p["id"]: p for p in fb["projects"]}
    listed = [by_id[i] for i in rank_projects(fb, kws, preferred)]    # most relevant to the JD first
    prj_txt = "\n".join(
        f"{p['id']}: {p['name']} ({p.get('stack', '')})\n" +
        "\n".join(f"  {b['id']}: {b['text']} [terms: {', '.join(b.get('terms', []))}]" for b in p["bullets"])
        for p in listed)
    preferred = [p for p in preferred or [] if p in by_id]          # only ids the model can actually see
    user = P.TAILOR_USER.format(
        title=job["title"], company=job["company"],
        headline=headline or fb["identity"].get("headline", ""),
        preferred=(f"Preferred projects for a {headline} role (a tie-breaker only: pick one over a project that is "
                   f"equally relevant to the JD, never over a more relevant one): {', '.join(preferred)}"
                   if preferred else ""),
        keywords="\n".join(f"- {k.term} ({k.weight:g})" for k in kws),
        feedback=feedback, profile_facts="\n".join(f"- {f}" for f in fb["profile_facts"]),
        skills="; ".join(f"{g}: {', '.join(i)}" for g, i in fb["skills"].items()),
        experience=exp_txt, projects=prj_txt)
    return P.TAILOR_SYSTEM.replace("{deny}", ", ".join(fb.get("deny_terms", []))), user


def _proposal(job, fb, kws, llm, feedback: str, settings, headline: str = "",
              preferred: list[str] | None = None) -> tuple[dict, dict]:
    """(model proposal, prompt meta). Only the bullets most relevant to the JD are sent, so that estimated input +
    max_tokens stays within llm.max_call_tokens (budget.py); the CV is still assembled from the whole fact bank."""
    max_tokens, limit = call_limits(settings, "tailor_cv")
    sent, sent_kws, meta = fit(fb, kws, lambda v, k: _tailor_prompt(job, v, k, headline, preferred, feedback),
                               max_tokens, limit, int(settings["llm"].get("top_bullets", 14)), preferred)
    system, user = _tailor_prompt(job, sent, sent_kws, headline, preferred, feedback)
    ctx = {"keywords": [asdict(k) for k in sent_kws], "experience": sent["experience"], "projects": sent["projects"],
           "profile_facts": fb["profile_facts"], "preferred_projects": preferred or []}
    return _llm_json(llm, "tailor_cv", system, user, ctx), meta


def _profile_retrier(llm, fb: dict, headline: str) -> Callable[[str, list[str], str], str]:
    """The one retry a profile that fails the guard gets: the model sees what was wrong and the facts it may use."""
    deny = ", ".join(fb.get("deny_terms", []))

    def retry(previous: str, problems: list[str], source: str) -> str:
        out = _llm_json(llm, "tailor_profile", P.PROFILE_RETRY_SYSTEM.replace("{deny}", deny),
                        P.PROFILE_RETRY_USER.format(headline=headline or fb["identity"].get("headline", ""),
                                                    previous=previous or "(none)", problems="; ".join(problems),
                                                    source=source),
                        {"profile_facts": fb["profile_facts"], "previous": previous, "problems": problems,
                         "headline": headline})
        return str(out.get("profile") or "")

    return retry


def rephrase_coverage(ctx: dict, fb: dict, kws: list[Keyword]) -> dict:
    """Of the selected bullets that could carry a JD keyword's wording (the bullet's terms evidence it, its text does
    not say it), how many were reworded? {"eligible", "rephrased", "ratio"}; ratio is None with nothing eligible."""
    bidx = bullet_index(fb)
    eligible = rephrased = 0
    for sec in ("experience", "projects"):
        for e in ctx[sec]:
            for bid, text in zip(e["bullet_ids"], e["bullets"]):
                b = bidx[bid]
                lit = norm_literal(b["text"])
                stems = [norm_stem(t) for t in b.get("terms", [])]
                if any(any(_contains(s, norm_stem(w)) for s in stems) and not _contains(lit, norm_literal(w))
                       for k in kws for w in k.members()):
                    eligible += 1
                    rephrased += text != b["text"]
    return {"eligible": eligible, "rephrased": rephrased, "ratio": rephrased / eligible if eligible else None}


def _assemble(fb: dict, prop: dict, kws: list[Keyword], headline: str = "",
              retry_profile: Callable[[str, list[str], str], str] | None = None) -> tuple[dict, list[str], list[str]]:
    """Turn a proposal into a render context, applying the faithfulness guard.
    Returns (ctx, violations, warnings). Violating rephrases fall back to canonical text. A profile that fails the
    guard is rewritten once by `retry_profile(previous, problems, source)` before profile_facts are used instead."""
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
    pids = [p for p in (prop.get("projects") or []) if p in proj_by_id][:2] or [p["id"] for p in fb["projects"][:2]]
    projects = [{**proj_by_id[p], "bullet_ids": [b["id"] for b in proj_by_id[p]["bullets"]],
                 "bullets": [text_for(b["id"]) for b in proj_by_id[p]["bullets"]]} for p in pids]

    profile = (prop.get("profile") or "").strip()
    # what the profile may draw on: profile_facts + skills + the canonical bullets selected for this CV
    picked = [bidx[i] for sec in (experience, projects) for e in sec for i in e["bullet_ids"]]
    src = (" ".join(fb["profile_facts"]) + " " + " ".join(i for v in fb["skills"].values() for i in v) + " "
           + " ".join(b["text"] for b in picked))
    terms = [t for b in picked for t in b.get("terms", [])]
    def profile_problems(text: str) -> list[str]:
        if not text:
            return ["no profile"]
        probs = check(text, src, terms, deny, kws, vocabulary=vocab, max_growth=3.0).violations
        if re.search(r"\b\d+\+?\s+years?\b", text, re.I):
            probs = probs + ["years-of-experience claim"]
        return probs

    problems = profile_problems(profile)
    if problems:
        violations.extend(f"profile: {p}" for p in problems if p != "no profile")
        retried, again = "", []
        if retry_profile:
            try:
                retried = retry_profile(profile, problems, src).strip()
            except Exception as e:  # noqa: BLE001 — a failed retry just means the fallback
                warnings.append(f"profile: retry failed ({e})")
            again = profile_problems(retried) if retried else []
            violations.extend(f"profile (retry): {p}" for p in again)
        if retried and not again:
            profile = retried
            warnings.append("profile: rewritten once after the guard rejected the first draft")
        else:
            profile = ". ".join(fb["profile_facts"][:3]) + "."
            warnings.append("profile: fell back to profile_facts")

    hit_terms = {norm_literal(w) for k in kws for w in k.members()}
    skills = {g: sorted(items, key=lambda s: norm_literal(s) not in hit_terms) for g, items in fb["skills"].items()}
    ctx = {"id": {**fb["identity"], "headline": headline or fb["identity"].get("headline", "")}, "profile": profile, "skills": skills, "experience": experience,
           "projects": projects, "education": fb.get("education", []),
           "certifications": fb.get("certifications", [])}
    return ctx, violations, warnings


def _shrink(ctx: dict, kws: list[Keyword]) -> bool:
    """Drop the least keyword-relevant bullet (never an entry's last one). False if nothing left to drop."""
    terms = [[norm_literal(w) for w in k.members()] for k in kws]
    worst = None
    for sec in ("projects", "experience"):
        for e in ctx[sec]:
            if len(e["bullets"]) <= 1:
                continue
            for i, b in enumerate(e["bullets"]):
                s = sum(any(t in norm_literal(b) for t in group) for group in terms)
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


def letter_evidence(fb: dict, kws: list[Keyword], chosen: list[str], chosen_ids: set[str] | None = None,
                    extra: int = 3) -> list[str]:
    """The bullets the CV selected plus the `extra` fact-bank bullets most relevant to the JD that it did not."""
    sc = bullet_scores(fb, kws)
    order = {b: i for i, b in enumerate(sc)}
    bidx = bullet_index(fb)
    taken = set(chosen_ids or ())
    more = [bidx[b]["text"] for b in sorted(sc, key=lambda b: (-sc[b], order[b]))
            if b not in taken and bidx[b]["text"] not in chosen][:extra]
    return list(chosen) + more


def _style_problems(paras: list[str], evidence: list[str]) -> list[str]:
    """Checks that keep the letter faithful in tone: it ends on a question, never promotes the candidate's role,
    keeps approximations approximate and uses no filler word the evidence does not use."""
    out = []
    text = " ".join(paras)
    if paras and "?" not in paras[-1]:
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


def _letter(job, fb, kws, hook, chosen_bullets: list[str], llm, settings,
            gaps: list[str] | None = None, chosen_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    deny = fb.get("deny_terms", [])
    gaps = (gaps or [])[:6]
    # a deny-listed tool that is a gap is nameable, so the prompt must not also forbid it
    named_ok = {d for g in gaps for d in deny_hits(g, deny)}
    max_words = settings["scoring"]["letter_max_words"]
    evidence = letter_evidence(fb, kws, chosen_bullets, chosen_ids)
    source = " ".join(evidence + fb["profile_facts"]) + " " + hook + " " + job["description"]
    problems: list[str] = []
    feedback = ""
    for _ in range(2):
        try:
            out = _llm_json(
                llm, "cover_letter",
                P.LETTER_SYSTEM.replace("{max_words}", str(max_words))
                .replace("{deny}", ", ".join(d for d in deny if d not in named_ok)),
                P.LETTER_USER.format(title=job["title"], company=job["company"], company_hook=hook or "(none)",
                                     keywords=", ".join(k.term for k in kws[:20]),
                                     gaps=", ".join(gaps) or "(none)",
                                     evidence="\n".join(f"- {e}" for e in evidence)) + feedback,
                {"company": job["company"], "title": job["title"], "evidence": evidence, "gaps": gaps})
            paras = [p.strip() for p in out.get("paragraphs", []) if p and p.strip()]
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
        problems += _style_problems(paras, evidence)
        extra = numbers(text) - numbers(source)
        if extra:
            problems.append(f"new numbers {sorted(extra)}")
        if not problems:
            return paras, []
        feedback = "\n\nYOUR LAST DRAFT BROKE THESE RULES; fix every one:\n" + "\n".join(f"- {p}" for p in problems)
    # deterministic fallback — always honest, never great; review will show why
    fallback = [
        f"{job['company']}'s {job['title']} role sits close to the work I have been doing most recently.",
        " ".join(evidence[:2]),
        f"Could we find twenty minutes to talk about where I could contribute first on the {job['title']} team?",
    ]
    return [p for p in fallback if p.strip()], problems + [FALLBACK_MARK]


def _make_letter(job, fb, kws, hook, chosen: list[str], llm, settings, out_dir: Path, final_name: str,
                 team: str = "", gaps: list[str] | None = None, chosen_ids: set[str] | None = None):
    """Write the cover letter PDF. Always produces one: a failed or rule-breaking LLM draft becomes the
    deterministic fallback, and report["letter"]["status"] says which of the two the reader is looking at."""
    paras, problems = _letter(job, fb, kws, hook, chosen, llm, settings, gaps, chosen_ids)
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paras,
                         "date": date.today().strftime("%d %B %Y"),
                         "greeting": letter_greeting(settings, job["company"], team),
                         "signoff": settings["letter"]["signoff"]}, out_dir, final_name)
    return pdf, {"paragraphs": paras, "problems": problems, "score": score(pdf_text(pdf), kws).literal,
                 "pages": page_count(pdf), "status": "fallback" if FALLBACK_MARK in problems else "ok"}


def _names(fb: dict, settings) -> tuple[str, str]:
    """Final file stems for the CV and the letter: '<name> CV', '<name> Cover Letter' (files.name_style)."""
    style = settings["files"]["name_style"]
    return (file_stem(fb["identity"]["name"], "CV", style), file_stem(fb["identity"]["name"], "Cover Letter", style))


def usage_since(llm, start: int) -> dict:
    """Token usage of the LLM calls made since `start` (an index into llm.usage): the calls and their sums.
    Empty for a model that records none (MockLLM)."""
    calls = list(getattr(llm, "usage", [])[start:])
    total = {k: sum(c.get(k) or 0 for c in calls) for k in USAGE_SUMS}
    return {"calls": calls, **total}


def regenerate_letter(job, fb: dict, llm, settings, rep: dict) -> dict:
    """Re-draft the letter for an already tailored job (review's 'l'). Keeps the file name; rewrites report.json."""
    job = dict(job)
    job["description"] = job.get("description") or ""
    kws = [Keyword(**k) for k in rep["keywords"]]
    chosen = [b for sec in ("experience", "projects") for e in rep["context"][sec] for b in e["bullets"]]
    out_dir = Path(rep["out_dir"])
    usage_from = len(getattr(llm, "usage", []))
    ids = {i for sec in ("experience", "projects") for e in rep["context"][sec] for i in e.get("bullet_ids", [])}
    pdf, letter = _make_letter(job, fb, kws, rep.get("company_hook") or "", chosen, llm, settings, out_dir,
                               Path(rep["letter_pdf"]).stem, rep.get("team") or "",
                               required_gaps(rep.get("genuine_gaps") or [], kws), ids)
    rep["letter"], rep["letter_pdf"] = letter, str(pdf)
    new, old = usage_since(llm, usage_from), rep.get("llm_usage") or {}      # the re-draft adds to the job's total
    rep["llm_usage"] = {"calls": (old.get("calls") or []) + new["calls"],
                        **{k: (old.get(k) or 0) + new[k] for k in USAGE_SUMS}}
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
        return report
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _tailor(job: dict, fb: dict, llm, settings, out_dir: Path, final_dir: Path) -> dict[str, Any]:
    """The loop proper. Writes into out_dir (the staging folder); the report names final_dir's paths."""
    sc = settings["scoring"]
    band = tuple(sc["band"])
    lo, hi = band
    usage_from = len(getattr(llm, "usage", []))
    family, tier, headline, preferred = role_prior(job, fb, settings)
    kws, hook, team = analyse(job, llm, settings)
    ceil, gaps = ceiling(fact_text(fb), kws)
    cv_name, letter_name = _names(fb, settings)
    report: dict[str, Any] = {"job_id": job["id"], "company": job["company"], "title": job["title"],
                              "family": family, "tier": tier, "headline": headline,
                              "keywords": [asdict(k) for k in kws], "company_hook": hook, "team": team,
                              "ceiling": ceil, "genuine_gaps": gaps, "attempts": [], "out_dir": str(final_dir)}

    feedback, best = "", None
    for attempt in range(1, sc["max_attempts"] + 1):
        prop, prompt = _proposal(job, fb, kws, llm, feedback, settings, headline, preferred)
        ctx, viol, warn = _assemble(fb, prop, kws, headline, _profile_retrier(llm, fb, headline))
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
               "pages": page_count(pdf), "shrinks": shrinks, "reverted": reverted, "violations": viol,
               "warnings": warn, "wording_misses": s.wording_misses, "misses": s.misses,
               "prompt": prompt, "proposal": prop, "selection": {e["id"]: e["bullet_ids"] for e in ctx["experience"] + ctx["projects"]},
               "profile": ctx["profile"], "rephrase_coverage": rephrase_coverage(ctx, fb, kws)}
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
    pdf = render_cv(ctx, out_dir, cv_name)                      # re-render the chosen attempt as the final file
    final_score = score(pdf_text(pdf), kws)
    chosen = [b for e in ctx["experience"] + ctx["projects"] for b in e["bullets"]]
    chosen_ids = {i for e in ctx["experience"] + ctx["projects"] for i in e["bullet_ids"]}
    letter_pdf, letter = _make_letter(job, fb, kws, hook, chosen, llm, settings, out_dir, letter_name, team,
                                      required_gaps(gaps, kws), chosen_ids)
    report.update({
        "final": {"score": final_score.literal, "score_stemmed": final_score.stemmed,
                  "band": in_band(final_score.literal, band), "pages": page_count(pdf),
                  "hits": final_score.hits, "wording_misses": final_score.wording_misses,
                  "misses": final_score.misses, "chosen_attempt": rec["attempt"]},
        "rephrase_coverage": rec["rephrase_coverage"],
        "letter": letter, "llm_usage": usage_since(llm, usage_from),
        "cv_pdf": str(final_dir / pdf.name), "letter_pdf": str(final_dir / letter_pdf.name), "context": ctx,
    })
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
