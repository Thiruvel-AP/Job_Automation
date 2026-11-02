"""The answer plan of an application form (v2.5): one row per question, made and printed in the TERMINAL before any browser window opens.

    #  req  type      label (short)                         answer (<=60 chars)              source                  status
    1  *    text      First Name                            Thiruvel                         applicant               READY
    7  *    combo     Location (City)                       Bristol -> "Bristol, England, UK" answers:current_location READY
    9       file      Cover Letter                          letter.pdf                       letter                  READY
   10  *    select    What are your working rights ...      Work Visa                        applicant:visa.by_country.GB READY
   11  *    text      ...specify the type of visa ...       -                                answers:visa_type       NEEDS USER
   13  *    textarea  What motivated you to apply ...       (draft, 112 words)               freetext                DRAFT
   17  *    checkbox  I confirm the information is true     -                                attestation             NEEDS USER

The questions come from the Greenhouse question schema (apply/gh_schema.py) or from a scan of the form (apply/formscan.py). Every answer comes from
where the assist has always taken it, and from nowhere else: applicant.yaml (the standard fields, the visa plan of the job's work country, the
equal-opportunities entries you listed), answers.yaml (your own words; `visa_type`, `current_location` ...), this job's CV and letter, and, for a
free-text question, a draft from the fact bank (apply/freetext.py) that you approve before it is used. A drop-down gets an option only through
apply/option_match.py (an exact option, else the one option holding the answer as a whole word); nothing else matches = NEEDS USER with the options
listed. An attestation is never answered. Nothing is invented.

Statuses: READY (an answer to fill), DRAFT (a free-text draft nobody approved yet: the auto-apply gate blocks it), NEEDS USER (only you can answer;
the reason is kept), BLANK (optional and nothing to say: left empty).

`answers_to_paste.txt` is written from the plan as soon as it exists, so a browser that fails later never loses an answer.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import answers as answers_mod
from . import attest, eo, filler, option_match, shortfields
from .handoff import PASTE_FILE

READY, DRAFT, NEEDS_USER, BLANK = "READY", "DRAFT", "NEEDS USER", "BLANK"
RESUME = re.compile(r"resume|résumé|\bcv\b|curr[ií]cul(?:um|o)|lebenslauf", re.I)
COVER = re.compile(r"cover[\s_-]*letter|motivation letter|anschreiben", re.I)
# consent / attestation labels the fill never ticks (the v2.5 addendum's list, beside attest.ATTEST)
CONSENT = re.compile(r"i agree|i consent|i accept|consent to|privacy notice|terms|gdpr|data protection|i (?:have )?read|information (?:is|provided is) true", re.I)
TEXTISH = ("text", "email", "tel", "url", "number", "date", "textarea", "search", "")
CHOICE = ("select", "combobox", "radio", "multiselect")


@dataclass
class Question:
    """One field of the form as the plan sees it, from the schema or from the page."""
    key: str                        # the schema `name` (== DOM id) or the page's native name / id / data-co-field
    type: str                       # text | email | tel | url | number | date | textarea | select | combobox | radio | checkbox | file
    label: str
    required: bool
    options: list[str] = field(default_factory=list)
    max_length: int | None = None
    dom_id: str = ""                # data-co-field when the question came from the page
    origin: str = "page"            # page | schema


@dataclass
class Row:
    n: int
    q: Question
    answer: str = ""                # what is filled (an option's text for a drop-down; the city typed for a place list)
    source: str = ""
    status: str = BLANK
    why: str = ""
    typed: str = ""                 # for a list that is searched by typing (a react-select place list): the text typed before the option is picked
    pick: str = ""                  # "city" | "country" | "hear": the option is picked among the LIVE options at fill time by that rule
    proposal: Any = None            # the freetext.Proposal of a DRAFT row

    @property
    def shown(self) -> str:
        if self.status == DRAFT:
            return f"(draft, {len(self.answer.split())} words)"
        if not self.answer:
            return "-"
        a = " ".join(self.answer.split())
        if self.typed and self.pick:
            a = f"{self.typed} -> {a}" if a != self.typed else f"{self.typed} -> (the option with {self.typed})"
        return a if len(a) <= 60 else a[:57] + "..."

    def line(self) -> str:
        kind = {"combobox": "combo", "multiselect": "select"}.get(self.q.type, self.q.type)
        label = self.q.label if len(self.q.label) <= 38 else self.q.label[:37] + "…"
        return f"{self.n:>3}  {'*' if self.q.required else ' ':<3}  {kind:<9} {label:<38} {self.shown:<60} {self.source:<24} {self.status}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("proposal", None)
        return d


@dataclass
class Plan:
    rows: list[Row]
    origin: str                     # "schema" | "page"
    url: str = ""
    notes: list[str] = field(default_factory=list)

    def by_status(self, status: str) -> list[Row]:
        return [r for r in self.rows if r.status == status]

    @property
    def ready(self) -> bool:
        return not any(r.status in (NEEDS_USER, DRAFT) for r in self.rows if r.q.required)

    def wants_letter(self) -> bool:
        return any(COVER.search(r.q.label) or COVER.search(r.q.key) for r in self.rows if r.q.type in ("file", "textarea"))

    def table(self) -> list[str]:
        head = f"{'#':>3}  {'req':<3}  {'type':<9} {'label (short)':<38} {'answer (<=60 chars)':<60} {'source':<24} status"
        return [head, *[r.line() for r in self.rows]]


@dataclass
class Sources:
    """Where the plan's answers may come from (all read only)."""
    applicant: Mapping[str, Any]
    book: answers_mod.AnswerBook
    job: Mapping[str, Any]
    visa: Any = None                # apply/visa.VisaPlan of the job's work country
    cv_pdf: str | None = None
    letter_pdf: str | None = None
    letter_text: str | None = None
    letter_why: str = ""            # why there is no letter (tailor's letter_reason)
    freetext: Any = None            # apply/freetext.FreeText (None: no drafting, a free-text question is NEEDS USER)
    history: Callable[[str], bool] = lambda company: False
    lever: bool = False             # a Lever form: radio buttons and check boxes are never set by code
    say: Callable[[str], None] = lambda text: None


def questions_from_schema(fields: Sequence[Any]) -> list[Question]:
    """Questions from gh_schema.SchemaField: a schema drop-down is a react-select on the page (a combobox); hidden fields are left out, and so is the
    text box of a question that also has an upload (Greenhouse's "Enter manually" alternative to attaching the CV or the letter)."""
    out = []
    with_file = {f.label for f in fields if f.type == "file"}
    for f in fields:
        if f.type == "hidden" or (f.type == "textarea" and f.label in with_file):
            continue
        typ = "combobox" if f.type in ("select", "multiselect") else f.type
        out.append(Question(f.name, typ, f.label, f.required, list(f.options), origin="schema"))
    return out


def questions_from_page(fields: Sequence[Any]) -> list[Question]:
    """Questions from formscan.DomField."""
    return [Question(f.native_name or f.native_id or f.id, f.type, f.label, f.required, list(f.options), f.max_length, f.id, "page") for f in fields]


def _q(label: str) -> str:
    return " ".join(re.sub(r"[*✱]|\(\s*(?:required|optional)\s*\)", " ", label or "").split())


def _choose(options: Sequence[str], value: str | None) -> str | None:
    return option_match.choose(options, value) if value else None


def _leave(row: Row, why: str, source: str = "") -> Row:
    row.status, row.why, row.source = (NEEDS_USER if row.q.required else BLANK), why, source or row.source
    return row


def resolve(q: Question, n: int, src: Sources, state: dict, has_cover_file: bool) -> Row:
    """The row for one question (see the module docstring for the order). `state` carries what an earlier row decided (how-did-you-hear = Other)."""
    from .assist import about_another_country, is_visa_question, rights_pick, sponsorship_answer

    row = Row(n, q)
    question = _q(q.label)
    label = f"{q.label} | {q.key}"
    job, applicant, book = src.job, src.applicant, src.book
    if q.type == "file":
        cover, resume = bool(COVER.search(label)), bool(RESUME.search(label))
        if cover and resume:
            return _leave(row, "this upload names both the CV and the cover letter: attach the right file yourself", "file")
        if resume:
            if src.cv_pdf:
                row.answer, row.source, row.status = Path(src.cv_pdf).name, "cv", READY
                return row
            return _leave(row, "this job has no CV PDF", "cv")
        if cover:
            if src.letter_pdf:
                row.answer, row.source, row.status = Path(src.letter_pdf).name, "letter", READY
                return row
            return _leave(row, f"no cover letter: {src.letter_why or 'this job has none'}", "letter")
        return _leave(row, "an upload that is neither the CV nor the cover letter: yours to attach", "file")
    if attest.is_attestation(question) or (q.type in ("checkbox", "radio") and CONSENT.search(question)):
        return _leave(row, "attestation: you tick it yourself, code never does", "attestation")
    if q.type == "textarea" and COVER.search(label) and not has_cover_file:
        if src.letter_text:
            row.answer, row.source, row.status = src.letter_text, "letter", READY
            return row
        return _leave(row, f"no cover letter: {src.letter_why or 'this job has none'}", "letter")
    if is_visa_question(f"{question} {q.label}"):
        where = getattr(getattr(src.visa, "work", None), "country", "") or "?"
        row.source = f"applicant:visa.by_country.{where}"
        other = about_another_country(question, src.visa)
        if other:
            return _leave(row, f"the question is about {other}, the job's work country is {where}")
        if q.type in CHOICE or q.options:
            value = sponsorship_answer(src.visa, question)
            if q.options:
                pick = _choose(q.options, value) if value else None
                if pick is None:
                    pick, why = rights_pick(src.visa, question, q.options)
                    if pick is None:
                        return _leave(row, why if not value else f"applicant.yaml says {value!r} and no option matches it ({' | '.join(q.options[:8])})")
                value = pick
            if not value:
                return _leave(row, "applicant.yaml's visa block does not answer this (or no visa plan for this job)")
            if q.type in ("radio", "checkbox") and src.lever:
                return _leave(row, f"Lever: radio buttons are never set by code; you click it, recommended: {value}")
            row.answer, row.status = value, READY
            return row
        if "share code" in question.lower():
            return _leave(row, "a share code is yours to enter")
        summary = getattr(src.visa, "summary", "") if src.visa is not None else ""
        if not summary:
            return _leave(row, "no visa summary in applicant.yaml for this work country")
        row.answer, row.status = summary, READY
        return row
    if eo.is_eo_field(f"{question} {q.label}"):
        entry = eo.entry_for(f"{question} {q.label}", eo.entries(applicant, book))
        row.source = f"equal_opportunities:{entry.key}" if entry else "equal_opportunities"
        if entry is None:
            row.status, row.why = BLANK, "not listed in equal_opportunities: left blank"
            return row
        if q.options:
            pick = eo.choose(q.options, entry)
            if pick is None:
                row.status, row.why = BLANK, f"no option matched match_any ({' | '.join(q.options[:8])}): left blank"
                return row
            if q.type in ("radio", "checkbox") and src.lever:
                return _leave(row, f"Lever: never set by code; you click it, recommended: {pick}")
            row.answer, row.status = pick, READY
            return row
        if q.type in TEXTISH and entry.key.startswith(eo.BOOK_PREFIX) and str(entry.answer).strip():
            row.answer, row.status = str(entry.answer).strip(), READY
            return row
        row.status, row.why = BLANK, "an equal-opportunities question with no option to choose: left blank"
        return row
    kind = shortfields.kind_of(question)
    if kind == "visa_type":
        text, why = shortfields.visa_type_answer(book, job, question)
        row.source = "answers:visa_type"
        if not text:
            return _leave(row, why)
        row.answer, row.status = text, READY
        return row
    if kind == "hear_follow":
        if state.get("hear_other"):
            row.answer, row.source, row.status = shortfields.HEAR_FOLLOW_UP, "how-heard: Other", READY
            return row
        row.status, row.why = BLANK, "the 'please specify' box of how-did-you-hear: only after 'Other'"
        return row
    if kind in ("city", "country"):
        value = shortfields.city_of(applicant, book, job) if kind == "city" else shortfields.country_of(applicant)
        row.source = "answers:current_location" if kind == "city" and book.by_id("current_location", job) else "applicant:location"
        if not value:
            return _leave(row, "answers.yaml `current_location` and applicant.yaml `location` give no " + kind)
        if q.options:
            pick = (shortfields.pick_city_option(q.options, value) or _choose(q.options, value)) if kind == "city" else shortfields.pick_country_option(q.options, value)
            if pick is None:
                return _leave(row, f"no option holds {value!r} ({' | '.join(q.options[:8])})")
            row.answer, row.status = pick, READY
            return row
        row.answer, row.status = value, READY
        row.typed, row.pick = value, kind      # a place list on the page (Greenhouse's location box is one; the schema calls it text): typed, then the live
        return row                             # option that holds it (and the UK, for a city) is picked; a plain text box just gets the value
    if kind == "hear":
        row.source = "how-heard"
        if q.options:
            pick, why = shortfields.hear_choice(q.options, job)
            if pick is None:
                return _leave(row, why)
            state["hear_other"] = answers_mod.norm(pick).startswith("other")
            row.answer, row.status, row.why = pick, READY, why
            return row
        if q.type == "combobox":
            row.answer, row.status, row.pick = "(chosen from the list)", READY, "hear"
            return row
        row.answer, row.status = shortfields.hear_text(job), READY
        return row
    if q.type in filler.STANDARD_TYPES and not filler._matches(label, list(applicant.get("never_fill") or [])):
        key = filler.standard_key(label, applicant)
        if key is not None:
            row.answer, row.source, row.status = filler.standard_value(applicant, key), "applicant", READY
            return row
    r = book.resolve(question, job)
    if r is not None and not r.known and r.entry_id == "previously_applied" and not src.history(str(job.get("company") or "")):
        r = answers_mod.Resolved(r.entry_id, r.pattern, "No", r.numeric, r.options)
    if r is not None and r.known and q.type != "textarea":
        row.source = f"answers:{r.entry_id}"
        if q.options:
            pick = answers_mod.pick_option(q.options, r) or _choose(q.options, r.text)
            if pick is None:
                return _leave(row, f"answers.yaml says {r.text!r} but no option matches it ({' | '.join(q.options[:8])})")
            if q.type in ("radio", "checkbox") and src.lever:
                return _leave(row, f"Lever: never set by code; you click it, recommended: {pick}")
            row.answer, row.status = pick, READY
            return row
        if q.type == "checkbox":
            if answers_mod.norm(r.text) in filler.YES and not src.lever:
                row.answer, row.status = "true", READY
                return row
            return _leave(row, f"answers.yaml says {r.text!r} for a check box")
        text = r.numeric if q.type == "number" and r.numeric else r.text
        if q.type != "textarea":
            short = shortfields.shortest_answer(book, question, job)
            if short is not None and short.known:
                text = short.text
            if shortfields.too_long_for_one_line(text, question):
                return _leave(row, f"the answers.yaml entry {r.entry_id!r} is a paragraph for a one-line box (give it a short form under `options`)")
        row.answer, row.status = text, READY
        return row
    if r is not None and r.known and q.type == "textarea":
        row.answer, row.source, row.status = r.text, f"answers:{r.entry_id}", READY
        return row
    free = q.type == "textarea" or (q.type == "text" and (q.max_length or 0) > 100)
    if free:
        row.source = "freetext"
        if src.freetext is None:
            return _leave(row, "a free-text question: no draft is made here (yours to write)")
        src.say(f"  drafting the answer to: {q.label[:90]}")
        p = src.freetext.propose(question, q.label, q.max_length)
        if p.origin == "reused":
            row.answer, row.source, row.status = p.text, f"library ({p.reused.date})", READY
            return row
        if p.text:
            row.answer, row.status, row.proposal, row.why = p.text, DRAFT, p, p.weak
            return row
        return _leave(row, p.why_not or "no draft passed the guard")
    if filler._matches(label, list(applicant.get("never_fill") or [])):
        return _leave(row, "listed in applicant.yaml never_fill: yours to answer", "never_fill")
    if r is not None and not r.known:
        return _leave(row, f"the entry {r.entry_id!r} in answers.yaml is blank", f"answers:{r.entry_id}")
    return _leave(row, "no answer in applicant.yaml or answers.yaml (type it with [e N], `save` stores it)")


def build(questions: Sequence[Question], src: Sources, origin: str, url: str = "", out: Callable[[str], None] = print) -> Plan:
    """The plan: one resolved row per question, in the form's order."""
    has_cover_file = any(q.type == "file" and COVER.search(f"{q.label} {q.key}") for q in questions)
    state: dict = {}
    rows = []
    src.say = out
    for n, q in enumerate(questions, 1):
        rows.append(resolve(q, n, src, state, has_cover_file))
    return Plan(rows, origin, url)


def paste_text(plan: Plan, job: Mapping[str, Any], cv: str | None, letter: str | None) -> str:
    """answers_to_paste.txt from the plan: every answer in full, then what only you can answer and why."""
    lines = [f"Answers for {job.get('company')} - {job.get('title')}", f"Form: {plan.url or job.get('apply_url') or job.get('url')}",
             f"CV: {cv or '-'}", f"Cover letter: {letter or '-'}", ""]
    for r in plan.rows:
        lines.append(f"{r.n}. {r.q.label}{' *' if r.q.required else ''}  [{r.status}; {r.source or '-'}]")
        if r.answer and r.status in (READY, DRAFT):
            lines.append(("(DRAFT, not approved) " if r.status == DRAFT else "") + r.answer)
        elif r.why:
            lines.append(f"(you answer: {r.why})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_paste(plan: Plan, folder: Path | None, job: Mapping[str, Any], cv: str | None, letter: str | None) -> Path | None:
    """Write answers_to_paste.txt into the job's output folder; None when there is no folder or it cannot be written."""
    if folder is None:
        return None
    path = Path(folder) / PASTE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(paste_text(plan, job, cv, letter), encoding="utf-8", newline="\n")
    except OSError:
        return None
    return path


MENU = "  [y] fill these / [e N] edit answer N / [v N] view full answer N / [q] quit > "


class Quit(Exception):
    """The person chose [q] at the plan: no browser opens."""


def confirm(plan: Plan, ask: Callable[[str], str], out: Callable[[str], None] = print, book_path: Path | None = None,
            on_change: Callable[[Plan], None] | None = None, freetext: Any = None) -> Plan:
    """The prompt under the printed plan (assist / review mode). `y` first shows each DRAFT in full for approval (freetext's own [y] use / [e] edit /
    [r] redraft / [s] I'll write it: an approved one is READY and stored in the answer library, any other is NEEDS USER), then returns. `e N` types a
    new answer for row N; typing `save` afterwards stores it in answers.yaml through the existing `answers.save` path (nothing else writes there).
    `v N` prints row N's whole answer. `q` raises Quit. `on_change(plan)` runs after each change (answers_to_paste.txt is written again)."""
    while True:
        reply = (ask(MENU) or "").strip()
        low = reply.lower()
        m = re.fullmatch(r"([ev])\s*(\d+)", low)
        if low in ("y", "yes"):
            for r in plan.rows:
                if r.status == DRAFT and r.proposal is not None:
                    text = freetext.review(r.proposal, strict=False) if freetext is not None else ""
                    if text:
                        r.answer, r.status, r.source = text, READY, "freetext (approved)"
                    else:
                        r.status, r.why = (NEEDS_USER if r.q.required else BLANK), "the draft was not approved: yours to write"
            if on_change:
                on_change(plan)
            return plan
        if low in ("q", "quit"):
            raise Quit("you chose [q] at the answer plan: no browser was opened")
        if not m:
            out("  y, e N, v N or q")
            continue
        n = int(m.group(2))
        row = next((r for r in plan.rows if r.n == n), None)
        if row is None:
            out(f"  there is no row {n}")
            continue
        if m.group(1) == "v":
            out(f"  {row.n}. {row.q.label}\n  [{row.status}; {row.source or '-'}]" + (f"  options: {' | '.join(row.q.options)}" if row.q.options else ""))
            out("  " + (row.answer or f"(no answer: {row.why or '-'})"))
            continue
        typed = (ask(f"  New answer for {row.n} ({row.q.label[:70]}), blank = keep: ") or "").strip()
        if not typed:
            continue
        if row.q.options:
            pick = option_match.choose(row.q.options, typed)
            if pick is None:
                out(f"  {typed!r} matches none of the options: {' | '.join(row.q.options[:12])}")
                continue
            typed = pick
        row.answer, row.status, row.source, row.why, row.typed, row.pick = typed, READY, "typed", "", "", ""
        if book_path is not None and row.q.type not in ("file", "checkbox") and (ask("  Type `save` to keep it in answers.yaml (anything else: this form only) > ") or "").strip().lower() == "save":
            try:
                entry = answers_mod.save(book_path, _q(row.q.label), typed)
                row.source = f"answers:{entry}"
                out(f"  saved to {book_path.name} as {entry}")
            except (OSError, answers_mod.AnswersError) as e:
                out(f"  not saved: {e}")
        if on_change:
            on_change(plan)
