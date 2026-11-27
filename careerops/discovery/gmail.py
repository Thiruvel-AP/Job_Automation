"""Job-alert emails through the Gmail API (`careerops discover --gmail-alerts`; `--gmail-auth` signs in once).

Scopes (SCOPES): gmail.readonly (the alert emails), gmail.compose (outreach drafts, `careerops outreach draft`) and spreadsheets
(the application tracker, `careerops tracker sync`); a token that was granted anything else is refused. gmail.compose is the narrowest
scope Google offers for creating a draft, and it is wide enough to send as well: what keeps this tool from sending is the code, not the
scope. The only write call to Gmail is `drafts.create` (outreach.py); tests/test_never_sends.py fails on any send. The OAuth client
file you download from Google Cloud (a "Desktop app" client) and the token this module writes both live in paths.secrets
(data/secrets/, git-ignored); nothing from there is printed, logged, committed or sent anywhere but accounts.google.com /
oauth2.googleapis.com / gmail.googleapis.com / sheets.googleapis.com. No Google client library is used: the loopback + PKCE flow of
"OAuth 2.0 for installed apps" is a few requests, made with `requests` like everything else.

`--gmail-auth` opens your browser at Google's consent page and waits for the redirect on 127.0.0.1; YOU sign in and agree there.
This code never types a password and never reads a mailbox other than through the token you granted. It never deletes or labels
anything, and it never sends.

Alert emails: the sources in config/sources.yaml that list sender `domains` (registry.py). LinkedIn, CV-Library, Indeed and Reed have a
dedicated parser; every other source is read by the generic one (job links on the source's own domains). Each is parsed for (title,
company, location, link) per job. The link is a
pointer only: linkedin.com and indeed.com pages are never fetched; the job is looked for on the employer's own board instead
(discover.py). The parsers are written from the usual layout of these emails and tested on hand-made fixtures; they have not been
run on a real mailbox, so an alert that does not parse lands in the manual queue rather than being guessed at.
"""
from __future__ import annotations

import base64
import hashlib
import html as htmllib
import json
import os
import re
import secrets as _secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .http import Fetcher

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"                 # the alert emails (discover --gmail-alerts)
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"          # outreach drafts: drafts.create only (outreach.py)
SCOPE_SHEETS = "https://www.googleapis.com/auth/spreadsheets"            # the application tracker (tracker.py)
SCOPES = (SCOPE, SCOPE_COMPOSE, SCOPE_SHEETS)
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
ALLOWED_TOKEN_HOSTS = ("accounts.google.com", "oauth2.googleapis.com", "gmail.googleapis.com", "sheets.googleapis.com")
CLIENT_FILE, TOKEN_FILE = "gmail_client.json", "gmail_token.json"
ENV_CLIENT_ID, ENV_CLIENT_SECRET = "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"   # the OAuth client, from .env (preferred over CLIENT_FILE)
SENDERS = {"linkedin": ("linkedin.com",), "cvlibrary": ("cv-library.co.uk",), "indeed": ("indeed.com", "indeed.co.uk"),
           "reed": ("reed.co.uk",)}


class GmailAuthError(RuntimeError):
    """Not signed in, a client file that is missing or wrong, or a token that was refused. One line, says what to do."""


# ------------------------------------------------------------------ OAuth (loopback + PKCE)
def _secrets_dir(settings) -> Path:
    return settings.path("secrets")


def client_config(settings) -> dict:
    """The OAuth client: GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET from the environment (`.env`, loaded by the CLI) when both are set;
    otherwise the client JSON in paths.secrets (older setups). Only one of the two variables set is an error, never a silent fallback."""
    cid, secret = os.environ.get(ENV_CLIENT_ID, "").strip(), os.environ.get(ENV_CLIENT_SECRET, "").strip()
    if cid or secret:
        if not (cid and secret):
            raise GmailAuthError(f"only one of {ENV_CLIENT_ID} / {ENV_CLIENT_SECRET} is set in .env: set both (or neither, to use "
                                 f"{_secrets_dir(settings) / CLIENT_FILE})")
        return {"client_id": cid, "client_secret": secret, "token_uri": TOKEN_URI, "auth_uri": AUTH_URI}
    p = _secrets_dir(settings) / CLIENT_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GmailAuthError(f"{p} not found and {ENV_CLIENT_ID} / {ENV_CLIENT_SECRET} are not set: create a Desktop-app OAuth "
                             "client in Google Cloud and put its id and secret in .env (README, 'Getting Gmail access')") from None
    except (OSError, ValueError):
        raise GmailAuthError(f"{p} is not a readable OAuth client file") from None
    c = raw.get("installed") or raw.get("web") or {}
    if not (c.get("client_id") and c.get("client_secret")):
        raise GmailAuthError(f"{p} has no client_id / client_secret: download the client JSON again from Google Cloud")
    token_uri = c.get("token_uri") or TOKEN_URI
    if urlparse(token_uri).hostname not in ALLOWED_TOKEN_HOSTS:
        raise GmailAuthError(f"{p}: token_uri {token_uri!r} is not a Google address; refusing to send credentials to it")
    return {"client_id": c["client_id"], "client_secret": c["client_secret"], "token_uri": token_uri,
            "auth_uri": c.get("auth_uri") or AUTH_URI}


def _write_token(settings, token: dict) -> None:
    d = _secrets_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(token, f)


def _read_token(settings) -> dict | None:
    try:
        return json.loads((_secrets_dir(settings) / TOKEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _check_scope(granted: str, need: tuple[str, ...] = (SCOPE,)) -> None:
    """A token holds only scopes of SCOPES (anything wider is refused) and every scope in `need` (the sign-in needs all of them; a
    caller needs the ones it uses, so a token from before the compose and Sheets scopes still reads alert emails)."""
    scopes = set((granted or "").split())
    if not scopes <= set(SCOPES):
        raise GmailAuthError(f"Google granted {sorted(scopes) or 'no scope'} but only {', '.join(SCOPES)} are accepted; "
                             "the token was not kept")
    missing = [s for s in need if s not in scopes]
    if missing:
        raise GmailAuthError(f"the Gmail sign-in lacks {', '.join(missing)}: run `careerops discover --gmail-auth` again and tick every box")


def authorize(settings, *, open_browser: Callable[[str], object] = webbrowser.open, post=requests.post,
              clock: Callable[[], float] = time.time, timeout_s: float = 240, say: Callable[[str], None] = print) -> None:
    """Sign in once: open Google's consent page, wait for the redirect on 127.0.0.1, exchange the code, keep the refresh token."""
    c = client_config(settings)
    verifier = _secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"career-ops: you can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = c["auth_uri"] + "?" + urlencode({
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": redirect, "scope": " ".join(SCOPES), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
    say("Opening your browser to let you grant career-ops access to read Gmail, create Gmail drafts (never send) and edit the tracker "
        "Sheet. If it does not open, visit:\n  " + url)
    open_browser(url)
    deadline = clock() + timeout_s
    try:
        while "code" not in got and "error" not in got and clock() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if got.get("error"):
        raise GmailAuthError(f"Google answered {got['error']}: nothing was saved")
    if got.get("state") != state or "code" not in got:
        raise GmailAuthError("no valid answer from Google within the time allowed: nothing was saved")
    r = post(c["token_uri"], data={"grant_type": "authorization_code", "code": got["code"], "client_id": c["client_id"],
                                   "client_secret": c["client_secret"], "redirect_uri": redirect, "code_verifier": verifier},
             timeout=30)
    body = r.json() if r.ok else {}
    if not r.ok or not body.get("refresh_token"):
        raise GmailAuthError(f"Google refused the sign-in (HTTP {r.status_code}); nothing was saved")
    _check_scope(body.get("scope", ""), need=SCOPES)
    _write_token(settings, {"refresh_token": body["refresh_token"], "access_token": body.get("access_token"),
                            "expires_at": clock() + float(body.get("expires_in", 0)), "scope": body["scope"],
                            "granted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})     # what `careerops doctor` ages
    say(f"Signed in. Token saved in {_secrets_dir(settings) / TOKEN_FILE} (git-ignored). career-ops reads mail, creates drafts "
        "and edits the tracker Sheet; it never sends.")


def token_age_days(settings, now: datetime | None = None) -> float | None:
    """Days since you signed in (`--gmail-auth`): a token granted while the OAuth app is in Testing stops working after about 7 days. Read
    from the token's own `granted_at`; for a token written before that was kept, the token file's age (a refresh rewrites the file, so
    that is only a lower bound). None when there is no token file."""
    path = _secrets_dir(settings) / TOKEN_FILE
    now = now or datetime.now(timezone.utc)
    tok = _read_token(settings) or {}
    try:
        granted = datetime.fromisoformat(str(tok.get("granted_at")))
        granted = granted if granted.tzinfo else granted.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            granted = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return None
    return max((now - granted).total_seconds() / 86400.0, 0.0)


def access_token(settings, *, post=requests.post, clock: Callable[[], float] = time.time, need: tuple[str, ...] = (SCOPE,)) -> str:
    tok = _read_token(settings)
    if not tok or not tok.get("refresh_token"):
        raise GmailAuthError("not signed in to Gmail: run `careerops discover --gmail-auth` first")
    _check_scope(tok.get("scope", ""), need)
    if tok.get("access_token") and tok.get("expires_at", 0) - clock() > 120:
        return tok["access_token"]
    c = client_config(settings)
    r = post(c["token_uri"], data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                   "client_id": c["client_id"], "client_secret": c["client_secret"]}, timeout=30)
    body = r.json() if r.ok else {}
    if not r.ok or not body.get("access_token"):
        raise GmailAuthError(f"Google refused to refresh the Gmail token (HTTP {r.status_code}): run `careerops discover --gmail-auth` again")
    tok.update(access_token=body["access_token"], expires_at=clock() + float(body.get("expires_in", 0)))
    _write_token(settings, tok)
    return tok["access_token"]


# ------------------------------------------------------------------ the Gmail API (read calls; drafts are made in outreach.py)
def source_clause(source) -> str:
    """The Gmail search clause for one registry source: its sender domains, and its subject words when it shares a sender."""
    frm = " OR ".join(f"from:{d}" for d in source.domains)
    if not source.subject_contains:
        return frm
    words = " OR ".join(f'"{w}"' if " " in w else w for w in source.subject_contains)
    return f"(({frm}) subject:({words}))"


def build_query(settings, registry=None) -> str:
    """The Gmail search for the alert emails of the last discover.gmail.query_days days. With a registry (and no explicit
    discover.gmail.senders) it names every source that lists sender domains; `senders` (kinds: linkedin, cvlibrary, indeed, reed) keeps
    the older fixed list."""
    g = settings["discover"].get("gmail") or {}
    days = int(g.get("query_days", 14))
    if registry is not None and len(registry) and not g.get("senders"):
        clauses = " OR ".join(source_clause(s) for s in registry.alert_sources())
        return f"({clauses}) newer_than:{days}d"
    senders = " OR ".join(f"from:{d}" for k in (g.get("senders") or list(SENDERS)) for d in SENDERS.get(k, ()))
    return f"({senders}) newer_than:{days}d"


def web_link(message_id: str) -> str:
    """Where to open one message in Gmail (for the manual queue: the alert that could not be read)."""
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def list_message_ids(fetcher: Fetcher, token: str, query: str, limit: int) -> list[str]:
    ids: list[str] = []
    page = None
    while len(ids) < limit:
        params = {"q": query, "maxResults": min(50, limit - len(ids)), **({"pageToken": page} if page else {})}
        data = fetcher.json(f"{API}/messages", params=params, headers={"Authorization": f"Bearer {token}"}, robots=False,
                            what="Gmail message list") or {}
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids[:limit]


def list_labels(fetcher: Fetcher, token: str) -> list[dict]:
    """Every label of the mailbox ({id, name, type}): `users.labels.list`, read-only (gmail.readonly covers it)."""
    data = fetcher.json(f"{API}/labels", headers={"Authorization": f"Bearer {token}"}, robots=False, what="Gmail label list") or {}
    return [x for x in data.get("labels", []) if isinstance(x, dict) and x.get("id") and x.get("name")]


def find_label(labels: list[dict], name: str) -> dict | None:
    """The label called `name`: the whole name as Gmail stores it ('Jobs/LinkedIn' is one nested label's name), compared without case and
    without stray spaces around the '/' parts. None when there is none."""
    def norm(n: str) -> str:
        return "/".join(part.strip() for part in str(n).split("/")).casefold()
    want = norm(name)
    return next((x for x in labels if norm(x["name"]) == want), None)


def list_label_message_ids(fetcher: Fetcher, token: str, label_id: str, limit: int, after: int | None = None) -> list[str]:
    """Message ids carrying one label, newest first: `labelIds=<id>` (never a `q="label:..."` string: names with spaces or slashes break it).
    `after` (epoch seconds) limits the window with Gmail's own `after:` time term; ids only, nothing is read or changed."""
    ids: list[str] = []
    page = None
    while len(ids) < limit:
        params = {"labelIds": [label_id], "maxResults": min(100, limit - len(ids)), **({"q": f"after:{int(after)}"} if after else {}),
                  **({"pageToken": page} if page else {})}
        data = fetcher.json(f"{API}/messages", params=params, headers={"Authorization": f"Bearer {token}"}, robots=False,
                            what="Gmail label message list") or {}
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids[:limit]


def get_message(fetcher: Fetcher, token: str, mid: str) -> dict:
    return fetcher.json(f"{API}/messages/{mid}", params={"format": "full"}, headers={"Authorization": f"Bearer {token}"},
                        robots=False, what="Gmail message") or {}


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def message_text(msg: dict) -> tuple[str, str, str, str]:
    """(from, subject, html, plain) of a Gmail API message resource (format=full)."""
    payload = msg.get("payload") or {}
    hdr = {h["name"].lower(): h["value"] for h in payload.get("headers", []) if "name" in h}
    html_parts, plain_parts = [], []

    def walk(part):
        mime, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
        if data and mime == "text/html":
            html_parts.append(_b64(data))
        elif data and mime == "text/plain":
            plain_parts.append(_b64(data))
        for sub in part.get("parts") or []:
            walk(sub)
    walk(payload)
    return hdr.get("from", ""), hdr.get("subject", ""), "\n".join(html_parts), "\n".join(plain_parts)


def message_time(msg: dict) -> str:
    """The moment the alert reached the mailbox as an ISO UTC datetime (Gmail's internalDate, else the Date header), '' when neither
    can be read. A job found through an alert was posted at or before this: an upper bound on its posting time."""
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        return datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        pass
    for h in (msg.get("payload") or {}).get("headers", []):
        if str(h.get("name", "")).lower() == "date":
            try:
                return parsedate_to_datetime(h["value"]).astimezone(timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError, IndexError):
                break
    return ""


def message_date(msg: dict) -> str:
    """YYYY-MM-DD the alert reached the mailbox (see `message_time`), '' when it cannot be read."""
    return message_time(msg)[:10]


def sender_kind(from_header: str) -> str | None:
    m = re.search(r"@([\w.-]+)", from_header or "")
    domain = (m.group(1) if m else "").lower()
    return next((k for k, ds in SENDERS.items() if any(domain == d or domain.endswith("." + d) for d in ds)), None)


# ------------------------------------------------------------------ parsing the alert emails
@dataclass
class AlertItem:
    kind: str                  # linkedin | cvlibrary | indeed | reed
    title: str
    company: str
    location: str
    link: str                  # canonical link; for linkedin / indeed a pointer only, never fetched
    message_id: str = ""
    job_id: str = ""           # linkedin: the number of /jobs/view/<id> (or currentJobId=<id>) in the link
    work_type: str = ""        # remote | hybrid | on-site, when the card shows it
    salary: str = ""           # the card's salary line, when it shows one
    easy_apply: bool = False   # the card carries LinkedIn's "Easy Apply" marker


JOB_LINK = {
    "linkedin": (r"linkedin\.com$", r"/(?:comm/)?jobs/view/(?:[\w-]*?-)?(\d{6,})", "https://www.linkedin.com/jobs/view/{}"),
    "cvlibrary": (r"cv-library\.co\.uk$", r"/job/(?:apply/)?(\d+)", "https://www.cv-library.co.uk/job/{}"),
    "reed": (r"reed\.co\.uk$", r"/jobs/[^?#]*?/(\d+)|/jobs/(\d+)", "https://www.reed.co.uk/jobs/{}"),
    "indeed": (r"indeed\.(?:com|co\.uk)$", None, "https://uk.indeed.com/viewjob?jk={}"),
}
NOISE = re.compile(r"^(view job|view|apply|easy apply|apply now|new|actively recruiting|promoted|save|see all jobs|unsubscribe|"
                   r"your job alert|job alert|instant job alert|manage your preferences|unsubscribe here\.?|"
                   r"\d+ (?:new )?jobs?.*|be an early applicant|\d+ (?:school alumni|connections?).*)$", re.I)
# CV-Library's alert mail links everything through its click tracker (https://clicks.cv-library.co.uk/f/a/<token>~~/<id>~/<token>~~): no link in it
# is a /job/<id> page. Such a link is only a pointer to the mail's job (it is never requested; discovery looks for the employer's own board).
CV_TRACKING_HOST = "clicks.cv-library.co.uk"
CV_TRACKING_PATH = "/f/a/"
SALARY = re.compile(r"[£$€]\s?\d")


def job_link(kind: str, href: str) -> str | None:
    """The canonical link when `href` is a link to one job of this kind of alert, else None."""
    host_rx, path_rx, template = JOB_LINK[kind]
    u = urlparse(htmllib.unescape(href or ""))
    if u.scheme not in ("http", "https") or not re.search(host_rx, (u.hostname or "").lower()):
        return None
    if kind == "indeed":
        jk = (parse_qs(u.query).get("jk") or [""])[0]
        return template.format(jk) if re.fullmatch(r"[0-9a-f]{8,}", jk) else None
    m = re.search(path_rx, u.path)
    if m:
        return template.format(next(g for g in m.groups() if g))
    if kind == "linkedin":
        cj = (parse_qs(u.query).get("currentJobId") or [""])[0]
        if re.fullmatch(r"\d{6,}", cj):
            return template.format(cj)
    if kind == "cvlibrary" and (u.hostname or "").lower() == CV_TRACKING_HOST and u.path.startswith(CV_TRACKING_PATH):
        return f"{u.scheme}://{u.netloc}{u.path}"
    return None


def is_cv_tracking(link: str) -> bool:
    return urlparse(link).hostname == CV_TRACKING_HOST


class _Flow(HTMLParser):
    """The email as a flat list of ('a', href, text) and ('t', text) pieces in reading order; a block end or <br> ends a piece."""
    BLOCK = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "table", "span_break"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._skip = 0

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.items.append(("a", self._href, text) if self._href else ("t", text))

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._flush()
            self._href = dict(attrs).get("href") or None
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a":
            self._flush()
            self._href = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _split_company_place(piece: str) -> tuple[str, str]:
    for sep in (" · ", " • ", " | ", " - ", " – "):
        if sep in piece:
            a, b = piece.split(sep, 1)
            return a.strip(), b.strip()
    return piece.strip(), ""


GENERIC_JOB_PATH = re.compile(
    r"/(?:jobs?|job-?details?|jobdetails?|viewjob|vacanc(?:y|ies)|vacatures?|stellenangebote?|stellenanzeigen?|offres?|listing|roles?|"
    r"positions?|careers?|opportunit(?:y|ies)|view|apply)(?:/|$|-)|/job[-_]?\w*\d|\d{5,}", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_|trk|tracking|mc_|ref$|refid$|campaign|cid$|eid$|source$)", re.I)


def generic_link(source, href: str) -> str | None:
    """A link to one job in an alert email of a source without a dedicated parser: http(s), on one of the source's own domains, and a
    path that looks like a job page (/jobs/..., /vacancy/..., /stellenangebote/..., a long job number). Tracking parameters are dropped."""
    u = urlparse(htmllib.unescape(href or ""))
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not any(host == d or host.endswith("." + d) for d in source.domains):
        return None
    if not GENERIC_JOB_PATH.search(u.path):
        return None
    if re.search(r"unsubscribe|preferences|settings|/alerts?(?:/|$)|/account|/login|/privacy|/help|/terms", u.path, re.I):
        return None
    keep = urlencode([(k, v) for k, v in parse_qs(u.query).items() for v in v if not TRACKING_PARAMS.match(k)])
    return f"{u.scheme}://{u.netloc}{u.path}" + (f"?{keep}" if keep else "")


def parse_alert(kind: str, html: str, text: str = "", message_id: str = "", link_fn: Callable[[str], str | None] | None = None
                ) -> list[AlertItem]:
    """Every job in one alert email: the anchor text is the title; the text pieces after it, before the next job, give the
    company and the place ('Company · London, UK', or two pieces). Missing parts are left empty, never guessed. `link_fn(href)` says
    which anchors are job links (default: the dedicated pattern of `kind`; the generic parser passes generic_link)."""
    p = _Flow()
    p.feed(html or "")
    p.close()
    items = p.items
    link_of = link_fn or (lambda href: job_link(kind, href))
    jobs = [(i, link_of(it[1])) for i, it in enumerate(items) if it[0] == "a" and link_of(it[1])]
    out: list[AlertItem] = []
    seen: set[str] = set()
    for n, (i, link) in enumerate(jobs):
        title = re.sub(r"\s+(?:with verification|new)$", "", items[i][2], flags=re.I).strip()
        if link in seen or NOISE.match(title) or not title:
            continue
        end = jobs[n + 1][0] if n + 1 < len(jobs) else min(len(items), i + 8)
        pieces = [it[1] if it[0] == "t" else it[2] for it in items[i + 1:end] if (it[0] == "t" or not link_of(it[1]))]
        pieces = [x for x in pieces if x and not NOISE.match(x) and not SALARY.match(x)][:3]
        company, place = ("", "")
        if pieces:
            company, place = _split_company_place(pieces[0])
            if not place and len(pieces) > 1:
                place = pieces[1]
        if kind == "cvlibrary" and is_cv_tracking(link) and not (company and place):
            continue                       # every link of the mail is a tracker link: only one followed by a company and a place is a job
        seen.add(link)
        extra_end = next((jobs[m][0] for m in range(n + 1, len(jobs)) if jobs[m][1] != link), len(items))
        out.append(AlertItem(kind, title, company, place, link, message_id, **_card_extras(kind, link, place, items, i, extra_end, jobs)))
    return out


WORK_TYPE = re.compile(r"\b(remote|hybrid|on[- ]?site)\b", re.I)
EASY_APPLY = re.compile(r"\beasy apply\b", re.I)


def _card_extras(kind: str, link: str, place: str, items: list[tuple], i: int, end: int, jobs: list[tuple]) -> dict:
    """What a card shows besides title, company and place: its job id (linkedin), work type, salary line and the Easy Apply marker. Read from the
    pieces between this card's title and the next card of ANOTHER link (a card's own 'View job' / 'Easy Apply' anchors repeat its link)."""
    texts = [it[1] if it[0] == "t" else it[2] for it in items[i + 1:end]]
    same_link = [items[j][2] for j, lk in jobs if lk == link and j != i]            # the card's own other anchors: "Easy Apply", "View job"
    pool = [*texts, *same_link]
    wt = next((WORK_TYPE.search(x) for x in [place, *pool] if WORK_TYPE.search(x or "")), None)
    work = re.sub(r"on[- ]?site", "on-site", wt.group(1).lower()) if wt else ""
    salary = next((x for x in texts if SALARY.search(x) and len(x) <= 80), "")
    job_id = (re.search(r"(\d{6,})$", urlparse(link).path) or re.search(r"(\d{6,})", link) or [None, ""])[1] if kind == "linkedin" else ""
    return {"job_id": job_id or "", "work_type": work, "salary": salary, "easy_apply": any(EASY_APPLY.search(x) for x in pool)}
