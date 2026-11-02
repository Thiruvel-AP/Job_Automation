"""The canonical keys of a job listing (v2.5), stored with every skip / applied decision (apply/decisions.py).

  ats          `ats:<vendor>:<board>:<job_id>` from the job's own ATS address, e.g. `ats:greenhouse:anthropic:5423029008`. The ONLY key that matches a
               decision automatically (as career-ops-hq's listing-fingerprint.mjs: only a resolved ATS-native triple authorises a match). The board
               and the id are kept as they are: no casing, decoding or punctuation change can merge two ids.
  url          the normalised address: https, lower-case host (`eu.` kept), no fragment, no trailing slash, and only a NARROW list of tracking
               parameters removed (utm_*, gclid, fbclid, lever-source, gh_src); every other parameter (gh_jid ...) is kept and the rest sorted.
               Over-stripping would merge two different jobs, which is silent data loss. A URL match is a decision too (the same posting).
  fingerprint  `normalised company family | normalised title | country`. It NEVER skips on its own: two different "AI Engineer" postings at one
               company must not hide each other. A fingerprint-only match labels the job `possible repost of a job you skipped on <date>`
               (auto mode leaves it for you; review shows the label and `s` skips it in one key).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING = re.compile(r"^(?:utm_.*|gclid|fbclid|lever-source|gh_src)$", re.I)

# (vendor, host pattern, path pattern with groups board, id)
_ATS = (
    ("greenhouse", re.compile(r"^(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io$"), re.compile(r"^/([^/]+)/jobs/(\d+)")),
    ("lever", re.compile(r"^jobs(?:\.eu)?\.lever\.co$"), re.compile(r"^/([^/]+)/([0-9a-f-]{8,})")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$"), re.compile(r"^/([^/]+)/([0-9a-f-]{8,})")),
    ("workable", re.compile(r"^apply\.workable\.com$"), re.compile(r"^/([^/]+)/j/([0-9A-F]+)", re.I)),
    ("smartrecruiters", re.compile(r"^jobs\.smartrecruiters\.com$"), re.compile(r"^/([^/]+)/(\d+)")),
)
_WORKDAY = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
_WORKDAY_ID = re.compile(r"_(R?-?\d[\w-]*)$", re.I)
_DB_ID = re.compile(r"^(greenhouse|lever|lever_eu|ashby|workable|smartrecruiters|workday):([^:]+):(.+)$")


@dataclass(frozen=True)
class Keys:
    ats: str = ""
    url: str = ""
    fingerprint: str = ""

    def strong(self) -> list[str]:
        """The keys that match a decision on their own."""
        return [k for k in (self.ats, self.url) if k]

    def all(self) -> list[str]:
        return [k for k in (self.ats, self.url, self.fingerprint) if k]


def ats_key(url: str | None) -> str:
    """`ats:<vendor>:<board>:<job_id>` for a job address on a known ATS, else ""."""
    u = urlparse((url or "").strip())
    host = (u.hostname or "").lower()
    if not host:
        return ""
    for vendor, h, p in _ATS:
        if h.match(host):
            m = p.match(u.path or "")
            if m and m.group(1) != "embed":
                return f"ats:{vendor}:{m.group(1)}:{m.group(2)}"
            q = dict(parse_qsl(u.query))
            if vendor == "greenhouse" and q.get("for") and (q.get("token") or "").isdigit():
                return f"ats:greenhouse:{q['for']}:{q['token']}"
            return ""
    w = _WORKDAY.match(host)
    if w:
        m = _WORKDAY_ID.search((u.path or "").rstrip("/"))
        return f"ats:workday:{w.group(1)}:{m.group(1)}" if m else ""
    return ""


def ats_from_id(job_id: str | None) -> str:
    """The ATS key of a database id such as `greenhouse:anaplan:8654662002`; "" for `manual:...` and the like."""
    m = _DB_ID.match(str(job_id or ""))
    if not m:
        return ""
    vendor = "lever" if m.group(1) == "lever_eu" else m.group(1)
    return f"ats:{vendor}:{m.group(2)}:{m.group(3)}"


def url_key(url: str | None) -> str:
    """The normalised address ("" for something that is not a web address)."""
    u = urlparse((url or "").strip())
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return ""
    host = u.hostname.lower() + (f":{u.port}" if u.port and u.port not in (80, 443) else "")
    path = re.sub(r"/+$", "", u.path or "")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not TRACKING.match(k)))
    return urlunparse(("https", host, path, "", query, ""))


def fingerprint(company: str | None, title: str | None, country: str | None) -> str:
    """`family | title | country` (country as its ISO code when known, else the place text lower-cased); "" without company and title."""
    from ..discovery import candidate as cand
    from ..discovery import countries

    fam, tit = cand.family_key(company or ""), cand.norm_title(title or "")
    if not (fam and tit):
        return ""
    where = countries.code(countries.detect(country)) or countries.code(country) or " ".join(str(country or "").lower().split())
    return f"{fam}|{tit}|{where}"


def keys_for(company: str | None, title: str | None, url: str | None = None, apply_url: str | None = None, location: str | None = None,
             job_id: str | None = None) -> Keys:
    """Every key of a job: the ATS key from its database id, else its address or its apply address; the URL key of its address."""
    ats = ats_from_id(job_id) or ats_key(url) or ats_key(apply_url)
    return Keys(ats, url_key(url) or url_key(apply_url), fingerprint(company, title, location))


def keys_of_row(row) -> Keys:
    def get(k: str):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    return keys_for(get("company"), get("title"), get("url"), get("apply_url"), get("location"), get("id"))


def parse_query(text: str) -> Keys:
    """What `careerops why <id|url|key>` was given, as keys: an `ats:` key, a URL, or nothing (a job id is looked up by the caller)."""
    t = (text or "").strip()
    if t.startswith("ats:"):
        return Keys(ats=t)
    if t.startswith(("http://", "https://")):
        return Keys(ats_key(t), url_key(t))
    return Keys(ats=ats_from_id(t))
