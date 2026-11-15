"""Where a job's application may be driven at all (13 Nov 2026), before any plan is made or field is filled. Zero tokens, no network.

  sign-in wall   `auth_url_reason(url)` / `page_reason(page)`: a URL whose host starts `secure.` or is a login host, whose path has a login / signin /
                 auth / oauth / account segment, or that carries OAuth parameters (`code_challenge`, or `state` with `client_id` / `redirect_uri` /
                 `response_type`), or a page with any `input[type=password]`. The run types nothing, closes the browser, sets the job to
                 `manual_apply_account_required` and prints the listing URL (errors.AccountRequired).
  aggregator     Reed, CV-Library, Indeed, LinkedIn ... (`apply_kind: aggregator_only`, or an apply address on such a host): the form is never entered.
                 `employer_url` first looks for the employer's own application address (the job's own ATS board or a non-aggregator address
                 already on the job); else the apply pack (CV, letter, answers_to_paste.txt) is built and the listing opened for the person.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..discovery import applykind
from .errors import AccountRequired

ACCOUNT_STATUS = "manual_apply_account_required"
AUTH_SEGMENTS = {"login", "log-in", "log_in", "signin", "sign-in", "sign_in", "auth", "oauth", "oauth2", "authorize", "authenticate",
                 "account", "accounts", "sso", "secure"}
AUTH_HOST_LABELS = {"secure", "login", "signin", "auth", "oauth", "account", "accounts", "sso"}
OAUTH_CONTEXT = {"client_id", "redirect_uri", "response_type", "scope"}
PASSWORD_FIELD = "input[type=password]"


def auth_url_reason(url: str | None) -> str | None:
    """Why `url` is a sign-in / authentication address, or None."""
    parts = urlparse(str(url or ""))
    host = (parts.hostname or "").lower()
    if not host:
        return None
    label = next((x for x in host.split(".")[:-2] if x in AUTH_HOST_LABELS), "")        # secure.reed.co.uk: the first labels, never the registered domain
    if label:
        return f"the host {host} is an authentication host ({label}.)"
    for seg in (s.lower() for s in parts.path.split("/") if s):
        seg = re.sub(r"\.(aspx?|html?|php|jsp)$", "", seg)
        if seg in AUTH_SEGMENTS:
            return f"the address path has a sign-in segment ({seg!r})"
    query = {k.lower() for k in parse_qs(parts.query + "&" + parts.fragment)}
    if "code_challenge" in query or ("state" in query and query & OAUTH_CONTEXT):
        return "the address carries OAuth parameters"
    return None


def page_reason(page) -> str | None:
    """Why the page as it is now is a sign-in page, or None: its URL, or any password field. Only looks; nothing is typed or clicked."""
    why = auth_url_reason(str(getattr(page, "url", "") or ""))
    if why:
        return why
    try:
        if page.query_selector_all(PASSWORD_FIELD):
            return "the page has a password field"
    except Exception:  # noqa: BLE001  (the page is navigating)
        return None
    return None


def require_no_auth(page) -> None:
    """Raise AccountRequired when `page` is a sign-in page."""
    why = page_reason(page)
    if why:
        raise AccountRequired(why, str(getattr(page, "url", "") or ""))


def account_line(why: str, listing: str) -> str:
    return (f"SIGN-IN PAGE: {why}. Nothing was typed; the browser is closed. This job needs an account on the site: status "
            f"'{ACCOUNT_STATUS}'. Listing: {listing}")


def _text(job: Mapping[str, Any] | None, key: str) -> str:
    try:
        return str(job[key] or "") if job is not None else ""
    except (KeyError, IndexError, TypeError):
        return ""


def is_aggregator(job: Mapping[str, Any]) -> bool:
    """Is the job's application on a job board? Its recorded kind says so, or its apply address is on a board's host."""
    if _text(job, "apply_kind") == "aggregator_only":
        return True
    link = _text(job, "apply_url") or _text(job, "url")
    return applykind.kind_of(link) == "aggregator_only"


def employer_url(job: Mapping[str, Any]) -> str:
    """The employer's own application address for an aggregator job, or '': a non-board address already on the job (apply_url, then url), else the
    page of the job's own ATS board (id `<provider>:<token>:<posting>`). Makes no request."""
    for key in ("apply_url", "url"):
        link = _text(job, key)
        if link.startswith(("http://", "https://")) and applykind.kind_of(link) not in ("", "aggregator_only"):
            return link
    provider, _, rest = _text(job, "id").partition(":")
    token, _, posting = rest.partition(":")
    if provider == "greenhouse" and token and posting.isdigit():
        return f"https://job-boards.greenhouse.io/{token}/jobs/{posting}"
    return ""


def listing_url(job: Mapping[str, Any]) -> str:
    return _text(job, "apply_url") or _text(job, "url")
