"""The cover letter of `letter.mode: company`: five paragraphs, 360-420 words, the middle three written by the cover_letter model under two
guards, the first and the last written in code.

  P1  the hook's letter line, word for word; with no hook the role family's profile line (fact bank `families[*].profile_body`), word for word
  P2  why this company: what it does, its product or customers, what this team is hiring to solve. The JOB AD is the only source.
  P3  the best-matching fact-bank bullet, tied to a named requirement of the ad        (written from that bullet alone)
  P4  the second best-matching bullet, tied to another named requirement               (written from that bullet alone)
  P5  `letter.closing_hook` with the problem phrase of the ad (the hook's, else the ad's heaviest keywords the bank can claim)

The model is asked once and, when a guard rejects the draft, once more with every rejection listed; a second rejection is NO LETTER and the
review screen says why (status "none", `reason`, `retry.failures`). Nothing falls back to a template.

User guard (P3, P4: what the letter says about the user). Every number must be in the two chosen bullets (or the opening); every fact-bank
tool and organisation named must be one of theirs; every sentence is checked against its own bullet (`grounding_problems` with no job ad
to lean on: a name, sector, client or capability word the bullet does not say is rejected); each paragraph names an ad requirement its bullet
evidences; no pasted bullet, no deny-listed term, no keyword list, no "we" / "our", no "I help <Company>".

Company guard (P2: what the letter says about the employer). The ad is split into sentences. Every clause of P2 that makes a claim (its
content stems, after the company and role names and the generic verbs are set aside) must have at least COMPANY_SUPPORT of its stems in one
window of up to WINDOW consecutive ad sentences (a clause of fewer than MIN_CLAIM_STEMS stems: all of them somewhere in the ad). Every number
must be in the ad. Every capitalised name (a person, a team, a product) must be in the ad, the fact bank or the opening.

Length: the whole letter, P1 to P5, must be between MIN_WORDS (360) and `scoring.letter_max_words_company` (420) words; `letter.company_words`
(360-420, default 400) is the length the model is asked for. The apply assist uploads the PDF to the cover-letter file field, or pastes the text
into a cover-letter text box (apply/assist.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import ats
from ..hook import Hook, org_short
from ..llm.router import TaskUnavailable
from . import evidence_fields
from . import letters as L
from . import prompts as P
from .faithfulness import numbers
from .grounding import _has, _names, grounding_problems, repeated_evidence_problems, sentences
from .letter_rules import bank_names, term_dump_problems
from .scorer import Keyword, keyword_hits, norm_literal, norm_stem

MIN_WORDS, MAX_WORDS = 360, 420           # the allowed range of letter.company_words, and of the whole letter
DEFAULT_WORDS = 400
ATTEMPTS = 2                              # the draft and one redraft
COMPANY_SUPPORT = 0.6                     # share of a P2 clause's content stems that one window of the ad must hold
WINDOW = 3                                # consecutive ad sentences that may together support one clause
MIN_CLAIM_STEMS = 3                       # a clause with fewer content stems must have all of them somewhere in the ad
MODE = "company"

# words that carry no claim: function words, and the generic verbs and nouns of any "what we do" sentence
_FUNCTION = frozenset("""the and for with that this from have has had are was were will would can could may might shall should their they them its our
    your you who whom what which when where while into onto over under about above below out off than then such each both been being not but nor
    also only very more most less much many some any all one two its via per across within without between among through during after before until
    here there those these other another own same just still even ever like well too yet now new""".split())
_GENERIC = frozenset("""company companies team teams role roles hiring hire hires hired looking look seeking seek position job join joining work works
    working help helps helping need needs needed solve solves solving problem problems challenge challenges opportunity make makes made use uses used
    using provide provides providing offer offers offering build builds building create creates creating deliver delivers delivering enable enables
    support supports include includes including allow allows get gets give gives take takes bring brings drive drives driving ensure ensures
    focus focuses focused part""".split())
_SKIP = _FUNCTION | _GENERIC
_CLAUSE = re.compile(r",(?!\d)|[;:]|\s+(?:and|but|while|so|because|which|who|whose|whereas)\s+|\s[-–—]\s")
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s*")


# ------------------------------------------------------------------ settings
def target_words(settings) -> int:
    """settings letter.company_words: the length the model is asked for, 360-420 (default 400)."""
    raw = settings["letter"].get("company_words", DEFAULT_WORDS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = -1
    if isinstance(raw, bool) or not MIN_WORDS <= n <= MAX_WORDS:
        raise ValueError(f"letter.company_words must be a whole number between {MIN_WORDS} and {MAX_WORDS}, got {raw!r}")
    return n


def word_cap(settings) -> int:
    """settings scoring.letter_max_words_company (default 420, never over 420): the most words a company-mode letter may have. The other
    modes keep scoring.letter_max_words."""
    return min(int(settings["scoring"].get("letter_max_words_company", MAX_WORDS)), MAX_WORDS)


def words(paragraphs: list[str]) -> int:
    return len(" ".join(p for p in paragraphs if p).split())


# ------------------------------------------------------------------ the ad
def ad_sentences(jd: str) -> list[str]:
    """The ad as sentences: every line (a bullet, a heading) and every sentence of a line, at least three words long."""
    out: list[str] = []
    for line in (jd or "").splitlines():
        line = _BULLET.sub("", line).strip()
        out += [s.strip() for s in sentences(line) if len(s.split()) >= 3]
    return out


def stems(text: str, skip: frozenset[str] = frozenset()) -> set[str]:
    """The content stems of `text`: three letters or more, not a function word or a generic verb, not in `skip` (stems of the company and the role)."""
    out = set()
    for w in re.findall(r"[a-z][a-z0-9+#]*", norm_literal(text)):
        if len(w) < 3 or w in _SKIP:
            continue
        s = norm_stem(w)
        if s and s not in skip and s not in _SKIP:
            out.add(s)
    return out


def _windows(ad: list[str], skip) -> tuple[list[set[str]], list[set[str]]]:
    """(the stems of each ad sentence, the stems of every run of up to WINDOW consecutive sentences)."""
    each = [stems(s, skip) for s in ad]
    runs = [set().union(*each[i:i + n]) for n in range(1, WINDOW + 1) for i in range(len(each) - n + 1)]
    return each, runs


def clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE.split(sentence) if c and c.strip()]


# ------------------------------------------------------------------ the company guard (P2)
def company_problems(p2: str, jd: str, skip: frozenset[str] = frozenset()) -> list[str]:
    """Every factual claim of P2 must be supported by a phrase of the ad. Normalised overlap, see the module docstring; plus: no number the ad
    does not have."""
    ad = ad_sentences(jd)
    each, runs = _windows(ad, skip)
    in_ad = set().union(*each) if each else set()
    out: list[str] = []
    if not p2.strip():
        return ["P2 is empty"]
    extra = numbers(p2) - numbers(jd)
    if extra:
        out.append(f"number(s) about the company not in the ad: {sorted(extra)}")
    for sentence in sentences(p2):
        for clause in clauses(sentence):
            cs = stems(clause, skip)
            if not cs:
                continue
            if len(cs) < MIN_CLAIM_STEMS:
                ok = cs <= in_ad
            else:
                ok = max((len(cs & run) / len(cs) for run in runs), default=0.0) >= COMPANY_SUPPORT
            if not ok:
                out.append(f"claim about the company is not supported by the ad (not in it: {', '.join(sorted(cs - in_ad)[:6]) or 'no single passage holds it'}): "
                           f"'{clause.strip()[:70]}'")
    return list(dict.fromkeys(out))


def p2_sources(p2: str, jd: str, skip: frozenset[str] = frozenset()) -> list[dict]:
    """[{"claim": a sentence of P2, "ad": [the ad sentences that hold the most of its stems]}] for the review screen."""
    ad = ad_sentences(jd)
    each, _ = _windows(ad, skip)
    rows = []
    for sentence in sentences(p2):
        left = stems(sentence, skip)
        floor = 1 if len(left) < MIN_CLAIM_STEMS else 2
        picked: list[int] = []
        while left and len(picked) < 3 and each:
            best = max(range(len(ad)), key=lambda i: (len(left & each[i]), -i))
            if len(left & each[best]) < floor or best in picked:
                break
            picked.append(best)
            left -= each[best]
        rows.append({"claim": sentence.strip(), "ad": [ad[i] for i in sorted(picked)]})
    return rows


def name_problems(paragraphs: list[str], allowed_text: str, label: str = "") -> list[str]:
    """A capitalised name (a person, a team, a product, a place) that the ad, the fact bank and the opening do not give is invented."""
    allowed = norm_literal(allowed_text)
    out = []
    for sentence in sentences(" ".join(paragraphs)):
        for name in _names(sentence):
            if not _has(allowed, name):
                out.append(f"'{name}' (a name) is not in the ad or the fact bank{label}: '{sentence.strip()[:60]}'")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the user guard (P3, P4)
def _labels(fb: dict) -> dict[str, str]:
    """{normalised label: label} of every employer and project of the fact bank (the short form: the part before the first comma)."""
    out: dict[str, str] = {}
    for sec, kind in (("experience", "experience"), ("projects", "project")):
        for e in (fb or {}).get(sec, []) or []:
            for raw in (org_short(e, kind), e.get("org"), e.get("name")):
                label = re.split(r"\s*,\s*", str(raw or ""))[0].strip()
                if len(label) >= 3:
                    out[norm_literal(label)] = label
    return out


def user_problems(paragraphs: list[str], evidence: list[dict], opening: str, fb: dict, kws: list[Keyword], reqs: list[list[str]],
                  company: str, title: str) -> list[str]:
    """Problems with P3 and P4, the two paragraphs about the user. `paragraphs[i]` is written from `evidence[i]`; `reqs[i]` are the ad
    requirements that bullet evidences (at least one must be named in the paragraph)."""
    out: list[str] = []
    bullets = " ".join(f"{e['text']} {' '.join(e.get('terms', []))} {e.get('where', '')} {e.get('org', '')}" for e in evidence)
    pool = norm_literal(f"{bullets} {opening}")
    allowed_numbers = numbers(bullets) | numbers(opening)
    mine = {norm_literal(e.get("org") or L._owner(fb, e["id"])[0]) for e in evidence} | {norm_literal(e.get("where") or "") for e in evidence}
    for n, par in enumerate(paragraphs):
        tag = f"P{n + 3}"
        extra = numbers(par) - allowed_numbers
        if extra:
            out.append(f"{tag}: number(s) about you not in the chosen bullets: {sorted(extra)}")
        lit = norm_literal(par)
        for tool in L._tools(fb):
            if len(tool) >= 3 and re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", lit) and not re.search(
                    r"(?<![\w])" + re.escape(tool) + r"(?![\w])", pool):
                out.append(f"{tag}: names '{tool}', which is not in the chosen bullets")
        for key, label in _labels(fb).items():
            if re.search(r"(?<![\w])" + re.escape(key) + r"(?![\w])", lit) and key not in pool and not any(key in m for m in mine if m):
                out.append(f"{tag}: names '{label}', which is not where the chosen bullets were done")
        names = [kws[i].term for i in sorted(keyword_hits(par, kws))]
        if reqs[n] and not set(map(str.lower, reqs[n])) & set(map(str.lower, names)):
            out.append(f"{tag}: does not name an ad requirement its bullet evidences (one of: {', '.join(reqs[n])})")
        extra_names = f"{company} {title} {' '.join(reqs[n])} {evidence[n].get('where', '')} {evidence[n].get('org', '')}"
        out += [f"{tag}: {p}" for p in grounding_problems(sentences(par), [evidence[n]], "", opening, extra_names)]
    out += repeated_evidence_problems(paragraphs, evidence)
    out += L.paste_problems(paragraphs, L._all_bullets(fb))
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the whole letter
def check(p1: str, body: list[str], p5: str, job: dict, fb: dict, kws: list[Keyword], evidence: list[dict], reqs: list[list[str]],
          team: str, settings) -> tuple[list[str], list[dict]]:
    """(problems, the ad sentences P2 drew from) for the five paragraphs [p1, *body, p5]; body = [P2, P3, P4]."""
    from . import tailor as T

    jd, company, title = job.get("description") or "", job["company"], job["title"]
    skip = frozenset(stems(f"{company} {title} {team}"))
    letter = [p1, *body, p5]
    text = " ".join(letter)
    out: list[str] = []
    if len(body) != 3:
        return [f"expected 3 paragraphs (P2, P3, P4), got {len(body)}"], []
    n, cap = words(letter), word_cap(settings)
    if n < MIN_WORDS or n > cap:
        out.append(f"{n} words: the letter must be between {MIN_WORDS} and {cap}")
    out += [f"P2: {p}" for p in company_problems(body[0], jd, skip)]
    out += user_problems(body[1:], evidence, p1, fb, kws, reqs, company, title)
    allowed = " ".join([jd, company, title, team, p1, *bank_names(fb), *_labels(fb).values(),
                        *(f"{e['text']} {e.get('where', '')} {e.get('org', '')}" for e in evidence)])
    out += name_problems(body, allowed)
    out += L.claim_problems(body[:1], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", jd)))      # P2: no "we" / "our", no "I help <Company>"
    framing = " ".join([company, title, *(" ".join(e.get("terms", [])) for e in evidence), *(t for r in reqs for t in r)])
    out += L.claim_problems(body[1:], company, fb, set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", framing)))       # P3, P4: and no linked noun phrase
    out += term_dump_problems(body, fb)
    out += T._deny_problems(text, fb.get("deny_terms", []), [])
    out += T._style_problems([*body, p5], [e["text"] for e in evidence])
    if T.BANNED_CLOSERS.search(text):
        out.append("banned closer")
    return list(dict.fromkeys(out)), p2_sources(body[0], jd, skip)


def _requirements(evidence: list[dict], kws: list[Keyword]) -> list[list[str]]:
    """For each evidence bullet the ad's keywords it evidences, heaviest first (at most three): the requirement its paragraph names."""
    out = []
    for e in evidence:
        hits = keyword_hits(" ".join([e["text"], *e.get("terms", [])]), kws)
        out.append([kws[i].term for i in sorted(hits, key=lambda i: (-kws[i].weight, i))][:3])
    return out


def _letter_dict(status: str, **kw) -> dict:
    base = {"paragraphs": [], "problems": [], "score": None, "pages": 0, "words": 0, "status": status, "mode": MODE, "notes": [], "retry": {},
            "evidence": [], "p2_sources": [], "requirements": []}
    return {**base, **kw}


@dataclass
class Plan:
    """What the letter is built from, settled in code before any model is asked: the opening and closing paragraphs, the two evidence bullets
    (with where they were done) and the ad requirement each evidences. `error` (when set) says why there is nothing to build a letter from."""
    p1: str = ""
    p5: str = ""
    evidence: list[dict] = field(default_factory=list)
    reqs: list[list[str]] = field(default_factory=list)
    error: str = ""


def prepare(job: dict, fb: dict, kws: list[Keyword], hk: Hook | None, family_line: str, evidence: list[dict], settings, claimable: list[str],
            chosen_ids: set[str] | None = None) -> Plan:
    """P1 (the hook's letter line, else the role family's profile line, word for word), P5 (closing_hook with the problem phrase of the ad), the
    two evidence bullets and their requirements. With a hook that has a proof bullet, P3 and P4 are written from the best two OTHER bullets of the
    CV (the proof bullet is already P1's story); `evidence` is used when there are not two."""
    from . import tailor as T

    jd = job.get("description") or ""
    p1 = (hk.letter_sentence if hk is not None else family_line).strip()
    if hk is not None and hk.proof_bullet_id and chosen_ids:
        other = L.body_evidence(fb, kws, chosen_ids, jd, hk)
        evidence = other if len(other) >= 2 else evidence
    evidence = T._with_orgs(fb, evidence[:2])
    if not p1:
        return Plan(error="no hook qualifies and the fact bank has no profile line for the role family: the letter has nothing to open with")
    if len(evidence) < 2:
        return Plan(evidence=evidence, error="fewer than two evidence bullets on the CV to write P3 and P4 from")
    reqs = _requirements(evidence, kws)
    for e, r in zip(evidence, reqs):
        if not r:
            return Plan(evidence=evidence, error=f"bullet {e['id']} evidences no requirement of the ad, so its paragraph cannot name one")
    problem = hk.problem.strip() if hk is not None and hk.problem.strip() else L.ad_problem(job, kws, fb, claimable)
    return Plan(p1, L.closing_text(problem, settings), evidence, reqs)


def make(job: dict, fb: dict, kws: list[Keyword], hk: Hook | None, family_line: str, evidence: list[dict], llm, settings, out_dir: Path,
         final_name: str, team: str, claimable: list[str], chosen_ids: set[str] | None = None) -> tuple[Path | None, dict]:
    """(letter pdf or None, the letter dict `report["letter"]` shows). Status "ok", "none" (every draft rejected, or nothing to build it from;
    `reason` and `retry.failures` say why) or "pending" (no provider may write it now: the job waits). `hk` is the hook (None: none qualifies or
    the hook is off), `family_line` the role family's profile line, `evidence` the letter's two best bullets of the CV."""
    from . import tailor as T
    from .render import page_count, pdf_text, render_letter

    jd = job.get("description") or ""
    plan = prepare(job, fb, kws, hk, family_line, evidence, settings, claimable, chosen_ids)
    evidence, reqs, p1, p5 = plan.evidence, plan.reqs, plan.p1, plan.p5
    shown = [{"id": e["id"], "text": e["text"]} for e in evidence]
    notes: list[str] = []

    def none(reason: str, problems: list[str] | None = None, retry: dict | None = None) -> tuple[None, dict]:
        return None, _letter_dict("none", reason=reason, problems=problems or [], notes=notes, retry=retry or {}, evidence=shown)

    if plan.error:
        return none(plan.error)
    target, cap = target_words(settings), word_cap(settings)
    body_words = max(target - words([p1, p5]), 60)
    deny = fb.get("deny_terms", [])
    system = (P.COMPANY_LETTER_SYSTEM.replace("{body_words}", str(body_words)).replace("{lo}", str(MIN_WORDS)).replace("{hi}", str(cap))
              .replace("{deny}", ", ".join(deny) or "(none)"))
    user = P.COMPANY_LETTER_USER.format(
        title=job["title"], company=job["company"], opening=p1, closing=p5, ad=jd[:9000], evidence=evidence_fields.block(evidence),
        requirements="\n".join(f"EVIDENCE {i}: {', '.join(r)}" for i, r in enumerate(reqs, 1)))
    ctx = {"mode": MODE, "company": job["company"], "title": job["title"], "opening": p1, "closing": p5, "ad": jd, "ad_sentences": ad_sentences(jd),
           "evidence": [e["text"] for e in evidence], "requirements": reqs, "body_words": body_words, "min_words": MIN_WORDS, "max_words": cap}
    retry: dict = {"count": 0, "rejected_words": [], "first_problems": [], "failures": [], "removed": [], "outcome": "first draft"}
    feedback, problems, body = "", [], []
    for attempt in range(ATTEMPTS):
        try:
            out = T._llm_json(llm, "cover_letter", system, user + feedback, ctx)
            body, letter_notes = T.normalise_letter(out)
            notes.extend(m for m in (f"cover_letter: {n}" for n in letter_notes) if m not in notes)
        except TaskUnavailable as e:
            return None, _letter_dict("pending", reason=str(e)[:300], notes=notes, evidence=shown)
        except Exception as e:  # noqa: BLE001
            problems = [f"letter LLM failed: {e}"]
            retry["failures"].append({"attempt": attempt + 1, "problems": problems})
            break
        problems, sources = check(p1, body, p5, job, fb, kws, evidence, reqs, team, settings)
        if not problems:
            retry["outcome"] = "ok after redraft" if attempt else "first draft"
            break
        retry["failures"].append({"attempt": attempt + 1, "problems": list(problems)})
        if attempt == 0:
            retry["first_problems"] = list(problems)
        if attempt + 1 < ATTEMPTS:
            retry["count"] = attempt + 1
            notes.append(f"cover_letter: draft {attempt + 1} rejected ({'; '.join(problems)[:200]}); redrafting once")
        feedback = ("\n\nYOUR LAST DRAFT BROKE THESE RULES; fix every one, keep P2 inside the ad, and make P2 + P3 + P4 "
                    f"about {body_words} words:\n" + "\n".join(f"- {p}" for p in problems))
    if problems:
        retry["outcome"] = "no letter"
        shown_problems = "; ".join(problems)[:300]
        return none(f"{len(retry['failures'])} draft(s) rejected by the {MODE} guards; last: {shown_problems}", problems, retry)
    paragraphs = [p1, *body, p5]
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paragraphs, "date": date.today().strftime("%d %B %Y"),
                         "greeting": L.greeting(job, team, settings), "signoff": settings["letter"]["signoff"]}, out_dir, final_name)
    return pdf, _letter_dict("ok", paragraphs=paragraphs, words=words(paragraphs), score=ats.score(pdf_text(pdf), kws).literal,
                             pages=page_count(pdf), notes=notes, retry=retry, evidence=shown, p2_sources=sources, requirements=reqs,
                             target_words=target, hook_source=(hk.source if hk is not None else "family profile line"))
