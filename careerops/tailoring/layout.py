"""The CV's length rules, measured on the rendered PDF (never on a string), and the render-time changes that keep to them.

  * bullets  — at most MAX_BULLET_LINES rendered lines each. A longer one is put back to its fact-bank wording, else split at a
               sentence or semicolon boundary into two bullets, else reworded shorter by the model (tailor.shorten_pass); it is
               never cut mid-clause. A bullet that still does not fit is reported, not trimmed.
  * profile  — at most MAX_PROFILE_LINES lines, the hook sentence first; whole sentences after it are dropped until it fits.
  * headline — one line (the 12pt line under the name); the tail is shortened a clause at a time, the trailing stack list first.
  * STAR     — an experience or project entry with `situation:` in the fact bank opens its first bullet with "<situation>: ", unless that
               bullet is `verbatim` (never touched) or the entry shows a `situation_included` bullet (it has its own opener: one per role).
  * links    — a project's GitHub URL is printed in the right-hand tag, or on the stack line when the name leaves no room (the template
               decides with TeX's own box widths); `link_problems` checks the rendered PDF's word boxes: every URL is there, inside the
               margin, and touches no other word.

Measuring uses `pdftotext -bbox-layout` (the line boxes the PDF really has): the 12pt headline lines, the lines between the PROFILE
and SKILLS headings, and the lines of every bullet (a bullet starts at a line whose first word is the bullet dot and goes on
through the indented lines under it). `splits`, the situation opener and the other render-time changes work on a copy of the render
context, like typography.typeset, so the checks that ran before rendering have seen the original wording.
"""
from __future__ import annotations

import copy
import html
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_BULLET_LINES = 3
MAX_PROFILE_LINES = 10               # a 150-word profile (the Document rules' limit) is about nine and a half lines of the 11pt body
DOT = "•"
_LINE = re.compile(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</line>', re.S)
_WORD = re.compile(r"<word [^>]*>(.*?)</word>", re.S)
_PAGE = re.compile(r"<page [^>]*>(.*?)</page>", re.S)
_BOXED_PAGE = re.compile(r'<page width="([\d.]+)" height="[\d.]+">(.*?)</page>', re.S)
_BOXED_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', re.S)
SIDE_MARGIN_PT = 0.6 * 72            # templates/cv2.tex.j2: geometry margin=0.6in


@dataclass
class Word:
    """One word of the PDF with its box in points (pdftotext -bbox), and the width of its page."""
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page_w: float = 595.276


@dataclass
class Line:
    page: int
    x: float
    y: float
    h: float                     # the font's ascent-to-descent box: about the font size in points (22 name, 13 heading, 12 headline, 11 body)
    words: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.words)


def pdf_lines(pdf: Path) -> list[Line]:
    """Every text line of the PDF in reading order. Pieces of one visual line (a title and its right-aligned dates) are one line."""
    xml = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, text=True).stdout
    out: list[Line] = []
    for n, page in enumerate(_PAGE.findall(xml), 1):
        rows: list[Line] = []
        for m in _LINE.finditer(page):
            words = [html.unescape(w) for w in _WORD.findall(m.group(4))]
            x, y0, y1 = float(m.group(1)), float(m.group(2)), float(m.group(3))
            rows.append(Line(n, x, y0, y1 - y0, words))
        rows.sort(key=lambda r: (round(r.y), r.x))
        for r in rows:
            if out and out[-1].page == n and abs(out[-1].y - r.y) < 2.0:
                out[-1].words += r.words
                out[-1].x = min(out[-1].x, r.x)
            else:
                out.append(r)
    return out


def pdf_words(pdf: Path) -> list[Word]:
    """Every word of the PDF with its box, in reading order (`pdftotext -bbox`)."""
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True).stdout
    return [Word(n, float(m[1]), float(m[2]), float(m[3]), float(m[4]), html.unescape(m[5]), float(width))
            for n, (width, body) in enumerate(_BOXED_PAGE.findall(xml), 1) for m in _BOXED_WORD.finditer(body)]


def _touch(a: Word, b: Word, pad: float = 0.3) -> bool:
    return a.page == b.page and a.x0 < b.x1 - pad and b.x0 < a.x1 - pad and a.y0 < b.y1 - pad and b.y0 < a.y1 - pad


def link_problems(pdf: Path, urls: list[str]) -> list[str]:
    """What is wrong with the project links on the rendered PDF, from its word boxes (never from the source text): a URL that is not on the
    page, one that reaches past the right margin, one whose box touches another word's box. [] = all clear."""
    words, out = pdf_words(pdf), []
    for url in urls:
        mine = [w for w in words if w.text == url]
        if not mine:
            out.append(f"{url} is not on the page")
        for w in mine:
            if w.x1 > w.page_w - SIDE_MARGIN_PT + 1.5:
                out.append(f"{url} runs past the right margin")
            clash = [o.text for o in words if o is not w and _touch(w, o)]
            if clash:
                out.append(f"{url} overlaps {', '.join(clash[:3])}")
    return out


def _is_heading(line: Line) -> bool:
    return 12.5 <= line.h < 13.6


def _is_body(line: Line) -> bool:
    return 10.4 <= line.h < 11.6


@dataclass
class Measure:
    headline_lines: int          # 0 = no headline on the page
    profile_lines: int
    bullets: list[int]           # rendered lines of every bullet, in document order (experience, projects, publications, achievements, certifications)


def measure(pdf: Path) -> Measure:
    lines = pdf_lines(pdf)
    first_heading = next((i for i, ln in enumerate(lines) if _is_heading(ln)), len(lines))
    headline = sum(1 for ln in lines[:first_heading] if ln.page == 1 and 11.5 <= ln.h < 12.5)
    profile = 0
    for i, ln in enumerate(lines):
        if _is_heading(ln) and ln.text.strip().upper() == "PROFILE":
            for nxt in lines[i + 1:]:
                if _is_heading(nxt):
                    break
                profile += _is_body(nxt)
            break
    body_x = [ln.x for ln in lines if _is_body(ln) and ln.words and ln.words[0] != DOT]
    left = min(body_x) if body_x else 0.0
    bullets: list[int] = []
    current = 0
    for ln in lines:
        if ln.words and ln.words[0] == DOT:
            if current:
                bullets.append(current)
            current = 1
        elif current and _is_body(ln) and ln.x > left + 6:            # an indented line under the dot: the bullet goes on
            current += 1
        elif current:
            bullets.append(current)
            current = 0
    if current:
        bullets.append(current)
    return Measure(headline, profile, bullets)


# ----------------------------------------------------------------------------------------------------- splits and openers
def sections(ctx: dict):
    """(section, entry) for every experience entry and project of a render context, in document order."""
    for sec in ("experience", "projects"):
        for e in ctx.get(sec) or []:
            yield sec, e


def slots(ctx: dict) -> list[tuple[str, int, int, int]]:
    """The bullets the PDF will show for `ctx` in document order, as (section, entry number, bullet number in the entry, part): a bullet
    in `ctx["splits"]` (and unchanged since) is two slots, part 0 and part 1."""
    splits = ctx.get("splits") or {}
    out = []
    for sec in ("experience", "projects"):
        for n, e in enumerate(ctx.get(sec) or []):
            for i, (bid, text) in enumerate(zip(e["bullet_ids"], e["bullets"])):
                s = splits.get(bid)
                out += [(sec, n, i, 0), (sec, n, i, 1)] if s and s["text"] == text else [(sec, n, i, 0)]
    return out


def with_splits(ctx: dict) -> dict:
    """A copy of `ctx` in which every bullet of `ctx["splits"]` ({bullet id: {"text": the bullet as it was split, "parts": [a, b]}}) is
    its two parts, both under the bullet's id. A split whose bullet has been reworded since is ignored."""
    splits = ctx.get("splits") or {}
    if not splits:
        return ctx
    out = dict(ctx)
    for sec in ("experience", "projects"):
        entries = []
        for e in ctx.get(sec) or []:
            ids, texts = [], []
            for bid, text in zip(e["bullet_ids"], e["bullets"]):
                s = splits.get(bid)
                parts = s["parts"] if s and s["text"] == text else [text]
                ids += [bid] * len(parts)
                texts += parts
            entries.append({**e, "bullet_ids": ids, "bullets": texts})
        out[sec] = entries
    return out


def _lower_first(s: str) -> str:
    """The action text after "<situation>: " starts in lower case, unless its first word is an acronym or a name ("LLM agent", "MCP based")."""
    first = s.split(" ", 1)[0]
    return s if len(first) < 2 or any(c.isupper() for c in first[1:]) or not first[0].isupper() else s[0].lower() + s[1:]


def opener(situation) -> str:
    """The fact bank's `situation:` as the words a first bullet opens with, without closing punctuation."""
    return " ".join(str(situation or "").split()).rstrip(" .:;,")


def with_openers(ctx: dict) -> dict:
    """A copy of `ctx` in which the first bullet of every experience entry and project that has a `situation:` starts with
    "<situation>: " and the (rephrased) action and result after it. Only the first bullet of an entry, once; the model never
    writes a situation, it is the fact bank's own text. Not when the entry shows a bullet with its own opener (`ctx["situated"]`, the
    ids of the `situation_included` bullets: one opener per role), and never on a `verbatim` bullet (`ctx["verbatim"]`)."""
    out = dict(ctx)
    situated, verbatim = set(ctx.get("situated") or ()), set(ctx.get("verbatim") or ())
    for sec in ("experience", "projects"):
        entries = []
        for e in ctx.get(sec) or []:
            sit = opener(e.get("situation"))
            ids = e.get("bullet_ids") or []
            if (sit and e["bullets"] and not e["bullets"][0].startswith(sit) and not situated & set(ids)
                    and not (ids and ids[0] in verbatim)):
                e = {**e, "bullets": [f"{sit}: {_lower_first(e['bullets'][0])}", *e["bullets"][1:]]}
            entries.append(e)
        out[sec] = entries
    return out


# --------------------------------------------------------------------------------------------------------- split points
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_CLAUSE = re.compile(r";\s+")
MIN_PART_WORDS = 4


def _capital(s: str) -> str:
    return s[:1].upper() + s[1:] if s and s[0].islower() and s[1:2].islower() else s


def split_options(text: str) -> list[list[str]]:
    """Every way to cut `text` into two whole bullets at a sentence end or a semicolon, best balanced first. [] when it has neither."""
    text = " ".join(str(text).split())
    cuts: list[tuple[str, str]] = []
    for m in _SENTENCE.finditer(text):
        cuts.append((text[:m.start()], text[m.end():]))
    for m in _CLAUSE.finditer(text):
        cuts.append((text[:m.start()] + ".", text[m.end():]))
    options = [[a.strip(), _capital(b.strip())] for a, b in cuts
               if len(a.split()) >= MIN_PART_WORDS and len(b.split()) >= MIN_PART_WORDS]
    return sorted(options, key=lambda p: abs(len(p[0]) - len(p[1])))


# ------------------------------------------------------------------------------------------------------ headline, profile
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
# " using A, B and C" / " with A, B, C": the LAST preposition that opens a list of at least two items (the first item holds no preposition)
_ITEM = r"(?:(?!\s(?:using|with|on|in|via|across)\s)[^,:;])+"
_TRAILING_LIST = re.compile(r"\s+(?:using|with|on|in|via|across|built with)\s+" + _ITEM + r"(?:,\s*" + _ITEM + r")+(?:,?\s+and\s+" + _ITEM + r")?\s*$",
                            re.I)
_CLAUSE_END = re.compile(r",\s+|\s+(?:and|with|using|on|in|via|across|for|to)\s+", re.I)


def headline_options(headline: str, number: str = "") -> list[str]:
    """Shorter versions of the hook headline "<role> · <tail>", most complete first: the tail without a trailing bracket or stack list,
    without its "problem:" label, then cut back clause by clause, and last the role alone. Versions that lose the hook's `number` come
    after those that keep it; none is ever cut mid-clause. The headline itself is not among them."""
    role, sep, tail = headline.partition(" · ")
    if not sep:
        return []
    seen, keep, lose = {headline}, [], []

    def add(t: str) -> None:
        t = t.strip(" ,;:")
        full = f"{role} · {t}" if t else role
        if t and full not in seen:
            seen.add(full)
            (keep if not number or number in t else lose).append(full)

    t = tail
    for pat in (_TRAILING_PAREN, _TRAILING_LIST):
        t2 = pat.sub("", t)
        if t2 != t:
            t = t2
            add(t)
    if ": " in t:
        add(t.split(": ", 1)[1])
    cuts = [m.start() for m in _CLAUSE_END.finditer(t)]
    for c in reversed(cuts):
        add(t[:c])
    options = keep + lose
    if role not in seen:
        options.append(role)
    return options


def profile_options(profile: str, hook_line: str, min_words: int = 0) -> list[str]:
    """The profile with whole sentences dropped from its end, longest first, the hook sentence (`hook_line`, first in the profile) always
    kept and, with `min_words`, no version under that many words (the Document rules' 100): the profile itself is not among them."""
    rest = profile[len(hook_line):].strip() if hook_line and profile.startswith(hook_line) else ""
    if not rest:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", rest)
    options = [" ".join([hook_line, *sentences[:k]]).strip() for k in range(len(sentences) - 1, -1, -1)]
    return [o for o in options if len(o.split()) >= min_words]


def copy_ctx(ctx: dict) -> dict:
    return copy.deepcopy(ctx)
