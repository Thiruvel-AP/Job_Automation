"""`careerops import-linkedin`: a LinkedIn profile saved as PDF (browser "Print to PDF" of your profile page, or the "Save to
PDF" item of the profile menu) -> a proposal in fact-bank schema and a list of every mismatch with the real fact bank.

Read-only towards the fact bank: it is opened to compare and never written. The proposal goes to its own file
(paths.fact_bank_proposed) and the user merges what they want by hand.

The PDF's text layer is messy (page headers and footers in the middle of an entry, two-column blocks, a lost bullet glyph),
so the parser is line-based and forgiving: it splits the text at the section headings (the first occurrence of each), finds
the entries by their date lines, and drops what it cannot place instead of guessing. LinkedIn prints only the first few
items of Projects, Licenses & certifications and Education ("Show all N ..."); a count in a heading that is larger than
what the PDF holds is reported as a warning in the diff: click "Show all" on LinkedIn before saving the PDF.

Nothing here is a model call; nothing is invented. Wording is LinkedIn's own, so every bullet needs the user's eye (and
`terms:` evidence, which the faithfulness check uses) before it goes into fact_bank.yaml.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml

HEADINGS = ("About", "Top skills", "Featured", "Activity", "Experience", "Education", "Licenses & certifications", "Projects",
            "Publications", "Courses", "Languages", "Volunteering", "Skills", "Recommendations", "Honors & awards",
            "Test scores", "Organizations", "Interests", "Analytics")
_HEADING = re.compile(r"^(" + "|".join(re.escape(h) for h in HEADINGS) + r")(?: \((\d+)\))?(?:\s{2,}.*)?$")
_SEP = re.compile(r"\s*[·•�]\s*")                 # LinkedIn's middle dot (a lost glyph arrives as U+FFFD)
_FURNITURE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}")   # the browser's page header: "10/11/26, 1:15 PM ..."
_URL_PREFIX = re.compile(r"^https?://(?:www\.)?linkedin\.com/\S*\s*")  # the page footer, sometimes glued to a real line
_PAGE_NO = re.compile(r"(?:^|\s{2,})\d{1,2}/\d{1,2}\s*$")
MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_SPAN = re.compile(rf"^((?:{MON} )?\d{{4}})\s*(?:-|–| · )\s*((?:{MON} )?\d{{4}}|Present)(?: · .*)?$")
_TYPES = (r"(?:Full-time|Part-time|Self[- ]employed|Freelance|Contract|Indirect Contract|Internship|Apprenticeship|Seasonal|"
          r"Temporary|Casual|Trainee)")
_ORG_TYPE = re.compile(rf"^(.+) · ({_TYPES})$", re.I)
_GROUP = re.compile(rf"^({_TYPES}) · \d+ (?:yrs?|mos?)\b", re.I)    # "Full-time · 1 yr 9 mos": a company with several roles
_SKILLS_LINE = re.compile(r"\band \+\d+ skills?$|^[^.]*\+\d+ skills?$")
_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")


class LinkedInError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ text -> sections
def extract_text(pdf: Path) -> str:
    """The PDF's text layer (pdftotext -layout)."""
    if not shutil.which("pdftotext"):
        raise LinkedInError("pdftotext not found — install poppler-utils (`careerops doctor` checks it)")
    proc = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise LinkedInError(f"pdftotext failed for {pdf}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def clean_lines(text: str) -> list[str]:
    """One stripped line per text line, page furniture removed, separators normalised to ' · '. Blank lines are kept (an
    empty string) because they separate a bullet from the next block."""
    out, after_header = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if _FURNITURE.match(line):
            after_header = True
            continue
        if after_header and line:
            after_header = False
            words = line.split()
            if len(words) >= 4 and sorted(len(w) for w in words)[len(words) // 2] <= 2:
                continue                      # the browser's page header printed over the first line of the page: debris
        line = _URL_PREFIX.sub("", line).strip()
        line = _PAGE_NO.sub("", line).strip()
        out.append(_SEP.sub(" · ", line) if line else "")
    return out


def split_sections(lines: list[str]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """({heading: its lines}, {heading: the count in its heading, e.g. "Projects (10)"}). The first line that is exactly a
    heading starts a section (a later "Education" line under Volunteering is only text); a section runs to the next heading."""
    starts: list[tuple[int, str]] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            starts.append((i, m.group(1)))
            if m.group(2):
                counts[m.group(1)] = int(m.group(2))
    sections = {}
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        sections[name] = lines[i + 1:end]
    return sections, counts


def _nonblank(lines: list[str], i: int, step: int) -> int | None:
    j = i + step
    while 0 <= j < len(lines):
        if lines[j]:
            return j
        j += step
    return None


def _join(parts: list[str]) -> str:
    """Wrapped lines back into one string; a word broken at a hyphen ("cross-" + "validation", "GTAI-" + "UserGroup")
    rejoins without the space. A hyphen that follows a space ("a - b") is a dash and keeps it."""
    out = ""
    for p in parts:
        if out[-2:-1].isalnum() and out.endswith("-") and p[:1].isalnum():
            out += p
        else:
            out = f"{out} {p}".strip()
    return out


def _bullets(lines: list[str]) -> list[str]:
    """The numbered bullets ("1. text", wrapped over lines and over blank lines) of an entry; free text without numbers is
    one bullet. A skills line ("Python, Pandas and +2 skills") ends the entry."""
    items: list[list[str]] = []
    for line in lines:
        if _SKILLS_LINE.search(line):
            break
        m = _NUMBERED.match(line)
        if m:
            items.append([m.group(2)])
        elif line and items:
            items[-1].append(line)
        elif line:
            items.append([line])
    return [t for t in (_join(i) for i in items) if t]


def _span(text: str) -> str:
    """"Jan 2025 - Aug 2025" (with LinkedIn's separator in any form) as "Jan 2025 – Aug 2025"."""
    m = _SPAN.match(text.strip())
    return f"{m.group(1)} – {m.group(2)}" if m else text.strip()


# ------------------------------------------------------------------------------------------------------ experience
def parse_experience(lines: list[str]) -> list[dict]:
    """Entries {title, org, employment_type, dates, location, bullets}. Two layouts: a stand-alone role is
    "Title / Org · Type / dates / location · mode"; a company with several roles is "Company / Type · duration / location"
    and then, per role, "Title / dates" (the roles share the company and its location)."""
    dates = [i for i, ln in enumerate(lines) if _SPAN.match(ln)]
    groups = {}                                          # line index of "Type · duration" -> (company, location)
    for i, ln in enumerate(lines):
        if _GROUP.match(ln):
            c, loc = _nonblank(lines, i, -1), _nonblank(lines, i, +1)
            if c is not None:
                loc_ln = lines[loc] if loc is not None and not _NUMBERED.match(lines[loc]) and not _SPAN.match(lines[loc]) else ""
                groups[i] = (lines[c], _SEP.split(loc_ln)[0].strip() if loc_ln else "", _GROUP.match(ln).group(1), c)
    entries, starts, bodies = [], [], []
    company: tuple[str, str, str] | None = None
    for k, d in enumerate(dates):
        p1 = _nonblank(lines, d, -1)
        if p1 is None:
            continue
        gi = max((g for g in groups if g < d and (k == 0 or g > dates[k - 1])), default=None)
        if gi is not None:                                # the first role of a company: the header sits between the dates
            company = groups[gi][:3]
        m = _ORG_TYPE.match(lines[p1])
        if m and not _GROUP.match(lines[p1]):             # stand-alone role
            company = None
            t = _nonblank(lines, p1, -1)
            title, org, etype, loc = (lines[t] if t is not None else ""), m.group(1).strip(), m.group(2).strip(), ""
            nxt = _nonblank(lines, d, +1)
            if nxt is not None and not _NUMBERED.match(lines[nxt]) and not _SKILLS_LINE.search(lines[nxt]):
                loc, body_from = _SEP.split(lines[nxt])[0].strip(), nxt + 1
            else:
                body_from = d + 1
            start = t if t is not None else p1
        elif company is not None:                         # a role inside a company
            title, org, etype = lines[p1], company[0], company[2]
            loc = company[1]
            body_from = d + 1
            start = groups[gi][3] if gi is not None else p1
        else:                                             # a role whose type LinkedIn left out
            t = _nonblank(lines, p1, -1)
            title, org, etype, loc = (lines[t] if t is not None else ""), lines[p1], "", ""
            body_from, start = d + 1, (t if t is not None else p1)
        entries.append({"title": title.strip(), "org": org.strip(), "employment_type": etype.strip().lower(),
                        "dates": _span(lines[d]), "location": loc})
        starts.append(start)
        bodies.append(body_from)
    for n, e in enumerate(entries):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        e["bullets"] = _bullets(lines[bodies[n]:max(end, bodies[n])])
    return entries


# ----------------------------------------------------------------------------------------------------------- education
def parse_education(lines: list[str]) -> list[dict]:
    """Entries {org, degree, dates, grade, activities, modules}. An entry ends at its skills line ("... and +23 skills"); the
    first line is the school, the second "Degree, Field", the third the dates. The numbered list of an entry is its modules
    (the part before the colon); "Activities and societies:" text is kept whole."""
    chunks, cur = [], []
    for ln in lines:
        if ln.startswith("Show all"):
            continue
        cur.append(ln)
        if _SKILLS_LINE.search(ln):
            chunks.append(cur)
            cur = []
    if any(x for x in cur):
        chunks.append(cur)
    out = []
    for chunk in chunks:
        body = [x for x in chunk if x]
        if len(body) < 3:
            continue
        ed = {"org": body[0], "degree": body[1], "dates": "", "grade": "", "activities": "", "modules": []}
        rest = body[2:]
        if rest and _SPAN.match(rest[0]):
            ed["dates"] = _span(rest[0])
            rest = rest[1:]
        act: list[str] = []
        mods: list[list[str]] = []
        mode = ""
        for ln in rest:
            if _SKILLS_LINE.search(ln):
                break
            if ln.startswith("Grade:"):
                ed["grade"] = ln.split(":", 1)[1].strip()
                mode = ""
            elif ln.startswith("Activities and societies:"):
                act.append(ln.split(":", 1)[1].strip())
                mode = "act"
            elif _NUMBERED.match(ln):
                mods.append([_NUMBERED.match(ln).group(2)])
                mode = "mod"
            elif mode == "mod" and mods:
                mods[-1].append(ln)
            elif mode == "act":
                act.append(ln)
        ed["activities"] = _join(act)
        ed["modules"] = [m for m in (_join(x).split(":")[0].strip() for x in mods) if m]
        out.append(ed)
    return out


def research_focus(activities: str) -> str:
    """The text after "Research Focus:" in an education entry's activities, "" when there is none."""
    m = re.search(r"Research Focus:\s*(.*)", activities, re.S)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------------------- projects, certifications, publications, languages
def parse_projects(lines: list[str]) -> list[dict]:
    """Entries {name, dates, description, associated_with}: title lines, a date line, a description, "Associated with X"."""
    out = []
    dates = [k for k, ln in enumerate(lines) if _SPAN.match(ln)]
    prev_end = 0
    for d in dates:
        title = _join([x for x in lines[prev_end:d] if x and not x.startswith("Show all")])
        desc: list[str] = []
        assoc = ""
        j = d + 1
        while j < len(lines):
            ln = lines[j]
            if ln.startswith("Associated with"):
                assoc = ln[len("Associated with"):].strip()
                j += 1
                break
            if ln.startswith("Show all"):
                break
            if ln:
                desc.append(ln)
            j += 1
        prev_end = j
        if title:
            out.append({"name": title, "dates": _span(lines[d]), "description": _join(desc), "associated_with": assoc})
    return out


def parse_certifications(lines: list[str]) -> list[dict]:
    """Entries {name, issuer, date}: the line before the issuer is the name, "Issued Mon YYYY" follows the issuer."""
    out = []
    for i, ln in enumerate(lines):
        m = re.match(rf"^Issued ({MON} \d{{4}}|\d{{4}})", ln)
        if not m:
            continue
        issuer_i = _nonblank(lines, i, -1)
        name_i = _nonblank(lines, issuer_i, -1) if issuer_i is not None else None
        if issuer_i is None or name_i is None:
            continue
        name = lines[name_i].rstrip(". ").strip()
        if name:
            out.append({"name": name, "issuer": lines[issuer_i].strip(), "date": m.group(1)})
    return out


def parse_publications(lines: list[str]) -> list[dict]:
    """Entries {title, venue, date}: title lines, then "Venue · Mon D, YYYY" (or "Venue · Mon YYYY")."""
    out = []
    for v, ln in enumerate(lines):
        m = re.match(rf"^(.*) · ({MON} (?:\d{{1,2}}, )?\d{{4}}|\d{{4}})$", ln)
        if not m:
            continue
        title, j = [], v - 1                              # the title is the block of lines just above the venue line
        while j >= 0 and not lines[j]:                    # (a blank line may separate them)
            j -= 1
        while j >= 0 and lines[j]:
            title.insert(0, lines[j])
            j -= 1
        if title:
            out.append({"title": _join(title).rstrip(". ").strip(), "venue": m.group(1).strip(), "date": m.group(2)})
    return out


def parse_languages(lines: list[str]) -> list[dict]:
    """Entries {name, level}: a language line, then its proficiency line. A name repeated across a page break is kept once."""
    out, seen = [], set()
    nonblank = [x for x in lines if x]
    for i, ln in enumerate(nonblank):
        if "proficiency" in ln.lower() and i > 0:
            name = nonblank[i - 1]
            if name.casefold() not in seen and "proficiency" not in name.lower():
                seen.add(name.casefold())
                out.append({"name": name, "level": ln})
    return out


def parse_linkedin(text: str) -> dict:
    """The PDF text as {experience, education, projects, certifications, publications, languages, research_focus, counts}."""
    sections, counts = split_sections(clean_lines(text))
    edu = parse_education(sections.get("Education", []))
    focus = next((f for f in (research_focus(e["activities"]) for e in edu) if f), "")
    return {"experience": parse_experience(sections.get("Experience", [])), "education": edu,
            "projects": parse_projects(sections.get("Projects", [])),
            "certifications": parse_certifications(sections.get("Licenses & certifications", [])),
            "publications": parse_publications(sections.get("Publications", [])),
            "languages": parse_languages(sections.get("Languages", [])),
            "research_focus": focus, "counts": counts}


# ------------------------------------------------------------------------------------------------------ comparing
_STOP = {"ltd", "limited", "the", "of", "and", "inc", "llc", "plc", "co", "company", "university"}


def _norm(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").casefold()).split())


def _tokens(s, stop: set[str] | None = None) -> set[str]:
    return {t for t in _norm(s).split() if t not in (stop or set())}


def _place(s) -> set[str]:
    """The words of a place, with "UK" and "USA" spelled out so "Bristol, UK" is inside "Bristol, England, United Kingdom"."""
    out: set[str] = set()
    for t in _tokens(s):
        out |= {"uk": {"united", "kingdom"}, "usa": {"united", "states"}, "us": {"united", "states"}}.get(t, {t})
    return out


def _words(s) -> set[str]:
    """The words of at least four letters, to tell two projects or certificates apart."""
    return {t for t in _tokens(s) if len(t) >= 4 and t not in {"project", "with", "from", "using", "certificate"}}


def _related(a: str, b: str, stop: set[str] | None = _STOP) -> bool:
    """Same entity by name: equal, or one's words all inside the other's ("Self Employed" in "Self-employed, Chennai")."""
    ta, tb = _tokens(a, stop) or _tokens(a), _tokens(b, stop) or _tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def _month_year(text: str):
    m = re.match(rf"^({MON})?\s*(\d{{4}})$", text.strip())
    if m:
        return (int(m.group(2)), (m.group(1) or "")[:3].casefold() or None)
    return "present" if text.strip().casefold() == "present" else None


def span_key(text: str):
    """(start, end) as ((year, month abbreviation or None), ...) for "Mon YYYY - Mon YYYY" in any separator, else None."""
    parts = [p for p in re.split(r"\s*(?:-|–|—| to )\s*", " ".join(str(text or "").split()), maxsplit=1) if p]
    if len(parts) == 2:
        a, b = _month_year(parts[0]), _month_year(parts[1])
        if a and b:
            return a, b
    return None


def _degree_key(s: str) -> set[str]:
    t = _norm(s)
    for long, short in (("master of science", "msc"), ("bachelor of engineering", "be"), ("bachelor of science", "bsc"),
                        ("b e", "be"), ("m sc", "msc")):
        t = t.replace(long, short)
    return {w for w in t.split() if w not in {"and", "of", "in"}}


def _union_span(spans: list[str]) -> str:
    keys = [(k, s) for s in spans if (k := span_key(s))]
    if not keys:
        return ""
    ordered = sorted(keys, key=lambda ks: (ks[0][0][0], ks[0][0][1] or ""))
    start = ordered[0][1].split(" – ")[0]
    ends = sorted(keys, key=lambda ks: (999999 if ks[0][1] == "present" else ks[0][1][0], ks[0][1][1] if ks[0][1] != "present" else ""))
    return f"{start} – {ends[-1][1].split(' – ')[-1]}"


def compare(parsed: dict, fb: dict) -> tuple[list[str], dict[str, int]]:
    """(markdown lines, counts) listing every mismatch between the parsed LinkedIn profile and the fact bank `fb`: titles,
    organisations, dates, locations, employment types, degrees, modules, and what is on only one side."""
    md: list[str] = []
    n = {"matched": 0, "mismatches": 0, "unverified": 0}

    def issue(label: str, fact, linkedin) -> None:
        n["mismatches"] += 1
        md.append(f"- **{label}** — fact bank: {fact if fact not in (None, '') else '(none)'} | LinkedIn: {linkedin if linkedin not in (None, '') else '(none)'}")

    # -- experience
    md.append("## Experience")
    li_exp, used = parsed["experience"], set()
    for e in fb.get("experience") or []:
        hits = [k for k, x in enumerate(li_exp) if _related(e.get("org", ""), x["org"])]
        if not hits and e.get("employment_type") == "freelance":
            hits = [k for k, x in enumerate(li_exp) if x["employment_type"] == "freelance"]
        head = f"### {e.get('org', '?')}  (fact bank id `{e.get('id', '?')}`)"
        if not hits:
            md += [head, "- **not on LinkedIn** (no entry with this organisation)"]
            n["mismatches"] += 1
            continue
        used.update(hits)
        rows = [li_exp[k] for k in hits]
        start = len(md)
        md.append(head)
        if (_tokens(e.get("org"), _STOP) or _tokens(e.get("org"))) != (_tokens(rows[0]["org"], _STOP) or _tokens(rows[0]["org"])):
            issue("org", e.get("org"), " / ".join(dict.fromkeys(r["org"] for r in rows)))
        titles = [r["title"] for r in rows]
        if not any(_norm(e.get("title")) == _norm(t) or _tokens(e.get("title")) == _tokens(t) for t in titles):
            issue("title", e.get("title"), " / ".join(titles))
        li_span = _union_span([r["dates"] for r in rows]) if len(rows) > 1 else rows[0]["dates"]
        fb_key, li_key = span_key(e.get("dates", "")), span_key(li_span)
        if fb_key != li_key:
            detail = f"{li_span}" + (" (" + "; ".join(f"{r['title']}: {r['dates']}" for r in rows) + ")" if len(rows) > 1 else "")
            issue("dates", e.get("dates"), detail)
        loc = next((r["location"] for r in rows if r["location"]), "")
        if not e.get("location") and loc:
            issue("location", None, loc)
        elif e.get("location") and loc and not (_place(e["location"]) <= _place(loc) or _place(loc) <= _place(e["location"])):
            issue("location", e["location"], loc)
        types = list(dict.fromkeys(r["employment_type"] for r in rows if r["employment_type"]))
        fb_type = str(e.get("employment_type") or "").strip().lower()
        if types and fb_type not in types:
            issue("employment_type", fb_type, " / ".join(types))
        if len(md) == start + 1:
            md.pop()
            n["matched"] += 1
    for k, x in enumerate(li_exp):
        if k not in used:
            md += [f"### {x['org']}  (only on LinkedIn)", f"- **title** {x['title']} | **dates** {x['dates']} | "
                   f"**employment_type** {x['employment_type'] or '(none)'} | **location** {x['location'] or '(none)'}"]
            n["mismatches"] += 1

    # -- education
    md.append("\n## Education")
    li_edu, used = parsed["education"], set()
    for ed in fb.get("education") or []:
        hit = next((k for k, x in enumerate(li_edu) if _related(ed.get("org", ""), x["org"], set())), None)
        head = f"### {ed.get('org', '?')}"
        if hit is None:
            md += [head, "- **not on LinkedIn**"]
            n["mismatches"] += 1
            continue
        used.add(hit)
        x, start = li_edu[hit], len(md)
        md.append(head)
        if _degree_key(ed.get("degree", "")) != _degree_key(x["degree"]):
            issue("degree", ed.get("degree"), x["degree"])
        if span_key(ed.get("dates", "")) != span_key(x["dates"]):
            issue("dates", ed.get("dates"), x["dates"])
        fb_mods, li_mods = ed.get("modules") or [], x["modules"]
        if {_norm(m) for m in fb_mods} != {_norm(m) for m in li_mods} and li_mods:
            issue("modules", ", ".join(map(str, fb_mods)), ", ".join(li_mods))
        grade_digits = re.findall(r"\d+", x["grade"])
        fb_note = " ".join(str(ed.get(k) or "") for k in ("grade", "note", "details"))
        if grade_digits and not all(g in fb_note for g in grade_digits):
            issue("grade", fb_note.strip(), x["grade"])
        if len(md) == start + 1:
            md.pop()
            n["matched"] += 1
    for k, x in enumerate(li_edu):
        if k not in used:
            md += [f"### {x['org']}  (only on LinkedIn)", f"- {x['degree']} | {x['dates']}"]
            n["mismatches"] += 1

    # -- projects
    md.append("\n## Projects")
    li_proj = parsed["projects"]
    fb_proj = fb.get("projects") or []
    cut = parsed["counts"].get("Projects", 0) > len(li_proj)     # LinkedIn printed only some of them: absence proves nothing

    def same_project(a: str, b: str) -> bool:
        return _related(a, b, _STOP | {"project", "poc"}) or len(_words(a) & _words(b)) >= 2

    for p in fb_proj:
        if any(same_project(p.get("name", ""), x["name"]) for x in li_proj):
            n["matched"] += 1
        elif cut:
            md.append(f"- not in the PDF, which holds {len(li_proj)} of {parsed['counts']['Projects']} (unverified): {p.get('name')}")
            n["unverified"] += 1
        else:
            md.append(f"- **in the fact bank, not on LinkedIn**: {p.get('name')}")
            n["mismatches"] += 1
    for x in li_proj:
        if not any(same_project(p.get("name", ""), x["name"]) for p in fb_proj):
            md.append(f"- **only on LinkedIn**: {x['name']} ({x['dates']})")
            n["mismatches"] += 1

    # -- certifications
    md.append("\n## Certifications")
    fb_certs = [str(c) for c in fb.get("certifications") or []]
    li_certs = parsed["certifications"]
    cut = parsed["counts"].get("Licenses & certifications", 0) > len(li_certs)

    def same_cert(c: str, x: dict) -> bool:          # two words in common, the issuer's name counting as part of the title
        return len(_words(c) & _words(f"{x['name']} {x['issuer']}")) >= 2

    for c in fb_certs:
        hits = [x for x in li_certs if same_cert(c, x)]
        if hits:
            n["matched"] += 1
            for x in hits:
                if x["date"] and _norm(x["date"]) not in _norm(c):
                    issue(f"date of '{x['name']}'", c, x["date"])
        elif cut:
            md.append(f"- not in the PDF, which holds {len(li_certs)} of {parsed['counts']['Licenses & certifications']} "
                      f"(unverified): {c}")
            n["unverified"] += 1
        else:
            md.append(f"- **in the fact bank, not on LinkedIn**: {c}")
            n["mismatches"] += 1
    for x in li_certs:
        if not any(same_cert(c, x) for c in fb_certs):
            md.append(f"- **only on LinkedIn**: {x['name']} ({x['issuer']}, {x['date']})")
            n["mismatches"] += 1

    # -- optional sections the CV can print
    for key, label, fmt in (("publications", "Publications", lambda x: f"{x['title']} ({x['venue']}, {x['date']})"),
                            ("languages", "Languages", lambda x: f"{x['name']} ({x['level']})")):
        md.append(f"\n## {label}")
        have, got = fb.get(key) or [], parsed[key]
        if not got and not have:
            md.append("- none on either side")
        elif got and not have:
            md.append(f"- **the fact bank has no `{key}`**; LinkedIn lists: " + "; ".join(fmt(x) for x in got))
            n["mismatches"] += 1
        else:
            for x in got:
                if not any(_tokens(x.get("title") or x.get("name")) <= _tokens(h.get("title") if isinstance(h, dict) else h) or
                           _tokens(h.get("title") or h.get("name") if isinstance(h, dict) else h) <= _tokens(x.get("title") or x.get("name"))
                           for h in have):
                    md.append(f"- **only on LinkedIn**: {fmt(x)}")
                    n["mismatches"] += 1
    md.append("\n## Research focus")
    if parsed["research_focus"] and not fb.get("research_focus"):
        md.append("- **the fact bank has no `research_focus`**; LinkedIn (MSc activities): " + parsed["research_focus"][:300])
        n["mismatches"] += 1
    else:
        md.append("- no difference")
    return md, n


def parse_warnings(parsed: dict) -> list[str]:
    """What the PDF cannot hold: a section whose heading counts more items than were found ("Projects (10)" but 2 parsed)."""
    got = {"Projects": len(parsed["projects"]), "Licenses & certifications": len(parsed["certifications"])}
    out = [f"LinkedIn lists {parsed['counts'][name]} items under '{name}' but the PDF holds {got[name]}: use 'Show all' on "
           f"LinkedIn, then save the PDF again (the rest cannot be compared)"
           for name in got if parsed["counts"].get(name, 0) > got[name]]
    out += [f"Project '{p['name'][:50]}': LinkedIn cut the description off with '...': complete it by hand"
            for p in parsed["projects"] if p["description"].endswith("...")]
    return out


# ------------------------------------------------------------------------------------------------------- proposal
def _slug(text: str, taken: set[str], n: int = 14) -> str:
    base = re.sub(r"[^a-z0-9]+", "", str(text).casefold())[:n] or "item"
    out, k = base, 2
    while out in taken:
        out, k = f"{base}{k}", k + 1
    taken.add(out)
    return out


def _bank_id(fb_rows: list[dict], is_match) -> str | None:
    return next((r.get("id") for r in fb_rows if r.get("id") and is_match(r)), None)


def proposal(parsed: dict, fb: dict) -> dict:
    """The LinkedIn profile in fact-bank schema. An entry that matches one in the fact bank keeps that entry's `id`.
    Bullets carry LinkedIn's text and an empty `terms` list: the user words them and lists their evidence by hand."""
    taken: set[str] = set()
    exp = []
    for e in parsed["experience"]:
        eid = _bank_id(fb.get("experience") or [], lambda r, e=e: _related(r.get("org", ""), e["org"])
                       and (len({x["org"] for x in parsed["experience"] if x["org"] == e["org"]}) == 1
                            or _tokens(r.get("title")) == _tokens(e["title"]))) or _slug(e["org"] or e["title"], taken)
        if eid in taken and eid != _slug(e["org"], set()):
            eid = _slug(eid, taken)
        taken.add(eid)
        row = {"id": eid, "title": e["title"], "org": e["org"], "dates": e["dates"]}
        if e["location"]:
            row["location"] = e["location"]
        if e["employment_type"]:
            row["employment_type"] = e["employment_type"]
        row["bullets"] = [{"id": f"{eid}{i}", "text": t, "terms": []} for i, t in enumerate(e["bullets"], 1)]
        exp.append(row)
    edu = []
    for ed in parsed["education"]:
        row = {"degree": ed["degree"], "org": ed["org"], "dates": ed["dates"]}
        if ed["grade"]:
            row["grade"] = ed["grade"]
        if ed["modules"]:
            row["modules"] = list(ed["modules"])
        edu.append(row)
    ptaken: set[str] = set()
    projects = []
    for p in parsed["projects"]:
        pid = _bank_id(fb.get("projects") or [], lambda r, p=p: len(_tokens(r.get("name", "")) & _tokens(p["name"])) >= 3) \
            or _slug(p["name"], ptaken)
        row = {"id": pid, "name": p["name"]}
        if p["description"]:
            row["bullets"] = [{"id": f"{pid}1", "text": p["description"], "terms": []}]
        projects.append(row)
    out: dict = {"experience": exp, "education": edu, "projects": projects,
                 "certifications": [f"{c['name']} ({c['issuer']}), {c['date']}" for c in parsed["certifications"]]}
    if parsed["publications"]:
        out["publications"] = [dict(p) for p in parsed["publications"]]
    if parsed["languages"]:
        out["languages"] = [dict(x) for x in parsed["languages"]]
    if parsed["research_focus"]:
        out["research_focus"] = parsed["research_focus"]
    return out


def write_proposal(prop: dict, path: Path, source: str) -> None:
    header = (f"# PROPOSED fact-bank entries from {source}, generated {date.today().isoformat()} by `careerops import-linkedin`.\n"
              "# Nothing reads this file. Merge what you want into fact_bank.yaml by hand: LinkedIn's wording, empty `terms`.\n"
              "# employment_type, location, modules, publications, languages and research_focus are optional fact-bank fields\n"
              "# that the two-page CV prints when they are present.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(prop, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")


def write_diff(md: list[str], counts: dict[str, int], warnings: list[str], path: Path, source: str) -> None:
    head = [f"# LinkedIn vs fact bank", "",
            f"Generated {date.today().isoformat()} from `{source}`. `fact_bank.yaml` was only read, never written. "
            f"{counts['matched']} entries match, {counts['mismatches']} mismatches and {counts['unverified']} unverified below.", ""]
    if warnings:
        head += ["## Warnings", *[f"- {w}" for w in warnings], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head + md) + "\n", encoding="utf-8")


def import_linkedin(pdf: Path, fact_bank: Path, proposed: Path, diff: Path, text: str | None = None) -> dict:
    """Parse the PDF (or `text`, the pdftotext output), write the proposal and the diff, and return
    {"parsed", "matched", "mismatches", "warnings"}. `fact_bank` is only read."""
    from ..config import load_yaml

    fb = load_yaml(fact_bank)
    parsed = parse_linkedin(extract_text(pdf) if text is None else text)
    md, counts = compare(parsed, fb)
    warnings = parse_warnings(parsed)
    write_proposal(proposal(parsed, fb), proposed, pdf.name)
    write_diff(md, counts, warnings, diff, pdf.name)
    return {"parsed": parsed, "matched": counts["matched"], "mismatches": counts["mismatches"],
            "unverified": counts["unverified"], "warnings": warnings}
