"""Job Bank Canada: the open-data monthly CSV of every posting (`careerops discover --jobbank`, source `jobbank-ca`).

Allowed access only: the dataset is published by Employment and Social Development Canada under the Open Government Licence - Canada
(https://open.canada.ca/data/en/dataset/ea639e28-c0fc-48bf-b5dd-b8899bd43072). The newest English file is found through the portal's
CKAN API (`package_show`, one request) and downloaded once (robots.txt is read first; open.canada.ca asks for a 20 s crawl delay,
which http.Fetcher keeps).

What the file holds (read from the September 2026 file, 2026-10-13): UTF-16, tab-separated, ~46 MB; one row per posting with its
Job Location Snapshot ID, Job Title, First Posting Date, Province/Territory, City, Employment Type, Salary Minimum / Maximum / Per ...
It has NO employer name, NO link and NO ad text. So a row can never become an inbox ad (`live` needs a company and a full JD): the rows
whose title is in a target role family and that were first posted recently are only LISTED, in paths.discover/listed_only.csv, with
Country Canada. Search the title on jobbank.gc.ca to see the ad.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from .http import FetchError, Fetcher

CKAN = "https://open.canada.ca/data/api/3/action/package_show"
CHUNK = 1 << 16
# our name -> header names the file has used (lower case)
COLUMNS = {
    "ref": ("wic job location snapshot id", "job location snapshot id", "job posting id", "job id"),
    "title": ("job title", "original job title"),
    "posted": ("first posting date", "posting date"),
    "province": ("province/territory", "province"),
    "city": ("city",),
    "salary_min": ("salary minimum",),
    "salary_max": ("salary maximum",),
    "salary_per": ("salary per",),
    "term": ("employment type", "employment term"),
}


class JobBankError(RuntimeError):
    """The dataset could not be found or read. One line, fit to print."""


def latest_csv(fetcher: Fetcher, dataset: str, language: str = "en") -> dict:
    """{name, url, created} of the newest CSV resource in `language` of the CKAN dataset."""
    data = fetcher.json(CKAN, params={"id": dataset}, what="Job Bank open-data catalogue")
    resources = ((data or {}).get("result") or {}).get("resources") if isinstance(data, dict) else None
    if not resources:
        raise JobBankError("the open.canada.ca catalogue has no resources for the Job Bank dataset")
    csvs = [r for r in resources if str(r.get("format", "")).upper() == "CSV" and r.get("url")
            and language in [str(x).lower() for x in (r.get("language") or [])]]
    if not csvs:
        raise JobBankError(f"the Job Bank dataset lists no {language} CSV")
    best = max(csvs, key=lambda r: str(r.get("created") or r.get("last_modified") or ""))
    return {"name": str(best.get("name") or ""), "url": str(best["url"]), "created": str(best.get("created") or "")}


def _decoder(first: bytes) -> codecs.IncrementalDecoder:
    if first.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return codecs.getincrementaldecoder("utf-16")(errors="replace")
    if first.startswith(codecs.BOM_UTF8):
        return codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


def _lines(chunks: Iterator[bytes], max_bytes: int) -> Iterator[str]:
    """Text lines (with their line ends) of a byte stream in UTF-16 or UTF-8; stops quietly after max_bytes."""
    dec, buf, seen = None, "", 0
    for chunk in chunks:
        if not chunk:
            continue
        if dec is None:
            dec = _decoder(chunk)
        seen += len(chunk)
        buf += dec.decode(chunk)
        *done, buf = buf.split("\n")
        for line in done:
            yield line + "\n"
        if seen > max_bytes:
            raise JobBankError(f"the file is larger than {max_bytes // 1_000_000} MB (discover.jobbank.max_mb); stopped reading")
    if dec is not None:
        buf += dec.decode(b"", final=True)
    if buf:
        yield buf


def rows(response, max_bytes: int) -> Iterator[dict[str, str]]:
    """One dict per CSV row with the keys of COLUMNS (empty string for a column the file lacks, and for 'NA')."""
    chunks = response.iter_content(CHUNK) if hasattr(response, "iter_content") else iter([response.content])
    lines = _lines(chunks, max_bytes)
    head = next(lines, "")
    delim = "\t" if head.count("\t") >= head.count(",") else ","
    names = [h.strip().lower() for h in next(csv.reader([head], delimiter=delim), [])]
    index = {key: next((names.index(n) for n in options if n in names), None) for key, options in COLUMNS.items()}
    if index["title"] is None:
        raise JobBankError(f"the file has no Job Title column (found: {', '.join(names[:8])} ...); the format changed")
    for rec in csv.reader(lines, delimiter=delim):
        if not rec:
            continue
        out = {}
        for key, i in index.items():
            v = rec[i].strip() if i is not None and i < len(rec) else ""
            out[key] = "" if v.upper() in ("NA", "N/A", "*NO DATA") else v
        yield out


def posted_on(value: str) -> datetime | None:
    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", value or "")
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc) if m else None
    except ValueError:
        return None


def salary(r: dict) -> str:
    lo, hi, per = r.get("salary_min", ""), r.get("salary_max", ""), r.get("salary_per", "")
    if not lo and not hi:
        return ""
    amount = lo if lo == hi or not hi else (hi if not lo else f"{lo} to {hi}")
    return f"${amount} per {per.lower()}" if per else f"${amount}"


def listed(fetcher: Fetcher, settings, dataset: str, *, want: Callable[[str], bool], today: datetime | None = None,
           say: Callable[[str], None] = lambda s: None) -> Iterator[tuple[str, dict | str]]:
    """Yield ('note', text) lines and ('row', listing) for each posting in a target role family first posted within
    discover.jobbank.max_days_old days. `want(title)` says whether the title is in a target role family."""
    cfg = (settings["discover"].get("jobbank") or {})
    max_bytes = int(float(cfg.get("max_mb", 150)) * 1_000_000)
    days = float(cfg.get("max_days_old", 14))
    today = today or datetime.now(timezone.utc)
    res = latest_csv(fetcher, dataset, str(cfg.get("language", "en")))
    yield "note", f"jobbank: reading {res['name'] or res['url'].rsplit('/', 1)[-1]}"
    say(f"jobbank: downloading {res['url'].rsplit('/', 1)[-1]} (the portal asks for a 20 s crawl delay between requests) ...")
    r = fetcher.request(res["url"], what="Job Bank open-data file", stream=True)
    try:
        if r.status_code != 200:
            raise FetchError(f"Job Bank open-data file: HTTP {r.status_code}")
        total = 0
        for rec in rows(r, max_bytes):
            total += 1
            posted = posted_on(rec["posted"])
            if not want(rec["title"]) or (posted is not None and today - posted > timedelta(days=days)):
                continue
            place = ", ".join(x for x in (rec["city"], rec["province"]) if x)
            ref = rec["ref"] or hashlib.sha1(f"{rec['title']}|{place}|{rec['posted']}".encode()).hexdigest()[:12]      # no id column: a stable stand-in
            yield "row", {"ref": ref, "title": rec["title"], "location": place, "posted": posted.date().isoformat() if posted else "",
                          "salary": salary(rec), "term": rec["term"]}
        yield "note", f"jobbank: {total:,} postings read"
    finally:
        close = getattr(r, "close", None)
        if close:
            close()
