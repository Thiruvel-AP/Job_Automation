"""The one filler for application-form fields. `apply/assist.py` (a human submits) and `autoapply.py` (the gated auto-submit) both fill a field
through `fill_field`, so a form gets the same answers whichever of them opens it:

  1. the STANDARD fields from applicant.yaml: full name, first name, last name, e-mail, phone, LinkedIn, GitHub, portfolio, location
     (the labels in applicant.yaml `label_map`, plus the built-in ones below; a label in `never_fill` is never a standard field);
  2. everything else from the user's answers.yaml (apply/answers.py): text boxes, drop-downs, radio groups, check boxes, script-built
     drop-downs. An answer is the user's own words or one of the options they listed; nothing is worked out, guessed or generated.

A free-text question that no answers.yaml entry matches ("Describe an LLM feature you have shipped...", required or not) is not answered here: with
`Ctx.loop` set (apply/formloop.py, which both callers set) it is left for the form loop, which drafts it from the fact bank once the whole form has
been scanned; without a loop a REQUIRED one is drafted inline by `Ctx.freetext` as before and an optional one stays empty.
What differs between the two callers is only what happens to a REQUIRED question nobody has answered: `Ctx.strict` (auto-apply) asks in the
terminal and stops the run if the person gives no answer; the assist (not strict) leaves it empty and records it, and its own check
marks it red for the person.

This module never clicks and never submits (a script-built drop-down is filled by the `combobox` function the caller hands in; only
autoapply.py has one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import answers as answers_mod
from . import ats_vendor, attest, shortfields
from . import freetext as freetext_mod

TEXT_TYPES = ("text", "email", "tel", "url", "textarea", "search", "number", "")
STANDARD_TYPES = ("text", "email", "tel", "url", "textarea", "search", "")      # a number box is never a name or an address
YES = ("yes", "y", "true", "tick", "ticked", "agree", "i agree", "accept", "i accept", "consent")

LABEL_JS = """el => {
  const bits = [];
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) bits.push(l.innerText); }
  const wrap = el.closest('label'); if (wrap) bits.push(wrap.innerText);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) lb.split(' ').forEach(i => { const n = document.getElementById(i); if (n) bits.push(n.innerText); });
  if (!bits.length && !el.getAttribute('aria-label') && el.tagName === 'TEXTAREA') {
    // a Lever custom question: the question is the text ABOVE the box (".application-label"), the box itself says only "Type your response"
    for (let n = el.parentElement, i = 0; n && i < 5; n = n.parentElement, i++) {
      if (n.querySelectorAll('input:not([type=hidden]), textarea, select').length > 1) break;
      const c = n.cloneNode(true);
      c.querySelectorAll('textarea, select, input, button, script, style').forEach(x => x.remove());
      const t = (c.textContent || '').split(String.fromCharCode(10033)).join('*').replace(/\\s+/g, ' ').trim();
      if (t) { bits.push(t.slice(0, 300)); break; }
    }
  }
  ['aria-label','placeholder','name','id'].forEach(a => { const v = el.getAttribute(a); if (v) bits.push(v); });
  if (!bits.length) { const p = el.closest('div,fieldset,li'); if (p) bits.push(p.innerText.slice(0,120)); }
  return bits.join(' | ');
}"""

# The group's question for a radio button, whose own label is only its option ("Yes").
QUESTION_JS = """el => {
  const g = el.closest('fieldset, [role=radiogroup], [role=group]');
  if (g) {
    const l = g.querySelector('legend'); if (l) return l.innerText;
    const lb = g.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb.split(' ')[0]); if (n) return n.innerText; }
    return g.getAttribute('aria-label') || g.innerText.slice(0, 200);
  }
  const p = el.closest('div,li,p'); return p ? p.innerText.slice(0, 200) : '';
}"""

# applicant.yaml `label_map` (key -> label regexes) wins; a key it does not list is found by these.
DEFAULT_LABELS = {
    "first_name": [r"first\s*name", r"given\s*name", r"forename"],
    "last_name": [r"last\s*name", r"surname", r"family\s*name"],
    "email": [r"e-?mail"],
    "phone": [r"phone", r"mobile", r"telephone"],
    "location": [r"^location", r"\bcity\b", r"current location"],
    "linkedin": [r"linkedin"],
    "github": [r"github"],
    "website": [r"website", r"portfolio"],
}
# One box for the whole name: "Name", "Full name", "Your name", "Legal name", or a field called _systemfield_name / name / full_name.
# Compared with the label's own words only, so "Company name", "Name of your referee" or "Preferred name" are not it.
FULL_NAME = re.compile(r"^(?:(?:your|full|legal|candidate|applicant|systemfield) )?name$")


def _matches(label: str, patterns: list[str]) -> bool:
    return any(re.search(p, label, re.I) for p in patterns)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class NeedsUser(Exception):
    """The run cannot go on without the person: a challenge on the page, or a question nobody has answered."""


@dataclass
class Ctx:
    job: Mapping[str, Any]
    applicant: Mapping[str, Any]
    book: answers_mod.AnswerBook
    book_path: Path
    plan: Any
    cv_pdf: str | None
    letter_pdf: str | None
    letter_text: str | None
    ask: Callable[[str], str]
    out: Callable[[str], None]
    history: Callable[[str], bool]                    # has the user applied to this company before?
    result: Any                                       # anything with `filled` and `blank` lists (autoapply.AutoResult, or the assist's own)
    strict: bool = True                               # True: a required question nobody answered is asked, and stops the run if not answered
    combobox: Callable[..., None] | None = None       # fills a script-built drop-down (picks an option = a click, which only autoapply.py may do)
    freetext: Any = None                              # apply/freetext.FreeText of this job: drafts, guards and asks about a required free-text question
    tracker: Any = None                               # apply/verify.Tracker: every field filled is noted, then read back once the form is filled
    loop: Any = None                                  # apply/formloop.FormLoop of this form: it answers the free-text fields (required or optional)
    attestations: list = field(default_factory=list)  # the questions of the attestation check boxes (apply/attest.py): never ticked, listed for the human
    recommended: list = field(default_factory=list)   # "question -> answer" of the radios / check boxes code may not set (Lever): the human sets them
    needs_user: dict = field(default_factory=dict)    # question -> why the code left the field empty and only the person can answer it (the required-field table)
    sources: dict = field(default_factory=dict)       # normalised question -> where its value came from (answers:<id> | applicant | freetext | letter | job ...)
    hear_other: bool = False                          # "How did you hear about us?" was answered "Other": the follow-up box gets the careers-page words
    verified: list = field(default_factory=list)      # labels the read-back confirmed (apply/verify.py)
    lost: list = field(default_factory=list)          # labels the form emptied after they were filled
    letter_note: str = ""                             # why a cover-letter field the form asks for has no letter (the guards' own words)

    def source(self, question: str, where: str) -> None:
        """Note where the value of a field came from (the dry-run record, apply/dryrun.py)."""
        self.sources[answers_mod.norm(question)[:80]] = where

    def track(self, el, label: str, kind: str = "text", value: str = "") -> None:
        """Note a field this run filled, so that it is read back (and filled once more if the form emptied it) before anyone is told it is done."""
        if self.tracker is not None:
            self.tracker.add(el, label, kind, value)

    def typed(self, prompt: str) -> str:
        try:
            return (self.ask(prompt) or "").strip()
        except EOFError:
            return ""

    def stop(self, why: str) -> None:
        """The form needs the person here. Strict: raise NeedsUser. Otherwise the field is left as it is and the reason is recorded."""
        if self.strict:
            raise NeedsUser(why)
        self.result.blank.append(why)

    def unknown(self, question: str, why: str, options: list[str] | None = None) -> str:
        """A required question with no exact answer: say so, ask, and return what the user typed. Blank = stop. The answer is saved to
        answers.yaml only when the user then types `save`."""
        self.out(f"\n  QUESTION WITH NO ANSWER: {question}\n  ({why})" + (f"\n  Options: {' | '.join(options[:15])}" if options else ""))
        typed = self.typed("  Your answer for this form (blank = stop here; nothing is guessed): ")
        if not typed:
            raise NeedsUser(f"no answer for the required question: {question}")
        return typed

    def offer_save(self, question: str, typed: str, entry_id: str | None = None) -> None:
        if self.typed("  Type `save` to keep this answer in answers.yaml for next time (anything else: used on this form only): ").lower() == "save":
            try:
                answers_mod.save(self.book_path, question, typed, entry_id)
            except answers_mod.AnswersError as e:
                self.out(f"  not saved: {e}")
            else:
                self.out(f"  saved to {self.book_path.name}")
                self.book = answers_mod.load(self.book_path)


# ------------------------------------------------------------------ the standard fields (applicant.yaml)
def standard_key(label: str, applicant: Mapping[str, Any]) -> str | None:
    """Which applicant.yaml value a field holds, from its label text (`LABEL_JS`: the visible label, then aria-label, placeholder, name, id);
    None when it is not a standard field or applicant.yaml has no value for it. `full_name` is `full_name:` or first + last name."""
    lmap = {**DEFAULT_LABELS, **(applicant.get("label_map") or {})}
    key = next((k for k, pats in lmap.items() if applicant.get(k) and _matches(label, pats)), None)
    if key is None and full_name(applicant) and any(FULL_NAME.match(_norm(answers_mod.norm(p))) for p in label.split("|")):
        return "full_name"                                  # after the others: a "First name" box whose name attribute is "name" is not this
    return key


def full_name(applicant: Mapping[str, Any]) -> str:
    return _norm(str(applicant.get("full_name") or f"{applicant.get('first_name') or ''} {applicant.get('last_name') or ''}"))


def standard_value(applicant: Mapping[str, Any], key: str) -> str:
    return full_name(applicant) if key == "full_name" else str(applicant[key])


def fill_standard(el, typ: str, label: str, applicant: Mapping[str, Any]) -> tuple[str, bool] | None:
    """(key, filled) when `el` is a standard field of applicant.yaml, else None. A box that already holds something is left alone
    (filled False). A label in `never_fill` is never a standard field."""
    if typ not in STANDARD_TYPES or _matches(label, list(applicant.get("never_fill") or [])):
        return None
    key = standard_key(label, applicant)
    if key is None:
        return None
    if (el.input_value() or "").strip():
        return key, False
    el.fill(standard_value(applicant, key))
    return key, True


# ------------------------------------------------------------------ answers.yaml
def decide(ctx: Ctx, question: str, required: bool, *, options: list[str] | None = None, numeric: bool = False, single_line: bool = False) -> str | None:
    """The text to enter (or the option to pick) for a question, from answers.yaml only; None = leave it empty (optional and unanswered, or
    not strict). A required question with no exact answer asks the user (Ctx.unknown) and raises NeedsUser if they give none.
    `single_line`: the box is a one-line input (apply/shortfields.py): the shortest matching answer is used, and a paragraph that is not a
    short form of the question is not pasted into it."""
    r = ctx.book.resolve(question, ctx.job)
    if single_line and options is None and not numeric and r is not None:
        r = shortfields.shortest_answer(ctx.book, question, ctx.job) or r
    why = "no entry in answers.yaml matches this question"
    if r is not None and not r.known and r.entry_id == "previously_applied" and not ctx.history(str(ctx.job.get("company") or "")):
        r = answers_mod.Resolved(r.entry_id, r.pattern, "No", r.numeric, r.options)          # no earlier application to this company in the database or archive
    if r is not None and r.known:
        if options is not None:
            pick = answers_mod.pick_option(options, r)
            if pick is not None:
                return pick
            why = f"answers.yaml says {r.text!r} but no option matches it"
        elif numeric and not r.numeric:
            why = "the field takes a number and the answers.yaml entry has no answer_numeric"
        elif single_line and not numeric and shortfields.too_long_for_one_line(r.text, question):
            why = (f"the answers.yaml entry {r.entry_id!r} is {len(r.text.split())} words, a paragraph for a one-line box that does not ask to explain "
                   f"(give the entry a short form under `options`)")
        else:
            ctx.source(question, f"answers:{r.entry_id}")
            return r.numeric if numeric else r.text
    elif r is not None:
        why = f"the entry {r.entry_id!r} in answers.yaml is blank"
    if not required or not ctx.strict:
        ctx.result.blank.append(question)
        if required:
            ctx.needs_user[question] = why
        return None
    typed = ctx.unknown(question, why, options)
    if options is not None:
        pick = answers_mod.pick_option(options, answers_mod.Resolved("typed", "", typed, None, ()))
        if pick is None:
            raise NeedsUser(f"the answer typed for {question!r} matches none of the options: {' | '.join(options[:15])}")
        ctx.offer_save(question, typed)
        return pick
    ctx.offer_save(question, typed)
    return typed


# ------------------------------------------------------------------ the fields
def is_required(el, label: str) -> bool:
    return el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true" or label.rstrip().endswith("*")


def numeric_only(el, typ: str, label: str) -> bool:
    return typ == "number" or (el.get_attribute("inputmode") or "") in ("numeric", "decimal") or \
        bool(re.search(r"\b(?:numbers? only|digits only|numeric|figure)\b|£|\bGBP\b", label, re.I))


def group_of(page, el, typ: str) -> list:
    name = el.get_attribute("name")
    return page.query_selector_all(f'input[type={typ}][name="{name}"]') if name else [el]


def label_of(el) -> str:
    return _norm((el.evaluate(LABEL_JS) or "").split("|")[0])


def lever_page(page) -> bool:
    """Is the page on a Lever host? Radio buttons and check boxes are never set by code there (a programmatic click triggers Lever's hCaptcha)."""
    return ats_vendor.vendor_of(str(getattr(page, "url", "") or "")) == "lever"


def hand_over(ctx: Ctx, question: str, recommended: str | None) -> None:
    """A choice code may not make: listed for the human with the answer the files recommend."""
    rec = recommended or "your answer"
    ctx.recommended.append(f"{question[:100]} -> {rec}")
    ctx.result.blank.append(f"{question} (Lever: you set it; recommended {rec})")


def choice(page, el, typ: str, question: str, ctx: Ctx, seen: set, required: bool) -> None:
    """A radio group, a group of check boxes, or one check box (a consent line). An attestation is never answered (apply/attest.py) and on a
    Lever page nothing is set: both are listed for the human."""
    name = el.get_attribute("name")
    group = group_of(page, el, typ)
    if typ == "radio" or len(group) > 1:
        if name and ("choice", name) in seen:
            return
        seen.add(("choice", name))
        q = _norm(el.evaluate(QUESTION_JS)).rstrip(" *") or question
        if attest.is_attestation(q):
            attest.mark(group, q, ctx.attestations)
            return
        labels = [label_of(g) for g in group]
        if lever_page(page):
            r = ctx.book.resolve(q, ctx.job)
            hand_over(ctx, q, answers_mod.pick_option(labels, r) if r is not None and r.known else None)
            return
        req = any(is_required(g, q) for g in group)
        pick = decide(ctx, q, req, options=labels)
        if pick is not None:
            chosen = next(g for g in group if label_of(g) == pick)
            chosen.check()
            ctx.track(chosen, q, "check")
            ctx.result.filled.append(f"{q[:80]} -> {pick}")
        return
    if el.is_checked():
        return
    if attest.is_attestation(question):
        attest.mark([el], question, ctx.attestations)
        return
    if lever_page(page):
        r = ctx.book.resolve(question, ctx.job)
        hand_over(ctx, question, r.text if r is not None and r.known else None)
        return
    value = decide(ctx, question, required)
    if value is not None and value.strip().lower() in YES:
        el.check()
        ctx.track(el, question, "check")
        ctx.result.filled.append(f"{question[:80]} -> ticked")
    elif value is not None:
        ctx.source(question, f"answered No (answers.yaml {value!r}: left unticked)")
        ctx.result.blank.append(f"{question} (answer {value!r}: left unticked)")


def select(el, question: str, ctx: Ctx, required: bool) -> None:
    options = [_norm(o.inner_text()) for o in el.query_selector_all("option") if _norm(o.inner_text())]
    pick = decide(ctx, question, required, options=options)
    if pick is not None:
        el.select_option(label=next(o.inner_text() for o in el.query_selector_all("option") if _norm(o.inner_text()) == pick))
        ctx.track(el, question, "select", pick)
        ctx.result.filled.append(f"{question[:80]} -> {pick}")


PROMPT_WORDS = frozenset("type enter write your add provide response answer text here please a an the".split())


def readable_question(question: str) -> bool:
    """False when the label of a text box does not say what is asked: nothing, only a prompt such as "Type your response", or a field name such
    as cards[abc][field0]. A required free-text box with such a label gets no draft (and the person is told), never a silent skip."""
    q = _norm(question).strip(" *:")
    words = re.findall(r"[a-z]+", q.lower())
    if not words or all(w in PROMPT_WORDS for w in words):
        return False
    return not (" " not in q and re.search(r"[\[\]_0-9]", q))


def _answered(ctx: Ctx, question: str) -> bool:
    r = ctx.book.resolve(question, ctx.job)
    return r is not None and r.known


def fill_field(page, el, tag: str, typ: str, label: str, short: str, question: str, required: bool, ctx: Ctx, seen: set) -> None:
    """Fill one visible field that is not a file box, not the cover-letter box, not a visa question and not an equal-opportunities question
    (the callers handle those their own way): a standard field from applicant.yaml, else the answers.yaml answer. What it did is added to
    `ctx.result.filled`; a field it could not answer is left as it is."""
    if typ in ("radio", "checkbox"):
        choice(page, el, typ, question, ctx, seen, required)
        return
    if shortfields.handle(page, el, tag, typ, label, short, question, required, ctx):         # city, country, how did you hear, visa type
        return
    if tag == "input" and el.get_attribute("role") == "combobox" and typ in TEXT_TYPES:       # before the standard fields: typing into a script-built
        if attest.is_attestation(question):                                                   # "I declare the information is true ...": never chosen by code
            attest.mark([el], question, ctx.attestations)
            ctx.needs_user[question] = "attestation: you choose it yourself, code never does"
            return
        if ctx.combobox is not None:                                                          # drop-down without picking its option is lost on blur
            ctx.combobox(page, el, question, ctx, required)
        else:
            ctx.result.blank.append(f"{question} (a script-built drop-down: you pick the option)")
            if required:
                ctx.needs_user[question] = "a script-built drop-down: you pick the option"
        return
    done = fill_standard(el, typ, label, ctx.applicant)
    if done is not None:
        key, filled = done
        if filled:
            ctx.track(el, short, "text", standard_value(ctx.applicant, key))
            ctx.result.filled.append(f"{short} <- {key}")
            ctx.source(question or short, "applicant")
        return
    if typ in TEXT_TYPES and (el.input_value() or "").strip():
        return                                                                  # the form (or a saved profile) already holds something
    if tag == "select":
        select(el, question, ctx, required)
    elif typ in TEXT_TYPES:
        maxlength = el.get_attribute("maxlength")
        if ctx.loop is not None and freetext_mod.is_free_text(typ, maxlength) and not _answered(ctx, question):
            return                                                              # the form loop drafts it (or asks) once the whole form is scanned
        if ctx.freetext is not None and required and freetext_mod.is_free_text(typ, maxlength) and not _answered(ctx, question):
            # a long required question answers.yaml has no answer for: drafted from the fact bank, guarded, and used only when the person says y
            if not readable_question(question):                       # never skip a required box without saying so
                ctx.out(f"  free text: the question of this required box cannot be read (its label says {question[:60]!r}): no draft is offered, it is yours")
                ctx.stop(f"cannot read the question of a required text box ({short}): it is yours to answer")
                return
            text = ctx.freetext.answer(question, label, maxlength, strict=ctx.strict)
            if text:
                el.fill(text)
                ctx.track(el, short, "text", text)
                ctx.result.filled.append(f"{question[:80]} -> free text ({ctx.freetext.last})")
            else:
                ctx.stop(f"no answer for the required question: {question}")
            return
        value = decide(ctx, question, required, numeric=numeric_only(el, typ, label), single_line=(tag == "input"))
        if value is not None:
            el.fill(value)
            ctx.track(el, short, "text", value)
            ctx.result.filled.append(f"{question[:80]} -> {value[:60]}")
    elif required:
        ctx.stop(f"a required field this module does not fill ({typ}): {short}")
