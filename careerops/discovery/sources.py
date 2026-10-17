"""Job sources. Every fetcher yields dicts with the keys DB.upsert_job expects.

Greenhouse / Lever / Ashby: public, unauthenticated job-board JSON APIs (the same ones their
hosted careers pages use). Adzuna: keyword search across the UK market (free key; its
descriptions are truncated snippets, so those jobs need the full JD fetched or pasted).
"""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterator

import requests

from . import freshness

UA = {"User-Agent": "career-ops/2.0 (personal job search)"}


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)            # Greenhouse double-encodes its HTML
    p = _Text()
    p.feed(s)
    t = "".join(p.out)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def _entry(e: Any) -> tuple[str, str]:
    """Config entries are either 'slug' or {slug: ..., name: ...}."""
    if isinstance(e, dict):
        return e["slug"], e.get("name") or e["slug"]
    return e, e.replace("-", " ").title()


def _get(url: str, **kw) -> Any:
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def greenhouse(entry: Any) -> Iterator[dict]:
    slug, name = _entry(entry)
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    for j in data.get("jobs", []):
        yield {
            "id": f"greenhouse:{slug}:{j['id']}",
            "source": "greenhouse",
            "company": j.get("company_name") or name,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"),
            "apply_url": j.get("absolute_url"),
            "description": html_to_text(j.get("content")),
            "posted_at": j.get("first_published"),            # never updated_at: it resets when a job is reposted
            "posted_at_source": freshness.label("greenhouse", "first_published") if j.get("first_published") else None,
        }


def lever(entry: Any, eu: bool = False) -> Iterator[dict]:
    slug, name = _entry(entry)
    host = "api.eu.lever.co" if eu else "api.lever.co"
    data = _get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
    for j in data:
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists", []):
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        created = j.get("createdAt")
        yield {
            "id": f"lever:{slug}:{j['id']}",
            "source": "lever",
            "company": name,
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl"),
            "apply_url": j.get("applyUrl") or j.get("hostedUrl"),
            "description": "\n\n".join(p for p in parts if p).strip(),
            "posted_at": freshness.from_millis(created),
            "posted_at_source": freshness.label("lever", "createdAt") if freshness.from_millis(created) else None,
        }


def ashby(entry: Any) -> Iterator[dict]:
    slug, name = _entry(entry)
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        yield {
            "id": f"ashby:{slug}:{j['id']}",
            "source": "ashby",
            "company": name,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or j.get("jobUrl"),
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            "posted_at": j.get("publishedAt"),
            "posted_at_source": freshness.label("ashby", "publishedAt") if j.get("publishedAt") else None,
        }


def adzuna(query: str, pages: int = 2, max_days_old: int = 7) -> Iterator[dict]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
    for page in range(1, pages + 1):
        data = _get(
            f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}",
            params={"app_id": app_id, "app_key": app_key, "what": query, "results_per_page": 50,
                    "max_days_old": max_days_old, "content-type": "application/json"},
        )
        for j in data.get("results", []):
            yield {
                "id": f"adzuna:{j['id']}",
                "source": "adzuna",
                "company": (j.get("company") or {}).get("display_name", ""),
                "title": html_to_text(j.get("title", "")),
                "location": (j.get("location") or {}).get("display_name", ""),
                "url": j.get("redirect_url"),
                "apply_url": j.get("redirect_url"),
                "description": html_to_text(j.get("description")),   # truncated snippet
                "posted_at": j.get("created"),
                "posted_at_source": freshness.label("adzuna", "created") if j.get("created") else None,
            }
        time.sleep(3)   # stay well inside 25 calls/minute


def fetch_all(settings) -> Iterator[tuple[str, dict | Exception]]:
    """Yield (source_label, job) — or (source_label, exception) so one dead board doesn't stop the run."""
    src = settings["sources"]
    plan: list[tuple[str, Any]] = []
    plan += [(f"greenhouse:{_entry(e)[0]}", lambda e=e: greenhouse(e)) for e in src.get("greenhouse") or []]
    plan += [(f"lever:{_entry(e)[0]}", lambda e=e: lever(e)) for e in src.get("lever") or []]
    plan += [(f"lever-eu:{_entry(e)[0]}", lambda e=e: lever(e, eu=True)) for e in src.get("lever_eu") or []]
    plan += [(f"ashby:{_entry(e)[0]}", lambda e=e: ashby(e)) for e in src.get("ashby") or []]
    az = src.get("adzuna") or {}
    if az.get("enabled"):
        plan += [(f"adzuna:{q}", lambda q=q: adzuna(q, az.get("pages_per_query", 2), az.get("max_days_old", 7)))
                 for q in az.get("queries", [])]
    for label, fn in plan:
        try:
            for job in fn():
                yield label, job
        except Exception as exc:  # noqa: BLE001 — report and continue
            yield label, exc
        time.sleep(1)   # Lever's robots.txt asks for a 1 s crawl delay; apply it everywhere
