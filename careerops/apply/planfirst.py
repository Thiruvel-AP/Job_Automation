"""Plan first, then fill (v2.5): everything the form needs is made in the terminal BEFORE a browser window opens.

The 2 Nov 2026 RightShip run opened the browser, then spent about 90 s writing a cover letter while the window sat on an empty form, and the window
was closed: `FORM FIELDS (0)`, nothing filled. Now, in this order:

  1. the questions: the Greenhouse question schema (apply/gh_schema.py: public board API, robots.txt first, cached in form_schema.json), else a
     HEADLESS scan of the form (apply/formscan.py; a drop-down built by script is opened and its options read, then closed with Escape; nothing is
     typed or filled), else none (the form is planned from the page once the browser is open, before anything is filled);
  2. the cover letter, when the form has a cover-letter field (or the form is not known yet) and the job has none: written now, with progress in the
     terminal (`letter_maker`, the same rules and guards as at tailoring time);
  3. the answer plan (apply/answerplan.py): every answer, the free-text drafts (the motivation answer among them) made now;
  4. answers_to_paste.txt from the plan, at once;
  5. the plan printed, and in the assist `[y] fill these / [e N] edit answer N / [v N] view full answer N / [q] quit` (a draft is approved there,
     one by one, with freetext's own menu); the auto-apply asks nothing (its F4 gate blocks any NEEDS USER or DRAFT); the headless dry run asks
     nothing and fills the drafts it made, marked as not approved;
  6. only then the browser (apply/browser.py), the form reached (apply/formscan.py) and filled from the plan (apply/planfill.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import answerplan as AP
from . import ats_vendor, formscan, gh_schema
from .errors import BrowserClosed

PRESCAN_WAIT_MS = 12_000
OPTIONS_JS = r"""() => { const sel = '.select__menu .select__option, .select__menu-list [role=option], [class*=menu i] [role=option], [role=listbox] [role=option]';
  let els = [...document.querySelectorAll(sel)];
  if (!els.length) els = [...document.querySelectorAll('[role=option]')].filter(o => o.offsetParent !== null);
  return els.map(o => (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean); }"""


@dataclass
class Planner:
    """What the plan-first assist needs from the CLI: the robots-checked JSON fetch (discovery.http.Fetcher.json; None = no network, e.g. --mock),
    a headless browser for the pre-scan (`() -> (page, close)`; None = none), and answers.yaml's path (for `e N` + `save`)."""
    fetch_json: Callable[[str], Any] | None
    headless: Callable[[], tuple] | None
    book_path: Path | None = None


@dataclass
class Questions:
    items: list[AP.Question]
    origin: str                     # "schema" | "page" | "" (not known before the browser)
    url: str = ""
    notes: list[str] = field(default_factory=list)


def form_address(job: Mapping[str, Any], tokens=()) -> str:
    url = str(job.get("apply_url") or job.get("url") or "")
    return ats_vendor.direct_url(url, kind=job.get("apply_kind"), job=job, tokens=tokens) if url else url


def read_combo_options(page, frame, fields: list[formscan.DomField], out: Callable[[str], None] = print) -> None:
    """Open each script-built drop-down that shows no options, read them, close it again with Escape (headless pre-scan only; nothing typed)."""
    for f in fields:
        if f.type != "combobox" or f.options:
            continue
        el = frame.locator(f'[data-co-field="{f.id}"]').first
        try:
            el.click(timeout=3000)
            page.wait_for_timeout(300)
            f.options = [o for o in (el.evaluate(formscan.OPTIONS_FOR_JS) or []) if o][:400]       # its own list, every option (a country list is long)
            page.keyboard.press("Escape")
            el.evaluate("e => e.blur()")
            page.wait_for_timeout(150)
        except Exception as e:  # noqa: BLE001  (a list that loads from a server shows nothing until typed: it is read at fill time)
            from .errors import raise_if_closed
            raise_if_closed(e, page)


def prescan(url: str, launch: Callable[[], tuple], out: Callable[[str], None] = print) -> list[formscan.DomField]:
    """The fields of the form, read in a headless browser that is closed again before anything else happens."""
    page, close = launch()
    try:
        reached = formscan.reach(page, url, out=lambda s: None, wait_ms=PRESCAN_WAIT_MS)
        if not reached.ok:
            return []
        read_combo_options(page, reached.frame, reached.fields, out)
        return reached.fields
    except BrowserClosed:
        return []
    except Exception as e:  # noqa: BLE001  (no browser, the page failed: the form is planned once the browser is open)
        out(f"  headless scan of the form failed ({str(e).splitlines()[0][:120]}): the form is planned from the page once the browser is open")
        return []
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def questions_for(job: Mapping[str, Any], folder: Path | None, *, fetch_json: Callable[[str], Any] | None, headless: Callable[[], tuple] | None,
                  tokens=(), out: Callable[[str], None] = print) -> Questions:
    """The questions of this job's form, before any visible browser: the Greenhouse schema, else the headless scan, else none."""
    url = form_address(job, tokens)
    board = gh_schema.parse(url) or gh_schema.parse(str(job.get("apply_url") or job.get("url") or ""))
    if board is not None:
        out(f"  reading the form's questions from the Greenhouse board API ({board.api_url.split('?')[0]})")
        got = gh_schema.load(folder, board, fetch_json, out)
        if got:
            return Questions(AP.questions_from_schema(got), "schema", url)
    if headless is not None and url:
        out("  reading the form's questions from the page (a headless browser, closed again before anything is filled)")
        fields = prescan(url, headless, out)
        if fields:
            return Questions(AP.questions_from_page(fields), "page", url)
    return Questions([], "", url)


def wants_letter(qs: Questions) -> bool:
    """Does the form have a cover-letter field (True when the form is not known yet: a letter that exists is never in the way)?"""
    if not qs.items:
        return True
    return any(AP.COVER.search(f"{q.label} {q.key}") for q in qs.items if q.type in ("file", "textarea"))


def letter_first(rep: dict, letter: str | None, letter_text: str | None, maker: Callable[[Any], Any] | None, wanted: bool,
                 out: Callable[[str], None] = print) -> tuple[dict, str | None, str | None, str]:
    """(report, letter pdf, letter text, why there is none) after writing a missing letter NOW, before any browser opens."""
    from ..tailoring.letter_reason import letter_reason
    from .letter_demand import letter_text_of

    if letter or not wanted:
        return rep, letter, letter_text, "" if letter else letter_reason(rep.get("letter"))
    if maker is None:
        return rep, None, None, letter_reason(rep.get("letter")) or "this job has no letter"
    out("  writing the cover letter now (before any browser opens) ...")
    res = maker(None)
    if res is not None and getattr(res, "pdf", None):
        new_rep = res.rep if res.rep is not None else rep
        out(f"  cover letter: written now, before any browser opens ({Path(res.pdf).name})")
        return new_rep, res.pdf, res.text or letter_text_of(new_rep, res.pdf), ""
    why = (getattr(res, "reason", "") if res is not None else "") or letter_reason(rep.get("letter")) or "no letter could be written"
    out(f"  cover letter: NONE ({why})")
    return rep, None, None, why


def show(plan: AP.Plan, out: Callable[[str], None] = print) -> None:
    where = {"schema": "the Greenhouse question schema", "page": "a scan of the form"}.get(plan.origin, plan.origin)
    out(f"\nANSWER PLAN ({len(plan.rows)} questions, from {where}; nothing has been typed into any form yet)")
    for line in plan.table():
        out(line)
    need = [r for r in plan.rows if r.q.required and r.status in (AP.NEEDS_USER, AP.DRAFT)]
    out(f"  READY {len(plan.by_status(AP.READY))} · DRAFT {len(plan.by_status(AP.DRAFT))} · NEEDS USER {len(plan.by_status(AP.NEEDS_USER))} · "
        f"BLANK {len(plan.by_status(AP.BLANK))}" + (f"  (required and not ready: {', '.join(str(r.n) for r in need)})" if need else ""))
