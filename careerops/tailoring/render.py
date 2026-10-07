"""LaTeX rendering (xelatex + Carlito) and post-compile checks: page count and extracted text."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2

from ..config import ROOT


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


def env() -> jinja2.Environment:
    e = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "templates"),
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True, autoescape=False, undefined=jinja2.StrictUndefined,
    )
    e.filters["e"] = latex_escape
    e.filters["skill"] = lambda s: latex_escape(skill_display(s))        # a skill entry with commas shows them as "/"
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


# The text area of templates/cv2.tex.j2 (geometry margin=0.6in on A4); change both together. The one-page template is
# never measured: its page count is all that matters.
CV2_MARGIN_PT = 0.6 * 72
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


def render_cv(ctx: dict, out_dir: Path, final_name: str | None = None, one_page: bool = False) -> Path:
    """Compile as cv.tex, then rename to `final_name` (e.g. 'Ada Lovelace CV') so the file a recruiter sees is
    already the final one. Two-page template (cv2.tex.j2) by default, the one-page cv.tex.j2 with `one_page`.
    Returns the PDF path."""
    compile_tex(env().get_template("cv.tex.j2" if one_page else "cv2.tex.j2").render(**ctx), out_dir, "cv")
    return _finalise(out_dir, "cv", final_name)


def render_letter(ctx: dict, out_dir: Path, final_name: str | None = None) -> Path:
    compile_tex(env().get_template("cover_letter.tex.j2").render(**ctx), out_dir, "letter")
    return _finalise(out_dir, "letter", final_name)
