"""The cover letter of `letter.mode: hook`: ASSEMBLED in code, never written by a model (round of 17 Oct).

Letter A = the hook entry's `letter_line` word for word, then TWO sentences each built from ONE selected bullet's own words, then the
call to action ("Could we take 20 minutes to talk about <problem>?", the problem being the hook entry's `terms` that the ad asked for,
as a lower-case noun phrase). A relocation line from applicant.yaml (`relocation: {willing, scope, remote_ok, letter_line}`) goes before
the call to action when the job is in another city. At most `scoring.letter_max_words` words. Salutation: "Dear <Company> <team the ad
names> team", or "Hi <agency> team" for a recruitment agency.

A body sentence is `<lead-in about the bullet's own terms>: <where>, I <the bullet's first clause>.` The clause is the longest cut of the
bullet (at a comma, a semicolon or a joining word, brackets left out) that keeps the sentence under the paste limit (0.7 of its words in
ANY fact-bank bullet, `letter_rules.paste_overlap`). Because one sentence holds one bullet's words and only that bullet's, it cannot
say that one bullet's tool ran another bullet's work. `claim_problems` is the guard every letter passes (this one and a model-drafted
one): no "our" or "we", no "I help <Company>", no noun phrase that links a bullet's object to a different bullet's tool (evidence, 17 Oct:
"Docker and AWS Lambda ran the test suite", "our LLM migration", "I help Anaplan solve forecasting"). Variant B (a gap sentence) is not
written any more: it was the model's, so B is "none". No letter text is produced by a model here.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .. import ats
from ..hook import Hook, _DANGLING, org_short
from .faithfulness import deny_hits, numbers
from .grounding import sentences
from .letter_rules import (PASTE_OVERLAP, bank_names, gap_display, i_clause, noun_phrase, paste_overlap, term_dump_problems,
                           where)
from .rewrite_rules import GLUE
from .scorer import Keyword, gap_terms, keyword_hits, norm_literal, norm_stem

PASTE_MARGIN = 0.05                  # a built sentence must stay this far under PASTE_OVERLAP: the paste check must pass with room
MIN_CLAUSE_WORDS = 4                 # a bullet's clause shorter than this says nothing
MAX_CLAUSE_WORDS = 24
BODY_SENTENCES = 2
LEAD_INS = ("On {phrase}, here is my closest example", "On {phrase}, here is a second example")
LEAD_INS_BARE = ("Here is my closest example", "Here is a second example")
VARIANTS = ("A", "B")
FAMILY_SOURCE = "family"             # Hook.source of the stand-in hook of a job with no library hook: sentence 1 is the role family's profile line
DECLINED = "declined"                # status of the letter the user chose to go without in review
HOME_PLACES = frozenset({"", "uk", "united kingdom", "remote", "england", "scotland", "wales", "great britain", "britain"})


# ------------------------------------------------------------------ the pieces code writes
TITLES = frozenset("dr mr mrs ms miss mx prof professor sir".split())


def contact_first_name(job: dict) -> str:
    """The first name of the person the letter is for (`job["contact"]["name"]` or `job["contact_name"]`, set from the job's stored contact); '' when unknown."""
    tokens = str(job.get("contact_name") or (job.get("contact") or {}).get("name") or "").split()
    while tokens and tokens[0].strip(".,;:").lower() in TITLES and len(tokens) > 1:
        tokens = tokens[1:]                                                       # "Dr. Priya Nair": the first name is Priya
    first = tokens[0].strip(".,;:") if tokens else ""
    return first if re.fullmatch(r"[^\W\d_][\w'’-]*", first) else ""


# A department or function word is not a team name (10 Nov 2026: RightShip's "Product team" gave "Hi RightShip Product team"; the user wants
# "Hi RightShip team" unless the ad names a team, such as "AI Accelerator").
GENERIC_TEAMS = frozenset("""product engineering data ai ml tech technology it software platform platforms analytics research design sales marketing
operations ops finance people hr legal security infrastructure development r&d innovation digital science""".split()) | frozenset(
    {"data science", "machine learning", "data and ai", "data & ai", "ai and data", "product and engineering", "product engineering",
     "software engineering", "data engineering", "applied ai", "ai/ml", "customer success"})


def named_team(team: str) -> str:
    """The team for the greeting: the name the ad gives, never a function word on its own ("Product", "Engineering", "Data Science")."""
    from .tailor import strip_team_word

    t = strip_team_word(team or "")
    return "" if " ".join(t.lower().split()) in GENERIC_TEAMS else t


def greeting(job: dict, team: str, settings) -> str:
    """"Hi <first name>" when the contact's name is known, else "Hi <Company> team" (`letter.greeting_hook`; never "Dear Sir/Madam"); a recruitment
    agency gets "Hi <agency> team". "Hi <Company> <Team> team" only when the ad names a team (`named_team`). The comma is added by the template."""
    cfg = settings["letter"]
    team = named_team(team)
    if contact_first_name(job) and not job.get("agency"):
        return f"Hi {contact_first_name(job)}"
    if job.get("agency"):
        return (cfg.get("greeting_agency") or "Hi {company} team").format(company=job["company"], team=f" {team}" if team else "")
    return (cfg.get("greeting_hook") or "Hi {company}{team} team").format(company=job["company"], team=f" {team}" if team else "")


def closing_text(problem: str, settings) -> str:
    """The call to action for a problem phrase: `letter.closing_hook`, written in settings and never by a model."""
    return (settings["letter"].get("closing_hook") or "Could we take 20 minutes to talk about {problem}?").format(
        problem=problem.strip().rstrip(".?"))


def closing(hk: Hook, settings) -> str:
    """The call to action: a 20-minute ask that names one problem from the ad (the hook's problem phrase)."""
    return closing_text(hk.problem, settings)


def _home_city(applicant: dict, fb: dict) -> str:
    loc = str((applicant.get("identity") or {}).get("location") or (fb.get("identity") or {}).get("location") or "")
    return re.split(r"[,;(]", loc)[0].strip().lower()


def job_city(job: dict) -> str:
    """The town of the job's place ("London, UK" -> "London"), "" when it names none (remote, a country only)."""
    place = re.split(r"[,;(/–-]", str(job.get("location") or ""), maxsplit=1)[0].strip()
    return "" if place.lower() in HOME_PLACES or re.search(r"\bremote\b", place, re.I) else place


def relocation_line(job: dict, applicant: dict | None, fb: dict | None = None) -> str:
    """The relocation sentence from applicant.yaml -> relocation, "" when there is none to say: no `relocation` block, `willing`
    false, a remote job (`remote_ok`), a job in the town the applicant lives in, a place that names no town, or a `scope` other
    than `anywhere` (or `uk` for a UK job)."""
    reloc = (applicant or {}).get("relocation") or {}
    line = str(reloc.get("letter_line") or "").strip()
    city = job_city(job)
    if not (reloc.get("willing") and line and city):
        return ""
    if city.lower() == _home_city(applicant or {}, fb or {}):
        return ""
    scope = str(reloc.get("scope") or "anywhere").strip().lower()
    if scope == "uk" and not re.search(r"\b(uk|united kingdom|england|scotland|wales)\b", str(job.get("location") or ""), re.I):
        return ""
    if scope not in ("anywhere", "uk"):
        return ""
    return line.format(city=city)


# ------------------------------------------------------------------ the learn list and the gap
def learn_list(kws: list[Keyword], claimable: list[str], deny: list[str]) -> list[dict]:
    """The ad's keywords the fact bank cannot claim, heaviest first: [{"term", "weight", "required", "confirmed_absent"}]. A term on
    the deny list is one the user has confirmed they lack."""
    have = {c.lower() for c in claimable}
    rows = [{"term": k.term, "weight": k.weight, "required": k.required, "confirmed_absent": bool(deny_hits(k.term, deny))}
            for k in kws if k.term.lower() not in have]
    return sorted(rows, key=lambda r: -r["weight"])            # stable: the JD's own order within a weight


def pick_gap(kws: list[Keyword], learn: list[dict], jd: str) -> str | None:
    """The heaviest REQUIRED keyword of the learn list that is a tool, platform, language or framework (the kinds a person can
    honestly say they have not used yet), in the JD's own casing."""
    nameable = gap_terms()
    for row in learn:
        if row["required"] and norm_literal(row["term"]) in nameable:
            return gap_display(row["term"], jd)
    return None


def pick_bridge(gap: str, fb: dict, kws: list[Keyword], claimable: list[str], possible: dict | None) -> str | None:
    """A skill of the fact bank to bridge the gap to: a neighbour term (evidence family) that the bank really says (it has places
    in the bank) and does not deny, else the heaviest claimable keyword that can be named (tool / framework / platform / language).
    None when the bank has neither: never a term the bank cannot back."""
    deny = (fb or {}).get("deny_terms", [])
    for key, via in (possible or {}).items():
        if key.lower() != gap.lower():
            continue
        for v in via or []:
            if v.get("where") and not deny_hits(str(v["via"]), deny):
                return str(v["via"])
    nameable = gap_terms()
    have = {c.lower() for c in claimable}
    pool = [k for k in kws if k.term.lower() in have and norm_literal(k.term) in nameable]
    return max(pool, key=lambda k: k.weight).term if pool else None


# ------------------------------------------------------------------ the checks
def _all_bullets(fb: dict) -> list[str]:
    from .tailor import bullet_index

    return [b["text"] for b in bullet_index(fb).values()]


def paste_problems(paragraphs: list[str], bullets: list[str]) -> list[str]:
    """A sentence with PASTE_OVERLAP (0.7) or more of its words in ANY fact-bank bullet is that bullet pasted in."""
    out = []
    for s in sentences(" ".join(paragraphs)):
        share = max((paste_overlap(s, b) for b in bullets), default=0.0)
        if share >= PASTE_OVERLAP:
            out.append(f"pastes a fact-bank bullet ({share:.0%} of its words are the bullet's): '{s.strip()[:50]}'")
    return out


def _words(paragraphs: list[str]) -> int:
    return len(" ".join(paragraphs).split())


_WE = re.compile(r"\b(our|ours|we|we're|we've|we'll|we'd)\b", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")
_PREP = r"(?:on|in|with|using|via|through|across|inside|within)"
_RAN = r"(?:ran|runs|run|running|hosted|hosts|served|serves|executed|executes|powered|powers)"
_DET = r"(?:(?:the|a|an|my)\s+)?"


def _stems(text: str) -> set[str]:
    return {norm_stem(w) for w in _WORD.findall(text) if len(w) > 2 and w.lower() not in GLUE}


def _tools(fb: dict) -> list[str]:
    """Every skill and stack name of the fact bank, normalised, longest first: the names a sentence can attach a piece of work to."""
    names: set[str] = set()
    for items in (fb.get("skills") or {}).values():
        names.update(str(s) for s in items if isinstance(items, list))
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []) or []:
            names.update(x for x in re.split(r"\s*,\s*", str(e.get("stack") or "")) if x)
    out = {norm_literal(re.sub(r"\s*\([^)]*\)", "", n)).strip() for n in names}
    return sorted((n for n in out if len(n) >= 2), key=len, reverse=True)


def link_problems(sentence: str, fb: dict, exempt: frozenset[str] = frozenset()) -> list[str]:
    """A noun phrase that attaches one bullet's object to a tool only a DIFFERENT bullet names: "ran the tests on Docker" (the tests are
    one bullet, Docker another) or "Docker and AWS Lambda ran the test framework". The object is the up-to-four words before
    "on / in / with / using ..." the tool, or the words after "<tool> ran / hosted / served ..."; it is linked when some bullet says
    the object, some bullet says the tool, and no bullet says both. `exempt` = stems of the words the letter's own framing supplies."""
    from .tailor import bullet_index

    bullets = {bid: b["text"] for bid, b in bullet_index(fb).items()}
    stems = {bid: _stems(text) for bid, text in bullets.items()}
    lits = {bid: norm_literal(text) for bid, text in bullets.items()}
    tools = _tools(fb)
    tool_stems = {s for t in tools for s in _stems(t)}
    n = norm_literal(sentence)
    spans: list[tuple[int, int, str]] = []
    for tool in tools:
        for m in re.finditer(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", n):
            if not any(a <= m.start() and m.end() <= b for a, b, _ in spans):
                spans.append((m.start(), m.end(), tool))
    out: list[str] = []
    for s, e, tool in spans:
        found: list[str] = []
        before = re.search(r"((?:[\w~/.'-]+\s+){1,4})" + _PREP + r"\s+" + _DET + r"(?:[^,.;:]*?\band\s+)?$", n[:s])
        if before:
            found.append(before.group(1))
        after = re.match(r"(?:\s+and\s+[^,.;:]*?)?\s+" + _RAN + r"\s+" + _DET + r"([^,.;:]+)", n[e:])
        if after:
            found.append(" ".join(after.group(1).split()[:4]))
        for obj in found:
            words = {norm_stem(w) for w in _WORD.findall(obj) if len(w) > 2 and w not in GLUE} - tool_stems - set(exempt)
            obj_in = {bid for bid, st in stems.items() if st & words}
            tool_in = {bid for bid, lit in lits.items() if re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", lit)}
            if obj_in and tool_in and not obj_in & tool_in:
                out.append(f"links '{obj.strip()}' to '{tool}', but no one bullet says both (the object is in {', '.join(sorted(obj_in)[:3])}, "
                           f"the tool in {', '.join(sorted(tool_in)[:3])}): '{sentence.strip()[:60]}'")
    return out


def claim_problems(paragraphs: list[str], company: str, fb: dict, exempt=()) -> list[str]:
    """The guard every letter passes. Rejects "our" / "we" (one person writes about their own work, not for the employer), "I help
    <Company>" (a claim about the employer's results), and any noun phrase that links a bullet's object to a different bullet's tool
    (`link_problems`). `exempt`: words the letter's own framing supplies (company, title, the hook's problem phrase, the bullets'
    terms), which are not part of the object."""
    text = " ".join(p for p in paragraphs if p)
    stems = frozenset(norm_stem(w) for w in exempt)
    out = [f"uses '{m.group(0).lower()}': the letter speaks for one person about their own work" for m in _WE.finditer(text)]
    names = [company.strip()] + [w for w in company.split()[:1] if len(w) >= 3]
    for name in dict.fromkeys(n for n in names if n):
        if re.search(r"\bI\s+(?:can\s+|will\s+|could\s+|would\s+|aim\s+to\s+|hope\s+to\s+)?help(?:s|ed|ing)?\s+(?:the\s+)?" + re.escape(name),
                     text, re.I):
            out.append(f"claims 'I help {name}': a result for the employer the fact bank cannot back")
    for s in sentences(text):
        out += link_problems(s, fb, stems)
    return list(dict.fromkeys(out))


def exempt_words(hk: Hook, job: dict, evidence: list[dict]) -> set[str]:
    """The words a built letter's framing supplies, which `claim_problems` does not treat as part of a bullet's object: the company, the
    job title, the hook's problem phrase and employer, and the terms of the evidence bullets."""
    text = " ".join([job.get("company", ""), job.get("title", ""), hk.problem, hk.org, *(" ".join(e.get("terms", [])) for e in evidence)])
    return {w.lower() for w in _WORD.findall(text)}


# ------------------------------------------------------------------ the two sentences
_BRACKET = re.compile(r"\s*\([^)]*\)")
_CUT = re.compile(r",(?!\d)|;|:|\s+(?:with|for|to|using|across|on|by|and|while|including|from|via|replacing|validated)\s+")


def _fragments(text: str) -> list[str]:
    """The cuts of a bullet's text that can follow "I ...", longest first: the whole text without its brackets, then every cut at a comma,
    a semicolon or a joining word, with dangling joining words taken off the end. Between MIN_CLAUSE_WORDS and MAX_CLAUSE_WORDS words."""
    t = " ".join(_BRACKET.sub("", text).split()).rstrip(".")
    cuts = [t] + [t[:m.start()].rstrip() for m in reversed(list(_CUT.finditer(t)))]
    out: list[str] = []
    for c in cuts:
        words = c.split()
        while words and words[-1].lower().strip(",;:") in _DANGLING:
            words.pop()
        frag = " ".join(words).rstrip(",;:")
        if MIN_CLAUSE_WORDS <= len(words) <= MAX_CLAUSE_WORDS and frag not in out:
            out.append(frag)
    return out


def _owner(fb: dict, bullet_id: str) -> tuple[str, bool]:
    """(where the bullet was done, is it a project): the employer, or the project's name."""
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            if any(b["id"] == bullet_id for b in e["bullets"]):
                label = org_short(e, "project" if sec == "projects" else "experience")
                return re.split(r"\s*,\s*", label)[0], sec == "projects"
    return "", False


def assemble_body(fb: dict, evidence: list[dict], hk: Hook, kws: list[Keyword], jd: str, company: str) -> list[str]:
    """The letter's two body sentences, one per evidence bullet, in code: `<lead-in about the bullet's own terms the ad asked for>:
    <where>, I <the bullet's clause>.` Each clause is the longest cut of that bullet whose sentence stays under PASTE_OVERLAP with room
    against EVERY fact-bank bullet. A bullet no cut of which passes gives no sentence ([] overall: no letter is better than a pasted
    one). Fewer than two evidence bullets give fewer sentences."""
    bullets = _all_bullets(fb)
    out: list[str] = []
    for n, e in enumerate(evidence[:BODY_SENTENCES]):
        org, project = _owner(fb, e["id"])
        asked = [t for t in e.get("terms", []) if keyword_hits(str(t), kws)]
        phrase = noun_phrase(asked[:2], jd, bank_names(fb))
        lead = (LEAD_INS[n] if phrase else LEAD_INS_BARE[n]).format(phrase=phrase)
        built = None
        for frag in _fragments(e["text"]):
            s = f"{lead}: {where(org, project, False)}, {i_clause(frag)}."
            if max((paste_overlap(s, b) for b in bullets), default=0.0) < PASTE_OVERLAP - PASTE_MARGIN:
                built = s
                break
        if built is None:
            return []
        out.append(built)
    return out


# ------------------------------------------------------------------ both variants
def body_evidence(fb: dict, kws: list[Keyword], chosen_ids: set[str], jd: str, hk: Hook) -> list[dict]:
    """The two bullets the sentences are built from: the best of the bullets on the CV other than the hook's proof bullet, from different
    entries when it can (the hook sentence already tells the proof)."""
    from .tailor import letter_evidence

    pool = {b for b in chosen_ids if b != hk.proof_bullet_id}
    return letter_evidence(fb, kws, pool, jd) if pool else []


def make_letters(job: dict, fb: dict, kws: list[Keyword], company_hook: str, hk: Hook, llm, settings, out_dir: Path, final_name: str,
                 team: str, chosen_ids: set[str], claimable: list[str], possible: dict | None, applicant: dict | None) -> dict:
    """Both variants for one job: {"A": letter, "B": letter, "choice": "A" | "none"}. A letter is {"variant", "status" (ok | none),
    "reason", "paragraphs", "problems", "score" (the ATS score of the letter alone, 0-1), "pages", "words", "pdf" (path or None), "tex",
    "notes", "retry", "evidence", "gap", "bridge", "gap_source"}. A is assembled by code (`assemble_body`); B is always "none". The PDF
    goes to <out_dir>/letters/A/ under the final file name. `llm` is accepted for the old signature and never used: no model writes a
    letter."""
    from .render import page_count, pdf_text, render_letter

    jd = job["description"] or ""
    max_words = settings["scoring"]["letter_max_words"]
    evidence = body_evidence(fb, kws, chosen_ids, jd, hk)
    base = {"evidence": [{"id": e["id"], "text": e["text"]} for e in evidence], "gap": None, "bridge": None, "gap_source": None}
    notes: list[str] = list(hk.notes) if hk.source == FAMILY_SOURCE else []

    def empty(variant: str, reason: str, problems: list[str] | None = None) -> dict:
        return {**base, "variant": variant, "status": "none", "reason": reason, "paragraphs": [], "problems": problems or [],
                "score": None, "pages": 0, "words": 0, "pdf": None, "tex": None, "notes": list(notes), "retry": {}}

    b_none = empty("B", "no gap sentence is written: no model writes letter text, so there is no variant B")
    if not evidence:
        return {"A": empty("A", "no evidence bullet on the CV to build a sentence from"), "B": b_none, "choice": "none", "retry": {}}
    body = assemble_body(fb, evidence, hk, kws, jd, job["company"])
    if len(body) < min(BODY_SENTENCES, len(evidence)):
        return {"A": empty("A", "no cut of an evidence bullet makes a sentence that is not a paste of the bullet"), "B": b_none,
                "choice": "none", "retry": {}}
    reloc = relocation_line(job, applicant, fb)
    final = " ".join(x for x in (reloc, closing(hk, settings)) if x)
    paragraphs = [hk.letter_sentence, " ".join(body), final]
    own = " ".join([*(e["text"] for e in evidence), *(_owner(fb, e["id"])[0] for e in evidence)])      # the bullets and the names of where they were done ("180 Degrees")
    extra = numbers(" ".join(body)) - numbers(own)
    problems = (claim_problems(body, job["company"], fb, exempt_words(hk, job, evidence)) + paste_problems(body, _all_bullets(fb))
                + term_dump_problems(body, fb) + [f"deny-listed '{d}'" for d in deny_hits(" ".join(paragraphs), fb.get("deny_terms", []))]
                + ([f"new numbers {sorted(extra)}"] if extra else [])
                + ([f"{_words(paragraphs)} words > {max_words}"] if _words(paragraphs) > max_words else []))
    if problems:
        return {"A": empty("A", "the built letter failed its own checks", list(dict.fromkeys(problems))), "B": b_none, "choice": "none",
                "retry": {}}
    folder = out_dir / "letters" / "A"
    pdf = render_letter({"id": fb["identity"], "company": job["company"], "paragraphs": paragraphs, "date": date.today().strftime("%d %B %Y"),
                         "greeting": greeting(job, team, settings), "signoff": settings["letter"]["signoff"]}, folder, final_name)
    a = {**base, "variant": "A", "status": "ok", "reason": "", "paragraphs": paragraphs, "problems": [],
         "score": ats.score(pdf_text(pdf), kws).literal, "pages": page_count(pdf), "words": _words(paragraphs), "pdf": str(pdf),
         "tex": str(pdf.with_suffix(".tex")), "notes": notes, "retry": {}}
    return {"A": a, "B": b_none, "choice": "A", "retry": {}}


def ad_problem(job: dict, kws: list[Keyword], fb: dict, claimable: list[str], headline: str = "") -> str:
    """A problem phrase made of the ad's own words, for the call to action of a letter with no hook entry: the ad's two heaviest keywords
    the fact bank can claim (the JD's order on a tie), as a lower-case noun phrase; else "the <role> role"."""
    have = {c.lower() for c in claimable}
    deny = (fb or {}).get("deny_terms", [])
    pool = sorted((k for k in kws if k.term.lower() in have and not deny_hits(k.term, deny)), key=lambda k: -k.weight)
    return noun_phrase([k.term for k in pool[:2]], job.get("description") or "", bank_names(fb)) or f"the {headline or job.get('title') or 'role'} role"


def family_hook(job: dict, kws: list[Keyword], fb: dict, claimable: list[str], line: str, headline: str = "") -> Hook:
    """The stand-in hook of a job that no hook_library entry fits: sentence 1 of the letter is `line` (the role family's profile line from the
    fact bank, word for word), the call to action names the ad's two heaviest keywords the fact bank can claim (else the role). It carries
    no proof bullet, so both body sentences come from the CV's own bullets."""
    problem = ad_problem(job, kws, fb, claimable, headline)
    text = " ".join(line.split())
    return Hook(problem, "", "", headline, text, text, source=FAMILY_SOURCE,
                notes=["no hook_library entry qualifies: sentence 1 is the role family's profile line, word for word"])


def none_letters(reason: str) -> dict:
    """Both variants "none" with the same reason (the letters of a job that has no hook): nothing is written and no file exists."""
    def one(variant: str) -> dict:
        return {"variant": variant, "status": "none", "reason": reason, "paragraphs": [], "problems": [], "score": None, "pages": 0,
                "words": 0, "pdf": None, "tex": None, "notes": [], "retry": {}, "evidence": [], "gap": None, "bridge": None,
                "gap_source": None}

    return {"A": one("A"), "B": one("B"), "choice": "none", "retry": {}}


def current(letters: dict) -> dict:
    """The letter that `report["letter"]` shows: the chosen variant; the "declined" stand-in when the user went without a letter in
    review; else variant A (which then holds why no letter was produced: none or pending)."""
    c = letters.get("choice", "none")
    if c in VARIANTS:
        return letters[c]
    return declined() if letters.get("declined") else letters["A"]


def declined() -> dict:
    """What `report["letter"]` holds when the user chose to go without a letter."""
    return {"variant": None, "status": DECLINED, "reason": "you chose to send no letter", "paragraphs": [], "problems": [], "score": None,
            "pages": 0, "words": 0, "pdf": None, "tex": None, "notes": [], "retry": {}, "evidence": [], "gap": None, "bridge": None}


def next_choice(letters: dict, choice: str) -> str:
    """A -> B -> none -> A, skipping a variant that was not produced."""
    order = ["A", "B", "none"]
    here = order.index(choice) if choice in order else 2
    for step in range(1, 4):
        c = order[(here + step) % 3]
        if c == "none" or letters.get(c, {}).get("status") == "ok":
            return c
    return "none"
