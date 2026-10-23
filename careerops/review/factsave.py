"""After the job: offer to keep the facts the user typed in review (review's `f`) in the fact bank.

Asked only when the job used provisional facts (its report says a typed fact is on the CV), once per fact:

    Add to fact_bank.yaml under az: Data Scientist, Northwind? "<text>" [y/N/e(dit)]

    y   careerops/factbank_append.py backs the bank up (config/profile/backups/fact_bank.<YYYYMMDD-HHMMSS>.yaml), appends the bullet
        (`<prefix><next number>`, `source: user_<date>`, `terms: [<keyword>]`), validates the file and rolls back if it does not pass.
    e   the user retypes the sentence (their own words again); then the same question, and only a `y` writes it.
    N   (or anything else, or enter) the fact stays in proposed.yaml marked `declined` and is never used again.

Offered whether or not the application was sent. When it was not (the user's `x`, a hand-off, a skip), the text says so in so many words:
this CV never left the machine. Nothing is written without a `y`; this module never touches the fact bank except through factbank_append.
A mock run (`--mock`) never writes the user's real fact bank: it says so and leaves the facts provisional.
"""
from __future__ import annotations

from typing import Callable

from .. import factbank_append as fba
from .. import mockroot
from ..tailoring import provisional as prov
from . import addfacts

PROMPT = 'Add to fact_bank.yaml under {label}? "{text}" [y/N/e(dit)] '
NOT_SENT = "NOT SENT: this CV was not sent ({why})."


def used_facts(store: prov.Store, job_id: str, rep: dict | None) -> list[prov.Fact]:
    """The job's provisional facts that its CV actually uses (the report lists each typed bullet with `on_cv`)."""
    on_cv = {p["id"] for p in (rep or {}).get("provisional") or [] if p.get("on_cv")}
    return [f for f in store.for_job(job_id) if f.bullet_id in on_cv]


def offer(settings, fb: dict, job_id: str, rep: dict | None, ask: Callable[[str], str], *, sent: bool, why: str = "",
          out: Callable[[str], None] = print) -> list[fba.Appended]:
    """Ask, for each provisional fact the job's CV used, whether to add it to the fact bank. Returns what was appended. `sent` False: the
    application was not submitted (`why` says how it ended), and the text says this CV was not sent."""
    store = prov.store(settings)
    facts = used_facts(store, job_id, rep)
    if not facts:
        return []
    if mockroot.is_mock(settings):
        out(f"  {len(facts)} typed fact(s) stay provisional: a --mock run never writes your real fact bank.")
        return []
    path = settings.path("fact_bank")
    out("\n" + (f"You applied with a CV that uses {len(facts)} fact(s) you typed in review. Keep them for next time?" if sent
                else NOT_SENT.format(why=why or "not submitted") + f" It used {len(facts)} fact(s) you typed in review; you can still keep them for next time."))
    done: list[fba.Appended] = []
    for f in facts:
        text = f.text
        label = addfacts.label_for(fb, f.target)
        answer = ask(PROMPT.format(label=label, text=text)).strip().lower()
        if answer in ("e", "edit"):
            retyped = ask("  your wording, one line (enter = keep it as typed): ").strip()
            text = retyped or text
            answer = ask(PROMPT.format(label=label, text=text).replace("[y/N/e(dit)]", "[y/N]")).strip().lower()
        if answer != "y":
            store.mark(f, prov.DECLINED)
            out(f"  not added; '{f.keyword}' stays in proposed.yaml as declined and is never used again.")
            continue
        try:
            done.append(fba.append_fact(path, f.target, text, f.keyword, confirmed="y"))
        except fba.AppendError as e:
            out(f"  NOT added ({f.keyword}): {e}")
            continue
        store.mark(f, prov.SAVED, saved_as=done[-1].bullet_id)
        fba.refresh(fb, path)                                       # the dict the rest of this session holds sees the new bullet
    if done:
        out("Added to fact_bank.yaml:")
        out("\n".join(f"  {a.bullet_id} under {addfacts.label_for(fb, a.target)}: {a.text}" for a in done))
        out("Backup of the fact bank before this change: " + ", ".join(sorted({str(a.backup) for a in done})))
    return done
