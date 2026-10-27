"""Find the application form, and recognise a page that is not one (shared by apply/assist.py and autoapply.py).

Where the form is:
  Ashby       <job url>/application      (the job page only describes the role)
  Lever       <job url>/apply
  Greenhouse  the job page itself: wait for the form to appear
  others      the page itself; an "Apply" link on it is followed once
If no form field has appeared after FORM_WAIT_S seconds the caller prints `FORM NOT FOUND <url>`: an empty `Filled: []` is never printed as if
the form had been looked at.

A page that must be handed to the person (`blocker`): a VISIBLE bot check (a reCAPTCHA / hCaptcha / Arkose checkbox or puzzle frame that is
displayed and at least MIN_CHALLENGE_W x MIN_CHALLENGE_H px, or a displayed Turnstile widget, or the words "verify you are human"), the
message "error verifying your application", a submit the form flagged as possible spam ("flagged as possible spam", "couldn't submit your
application"), or a Google or Microsoft sign-in page or popup. An invisible reCAPTCHA (v3) frame or badge
(`size=invisible`, `data-size="invisible"`, a submit button carrying a data-sitekey) is not a trigger: the form is filled as usual. Nothing here
touches, solves or works around any of them (it only asks whether an element is displayed and how big it is), and no credentials are ever
entered: the caller stops and apply/handoff.py opens the page in the person's own browser.
"""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from ..discovery import applykind
from . import ats_vendor
from .filler import NeedsUser

FORM_WAIT_S = 15
POLL_MS = 500
SETTLE_MS = 2500                     # a page's own scripts after it loads
APPLY_BUTTON = re.compile(r"^\s*(apply( for this job| now)?|i'?m interested)\s*$", re.I)

CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"]', 'iframe[title*="recaptcha" i]', ".g-recaptcha", 'iframe[src*="hcaptcha"]', ".h-captcha",
    'iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="turnstile"]', ".cf-turnstile", 'iframe[src*="arkoselabs"]',
    'iframe[src*="funcaptcha"]', "[data-sitekey]")
TURNSTILE_SELECTORS = ('iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="turnstile"]', ".cf-turnstile")
MIN_CHALLENGE_W, MIN_CHALLENGE_H = 100, 60      # a checkbox or puzzle frame is bigger than this; the invisible reCAPTCHA badge is not a challenge
CAPTCHA_TEXT = re.compile(r"verify (?:that )?you(?:'re| are) (?:a )?human|confirm (?:that )?you(?:'re| are) (?:a )?human|"
                          r"i['’]m not a robot|are you a robot|checking (?:if|that) you(?:'re| are) (?:a )?human|human verification", re.I)
VERIFY_ERROR = re.compile(r"error verifying your application", re.I)
# What Ashby shows after a submit from the automated browser: "Your application submission was flagged as possible spam" / "We couldn't submit your
# application". The form is never refreshed or submitted again from automation.
SPAM_FLAG = re.compile(r"flagged (?:\w+ ){0,3}as (?:a )?(?:possible |potential |likely |suspected )?spam|(?:possible|potential|suspected) spam|"
                       r"(?:could not|couldn['’]t|unable to|were not able to|weren['’]t able to) submit your application", re.I)
SIGN_IN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "login.microsoft.com")
FIELDS = "input[type=email], input[type=tel], input[type=file], input[type=text], textarea"


class HandOff(NeedsUser):
    """The page needs the person (a bot check or a sign-in wall): the run stops and the page is opened in their own browser."""


def host_of(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def form_url(url: str, kind: str | None = None, job=None, tokens=()) -> str:
    """The address of the application form for a job-page `url` (apply/ats_vendor.py): a Greenhouse job on a company domain opens on job-boards.greenhouse.io,
    Ashby `.../application`, Lever `.../apply`, any other page as it is."""
    return ats_vendor.direct_url(url, kind=kind, job=job, tokens=tokens) if url else url


def _visible(el) -> bool:
    try:
        return bool(el.is_visible())
    except Exception:  # noqa: BLE001  (the page is navigating)
        return False


def has_form(page) -> bool:
    """Is an application form on the page? An e-mail, phone or file field, or at least two visible text boxes."""
    try:
        fields = [el for el in page.query_selector_all(FIELDS) if _visible(el)]
    except Exception:  # noqa: BLE001
        return False
    kinds = {(el.get_attribute("type") or "text").lower() for el in fields}
    return bool(kinds & {"email", "tel", "file"}) or len(fields) >= 2


def _attr(el, name: str) -> str:
    try:
        return str(el.get_attribute(name) or "")
    except Exception:  # noqa: BLE001
        return ""


def _tag(el) -> str:
    try:
        return str(el.evaluate("e => e.tagName") or "").lower()
    except Exception:  # noqa: BLE001  (not an element we can ask)
        return ""


def challenge_shown(el, selector: str) -> bool:
    """Is `el` (matched by `selector`) a challenge the person can see: displayed, not marked invisible, and (a Turnstile widget aside) at
    least MIN_CHALLENGE_W x MIN_CHALLENGE_H px. Looks only; nothing is clicked, typed or focused."""
    if not _visible(el):
        return False
    if re.search(r"[?&#]size=invisible", _attr(el, "src"), re.I) or _attr(el, "data-size").lower() == "invisible":
        return False
    if selector in TURNSTILE_SELECTORS:
        return True
    if _tag(el) in ("button", "input", "a"):                  # a submit button that carries the site key (reCAPTCHA v3), not a widget
        return False
    try:
        box = el.bounding_box()
    except Exception:  # noqa: BLE001
        return False
    return bool(box) and box["width"] >= MIN_CHALLENGE_W and box["height"] >= MIN_CHALLENGE_H


def captcha_reason(page) -> str | None:
    """What kind of challenge the page shows, or None. Only looks (a selector query, the size of what it finds and the visible text); never
    touches it. An invisible reCAPTCHA iframe or badge is not a challenge."""
    for sel in CAPTCHA_SELECTORS:
        try:
            if any(challenge_shown(el, sel) for el in page.query_selector_all(sel)):
                return f"a CAPTCHA / bot check is on the page ({sel})"
        except Exception:  # noqa: BLE001  (the page is navigating)
            continue
    text = _body(page)
    m = CAPTCHA_TEXT.search(text)
    return f"the page asks you to verify you are human ({m.group(0)!r})" if m else None


def _body(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        return ""


def sign_in_reason(page) -> str | None:
    """A Google or Microsoft sign-in page, as the page itself or as another window (a popup) of the same browser."""
    pages = [page]
    try:
        pages += [p for p in page.context.pages if p is not page]
    except Exception:  # noqa: BLE001  (a page without a context)
        pass
    for p in pages:
        host = (urlparse(str(getattr(p, "url", "") or "")).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in SIGN_IN_HOSTS):
            return f"a {'Google' if 'google' in host else 'Microsoft'} sign-in page is open ({host}); credentials are never entered by this program"
    return None


def spam_reason(page) -> str | None:
    """The sentence of a page that says the submission was flagged as spam or could not be submitted, or None. Only reads the visible text."""
    m = SPAM_FLAG.search(_body(page))
    return f"the form flagged this submission as possible spam ({m.group(0)!r}): not refreshed, not submitted again from automation" if m else None


def blocker(page) -> str | None:
    """Why this page has to be handed to the person, or None: a bot check, "error verifying your application", a submit flagged as spam, or a
    sign-in wall."""
    found = captcha_reason(page)
    if found:
        return found
    if VERIFY_ERROR.search(_body(page)):
        return "the page says 'error verifying your application' (a bot check turned this browser away)"
    return spam_reason(page) or sign_in_reason(page)


def wait_for_form(page, seconds: float | None = None, follow: Callable[[Any], None] | None = None) -> str | None:
    """Wait up to `seconds` for a form to appear; an "Apply" link on the page is followed once, after a few seconds, by `follow(link)` (the
    caller's own action: this module clicks nothing; without `follow` no link is followed). Returns None when the
    form is there. Raises HandOff when a bot check or a sign-in wall shows up first. Anything else returns 'FORM NOT FOUND'."""
    clicked = False
    seconds = FORM_WAIT_S if seconds is None else seconds
    for i in range(int(seconds * 1000 / POLL_MS) + 1):
        stop = blocker(page)
        if stop:
            raise HandOff(stop)
        if has_form(page):
            return None
        if follow is not None and not clicked and i * POLL_MS >= 3000:
            clicked = True
            for a in page.query_selector_all("a, button"):
                if APPLY_BUTTON.match(a.inner_text() or ""):
                    follow(a)                            # the "Apply" link that opens the form: never the submit button
                    break
        page.wait_for_timeout(POLL_MS)
    return "FORM NOT FOUND"


def open_form(page, url: str, kind: str | None = None, wait: Callable[[str], None] | None = None, seconds: float | None = None,
              follow: Callable[[Any], None] | None = None, job=None, tokens=()) -> str | None:
    """Open the application form of the job page `url` and wait for it. Returns None when the form is there, else 'FORM NOT FOUND <address>'.
    Raises HandOff for a bot check or sign-in wall. `wait(url)` is called before each request (the throttle: one a second per host)."""
    target = form_url(url, kind, job, tokens)
    if wait:
        wait(target)
    page.goto(target, wait_until="domcontentloaded")
    page.wait_for_timeout(SETTLE_MS)
    if wait_for_form(page, seconds, follow) is None:
        return None
    return f"FORM NOT FOUND {target}"
