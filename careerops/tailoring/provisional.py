"""Provisional facts: what the user typed, in review, for a JD keyword the fact bank has no evidence for.

    data/facts/proposed.yaml  (settings.path("facts_proposed"))
    facts:
      - {keyword, text (exactly as typed), target (the role or project id it belongs to), job_id, date, status}

A fact lives here and never in the fact bank (the only writer of that is factbank_append.py, after a `y` for the entry). `status`:

    provisional   usable as a source for ITS OWN job only: a re-tailor of that job gets a copy of the fact bank with the fact added as a
                  bullet of `target`, marked `provisional` (and `verbatim`: the model never rewords it, the CV carries the user's own words)
    declined      the user said N when asked to save it: kept in the file, never used again
    saved         the user said y: it is a bullet of the fact bank now (`saved_as` = its id), not provisional any more
    user-cannot-confirm
                  the user said they cannot confirm it (`n` at the question, `c` at the save prompt). PERMANENT and about the KEYWORD, not the job:
                  no later job is asked about that keyword, no fact for it is stored, and a fact already stored for it is not used. Unlike
                  `declined` (this wording was not kept), nothing brings it back; only the user editing this file does.

`augment` is the only way a provisional fact reaches tailoring, and it takes the facts of ONE job (`Store.for_job`): another job's bank is
built without it, so the claim guard (which checks a text against the bullets of the bank it is given) has no such source there.
An answer the user skipped is never stored, so it can never become a claim. Nothing here edits config/profile/.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import yaml

from .faithfulness import deny_hits

PROVISIONAL, DECLINED, SAVED, CANNOT_CONFIRM = "provisional", "declined", "saved", "user-cannot-confirm"
SECTIONS = ("experience", "projects")
ID_PREFIX = "prov"


@dataclass
class Fact:
    keyword: str
    text: str
    target: str
    job_id: str
    date: str
    status: str = PROVISIONAL
    saved_as: str = ""
    n: int = 0                                           # its place in the file: the id of the bullet it becomes is prov<n>

    @property
    def bullet_id(self) -> str:
        return f"{ID_PREFIX}{self.n}"


def keyword_key(keyword: str) -> str:
    """A keyword as the store compares it: lower case, one space between words."""
    return " ".join(str(keyword).lower().split())


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def facts(self) -> list[Fact]:
        if not self.path.exists():
            return []
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return [Fact(str(f["keyword"]), str(f["text"]), str(f["target"]), str(f["job_id"]), str(f["date"]), str(f.get("status") or PROVISIONAL),
                     str(f.get("saved_as") or ""), n) for n, f in enumerate(raw.get("facts") or [], 1)]

    def _save(self, facts: list[Fact]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{k: v for k, v in asdict(f).items() if k != "n" and (v or k != "saved_as")} for f in facts]
        self.path.write_text(yaml.safe_dump({"facts": rows}, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")

    def add(self, keyword: str, text: str, target: str, job_id: str, on: str | None = None) -> Fact:
        """Store one fact, `text` exactly as typed. A blank or multi-line answer is not a fact."""
        if not text.strip() or "\n" in text or "\r" in text:
            raise ValueError("a fact is one non-empty line")
        if self.cannot_confirm(keyword):
            raise ValueError(f"you said you cannot confirm '{keyword}': no fact is stored for it")
        facts = self.facts()
        f = Fact(keyword, text, target, job_id, on or date.today().isoformat(), PROVISIONAL, "", len(facts) + 1)
        self._save([*facts, f])
        return f

    def for_job(self, job_id: str) -> list[Fact]:
        """The facts that are provisional for this job: its own, not declined, not saved, and not for a keyword the user cannot confirm."""
        blocked = self.cannot_confirm_keywords()
        return [f for f in self.facts() if f.job_id == job_id and f.status == PROVISIONAL and keyword_key(f.keyword) not in blocked]

    def cannot_confirm_keywords(self) -> set[str]:
        """The keywords (keyword_key) the user said they cannot confirm, from any job."""
        return {keyword_key(f.keyword) for f in self.facts() if f.status == CANNOT_CONFIRM}

    def cannot_confirm(self, keyword: str) -> bool:
        return keyword_key(keyword) in self.cannot_confirm_keywords()

    def mark_cannot_confirm(self, keyword: str, job_id: str, on: str | None = None) -> Fact:
        """Record, for good, that the user cannot confirm `keyword`. There is no text: nothing was claimed. Every stored fact for the keyword is marked
        too, so the file shows it on each of them."""
        facts = self.facts()
        for f in facts:
            if keyword_key(f.keyword) == keyword_key(keyword) and f.status != SAVED:
                f.status = CANNOT_CONFIRM
        if not any(f.status == CANNOT_CONFIRM and keyword_key(f.keyword) == keyword_key(keyword) for f in facts):
            facts.append(Fact(keyword, "", "", job_id, on or date.today().isoformat(), CANNOT_CONFIRM, "", len(facts) + 1))
        self._save(facts)
        return next(f for f in facts if f.status == CANNOT_CONFIRM and keyword_key(f.keyword) == keyword_key(keyword))

    def mark(self, fact: Fact, status: str, saved_as: str = "") -> None:
        facts = self.facts()
        for f in facts:
            if (f.n, f.text, f.job_id) == (fact.n, fact.text, fact.job_id):
                f.status, f.saved_as = status, saved_as
        self._save(facts)
        fact.status, fact.saved_as = status, saved_as


def deny_problem(fb: dict, text: str) -> str | None:
    """The deny-listed term (the user confirmed they lack it) that `text` names, else None."""
    hit = deny_hits(text, list(fb.get("deny_terms") or []))
    return hit[0] if hit else None


def augment(fb: dict, facts: list[Fact]) -> tuple[dict, list[str]]:
    """(a copy of `fb` with each fact added as the last bullet of its role or project, notes). The copy's bullets are marked
    `provisional: true` and `verbatim: true`; the bank itself is not touched. A fact whose role or project is not in the bank is left
    out, and a note says so."""
    out, notes = copy.deepcopy(fb), []
    for f in facts:
        entry = next((e for sec in SECTIONS for e in out.get(sec) or [] if str(e.get("id")) == f.target), None)
        if entry is None:
            notes.append(f"provisional fact for '{f.keyword}' left out: there is no role or project '{f.target}' in the fact bank")
            continue
        entry.setdefault("bullets", []).append({"id": f.bullet_id, "text": f.text, "terms": [f.keyword], "verbatim": True,
                                                "provisional": True, "source": f"typed {f.date}"})
    return out, notes


def provisional_bullets(fb: dict) -> list[dict]:
    """The bullets of a bank that are provisional facts (`augment` marked them)."""
    return [b for sec in SECTIONS for e in fb.get(sec) or [] for b in e.get("bullets") or [] if b.get("provisional")]


def store(settings) -> Store:
    """The user's proposed facts, at settings.path("facts_proposed") (data/facts/proposed.yaml unless the settings say otherwise)."""
    return Store(settings.path("facts_proposed"))
