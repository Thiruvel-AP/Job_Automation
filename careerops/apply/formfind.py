"""Find the application form, and recognise a page that is not one (shared by apply/assist.py and autoapply.py).

Where the form is:
  Ashby       <job url>/application      (the job page only describes the role)
  Lever       <job url>/apply
  Greenhouse  the job page itself: wait for the form to appear
  others      the page itself; an "Apply" link on it is followed once
If no form field has appeared after FORM_WAIT_S seconds the caller prints `FORM NOT FOUND <url>`: an empty `Filled: []` is never printed as if
the form had been looked at.

A page that must be handed to the person (`blocker`): a bot check (reCAPTCHA, hCaptcha, Turnstile ... frames, "verify you are human"), the
message "error verifying your application", or a Google or Microsoft sign-in page or popup. Nothing here touches, solves or works around any
of them, and no credentials are ever entered: the caller stops and apply/handoff.py opens the page in the person's own browser.
"""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from ..discovery import applykind
from .filler import NeedsUser

FORM_WAIT_S = 15
POLL_MS = 500
SETTLE_MS = 2500                     # a page's own scripts after it loads
APPLY_BUTTON = re.compile(r"^\s*(apply( for this job| now)?|i'?m interested)\s*$", re.I)

CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"]', 'iframe[title*="recaptcha" i]', ".g-recaptcha", 'iframe[src*="hcaptcha"]', ".h-captcha",
    'iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="turnstile"]', ".cf-turnstile", 'iframe[src*="arkoselabs"]',
    'iframe[src*="funcaptcha"]', "[data-sitekey]")
CAPTCHA_TEXT = re.compile(r"verify (?:that )?you(?:'re| are) (?:a )?human|confirm (?:that )?you(?:'re| are) (?:a )?human|"
                          r"i['’]m not a robot|are you a robot|checking (?:if|that) you(?:'re| are) (?:a )?human|human verification", re.I)
VERIFY_ERROR = re.compile(r"error verifying your application", re.I)
SIGN_IN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "login.microsoft.com")
FIELDS = "input[type=email], input[type=tel], input[type=file], input[type=text], textarea"


class HandOff(NeedsUser):
    """The page needs the person (a bot check or a sign-in wall): the run stops and the page is opened in their own browser."""


def host_of(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def form_url(url: str, kind: str | None = None) -> str:
    """The address of the application form for a job-page `url`: Ashby `.../application`, Lever `.../apply`, any other page as it is."""
    kind = kind if kind in applykind.KINDS else applykind.kind_of(url)
    parts = urlparse(url or "")
    path = parts.path.rstrip("/")
    if kind == "ashby" and not path.endswith("/application"):
        path += "/application"
    elif kind == "lever" and not path.endswith("/apply"):
        path += "/apply"
    else:
        return url
    return urlunparse(parts._replace(path=path))


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


def captcha_reason(page) -> str | None:
    """What kind of challenge the page shows, or None. Only looks (a selector query and the visible text); never touches it."""
    for sel in CAPTCHA_SELECTORS:
        try:
            if page.query_selector(sel):
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


def blocker(page) -> str | None:
    """Why this page has to be handed to the person, or None: a bot check, "error verifying your application", or a sign-in wall."""
    found = captcha_reason(page)
    if found:
        return found
    if VERIFY_ERROR.search(_body(page)):
        return "the page says 'error verifying your application' (a bot check turned this browser away)"
    return sign_in_reason(page)


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
              follow: Callable[[Any], None] | None = None) -> str | None:
    """Open the application form of the job page `url` and wait for it. Returns None when the form is there, else 'FORM NOT FOUND <address>'.
    Raises HandOff for a bot check or sign-in wall. `wait(url)` is called before each request (the throttle: one a second per host)."""
    target = form_url(url, kind)
    if wait:
        wait(target)
    page.goto(target, wait_until="domcontentloaded")
    page.wait_for_timeout(SETTLE_MS)
    if wait_for_form(page, seconds, follow) is None:
        return None
    return f"FORM NOT FOUND {target}"
