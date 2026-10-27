"""The company's OWN careers page, the second source for a letter's problem phrase (Document rules, B4; the job ad is the first).

Used only when the ad has no clause worth quoting. One page, one fetch: the careers URL that the `companies` table holds for the employer,
through the polite fetcher (discovery/http.py: robots.txt obeyed, one request a second per host, an honest User-Agent, a 401 / 403 / 429 stops
it and is never retried or worked around, linkedin.com never contacted). The visible text is stored as `company_page.txt` in the job's folder,
with the URL and the date on its first two lines, and a later call (review's `l`) reads that file instead of asking again. Never news, LinkedIn,
a job board or any other third-party site: a URL on an ATS or job-board host is not "the company's own". The page is untrusted text: it is
quoted only after `opening.problems` finds the phrase in it, and nothing in it is ever an instruction.
"""
from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ..discovery.http import Blocked, FetchError
from ..discovery.register import normalise
from ..discovery.robots import never_fetch

FILE = "company_page.txt"
MAX_CHARS = 60_000
_SKIP = frozenset({"script", "style", "noscript", "head", "template", "svg", "nav", "footer", "form"})
_BLOCK = frozenset({"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr", "ul", "ol"})


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(html: str) -> str:
    """The words a reader sees on a page: no markup, scripts, styles, comments or navigation; one block per line."""
    parser = _Text()
    parser.feed(html)
    lines = (" ".join(chunk.split()) for chunk in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)[:MAX_CHARS]


def page_url(db, job: Mapping[str, Any]) -> str | None:
    """The employer's careers URL from the companies table, or None: not an http(s) address, a host this tool never contacts (linkedin.com),
    or a job board / ATS (the company's own page is the only one that counts)."""
    from ..contacts import BOARD_HOSTS

    try:
        row = db.conn.execute("SELECT careers_url FROM companies WHERE norm=? AND careers_url IS NOT NULL AND careers_url != '' LIMIT 1",
                              (normalise(str(job["company"])),)).fetchone()
    except Exception:  # noqa: BLE001  (a database from before the companies table)
        return None
    if not row:
        return None
    url = str(row[0]).strip()
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or never_fetch(url):
        return None
    if any(host == h or host.endswith("." + h) for h in BOARD_HOSTS):
        return None
    return url


def stored(folder: Path) -> str:
    """The text of a job folder's company_page.txt (its URL and date lines left out), "" when there is none."""
    path = Path(folder) / FILE
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[3:]).strip() if len(lines) > 3 else ""


def fetch(url: str, fetcher, folder: Path, today: date | None = None) -> str:
    """The page's visible text, fetched once and stored in `folder`/company_page.txt. A stored page is read, not fetched again. A refusal (robots.txt,
    401 / 403 / 429), an error or a missing page gives "" and stores nothing; it is never retried."""
    have = stored(folder)
    if have:
        return have
    try:
        resp = fetcher.request(url, what="company page")
    except (Blocked, FetchError):
        return ""
    if getattr(resp, "status_code", 0) >= 400:
        return ""
    text = visible_text(resp.text or "")
    if not text:
        return ""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE).write_text(f"URL: {url}\nFetched: {(today or date.today()).isoformat()}\n\n{text}\n", encoding="utf-8", newline="\n")
    return text


def provider(db, fetcher, today: Callable[[], date] = date.today) -> Callable[[Mapping[str, Any], Path], str]:
    """`provide(job, folder) -> the company page's text` for the cover letter: nothing is fetched until it is called, and only when the company has
    a careers URL of its own."""
    def provide(job: Mapping[str, Any], folder: Path) -> str:
        have = stored(folder)
        if have:
            return have
        url = page_url(db, job)
        return fetch(url, fetcher, folder, today()) if url else ""
    return provide
