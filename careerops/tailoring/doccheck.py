"""The document check: every rule of the Document rules (CLAUDE.md) on a finished CV and letter, in one answer.

    DOC CHECK: pass
    DOC CHECK: FAILED (2)
      - PROFILE 84/100 words: add profile sentences to fact_bank.yaml → profile.sentences
      - SKILLS repeat "Lambda" in Cloud & Deployment and Software: fix in fact_bank.yaml families.ai_engineer.skill_lines

CV (the render context, the .tex and the PDF text of the final files):
  HOOK            the headline, profile sentence 1 and the first bullet of the hook's role are the same hook entry's, or, with no hook, the
                  role family's headline and profile line
  PROFILE         100 to 150 words
  SKILLS          no skill twice (profile.py / skills.row_repeats); a verbatim family line with a repeat is reported, never rewritten
  PROJECTS        at most 3, each 1 to 3 bullets; a URL only for a project the bank does not mark `public: false`
  CERTIFICATIONS  a bulleted list, one item per certification, no middle-dot join
  CONTEXT         (v3.5.2, honesty over lint) an entry the fact bank marks as a dissertation, coursework, volunteer or hackathon piece of work (the
                  word in its title, employment_type, name, display_name, badge or kind) shows that word in its own CV heading (title, name, tag or
                  stack line; a bullet that happens to say it does not count); a degree whose display_line names a dissertation keeps a dissertation line in Education
Cover letter (when there is one; see `letter_problems`): length, the CV's header, the CV's evidence, the opening, no reuse.

A failure blocks review's `[a]pprove` (the user may type `override doc check` in full) and always blocks auto-submit (apply/autogate.py). The
check only reads: it never edits a report, a fact bank or a file. A one-page CV (`--one-page`, the old template) is exempt from the
two-page rules (profile length, projects, certifications as a list).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import company_letter as CL
from .attempts import job_name as attempts_job_name
from . import letter_reuse as RU
from . import opening as OP
from . import profile as profile_mod
from . import render
from . import skills as skills_mod
from .scorer import Keyword
from .selection import MAX_PROJECTS

OVERRIDE = "override doc check"            # what the user types in full to approve a CV that failed the check
PROJECT_BULLETS = (1, 3)


class DocCheckError(Exception):
    """Approving a CV that failed the document check without typing OVERRIDE."""


@dataclass
class DocCheck:
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def text(self) -> str:
        if self.ok:
            return "DOC CHECK: pass"
        return "\n".join([f"DOC CHECK: FAILED ({len(self.failures)})", *(f"  - {f}" for f in self.failures)])


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _words(text) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _inside(shown: str, whole: str) -> bool:
    """Is every word of `shown` a word of `whole`? A headline the length fit shortened clause by clause still is the hook's."""
    return bool(_words(shown)) and set(_words(shown)) <= set(_words(whole))


# ------------------------------------------------------------------------------------------------------------------ the CV rules
def hook_problems(rep: Mapping[str, Any]) -> list[str]:
    """A1: headline, profile sentence 1 and the first bullet of the hook's role come from one hook entry; with no hook, from the role family."""
    ctx, hk = rep["context"], rep.get("hook")
    out: list[str] = []
    profile = _norm(ctx.get("profile"))
    if hk:
        shown = ctx.get("hook_headline") or ""
        if not _inside(shown, hk.get("headline", "")):
            out.append(f"HOOK headline \"{shown}\" is not the headline of hook {hk.get('hook_id') or '?'} (\"{hk.get('headline', '')}\")")
        if not profile.startswith(_norm(hk.get("profile_line"))):
            out.append(f"HOOK profile sentence 1 is not the profile line of hook {hk.get('hook_id') or '?'}")
        section = "experience" if hk.get("entry_kind", "experience") == "experience" else "projects"
        entry = next((e for e in ctx.get(section) or [] if e.get("id") == hk.get("entry_id")), None)
        if entry is None:
            out.append(f"HOOK first bullet: the hook's {hk.get('entry_kind', 'experience')} {hk.get('entry_id')} is not on the CV")
        elif (entry.get("bullet_ids") or [None])[0] != hk.get("proof_bullet_id"):
            first = (entry.get("bullet_ids") or ["none"])[0]
            out.append(f"HOOK first bullet of {hk.get('entry_id')} is {first}, not the hook's proof bullet {hk.get('proof_bullet_id')}")
        return out
    family, shown = rep.get("headline") or "", ctx.get("hook_headline") or (ctx.get("id") or {}).get("headline") or ""
    if family and not _inside(shown, family):
        out.append(f"HOOK headline \"{shown}\" is not the role family's headline (\"{family}\")")
    opening = _norm(rep.get("family_opening"))                    # v3.5.3: no hook = the family's opening sentence first, then its profile line
    line = _norm(" ".join(x for x in (rep.get("family_opening"), rep.get("family_line")) if x))
    if opening and not profile.startswith(opening):
        out.append("HOOK profile sentence 1 is not the role family's opening sentence (profile_opening)")
    elif line and not profile.startswith(line):
        out.append("HOOK profile sentence 1 is not the role family's profile line")
    return out


def profile_problems(rep: Mapping[str, Any], settings) -> list[str]:
    """A2: 100 to 150 words (scoring.profile_words)."""
    return profile_mod.word_problems(str(rep["context"].get("profile") or ""), profile_mod.word_range(settings))


def skill_problems(rep: Mapping[str, Any]) -> list[str]:
    """A3: no skill twice in the Skills section."""
    family = (rep.get("skills") or {}).get("family")
    return skills_mod.repeat_problems(rep["context"].get("skills") or {}, family=str(family) if family else None)


def _tex_url(url: str) -> str:
    return url.replace("%", r"\%").replace("#", r"\#")


def project_problems(rep: Mapping[str, Any], tex: str | None, settings=None) -> list[str]:
    """A4: at most 3 projects, each with 1 to 3 bullets, and a URL exactly when the bank does not say `public: false`; with
    scoring.cv_projects_need_url (v2.3) every project has a public URL."""
    projects = rep["context"].get("projects") or []
    out: list[str] = []
    if settings is not None and settings["scoring"].get("cv_projects_need_url"):
        out += [f"PROJECTS \"{p.get('name') or p.get('id')}\" has no public GitHub URL (scoring.cv_projects_need_url)" for p in projects if not render.url_shown(p)]
    if len(projects) > MAX_PROJECTS:
        out.append(f"PROJECTS {len(projects)} projects on the CV (at most {MAX_PROJECTS})")
    lo, hi = PROJECT_BULLETS
    for p in projects:
        n = len(p.get("bullets") or [])
        name = p.get("name") or p.get("id")
        if not lo <= n <= hi:
            out.append(f"PROJECTS \"{name}\" has {n} bullets ({lo} to {hi})")
        url = str(p.get("github") or "")
        if not url or tex is None:
            continue
        printed = _tex_url(url) in tex
        if render.url_shown(p) == "" and printed:
            out.append(f"PROJECTS \"{name}\" prints {url} but the fact bank says public: false")
        elif render.url_shown(p) and not render.project_parts(p)[1] and not printed:
            out.append(f"PROJECTS \"{name}\" does not print its public URL {url}")
    return out


def cert_problems(rep: Mapping[str, Any], tex: str | None) -> list[str]:
    """A6: Certifications & Achievements is a bulleted list with one item per certification (and per award line), not joined with a dot."""
    if tex is None:
        return []
    ctx = rep["context"]
    wanted = len(ctx.get("certifications") or []) + len(ctx.get("achievements") or [])
    start = tex.find("\\section{Certifications")
    if start < 0:
        return ["CERTIFICATIONS section is missing"] if wanted else []
    end = min((i for i in (tex.find("\\section{", start + 10), tex.find("\\end{document}", start)) if i >= 0), default=len(tex))
    block = tex[start:end]
    items = block.split("\\begin{itemize}", 1)[-1].split("\\end{itemize}", 1)[0] if "\\begin{itemize}" in block else ""
    out: list[str] = []
    n = items.count("\\item")
    if n != wanted:
        out.append(f"CERTIFICATIONS {n} list item(s) for {wanted} certification(s): one item each")
    if "textperiodcentered" in items or "·" in items:
        out.append("CERTIFICATIONS are joined with a middle dot")
    return out


MARKS = ("dissertation", "coursework", "volunteer", "hackathon")
MARK_FIELDS = ("title", "employment_type", "name", "display_name", "badge", "kind", "type")
_MARK = re.compile(r"\b(dissertation|coursework|volunteer(?:ing|ed|s)?|hackathon)s?\b", re.I)


def marks(entry: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """[(word, field, value)]: the context words (MARKS) a fact-bank entry carries in MARK_FIELDS, each word once."""
    out: list[tuple[str, str, str]] = []
    for key in MARK_FIELDS:
        value = " ".join(str(entry.get(key) or "").split())
        for m in _MARK.finditer(value):
            word = next(w for w in MARKS if m.group(1).lower().startswith(w))
            if word not in [o[0] for o in out]:
                out.append((word, key, value))
    return out


def _shows(text: str, word: str) -> bool:
    return re.search(rf"\b{word}", text, re.I) is not None


def printed_entry(e: Mapping[str, Any], kind: str) -> str:
    """The heading a CV entry prints (its bullets change from job to job, so they do not count): experience = employer, location and title;
    project = name, tag and stack line."""
    if kind == "experience":
        return " ".join([*render.org_parts(dict(e)), render.role_line(dict(e))])
    return " ".join([*render.project_parts(dict(e)), str(e.get("stack") or "")])


def context_problems(rep: Mapping[str, Any], fb: Mapping[str, Any] | None) -> list[str]:
    """CONTEXT (10 Nov 2026): the CV never drops the context the fact bank gives a piece of work. A role or project marked dissertation /
    coursework / volunteer / hackathon (`marks`) shows that word in its own CV heading, or in its Additional experience line; a degree whose
    display_line names a dissertation keeps a dissertation line in the compact Education."""
    if fb is None:
        return []
    ctx, out = rep["context"], []
    extra = " ".join(str(x) for x in ctx.get("additional_experience") or [])
    for sec, kind in (("experience", "experience"), ("projects", "project")):
        bank = {str(e.get("id")): e for e in fb.get(sec) or [] if isinstance(e, Mapping)}
        for e in ctx.get(sec) or []:
            entry = bank.get(str(e.get("id")))
            if entry is None:
                continue
            shown = printed_entry(e, sec)
            label = str(entry.get("org") or entry.get("name") or entry.get("id"))
            for word, key, value in marks(entry):
                if not _shows(shown, word):
                    out.append(f"CONTEXT {kind} \"{label}\" is marked {word} in the fact bank ({key}: \"{value}\") but its CV entry never says "
                               f"\"{word}\": put the word in its {'title' if sec == 'experience' else 'name'} (fact_bank.yaml {sec} id `{entry.get('id')}`)")
        if sec == "experience" and extra:
            on_cv = {str(e.get("id")) for e in ctx.get(sec) or []}
            for eid, entry in bank.items():
                org = str(entry.get("org") or "")
                if eid in on_cv or not org or org.split(",")[0] not in extra:
                    continue
                for word, key, value in marks(entry):
                    if not _shows(extra, word):
                        out.append(f"CONTEXT experience \"{org}\" is marked {word} in the fact bank ({key}: \"{value}\") but its Additional "
                                   f"experience line never says \"{word}\" (fact_bank.yaml experience id `{eid}`)")
    rows = ctx.get("education_compact") or []
    if rows and not rep.get("one_page"):
        from . import sections as SEC
        said = " ".join(str(r.get("text") or "") for r in rows if r.get("kind") == "dissertation")
        for ed in fb.get("education") or []:
            if isinstance(ed, Mapping) and (str(ed.get("dissertation") or "").strip() or SEC.dissertation_clause(ed)) and not _shows(said, "dissertation"):
                out.append(f"CONTEXT education \"{ed.get('degree')}\" names a dissertation in the fact bank but the CV's Education has no "
                           "dissertation line")
    return out


# --------------------------------------------------------------------------------------------------------------------- the cover letter
def letter_problems(rep: Mapping[str, Any], settings, letter_tex: str | None, fb: Mapping[str, Any] | None, texts: Sequence[str],
                    earlier: Sequence[RU.Earlier]) -> list[str]:
    """B1 to B5 (v2.3) on a company-mode letter that exists (status ok): 200 to 320 body words; the CV's headline and contact line in its header;
    P1, P3, P4, P5 and P7 present; the evidence and the skills it names on this CV and a body whose P1 to P4 are mostly about the CV's bullets; an
    opening that carries a fact-bank number and quotes a problem phrase of the ad or the company's page; no memory / GB figure for an ad that is not
    about hardware; and no copy of an earlier letter (P2 to P5). Other letter modes, and a letter that does not exist, are not judged here (review
    has its own confirmations for those)."""
    letter = rep.get("letter") or {}
    paragraphs = [str(p) for p in letter.get("paragraphs") or []]
    names = list(letter.get("names") or [])
    if letter.get("mode") != "company" or letter.get("status") != "ok" or not names or len(names) != len(paragraphs):
        return []
    parts = dict(zip(names, paragraphs))
    ctx, out = rep["context"], []
    if letter.get("draft"):                       # v3.5.3: no letter_template for the family, or the template letter failed its fact check
        out.append(f"LETTER is a DRAFT ({letter.get('draft_reason') or 'not checked'}): edit it with [e] before it can be approved")
    if letter.get("compose") == "template":       # v3.5.3: the user's own letter with three slots: the template checks, not P3 / P4 rules
        return out + template_letter_problems(rep, letter, parts, settings, letter_tex, fb, texts)
    absent = [n for n in ("P1", "P3", "P4", "P5", "P7") if n not in parts]
    if absent:
        out.append(f"LETTER lacks paragraph(s) {', '.join(absent)}")
    lo, hi = CL.word_range(settings)
    n = CL.words(paragraphs)
    if not lo <= n <= hi:
        out.append(f"LETTER {n} words: must be between {lo} and {hi}")
    if letter_tex is not None:
        from . import tailor as T

        head = T.letter_header(ctx, [Keyword(**k) for k in rep.get("keywords") or []])
        if head["headline"] and render.hook_tex(head["headline"], head["number"], "num") not in letter_tex:
            out.append("LETTER header: the CV's hook headline is not under the name")
        if render.contact_line(ctx.get("id") or {}) not in letter_tex:
            out.append("LETTER header: the CV's contact line is not under the name")
    body = [parts[n] for n in ("P3", "P4", "P4b", "P5") if n in parts]
    cv = CL.cv_from_ctx(ctx)
    out += [f"LETTER P3/P4 draw on bullet {e['id']}, which is not printed on this job's CV" for e in letter.get("evidence") or [] if e.get("id") not in cv["ids"]]
    tags = [t for t in letter.get("cites") or []]
    out += CL.balance_problems(tags) if tags else []
    out += CL.hardware_problems(parts, " ".join(t for t in texts if t))
    if fb is not None:
        out += CL.skill_problems(body, dict(fb), cv["text"])
        out += OP.problems(parts.get("P1", ""), dict(fb), [t for t in texts if t])
    reuse_body = [parts[n] for n in CL.REUSE_NAMES if n in parts]
    score, who = RU.worst(reuse_body, earlier)
    if who is not None and score >= RU.LIMIT:
        out.append(f"reuse: the body is {score:.2f} like the letter '{who.name}' (limit {RU.LIMIT:.2f})")
    return [p if p.startswith("LETTER") else f"LETTER {p}" for p in out]


def template_letter_problems(rep: Mapping[str, Any], letter: Mapping[str, Any], parts: dict[str, str], settings, letter_tex: str | None,
                             fb: Mapping[str, Any] | None, texts: Sequence[str]) -> list[str]:
    """A template letter (tailoring/template_letter.py): 200 to 320 body words, the CV's headline and contact line in its header, no memory / GB figure
    for an ad that is not about hardware, and the fact check on the whole letter run again on the letter as it now stands."""
    from . import template_letter as TL

    paragraphs = list(parts.values())
    out: list[str] = []
    lo, hi = CL.word_range(settings)
    n = CL.words(paragraphs)
    if not lo <= n <= hi:
        out.append(f"LETTER {n} words: must be between {lo} and {hi}")
    ctx = rep["context"]
    if letter_tex is not None:
        from . import tailor as T

        head = T.letter_header(ctx, [Keyword(**k) for k in rep.get("keywords") or []])
        if head["headline"] and render.hook_tex(head["headline"], head["number"], "num") not in letter_tex:
            out.append("LETTER header: the CV's headline is not under the name")
        if render.contact_line(ctx.get("id") or {}) not in letter_tex:
            out.append("LETTER header: the CV's contact line is not under the name")
    out += CL.hardware_problems(parts, " ".join(t for t in texts if t))
    if fb is not None:
        text = " ".join(paragraphs)
        slots = {k: v for k, v in (letter.get("slots") or {}).items() if v and v in text}
        out += TL.fact_problems(paragraphs, slots, dict(fb), [Keyword(**k) for k in rep.get("keywords") or []], " ".join(t for t in texts if t),
                                str(rep.get("company") or ""), str(rep.get("title") or ""))
    return [p if p.startswith("LETTER") else f"LETTER {p}" for p in out]


# ------------------------------------------------------------------------------------------------------------------------ the check
def check(rep: Mapping[str, Any], settings, *, tex: str | None = None, cv_text: str | None = None, letter_tex: str | None = None,
          fb: Mapping[str, Any] | None = None, texts: Sequence[str] = (), earlier: Sequence[RU.Earlier] = ()) -> DocCheck:
    """Every rule on one tailored job. `rep` is the job's report.json; `tex` the CV's final .tex and `letter_tex` the letter's (None: that part of
    the check is skipped); `fb` the fact bank; `texts` = the job ad and the company's careers page (what the letter's opening may quote); `earlier`
    the earlier letters (letter_reuse.earlier_letters) the body must not copy."""
    failures: list[str] = hook_problems(rep)
    if not rep.get("one_page"):
        failures += profile_problems(rep, settings)
        failures += skill_problems(rep)
        failures += project_problems(rep, tex, settings)
        failures += cert_problems(rep, tex)
    failures += context_problems(rep, fb)
    failures += letter_problems(rep, settings, letter_tex, fb, texts, earlier)
    return DocCheck(list(dict.fromkeys(failures)))


def for_report(rep: Mapping[str, Any], settings, fb: Mapping[str, Any] | None = None, job: Mapping[str, Any] | None = None) -> DocCheck:
    """The check of a tailored job, read from its files: the final .tex of the CV and of the letter beside their PDFs, the job's stored company page,
    and the earlier letters of the outputs and archive folders. `job` (the database row) supplies the ad the opening is checked against."""
    from . import companypage

    def tex_beside(key: str) -> str | None:
        pdf = Path(rep[key]) if rep.get(key) else None
        path = pdf.with_suffix(".tex") if pdf else None
        return path.read_text(encoding="utf-8") if path is not None and path.is_file() else None

    jd = ""
    if job is not None:
        try:
            jd = str(job["description"] or "")
        except (KeyError, IndexError):
            jd = ""
    folder = Path(rep["out_dir"]) if rep.get("out_dir") else None
    page = companypage.stored(folder) if folder is not None else ""
    letter_ok = (rep.get("letter") or {}).get("status") == "ok"
    earlier = RU.earlier_letters(settings, exclude=[attempts_job_name(folder)]) if letter_ok and folder is not None else []
    return check(rep, settings, tex=tex_beside("cv_pdf"), letter_tex=tex_beside("letter_pdf"), fb=fb, texts=(jd, page), earlier=earlier)
