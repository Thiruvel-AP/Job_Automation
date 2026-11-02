"""Reach an application form and read its fields from the page (v2.5). Ideas from career-ops-hq/career-ops `web/src/lib/apply/session.ts`,
`extract.ts` and `diagnose.ts` (20b9fd3); this is our own Python.

  goto         up to 3 attempts with a growing pause (0.8 s, 1.6 s): `domcontentloaded`, then `load` capped at 8 s;
  consent      a cookie / consent banner (OneTrust, Cookiebot, TrustArc, Quantcast, Usercentrics, a cookie banner) gets its own accept / close button
               pressed so it stops covering the form: only a button INSIDE the banner, and never one whose text could submit (shortfields.clickable_text);
  wait         in every frame, up to 12 s for `form input, form textarea, input[type=file], [role=combobox], #application_form`, then 1.2 s to settle:
               nothing is scanned straight away (the 2 Nov RightShip run scanned a page that was not there);
  extract      every frame is read and the frame with the most fields is kept (a form inside an iframe is found). Each field is TAGGED on the live
               page (`data-co-field=<id>`) so the fill finds exactly that element again. A react-select keeps only its role=combobox input;
  lazy form    a result that does not look like an application form (no file, e-mail, name, CV or phone field) is scrolled in 3 steps of 1,200 px,
               back to the top, and read again;
  ATS link     still nothing: a link on the page to a known ATS (greenhouse, ashbyhq, lever.co, /apply ...) whose address does not say "submit" is
               NAVIGATED to (page.goto); nothing on the page is clicked to get there.

A closed page, context or browser raises errors.BrowserClosed at once. Nothing here fills, ticks, submits, signs in or touches a bot check.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from . import shortfields
from .errors import BrowserClosed, raise_if_closed

FORM_SEL = "form input, form textarea, input[type=file], [role=combobox], [class*=application-form i], #application_form"
WAIT_MS = 12_000
SETTLE_MS = 1_200
POLL_MS = 250
GOTO_TRIES = 3
SCROLL_STEPS, SCROLL_PX = 3, 1_200
ATS_LINK = re.compile(r"greenhouse\.io|ashbyhq\.com|lever\.co|workable\.com|smartrecruiters\.com|myworkdayjobs\.com|teamtailor\.com|/apply\b", re.I)

CONSENT_ROOTS = ('#onetrust-banner-sdk, #CybotCookiebotDialog, #truste-consent-track, .qc-cmp2-container, #usercentrics-root, '
                 '[id*="cookie" i][class*="banner" i], [class*="cookie-consent" i], [aria-label*="cookie" i]')
CONSENT_BUTTONS = ("#onetrust-accept-btn-handler", "#onetrust-button-accept-all", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                   "#CybotCookiebotDialogBodyButtonAccept", '.qc-cmp2-button[mode="primary"]', "#truste-consent-button")
CONSENT_TEXT = re.compile(r"^\s*(accept|allow|agree|got it|i agree|accept all|ok|close|dismiss)\b", re.I)

# The structure of the form, every field tagged with data-co-field. Port of extract.ts: labels from aria-label / aria-labelledby / label[for] / a
# wrapping label / the field's wrapper / a nearby heading / placeholder; react-select's dummy inputs dropped; a radio group is one field.
EXTRACT_JS = r"""() => {
  for (const s of document.querySelectorAll('[data-co-field]')) { s.removeAttribute('data-co-field'); s.removeAttribute('data-co-option'); }
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const isUuid = s => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((s || '').trim());
  const isPh = s => /^(start typing|select\b|choose|search\b|type\b|--|please select|e\.?g\.?)/i.test((s || '').trim());
  const ok = s => { const t = (s || '').trim(); return t && !isUuid(t) && !isPh(t) ? t : ''; };
  const pure = node => { if (!node) return ''; const c = node.cloneNode(true);
    c.querySelectorAll('input, select, textarea, option, button, [role=option], [class*="menu" i]').forEach(n => n.remove());
    return clean(c.textContent); };
  const group = el => el.closest('[class*="field-entry" i], [class*="fieldEntry" i], [class*="form-group" i], [class*="question" i], ' +
                                 '[class*="field__" i], [class*="__field" i], fieldset, [class*="field" i]');
  const labelFor = el => {
    const aria = el.getAttribute('aria-label'); if (ok(aria)) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' '); if (ok(t)) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && ok(pure(l))) return pure(l); }
    const pl = el.closest('label'); if (pl && ok(pure(pl))) return pure(pl);
    const g = group(el);
    if (g) { const l = g.querySelector('label, legend, [class*="question-title" i], [class*="heading" i], [class*="label" i], [class*="title" i]');
             if (l && ok(pure(l))) return pure(l); }
    let c = el.parentElement;
    for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
      const l = c.querySelector('label, legend, [class*="label" i], [class*="title" i], h3, h4, h5'); if (l && ok(pure(l))) return pure(l); }
    if (ok(el.placeholder)) return el.placeholder;
    return ok(el.name);
  };
  const groupLabel = (first, options) => {
    const rg = first.closest('[role=radiogroup], fieldset');
    if (rg) {
      const al = rg.getAttribute('aria-label'); if (al) return al;
      const lb = rg.getAttribute('aria-labelledby');
      if (lb) { const t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' '); if (t.trim()) return t; }
      const lg = rg.querySelector('legend'); if (lg && lg.textContent.trim()) return lg.textContent;
    }
    const opt = new Set(options.map(o => o.toLowerCase()));
    const box = group(first) || (first.parentElement && first.parentElement.parentElement) || first.parentElement;
    if (box) for (const c of box.querySelectorAll('label, legend, h1, h2, h3, h4, h5, h6, [class*="question-title" i], [class*="heading" i], [class*="label" i], [class*="title" i], [class*="question" i]')) {
      const t = pure(c); if (t && t.length > 2 && t.length < 300 && !opt.has(t.toLowerCase()) && !isUuid(t) && !isPh(t)) return t; }
    return '';
  };
  const visible = el => !!(el.offsetParent !== null || el.getClientRects().length);
  const fields = [], seen = new Set(); let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase(), itype = (el.type || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(itype)) continue;
    if (!visible(el) && !['radio', 'checkbox', 'file'].includes(itype)) continue;
    if (el.closest('[class*="autofill" i]')) continue;
    const inRS = el.closest('[class*="select__"], .select-shell'), combo = el.getAttribute('role') === 'combobox';
    if (inRS && tag === 'input' && !combo) continue;
    const required = !!(el.required || el.getAttribute('aria-required') === 'true');
    const base = {tag, nativeId: el.id || '', nativeName: el.getAttribute('name') || '', visible: visible(el)};
    const id = `co${n++}`;
    if (combo) { el.setAttribute('data-co-field', id);
      fields.push({...base, id, type: 'combobox', label: clean(labelFor(el)), required, options: [], value: ''}); continue; }
    if (itype === 'radio') {
      const name = el.name;
      if (name && seen.has(name)) { n--; continue; }
      if (name) seen.add(name);
      const grp = name ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(name)}"]`)] : [el];
      const options = grp.map(r => clean(labelFor(r) || r.value)).filter(Boolean);
      grp.forEach((r, i) => { r.setAttribute('data-co-field', id); r.setAttribute('data-co-option', options[i] || String(i)); });
      fields.push({...base, id, type: 'radio', label: clean(groupLabel(el, options)) || name, required: grp.some(r => r.required), options,
                   value: (grp.find(r => r.checked) || {}).value || ''}); continue; }
    el.setAttribute('data-co-field', id);
    if (itype === 'file') {                                   // the upload's own heading ("Resume/CV", "Cover Letter"), not its "Attach" button
      const generic = /^(attach|upload|choose( a)? file|browse|select( a)? file|drop|dropbox|google drive|enter manually)\b/i;
      let lab = clean(labelFor(el));
      if (!lab || generic.test(lab)) {
        const u = el.id && document.getElementById('upload-label-' + el.id);
        if (u && ok(pure(u))) lab = clean(pure(u));
        for (let c = el.parentElement, i = 0; (!lab || generic.test(lab)) && c && i < 6; c = c.parentElement, i++) {
          for (const h of c.querySelectorAll('label, legend, h3, h4, h5, [class*="label" i], [class*="title" i]')) {
            const t = clean(pure(h));
            if (t && !generic.test(t) && t.length < 80) { lab = t; break; }
          }
        }
      }
      fields.push({...base, id, type: 'file', label: lab || (el.id || el.name || 'file'), required, options: [],
                   value: (el.files && el.files.length) ? 'file' : ''});
      continue;
    }
    if (tag === 'select') {
      const options = [...el.options].map(o => clean(o.textContent)).filter(o => o && !/^(select|choose|--|please select)/i.test(o));
      fields.push({...base, id, type: 'select', label: clean(labelFor(el)), required, options, value: clean((el.selectedOptions[0] || {}).textContent || '')}); continue; }
    const type = tag === 'textarea' ? 'textarea' : ['email', 'tel', 'url', 'number', 'date', 'checkbox', 'file'].includes(itype) ? itype : 'text';
    fields.push({...base, id, type, label: clean(labelFor(el)), required, options: [], maxLength: el.maxLength > 0 ? el.maxLength : null,
                 value: type === 'checkbox' ? (el.checked ? 'true' : '') : (type === 'file' ? ((el.files && el.files.length) ? 'file' : '') : (el.value || ''))});
  }
  return fields;
}"""


# The options of THIS drop-down (a react-select's own menu), each tagged data-co-opt=<n> so the fill clicks exactly that one: the listbox the input
# names (aria-controls / aria-owns), else options inside the drop-down's own container, else the ONE open menu of the page (a menu in a portal).
# Never another widget's list (the RightShip page keeps the phone field's country list in the DOM: it was read for every drop-down, 2 Nov 2026).
OPTIONS_FOR_JS = r"""el => {
  for (const s of document.querySelectorAll('[data-co-opt]')) s.removeAttribute('data-co-opt');
  const vis = o => !!(o.offsetParent !== null || o.getClientRects().length);
  const text = o => (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim();
  let opts = [];
  for (const id of [el.getAttribute('aria-controls'), el.getAttribute('aria-owns')].filter(Boolean)) {
    const box = document.getElementById(id);
    if (box) opts = [...box.querySelectorAll('[role=option], .select__option')];
    if (opts.length) break;
  }
  if (!opts.length) {
    let c = el.parentElement;
    for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
      if (c.querySelectorAll('input:not([type=hidden]), textarea, select').length > 2) break;
      const found = [...c.querySelectorAll('[role=option], .select__option')];
      if (found.length) { opts = found; break; }
    }
  }
  if (!opts.length) {
    const menus = [...document.querySelectorAll('.select__menu, [class*="select__menu"], [id$="-listbox"], [role=listbox]')].filter(vis);
    if (menus.length === 1) opts = [...menus[0].querySelectorAll('[role=option], .select__option')];
  }
  if (!opts.length) {                        // a list some other script built: the visible options, only when they are ONE list (one parent)
    const all = [...document.querySelectorAll('[role=option]')].filter(vis);
    if (all.length && new Set(all.map(o => o.parentElement)).size === 1) opts = all;
  }
  opts = opts.filter(vis);
  opts.forEach((o, i) => o.setAttribute('data-co-opt', String(i)));
  return opts.map(text);
}"""


@dataclass
class DomField:
    id: str                         # data-co-field on the live element
    type: str                       # text | email | tel | url | number | date | textarea | select | combobox | radio | checkbox | file
    label: str
    required: bool
    options: list[str] = field(default_factory=list)
    value: str = ""
    native_id: str = ""
    native_name: str = ""
    max_length: int | None = None
    visible: bool = True

    @classmethod
    def from_js(cls, d: dict) -> "DomField":
        label = " ".join(str(d.get("label") or "").split())
        req = bool(d.get("required")) or label.rstrip().endswith(("*", "✱"))
        return cls(str(d["id"]), str(d.get("type") or "text"), re.sub(r"\s*[*✱]+\s*$", "", label), req, list(d.get("options") or []),
                   str(d.get("value") or ""), str(d.get("nativeId") or ""), str(d.get("nativeName") or ""), d.get("maxLength"), bool(d.get("visible", True)))

    def as_dict(self) -> dict:
        return asdict(self)


def _guard(page, fn: Callable[[], Any], default: Any = None) -> Any:
    """Run one page call: a closed page raises BrowserClosed, any other error gives `default`."""
    try:
        return fn()
    except BrowserClosed:
        raise
    except Exception as e:  # noqa: BLE001
        raise_if_closed(e, page)
        return default


def goto(page, url: str, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Open `url` (up to GOTO_TRIES attempts, 0.8 s then 1.6 s between them); the navigation response, or the last error raised."""
    last: Exception | None = None
    for attempt in range(GOTO_TRIES):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("load", timeout=8_000)
            except Exception as e:  # noqa: BLE001  (a page that never fires `load`: what has loaded is used)
                raise_if_closed(e, page)
            return resp
        except BrowserClosed:
            raise
        except Exception as e:  # noqa: BLE001
            raise_if_closed(e, page)
            last = e
            if attempt < GOTO_TRIES - 1:
                sleep(0.8 * (attempt + 1))
    raise last if last else RuntimeError(f"could not open {url}")


def dismiss_consent(page) -> str:
    """Press the accept / close button of a visible cookie banner; '' when there was none. Only a button inside the banner is pressed."""
    root = _guard(page, lambda: page.locator(CONSENT_ROOTS).first)
    if root is None or not _guard(page, lambda: root.count(), 0) or not _guard(page, lambda: root.is_visible(), False):
        return ""
    for sel in CONSENT_BUTTONS:
        b = page.locator(sel).first
        if _guard(page, lambda: b.count(), 0) and _guard(page, lambda: b.is_visible(), False):
            text = _guard(page, lambda: b.inner_text(), "") or ""
            if shortfields.clickable_text(text):
                _guard(page, lambda: b.click(timeout=2000))
                return f"dismissed a cookie banner ({sel})"
    for b in _guard(page, lambda: root.locator("button, [role=button]").all(), []) or []:
        text = " ".join((_guard(page, lambda: b.inner_text(), "") or "").split())
        if CONSENT_TEXT.match(text) and shortfields.clickable_text(text) and _guard(page, lambda: b.is_visible(), False):
            _guard(page, lambda: b.click(timeout=2000))
            return f"dismissed a cookie banner ({text!r})"
    return ""


def _frames(page) -> list:
    return list(_guard(page, lambda: page.frames, []) or [])


def wait_for_form(page, wait_ms: int = WAIT_MS, settle_ms: int = SETTLE_MS, clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait in every frame for a form control (FORM_SEL), then let the page settle. True when one appeared."""
    end = clock() + wait_ms / 1000
    found = False
    while True:
        for fr in _frames(page):
            if _guard(page, lambda: fr.query_selector(FORM_SEL)) is not None:
                found = True
                break
        if found or clock() >= end:
            break
        _guard(page, lambda: page.wait_for_timeout(POLL_MS))
    _guard(page, lambda: page.wait_for_timeout(settle_ms))
    return found


def extract(frame, page=None) -> list[DomField]:
    """The fields of one frame, tagged on the page."""
    data = _guard(page or frame, lambda: frame.evaluate(EXTRACT_JS), []) or []
    return [DomField.from_js(d) for d in data if isinstance(d, dict) and d.get("id")]


def richest(page) -> tuple[Any, list[DomField]]:
    """(frame, fields) of the frame with the most fields; the main frame when no frame has any."""
    best = (_guard(page, lambda: page.main_frame), [])
    for fr in _frames(page):
        got = extract(fr, page)
        if len(got) > len(best[1]):
            best = (fr, got)
    return best


APPISH = re.compile(r"first name|last name|full name|resume|résumé|\bcv\b|cover letter|phone|linkedin|github|why |portfolio|sponsorship|relocat", re.I)
SEARCHISH = re.compile(r"search|buscar|filtr|keyword|department|office|location|remote|category", re.I)


def looks_like_application(fields: list[DomField]) -> bool:
    """An application form, not a careers-site search form (a closed Greenhouse posting redirects to the board, whose filters look like a form)."""
    if not fields:
        return False
    if any(f.type in ("file", "email") or re.search(r"e-?mail", f.label, re.I) or APPISH.search(f.label) for f in fields):
        return True
    if all(SEARCHISH.search(f.label) or SEARCHISH.search(f.native_id) for f in fields):
        return False
    return not (len(fields) <= 3 and not any(f.required for f in fields))


def nudge_scroll(page) -> None:
    for i in range(1, SCROLL_STEPS + 1):
        _guard(page, lambda: page.evaluate(f"() => window.scrollTo(0, {i * SCROLL_PX})"))
        _guard(page, lambda: page.wait_for_timeout(250))
    _guard(page, lambda: page.evaluate("() => window.scrollTo(0, 0)"))
    _guard(page, lambda: page.wait_for_timeout(400))


def ats_link(page) -> str | None:
    """The address of a link on the page to a known ATS (or an /apply page) that does not say "submit"; None when there is none."""
    here = _guard(page, lambda: page.url, "") or ""
    for a in _guard(page, lambda: page.query_selector_all("a[href]"), []) or []:
        href = _guard(page, lambda: a.get_attribute("href"), "") or ""
        full = urljoin(here, href)
        if urlparse(full).scheme in ("http", "https") and ATS_LINK.search(full) and "submit" not in full.lower() and full.split("#")[0] != here.split("#")[0]:
            return full
    return None


@dataclass
class Reached:
    frame: Any
    fields: list[DomField]
    notes: list[str] = field(default_factory=list)
    url: str = ""

    @property
    def ok(self) -> bool:
        return looks_like_application(self.fields)


def reach(page, url: str, *, navigate: bool = True, agent: Callable[[Any], bool] | None = None, out: Callable[[str], None] = print,
          sleep: Callable[[float], None] = time.sleep, wait_ms: int = WAIT_MS) -> Reached:
    """Open `url` (unless `navigate` is False: the page is already there), wait for the form and read it, in the order the module docstring gives.
    `agent(page)` is the last resort (the form agent in reach mode, apply/formagent.py): it only REACHES the form; True when it did."""
    notes: list[str] = []
    if navigate:
        goto(page, url, sleep)
    note = dismiss_consent(page)
    if note:
        notes.append(note)
    wait_for_form(page, wait_ms)
    frame, fields = richest(page)
    if not looks_like_application(fields):
        nudge_scroll(page)
        frame, fields = richest(page)
    if not looks_like_application(fields):
        link = ats_link(page)
        if link:
            notes.append(f"followed the link to the form: {link}")
            goto(page, link, sleep)
            dismiss_consent(page)
            wait_for_form(page, min(wait_ms, 6_000))
            frame, fields = richest(page)
    if not looks_like_application(fields) and agent is not None:
        if agent(page):
            notes.append("the form agent reached the form")
            frame, fields = richest(page)
    for n in notes:
        out(f"  {n}")
    return Reached(frame, fields, notes, _guard(page, lambda: frame.url, "") or "")


def path_of(frame, page=None) -> str:
    """The URL path of a frame (to see whether the page navigated during the fill), read from the page itself: Playwright's `frame.url` can lag a
    same-document navigation (history.pushState) by a moment, which let one more field be filled before the stop."""
    here = _guard(page or frame, lambda: frame.evaluate("() => location.pathname"), "") or ""
    if here:
        return str(here)
    u = _guard(page or frame, lambda: frame.url, "") or ""
    return urlparse(u).path or u


def dom_fields(page, wait_ms: int = WAIT_MS) -> tuple[Any, list[DomField]]:
    """Wait for and read the form of the page as it is now (no navigation)."""
    wait_for_form(page, wait_ms)
    return richest(page)
