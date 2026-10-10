"""URL intake (`careerops add-url`, `careerops live --url`): fetch ONE job ad and write it to data/jds/inbox/ as a JD file.
A site that answers 401 / 403 / 429 is not retried or worked around: `live --clipboard` takes the text you copied instead.

The file starts with `Company:` / `Title:` / `URL:` / `Location:` header lines (the ones `manual.header_fields` reads), then
the ad's text. Nothing here adds a job to the database or applies to anything: `careerops live` does that with you at every
gate. Public, unauthenticated APIs only, at most one request per second per host, identified by the shared `UA` header.

  Greenhouse  GET boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}        (job-boards.greenhouse.io, boards.greenhouse.io)
  Lever       GET api.lever.co/v0/postings/{site}/{id}  (api.eu.lever.co for jobs.eu.lever.co)  - documented single posting
  Ashby       the board list GET api.ashbyhq.com/posting-api/job-board/{board}, then the posting in it by its id. Ashby
              documents no single-posting endpoint, so this is the one documented call, filtered here.
  otherwise   the page itself: a JobPosting JSON-LD block if it has one, else the page's main text. Under MIN_PAGE_CHARS
              characters is refused rather than guessed at (a page that builds its text with JavaScript, a login wall).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests

from . import manual
from .sources import UA, html_to_text

MIN_PAGE_CHARS = 2000                  # a page with less text than this is not taken for a job description
MIN_HOST_GAP_S = 1.0                   # at most one request per second per host
MAX_BYTES = 5_000_000                  # a bigger response is not a job ad
GREENHOUSE_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io")
LEVER_HOSTS = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}
ASHBY_HOSTS = ("jobs.ashbyhq.com",)
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


BLOCKED_STATUSES = (401, 403, 429)     # the site says no to a script: no retry, no workaround, the human pastes the text
BLOCKED_MESSAGE = "This site blocks automated access — copy the job text and use --clipboard"
CLIPBOARD_CMD = ["powershell.exe", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard"]


class IntakeError(ValueError):
    """The ad could not be fetched or is not usable. The message is one line, fit to print as it is."""


class BlockedError(IntakeError):
    """The site answered 401 / 403 / 429. It is never retried or worked around."""


@dataclass
class Target:
    kind: str                          # greenhouse | lever | ashby | page
    url: str
    board: str = ""
    ad_id: str = ""
    api_host: str = ""


@dataclass
class Ad:
    company: str
    title: str
    url: str
    location: str
    text: str
    source: str
    ad_id: str


# ------------------------------------------------------------------ network (tests replace _get / _sleep / _clock)
_last: dict[str, float] = {}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _throttle(host: str) -> None:
    wait = _last.get(host, -MIN_HOST_GAP_S) + MIN_HOST_GAP_S - _clock()
    if wait > 0:
        _sleep(wait)
    _last[host] = _clock()


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=60, **kw)
    return r


def _fetch(url: str, what: str) -> requests.Response:
    """One GET, paced per host. Any failure becomes an IntakeError saying what could not be fetched."""
    _throttle(urlparse(url).netloc.lower())
    try:
        r = _get(url)
    except requests.RequestException as e:
        raise IntakeError(f"{what}: could not be fetched ({type(e).__name__}: {e})") from None
    if r.status_code in BLOCKED_STATUSES:
        raise BlockedError(BLOCKED_MESSAGE)
    if r.status_code == 404:
        raise IntakeError(f"{what}: not found (HTTP 404): the posting is closed, or the link is wrong")
    if not r.ok:
        raise IntakeError(f"{what}: HTTP {r.status_code}")
    if len(r.content or b"") > MAX_BYTES:
        raise IntakeError(f"{what}: the response is over {MAX_BYTES // 1_000_000} MB; that is not a job ad")
    return r


def _json(url: str, what: str):
    r = _fetch(url, what)
    try:
        return r.json()
    except ValueError:
        raise IntakeError(f"{what}: the reply was not JSON") from None


# ------------------------------------------------------------------ url -> target
def parse_url(url: str) -> Target:
    """Which kind of job page this is. Anything that is not a http(s) link raises IntakeError."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise IntakeError(f"not a web link: {url!r} (it must start with http:// or https://)")
    host, parts = u.netloc.lower().split(":")[0], [p for p in u.path.split("/") if p]
    full = u.geturl()
    if host in GREENHOUSE_HOSTS:
        q = parse_qs(u.query)
        if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
            return Target("greenhouse", full, board=parts[0], ad_id=parts[2])
        if parts[:2] == ["embed", "job_app"] and q.get("for") and (q.get("token") or [""])[0].isdigit():
            return Target("greenhouse", full, board=q["for"][0], ad_id=q["token"][0])
        raise IntakeError(f"{full}: a Greenhouse link needs the form {host}/<board>/jobs/<number>")
    if host in LEVER_HOSTS:
        if len(parts) >= 2 and UUID.fullmatch(parts[1]):
            return Target("lever", full, board=parts[0], ad_id=parts[1].lower(), api_host=LEVER_HOSTS[host])
        raise IntakeError(f"{full}: a Lever link needs the form {host}/<site>/<posting id>")
    if host in ASHBY_HOSTS:
        if len(parts) >= 2 and UUID.fullmatch(parts[1]):
            return Target("ashby", full, board=parts[0], ad_id=parts[1].lower())
        raise IntakeError(f"{full}: an Ashby link needs the form {host}/<board>/<posting id>")
    return Target("page", full, ad_id=hashlib.sha1(full.encode()).hexdigest()[:10])


def _name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def _http_url(candidate, fallback: str) -> str:
    c = str(candidate or "").strip()
    return c if urlparse(c).scheme in ("http", "https") else fallback


# ------------------------------------------------------------------ the APIs
def greenhouse_ad(t: Target) -> Ad:
    j = _json(f"https://boards-api.greenhouse.io/v1/boards/{t.board}/jobs/{t.ad_id}", f"Greenhouse job {t.board}/{t.ad_id}")
    return Ad(company=str(j.get("company_name") or "").strip() or _name(t.board), title=str(j.get("title") or "").strip(),
              url=_http_url(j.get("absolute_url"), t.url), location=str((j.get("location") or {}).get("name") or "").strip(),
              text=html_to_text(j.get("content")), source="greenhouse", ad_id=t.ad_id)


def lever_ad(t: Target) -> Ad:
    j = _json(f"https://{t.api_host}/v0/postings/{t.board}/{t.ad_id}", f"Lever posting {t.board}/{t.ad_id}")
    parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
    for lst in j.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
    parts.append(j.get("additionalPlain") or "")
    return Ad(company=_name(t.board), title=str(j.get("text") or "").strip(), url=_http_url(j.get("hostedUrl"), t.url),
              location=str((j.get("categories") or {}).get("location") or "").strip(),
              text="\n\n".join(p for p in parts if p).strip(), source="lever", ad_id=t.ad_id)


def ashby_ad(t: Target) -> Ad:
    data = _json(f"https://api.ashbyhq.com/posting-api/job-board/{t.board}", f"Ashby board {t.board}")
    for j in data.get("jobs") or []:
        if (str(j.get("id") or "").lower() == t.ad_id or t.ad_id in str(j.get("jobUrl") or "").lower()
                or t.ad_id in str(j.get("applyUrl") or "").lower()):
            loc = j.get("location") or ""
            return Ad(company=_name(t.board), title=str(j.get("title") or "").strip(),
                      url=_http_url(j.get("jobUrl"), t.url), location=str(loc.get("name") if isinstance(loc, dict) else loc).strip(),
                      text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), source="ashby", ad_id=t.ad_id)
    raise IntakeError(f"Ashby board {t.board}: no posting {t.ad_id} in its public list (closed, or not listed)")


# ------------------------------------------------------------------ any other page
class _Page(HTMLParser):
    """Visible text of a page, minus scripts, styles and page furniture; also the text inside <main>/<article>, the
    <title>, and the JSON-LD blocks."""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.keep = 0                       # depth inside <main> / <article>
        self.all: list[str] = []
        self.main: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self.ld: list[str] = []
        self._in_title = False
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("property") or a.get("name")) and a.get("content"):
            self.meta.setdefault((a.get("property") or a.get("name")).lower(), a["content"])
        elif tag == "script" and "ld+json" in (a.get("type") or "").lower():
            self._in_ld = True
            self.ld.append("")
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in ("main", "article"):
            self.keep += 1
        if self.skip == 0:
            mark = "\n- " if tag == "li" else "\n" if tag in self.BLOCK else ""
            if mark:
                self.all.append(mark)
                if self.keep:
                    self.main.append(mark)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_ld = False
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in ("main", "article") and self.keep:
            self.keep -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_ld:
            self.ld[-1] += data
        elif self.skip == 0:
            self.all.append(data)
            if self.keep:
                self.main.append(data)


def _tidy(chunks: list[str]) -> str:
    t = re.sub(r"[ \t\xa0]+", " ", "".join(chunks))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", t)).strip()


def _job_posting(blocks: list[str]) -> dict:
    """The first schema.org JobPosting in the page's JSON-LD blocks ({} when there is none)."""
    def walk(node):
        if isinstance(node, list):
            for n in node:
                yield from walk(n)
        elif isinstance(node, dict):
            kinds = node.get("@type")
            if "JobPosting" in (kinds if isinstance(kinds, list) else [kinds]):
                yield node
            for v in node.values():
                if isinstance(v, (list, dict)):
                    yield from walk(v)
    for raw in blocks:
        try:
            for node in walk(json.loads(raw)):
                return node
        except ValueError:
            continue
    return {}


def _ld_location(jp: dict) -> str:
    loc = jp.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    addr = (loc or {}).get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        return ", ".join(str(addr[k]) for k in ("addressLocality", "addressRegion", "addressCountry")
                         if isinstance(addr.get(k), (str, int)) and str(addr[k]).strip())
    return ""


def page_ad(t: Target) -> Ad:
    r = _fetch(t.url, t.url)
    p = _Page()
    p.feed(r.text)
    jp = _job_posting(p.ld)
    from_ld = html_to_text(jp.get("description")) if isinstance(jp.get("description"), str) else ""
    main, everything = _tidy(p.main), _tidy(p.all)
    text = next((c for c in (from_ld, main, everything) if len(c) >= MIN_PAGE_CHARS), max((from_ld, main, everything), key=len))
    if len(text) < MIN_PAGE_CHARS:
        raise IntakeError(f"{t.url}: only {len(text)} characters of text found (a job description needs at least "
                          f"{MIN_PAGE_CHARS}); the page probably builds its text with JavaScript or needs a login. "
                          "Save the ad as a .txt file by hand instead.")
    org = jp.get("hiringOrganization")
    company = (org.get("name") if isinstance(org, dict) else org) or p.meta.get("og:site_name") or ""
    title = jp.get("title") or p.meta.get("og:title") or " ".join(p.title.split())
    return Ad(company=str(company).strip(), title=str(title).strip(), url=t.url, location=_ld_location(jp), text=text,
              source="page", ad_id=t.ad_id)


# ------------------------------------------------------------------ the entry points
def fetch_ad(url: str, company: str | None = None, title: str | None = None) -> Ad:
    """Fetch the ad behind `url`. `company` / `title` replace what the page or API says."""
    t = parse_url(url)
    ad = {"greenhouse": greenhouse_ad, "lever": lever_ad, "ashby": ashby_ad, "page": page_ad}[t.kind](t)
    if company:
        ad.company = company.strip()
    if title:
        ad.title = title.strip()
    if len(ad.text.encode("utf-8")) < manual.MIN_JD_BYTES:
        raise IntakeError(f"{url}: the ad has only {len(ad.text)} characters of text (a job description needs at least "
                          f"{manual.MIN_JD_BYTES} bytes)")
    return ad


def slug(text: str, limit: int = 40) -> str:
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:limit].strip("-")


def file_name(ad: Ad) -> str:
    return "-".join(p for p in (slug(ad.company), slug(ad.title), slug(ad.ad_id, 36)) if p) + ".txt"


def render(ad: Ad) -> str:
    one_line = lambda s: " ".join(str(s).split())          # noqa: E731 — a header value is one line
    head = [f"Company: {one_line(ad.company)}", f"Title: {one_line(ad.title)}", f"URL: {ad.url}"]
    if one_line(ad.location):
        head.append(f"Location: {one_line(ad.location)}")
    return "\n".join(head) + "\n\n" + ad.text.strip() + "\n"


def write_inbox(inbox: Path, ad: Ad) -> tuple[Path, bool]:
    """Write the ad to <inbox>/<company>-<title>-<id>.txt and return (path, created). An existing file with that name is
    left exactly as it is (created=False): a fetch never overwrites. The headers must pass the same checks `careerops live`
    makes, so a file written here is never one that live would reject for its headers."""
    for field, value in (("company", ad.company), ("title", ad.title), ("url", ad.url)):
        if not value.strip():
            raise IntakeError(f"the {field} could not be read from {ad.url}: pass --company and --title")
        problem = manual.meta_problem(field, value)
        if problem:
            raise IntakeError(f"{problem} (from {ad.url}); pass --company and --title to set it yourself")
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / file_name(ad)
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(render(ad))
    except FileExistsError:
        return path, False
    return path, True


# ------------------------------------------------------------------ the Windows clipboard (from WSL)
def _run_clipboard() -> bytes:
    """Raw stdout of powershell.exe's Get-Clipboard (tests replace this)."""
    try:
        p = subprocess.run(CLIPBOARD_CMD, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise IntakeError("powershell.exe was not found: --clipboard reads the Windows clipboard from WSL") from None
    except subprocess.SubprocessError as e:
        raise IntakeError(f"the clipboard could not be read ({type(e).__name__})") from None
    if p.returncode != 0:
        raise IntakeError(f"the clipboard could not be read (powershell.exe exited {p.returncode})")
    return p.stdout


def read_clipboard() -> str:
    """The clipboard's text: UTF-8, \\r removed, trimmed. Under MIN_PAGE_CHARS characters is refused (a snippet, or
    something else on the clipboard)."""
    text = _run_clipboard().decode("utf-8", errors="replace").removeprefix(chr(0xFEFF)).replace("\r", "").strip()
    if len(text) < MIN_PAGE_CHARS:
        raise IntakeError(f"the clipboard holds {len(text)} characters of text (a job description needs at least "
                          f"{MIN_PAGE_CHARS}): copy the whole job ad, then run this again")
    return text


def _ask_field(ask: Callable[[str], str], field: str, prompt: str) -> str:
    """Ask until the answer passes `manual.meta_problem` (and, for a URL, starts with http:// or https://). Three bad or
    blank answers in a row raise IntakeError, so nothing is written for a job with no usable company, title or link."""
    for _ in range(3):
        answer = ask(prompt).strip()
        problem = manual.meta_problem(field, answer) if answer else f"{field} is blank"
        if not problem and field == "url" and urlparse(answer).scheme not in ("http", "https"):
            problem = f"url {answer[:60]!r} must start with http:// or https://"
        if not problem:
            return answer
        print(f"  {problem}; try again.")
    raise IntakeError(f"no usable {field} given; nothing was written")


def add_clipboard(settings, ask: Callable[[str], str] = input) -> tuple[Path, bool, Ad]:
    """Read the clipboard, ask for Company / Title / URL, and put the ad in paths.jds/inbox as <company>-<title>.txt.
    Returns (file, created, ad); an existing file of that name is left exactly as it is (created=False)."""
    text = read_clipboard()
    company = _ask_field(ask, "company", "Company: ")
    title = _ask_field(ask, "title", "Job title: ")
    url = _ask_field(ask, "url", "Application URL: ")
    ad = Ad(company=company, title=title, url=url, location="", text=text, source="clipboard", ad_id="")
    path, created = write_inbox(settings.path("jds") / "inbox", ad)
    return path, created, ad


def add_url(settings, url: str, company: str | None = None, title: str | None = None) -> tuple[Path, bool, Ad]:
    """Fetch `url` and put the ad in paths.jds/inbox. Returns (file, created, ad)."""
    ad = fetch_ad(url, company, title)
    path, created = write_inbox(settings.path("jds") / "inbox", ad)
    return path, created, ad
