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


def env() -> jinja2.Environment:
    e = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "templates"),
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True, autoescape=False, undefined=jinja2.StrictUndefined,
    )
    e.filters["e"] = latex_escape
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


def _finalise(out_dir: Path, stem: str, final_name: str | None) -> Path:
    """Rename <stem>.tex/.pdf to '<final_name>.tex/.pdf' inside out_dir (the working copy, never the archive).
    xelatex only ever sees the fixed stem, so a name with spaces never reaches it."""
    if not final_name:
        return out_dir / f"{stem}.pdf"
    for ext in (".tex", ".pdf"):
        os.replace(out_dir / f"{stem}{ext}", out_dir / f"{final_name}{ext}")
    return out_dir / f"{final_name}.pdf"


def render_cv(ctx: dict, out_dir: Path, final_name: str | None = None) -> Path:
    """Compile as cv.tex, then rename to `final_name` (e.g. 'Ada Lovelace CV') so the file a recruiter sees is
    already the final one. Returns the PDF path."""
    compile_tex(env().get_template("cv.tex.j2").render(**ctx), out_dir, "cv")
    return _finalise(out_dir, "cv", final_name)


def render_letter(ctx: dict, out_dir: Path, final_name: str | None = None) -> Path:
    compile_tex(env().get_template("cover_letter.tex.j2").render(**ctx), out_dir, "letter")
    return _finalise(out_dir, "letter", final_name)
