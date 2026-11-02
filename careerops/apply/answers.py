"""config/profile/answers.yaml: the user's own answers to application-form questions.

The file is written by the user; this module reads it and never writes it, except `save`, which appends one entry when the user types
`save` at the prompt for a question that had no answer. A form question gets an answer only from here (or from applicant.yaml, which
autoapply.py handles): nothing is ever worked out, guessed or generated.

  match            whole words, case and punctuation ignored, so "sex" never matches "Essex"; the longest matching pattern wins
  answer           "" (blank) = unknown: the caller stops and asks the user
  answer_numeric   used instead of `answer` when the field only accepts a number
  overrides        company name -> answer, for that company only (the name is compared normalised)
  options          for a dropdown or radio group: the option equal to one of these, else the first option with one of them as whole words
  {source} {company} {role}   filled per job
  motivations      (top level) your reasons, in your words: the only source "Why {company}?" may draw on besides the job ad (apply/freetext.py)
  style            (top level) how free-text answers are written: `avoid` (words never used), `person`, `voice` (apply/freetext.py)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

FILE = "answers.yaml"
ALT_FILE = "answer.yaml"                      # the name the first copy of the file was given: found when answers.yaml does not exist
SOURCE_LABELS = {"manual": "Company website", "gmail_alert": "Job alert email", "gmail": "Job alert email", "reed": "Reed",
                 "adzuna": "Adzuna", "lever_eu": "Lever", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}


class AnswersError(ValueError):
    pass


def norm(text: str) -> str:
    """Lower case, every run of non-alphanumerics a single space: the form questions and the patterns are compared like this."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _whole(needle: str, haystack: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def path_for(settings) -> Path:
    """paths.answers; when that file is missing and `answer.yaml` sits beside it, that one."""
    p = settings.path("answers")
    alt = p.with_name(ALT_FILE)
    return alt if not p.exists() and alt.exists() else p


def source_label(job: Mapping[str, Any]) -> str:
    """How the job was found, in words for a "how did you hear about us" box. A job added by hand is named after the page it is applied on."""
    source = str(job.get("source") or "").lower()
    if source == "manual" and str(job.get("apply_kind") or "") in ("greenhouse", "lever", "ashby"):
        return str(job["apply_kind"]).capitalize()
    return SOURCE_LABELS.get(source) or (source.replace("_", " ").capitalize() if source else "Company website")


@dataclass(frozen=True)
class Entry:
    id: str
    patterns: tuple[str, ...]                     # normalised
    answer: str
    numeric: str | None = None
    overrides: Mapping[str, str] = field(default_factory=dict)       # normalised company -> answer
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolved:
    entry_id: str
    pattern: str                                  # the pattern that matched
    text: str                                     # "" = the entry has no answer for this job
    numeric: str | None
    options: tuple[str, ...]

    @property
    def known(self) -> bool:
        return bool(self.text.strip())


def _fill(text: str, job: Mapping[str, Any]) -> str:
    for key, value in (("{source}", source_label(job)), ("{company}", str(job.get("company") or "")), ("{role}", str(job.get("title") or ""))):
        text = text.replace(key, value)
    return text.strip()


@dataclass
class AnswerBook:
    entries: list[Entry] = field(default_factory=list)
    motivations: list[str] = field(default_factory=list)          # read only: free-text answers about the company are drawn from these and the ad
    style: Mapping[str, Any] = field(default_factory=dict)         # read only: {avoid: [words], person: first, voice: text}

    def resolve(self, question: str, job: Mapping[str, Any]) -> Resolved | None:
        """The entry whose longest pattern is a whole-word run of the question, answered for this job; None when no pattern matches."""
        q = norm(question)
        best: tuple[int, Entry, str] | None = None
        for e in self.entries:
            for p in e.patterns:
                if _whole(p, q) and (best is None or len(p) > best[0]):
                    best = (len(p), e, p)
        if best is None:
            return None
        _, e, pattern = best
        text = e.overrides.get(norm(str(job.get("company") or "")), e.answer)
        return Resolved(e.id, pattern, _fill(text, job), e.numeric, e.options)

    def resolve_all(self, question: str, job: Mapping[str, Any]) -> list[Resolved]:
        """Every entry with a pattern that is a whole-word run of the question (each with its own longest matching pattern), answered for this job.
        `resolve` is the longest of them; a single-line box looks at the rest to find the shortest answer (apply/shortfields.py)."""
        q = norm(question)
        out = []
        for e in self.entries:
            hits = [p for p in e.patterns if _whole(p, q)]
            if hits:
                pattern = max(hits, key=len)
                out.append(Resolved(e.id, pattern, _fill(e.overrides.get(norm(str(job.get("company") or "")), e.answer), job), e.numeric, e.options))
        return out

    def by_id(self, entry_id: str, job: Mapping[str, Any]) -> Resolved | None:
        """The entry with this id, answered for this job, whatever the question says (the first one when the id is used twice); None when absent.
        For a target the code names itself (`visa_type`, `current_location`) whose match words are the user's own."""
        for e in self.entries:
            if e.id == entry_id:
                text = e.overrides.get(norm(str(job.get("company") or "")), e.answer)
                return Resolved(e.id, "", _fill(text, job), e.numeric, e.options)
        return None


def load(path: Path) -> AnswerBook:
    """The user's file as an AnswerBook; a missing file is an empty book (every question unknown). A file that cannot be read says which."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return AnswerBook()
    except (OSError, yaml.YAMLError) as e:
        raise AnswersError(f"{Path(path).name} cannot be read ({e}); nothing was changed") from None
    rows = raw.get("answers") if isinstance(raw, dict) else None
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise AnswersError(f"{Path(path).name}: `answers` must be a list")
    entries = []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict) or not isinstance(r.get("match"), list) or not all(isinstance(m, str) for m in r["match"]):
            raise AnswersError(f"{Path(path).name}: entry {i} ({(r or {}).get('id', '?') if isinstance(r, dict) else '?'}) needs `match` "
                               f"as a list of texts")
        numeric = r.get("answer_numeric")
        entries.append(Entry(
            id=str(r.get("id") or f"entry{i}"), patterns=tuple(p for p in (norm(m) for m in r["match"]) if p),
            answer=str(r.get("answer") if r.get("answer") is not None else ""),
            numeric=None if numeric in (None, "") else str(numeric),
            overrides={norm(str(k)): str(v) for k, v in (r.get("overrides") or {}).items()},
            options=tuple(str(o) for o in (r.get("options") or []))))
    motivations = raw.get("motivations") if isinstance(raw, dict) else None
    style = raw.get("style") if isinstance(raw, dict) else None
    return AnswerBook(entries, [str(m).strip() for m in motivations if str(m).strip()] if isinstance(motivations, list) else [],
                      style if isinstance(style, dict) else {})


def pick_option(options: list[str], r: Resolved) -> str | None:
    """Which of a dropdown's or radio group's option texts answers `r`. With the entry's own `options`: an option equal to one of them, else
    one holding it as whole words (so "Male" is never "Female" and "Man" never "Woman"), in the order the user wrote them. Without: only
    an option equal to the answer. None when nothing fits; the caller then asks the user."""
    shown = [(o, norm(o)) for o in options if norm(o)]
    wanted = [norm(w) for w in r.options] or ([norm(r.text)] if r.known else [])
    for w in wanted:
        for original, n in shown:
            if n == w:
                return original
    if r.options:
        for w in wanted:
            for original, n in shown:
                if _whole(w, n):
                    return original
    return None


def _end_of_answers(text: str) -> int | None:
    """Where a new list item goes when another top-level key follows `answers:` (the user's file has `motivations:` and `style:` after it): the
    start of the comment lines that belong to that next key (or of the key). None = `answers:` is the last key, the item goes at the end."""
    start = re.search(r"^answers:", text, re.M)
    nxt = re.search(r"^(?!answers:)[A-Za-z_][\w-]*:", text[start.end():], re.M) if start else None
    if nxt is None:
        return None
    at = start.end() + nxt.start()
    lines = text[:at].split("\n")
    while len(lines) > 1 and (lines[-2].lstrip().startswith("#")):             # the comment block right above that key belongs to it
        lines.pop(-2)
    return len("\n".join(lines[:-1])) + 1 if len(lines) > 1 else 0


def save(path: Path, question: str, answer: str, entry_id: str | None = None) -> str:
    """Append one entry for `question` to the user's file (only after the user typed `save`); returns its id. Everything above is untouched.
    `entry_id` names a target the code looks up by id (`visa_type`); without it the id is made from the question. An id that already exists is refused."""
    pattern = norm(question)
    if not pattern:
        raise AnswersError("nothing to save: the question has no text")
    if not answer.strip():
        raise AnswersError("nothing to save: the answer is blank")
    named = entry_id is not None
    entry_id = entry_id or "saved_" + pattern.replace(" ", "_")[:40]
    text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else "answers:\n"
    if named and re.search(r"^\s*-\s*id:\s*[\"']?" + re.escape(entry_id) + r"[\"']?\s*$", text, re.M):
        raise AnswersError(f"{Path(path).name} already has an entry with the id {entry_id!r}; nothing was changed")
    empty = re.search(r"^answers:[ \t]*\[[ \t]*\][ \t]*$", text, re.M)       # `answers: []` cannot take a list item below it
    if empty:
        text = text[:empty.start()] + "answers:" + text[empty.end():]
    first = re.search(r"^answers:[ \t]*(?:#.*)?\n(?:[ \t]*(?:#.*)?\n)*?([ \t]*)- ", text, re.M)       # the file's own indent of a list item
    pad = first.group(1) if first else "  "
    block = ("\n" if text and not text.endswith("\n") else "") + (
        f"\n{pad}# saved by autoapply when you typed `save` for this question\n"
        f"{pad}- id: {entry_id}\n{pad}  match: [{json.dumps(pattern)}]\n{pad}  answer: {json.dumps(answer.strip(), ensure_ascii=False)}\n")
    after = _end_of_answers(text)                                              # other top-level keys (motivations, style) may follow the list
    new = text + block if after is None else text[:after].rstrip("\n") + "\n" + block + "\n" + text[after:]
    try:
        rows = (yaml.safe_load(new) or {}).get("answers") or []
        before = (yaml.safe_load(text) or {}).get("answers") or []
    except (yaml.YAMLError, AttributeError) as e:
        raise AnswersError(f"{Path(path).name} was not changed: the new entry would not fit its layout ({e})") from None
    if len(rows) != len(before) + 1 or rows[-1].get("id") != entry_id:
        raise AnswersError(f"{Path(path).name} was not changed: the new entry would not become a separate entry in its layout")
    if empty or after is not None or not Path(path).exists():
        Path(path).write_text(new, encoding="utf-8", newline="\n")
    else:
        with open(path, "a", encoding="utf-8", newline="\n") as f:                # the user's text above is never rewritten
            f.write(block)
    return entry_id
