"""LaTeX rendering (xelatex + Carlito) and post-compile checks: page count and extracted text."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2

from ..config import ROOT
from . import layout, typography


_ESC = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "\\": r"\textbackslash{}",
        # straight apostrophe: xelatex otherwise emits U+2019, which naive ATS string-matching misses
        # ("Master's" ≠ "Master’s"). \textquotesingle keeps it a plain ASCII quote in the PDF text layer.
        "'": r"\textquotesingle{}"}


def latex_escape(s) -> str:
    return "".join(_ESC.get(ch, ch) for ch in str(s))


def skill_display(skill) -> str:
    """One skill entry as the CV shows it. An entry that lists several things ("S3, SQS, DynamoDB (MSc coursework)") would
    read as three skills among the comma-separated ones, so its commas outside brackets become "/": "S3/SQS/DynamoDB (MSc
    coursework)". Commas inside brackets stay."""
    s, depth, out, i = str(skill).strip(), 0, [], 0
    while i < len(s):
        ch = s[i]
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            while out and out[-1] == " ":
                out.pop()
            out.append("/")
            while i + 1 < len(s) and s[i + 1] == " ":
                i += 1                                   # no space after the slash
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _tag_shown(project: dict, key: str) -> str:
    """project[key] to print in the right-hand tag: "" when it has none, or when its text is already in the project's name."""
    tag = " ".join(str(project.get(key) or "").split())
    if not tag or tag.casefold() in " ".join(str(project.get("name") or "").split()).casefold():
        return ""
    return tag


def url_shown(project: dict) -> str:
    """The URL a project prints: its `github` unless the fact bank says `public: false` (a missing `public` counts as public; `careerops doctor`
    warns about it). "" = no URL."""
    return "" if project.get("public") is False else str(project.get("github") or "")


def with_public_links(ctx: dict) -> dict:
    """A copy of a render context in which a project the bank marks `public: false` has no `github`: nothing prints its URL. `ctx` is unchanged."""
    if not any(p.get("github") and url_shown(p) == "" for p in ctx.get("projects") or []):
        return ctx
    return {**ctx, "projects": [{**p, "github": url_shown(p)} if p.get("github") else p for p in ctx.get("projects") or []]}


def award_shown(project: dict) -> str:
    """The award to print beside a project: "" when it has none, or when its text is already in the project's name
    ("Lloyd's Banking Group Datathon — 3rd place" + award "3rd place"), so it is printed once."""
    return _tag_shown(project, "award")


def badge_shown(project: dict) -> str:
    """A project's own `badge:` label ("Open source", "Client work"), printed in the same tag as an award and, like it, once."""
    return _tag_shown(project, "badge")


def tag_shown(project: dict) -> str:
    """The text for the right-hand tag of a project: its award, else its badge. "" = none (a link or a coursework label may follow)."""
    return award_shown(project) or badge_shown(project)


_MONTHS = {m.lower(): m[:3] for m in ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                                      "October", "November", "December")}
_MONTHS.update({"sept": "Sep"})
_RANGE = re.compile(r"\s*(?:[-–—]+|\bto\b)\s*", re.I)
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")


def _date_part(part: str) -> str:
    part = " ".join(part.split())
    if part.lower() in ("present", "now", "current", "ongoing"):
        return "Present"
    m = _MONTH_YEAR.match(part)
    if m:
        mon = _MONTHS.get(m.group(1).lower()) or _MONTHS.get(m.group(1).lower()[:3])
        if mon:
            return f"{mon} {m.group(2)}"
    return part


DATE_SEPARATOR = " – "      # settings cv.date_separator: the default (an en dash with spaces); " to " is the other choice


def dates_display(text, sep: str = DATE_SEPARATOR) -> str:
    """A date range as the CV prints it: "Mon YYYY<sep>Mon YYYY" (`sep` is cv.date_separator: " – " or " to "; three-letter
    month, "Present" for an open end), whatever separator and month spelling the fact bank uses. A part without a month
    ("2025") is printed as it is: a month is never invented."""
    raw = " ".join(str(text or "").split())
    parts = [x for x in _RANGE.split(raw, maxsplit=1) if x]
    if len(parts) == 2:
        return f"{_date_part(parts[0])}{sep}{_date_part(parts[1])}"
    return _date_part(raw)


_SELF = re.compile(r"^(?:self[- ]?employed|freelance[r]?)\b[\s,–—-]*", re.I)


def is_freelance(entry: dict) -> bool:
    """`employment_type: freelance` in the fact bank, or (the bank predates the field) an org that begins "Self-employed" or
    "Freelance"."""
    kind = str(entry.get("employment_type") or "").strip().lower()
    return kind == "freelance" or (not kind and bool(_SELF.match(str(entry.get("org") or "").strip())))


def org_parts(entry: dict) -> tuple[str, str]:
    """(name, location) of an experience heading: the name is bold, the location italic. A freelance entry is ("Freelance",
    location): the word Freelance replaces the org ("Self-employed" is never printed) and the location stays on the same line."""
    org, loc = str(entry.get("org") or "").strip(), str(entry.get("location") or "").strip()
    if is_freelance(entry):
        # "Self-employed, Chennai" -> "Chennai", for a bank without `location`; any other org name is not printed
        rest = _SELF.sub("", org).strip(" ,") if _SELF.match(org) else ""
        return "Freelance", loc or rest
    if loc and loc.casefold() not in org.casefold():
        return org, loc
    return org, ""


def org_line(entry: dict) -> str:
    """The left side of an experience heading as text: "Org, Location"."""
    return ", ".join(x for x in org_parts(entry) if x)


def role_line(entry: dict) -> str:
    """The job title under the employer. A freelance entry drops a leading "Freelance " ("Freelance AI/ML Engineer" under
    "Freelance" reads "AI/ML Engineer")."""
    title = str(entry.get("title") or "").strip()
    if is_freelance(entry):
        short = re.sub(r"^freelance[r]?\s+", "", title, flags=re.I)
        return short or title
    return title


def school_parts(ed: dict) -> tuple[str, str]:
    org, loc = str(ed.get("org") or "").strip(), str(ed.get("location") or "").strip()
    return (org, loc) if loc and loc.casefold() not in org.casefold() else (org, "")


def school_line(ed: dict) -> str:
    return ", ".join(x for x in school_parts(ed) if x)


def degree_parts(ed: dict) -> tuple[str, str]:
    """(degree, the rest) of an education entry's second line: the degree name is italic, "(module, module, ...), grade or note;
    details" is not."""
    degree = str(ed.get("degree") or "").strip()
    rest = ""
    mods = [str(m).strip() for m in ed.get("modules") or [] if str(m).strip()]
    if mods:
        rest += f" ({', '.join(mods)})"
    extra = [str(ed.get(k)).strip() for k in ("grade", "note", "details") if ed.get(k)]
    if extra:
        rest += ("; " if mods else ", ") + "; ".join(extra)
    return degree, rest


def degree_line(ed: dict) -> str:
    """"Degree (module, module, ...), grade or note; details": the second line of an education entry."""
    return "".join(degree_parts(ed))


_COURSEWORK_TAG = re.compile(r"\s*\(([^()]*\bcoursework)\)\s*$", re.I)


def project_parts(project: dict) -> tuple[str, str]:
    """(name, badge) of a project heading. The badge is its award, else its `badge:` (each unless the name already says it) or, for a
    project that has no link to show instead, the "(MSc coursework)" its name ends with, moved out of the name: "MSc Coursework".
    "" = no badge."""
    name = " ".join(str(project.get("name") or "").split())
    tag = tag_shown(project)
    if tag:
        return name, tag
    m = _COURSEWORK_TAG.search(name)
    if m and not project.get("github"):
        return name[:m.start()].rstrip(), re.sub("coursework", "Coursework", m.group(1), flags=re.I)
    return name, ""


def education_lines(ed: dict) -> tuple[str, str]:
    """(title, modules) of an education entry under its school: the title is the degree with its grade and note ("B.E. EEE,
    First Class"); the muted line below it is the modules, then "; details". "" = no such line."""
    title = ", ".join(str(x).strip() for x in (ed.get("degree"), ed.get("grade"), ed.get("note")) if x and str(x).strip())
    mods = ", ".join(str(m).strip() for m in ed.get("modules") or [] if str(m).strip())
    details = str(ed.get("details") or "").strip()
    return title, "; ".join(x for x in (mods, details) if x)


def publication_parts(pub) -> tuple[str, str, str]:
    """(title, venue, date) of a publication: a plain string is all title, {title, venue, date} is the three. The venue is italic."""
    if not isinstance(pub, dict):
        return str(pub), "", ""
    return (str(pub.get("title") or "").strip().rstrip("."), str(pub.get("venue") or "").strip(),
            ", ".join(str(pub[k]).strip() for k in ("date", "year") if pub.get(k)))


def publication_line(pub) -> str:
    """A publication as one line: a plain string as written, or {title, venue, date} as "Title. Venue, date"."""
    if not isinstance(pub, dict):
        return str(pub)
    head, venue, date = publication_parts(pub)
    tail = ", ".join(x for x in (venue, date) if x)
    return f"{head}. {tail}" if tail else head


def language_line(lang) -> str:
    """A language as written, or {name, level} (the bank also writes {language, level}) as "Name (level)"."""
    if not isinstance(lang, dict):
        return str(lang)
    name, level = str(lang.get("name") or lang.get("language") or "").strip(), str(lang.get("level") or "").strip()
    return f"{name} ({level})" if level else name


# Text the page prints in italics wherever it appears: "(Expected)", "(coursework)", "(MSc coursework)".
_ITALIC_MARK = re.compile(r"\((?:expected\b[^()]*|[^()]*\bcoursework)\)", re.I)


def inline(text) -> str:
    """Escaped LaTeX with the italic markers ("(Expected)", "(coursework)") in italics."""
    s, out, pos = str(text), [], 0
    for m in _ITALIC_MARK.finditer(s):
        out += [latex_escape(s[pos:m.start()]), r"\textit{" + latex_escape(m.group(0)) + "}"]
        pos = m.end()
    out.append(latex_escape(s[pos:]))
    return "".join(out)


def _tok(word: str) -> str:
    return word.lower().strip(".,;:()[]\"'")


MAX_SKIPPED = 2           # words of a bullet that may sit between two words of its `result:` (a bullet says "14 heterogeneous source datasets")


def result_span(text: str, result) -> tuple[int, int] | None:
    """Where a bullet says its `result:` (the measured outcome in the fact bank): the span of `text` from the first word of the
    result to the last, the words in the same order with at most MAX_SKIPPED other words between two of them. None when the
    bullet does not say it."""
    want = [t for t in (_tok(w) for w in str(result or "").split()) if t]
    if not want:
        return None
    words = [(m.start(), m.end(), _tok(m.group())) for m in re.finditer(r"\S+", text)]
    for i, (_, _, w) in enumerate(words):
        if w != want[0]:
            continue
        last, ok = i, True
        for t in want[1:]:
            nxt = next((k for k in range(last + 1, min(last + 2 + MAX_SKIPPED, len(words))) if words[k][2] == t), None)
            if nxt is None:
                ok = False
                break
            last = nxt
        if ok:
            start, end = words[i][0], words[last][1]
            return start, start + len(text[start:end].rstrip(".,;:"))
    return None


def bullet_latex(text, result="", wrap: str = "textbf") -> str:
    """A bullet as LaTeX: escaped, with the bullet's `result:` text inside \\<wrap>{...} when the bullet says it (nothing else in
    a bullet is wrapped). `wrap`: textbf for the one-page template, num (the reference CV's macro for result numbers) for cv2."""
    s = str(text)
    span = result_span(s, result)
    if not span:
        return inline(s)
    return inline(s[:span[0]]) + "\\" + wrap + "{" + inline(s[span[0]:span[1]]) + "}" + inline(s[span[1]:])


def hook_tex(text, number="", wrap: str = "textbf") -> str:
    """The CV headline (hook.py) escaped for LaTeX, with its number (verbatim) in bold: the one figure a skim lands on."""
    t, n = str(text or ""), str(number or "")
    if n and n in t:
        a, b = t.split(n, 1)
        return latex_escape(a) + "\\" + wrap + "{" + latex_escape(n) + "}" + latex_escape(b)
    return latex_escape(t)


def bullet_at(entry: dict, i: int, results: dict | None = None, wrap: str = "textbf") -> str:
    """The i-th bullet of an experience or project entry as LaTeX, its `result:` (results[bullet id]) inside \\<wrap>{...}."""
    ids = entry.get("bullet_ids") or []
    return bullet_latex(entry["bullets"][i], (results or {}).get(ids[i], "") if i < len(ids) else "", wrap)


def org_tex(entry: dict) -> str:
    """The employer in bold, its location in italics."""
    name, loc = org_parts(entry)
    return r"\textbf{" + inline(name) + "}" + (r", \textit{" + inline(loc) + "}" if loc else "")


def school_tex(ed: dict) -> str:
    name, loc = school_parts(ed)
    return r"\textbf{" + inline(name) + "}" + (r", \textit{" + inline(loc) + "}" if loc else "")


def role_tex(entry: dict) -> str:
    return r"\textit{" + inline(role_line(entry)) + "}"


def degree_tex(ed: dict) -> str:
    degree, rest = degree_parts(ed)
    return r"\textit{" + inline(degree) + "}" + inline(rest)


def publication_tex(pub) -> str:
    title, venue, date = publication_parts(pub)
    if not venue and not date:
        return inline(title)
    tail = ", ".join(x for x in ((r"\textit{" + inline(venue) + "}") if venue else "", inline(date) if date else "") if x)
    return f"{inline(title)}. {tail}"


_MONTH_WORDS = "|".join(sorted(set(_MONTHS) | {m.lower() for m in _MONTHS.values()}, key=len, reverse=True))   # "march", "mar", "sept"
_CERT_YEAR = re.compile(r"(?:,\s*|\s+)((?:(?:" + _MONTH_WORDS + r")\.?\s+)?(?:19|20)\d{2})$", re.I)


def cert_tex(cert) -> str:
    """A certification: the name, its issuer, then the date in italics ("Jun 2026"). A plain string that ends in a date
    ("AWS Academy Cloud Foundations, Nov 2025") has it split off the name."""
    c = typography.parse_cert(cert)
    name, issuer, when = c["name"], c["issuer"], c["rest"].strip(" ,")
    if not when and (m := _CERT_YEAR.search(name)):
        name, when = name[:m.start()].rstrip(" ,"), m.group(1)
    return ", ".join(x for x in (inline(name), inline(issuer), (r"\textit{" + inline(when) + "}") if when else "") if x)


def achievement_head(a: dict) -> str:
    """The bold lead of an achievement line: the project's name, then its award unless the name already says it ("Lloyd's Banking
    Group Datathon, 3rd place"). Escaped LaTeX."""
    name = " ".join(str(a.get("name") or "").split())
    award = award_shown({"name": name, "award": a.get("award")})
    return inline(name) + (", " + inline(award) if award else "")


def skill_row(items) -> str:
    """One skills row: every entry as `skill_display` shows it, the first with a capital letter. A row that is one line of text (a role
    family's `skill_lines` item, as the user wrote it) is printed as it is: its commas are not turned into slashes."""
    if isinstance(items, str):
        return inline(items)
    shown = [skill_display(s) for s in items]
    if shown and shown[0][:1].islower():
        shown[0] = shown[0][0].upper() + shown[0][1:]
    return ", ".join(inline(s) for s in shown)


DOT = "\\nobreak\\ \\,\\textperiodcentered\\, "          # the separator of the contact line and the status line


def contact_line(identity: dict, dot: str = DOT) -> str:
    """The CV's contact line as LaTeX, and the cover letter's (Document rules, B2: one header for both): location, e-mail, phone, then the
    LinkedIn, GitHub and portfolio links the fact bank has, joined by `dot`."""
    parts = []
    if identity.get("location"):
        parts.append(latex_escape(identity["location"]))
    parts.append(r"\href{mailto:" + str(identity["email"]) + "}{" + latex_escape(identity["email"]) + "}")
    if identity.get("phone"):
        parts.append(r"\href{tel:" + str(identity["phone"]).replace(" ", "") + "}{" + latex_escape(identity["phone"]) + "}")
    links = identity.get("links") or {}
    for k in ("linkedin", "github", "portfolio"):
        if links.get(k):
            shown = "Portfolio" if k == "portfolio" else latex_escape(re.sub(r"^https?://(www\.)?", "", str(links[k])).rstrip("/"))
            parts.append(r"\href{" + str(links[k]).replace("%", r"\%").replace("#", r"\#") + "}{" + shown + "}")
    return dot.join(parts)


def env(date_separator: str = DATE_SEPARATOR) -> jinja2.Environment:
    e = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "templates"),
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True, autoescape=False, undefined=jinja2.StrictUndefined,
    )
    e.filters["e"] = latex_escape
    e.filters["inline"] = inline                                         # escaped, with (Expected) / (coursework) in italics
    e.filters["award"] = tag_shown                                       # a project's award or badge, unless its name already says it
    e.filters["skill"] = lambda s: latex_escape(skill_display(s))        # a skill entry with commas shows them as "/"
    e.filters["dates"] = lambda t: dates_display(t, date_separator)
    e.filters.update(org_line=org_line, role_line=role_line, school_line=school_line, degree_line=degree_line,
                     publication_line=publication_line, language_line=language_line,
                     bullet=bullet_latex, bullet_at=bullet_at, hook_tex=hook_tex,           # bullet(result): escaped, the bullet's `result:` in bold
                     org_parts=org_parts, school_parts=school_parts, project_parts=project_parts,
                     education_lines=education_lines, org_tex=org_tex, school_tex=school_tex, role_tex=role_tex, degree_tex=degree_tex,
                     publication_tex=publication_tex, cert_tex=cert_tex, skill_row=skill_row, achievement_head=achievement_head,
                     cert_text=typography.cert_text, contact_line=contact_line)
    # a URL inside \href{...}: hyperref needs % and # escaped
    e.filters["urlesc"] = lambda u: str(u).replace("%", r"\%").replace("#", r"\#")
    # what a reader sees for a link: no scheme, no "www.", no trailing slash
    e.filters["urlshow"] = lambda u: re.sub(r"^https?://(www\.)?", "", str(u)).rstrip("/")
    return e


class RenderError(RuntimeError):
    pass


def compile_tex(tex: str, out_dir: Path, stem: str) -> Path:
    if not shutil.which("xelatex"):
        raise RenderError("xelatex not found — install texlive-xetex (or set up tectonic and edit render.py)")
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")
    proc = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                          cwd=out_dir, capture_output=True, text=True, timeout=180)
    pdf = out_dir / f"{stem}.pdf"
    if proc.returncode != 0 or not pdf.exists():
        tail = "\n".join(proc.stdout.splitlines()[-25:])
        raise RenderError(f"xelatex failed for {tex_path}:\n{tail}")
    for ext in (".aux", ".log", ".out"):
        (out_dir / f"{stem}{ext}").unlink(missing_ok=True)
    return pdf


def page_count(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", out)
    return int(m.group(1)) if m else -1


def pdf_text(pdf: Path) -> str:
    """The text layer an ATS parser will see."""
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout


# The text area of templates/cv2.tex.j2 (geometry top=0.5in, bottom=0.5in on A4); change both together. The one-page
# template is never measured: its page count is all that matters.
CV2_MARGIN_PT = 0.5 * 72
_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_YMAX = re.compile(r'yMax="([\d.]+)"')


def last_line_fills(pdf: Path, margin_pt: float = CV2_MARGIN_PT) -> list[float]:
    """Per page: how far down the text area the last text line reaches, 0.0 (nothing below the top margin) to 1.0 (the
    bottom margin). Measured from the rendered PDF (pdftotext -bbox: y of the lowest word, in points), never from
    character counts. One entry per page that has any words; a blank page has no entry, so the list may be shorter than
    the page count."""
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True).stdout
    out = []
    for m in _PAGE.finditer(xml):
        height, body = float(m.group(2)), m.group(3)
        ys = [float(y) for y in _YMAX.findall(body)]
        if ys:
            area = height - 2 * margin_pt
            out.append(min(max((max(ys) - margin_pt) / area, 0.0), 1.0))
    return out


def page2_fill(pdf: Path, pages: int | None = None) -> float:
    """How full page 2 is (see `last_line_fills`): 0.0 for a one-page CV, 1.0 once there is a third page, so
    "fill >= min_fill_page2" is only ever true of a CV that really has two pages."""
    pages = page_count(pdf) if pages is None else pages
    if pages < 2:
        return 0.0
    if pages > 2:
        return 1.0
    fills = last_line_fills(pdf)
    return fills[1] if len(fills) > 1 else 0.0


def _finalise(out_dir: Path, stem: str, final_name: str | None) -> Path:
    """Rename <stem>.tex/.pdf to '<final_name>.tex/.pdf' inside out_dir (the working copy, never the archive).
    xelatex only ever sees the fixed stem, so a name with spaces never reaches it."""
    if not final_name:
        return out_dir / f"{stem}.pdf"
    for ext in (".tex", ".pdf"):
        os.replace(out_dir / f"{stem}{ext}", out_dir / f"{final_name}{ext}")
    return out_dir / f"{final_name}.pdf"


def render_cv(ctx: dict, out_dir: Path, final_name: str | None = None, one_page: bool = False,
              date_separator: str = DATE_SEPARATOR, keep_hyphens=()) -> Path:
    """Compile as cv.tex, then rename to `final_name` (e.g. 'Ada Lovelace CV') so the file a recruiter sees is
    already the final one. Two-page template (cv2.tex.j2) by default, the one-page cv.tex.j2 with `one_page`.
    The typography (typography.py: no em dash, hyphens closed up per config/typography.yaml, no award repeated in a bullet) is
    applied here, to a copy of `ctx`, so every check has already seen the original wording. `keep_hyphens`: hyphenated forms
    to leave alone (the JD keywords spelt with the hyphen). Returns the PDF path."""
    ctx = with_public_links(ctx)                      # a project marked `public: false` prints no URL
    if not one_page:                                  # the situation openers and the split bullets are the two-page CV's (layout.py)
        ctx = layout.with_openers(layout.with_splits(ctx))
    ctx = typography.typeset(ctx, keep=keep_hyphens)
    compile_tex(env(date_separator).get_template("cv.tex.j2" if one_page else "cv2.tex.j2").render(**ctx), out_dir, "cv")
    return _finalise(out_dir, "cv", final_name)


def render_letter(ctx: dict, out_dir: Path, final_name: str | None = None) -> Path:
    compile_tex(env().get_template("cover_letter.tex.j2").render(**ctx), out_dir, "letter")
    return _finalise(out_dir, "letter", final_name)
