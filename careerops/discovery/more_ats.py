"""More employer boards (v2.6), ported from career-ops providers/ (commit 20b9fd3): the behaviour, not the JavaScript.

Single-company ATS providers. Each turns a board address into a list of `ats.Posting` (title, URL, place, posted time, description when the API gives one):

  workable         GET apply.workable.com/api/v1/widget/accounts/<slug>?details=true      name = reply `name`
  smartrecruiters  GET api.smartrecruiters.com/v1/companies/<slug>/postings (+ one detail GET per posting worth reading)   name = posting company.name
  teamtailor       GET <slug>.teamtailor.com/jobs.rss                                     name = RSS channel title
  recruitee        GET <slug>.recruitee.com/api/offers/                                   name = offer company_name
  personio         GET <slug>.jobs.personio.(de|com)/xml                                  name = position subcompany
  pinpoint         GET <slug>.pinpointhq.com/postings.json                                no name in the feed: LINK-ONLY
  eploy            GET <host>/live-jobs.xml (+ one detail page each, JSON-LD)             no name in the feed: CONFIG-ONLY

Rules (CLAUDE.md): public no-auth endpoints only; every request goes through discovery/http.py (robots.txt first, one request a second per host, a
401 / 403 / 429 stops the host and is never worked around); no cookie, no session, no key, no forged Referer / User-Agent (the reference sends a
browser User-Agent and an Origin to Workable: this tool does not). A slug GUESSED from a company name is allowed only for the providers whose reply
names the company (NAME_CHECKED) and is held to the board-name check in boards.py (0.90). Pinpoint and Eploy are used only from a real link or from
your configuration, never from a guess.

Multi-employer VC portfolio boards (`getro`, `consider`) are read by `vc_read`. Getro's API host disallows scripts in its robots.txt and Consider's
search endpoint needs a session cookie and a CSRF token; both are refused at run time (see their sources.yaml notes), not worked around.

`link_board(url)` names the board (provider, slug) and the posting a link points to, so an alert whose link is on one of these hosts resolves to the
employer's own posting (discover.resolve_link). `liveness(url, fetcher)` answers "is it still on the board".
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote, unquote, urlparse

from . import freshness
from .ats import Board, Posting
from .http import Blocked, FetchError, Fetcher
from .sources import html_to_text

NAME_CHECKED = ("workable", "smartrecruiters", "teamtailor", "recruitee", "personio")      # may be found by guessing a slug; the reply names the company
LINK_ONLY = ("pinpoint",)                                                                  # no name in the feed: only from a link
CONFIG_ONLY = ("eploy",)                                                                   # no name in the feed: only from `sources.eploy`
SINGLE = NAME_CHECKED + LINK_ONLY + CONFIG_ONLY
VC = ("getro", "consider")
SUBDOMAIN = ("teamtailor", "recruitee", "personio", "pinpoint")                            # one host per company: an unknown slug is a name that does not resolve

SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOST_RX = re.compile(r"^[a-z0-9][a-z0-9.-]{0,120}\.[a-z]{2,}$")
SR_PAGE, SR_MAX_PAGES = 100, 50
DETAIL_CAP = 40                          # postings read in full per board per run (those in a target role family and place)
GETRO_PAGE, GETRO_MAX_PAGES, GETRO_MAX_AGE_DAYS = 20, 40, 90
CONSIDER_SIZE = 500


class SessionRequired(Blocked):
    """The endpoint answers only to a browser session (a cookie and a CSRF token). This tool uses no session: the source is stopped, not worked around."""


@dataclass
class Reply:
    """What one board answered: is it a board, the company name it gives itself, its postings."""
    found: bool = False
    name: str = ""
    postings: list[Posting] = field(default_factory=list)
    slug: str = ""                       # the slug to keep (Personio: the host that answered)
    dns: bool = False                    # the host does not resolve: no such company on this provider (not an error, not remembered)
    detail: bool = False                 # postings still lack their description: fill() reads them one by one


# ------------------------------------------------------------------ small helpers
def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    if not (SLUG_RX.match(s) or HOST_RX.match(s)):
        raise FetchError(f"{s!r} is not a board name this tool will put in an address")
    return s


def _tag(block: str, tag: str) -> str:
    m = re.search(rf"<{re.escape(tag)}\b[^>]*>([\s\S]*?)</{re.escape(tag)}>", block, re.I)
    if not m:
        return ""
    inner = m.group(1)
    cdata = re.match(r"^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$", inner)
    return cdata.group(1).strip() if cdata else htmllib.unescape(inner).strip()


def _safe_xml(text: str) -> bool:
    """A feed with a DOCTYPE or ENTITY declaration is refused (nothing here expands entities, but nothing needs them either)."""
    return "<!DOCTYPE" not in text[:2000].upper() and "<!ENTITY" not in text[:4000].upper()


def _https(url: object) -> str:
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    return u.geturl() if u.scheme == "https" and u.hostname else ""


def _iso(value: object) -> str | None:
    """An ISO time from the forms these feeds use (ISO, `2026-10-20 09:11:52 UTC`, RFC 822), else None."""
    text = str(value or "").strip()
    if not text:
        return None
    if freshness.is_iso(text):
        return text
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC$", text)
    if m:
        return f"{m.group(1)}T{m.group(2)}+00:00"
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _place(*parts: object) -> str:
    return ", ".join(dict.fromkeys(str(p).strip() for p in parts if isinstance(p, str) and p.strip()))


def clean_name(name: str) -> str:
    """A feed title as a company name: 'Acme - Jobs' / 'Jobs at Acme' / 'Acme Careers' -> 'Acme'."""
    t = re.sub(r"\s*[-–—|:]\s*(jobs?|careers?|vacancies|open positions|work with us|join us)\s*$", "", name or "", flags=re.I)
    t = re.sub(r"^\s*(jobs?|careers?|vacancies)\s+(at|with|@)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(jobs?|careers?|vacancies)\s*$", "", t, flags=re.I)
    return t.strip()


# ------------------------------------------------------------------ the addresses
def endpoint(ats: str, slug: str) -> tuple[str, dict | None, str]:
    """(url, params, what) of the call that lists a board."""
    s = _slug(slug)
    if ats == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{s}", {"details": "true"}, f"Workable account {s}"
    if ats == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{s}/postings", {"limit": SR_PAGE, "offset": 0, "status": "PUBLIC"}, f"SmartRecruiters company {s}"
    if ats == "teamtailor":
        return f"https://{s if '.' in s else s + '.teamtailor.com'}/jobs.rss", None, f"Teamtailor feed {s}"
    if ats == "recruitee":
        return f"https://{s}.recruitee.com/api/offers/", None, f"Recruitee {s}"
    if ats == "personio":
        return f"https://{s if '.' in s else s + '.jobs.personio.de'}/xml", None, f"Personio {s}"
    if ats == "pinpoint":
        return f"https://{s}.pinpointhq.com/postings.json", None, f"Pinpoint {s}"
    if ats == "eploy":
        return f"https://{s}/live-jobs.xml", None, f"Eploy {s}"
    raise ValueError(f"unknown provider {ats!r}")


# ------------------------------------------------------------------ Workable
def parse_workable(data: dict, company: str, slug: str) -> list[Posting]:
    """The widget answers one entry per LOCATION of a multi-country posting (same shortcode, title, text): they are folded into one posting whose place lists
    them all (' · '), as the reference does, so a UK block rule does not reject a role that also has a UK entry."""
    out: dict[str, Posting] = {}
    places: dict[str, list[str]] = {}
    for j in data.get("jobs") or []:
        title = str(j.get("title") or "").strip()
        url = _https(j.get("url")) or _https(j.get("shortlink"))
        if not title or not url or urlparse(url).hostname != "apply.workable.com":
            continue
        place = _place(j.get("city"), j.get("country")) or ("Remote" if j.get("telecommuting") else "")
        code = str(j.get("shortcode") or url.rstrip("/").rsplit("/", 1)[-1])
        if code in out:
            if place and place not in places[code]:
                places[code].append(place)
            continue
        out[code] = Posting(code, company, title, place, url, html_to_text(j.get("description")), _iso(j.get("published_on") or j.get("created_at")),
                            str(j.get("country") or ""))
        places[code] = [place] if place else []
    for code, p in out.items():
        p.location = " · ".join(places[code])
    return list(out.values())


def _fetch_workable(slug: str, f: Fetcher, company: str) -> Reply:
    url, params, what = endpoint("workable", slug)
    d = f.json(url, params=params, what=what)
    if not (isinstance(d, dict) and isinstance(d.get("jobs"), list)):
        return Reply()
    name = str(d.get("name") or "")
    return Reply(True, name, parse_workable(d, company or name, slug), slug)


# ------------------------------------------------------------------ SmartRecruiters
def sr_url(slug: str, j: dict) -> str:
    pid = str(j.get("id") or "")
    if not pid:
        return ""
    words = re.sub(r"[^a-z0-9]+", "-", str(j.get("name") or "").lower()).strip("-")
    return f"https://jobs.smartrecruiters.com/{slug}/{pid}" + (f"-{words}" if words else "")


def parse_smartrecruiters(content: list, company: str, slug: str) -> list[Posting]:
    out = []
    for j in content:
        title = str(j.get("name") or "").strip()
        url = sr_url(slug, j)
        if not title or not url:
            continue
        loc = j.get("location") or {}
        place = str(loc.get("fullLocation") or "") or _place(loc.get("city"), loc.get("region"), (loc.get("country") or "").upper())
        if loc.get("remote"):
            place = _place(place, "Remote")
        out.append(Posting(str(j.get("id")), company or str((j.get("company") or {}).get("name") or ""), title, place, url, "",
                           _iso(j.get("releasedDate")), str(loc.get("country") or "").upper()))
    return out


def _fetch_smartrecruiters(slug: str, f: Fetcher, company: str) -> Reply:
    url, params, what = endpoint("smartrecruiters", slug)
    content: list = []
    name = ""
    for page in range(SR_MAX_PAGES):
        d = f.json(url, params={**params, "offset": page * SR_PAGE}, what=what)
        batch = d.get("content") if isinstance(d, dict) else None
        if not isinstance(batch, list):
            return Reply()
        if page == 0 and batch:
            name = str(((batch[0] or {}).get("company") or {}).get("name") or "")
        content += batch
        if len(batch) < SR_PAGE:
            break
    if not content:                          # an unknown company answers an empty list too: an empty board is not told apart, so it is not a board
        return Reply()
    return Reply(True, name, parse_smartrecruiters(content, company or name, _slug(slug)), _slug(slug), detail=True)


def sr_detail_text(d: dict) -> str:
    sections = ((d or {}).get("jobAd") or {}).get("sections") or {}
    parts = [str((sections.get(k) or {}).get("text") or "") for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
    return html_to_text("\n".join(p for p in parts if p.strip()))


# ------------------------------------------------------------------ Teamtailor
def parse_teamtailor(xml: str, company: str) -> tuple[str, list[Posting]]:
    channel = re.search(r"<channel\b[^>]*>([\s\S]*?)(?=<item\b|</channel>)", xml, re.I)
    name = clean_name(_tag(channel.group(1), "title")) if channel else ""
    out = []
    for item in re.findall(r"<item\b[^>]*>[\s\S]*?</item>", xml, re.I):
        url, title = _https(_tag(item, "link")), _tag(item, "title")
        if not url or not title:
            continue
        place = _place(_tag(item, "tt:city"), _tag(item, "tt:country"))
        if not place and _tag(item, "remoteStatus").lower() in ("fully", "temporary"):
            place = "Remote"
        m = re.search(r"/jobs/(\d+)", url)
        out.append(Posting(m.group(1) if m else url, company or name, title, place, url, html_to_text(_tag(item, "description")), _iso(_tag(item, "pubDate"))))
    return name, out


def _fetch_teamtailor(slug: str, f: Fetcher, company: str) -> Reply:
    url, _, what = endpoint("teamtailor", slug)
    r = f.request(url, what=what)
    if r.status_code == 404:
        return Reply()
    if not r.ok:
        raise FetchError(f"{what}: HTTP {r.status_code}")
    text = r.text or ""
    if "<rss" not in text[:2000].lower() or not _safe_xml(text):
        return Reply()
    name, posts = parse_teamtailor(text, company)
    return Reply(True, name, posts, _slug(slug))


# ------------------------------------------------------------------ Recruitee
def parse_recruitee(data: dict, company: str) -> tuple[str, list[Posting]]:
    out, name = [], ""
    for j in data.get("offers") or []:
        if not isinstance(j, dict) or re.search(r"\(sample\)", str(j.get("title") or ""), re.I):
            continue
        name = name or str(j.get("company_name") or "")
        title = str(j.get("title") or "").strip()
        url = _https(j.get("careers_url")) or _https(j.get("url"))
        if not title or not url:
            continue
        place = str(j.get("location") or "").strip() or _place(j.get("city"), j.get("country"))
        if j.get("remote") and "remote" not in place.lower():
            place = _place(place, "Remote")
        out.append(Posting(str(j.get("slug") or j.get("id") or url), company or str(j.get("company_name") or ""), title, place, url,
                           html_to_text("\n".join(str(j.get(k) or "") for k in ("description", "requirements"))),
                           _iso(j.get("published_at")), str(j.get("country_code") or "")))
    return name, out


def _fetch_recruitee(slug: str, f: Fetcher, company: str) -> Reply:
    url, _, what = endpoint("recruitee", slug)
    d = f.json(url, what=what)
    if not (isinstance(d, dict) and isinstance(d.get("offers"), list)):
        return Reply()
    name, posts = parse_recruitee(d, company)
    return Reply(True, name, posts, _slug(slug))


# ------------------------------------------------------------------ Personio
def parse_personio(xml: str, company: str, host: str) -> tuple[str, list[Posting]]:
    out, name = [], ""
    for block in re.findall(r"<position\b[^>]*>[\s\S]*?</position>", xml, re.I):
        pid = _tag(block, "id")
        head = re.sub(r"<jobDescriptions\b[\s\S]*?</jobDescriptions>", "", block, flags=re.I)
        title = _tag(head, "name")
        if not title or not pid.isdigit():
            continue
        name = name or _tag(head, "subcompany")
        offices = list(dict.fromkeys(htmllib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", o)).strip()
                                     for o in re.findall(r"<office\b[^>]*>([\s\S]*?)</office>", head, re.I) if o.strip()))
        text = "\n\n".join(f"{_tag(d, 'name')}\n{html_to_text(_tag(d, 'value'))}".strip()
                           for d in re.findall(r"<jobDescription\b[^>]*>[\s\S]*?</jobDescription>", block, re.I))
        out.append(Posting(pid, company or _tag(head, "subcompany"), title, ", ".join(offices), f"https://{host}/job/{pid}", text, _iso(_tag(head, "createdAt"))))
    return name, out


def _fetch_personio(slug: str, f: Fetcher, company: str) -> Reply:
    s = _slug(slug)
    hosts = [s] if "." in s else [f"{s}.jobs.personio.de", f"{s}.jobs.personio.com"]
    for host in hosts:
        url, _, what = endpoint("personio", host)
        r = f.request(url, what=what)
        if r.status_code == 404:
            continue
        if not r.ok:
            raise FetchError(f"{what}: HTTP {r.status_code}")
        text = r.text or ""
        if "<workzag-jobs" not in text[:2000].lower() or not _safe_xml(text):
            continue
        name, posts = parse_personio(text, company, host)
        return Reply(True, name, posts, host)
    return Reply()


# ------------------------------------------------------------------ Pinpoint
def parse_pinpoint(data: dict, company: str) -> list[Posting]:
    out = []
    for j in data.get("data") or []:
        if not isinstance(j, dict) or "youtube.com/embed/pFxm6fszrpw" in str(j.get("description") or ""):      # Pinpoint's own demo posting
            continue
        title, url = str(j.get("title") or "").strip(), _https(j.get("url"))
        if not title or not url:
            continue
        loc = j.get("location") if isinstance(j.get("location"), dict) else {}
        place = str(loc.get("name") or "").strip() or _place(loc.get("city"), loc.get("province"))
        text = "\n\n".join(html_to_text(str(j.get(k) or "")) for k in ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits") if j.get(k))
        out.append(Posting(str(j.get("id") or url.rstrip("/").rsplit("/", 1)[-1]), company, title, place, url, text, None))
    return out


def _fetch_pinpoint(slug: str, f: Fetcher, company: str) -> Reply:
    url, _, what = endpoint("pinpoint", slug)
    d = f.json(url, what=what)
    if not (isinstance(d, dict) and isinstance(d.get("data"), list)):
        return Reply()
    return Reply(True, "", parse_pinpoint(d, company), _slug(slug))


# ------------------------------------------------------------------ Eploy
EPLOY_JOB = re.compile(r"^/vacancies/(\d+)/([^/?#]+)\.html/?$", re.I)


def title_from_slug(slug: str) -> str:
    words = re.sub(r"[-_]+", " ", unquote(slug)).split()
    keep = {"ai": "AI", "api": "API", "hr": "HR", "it": "IT", "ml": "ML", "qa": "QA", "uk": "UK", "ui": "UI", "ux": "UX", "cto": "CTO"}
    return " ".join(keep.get(w.lower(), w[:1].upper() + w[1:]) for w in words)


def parse_eploy(xml: str, host: str, company: str) -> list[Posting]:
    out, seen = [], set()
    for loc in re.findall(r"<loc\b[^>]*>([\s\S]*?)</loc\s*>", xml, re.I):
        u = urlparse(htmllib.unescape(loc.strip()))
        m = EPLOY_JOB.match(u.path or "")
        ok_host = (u.hostname or "") == host or ((u.hostname or "").endswith(".eploy.net"))
        if u.scheme != "https" or not m or not ok_host or u.query or u.username or u.port or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append(Posting(m.group(1), company, title_from_slug(m.group(2)), "", u.geturl(), "", None))
    return out


def parse_eploy_detail(page: str) -> dict:
    """Title, place, description and posted time from the JSON-LD JobPosting of a vacancy page ({} when there is none)."""
    for raw in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script\s*>', page or "", re.I):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
            elif isinstance(x, dict):
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    locs = x.get("jobLocation") if isinstance(x.get("jobLocation"), list) else [x.get("jobLocation")]
                    places = []
                    for loc in locs:
                        a = (loc or {}).get("address") if isinstance(loc, dict) else None
                        if isinstance(a, dict):
                            c = a.get("addressCountry")
                            places.append(_place(a.get("addressLocality"), a.get("addressRegion"), c.get("name") if isinstance(c, dict) else c))
                    return {"title": str(x.get("title") or "").strip(), "location": "; ".join(p for p in places if p),
                            "description": html_to_text(str(x.get("description") or "")), "posted": _iso(x.get("datePosted"))}
                stack += list(x.get("@graph") or [])
    return {}


def _fetch_eploy(slug: str, f: Fetcher, company: str) -> Reply:
    url, _, what = endpoint("eploy", slug)
    r = f.request(url, what=what)
    if r.status_code == 404:
        return Reply()
    if not r.ok:
        raise FetchError(f"{what}: HTTP {r.status_code}")
    text = r.text or ""
    if "<urlset" not in text[:3000].lower() or not _safe_xml(text):
        return Reply()
    return Reply(True, "", parse_eploy(text, _slug(slug), company), _slug(slug), detail=True)


# ------------------------------------------------------------------ reading a board
_FETCH: dict[str, Callable[[str, Fetcher, str], Reply]] = {
    "workable": _fetch_workable, "smartrecruiters": _fetch_smartrecruiters, "teamtailor": _fetch_teamtailor, "recruitee": _fetch_recruitee,
    "personio": _fetch_personio, "pinpoint": _fetch_pinpoint, "eploy": _fetch_eploy}


def fetch(ats: str, slug: str, fetcher: Fetcher, company: str = "") -> Reply:
    """Ask one provider about one board. Raises Blocked / FetchError. A host that does not resolve (a one-host-per-company provider, an unknown slug)
    is `Reply(dns=True)`: no such board, not an error."""
    try:
        reply = _FETCH[ats](slug, fetcher, company)
    except Blocked:
        raise
    except FetchError as e:
        if ats in SUBDOMAIN and "ConnectionError" in str(e):
            return Reply(dns=True)
        if "reply was not JSON" in str(e):                    # an unknown company's host answers with a web page (Recruitee redirects to recruitee.com/careers_not_hosted): no board
            return Reply()
        raise
    return reply


def fill(ats: str, slug: str, post: Posting, fetcher: Fetcher) -> Posting:
    """Read the one thing a list lacks: SmartRecruiters' ad text (a detail call), Eploy's place and text (the vacancy page's JSON-LD). A failure leaves the
    posting as it was."""
    try:
        if ats == "smartrecruiters":
            d = fetcher.json(f"https://api.smartrecruiters.com/v1/companies/{_slug(slug)}/postings/{quote(post.ext_id, safe='')}", what="SmartRecruiters posting")
            post.description = sr_detail_text(d) if isinstance(d, dict) else ""
        elif ats == "eploy":
            r = fetcher.request(post.url, what="Eploy vacancy")
            d = parse_eploy_detail(r.text or "") if r.ok else {}
            post.title = d.get("title") or post.title
            post.location = d.get("location") or post.location
            post.description = d.get("description") or post.description
            post.posted_at = d.get("posted") or post.posted_at
    except Blocked:
        raise
    except (FetchError, ValueError):
        pass
    return post


def board_postings(board: Board, fetcher: Fetcher, keep: Callable[[Posting], bool] | None = None) -> list[Posting]:
    """Every open posting on the board under the name `board.name`. Where the list has no text, the ones `keep` accepts (a role family and place that
    match; at most DETAIL_CAP) are read in full. [] for a board that does not exist. Raises FetchError / Blocked."""
    reply = fetch(board.ats, board.slug, fetcher, board.name)
    if not reply.found:
        return []
    posts = reply.postings
    for p in posts:
        p.company = board.name or p.company or reply.name
    if reply.detail:
        n = 0
        for p in posts:
            if n >= DETAIL_CAP:
                break
            if keep is None or keep(p):
                fill(board.ats, reply.slug or board.slug, p, fetcher)
                n += 1
    return posts


# ------------------------------------------------------------------ links
@dataclass(frozen=True)
class Link:
    ats: str
    slug: str
    ref: str = ""                        # the posting's own id in the link ('' = the link names only the board)


_LINKS = (
    ("greenhouse", re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$"), re.compile(r"^/([A-Za-z0-9_-]+)/jobs/(\d+)")),
    ("lever", re.compile(r"^jobs\.lever\.co$"), re.compile(r"^/([A-Za-z0-9_-]+)/([0-9a-f-]{8,})")),
    ("lever_eu", re.compile(r"^jobs\.eu\.lever\.co$"), re.compile(r"^/([A-Za-z0-9_-]+)/([0-9a-f-]{8,})")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$"), re.compile(r"^/([A-Za-z0-9_-]+)/([0-9a-f-]{8,})")),
    ("workable", re.compile(r"^apply\.workable\.com$"), re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]*)/j/([A-Za-z0-9]+)")),
    ("smartrecruiters", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com$"), re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]*)/(\d+)")),
    ("teamtailor", re.compile(r"^([a-z0-9][a-z0-9-]*)\.teamtailor\.com$", re.I), re.compile(r"^/jobs/(\d+)")),
    ("recruitee", re.compile(r"^([a-z0-9][a-z0-9-]*)\.recruitee\.com$", re.I), re.compile(r"^/o/([^/?#]+)")),
    ("pinpoint", re.compile(r"^([a-z0-9][a-z0-9-]*)\.pinpointhq\.com$", re.I), re.compile(r"^(?:/[a-z]{2})?/postings/([0-9a-f-]{8,})")),
    ("personio", re.compile(r"^([a-z0-9][a-z0-9-]*\.jobs\.personio\.(?:de|com))$", re.I), re.compile(r"^/job/(\d+)")),
)


def link_board(url: str | None) -> Link | None:
    """The board (and posting) a link on one of these hosts names: apply.workable.com/<slug>/j/<id>, <slug>.teamtailor.com/jobs/<id>-..., ... and a
    Workday careers address. None for any other link."""
    from . import workday
    u = urlparse(str(url or "").strip())
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return None
    for ats, hrx, prx in _LINKS:
        hm = hrx.match(host)
        if not hm:
            continue
        pm = prx.match(u.path or "")
        if ats in ("workable", "smartrecruiters", "greenhouse", "lever", "lever_eu", "ashby"):
            return Link(ats, pm.group(1) if ats in ("greenhouse", "lever", "lever_eu", "ashby") else pm.group(1).lower(), pm.group(2)) if pm else None
        slug = hm.group(1).lower()
        return Link(ats, slug, pm.group(1) if pm else "")
    site = workday.parse_url(str(url))
    if site is not None:
        tail = (u.path or "").rstrip("/").rsplit("/", 1)[-1]
        return Link("workday", site.slug, tail if "/job/" in (u.path or "") else "")
    return None


def matches(post: Posting, link: Link) -> bool:
    """Is this posting the one the link points to (its id, or the id inside its address)?"""
    if not link.ref:
        return False
    ref = link.ref.lower()
    return post.ext_id.lower() == ref or ref in post.url.lower()


def liveness(url: str, fetcher: Fetcher):
    """('open'|'closed', reason) from the employer's board, or None when the board cannot answer (the generic page check then goes on). A posting that the
    board lists is open; one it does not list is closed. Workday links are left to the page check."""
    link = link_board(url)
    if link is None or link.ats not in SINGLE or not link.ref:        # Greenhouse / Lever / Ashby / Workday links are left to the page check
        return None
    try:
        reply = fetch(link.ats, link.slug, fetcher)
    except (Blocked, FetchError):
        return None
    if not reply.found:
        return None
    if any(matches(p, link) for p in reply.postings):
        return "open", f"listed on the {link.ats} board of {link.slug}"
    return "closed", f"not on the {link.ats} board of {link.slug} any more"


# ------------------------------------------------------------------ sources.yaml entries (printed by `careerops doctor`; the tool never edits that file)
SOURCES_YAML = (
    "  - {id: workable-boards, regions: [any], name: Workable boards, url: 'https://apply.workable.com', country: multi, method: workable, note: 'public widget API "
    "apply.workable.com/api/v1/widget/accounts/<slug>?details=true; robots.txt allows it (checked 3 Nov 2026); a guessed slug must pass the 0.90 board-name check'}",
    "  - {id: smartrecruiters-boards, regions: [any], name: SmartRecruiters boards, url: 'https://careers.smartrecruiters.com', country: multi, method: smartrecruiters, "
    "enabled: false, note: 'api.smartrecruiters.com/robots.txt is User-agent * Disallow / (only LinkedInBot is allowed) as checked 3 Nov 2026, so this tool refuses it; "
    "set enabled: true and add smartrecruiters to discover.ats.providers if that changes'}",
    "  - {id: teamtailor-boards, regions: [any], name: Teamtailor boards, url: 'https://www.teamtailor.com', country: multi, method: teamtailor, note: 'public RSS "
    "<slug>.teamtailor.com/jobs.rss (company = channel title, name-checked); each tenant host has its own robots.txt, read before the first request'}",
    "  - {id: pinpoint-boards, regions: [any], name: Pinpoint boards, url: 'https://www.pinpointhq.com', country: multi, method: pinpoint, note: 'link-detected only: "
    "<slug>.pinpointhq.com/postings.json has no company name, so a slug is never guessed; some tenants disallow scripts in robots.txt (honoured)'}",
    "  - {id: recruitee-boards, regions: [any], name: Recruitee boards, url: 'https://recruitee.com', country: multi, method: recruitee, note: 'public "
    "<slug>.recruitee.com/api/offers/ (company = offer company_name, name-checked)'}",
    "  - {id: personio-boards, regions: [any], name: Personio boards, url: 'https://www.personio.com', country: multi, method: personio, note: 'public XML feed "
    "<slug>.jobs.personio.(de|com)/xml (company = position subcompany, name-checked); robots.txt allows it (checked 3 Nov 2026)'}",
    "  - {id: workday-boards, regions: [any], name: Workday boards, url: 'https://www.workday.com', country: multi, method: workday, note: 'the sites under sources.workday "
    "in settings.yaml, and alert links on *.myworkdayjobs.com (public CXS list API only, never the careers pages)'}",
    "  - {id: vc-atomico, regions: [any], name: Atomico portfolio jobs, url: 'https://www.atomico.com', country: multi, method: getro, enabled: false, note: 'NOT read: "
    "atomico.com sits behind a Vercel security checkpoint (a bot check, not bypassed) so its board address could not be found from the website of the fund itself, and "
    "api.getro.com/robots.txt is User-agent * Disallow / (checked 3 Nov 2026)'}",
    "  - {id: vc-balderton, regions: [any], name: Balderton portfolio jobs, url: 'https://careers.balderton.com/jobs', country: multi, method: consider, token: balderton-capital, "
    "enabled: false, note: 'board found on balderton.com; robots.txt allows; provider confirmed as Consider from the page; NOT read: POST /api-boards/search-jobs answers "
    "HTTP 412 INVALID_CSRF without a session cookie and CSRF token, which this tool does not use (checked 3 Nov 2026)'}",
    "  - {id: vc-notion, regions: [any], name: Notion Capital portfolio jobs, url: 'https://jobs.notioncapital.com/jobs', country: multi, method: consider, token: notion-capital, "
    "enabled: false, note: 'board found on notioncapital.com (notion.vc redirects there); robots.txt allows; provider confirmed as Consider from the page; NOT read: "
    "the search API answers HTTP 412 INVALID_CSRF without a session cookie and CSRF token (checked 3 Nov 2026)'}",
)


def missing_source_lines(registry) -> list[str]:
    """The SOURCES_YAML entries whose id the registry does not have yet."""
    have = {s.id for s in registry}
    return [x for x in SOURCES_YAML if re.search(r"\{id: ([a-z0-9-]+)", x).group(1) not in have]


def config_problems(settings) -> list[str]:
    """What is wrong in the settings these providers read: `discover.ats.providers` names, `sources.<provider>` slugs, `sources.eploy` entries."""
    out: list[str] = []
    wanted = (settings["discover"].get("ats") or {}).get("providers")
    if wanted is not None and not isinstance(wanted, list):
        out.append("discover.ats.providers must be a list of provider names")
    for p in wanted if isinstance(wanted, list) else []:
        if p not in NAME_CHECKED:
            out.append(f"discover.ats.providers: {p!r} is not one of {', '.join(NAME_CHECKED)} (pinpoint and eploy are never guessed; list them under sources:)")
    src = settings["sources"] or {}
    for key in SINGLE:
        for e in src.get(key) or []:
            if key == "eploy":
                host = urlparse(str(e.get("url") or "")).hostname if isinstance(e, dict) else None
                if not (isinstance(e, dict) and e.get("company") and host and HOST_RX.match(host.lower())):
                    out.append(f"sources.eploy: {e!r} needs company and an https url of the tenant")
                continue
            slug = str(e.get("slug") if isinstance(e, dict) else e).lower()
            if not (SLUG_RX.match(slug) or (key in ("teamtailor", "personio") and HOST_RX.match(slug))):
                out.append(f"sources.{key}: {slug!r} is not a board name")
    return out


# ------------------------------------------------------------------ VC portfolio boards (multi-employer)
def _epoch_iso(v: object) -> str | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return _iso(v)
    if n <= 0:
        return None
    return datetime.fromtimestamp(n / 1000 if n >= 1e12 else n, timezone.utc).isoformat(timespec="seconds")


def getro_collection(page: str) -> str:
    """The numeric collection id of a Getro board, from the board page's __NEXT_DATA__ (props.pageProps.network.id)."""
    m = re.search(r'<script\b[^>]*\sid\s*=\s*["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', page or "")
    try:
        nid = json.loads(m.group(1))["props"]["pageProps"]["network"]["id"] if m else None
    except (ValueError, KeyError, TypeError):
        return ""
    return str(nid) if str(nid or "").isdigit() else ""


def consider_board(page: str) -> str:
    """The board id of a Consider board, from the page's serverInitialData (board.id)."""
    m = re.search(r"window\.serverInitialData\s*=\s*(\{.*?\})\s*;?\s*(?:</script>|$)", page or "", re.M)
    try:
        bid = json.loads(m.group(1)).get("board", {}).get("id") if m else ""
    except (ValueError, AttributeError):
        return ""
    return str(bid or "")


def parse_getro(data: dict) -> list[Posting]:
    out = []
    for j in ((data or {}).get("results") or {}).get("jobs") or []:
        title, url = str(j.get("title") or "").strip(), _https(j.get("url"))
        if not title or not url:
            continue
        locs = [str(x).strip() for x in (j.get("locations") or j.get("searchable_locations") or []) if isinstance(x, str) and x.strip()]
        if j.get("work_mode") == "remote" and not any("remote" in x.lower() for x in locs):
            locs.append("Remote")
        out.append(Posting(str(j.get("id") or url), str((j.get("organization") or {}).get("name") or ""), title, ", ".join(locs), url, "",
                           _epoch_iso(j.get("created_at"))))
    return out


def parse_consider(data: dict) -> list[Posting]:
    out = []
    for j in (data or {}).get("jobs") or []:
        title, url = str(j.get("title") or "").strip(), _https(j.get("url")) or _https(j.get("applyUrl"))
        if not title or not url:
            continue
        locs = [str(x).strip() for x in (j.get("locations") or []) if isinstance(x, str) and x.strip()]
        place = ", ".join(locs) or ("Remote" if j.get("remote") else "")
        out.append(Posting(str(j.get("id") or url), str(j.get("companyName") or ""), title, place, url, "", _epoch_iso(j.get("timeStamp"))))
    return out


def vc_read(method: str, page_url: str, token: str, fetcher: Fetcher, *, now: datetime | None = None) -> list[Posting]:
    """The jobs of one VC portfolio board (company = the portfolio company, no ad text: the caller looks for the full ad). Raises Blocked / FetchError."""
    u = urlparse(page_url)
    if u.scheme != "https" or not u.hostname or "." not in u.hostname:
        raise FetchError(f"{method}: {page_url!r} is not a public https board address")
    now = now or datetime.now(timezone.utc)
    if method == "getro":
        cid = token if token.isdigit() else getro_collection(fetcher.request(page_url, what="Getro board page").text or "")
        if not cid:
            raise FetchError(f"getro: no collection id on {page_url} (set `token:` in sources.yaml)")
        out: list[Posting] = []
        for page in range(GETRO_MAX_PAGES):
            d = fetcher.request(f"https://api.getro.com/api/v2/collections/{cid}/search/jobs", method="POST", what="Getro search",
                                json={"hitsPerPage": GETRO_PAGE, "page": page, "filters": {"page": page}, "query": ""})
            if not d.ok:
                raise FetchError(f"getro search: HTTP {d.status_code}")
            body = d.json()
            batch = parse_getro(body)
            out += batch
            total = int((((body or {}).get("results") or {}).get("count")) or 0)
            old = [p for p in batch if (freshness.parse(p.posted_at) or now) < now - timedelta(days=GETRO_MAX_AGE_DAYS)]
            if not batch or (page + 1) * GETRO_PAGE >= total or (old and len(old) == len(batch)):
                break
        return out
    if method == "consider":
        bid = token or consider_board(fetcher.request(page_url, what="Consider board page").text or "")
        if not bid:
            raise FetchError(f"consider: no board id on {page_url} (set `token:` in sources.yaml)")
        r = fetcher.request(f"https://{u.hostname}/api-boards/search-jobs", method="POST", what="Consider search",
                            json={"meta": {"size": CONSIDER_SIZE}, "board": {"id": bid, "isParent": True}, "query": {"promoteFeatured": True}})
        if r.status_code == 412:
            raise SessionRequired(f"consider: {u.hostname} answers only to a browser session (a cookie and a CSRF token; HTTP 412 INVALID_CSRF). "
                                  "This tool uses no cookie or session, so the board is not read.", 412)
        if not r.ok:
            raise FetchError(f"consider search: HTTP {r.status_code}")
        return parse_consider(r.json())
    raise ValueError(f"unknown VC provider {method!r}")
