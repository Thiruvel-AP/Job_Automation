"""The one writer of config/profile/fact_bank.yaml.

The fact bank holds the user's real facts: only the user changes it, with this one exception. A bullet the user TYPED during review
(review/factsave.py asked "Add to fact_bank.yaml under <role>? [y/N/e(dit)]") is appended, word for word, and only when

  * the caller passes `confirmed="y"` for that exact entry (anything else raises NotConfirmed before a single file is touched),
  * a dated backup, config/profile/backups/fact_bank.<YYYYMMDD-HHMMSS>.yaml, was written first (never over an existing file), and
  * the file still loads and validates afterwards (no duplicate ids, the deny list passes: no NEW problem compared with the file before),
    else the backup is put back byte for byte and AppendError says why.

The bullet is inserted as text after the last bullet of its role or project, so every existing line (comments, blank lines, quoting,
line endings) stays byte-identical:

      - id: <prefix><next number>        the prefix of that entry's own bullet ids (az12 -> az13, p_gg5 -> p_gg6)
        text: "<exactly what was typed>"
        terms: [<the JD keyword>]
        source: user_<YYYYMMDD>

Nothing is reworded, reordered or deleted, and no other module writes the file (tests/test_fact_bank_single_writer.py).

v3.4, `add_field`: a plain-language twin of an existing bullet (`plain:`) or of a hook entry's line (`plain_headline:`, `plain_profile_line:`,
`plain_letter_line:`), shown to the user by `careerops plain-draft` and saved only with `confirmed="y"` for that exact text (the candidate as
shown, or the user's own edit of it). It is ONE new line, inserted after the last line of that bullet or entry, at its key indent; the entry's
other lines (its `text:` included) stay byte-identical. The same backup-first, validate-or-restore rules apply, a field that is already there is
never replaced, and the deny list is checked on the new text too.

v3.5.2, `add_field(..., "result", ...)`: a bullet's `result:` the user TYPED at `careerops results` (review/results.py) for a bullet listed under
"Needs a result", word for word, after a `y` for that exact text. Nothing is drafted for the user; the same one-line insert, backup first and
validate-or-restore apply. A hook entry never gets a `result:` here.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from .tailoring.faithfulness import deny_hits
from .tailoring.skills import core_deny_problems

BACKUP_DIR = "backups"
SECTIONS = ("experience", "projects")
PLAIN_FIELDS = ("plain", "plain_headline", "plain_profile_line", "plain_letter_line")
RESULT_FIELDS = ("result",)                     # v3.5.2: a bullet's result, typed by the user at `careerops results`
_ID_LINE = re.compile(r"^(\s*)-\s+id:\s*[\"']?([^\s\"'#]+)[\"']?\s*(?:#.*)?$")
_PLAIN_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/&_-]*$")


class AppendError(Exception):
    """The entry was not added (or was taken out again): the message says why. The fact bank is as it was."""


class NotConfirmed(AppendError):
    """No `y` for this entry: nothing was written."""


@dataclass(frozen=True)
class Appended:
    bullet_id: str
    target: str
    text: str
    backup: Path


# ------------------------------------------------------------------ validation
def bullet_ids(fb: dict) -> list[str]:
    return [str(b.get("id")) for sec in SECTIONS for e in fb.get(sec) or [] for b in e.get("bullets") or []]


def problems(fb: dict) -> list[str]:
    """What is wrong with a loaded fact bank: duplicate entry or bullet ids, a deny-listed term in a bullet, in its terms or in
    skills.core."""
    out: list[str] = []
    seen: set[str] = set()
    for sec in SECTIONS:
        for e in fb.get(sec) or []:
            ids = [("entry", str(e.get("id")))] + [("bullet", str(b.get("id"))) for b in e.get("bullets") or []]
            for kind, i in ids:
                if i in seen:
                    out.append(f"duplicate {kind} id '{i}'")
                seen.add(i)
    deny = list(fb.get("deny_terms") or [])
    for sec in SECTIONS:
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                for d in deny_hits(" ".join([str(b.get("text") or ""), str(b.get("plain") or ""), *[str(t) for t in b.get("terms") or []]]), deny):
                    out.append(f"bullet {b.get('id')} contains deny-listed '{d}'")
    return out + core_deny_problems(fb)


def _load(data: bytes) -> dict:
    try:
        fb = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise AppendError(f"the fact bank no longer loads ({str(e).splitlines()[0] if str(e) else type(e).__name__})") from None
    if not isinstance(fb, dict):
        raise AppendError("the fact bank is not a mapping")
    return fb


# ------------------------------------------------------------------ where and what to insert
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _entry_span(lines: list[str], target: str) -> tuple[int, int] | None:
    """[start, end) of the experience / projects entry with this id: from its `- id:` line to the next entry or the next top-level key."""
    for sec in SECTIONS:
        start = next((i for i, l in enumerate(lines) if l.rstrip("\r\n") == f"{sec}:"), None)
        if start is None:
            continue
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() and not lines[i].startswith((" ", "#", "\t", "-"))), len(lines))
        items = [(i, m.group(1), m.group(2)) for i in range(start + 1, end) if (m := _ID_LINE.match(lines[i].rstrip("\r\n")))]
        if not items:
            continue
        top = min(len(ind) for _, ind, _ in items)
        entries = [(i, name) for i, ind, name in items if len(ind) == top]
        for n, (i, name) in enumerate(entries):
            if name == target:
                return i, (entries[n + 1][0] if n + 1 < len(entries) else end)
    return None


def _insert_at(lines: list[str], span: tuple[int, int]) -> tuple[int, int]:
    """(line index to insert at, indent of the bullet items): after the last content line of the entry's last bullet."""
    start, end = span
    bkey = next((i for i in range(start, end) if lines[i].strip() == "bullets:" or lines[i].lstrip().startswith("bullets:")), None)
    if bkey is None:
        raise AppendError("that entry has no `bullets:` list to add to")
    items = [i for i in range(bkey + 1, end) if lines[i].lstrip().startswith("- ") and _indent(lines[i]) >= _indent(lines[bkey])]
    if not items:
        raise AppendError("that entry has no bullet to continue (an empty `bullets:` list)")
    level = _indent(lines[items[0]])
    last = [i for i in items if _indent(lines[i]) == level][-1]
    stop = last
    for i in range(last + 1, end):
        if lines[i].strip() and _indent(lines[i]) <= level:
            break
        if lines[i].strip():
            stop = i
    return stop + 1, level


def _next_id(fb: dict, target: str, own_ids: list[str]) -> str:
    """<prefix><next number>: the prefix of the entry's own bullet ids (their last one), else the entry id; the number follows the highest
    one with that prefix anywhere in the bank, and skips an id that is taken."""
    prefix = re.sub(r"\d+$", "", own_ids[-1]) if own_ids else target
    taken = set(bullet_ids(fb))
    n = max([int(m.group(1)) for i in taken if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", i))] or [0]) + 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _scalar(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)              # a JSON string is a YAML double-quoted scalar: the bank's own style for `text:`


def _block(bullet_id: str, text: str, keyword: str, stamp: str, level: int, eol: str) -> list[str]:
    pad, key = " " * level, " " * (level + 2)
    term = keyword if _PLAIN_TERM.match(keyword) else _scalar(keyword)
    return [f"{pad}- id: {bullet_id}{eol}", f"{key}text: {_scalar(text)}{eol}", f"{key}terms: [{term}]{eol}", f"{key}source: {stamp}{eol}"]


# ------------------------------------------------------------------ files
def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)                                  # the whole file or the old one: an interrupt never leaves half a fact bank


def _backup(path: Path, data: bytes, now: datetime) -> Path:
    folder = path.parent / BACKUP_DIR
    folder.mkdir(exist_ok=True)
    stamp, n = now.strftime("%Y%m%d-%H%M%S"), 1
    while True:
        dst = folder / (f"fact_bank.{stamp}.yaml" if n == 1 else f"fact_bank.{stamp}-{n}.yaml")
        try:
            with open(dst, "xb") as f:                      # exclusive: an existing backup is never overwritten
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return dst
        except FileExistsError:
            n += 1


def _item_span(lines: list[str], owner: str) -> tuple[int, int, int] | None:
    """(the `- id: <owner>` line, the end of that item, its dash indent) for a bullet of experience / projects or an entry of hook_library."""
    for sec in (*SECTIONS, "hook_library"):
        start = next((i for i, l in enumerate(lines) if l.rstrip("\r\n") == f"{sec}:"), None)
        if start is None:
            continue
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() and not lines[i].startswith((" ", "#", "\t", "-"))), len(lines))
        for i in range(start + 1, end):
            m = _ID_LINE.match(lines[i].rstrip("\r\n"))
            if not m or m.group(2) != owner:
                continue
            level = len(m.group(1))
            stop = i
            for j in range(i + 1, end):
                if lines[j].strip() and not lines[j].lstrip().startswith("#") and _indent(lines[j]) <= level:
                    break
                if lines[j].strip() and _indent(lines[j]) > level:
                    stop = j
            return i, stop + 1, level
    return None


def _find(fb: dict, owner: str) -> dict | None:
    for sec in SECTIONS:
        for e in fb.get(sec) or []:
            for b in e.get("bullets") or []:
                if str(b.get("id")) == owner:
                    return b
    return next((h for h in fb.get("hook_library") or [] if str(h.get("id")) == owner), None)


def add_field(path: Path, owner: str, key: str, text: str, *, confirmed: str | None, now: datetime | None = None) -> Appended:
    """Add `key: "<text>"` (a PLAIN_FIELDS key) to the bullet or hook entry `owner`, after the user's `y` for this exact text. Raises NotConfirmed
    without it (nothing touched); AppendError when the owner is unknown, already has the key, or the result does not validate (restored)."""
    if (confirmed or "").strip().lower() != "y" or confirmed != confirmed.strip():
        raise NotConfirmed("not saved: it needs a `y` for this text")
    if key not in PLAIN_FIELDS + RESULT_FIELDS:
        raise AppendError(f"not saved: '{key}' is not a field this writer adds ({', '.join(PLAIN_FIELDS + RESULT_FIELDS)})")
    path, now = Path(path), now or datetime.now()
    if not text.strip() or "\n" in text or "\r" in text:
        raise AppendError("not saved: the text must be one non-empty line")
    old = path.read_bytes()
    eol = "\r\n" if b"\r\n" in old else "\n"
    lines = old.decode("utf-8").splitlines(keepends=True)
    before = _load(old)
    item = _find(before, owner)
    if item is None:
        raise AppendError(f"not saved: there is no bullet or hook entry '{owner}' in the fact bank")
    if key in RESULT_FIELDS and owner not in bullet_ids(before):
        raise AppendError(f"not saved: '{owner}' is not a bullet (a `{key}:` belongs to a bullet)")
    if str(item.get(key) or "").strip():
        raise AppendError(f"not saved: '{owner}' already has `{key}:` (it is never replaced; edit the file yourself to change it)")
    span = _item_span(lines, owner)
    if span is None:
        raise AppendError(f"not saved: the line of '{owner}' was not found in the file")
    _, at, level = span
    if lines and not lines[at - 1].endswith(("\n", "\r")):
        lines[at - 1] += eol
    lines[at:at] = [f"{' ' * (level + 2)}{key}: {_scalar(text)}{eol}"]
    new = "".join(lines).encode("utf-8")
    backup = _backup(path, old, now)                        # first: the old file, dated
    _write_atomic(path, new)
    try:
        after = _load(new)
        got = _find(after, owner)
        if got is None or str(got.get(key)) != text:
            raise AppendError(f"not saved: the file did not read back `{key}` on '{owner}' as written")
        fresh = [p for p in problems(after) if p not in problems(before)]
        if fresh:
            raise AppendError("not saved: the fact bank would not validate (" + "; ".join(fresh) + ")")
        for sec in SECTIONS:                                # nothing else of the bank changed
            if [{k: v for k, v in b.items() if k != key or str(b.get("id")) != owner} for e in after.get(sec) or [] for b in e.get("bullets") or []] != \
                    [b for e in before.get(sec) or [] for b in e.get("bullets") or []]:
                raise AppendError("not saved: another part of the fact bank read back differently")
    except AppendError as e:
        _write_atomic(path, old)                            # put the backup's bytes back
        raise AppendError(f"{e}. The fact bank is unchanged; the backup is {backup}") from None
    return Appended(owner, owner, text, backup)


def refresh(fb: dict, path: Path) -> None:
    """Reload a fact bank a caller holds in memory, in place, so every holder of the dict sees the appended bullet."""
    fresh = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    fb.clear()
    fb.update(fresh)


def append_fact(path: Path, target: str, text: str, keyword: str, *, confirmed: str | None, now: datetime | None = None) -> Appended:
    """Append `text` (exactly as typed) as a new bullet of the role or project `target`. `confirmed` must be "y": the user's answer to
    "Add to fact_bank.yaml under <target>? ...". Raises NotConfirmed otherwise (nothing touched), AppendError when the entry cannot be
    added or the result does not validate (the file is restored from the backup, which stays)."""
    if (confirmed or "").strip().lower() != "y" or confirmed != confirmed.strip():
        raise NotConfirmed("not added: it needs a `y` for this entry")
    path, now = Path(path), now or datetime.now()
    if not text.strip() or "\n" in text or "\r" in text:
        raise AppendError("not added: the fact must be one non-empty line")
    if not keyword.strip():
        raise AppendError("not added: the fact has no keyword")
    old = path.read_bytes()
    eol = "\r\n" if b"\r\n" in old else "\n"
    lines = old.decode("utf-8").splitlines(keepends=True)
    before = _load(old)
    span = _entry_span(lines, target)
    if span is None:
        raise AppendError(f"not added: there is no role or project '{target}' in the fact bank")
    at, level = _insert_at(lines, span)
    entry = next(e for sec in SECTIONS for e in before.get(sec) or [] if str(e.get("id")) == target)
    new_id = _next_id(before, target, [str(b.get("id")) for b in entry.get("bullets") or []])
    if lines and not lines[at - 1].endswith(("\n", "\r")):
        lines[at - 1] += eol                                # the last line of a file with no final newline
    lines[at:at] = _block(new_id, text, keyword.strip(), "user_" + now.strftime("%Y%m%d"), level, eol)
    new = "".join(lines).encode("utf-8")
    backup = _backup(path, old, now)                        # first: the old file, dated
    _write_atomic(path, new)
    try:
        after = _load(new)
        fresh = [p for p in problems(after) if p not in problems(before)]
        if fresh:
            raise AppendError("not added: the fact bank would not validate (" + "; ".join(fresh) + ")")
    except AppendError as e:
        _write_atomic(path, old)                            # put the backup's bytes back
        raise AppendError(f"{e}. The fact bank is unchanged; the backup is {backup}") from None
    return Appended(new_id, target, text, backup)
