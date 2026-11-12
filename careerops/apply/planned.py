"""The assist, plan first (v2.5): apply/assist.py hands a job here when cli.py gives it a planner (apply/planfirst.Planner).

    questions (schema / headless scan) -> letter -> knock-outs -> answer plan (drafts) -> answers_to_paste.txt -> the plan printed and confirmed
    -> a fresh browser, the form reached and filled from the plan, read back field by field -> the required-field table -> the window brought
    on-screen: `Form ready: tick the declarations and press Submit yourself` -> the confirmation prompt.

It never submits. A closed page, context or browser at any point ends the job with `BROWSER CLOSED: nothing submitted` (answers_to_paste.txt is
already written) and no confirmation prompt. `record` (live --dry-run --record) runs headless with no prompt at all, fills the drafts it made (marked
not approved, never stored), writes the record (apply/dryrun.py + plan.json / plan.txt) and closes.
"""
from __future__ import annotations

from .. import pathstore

from pathlib import Path
from typing import Any

from . import answerplan as AP
from . import answers as answers_mod
from . import attest, browser as browser_mod, dryrun, failures as failures_mod, formfind, formscan, knockout, letter_demand, planfill, planfirst, verify
from .errors import BrowserClosed
from .guard import check_job_files

CONFIRM_PROMPT = "After submitting, paste confirmation/reference (blank = submitted, no ref; 'x' = did not submit): "


def _folder(job: dict, cv: str | None) -> Path | None:
    return Path(pathstore.resolve(job["out_dir"])) if job.get("out_dir") else (Path(cv).parent if cv else None)


def _report(r: dict, plan: AP.Plan, res: planfill.Result) -> None:
    """The result keys live and the apply log read, from the plan and the read-back."""
    for d in res.done:
        q, row = d.row.q, d.row
        if d.status == planfill.VERIFIED:
            (r["uploaded"] if q.type == "file" else r["filled"]).append(f"{q.label[:60]} <- {row.source or '-'}")
        elif q.required and d.status in (AP.NEEDS_USER, planfill.LOST, planfill.NOT_FOUND):
            r["required_empty"].append(q.label[:60])
        elif d.status in (AP.NEEDS_USER, AP.BLANK) and q.type != "file":
            r["manual"].append(q.label[:60])
        if row.source.startswith("applicant:visa"):
            r["visa"].append({"question": q.label, "value": d.value or None})
        if row.source.startswith("equal_opportunities"):
            r["eo"].append({"question": q.label, "value": d.value or None})
        if row.source == "attestation":
            r.setdefault("attestations", []).append(q.label)
    r["required_table"] = [x.as_dict() for x in res.required_rows()]
    r["plan"] = [x.as_dict() for x in plan.rows]
    r["form_errors"] = list(res.errors)


def run(job: dict, rep: dict, applicant: dict, profile_dir: Path, *, plan, cv: str | None, letter: str | None, letter_text: str | None, launch, ask,
        book, history, freetext, handoff, failures, fact_bank, tokens, letter_maker, record: bool, planner: "planfirst.Planner", url: str) -> dict:
    r: dict[str, Any] = {"filled": [], "manual": [], "uploaded": [], "required_empty": [], "visa": [], "eo": []}
    book = book or answers_mod.AnswerBook()
    folder = _folder(job, cv)

    def hand_off(why: str, remember: bool = True) -> None:
        r["handoff"] = why
        if handoff is not None:
            handoff.run(job, rep, applicant, book, plan, url, why, r["filled"], remember=remember)
        else:
            print(f"\n  HAND-OFF: {why}\n  Open this in your own browser: {url}\n  CV: {cv}")

    used_up = failures.gate(job["id"]) if failures is not None else None
    known = (handoff.why(url) if handoff is not None else "") or used_up
    if known:                                                      # a host that turned the automated browser away, or a job out of attempts
        hand_off(known, remember=False)
        r["confirmation"] = "x" if record else ask(CONFIRM_PROMPT).strip()
        return r

    # 1-5: everything in the terminal, before any browser window
    qs = planfirst.questions_for(job, folder, fetch_json=planner.fetch_json, headless=planner.headless, tokens=tokens)
    rep, letter, letter_text, letter_why = planfirst.letter_first(rep, letter, letter_text, letter_maker, planfirst.wants_letter(qs))
    if letter:
        check_job_files(job, cv, letter)
        if freetext is not None and hasattr(freetext, "use_letter"):
            freetext.use_letter(rep)
    if qs.items:
        knocks = knockout.scan([(q.label, q.options) for q in qs.items], applicant, book, job, plan, fact_bank)
        if knocks and not record and not knockout.confirm(knocks, ask, print):
            print(f"\n  Skipped at a knock-out: {knockout.stop_reason(knocks)}")
            r["skipped"], r["confirmation"] = knockout.stop_reason(knocks), "x"
            return r
    src = AP.Sources(applicant, book, job, plan, cv, letter, letter_text, letter_why, freetext, history or (lambda company: False),
                     lever="lever.co" in (url or "").lower())
    the_plan = AP.build(qs.items, src, qs.origin, qs.url) if qs.items else None

    def save_paste(p: AP.Plan) -> Path | None:
        path = AP.write_paste(p, folder, job, cv, letter)
        r["paste_file"] = str(path or "")
        return path

    def plan_and_confirm(p: AP.Plan) -> None:
        planfirst.show(p)
        path = save_paste(p)
        if path:
            print(f"  answers_to_paste.txt written now: {path}")
        if not record:
            AP.confirm(p, ask, print, planner.book_path, on_change=save_paste, freetext=freetext)

    try:
        if the_plan is not None:
            plan_and_confirm(the_plan)
        else:
            print("  the form's questions could not be read before the browser: it is planned from the page, before anything is filled")
    except AP.Quit as e:
        print(f"\n  {e}")
        r["skipped"], r["confirmation"] = str(e), "x"
        return r

    # 6: the browser, only now
    page, close = (launch or browser_mod.launcher(None, headless=record))(profile_dir)
    conf = "x"
    try:
        reached = formscan.reach(page, qs.url or url, out=print)
        stop = formfind.blocker(page)
        if stop:
            raise formfind.HandOff(stop)
        if not reached.ok:
            problem = f"FORM NOT FOUND {qs.url or url}"
            print(f"\n{problem}\nThe page opened but shows no application form; apply by hand (every answer is in answers_to_paste.txt).")
            r["not_found"] = problem
            kind = failures_mod.kind_of(problem)
            if failures is not None and kind and failures.record(job["id"], url, kind, problem) >= failures.limit:
                raise formfind.HandOff(f"{failures.limit} failed attempts on this job: {problem}")
        else:
            if the_plan is None:                                   # no plan before the browser: made from this page now, before anything is filled
                the_plan = AP.build(AP.questions_from_page(reached.fields), src, "page", reached.url)
                plan_and_confirm(the_plan)
            print(f"\nFilling the form from the plan ({reached.url or url}):")
            filler = planfill.Filler(page, reached.frame, job=job, cv=cv, letter=letter)
            res = filler.run(the_plan, reached.fields, src)
            if letter_text and not any(f.type == "file" and AP.COVER.search(f"{f.label} {f.native_id} {f.native_name}") for f in reached.fields):
                letter_demand.deliver(page, rep, letter, letter_text, print, reason=letter_why)       # only "Enter manually": clicked, letter.txt pasted
            _report(r, the_plan, res)
            print()
            for line in res.lines():
                print(line)
            for line in attest.lines(r.get("attestations") or []):
                print(line)
            if res.navigated:
                r["navigated"] = True
            refused = verify.upload_refused(page)
            lost = [d.row.q.label for d in res.done if d.status == planfill.LOST]
            trouble = (f"the form refused an upload: {refused}" if refused else
                       f"the form emptied field(s) after they were filled: {'; '.join(lost[:5])}" if lost else "")
            kind = failures_mod.kind_of(trouble) if trouble else None
            if trouble and failures is not None and kind and failures.record(job["id"], url, kind, trouble) >= failures.limit:
                raise formfind.HandOff(f"{failures.limit} failed attempts on this job: {trouble}")
            stop = formfind.blocker(page)
            if stop:
                raise formfind.HandOff(stop)
            if record:
                try:
                    out_dir = dryrun.folder_for(rep, job)
                    dryrun.record(out_dir, page, job, rep, res.required_rows(required_only=False), cv=cv, letter=letter, letter_text=letter_text,
                                  freetext=freetext)
                    planfill.write_record(out_dir, the_plan, res, page)
                    r["record"] = str(out_dir)
                except OSError as e:
                    print(f"  dry-run record not written: {e}")
            else:
                browser_mod.show(page)
                print("\nForm ready: tick the declarations and press Submit yourself." +
                      (" (The page navigated during the fill: check it first.)" if res.navigated else ""))
        if not record:
            conf = ask(CONFIRM_PROMPT).strip()
    except BrowserClosed as e:
        from .assist import closed_browser

        r["browser_closed"] = str(e) or "the browser was closed"
        closed_browser(job, rep, applicant, book, plan, url, r["filled"], paste=Path(r["paste_file"]) if r.get("paste_file") else None)
        conf = "x"
    except formfind.HandOff as e:
        if failures is not None and failures_mod.kind_of(str(e)) == failures_mod.VERIFICATION_ERROR:
            failures.record(job["id"], url, failures_mod.VERIFICATION_ERROR, str(e))
        hand_off(str(e))
        conf = "x" if record else ask(CONFIRM_PROMPT).strip()
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001  (already closed by the person)
            pass
    r["confirmation"] = conf
    return r
