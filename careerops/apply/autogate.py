"""The gate in front of every auto-submit. A job is submitted by `careerops live --auto` only when ALL of these pass:

  ATS            the strict CV score (the CV's own text, negation-aware) is at least apply.min_ats_strict, not just the ceiling
  format         exactly 2 pages; every font embedded; no two words overlap and none sits outside the margins (pdftotext -bbox); the
                 PDF's text layer holds the name, the e-mail and every section heading, in order
  faithfulness   the final CV has no claim-guard violation and no warning, and a letter that exists passed its own check. A rewrite the
                 guard BLOCKED never reached the CV: those are counted apart ("blocked rewrites", information) and do not gate
  level          L = 1.0: no manager, lead or senior word in the title
  sponsorship    the JD has no "no sponsorship" line
  kind           apply_kind is in apply.kinds
  duplicates     not applied to before: database, archive, AIApply export, this log, and the tracker Sheet
  daily cap      fewer than apply.max_auto_per_day auto-submits today
  doc check      the Document rules hold for the CV and the letter (tailoring/doccheck.py: hook, profile 100-150 words, no skill twice, at most 3
                 projects, certifications as a list, a 300-500 word letter with the CV's header, evidence and an opening that quotes the ad, no
                 reuse). A failure always blocks auto-submit; only a person can approve over it, by typing `override doc check` in review.
  ats structure  v3.3 (ats_structure.py): the CV's PDF text layer parses: no critical issue (no text layer, no Experience heading, no e-mail in the
                 text, scrambled reading order) and a structural score of at least 80; a letter PDF with a critical issue blocks too. A check that
                 cannot run (poppler missing, no PDF) blocks. The keyword band (strict >= min_ats_strict) is unchanged; lints are advisory.

Nothing here opens a browser or clicks anything. A failed gate sends the job to the normal review prompt, with `Gates.line()` printed first.
"""
from __future__ import annotations

import html
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

from ..discovery import applykind, discover, priority
from .. import ats_structure
from ..tailoring import doccheck
from ..tailoring.render import page_count, pdf_text
from . import autolog
from .archive import _own_role

MODES = ("off", "assist", "auto")
DEFAULT_KINDS = ("greenhouse", "ashby", "lever")
# The section headings of templates/cv2.tex.j2, in the order it prints them. The first five are always printed; the others when the bank has them.
HEADINGS = ["Profile", "Skills", "Work Experience", "Projects", "Education", "Research Focus", "Publications",
            "Certifications & Achievements", "Languages"]
SIDE_MARGIN_PT = 0.6 * 72            # templates/cv2.tex.j2: geometry margin=0.6in left and right, 0.5in top and bottom
TB_MARGIN_PT = 0.5 * 72
SLACK_PT = 3.0                       # an overfull box of a point or two is not a layout fault


class GateConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str
    min_ats_strict: float
    max_auto_per_day: int
    confirm_first_n: int
    kinds: tuple[str, ...]


def config(settings) -> Config:
    """settings.yaml -> apply, with the defaults of the table in README.md; a value of the wrong kind is refused with its name."""
    raw = (settings.raw.get("apply") or {}) if hasattr(settings, "raw") else {}
    mode = str(raw.get("mode", "assist")).strip().lower()
    if mode not in MODES:
        raise GateConfigError(f"apply.mode is {raw.get('mode')!r}; it must be one of {', '.join(MODES)}")

    def number(key: str, default: float, whole: bool = False) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (whole and int(v) != v):
            raise GateConfigError(f"apply.{key} is {v!r}; it must be a number of 0 or more" + (" (a whole number)" if whole else ""))
        return float(v)

    kinds = raw.get("kinds", list(DEFAULT_KINDS))
    if not isinstance(kinds, (list, tuple)) or not all(isinstance(k, str) for k in kinds):
        raise GateConfigError(f"apply.kinds is {kinds!r}; it must be a list such as [greenhouse, ashby, lever]")
    return Config(mode, number("min_ats_strict", 70.0), int(number("max_auto_per_day", 5, True)), int(number("confirm_first_n", 3, True)),
                  tuple(k.strip().lower() for k in kinds))


# ------------------------------------------------------------------ the result
@dataclass
class Gate:
    name: str
    ok: bool
    why: str = ""            # the failure in a few words; "" when it passed
    info: str = ""           # what the gate measured, shown whether it passed or not (live prints it)


@dataclass
class Gates:
    items: list[Gate] = field(default_factory=list)

    @property
    def failed(self) -> list[Gate]:
        return [g for g in self.items if not g.ok]

    @property
    def ok(self) -> bool:
        return not self.failed

    def line(self) -> str:
        """`AUTO BLOCKED: ATS 58 < 70; level: senior`, or '' when every gate passed."""
        return "AUTO BLOCKED: " + "; ".join(g.why for g in self.failed) if self.failed else ""


def _num(x: float) -> str:
    return f"{round(float(x), 1):g}"


# ------------------------------------------------------------------ the PDF
def expected_headings(ctx: Mapping[str, Any]) -> list[str]:
    """The headings the two-page template prints for this CV's context, in its order."""
    optional = {"Research Focus": ctx.get("research_focus"), "Publications": ctx.get("publications"),
                "Certifications & Achievements": ctx.get("certifications") or ctx.get("achievements"), "Languages": ctx.get("languages")}
    return [h for h in HEADINGS if h in HEADINGS[:5] or optional.get(h)]


def unembedded_fonts(pdffonts_output: str) -> list[str]:
    """Names of the fonts `pdffonts` lists with emb = no. A listing with no font at all is itself a fault."""
    out, listed = [], 0
    for line in pdffonts_output.splitlines():
        if line.startswith("name ") or set(line.strip()) <= {"-", " "}:
            continue
        m = re.search(r"\s(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)
        if not m:
            continue
        listed += 1
        if m.group(1) == "no":
            out.append(line.split()[0].split("+")[-1])
    return out if listed else ["(no fonts listed)"]


_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', re.S)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _geometry(pdf: Path) -> list[str]:
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True).stdout
    out: list[str] = []
    for n, (w, h, body) in enumerate(_PAGE.findall(xml), 1):
        width, height = float(w), float(h)
        words = [(float(a), float(b), float(c), float(d), html.unescape(t)) for a, b, c, d, t in _WORD.findall(body)]
        words.sort()
        edge = [t for x0, y0, x1, y1, t in words
                if x0 < SIDE_MARGIN_PT - SLACK_PT or x1 > width - SIDE_MARGIN_PT + SLACK_PT or y1 > height - TB_MARGIN_PT + SLACK_PT]
        if edge:
            out.append(f"page {n}: text outside the margins ({', '.join(repr(t) for t in edge[:3])})")
        clashes = []
        for i, a in enumerate(words):
            for b in words[i + 1:]:
                if b[0] >= a[2]:
                    break
                across, down = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
                if across > 1.0 and down > 0.4 * min(a[3] - a[1], b[3] - b[1]):
                    clashes.append(f"{a[4]!r} / {b[4]!r}")
        if clashes:
            out.append(f"page {n}: text overlap ({', '.join(clashes[:3])})")
    return out


def pdf_problems(pdf: Path | str | None, *, name: str, email: str, headings: list[str]) -> list[str]:
    """Everything wrong with a CV PDF for the format gate; [] = all clear. Measured on the rendered file, never on the source."""
    if not pdf or not Path(pdf).is_file():
        return [f"no CV PDF ({pdf or 'none recorded'})"]
    pdf = Path(pdf)
    problems: list[str] = []
    pages = page_count(pdf)
    if pages != 2:
        problems.append(f"{pages} page{'' if pages == 1 else 's'}, not 2")
    fonts = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout
    problems += [f"font {f} is not embedded" if not f.startswith("(") else f for f in unembedded_fonts(fonts)]
    problems += _geometry(pdf)
    text = pdf_text(pdf) or ""
    if name and _alnum(name) not in _alnum(text):
        problems.append(f"the name {name!r} is not in the PDF's text")
    if email and email.casefold().replace(" ", "") not in text.casefold().replace(" ", ""):
        problems.append(f"the email {email!r} is not in the PDF's text")
    lines = [_alnum(ln) for ln in text.splitlines()]
    at = -1
    for h in headings:
        want = _alnum(h)
        found = next((i for i in range(at + 1, len(lines)) if lines[i] == want), None)
        if found is not None:
            at = found
        elif want in lines:
            problems.append(f"heading {h!r} is out of order")
        else:
            problems.append(f"heading {h!r} is not in the PDF's text")
    return problems


# ------------------------------------------------------------------ the gates
def _ats(rep: Mapping[str, Any], cfg: Config) -> Gate:
    strict = (rep.get("ats") or {}).get("strict")
    if strict is None:
        return Gate("ATS", False, f"ATS not scored (needs {_num(cfg.min_ats_strict)})")
    ok = float(strict) >= cfg.min_ats_strict
    return Gate("ATS", ok, "" if ok else f"ATS {_num(strict)} < {_num(cfg.min_ats_strict)}")


def _format(rep: Mapping[str, Any], fb: Mapping[str, Any]) -> Gate:
    ctx = rep.get("context") or {}
    ident = {**(ctx.get("id") or {}), **(fb.get("identity") or {})}
    problems = pdf_problems(rep.get("cv_pdf"), name=str(ident.get("name") or ""), email=str(ident.get("email") or ""),
                            headings=expected_headings(ctx))
    return Gate("format", not problems, "format: " + "; ".join(problems) if problems else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _final_bullets(rep: Mapping[str, Any]) -> dict[str, str]:
    """{bullet id: the text it has on the final CV}, from the CV's own context."""
    ctx, out = rep.get("context") or {}, {}
    for entry in [*(ctx.get("experience") or []), *(ctx.get("projects") or [])]:
        out.update(zip(entry.get("bullet_ids") or [], entry.get("bullets") or []))
    return out


def split_violations(rep: Mapping[str, Any], rec: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(blocked, in the final CV): a violation is "<bullet id>: <problem>" for a rewrite the model proposed. When that rewrite is not what
    the CV says now, the guard blocked it and the fact-bank wording stands (information). Every other violation, and one of a rewrite
    that did reach the CV, is an issue in the final CV."""
    proposed = (rec.get("proposal") or {}).get("rephrase") or {}
    final = _final_bullets(rep)
    blocked, issues = [], []
    for v in rec.get("violations") or []:
        bid = str(v).split(":", 1)[0].strip()
        (blocked if bid in proposed and final.get(bid) != proposed[bid] else issues).append(v)
    return blocked, issues


def _faithfulness(rep: Mapping[str, Any]) -> Gate:
    attempts = rep.get("attempts") or []
    chosen = (rep.get("final") or {}).get("chosen_attempt")
    rec = next((a for a in attempts if a.get("attempt") == chosen), None)
    if rec is None and chosen and 0 < chosen <= len(attempts):
        rec = attempts[chosen - 1]
    if rec is None:
        return Gate("faithfulness", False, "faithfulness: no recorded attempt to check")
    blocked, issues = split_violations(rep, rec)
    info = f"faithfulness: {_plural(len(issues), 'issue')} in the final CV; {_plural(len(blocked), 'blocked rewrite')} (the guard kept the fact-bank wording)"
    bad = []
    if issues:
        bad.append(_plural(len(issues), "claim-guard violation") + " in the final CV")
    if rec.get("warnings"):
        bad.append(_plural(len(rec["warnings"]), "claim-guard warning"))
    letter = rep.get("letter") or {}
    if letter.get("status") in ("fallback", "pending") or (letter.get("status") == "ok" and letter.get("problems")):
        bad.append(f"letter: {letter.get('status')}" + (" with problems" if letter.get("problems") else ""))
    return Gate("faithfulness", not bad, "faithfulness: " + ", ".join(bad) if bad else "", info)


def _level(job: Mapping[str, Any], settings) -> Gate:
    factor, level = priority.level_factor(str(job.get("title") or ""), settings)
    ok = level == "other" and factor == 1.0
    return Gate("level", ok, "" if ok else f"level: {level}")


def _sponsorship(job: Mapping[str, Any]) -> Gate:
    no = priority.says_no_sponsorship(str(job.get("description") or ""))
    return Gate("sponsorship", not no, "sponsorship: the JD says no sponsorship" if no else "")


def _kind(job: Mapping[str, Any], cfg: Config) -> Gate:
    url = job.get("apply_url") or job.get("url")
    kind = str(job.get("apply_kind") or "") or applykind.kind_of(url)
    if not kind:
        return Gate("kind", False, "kind: no application page")
    ok = kind in cfg.kinds
    return Gate("kind", ok, "" if ok else f"kind: {kind} (auto-submit is for {'/'.join(cfg.kinds)})")


APPLIED_STATUSES = ("applied", "applied (archive incomplete)", "submit_unconfirmed")


def duplicate_reason(db, settings, job: Mapping[str, Any], log: list[dict[str, str]], tracker: Callable[[], set[str]] | None,
                     submitting: bool) -> str | None:
    """Why this job counts as applied already, or None. Every check only reads."""
    jid = str(job["id"])
    if job.get("status") in APPLIED_STATUSES or job.get("applied_at"):
        return "already applied (database)"
    row = db.job(jid)
    if row is not None and (row["status"] in APPLIED_STATUSES or row["applied_at"]):
        return "already applied (database)"
    if db.conn.execute("SELECT 1 FROM outcomes WHERE job_id=? AND event='applied'", (jid,)).fetchone():
        return "already applied (outcome log)"
    if _own_role(db, settings.path("applications_auto"), jid):
        return "already applied (archive)"
    if jid in autolog.clicked_ids(log):
        return "already auto-submitted (data/live/applied_auto.csv)"
    known = discover.Known()
    for r in db.conn.execute("SELECT id, company, title, location FROM jobs WHERE id<>? AND (status IN (?,?,?) OR applied_at IS NOT NULL)",
                             (jid, *APPLIED_STATUSES)):
        known.add(r["company"], r["title"], r["location"] or "", f"already applied (database: {r['id']})")
    for company, role in discover.archive_pairs(settings.archive_roots()):
        known.add(company, role, "", f"already applied (archive: {company}/{role})")
    discover.add_aiapply(settings, known)
    where = known.where(str(job.get("company") or ""), str(job.get("title") or ""), str(job.get("location") or ""))
    if where:
        return where
    if tracker is not None:
        try:
            if jid in tracker():
                return "already applied (tracker)"
        except Exception as e:  # noqa: BLE001  (no Google sign-in, no network)
            if submitting:
                return f"the tracker could not be read ({e}), so a duplicate cannot be ruled out"
    return None


def _duplicates(db, settings, job, log, tracker, submitting) -> Gate:
    why = duplicate_reason(db, settings, job, log, tracker, submitting)
    return Gate("duplicates", why is None, f"duplicate: {why}" if why else "")


def _cap(cfg: Config, log: list[dict[str, str]], today: date) -> Gate:
    used = autolog.clicked_on(log, today)
    ok = used < cfg.max_auto_per_day
    return Gate("daily cap", ok, "" if ok else f"daily cap: {used} of {cfg.max_auto_per_day} auto-submits used today")


def _doc_check(rep: Mapping[str, Any], settings, fb: Mapping[str, Any], job: Mapping[str, Any]) -> Gate:
    """The document check (tailoring/doccheck.py). A check that cannot run is a failure: nothing is submitted on a CV nobody could check."""
    try:
        found = doccheck.for_report(rep, settings, fb, job)
    except Exception as e:  # noqa: BLE001
        found = doccheck.DocCheck([f"the check could not run ({type(e).__name__}: {str(e)[:80]})"])
    if found.ok:
        return Gate("doc check", True, "", "DOC CHECK: pass")
    return Gate("doc check", False, f"doc check: {len(found.failures)} failure(s), first: {found.failures[0][:70]}", found.text())


def _ats_structure(rep: Mapping[str, Any], fb: Mapping[str, Any]) -> Gate:
    """v3.3: the structural ATS check of the CV (and the letter) PDF. A check that cannot run is a failure."""
    try:
        st = ats_structure.for_report(rep, fb)
    except Exception as e:  # noqa: BLE001
        st = ats_structure.JobStructure(None, None, [], error=f"{type(e).__name__}: {str(e)[:80]}")
    info = st.cv.line() if st.cv is not None else ""
    return Gate("ats structure", st.ok, "" if st.ok else st.why_not(), info)


def evaluate(db, settings, job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], *, today: date | None = None,
             tracker: Callable[[], set[str]] | None = None, submitting: bool = True) -> Gates:
    """Every gate for one tailored job, in the order of the table above. `tracker` returns the job ids in the tracker Sheet (it may raise);
    `submitting=False` (a --no-submit run, which never clicks) lets an unreadable tracker pass, because the check only guards against a
    second submit."""
    cfg, job = config(settings), dict(job)
    log = autolog.rows(settings)
    return Gates([_ats(rep, cfg), _format(rep, fb), _faithfulness(rep), _level(job, settings), _sponsorship(job), _kind(job, cfg),
                  _duplicates(db, settings, job, log, tracker, submitting), _cap(cfg, log, today or date.today()),
                  _doc_check(rep, settings, fb, job), _ats_structure(rep, fb)])
