"""A letter never reuses another letter's body (Document rules, B5).

The body of a letter (P2 to P4: why this company, the two evidence paragraphs) is compared with every earlier letter the pipeline knows:
the letters of the job folders under paths.outputs (their report.json) and the cover letters in the archive folders (paths.applications_auto
and the user's own paths.applications, which are only ever READ). The measure is the Jaccard overlap of 5-word shingles. At LIMIT (0.30) or
more the model is asked once more with `do not reuse these sentences: ...`; a second copy means no letter, and the reason names the letter
it copies. The greeting, the sign-off, sentence 1 (the hook) and the closing question are not counted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .grounding import sentences
from .render import pdf_text

SHINGLE = 5
LIMIT = 0.30
STAGING_SUFFIXES = (".partial", ".old", ".failed")
LETTER_FILE = re.compile(r"cover|letter", re.I)
MAX_FILE_BYTES = 2_000_000
SHARED_SHOWN = 6                       # sentences named in the redraft instruction
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_cache: dict[tuple[str, int, int], str] = {}          # (path, mtime_ns, size) -> text: an archive letter is read once per process


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    """The n-word windows of `text` (lower case, punctuation ignored); a text of fewer than n words is one shingle; "" has none."""
    words = _WORD.findall(str(text).casefold())
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Earlier:
    """One earlier letter: where it came from and the text its overlap is measured on."""
    name: str
    text: str
    shingles: set = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.shingles:
            self.shingles = shingles(self.text)


def _read(path: Path, read_pdf: Callable[[Path], str]) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    if st.st_size > MAX_FILE_BYTES:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _cache:
        try:
            _cache[key] = read_pdf(path) if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  (an unreadable file is not a letter)
            _cache[key] = ""
    return _cache[key]


def _outputs(root: Path, skip: set[str]) -> Iterable[Earlier]:
    if not root.is_dir():
        return
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.endswith(STAGING_SUFFIXES) or folder.name in skip:
            continue
        try:
            letter = json.loads((folder / "report.json").read_text(encoding="utf-8")).get("letter") or {}
        except (OSError, ValueError, AttributeError):
            continue
        paragraphs = letter.get("paragraphs") or []
        if letter.get("status") == "ok" and len(paragraphs) >= 5:
            yield Earlier(folder.name, " ".join(str(p) for p in paragraphs[1:4] if p))


def _archive(root: Path, read_pdf: Callable[[Path], str]) -> Iterable[Earlier]:
    if not root.is_dir():
        return
    for company in sorted(p for p in root.iterdir() if p.is_dir()):
        for role in sorted(p for p in company.iterdir() if p.is_dir()):
            for f in sorted(role.iterdir()):
                if f.is_file() and f.suffix.lower() in (".pdf", ".txt", ".md") and LETTER_FILE.search(f.stem):
                    text = _read(f, read_pdf)
                    if text.strip():
                        yield Earlier(f"{company.name}/{role.name}", text)
                        break                                   # one letter per application folder


def _bare(name: str) -> str:
    """A folder name without its staging suffix (`acme_ds_1.partial` is the folder `acme_ds_1` being written)."""
    for s in STAGING_SUFFIXES:
        if name.endswith(s):
            return name[:-len(s)]
    return name


def earlier_letters(settings, exclude: Iterable[str] = (), read_pdf: Callable[[Path], str] = pdf_text) -> list[Earlier]:
    """Every earlier letter: the outputs folders' (ok) letters' bodies and the archives' cover letters. `exclude` = folder names to leave out
    (the job being written, as `acme_ds_1` or its `.partial` staging name). Reads only; nothing is written anywhere."""
    skip = {_bare(str(x)) for x in exclude}
    out = list(_outputs(settings.path("outputs"), skip))
    for key in ("applications_auto", "applications"):
        try:
            out += list(_archive(settings.path(key), read_pdf))
        except (KeyError, OSError):
            continue
    return out


def worst(body: Sequence[str], earlier: Sequence[Earlier]) -> tuple[float, Earlier | None]:
    """(the highest Jaccard overlap of the body with an earlier letter, that letter); (0.0, None) with none."""
    mine = shingles(" ".join(body))
    best: tuple[float, Earlier | None] = (0.0, None)
    for e in earlier:
        score = jaccard(mine, e.shingles)
        if score > best[0]:
            best = (score, e)
    return best


def shared_sentences(body: Sequence[str], other: Earlier) -> list[str]:
    """The sentences of the body that have a 5-word shingle in common with `other`, in order (at most SHARED_SHOWN, each shortened for display)."""
    out: list[str] = []
    for paragraph in body:
        for s in sentences(paragraph):
            if shingles(s) & other.shingles:
                out.append(s if len(s) <= 160 else s[:157].rstrip() + "...")
    return list(dict.fromkeys(out))[:SHARED_SHOWN]


def problem(score: float, who: Earlier, shared: Sequence[str]) -> str:
    """The guard's message, also the redraft instruction: how much it copies, what, and which sentences not to reuse."""
    quoted = " | ".join(f'"{s}"' for s in shared) or "(none can be named)"
    return f"reuse: the body is {score:.2f} like the letter '{who.name}' (limit {LIMIT:.2f}); do not reuse these sentences: {quoted}"
