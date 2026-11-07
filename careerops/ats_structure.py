"""Can an ATS parser READ the CV? The structural score (0-100, deterministic, zero tokens) and the content lints, on the real PDF text layer.

Ported from career-ops (`ext/career-ops`, `verify-ats.mjs`, `ats-payload.mjs`, `modes/ats.md`, `modes/pdf.md` "ATS Rules", checked at 8ce89bdc). career-ops
reads the HTML before rendering; this reads the finished PDF (`pdftotext -layout` and `-raw`, `pdffonts`, `pdfimages -list`, `pdftotext -bbox`), which
is closer to what a parser gets. `ats.py` is the other half: keyword coverage, which career-ops treats as advisory.

    weight  check (on the PDF)
      15    selectable text: at least 300 characters, and no page without text
      20    standard headings on lines of their own: Experience (Work Experience), Education, Skills, 5 each; Profile / Summary, Projects,
            Certifications 2 each, at most 5
      15    e-mail (10) and phone (5) in the extracted text, not only inside a link annotation
      20    reading order: one column; in `-raw` order every role's title, employer and dates come before its bullets; no two sections interleave
      10    no text baked into images (no image half the page wide or wider)
      10    fonts embedded and not Type 3 (Carlito, Latin Modern: fine); no ligature or mojibake glyphs (ﬁ ﬂ ﬀ, U+FFFD, â€) in the extraction
       5    UTF-8 clean: the non-ASCII characters of the source (£ – é) survive extraction
       5    no hidden text or stuffing: no white text in the source, no text under 3 pt, no keyword more than 4 times outside Skills

Grades A >= 90, B >= 80, C >= 70, D >= 60, F. CRITICAL: no text layer, no Experience heading (or two required headings missing), no e-mail in the text,
scrambled reading order. A letter is scored on the checks that apply to it (text, contact, images, fonts, UTF-8, hidden), scaled to 100.

Content lints (reported, never applied: each is a decision only you can make, and the fix, if any, is yours in fact_bank.yaml or the template):
    multiple-date-ranges      two date ranges in one role line (title + dates): an ATS merges the stints
    parenthetical-in-title    a parenthesis in a job title: the title matches no search
    parenthetical-in-company  ... in an employer name
    employer-in-role          an employer or client name inside the title (a corporate suffix, " at " / "@", "client:", another role's employer)
    date-format-mixed         two date separators (- / -- / – / — / to) or two endpoint formats (Sep 2025 / September 2025 / 09/2025) in the text
    skills-undelimited        a Skills line of six or more words with no comma, semicolon or bar: the items run together
Nothing here writes a file.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

WEIGHTS = {"text": 15, "headings": 20, "contact": 15, "order": 20, "images": 10, "fonts": 10, "utf8": 5, "hidden": 5}
LETTER_CHECKS = ("text", "contact", "images", "fonts", "utf8", "hidden")
PASS_SCORE = 80                       # a CV under this, or with a critical issue, does not go out (apply/autogate.py)
TEXT_MIN = 300
PAGE_MIN = 20                         # characters a page needs to count as having text
GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"))
TOOLS = ("pdftotext", "pdffonts", "pdfimages", "pdfinfo")
CRITICAL, WARNING, INFO = "critical", "warning", "info"
STUFF_MAX = 4                         # a keyword more often than this outside Skills is stuffing
TINY_PT = 3.0
FULL_WIDTH = 0.5                      # an image this share of the page width or wider may hold text

HEADINGS = {
    "experience": re.compile(r"^(?:work|professional|relevant|employment)?\s*(?:experience|history)$|^employment(?: history)?$|^career history$", re.I),
    "education": re.compile(r"^education(?:\s*(?:&|and)\s*\w+.*)?$|^academic (?:background|qualifications)$", re.I),
    "skills": re.compile(r"^(?:technical |key |core )?skills(?:\s*(?:&|and)\s*\w+.*)?$|^(?:core )?competenc(?:ies|es)$", re.I),
}
BONUS = {"profile": re.compile(r"^(?:professional )?(?:profile|summary|objective)$", re.I),
         "projects": re.compile(r"^(?:selected |personal |key )?projects$", re.I),
         "certifications": re.compile(r"^(?:certifications?|licen[cs]es)(?:\s*(?:&|and)\s*\w+.*)?$", re.I)}
ANY_HEADING = [*HEADINGS.values(), *BONUS.values(), re.compile(r"^(?:publications|languages|research focus|achievements|awards.*)$", re.I)]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){2,4}\d{3,4}")
LIGATURES = "ﬀﬁﬂﬃﬄﬅﬆ"
MOJIBAKE = re.compile(r"â€|Ã[\x80-\xbf]|Â[\x80-\xbf]|\ufffd")
WHITE = re.compile(r"\\(?:text)?color\s*\{\s*white\s*\}|\\color\[[a-z]+\]\{1,1,1\}|\\fontsize\{[0-2](?:\.\d+)?(?:pt)?\}", re.I)
BULLET = re.compile(r"^\s*[•▪●◦∙\-*]\s+")

_MON_ABBR = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MON_FULL = r"january|february|march|april|june|july|august|september|october|november|december"
DATE_POINT = (rf"(?:(?:{_MON_FULL}|{_MON_ABBR})\.?\s*,?\s*\d{{4}}|\d{{1,2}}\s*/\s*\d{{4}}|(?<![\d/])\d{{4}}(?![\d/])|present|current|now|ongoing)")
DATE_SEP = r"\s*(--|[-–—]|\bto\b|\buntil\b)\s*"
DATE_RANGE = re.compile(rf"(?P<a>{DATE_POINT}){DATE_SEP}(?P<b>{DATE_POINT})", re.I)

CORPORATE = re.compile(r"\b(?:inc|llc|ltd|limited|gmbh|ag|plc|corp|corporation|company|pty|holdings|group)\b\.?", re.I)
AT_EMPLOYER = re.compile(r"(?:\s(?:at|@)\s|\s@|^@)", re.I)
CLIENT = re.compile(r"\b(?:client|customer|on behalf of|seconded to)\s*[:—–-]?\s*\S", re.I)
PAREN = re.compile(r"[(（]\s*([^)）]*?)\s*[)）]")
DELIMITERS = re.compile(r"[,;|•·/]")


class ToolMissing(RuntimeError):
    """poppler-utils is not installed: the check cannot run (one line, fit to print)."""


# ------------------------------------------------------------------ what the parser sees
@dataclass
class Extract:
    layout: str
    raw: str
    fonts: str = ""
    images: str = ""
    page_width: float = 595.0
    uris: list[str] = field(default_factory=list)
    tiny: int = 0                       # words under TINY_PT high

    @property
    def pages(self) -> list[str]:
        got = self.layout.split("\f")
        return got[:-1] if len(got) > 1 and not got[-1].strip() else got


def _run(args: Sequence[str]) -> str:
    proc = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout


def pdf_uris(data: bytes) -> list[str]:
    """The link targets (/URI) in the PDF, from its plain objects and from every Flate stream (object streams hold the annotations)."""
    blobs = [data]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            blobs.append(zlib.decompress(m.group(1)))
        except zlib.error:
            continue
    out = []
    for b in blobs:
        for m in re.finditer(rb"/URI\s*\((.*?)(?<!\\)\)", b, re.S):
            out.append(m.group(1).decode("latin-1", "replace"))
    return list(dict.fromkeys(out))


def extract(pdf: Path | str) -> Extract:
    """Run the poppler tools on one PDF. Raises ToolMissing when one is not installed (a check that cannot run is never a pass)."""
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        raise ToolMissing(f"{', '.join(missing)} not found: install poppler-utils (`careerops doctor` checks it)")
    p = str(pdf)
    info = _run(["pdfinfo", p])
    m = re.search(r"Page size:\s*([\d.]+)\s*x\s*([\d.]+)", info)
    bbox = _run(["pdftotext", "-bbox", p, "-"])
    tiny = sum(1 for y0, y1 in re.findall(r'<word xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)"', bbox) if float(y1) - float(y0) < TINY_PT)
    return Extract(_run(["pdftotext", "-layout", "-enc", "UTF-8", p, "-"]), _run(["pdftotext", "-raw", "-enc", "UTF-8", p, "-"]),
                   _run(["pdffonts", p]), _run(["pdfimages", "-list", p]), float(m.group(1)) if m else 595.0,
                   pdf_uris(Path(p).read_bytes()), tiny)


# ------------------------------------------------------------------ the score
@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str

    def line(self) -> str:
        return f"{self.severity.upper()} {self.code}: {self.message}"


@dataclass
class Structure:
    kind: str                                   # cv | letter
    score: int
    issues: list[Issue] = field(default_factory=list)
    parts: dict[str, tuple[float, int]] = field(default_factory=dict)       # check -> (points, weight)

    @property
    def grade(self) -> str:
        return grade(self.score)

    @property
    def critical(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == CRITICAL]

    @property
    def ok(self) -> bool:
        """Fit to go out: no critical issue and at least PASS_SCORE."""
        return not self.critical and self.score >= PASS_SCORE

    def line(self) -> str:
        head = "ATS structure" if self.kind == "cv" else "letter structure"
        return f"{head} {self.score}/{self.grade} ({len(self.critical)} critical)"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "score": self.score, "grade": self.grade, "ok": self.ok,
                "issues": [{"code": i.code, "severity": i.severity, "message": i.message} for i in self.issues]}


def grade(score: float) -> str:
    return next((g for lo, g in GRADES if score >= lo), "F")


def _lines(text: str) -> list[str]:
    return [" ".join(l.split()) for l in text.splitlines()]


def heading_of(line: str) -> str | None:
    """The heading a stripped line is, by name (experience, education, skills, profile, projects, certifications, other), or None."""
    s = line.strip().rstrip(":").strip()
    if not s or len(s.split()) > 5:
        return None
    for name, rx in {**HEADINGS, **BONUS}.items():
        if rx.match(s):
            return name
    return "other" if ANY_HEADING[-1].match(s) else None


def headings_found(text: str) -> list[tuple[int, str]]:
    """(line number, name) of every heading line, in order."""
    return [(n, h) for n, l in enumerate(_lines(text)) if (h := heading_of(l))]


def section(text: str, name: str) -> str:
    """The lines under heading `name` up to the next heading ('' when it has none)."""
    lines = text.splitlines()
    found = headings_found(text)
    for i, (n, h) in enumerate(found):
        if h == name:
            end = found[i + 1][0] if i + 1 < len(found) else len(lines)
            return "\n".join(lines[n + 1:end])
    return ""


def _gap_split(line: str) -> tuple[str, str] | None:
    m = re.search(r"\S( {6,})\S", line)
    if not m:
        return None
    return line[:m.start(1)].strip(), line[m.end(1):].strip()


def two_column_lines(layout: str) -> list[str]:
    """`-layout` lines that carry text in two columns: a wide gap with three or more words on each side, the right side not a date range or a link (the
    template right-aligns those)."""
    out = []
    for line in layout.splitlines():
        got = _gap_split(line)
        if not got:
            continue
        left, right = got
        if len(left.split()) < 3 or len(right.split()) < 3 or DATE_RANGE.fullmatch(right.strip(" ()")) or re.match(r"https?://|www\.", right):
            continue
        out.append(line.strip())
    return out


def _norm(s: str) -> str:
    return " ".join(str(s or "").replace("–", "-").replace("—", "-").replace("--", "-").lower().split())


def first_bullets(layout: str, roles: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """org -> the first 40 characters of the first bullet under that role's header on the page (`-layout`)."""
    lines = _lines(layout)
    out = {}
    for r in roles:
        org = _norm(r.get("org"))
        at = next((i for i, l in enumerate(lines) if org and org in _norm(l)), None)
        if at is None:
            continue
        b = next((l for l in lines[at + 1:at + 8] if BULLET.match(l) or l[:1] in "•▪●◦∙"), None)
        if b:
            out[org] = _norm(BULLET.sub("", b).lstrip("•▪●◦∙ "))[:40]
    return out


def role_order_problems(raw: str, roles: Sequence[Mapping[str, Any]], layout: str = "") -> list[str]:
    """Roles whose title, employer or dates come after a bullet of theirs in `-raw` order: a bullet between them, or the role's first bullet on the page
    (from `layout`) read before them; a part missing from the text layer is a problem too. `roles`: dicts with title, org, dates."""
    flat = " " + _norm(raw.replace("\n", " "))
    bpos = [m.start() for m in re.finditer(r"\s[•▪●◦∙]\s", flat)]
    bullets = first_bullets(layout, roles) if layout else {}
    out = []

    def nearest(v: str, at: int) -> int:
        """The occurrence of `v` nearest the employer's (a title also found in the profile is not this role's), -1 when there is none."""
        hits = [m.start() for m in re.finditer(re.escape(v), flat)]
        return min(hits, key=lambda p: abs(p - at)) if hits else -1

    for r in roles:
        org, title, dates = _norm(r.get("org")), _norm(r.get("title"))[:30], _norm(r.get("dates"))
        if not org or org not in flat:
            continue
        at = flat.find(org)
        parts = [(name, nearest(v, at)) for name, v in (("employer", org), ("title", title), ("dates", dates)) if v]
        lost = [n for n, p in parts if p < 0]
        if lost:
            out.append(f"{r.get('org')}: {', '.join(lost)} not found in the text layer")
            continue
        first, last = min(p for _, p in parts), max(p for _, p in parts)
        if any(first < b < last for b in bpos):
            out.append(f"{r.get('org')}: a bullet comes between its {', '.join(n for n, _ in parts)} in reading order")
            continue
        lead = bullets.get(org)
        if lead and 0 <= nearest(lead, at) < first:
            out.append(f"{r.get('org')}: its first bullet (\"{lead[:30]}...\") is read before its {', '.join(n for n, _ in parts)}")
    return out


def interleave_problems(layout: str, raw: str) -> list[str]:
    """The sections in `-raw` order against `-layout` order: a heading twice, or the order changed."""
    a, b = [h for _, h in headings_found(layout)], [h for _, h in headings_found(raw)]
    out = []
    dup = sorted({h for h in b if b.count(h) > 1 and h != "other"})
    if dup:
        out.append(f"heading(s) {', '.join(dup)} found more than once in reading order")
    if [h for h in a if h != "other"] != [h for h in b if h != "other"]:
        out.append(f"sections in reading order {b} differ from the page {a}")
    return out


def font_problems(pdffonts: str) -> tuple[list[str], list[str]]:
    """(unembedded font names, Type 3 font names) from a `pdffonts` listing."""
    unemb, type3, listed = [], [], 0
    for line in pdffonts.splitlines():
        m = re.search(r"\s(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)
        if not m or line.startswith("name "):
            continue
        listed += 1
        name = line.split()[0].split("+")[-1]
        if m.group(1) == "no":
            unemb.append(name)
        if "Type 3" in line:
            type3.append(name)
    return (unemb if listed else ["(no fonts listed)"]), type3


def wide_images(images: str, page_width: float) -> list[str]:
    """`pdfimages -list` rows of images FULL_WIDTH of the page wide or wider (their width in points from the pixels and x-ppi)."""
    out = []
    for line in images.splitlines():
        cols = line.split()
        if len(cols) < 14 or not cols[0].isdigit() or cols[2] != "image":
            continue
        try:
            px, ppi = float(cols[3]), float(cols[12])
        except ValueError:
            continue
        width_pt = px / ppi * 72 if ppi > 0 else px
        if width_pt >= FULL_WIDTH * page_width:
            out.append(f"page {cols[0]}: {cols[3]}x{cols[4]} px ({width_pt:.0f} pt wide)")
    return out


def stuffing(layout: str, keywords: Iterable[str]) -> list[str]:
    """`term ×n` for every keyword found more than STUFF_MAX times outside the Skills section."""
    skills = section(layout, "skills")
    rest = _norm(layout.replace(skills, " ") if skills else layout)
    out = []
    for k in dict.fromkeys(k for k in keywords if k and len(k) > 2):
        n = len(re.findall(rf"(?<![a-z0-9]){re.escape(_norm(k))}(?![a-z0-9])", rest))
        if n > STUFF_MAX:
            out.append(f"{k} ×{n}")
    return out


def score(ex: Extract, *, kind: str = "cv", roles: Sequence[Mapping[str, Any]] = (), keywords: Iterable[str] = (), source: str = "") -> Structure:
    """The structural score of one extracted PDF. `roles` (title / org / dates of the roles on the CV) adds the role-order check; `keywords` the stuffing
    check; `source` (the .tex) the white-text check and the characters UTF-8 must keep. Pure: no tool is run here."""
    issues: list[Issue] = []
    got: dict[str, float] = {}
    add = lambda code, sev, msg: issues.append(Issue(code, sev, msg))      # noqa: E731
    text = ex.layout
    body = text.strip()
    # 1. text layer
    empty = [n for n, p in enumerate(ex.pages, 1) if len(p.strip()) < PAGE_MIN]
    if len(body) < TEXT_MIN:
        got["text"] = 0
        add("text-layer", CRITICAL, f"only {len(body)} characters of selectable text (at least {TEXT_MIN}): the PDF is image-based or empty; a parser reads nothing")
    elif empty:
        got["text"] = 0
        add("image-only-page", WARNING, f"page(s) {', '.join(map(str, empty))} have no text layer")
    else:
        got["text"] = WEIGHTS["text"]
    # 2. headings
    if kind == "cv":
        names = {h for _, h in headings_found(text)}
        missing = [h for h in HEADINGS if h not in names]
        got["headings"] = 5 * (3 - len(missing)) + min(5, 2 * sum(1 for b in BONUS if b in names))
        if missing:
            crit = "experience" in missing or len(missing) >= 2
            add("section-headings", CRITICAL if crit else WARNING, f"no {', '.join(m.capitalize() for m in missing)} heading on a line of its own")
    # 3. contact
    email_link = any(u.lower().startswith("mailto:") for u in ex.uris)
    pts = 0
    if EMAIL.search(text):
        pts += 10
    elif email_link:
        add("email-in-link-only", CRITICAL, "the e-mail is only inside a link annotation (mailto:), not in the text: a parser that reads text never sees it")
    else:
        add("email-missing", CRITICAL, "no e-mail address in the text layer")
    if any(len(re.sub(r"\D", "", m.group(0))) >= 10 for m in PHONE.finditer(text)):
        pts += 5
    else:
        add("phone-missing", INFO, "no phone number in the text layer" + (" (only a tel: link)" if any(u.startswith("tel:") for u in ex.uris) else ""))
    got["contact"] = pts
    # 4. reading order
    if kind == "cv":
        pts = WEIGHTS["order"]
        cols = two_column_lines(text)
        nonblank = max(1, sum(1 for l in text.splitlines() if l.strip()))
        if len(cols) >= 5 and len(cols) / nonblank >= 0.15:
            pts -= 12
            add("two-column", CRITICAL, f"{len(cols)} lines carry two columns of text (e.g. \"{cols[0][:70]}\"): a parser reads across them and scrambles both")
        if (bad := interleave_problems(text, ex.raw)):
            pts -= 8
            add("sections-interleave", CRITICAL, "; ".join(bad))
        if (bad := role_order_problems(ex.raw, roles, text)):
            pts -= 8
            add("role-order", CRITICAL, "; ".join(bad))
        got["order"] = max(0, pts)
    # 5. images
    wide = wide_images(ex.images, ex.page_width)
    got["images"] = 0 if wide else WEIGHTS["images"]
    if wide:
        add("images-text", WARNING if len(body) >= TEXT_MIN else CRITICAL, f"{len(wide)} image(s) half the page wide or more ({wide[0]}): text inside an image is invisible to a parser")
    # 6. fonts and glyphs
    pts = WEIGHTS["fonts"]
    unemb, type3 = font_problems(ex.fonts)
    if unemb:
        pts -= 6
        add("fonts-not-embedded", WARNING, f"font(s) not embedded: {', '.join(unemb)}")
    if type3:
        pts -= 4
        add("fonts-type3", WARNING, f"Type 3 (bitmap) font(s): {', '.join(type3)}")
    ligs = sorted({c for c in text if c in LIGATURES})
    if ligs:
        pts -= 4
        add("ligature-glyphs", WARNING, f"ligature glyph(s) {' '.join(ligs)} in the extracted text: \"ﬁnance\" is not \"finance\" to a parser (turn ligatures off in the template)")
    if MOJIBAKE.search(text):
        pts -= 4
        bad = MOJIBAKE.search(text).group(0)
        add("mojibake", WARNING, f"broken characters in the extraction ({bad!r}): the encoding did not survive")
    got["fonts"] = max(0, pts)
    # 7. UTF-8
    want = sorted({c for c in _visible(source) if ord(c) > 127 and c.isprintable() and c not in LIGATURES}) if source else []
    lost = [c for c in want if c not in text]
    got["utf8"] = 0 if lost or MOJIBAKE.search(text) else WEIGHTS["utf8"]
    if lost:
        add("utf8-lost", WARNING, f"character(s) {' '.join(lost)} of the source are not in the extracted text")
    # 8. hidden text and stuffing
    hidden = []
    if source and WHITE.search(source):
        hidden.append(f"white or tiny text in the source ({WHITE.search(source).group(0)})")
    if ex.tiny:
        hidden.append(f"{ex.tiny} word(s) under {TINY_PT:g} pt")
    if kind == "cv" and (stuffed := stuffing(text, keywords)):
        hidden.append(f"keyword(s) more than {STUFF_MAX} times outside Skills: {', '.join(stuffed[:5])}")
    got["hidden"] = 0 if hidden else WEIGHTS["hidden"]
    if hidden:
        add("hidden-or-stuffed", WARNING, "; ".join(hidden))
    checks = list(WEIGHTS) if kind == "cv" else list(LETTER_CHECKS)
    total = sum(WEIGHTS[c] for c in checks)
    points = sum(got.get(c, 0) for c in checks)
    return Structure(kind, round(100 * points / total), issues, {c: (got.get(c, 0), WEIGHTS[c]) for c in checks})


def _visible(tex: str) -> str:
    """The characters of a .tex a reader would see: comments, commands and their option brackets dropped (roughly; enough to know which non-ASCII
    characters the CV prints)."""
    lines = [re.sub(r"(?<!\\)%.*", "", l) for l in (tex or "").splitlines()]
    return re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?", " ", "\n".join(lines))


def check_pdf(pdf: Path | str, *, kind: str = "cv", roles: Sequence[Mapping[str, Any]] = (), keywords: Iterable[str] = ()) -> Structure:
    """extract + score; the .tex beside the PDF (when there is one) is the source."""
    p = Path(pdf)
    tex = p.with_suffix(".tex")
    return score(extract(p), kind=kind, roles=roles, keywords=keywords, source=tex.read_text(encoding="utf-8", errors="replace") if tex.is_file() else "")


# ------------------------------------------------------------------ the lints
@dataclass(frozen=True)
class Lint:
    code: str
    where: str                  # the employer, or "text" for a text-layer lint
    value: str                  # the exact line
    decision: str               # what you need to decide

    def line(self) -> str:
        return f"LINT {self.code} ({self.where}): {self.value}\n      decide: {self.decision}"


def count_ranges(text: str) -> int:
    return len(DATE_RANGE.findall(text or ""))


def _token(hay: str, needle: str) -> bool:
    n = needle.strip().lower()
    return len(n) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", hay.lower()) is not None


def role_lints(roles: Sequence[Mapping[str, Any]]) -> list[Lint]:
    """multiple-date-ranges, parenthetical-in-title / -in-company and employer-in-role on the roles of the CV (fact-bank entries: id, title, org, dates)."""
    out: list[Lint] = []
    orgs = [str(r.get("org") or "").strip() for r in roles if str(r.get("org") or "").strip()]
    for r in roles:
        rid, title, org, dates = str(r.get("id") or "?"), str(r.get("title") or ""), str(r.get("org") or ""), str(r.get("dates") or "")
        where, entry = org or rid, f"fact_bank.yaml experience id `{rid}`"
        n = count_ranges(title) + count_ranges(dates)
        if n > 1:
            out.append(Lint("multiple-date-ranges", where, f'title "{title}" | dates "{dates}"',
                            f"{n} date ranges in one role parse as one stint. Split it into separate roles (and say which bullets belong to which), or keep "
                            f"it as one: your call, in {entry}; nothing is changed for you"))
        if (m := PAREN.search(title)):
            out.append(Lint("parenthetical-in-title", where, f'title "{title}"',
                            f'"({m.group(1)})" is read as part of the job title, so the title matches no search. Decide where it belongs (a bullet, the '
                            f"dates, nowhere) and edit {entry} yourself; the title is never reworded for you"))
        if (m := PAREN.search(org)):
            out.append(Lint("parenthetical-in-company", where, f'employer "{org}"',
                            f'"({m.group(1)})" is kept as part of the employer name, so the record matches no search for the employer. Decide where '
                            f"it goes and edit {entry} yourself"))
        signals = [s for s, hit in (("a corporate suffix", CORPORATE.search(title)), ('"at" / "@"', AT_EMPLOYER.search(title)),
                                    ("a client marker", CLIENT.search(title))) if hit]
        other = next((o for o in orgs if o != org and _token(title, o)), None)
        if other:
            signals.append(f"another role's employer ({other})")
        if signals:
            out.append(Lint("employer-in-role", where, f'title "{title}"',
                            f"the title reads as if it names an employer ({', '.join(signals)}): a parser lifts it into the employer field. Whether it "
                            f"belongs in the employer instead is yours to decide, in {entry}"))
    return out


def _sep_kind(sep: str) -> str:
    s = sep.strip().lower()
    return {"--": "--", "-": "-", "–": "–", "—": "—"}.get(s, s)


def _point_kind(p: str) -> str:
    s = p.strip().lower().rstrip(".")
    if re.match(r"\d{1,2}\s*/", s):
        return "MM/YYYY"
    if re.fullmatch(r"\d{4}", s):
        return "YYYY"
    word = re.match(r"[a-z]+", s)
    if not word or word.group(0) in ("present", "current", "now", "ongoing"):
        return ""
    w = word.group(0)
    if w == "may":
        return ""                                   # the same written out and abbreviated
    return "Month YYYY" if re.fullmatch(_MON_FULL, w) else "Mon YYYY"


def date_lints(text: str) -> list[Lint]:
    """date-format-mixed on the text layer: every date range's separator and endpoint format; two kinds of either is a finding, with an example of each."""
    seps: dict[str, str] = {}
    fmts: dict[str, str] = {}
    for line in _lines(text):
        for m in DATE_RANGE.finditer(line):
            seps.setdefault(_sep_kind(re.search(DATE_SEP, m.group(0)[len(m.group("a")):]).group(1)), m.group(0))
            for p in (m.group("a"), m.group("b")):
                if (k := _point_kind(p)):
                    fmts.setdefault(k, m.group(0))
    out = []
    if len(seps) > 1:
        out.append(Lint("date-format-mixed", "text", "; ".join(f'"{ex}" ({k})' for k, ex in seps.items()),
                        "the date ranges use different separators, which breaks years-of-experience counting in some parsers. Pick one and change the "
                        "fact bank or the template yourself"))
    if len(fmts) > 1:
        out.append(Lint("date-format-mixed", "text", "; ".join(f'"{ex}" ({k})' for k, ex in fmts.items()),
                        "the dates are written in different formats. Pick one (e.g. \"Sep 2025\") and change the fact bank yourself"))
    return out


def skills_lints(text: str) -> list[Lint]:
    """skills-undelimited: a line of the Skills section with six or more words and no delimiter (a group label is shorter)."""
    out = []
    for line in _lines(section(text, "skills")):
        if len(line.split()) >= 6 and not DELIMITERS.search(line):
            out.append(Lint("skills-undelimited", "Skills", f'"{line}"',
                            "the items of this line have no comma or semicolon between them, so a parser reads them as one long skill. Add delimiters "
                            "in the fact bank's skill lines (or the template) yourself"))
    return out


def lints(roles: Sequence[Mapping[str, Any]], text: str) -> list[Lint]:
    return role_lints(roles) + date_lints(text) + skills_lints(text)


def roles_on_cv(fb: Mapping[str, Any] | None, text: str) -> list[dict]:
    """The fact bank's experience entries whose employer is in the CV's text, in the order the CV shows them (read only)."""
    flat = _norm(text)
    found = []
    for r in (fb or {}).get("experience") or []:
        if isinstance(r, Mapping) and r.get("org") and (at := flat.find(_norm(r["org"]))) >= 0:
            found.append((at, {k: r.get(k) for k in ("id", "title", "org", "dates")}))
    return [r for _, r in sorted(found, key=lambda x: x[0])]


# ------------------------------------------------------------------ one tailored job
@dataclass
class JobStructure:
    cv: Structure | None
    letter: Structure | None
    lints: list[Lint]
    text: str = ""                          # the CV's -layout text (section C reads it)
    roles: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """The CV may go out: it was checked, it has no critical issue and scores PASS_SCORE or more; a letter that exists has no critical issue."""
        return (not self.error and self.cv is not None and self.cv.ok and (self.letter is None or not self.letter.critical))

    def why_not(self) -> str:
        if self.error:
            return f"ATS structure: the check could not run ({self.error})"
        if self.cv is None:
            return "ATS structure: no CV PDF to check"
        bits = []
        if self.cv.critical:
            bits.append(f"{len(self.cv.critical)} critical ({self.cv.critical[0].code})")
        if self.cv.score < PASS_SCORE:
            bits.append(f"{self.cv.score} < {PASS_SCORE}")
        if self.letter is not None and self.letter.critical:
            bits.append(f"letter {self.letter.critical[0].code}")
        return "ATS structure: " + "; ".join(bits) if bits else ""


def for_report(rep: Mapping[str, Any], fb: Mapping[str, Any] | None = None) -> JobStructure:
    """The structural check of a tailored job's CV and letter PDFs, and the lints of the roles it shows. A tool that is missing or a PDF that is not there
    is an `error`, never a pass."""
    keywords = [k.get("term") for k in rep.get("keywords") or [] if isinstance(k, Mapping)]
    cv_pdf, letter_pdf = rep.get("cv_pdf"), rep.get("letter_pdf")
    try:
        if not cv_pdf or not Path(cv_pdf).is_file():
            return JobStructure(None, None, [], error="no CV PDF")
        ex = extract(cv_pdf)
        roles = roles_on_cv(fb, ex.layout)
        tex = Path(cv_pdf).with_suffix(".tex")
        cv = score(ex, kind="cv", roles=roles, keywords=keywords, source=tex.read_text(encoding="utf-8", errors="replace") if tex.is_file() else "")
        letter = check_pdf(letter_pdf, kind="letter") if letter_pdf and Path(letter_pdf).is_file() else None
        return JobStructure(cv, letter, lints(roles, ex.layout), ex.layout, roles)
    except (ToolMissing, OSError) as e:
        return JobStructure(None, None, [], error=str(e)[:160])
