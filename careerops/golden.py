"""Compare a pipeline CV with the golden CV (tests/golden/anaplan/cv.txt): which lines are only in one, which only in the other.

    python -m careerops.golden <pipeline CV pdf or txt> [golden cv.txt]

Lines are the text lines of the CV with their whitespace collapsed (a wrapped bullet is joined to its continuation lines first, so a
sentence is one line however the PDF broke it). The report is two lists, nothing more: it judges nothing and changes nothing. A CV
tailored from another fact bank than the one the golden was made from will differ everywhere: the diff is for the same fact bank.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GOLDEN_CV = Path(__file__).resolve().parent.parent / "tests" / "golden" / "anaplan" / "cv.txt"
_BULLET = re.compile(r"^\s*[•·*-]\s+")


def logical_lines(text: str) -> list[str]:
    """The text as logical lines: a bullet line starts one and swallows its indented continuation lines; blank lines separate;
    every other line is its own. Whitespace is collapsed; a bullet mark is kept as "• "."""
    out: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        if _BULLET.match(raw):
            if cur:
                out.append(" ".join(cur))
            cur = ["• " + _BULLET.sub("", line).strip()]
        elif cur and raw.startswith("  ") and not _BULLET.match(raw):
            cur.append(line)                           # an indented continuation of the bullet above
        else:
            if cur:
                out.append(" ".join(cur))
                cur = []
            out.append(line)
    if cur:
        out.append(" ".join(cur))
    return out


def diff_lines(pipeline_text: str, golden_text: str) -> tuple[list[str], list[str]]:
    """(lines only in the pipeline CV, lines only in the golden CV), each in the order they appear."""
    a, b = logical_lines(pipeline_text), logical_lines(golden_text)
    sa, sb = set(a), set(b)
    return [x for x in a if x not in sb], [x for x in b if x not in sa]


def report(pipeline_text: str, golden_text: str) -> str:
    only_pipeline, only_golden = diff_lines(pipeline_text, golden_text)
    out = [f"Lines only in the pipeline CV ({len(only_pipeline)}):", *(f"  + {x}" for x in only_pipeline),
           "", f"Lines only in the golden CV ({len(only_golden)}):", *(f"  - {x}" for x in only_golden)]
    return "\n".join(out)


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from .tailoring.render import pdf_text
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    golden = Path(args[1]) if len(args) > 1 else GOLDEN_CV
    print(report(_read(Path(args[0])), _read(golden)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
