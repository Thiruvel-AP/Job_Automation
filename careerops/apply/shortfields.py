"""Short structured fields of an application form, answered the same way by the assist (apply/assist.py) and the gated auto-apply (autoapply.py)
because both fill a field through apply/filler.py (v2.4.1, the RightShip Greenhouse form of 31 Oct 2026):

  city          "Location (City)", "City", "Location", "Town": the CITY part of answers.yaml `current_location` ("Bristol, UK" -> "Bristol"), else of
                applicant.yaml `location`. A script-built drop-down (autocomplete) gets the city typed, then the option that holds the city and the
                country ("United Kingdom" / "England" / "UK") is picked. The form's own "Locate me" button is NEVER clicked: it asks the browser for
                the geolocation.
  country       "Country": the country part of applicant.yaml `location`; the option equal to it is picked.
  how_heard     "How did you hear about us?": a job found on a Greenhouse / Lever / Ashby board -> "Company website" / "Careers page" / "Website";
                a LinkedIn alert -> "LinkedIn"; an Indeed alert -> "Indeed"; no such option -> "Other" (and "Company careers page" in the follow-up
                text if one appears). The option chosen is logged. Never the literal word "Greenhouse".
  visa_type     "Please specify the type of visa you hold ... or 'N.A.'": a SHORT answer (8 words at most) that comes only from the `visa_type`
                entry of answers.yaml, which the user writes. No entry (or a blank one) = the field stays empty and is NEEDS USER; the assist then
                offers `save` (the existing rule). It is a separate target from `visa_statement` and never gets the Graduate-visa paragraph.
  length rule   a single-line input whose label asks "what / which / type / specify" gets the shortest answer that matches; a paragraph over 25
                words never goes into a single-line input unless the label asks to "explain" or "describe".

Nothing here guesses an answer, signs in, solves a check or clicks a submit control. The only clicks are on `[role=option]` items of an open list, and a
control whose text names the geolocation, a cloud-drive picker or a submit is refused by `clickable_text` before any click.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Mapping, Sequence

from . import answers as answers_mod

VISA_TYPE_WORDS = 8             # a visa TYPE is a few words ("Graduate visa", "N.A."), never a paragraph
SINGLE_LINE_WORDS = 25          # the most words that go into a single-line input unless the label asks to explain / describe
HEAR_FOLLOW_UP = "Company careers page"

# controls that are never clicked, whatever else matches: the browser's geolocation and the cloud-drive pickers (a list ITEM is also refused
# these); a BUTTON is also refused anything that could submit
NO_ITEM = re.compile(r"locate\s*me|use\s+my\s+(?:current\s+)?location|geolocat|dropbox|google\s*drive|one\s*drive", re.I)
NEVER_CLICK = re.compile(NO_ITEM.pattern + r"|submit|send|apply|absenden|envoyer|enviar|bewerben|postuler", re.I)

_CITY = re.compile(r"^(?:your |current |home )?(?:location(?: city| town)?|city|town)(?: of residence| name)?$")
_COUNTRY = re.compile(r"^(?:your |current |home )?country(?: of residence| region)?$")
_HEAR = re.compile(r"\bhow (?:did|do) you (?:first |initially |originally )?(?:hear|find|learn|come across|discover|know)\b|"
                   r"\bwhere did you (?:first )?(?:hear|find|learn|see)\b|\bsource of (?:your )?application\b|\bhow (?:did|do) you know (?:about )?us\b")
_HEAR_FOLLOW = re.compile(r"\b(?:please )?(?:specify|tell us|give details|elaborate)\b|\bother\b")
_VISA_TYPE = re.compile(r"\b(?:type of (?:work |uk )?visa|visa type|which visa|what visa|visa category|visa (?:you|that you) (?:currently |now )?(?:hold|have)|"
                        r"visa(?: or permit)? (?:class|route))\b")
_WHAT = re.compile(r"\b(?:what|which|type|specify|name)\b")
_EXPLAIN = re.compile(r"\b(?:explain|describe|elaborate|tell us (?:more|about)|in your own words|why|how)\b")
_COUNTRIES = ("united kingdom", "england", "great britain", "uk", "scotland", "wales", "northern ireland")


# ------------------------------------------------------------------ what a label asks
def _q(question: str) -> str:
    return answers_mod.norm(re.sub(r"[*✱]|\(\s*(?:required|optional)\s*\)", " ", question or ""))


def kind_of(question: str) -> str | None:
    """"city" | "country" | "hear" | "hear_follow" | "visa_type" for a label; None for any other question. A label is read after its star and its
    `(required)` are removed. `hear_follow` is the "please specify" box that follows "Other" in a how-did-you-hear list."""
    q = _q(question)
    if _VISA_TYPE.search(q):
        return "visa_type"
    if _HEAR.search(q):
        return "hear"
    if _CITY.fullmatch(q):
        return "city"
    if _COUNTRY.fullmatch(q):
        return "country"
    if _HEAR_FOLLOW.search(q) and re.search(r"\b(?:hear|source|find|heard|referral|how)\b", q):
        return "hear_follow"
    return None


def is_visa_type(question: str) -> bool:
    return kind_of(question) == "visa_type"


# ------------------------------------------------------------------ the answers
def city_of(applicant: Mapping[str, Any], book: answers_mod.AnswerBook | None = None, job: Mapping[str, Any] | None = None) -> str:
    """The city: the part of answers.yaml `current_location` before the first comma, else of applicant.yaml `location`; "" when neither says."""
    raw = ""
    if book is not None:
        r = book.by_id("current_location", job or {})
        raw = r.text if r is not None and r.known else ""
    raw = raw or str(applicant.get("location") or "")
    return raw.split(",")[0].strip()


def country_of(applicant: Mapping[str, Any]) -> str:
    """The country: the last comma-separated part of applicant.yaml `location` ("Bristol, United Kingdom" -> "United Kingdom"); "" when it has no comma."""
    parts = [p.strip() for p in str(applicant.get("location") or "").split(",") if p.strip()]
    return parts[-1] if len(parts) > 1 else ""


def pick_city_option(options: Sequence[str], city: str) -> str | None:
    """The option that holds the city AND a UK country word ("Bristol, England, United Kingdom"); None when no option does."""
    c = answers_mod.norm(city)
    best: tuple[int, str] | None = None
    for o in options:
        n = answers_mod.norm(o)
        if not c or f" {c} " not in f" {n} ":
            continue
        if any(f" {w} " in f" {n} " for w in _COUNTRIES):
            if best is None or len(n) < best[0]:
                best = (len(n), o)
    return best[1] if best else None


def pick_country_option(options: Sequence[str], country: str) -> str | None:
    """The option equal to the country, else the shortest one that holds it as whole words."""
    c = answers_mod.norm(country)
    for o in options:
        if answers_mod.norm(o) == c:
            return o
    held = [o for o in options if c and f" {c} " in f" {answers_mod.norm(o)} "]
    return min(held, key=len) if held else None


# ------------------------------------------------------------------ how did you hear about us
def discovery_channel(job: Mapping[str, Any]) -> str:
    """"board" (a Greenhouse / Lever / Ashby board, or the company's own page), "linkedin", "indeed" or "other", from how the job reached the pipeline."""
    def get(k: str) -> str:
        try:
            return str(job[k] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    source, kind = get("source").lower(), get("apply_kind").lower()
    blob = " ".join([source, get("url"), get("apply_url"), get("description")[:600]]).lower()
    if source in ("gmail_alert", "gmail"):
        if "linkedin" in blob:
            return "linkedin"
        if "indeed" in blob:
            return "indeed"
        return "other"
    if source in ("greenhouse", "lever", "lever_eu", "ashby") or kind in ("greenhouse", "lever", "ashby") or source == "manual":
        return "board"
    return "other"


_BOARD_WORDS = ("company website", "company careers page", "careers page", "careers site", "career page", "company site", "corporate website",
                "your website", "company career", "website", "careers")


def hear_choice(options: Sequence[str], job: Mapping[str, Any]) -> tuple[str | None, str]:
    """(the option to pick, why). A fuzzy match of the job's discovery channel to the list; no match = "Other" when the list has it; else (None, why)."""
    channel = discovery_channel(job)
    opts = [(o, answers_mod.norm(o)) for o in options if answers_mod.norm(o)]
    wanted: Sequence[str] = ()
    if channel == "board":
        wanted = _BOARD_WORDS
    elif channel in ("linkedin", "indeed"):
        wanted = (channel,)
    for w in wanted:
        for o, n in opts:
            if n == w:
                return o, f"{channel} -> '{o}'"
    for w in wanted:
        for o, n in opts:
            if f" {w} " in f" {n} " and "referral" not in n:
                return o, f"{channel} -> '{o}' (contains '{w}')"
    for o, n in opts:
        if n in ("other", "others", "other please specify", "other specify"):
            return o, f"no option for the {channel} channel -> '{o}'"
    return None, f"no option for the {channel} channel and no 'Other' in {list(options)[:8]}"


# ------------------------------------------------------------------ visa type
def visa_type_answer(book: answers_mod.AnswerBook | None, job: Mapping[str, Any], question: str = "") -> tuple[str, str]:
    """(the answer, why not). Only the `visa_type` entry of answers.yaml answers (or the `saved_...` entry the user saved for this very question
    at the prompt); blank, missing, or over VISA_TYPE_WORDS words = ("", the reason)."""
    r = book.by_id("visa_type", job) if book is not None else None
    if r is None and book is not None and question:
        saved = book.resolve(question, job)
        r = saved if saved is not None and saved.entry_id.startswith("saved_") else None
    if r is None:
        return "", "answers.yaml has no `visa_type` entry (a few words: your visa type, or 'N.A.')"
    if not r.known:
        return "", "the `visa_type` entry of answers.yaml is blank"
    n = len(r.text.split())
    if n > VISA_TYPE_WORDS:
        return "", f"the `visa_type` entry is {n} words; a visa type is at most {VISA_TYPE_WORDS}"
    return r.text, ""


# ------------------------------------------------------------------ the length rule for a single-line input
def asks_what(question: str) -> bool:
    return bool(_WHAT.search(_q(question)))


def asks_to_explain(question: str) -> bool:
    return bool(_EXPLAIN.search(_q(question)))


def too_long_for_one_line(text: str, question: str) -> bool:
    """Is `text` a paragraph (over SINGLE_LINE_WORDS words) for a single-line input whose label does not ask to explain or describe?"""
    return len(text.split()) > SINGLE_LINE_WORDS and not asks_to_explain(question)


def shortest_answer(book: answers_mod.AnswerBook, question: str, job: Mapping[str, Any]) -> answers_mod.Resolved | None:
    """The answers.yaml answer for a single-line input: of the entries whose longest matching pattern ties, the one with the shortest text; and for a
    question that asks what / which / type / specify, a SHORT form (one of the entry's `options`, the user's own words) when the text is a paragraph."""
    r = book.resolve(question, job)
    if r is None:
        return None
    if not r.known:
        return r
    same = [c for c in book.resolve_all(question, job) if c.known and len(c.pattern) == len(r.pattern)]
    best = min(same or [r], key=lambda c: len(c.text.split()))
    if too_long_for_one_line(best.text, question):
        shorts = [o for c in (same or [r]) for o in c.options if o.strip()]          # the user's own short forms (a notice-period list: "Immediately")
        short = min(shorts, key=lambda o: len(o.split()), default="")
        if short and not too_long_for_one_line(short, question):
            return answers_mod.Resolved(best.entry_id, best.pattern, short, best.numeric, best.options)
    return best


# ------------------------------------------------------------------ the one click
def clickable_text(text: str) -> bool:
    """False for a control that names the geolocation ("Locate me"), a cloud-drive picker (Dropbox / Google Drive) or a submit."""
    return not NEVER_CLICK.search(text or "")


# ------------------------------------------------------------------ the page: a script-built drop-down (role=combobox)
# The text a combobox shows as chosen (a react-select keeps it in a sibling of the input, never in the input's value); the input's own value when
# no such sibling is found. Up to five ancestors are read so the list's container is found whatever the library calls its classes, but never an
# ancestor that holds another visible field: that would be some other field's value.
COMBO_VALUE_JS = """el => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  let node = el;
  for (let i = 0; node && i < 5; i++, node = node.parentElement) {
    if (node.querySelectorAll('input:not([type=hidden]), textarea, select').length > 1) break;
    const v = node.querySelectorAll('[class*="single-value" i], [class*="singleValue" i], [class*="multi-value__label" i], [class*="multiValue" i] [class*="label" i]');
    if (v.length) return [...v].map(x => clean(x.innerText || x.textContent)).filter(Boolean).join(', ');
  }
  return clean(el.value || '');
}"""
OPTION_WAIT_MS = 500


def combo_value(el) -> str:
    """What a script-built drop-down shows as chosen ("" = nothing, or the box cannot be read)."""
    try:
        return " ".join(str(el.evaluate(COMBO_VALUE_JS) or "").split())
    except Exception:  # noqa: BLE001  (a detached node)
        return ""


def list_options(page) -> list[tuple[str, Any]]:
    """(text, element) of every visible `[role=option]` of an open list, except an item that names the geolocation or a cloud drive."""
    out = []
    try:
        for o in page.query_selector_all("[role=option]"):
            if o.is_visible():
                text = " ".join((o.inner_text() or "").split())
                if text and not NO_ITEM.search(text):
                    out.append((text, o))
    except Exception:  # noqa: BLE001  (the page moved on)
        return []
    return out


def _open(page, el, typed: str | None) -> list[tuple[str, Any]]:
    """Open the list (a click on the box) and, when `typed` is given, type it to filter; the visible options after a short wait."""
    el.click()
    if typed:
        el.fill("")
        el.fill(typed)
    page.wait_for_timeout(OPTION_WAIT_MS)
    return list_options(page)


def _choose(page, el, options: list[tuple[str, Any]], pick: str) -> bool:
    for text, o in options:
        if text == pick and not NO_ITEM.search(text):
            o.click()
            page.wait_for_timeout(120)
            return True
    return False


def _leave(ctx, question: str, why: str) -> None:
    """A field the code could not answer: it stays empty, is NEEDS USER in the required-field table, and (auto) stops the run before any submit."""
    ctx.needs_user[question] = why
    ctx.out(f"  NEEDS USER: {question[:90]}: {why}")
    ctx.stop(f"{question[:80]}: {why}")


def _fill_text(el, ctx, short: str, text: str) -> None:
    el.fill(text)
    ctx.track(el, short, "text", text)
    ctx.result.filled.append(f"{short} <- {text[:60]}")


def _log(ctx, question: str, what: str, **info: Any) -> None:
    loop = getattr(ctx, "loop", None)
    if loop is not None and hasattr(loop, "log_note"):
        loop.log_note(question, what, **info)


def handle(page, el, tag: str, typ: str, label: str, short: str, question: str, required: bool, ctx) -> bool:
    """Answer the field when it is one of the short structured ones (city, country, how did you hear, visa type); True = handled (filled, or left
    empty and recorded as NEEDS USER), False = not one of them, the filler goes on."""
    kind = kind_of(question)
    if kind is None or typ in ("radio", "checkbox", "file", "hidden", "submit", "button"):
        return False
    combo = tag == "input" and el.get_attribute("role") == "combobox"
    held = combo_value(el) if combo else ((el.input_value() or "").strip() if tag != "select" else "")
    if held:
        return True                                                               # the form (or an earlier pass) already holds something

    if kind == "visa_type":
        text, why = visa_type_answer(ctx.book, ctx.job, question)
        if not text and sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)():
            typed = ctx.typed(f"  {question[:140]}\n  Your visa type, a few words (blank = leave it for you; nothing is guessed): ")
            if typed and len(typed.split()) <= VISA_TYPE_WORDS:
                text = typed
                ctx.offer_save(question, typed, entry_id=None if ctx.book.by_id("visa_type", ctx.job) else "visa_type")
            elif typed:
                ctx.out(f"  not used: {len(typed.split())} words; a visa type is at most {VISA_TYPE_WORDS}")
        if not text:
            _leave(ctx, question, why)
            return True
        _fill_text(el, ctx, short, text)
        _log(ctx, question, "visa_type", value=text)
        return True

    if kind == "hear_follow":                                                       # the "please specify" box that follows "Other"
        if not getattr(ctx, "hear_other", False):
            return False
        _fill_text(el, ctx, short, HEAR_FOLLOW_UP)
        return True

    if kind == "city":
        city = city_of(ctx.applicant, ctx.book, ctx.job)
        if not city:
            _leave(ctx, question, "answers.yaml `current_location` and applicant.yaml `location` give no city")
            return True
        if tag == "select":
            names = [" ".join((o.inner_text() or "").split()) for o in el.query_selector_all("option")]
            pick = pick_city_option(names, city) or next((n for n in names if answers_mod.norm(n) == answers_mod.norm(city)), None)
            if pick is None:
                _leave(ctx, question, f"no option holds {city!r}")
                return True
            el.select_option(label=pick)
            ctx.track(el, short, "select", pick)
            ctx.result.filled.append(f"{short} <- {pick}")
            return True
        if not combo:
            _fill_text(el, ctx, short, city)
            return True
        options = _open(page, el, city)
        pick = pick_city_option([t for t, _ in options], city)
        if pick is None or not _choose(page, el, options, pick):
            _leave(ctx, question, f"typed {city!r} but no listed place holds it with the United Kingdom / England "
                                   f"(options: {', '.join(t for t, _ in options[:6]) or 'none'}); the 'Locate me' button is never used")
            return True
        ctx.track(el, short, "combo", pick)
        ctx.result.filled.append(f"{short} <- {pick}")
        _log(ctx, question, "city", typed=city, chose=pick)
        return True

    if kind == "country":
        country = country_of(ctx.applicant)
        if not country:
            _leave(ctx, question, "applicant.yaml `location` has no country part")
            return True
        if tag == "select":
            names = [" ".join((o.inner_text() or "").split()) for o in el.query_selector_all("option")]
            pick = pick_country_option(names, country)
            if pick is None:
                _leave(ctx, question, f"no option equals {country!r}")
                return True
            el.select_option(label=pick)
            ctx.track(el, short, "select", pick)
            ctx.result.filled.append(f"{short} <- {pick}")
            return True
        if not combo:
            _fill_text(el, ctx, short, country)
            return True
        options = _open(page, el, country)
        pick = pick_country_option([t for t, _ in options], country)
        if pick is None or not _choose(page, el, options, pick):
            _leave(ctx, question, f"typed {country!r} but the list has no such option (options: {', '.join(t for t, _ in options[:6]) or 'none'})")
            return True
        ctx.track(el, short, "combo", pick)
        ctx.result.filled.append(f"{short} <- {pick}")
        _log(ctx, question, "country", typed=country, chose=pick)
        return True

    if kind == "hear":
        if tag == "select":
            names = [" ".join((o.inner_text() or "").split()) for o in el.query_selector_all("option")]
            pick, why = hear_pick([n for n in names if n and not n.lower().startswith("select")], question, ctx)
            if pick is None:
                _leave(ctx, question, why)
                return True
            el.select_option(label=pick)
            ctx.track(el, short, "select", pick)
        elif combo:
            options = _open(page, el, None)
            pick, why = hear_pick([t for t, _ in options], question, ctx)
            if pick is None or not _choose(page, el, options, pick):
                _leave(ctx, question, why)
                return True
            ctx.track(el, short, "combo", pick)
        else:
            text = hear_text(ctx.job)
            _fill_text(el, ctx, short, text)
            ctx.out(f"  how did you hear about us: wrote {text!r}")
            _log(ctx, question, "hear", chose=text)
            return True
        ctx.hear_other = answers_mod.norm(pick).startswith("other")
        ctx.result.filled.append(f"{short} <- {pick}")
        ctx.out(f"  how did you hear about us: chose {pick!r} ({why})")
        _log(ctx, question, "hear", chose=pick, why=why)
        return True
    return False


def hear_pick(options: Sequence[str], question: str, ctx) -> tuple[str | None, str]:
    """(the option to pick, why) for a how-did-you-hear list. The user's own answers.yaml answer comes first: one that is not the default `{source}` (say
    "A friend") is used when an option equals it and otherwise NOT guessed (None). With the default, an option equal to where the job came from ("Greenhouse",
    if the form lists it) is picked, else the discovery-channel mapping of `hear_choice` (Company website / Careers page / LinkedIn / Indeed / Other)."""
    names = [o for o in options if o]
    r = ctx.book.resolve(question, ctx.job) if ctx.book is not None else None
    if r is not None and r.known and r.text != answers_mod.source_label(ctx.job):
        pick = answers_mod.pick_option(names, r)
        return (pick, f"your answers.yaml answer {r.text!r} -> '{pick}'") if pick else (None, f"answers.yaml says {r.text!r} but no option matches it")
    label = answers_mod.source_label(ctx.job)
    for o in names:
        if answers_mod.norm(o) == answers_mod.norm(label):
            return o, f"the job came from {label} -> '{o}'"
    return hear_choice(names, ctx.job)


def hear_text(job: Mapping[str, Any]) -> str:
    """The words for a how-did-you-hear TEXT box: the channel's own name for LinkedIn / Indeed, else the careers page."""
    return {"linkedin": "LinkedIn", "indeed": "Indeed"}.get(discovery_channel(job), HEAR_FOLLOW_UP)


def is_combobox(el, typ: str) -> bool:
    """True for a script-built drop-down: a text input with role=combobox."""
    return typ in ("text", "search", "") and (el.get_attribute("role") or "") == "combobox"


def visa_combobox(page, el, question: str, plan, ctx) -> tuple[str | None, str]:
    """(the option picked, why not) for a working-rights drop-down built by script. The list is opened and read, `assist.rights_pick` chooses among its
    options from applicant.yaml (never answers.yaml, never a guess), and that option is clicked. Nothing is typed: the visa plan's summary paragraph is
    never put into a drop-down."""
    from .assist import rights_pick

    held = combo_value(el)
    if held:
        return held, ""
    options = _open(page, el, None)
    pick, why = rights_pick(plan, question, [t for t, _ in options])
    if pick is None:
        return None, why
    if not _choose(page, el, options, next((t for t, _ in options if " ".join(t.split()).lower() == pick.lower()), pick)):
        return None, f"the list shows no option {pick!r}"
    ctx.track(el, question[:60], "combo", pick)
    ctx.result.filled.append(f"{question[:80]} -> {pick}")
    ctx.source(question, "applicant.yaml visa.by_country")
    _log(ctx, question, "working_rights", chose=pick)
    return pick, ""


def generic_combobox(page, el, question: str, ctx, required: bool) -> None:
    """Any other script-built drop-down: open it, read its options, and pick the one the user's answers.yaml names (an entry's `options`, or an
    option equal to its answer). No entry, or no option equal to it: the field stays empty (NEEDS USER) and nothing is guessed."""
    from . import filler

    options = _open(page, el, None)
    names = [t for t, _ in options]
    want = filler.decide(ctx, question, required, options=names or None) if names else filler.decide(ctx, question, required)
    if want is None:
        if required:
            ctx.needs_user[question] = f"answers.yaml has no answer for it (options: {', '.join(names[:8]) or 'not shown'})"
        return
    if names and _choose(page, el, options, want):
        ctx.track(el, question[:60], "combo", want)
        ctx.result.filled.append(f"{question[:80]} -> {want}")
        return
    options = _open(page, el, want)
    pick = answers_mod.pick_option([t for t, _ in options], answers_mod.Resolved("combobox", "", want, None, ()))
    if pick is None or not _choose(page, el, options, pick):
        _leave(ctx, question, f"the drop-down lists no option equal to {want!r}")
        return
    ctx.track(el, question[:60], "combo", pick)
    ctx.result.filled.append(f"{question[:80]} -> {pick}")
