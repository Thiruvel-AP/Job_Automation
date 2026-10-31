"""Company size: SME or large, with the evidence. Zero tokens; the only network use is the Companies House public API.

UK Companies Act thresholds (checked 2026-10-31): a company is SMALL when it meets 2 of 3 of <= 50 employees, <= GBP 15m turnover, <= GBP 7.5m balance
sheet, MEDIUM when it meets 2 of 3 of <= 250 / <= GBP 54m / <= GBP 27m (micro, <= 10 employees, is kept as a label). SME = micro, small or medium; large
= above medium. What is shown is `SME`, `Large` or `Unknown`; the sub-label (micro, small, medium) is for display.

Evidence, in this order, cached per company in paths.company_size (data/company_size.jsonl) for 90 days:
  1. the user's overrides, data/company_size_overrides.yaml (`Graphcore: large`): always wins, never written by code;
  2. the ad's own text (already read, nothing fetched): an explicit headcount ("team of 40", "1,200 employees", "500+ people", a range counts its upper
     value) by the 50 / 250 limits; "Series A/B", seed, "founding team" -> SME; "FTSE 100/250", "Fortune 500", NYSE / NASDAQ / LSE-listed, offices in 10+
     countries, "10,000+" -> Large; both kinds at once -> unknown;
  3. Companies House (GET only, COMPANIES_HOUSE_API_KEY from .env, at most one request a second, through the shared polite client): search by name, accept
     only when EXACTLY ONE ACTIVE company has the same normalised name (lower case, "Ltd", "Limited", "PLC", "UK" and punctuation removed), then read
     accounts.last_accounts.type: micro-entity / small / total-exemption-small / total-exemption-full / audited-abridged / unaudited-abridged / dormant
     -> SME, medium -> SME (medium), group -> Large, `full` -> UNKNOWN (small companies may file full accounts too: kept as "full (large?)");
  4. nothing found -> unknown.
Never LinkedIn or a data-broker site. Without a key Companies House is skipped (`careerops doctor` says so) and nothing else changes.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

import yaml

from .http import Blocked, FetchError, Fetcher

SME, LARGE, UNKNOWN = "sme", "large", "unknown"
BUCKETS = (SME, LARGE, UNKNOWN)
TTL_DAYS = 90
KEY_ENV = "COMPANIES_HOUSE_API_KEY"
CH_BASE = "https://api.company-information.service.gov.uk"
MAX_LOOKUPS = 60                       # new Companies House lookups (two requests each) in one command; the rest wait for the next run
MICRO_MAX, SMALL_MAX, MEDIUM_MAX = 10, 50, 250


@dataclass(frozen=True)
class Size:
    size: str = UNKNOWN                # sme | large | unknown
    label: str = "?"                   # micro | small | medium | large | ?
    evidence: str = ""
    checked: str = ""                  # YYYY-MM-DD

    @property
    def tag(self) -> str:
        """`SME·small`, `Large` or `size?`: how a shortlist line shows it."""
        return {SME: f"SME·{self.label}" if self.label not in ("", "?") else "SME", LARGE: "Large"}.get(self.size, "size?")

    @property
    def cell(self) -> str:
        """`SME (small)`, `Large` or `Unknown`: what the tracker Sheet's Size column holds."""
        return {SME: f"SME ({self.label})" if self.label not in ("", "?") else "SME", LARGE: "Large"}.get(self.size, "Unknown")


NONE = Size()


def from_headcount(n: int, evidence: str) -> Size:
    """The bucket of an explicit headcount by the 10 / 50 / 250 limits."""
    if n <= MICRO_MAX:
        return Size(SME, "micro", evidence)
    if n <= SMALL_MAX:
        return Size(SME, "small", evidence)
    if n <= MEDIUM_MAX:
        return Size(SME, "medium", evidence)
    return Size(LARGE, "large", evidence)


# ------------------------------------------------------------------ the ad's text
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)"
_LEAD = r"(?:we are|we're|we’re|we have|company of|team of|startup of|over|more than|around|about|approximately|nearly|almost|grown to|grew to|c\.)"
_RANGE = re.compile(rf"{_NUM}\+?\s*(?:-|–|—|to)\s*{_NUM}\+?\s*(?:full[- ]time\s+)?(?:employees|staff|people|team members|colleagues)", re.I)
_EMPLOYEES = re.compile(rf"{_NUM}\+?\s*(?:full[- ]time\s+)?(?:employees|staff)\b", re.I)
_PEOPLE = re.compile(rf"{_LEAD}\s*(?:a\s+)?(?:team\s+of\s+)?(?:about\s+|around\s+|over\s+|more than\s+)?{_NUM}\+?\s*(?:people|colleagues|team members|co-?workers)", re.I)
_PEOPLE_BARE = re.compile(rf"(?<![\w,.]){_NUM}\+?\s*(?:people|colleagues|team members)\b", re.I)          # "500+ people": only from 50 up ("work with 5 people" is not a headcount)
_TEAM_OF = re.compile(rf"(?P<lead>(?:we are|we're|we’re|our company is|the company is|a startup)\s+(?:a\s+)?(?:small\s+|growing\s+)?)?team\s+of\s+(?:about\s+|around\s+|over\s+|~)?{_NUM}\+?(?P<tail>\s+\w+)?", re.I)
_PERSON = re.compile(rf"{_NUM}[- ]person\s+(?:team|company|startup|business)", re.I)
_ROLE_NOUN = re.compile(r"^\s+(?:data|ml|ai|machine|software|research|product|design|analytics?|engineer|scientist|analyst|developer|designer)", re.I)

_LARGE_MARKS = [
    re.compile(r"FTSE\s?(?:100|250)\b", re.I), re.compile(r"Fortune\s?(?:500|1000)\b", re.I), re.compile(r"\bNYSE\b|\bNASDAQ\b"),
    re.compile(r"\bLSE[- ]listed\b|\blisted on the (?:London Stock Exchange|LSE)\b|\bpublicly[- ](?:listed|traded)\b", re.I),
    re.compile(r"(?:offices?|presence|operat\w+|locations?)\s+(?:in|across)\s+(?:over\s+|more than\s+)?(?P<n>\d+)\+?\s+countries", re.I),
]
_BIG_NUMBER = re.compile(r"(?<![\w,])(\d{2,3},000)\+")
_BIG_NOT = re.compile(r"customers|users|clients|downloads|members|businesses|companies|jobs|developers|subscribers|orders|stores|merchants", re.I)
_SME_MARKS = [
    re.compile(r"\bSeries\s+[AB]\b", re.I), re.compile(r"\bpre-?seed\b", re.I),
    re.compile(r"\bseed[- ](?:stage|funded|funding|round|investment|backed)", re.I),
    re.compile(r"\bfounding\s+(?:team|engineer|member|data)", re.I), re.compile(r"\bearly[- ]stage\s+(?:startup|company)", re.I),
]


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _quote(text: str, m: re.Match, pad: int = 12) -> str:
    return '"' + " ".join(text[max(0, m.start() - 0): min(len(text), m.end() + pad)].split()).strip(" ,.;:") + '"'


def _headcount(text: str) -> Size | None:
    if (m := _RANGE.search(text)):
        return from_headcount(_int(m.group(2)), f"ad: {_quote(text, m, 0)} (upper value of the range)")
    if (m := _EMPLOYEES.search(text)) and 0 < _int(m.group(1)) < 2_000_000:
        return from_headcount(_int(m.group(1)), f"ad: {_quote(text, m, 0)}")
    if (m := _PEOPLE.search(text)) and 0 < _int(m.group(1)) < 2_000_000:
        return from_headcount(_int(m.group(1)), f"ad: {_quote(text, m, 0)}")
    for m in _PEOPLE_BARE.finditer(text):
        if 50 <= _int(m.group(1)) < 2_000_000:
            return from_headcount(_int(m.group(1)), f"ad: {_quote(text, m, 0)}")
    for m in _TEAM_OF.finditer(text):
        n = _int(m.group(2))
        led = bool(m.group("lead"))
        if 0 < n < 2_000_000 and (led or (n >= 20 and not _ROLE_NOUN.match(m.group("tail") or ""))):
            return from_headcount(n, f"ad: {_quote(text, m, 0)}")
    if (m := _PERSON.search(text)) and 0 < _int(m.group(1)) < 2_000_000:
        return from_headcount(_int(m.group(1)), f"ad: {_quote(text, m, 0)}")
    return None


def from_text(text: str | None) -> Size | None:
    """The size an ad's own text states, with the quoted words; None when it says nothing."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    if (hc := _headcount(t)) is not None:
        return hc
    large = next((f"ad: {_quote(t, m, 0)}" for rx in _LARGE_MARKS if (m := rx.search(t)) and not (m.groupdict().get("n") and int(m.group("n")) < 10)), None)
    if large is None and (m := _BIG_NUMBER.search(t)) and not _BIG_NOT.search(t[m.end(): m.end() + 25]):
        large = f"ad: {_quote(t, m, 0)}"
    sme = next((f"ad: {_quote(t, m, 0)}" for rx in _SME_MARKS if (m := rx.search(t))), None)
    if large and sme:
        return Size(UNKNOWN, "?", f"the ad says both {sme[4:]} and {large[4:]}")
    if large:
        return Size(LARGE, "large", large)
    if sme:
        return Size(SME, "small", sme)
    return None


# ------------------------------------------------------------------ names
_DROP = {"ltd", "limited", "plc", "uk"}


def norm(name: str | None) -> str:
    """lower case, "Ltd", "Limited", "PLC", "UK" and punctuation removed: "Graphcore Ltd." and "GRAPHCORE LIMITED" are one name."""
    words = re.sub(r"[^\w\s]", " ", str(name or "").lower().replace("&", " ")).split()
    return " ".join(w for w in words if w not in _DROP)


# ------------------------------------------------------------------ the user's overrides
def parse_value(value: object) -> Size | None:
    v = str(value or "").strip().lower()
    if v in ("large", "big", "enterprise"):
        return Size(LARGE, "large")
    if v in ("sme", "small", "medium", "micro"):
        return Size(SME, v if v != "sme" else "?")
    return None


def load_overrides(path: Path) -> tuple[dict[str, Size], list[str]]:
    """({normalised company: Size}, notes) from the user's overrides file (read only). A line that is not `Company: sme|small|medium|micro|large` is a note."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}, []
    except (OSError, yaml.YAMLError) as e:
        return {}, [f"{path.name} cannot be read ({type(e).__name__}): overrides ignored"]
    if not isinstance(raw, dict):
        return {}, [f"{path.name} must be a list of `Company: sme|large` lines: overrides ignored"]
    out: dict[str, Size] = {}
    notes: list[str] = []
    for company, value in raw.items():
        got = parse_value(value)
        if got is None or not norm(str(company)):
            notes.append(f"{path.name}: {company}: {value!r} is not sme, small, medium, micro or large: ignored")
            continue
        out[norm(str(company))] = replace(got, evidence=f"override: {company}: {value}")
    return out, notes


# ------------------------------------------------------------------ the 90-day cache
class Cache:
    """data/company_size.jsonl: one line per answer, the newest line of a company wins, a line older than 90 days is ignored. Append-only."""

    def __init__(self, path: Path, readonly: bool = False):
        self.path, self.readonly = path, readonly
        self.rows: dict[str, dict] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("norm"):
                    self.rows[e["norm"]] = e
        except OSError:
            pass

    def get(self, key: str, today: date) -> Size | None:
        e = self.rows.get(key)
        if not e:
            return None
        try:
            if (today - date.fromisoformat(str(e.get("checked")))).days > TTL_DAYS:
                return None
        except ValueError:
            return None
        return Size(str(e.get("size") or UNKNOWN), str(e.get("size_label") or "?"), str(e.get("size_evidence") or ""), str(e.get("checked")))

    def put(self, key: str, company: str, s: Size) -> None:
        row = {"norm": key, "company": company, "size": s.size, "size_label": s.label, "size_evidence": s.evidence, "checked": s.checked}
        self.rows[key] = row
        if self.readonly:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                                                   # a cache that cannot be written only costs a lookup next time


# ------------------------------------------------------------------ Companies House
SME_ACCOUNTS = {"micro-entity": "micro", "small": "small", "total-exemption-small": "small", "total-exemption-full": "small",
                "audited-abridged": "small", "unaudited-abridged": "small", "dormant": "micro", "medium": "medium"}


def from_accounts(kind: str | None, company: str) -> Size:
    """The size a `last_accounts.type` says. `full` is UNKNOWN: a small company may file full accounts too."""
    k = str(kind or "").strip().lower()
    if k in SME_ACCOUNTS:
        return Size(SME, SME_ACCOUNTS[k], f"Companies House: {company}: accounts type {k}")
    if k == "group":
        return Size(LARGE, "large", f"Companies House: {company}: accounts type group")
    if k == "full":
        return Size(UNKNOWN, "?", f"Companies House: {company}: accounts type full (large?)")
    return Size(UNKNOWN, "?", f"Companies House: {company}: accounts type {k or 'not given'}")


class CompaniesHouse:
    """The public API, GET only, through the shared Fetcher (one request a second per host, honest User-Agent, a 401 / 403 / 429 stops the host)."""

    def __init__(self, fetcher: Fetcher, key: str):
        self.f, self._key = fetcher, key

    def _get(self, path: str, what: str, **params):
        return self.f.json(f"{CH_BASE}{path}", params=params or None, auth=(self._key, ""), robots=False, what=what)

    def lookup(self, name: str) -> Size | None:
        """Size of the one active company called `name`; UNKNOWN (cacheable) when there is no unique match; None when the answer could not be had."""
        want = norm(name)
        if not want:
            return None
        found = self._get("/search/companies", "Companies House search", q=name, items_per_page=20)
        if not isinstance(found, dict):
            return None
        items = [i for i in found.get("items") or [] if isinstance(i, dict) and str(i.get("company_status") or "").lower() == "active"
                 and norm(i.get("title")) == want and i.get("company_number")]
        if len(items) != 1:
            return Size(UNKNOWN, "?", f"Companies House: {len(items)} active companies are called {name!r}" + (": not guessed" if items else ""))
        prof = self._get(f"/company/{items[0]['company_number']}", "Companies House company")
        if not isinstance(prof, dict):
            return None
        prof = prof.get("company_profile") if isinstance(prof.get("company_profile"), dict) else prof
        kind = ((prof.get("accounts") or {}).get("last_accounts") or {}).get("type")
        return from_accounts(kind, items[0].get("title") or name)


# ------------------------------------------------------------------ the resolver
class Resolver:
    """Size of a company: overrides, then the ad's text, then the cache, then Companies House. One per command; remembers what it asked."""

    def __init__(self, settings, *, fetcher: Fetcher | None = None, key: str | None = None, today: date | None = None,
                 max_lookups: int = MAX_LOOKUPS, ch: CompaniesHouse | None = None, write_cache: bool = True):
        self.today = today or datetime.now(timezone.utc).date()
        self.cache = Cache(settings.path("company_size"), readonly=not write_cache)
        self.overrides, self.notes = load_overrides(settings.path("company_size_overrides"))
        self.key = (os.environ.get(KEY_ENV, "") if key is None else key).strip()
        self._fetcher, self._ch = fetcher, ch
        self.max_lookups, self.lookups = max_lookups, 0
        self.blocked = False
        self.skipped_for_limit = 0
        self.memo: dict[str, Size] = {}
        self.sources: Counter = Counter()

    @property
    def has_key(self) -> bool:
        return bool(self.key) or self._ch is not None

    def _client(self) -> CompaniesHouse:
        if self._ch is None:
            from .robots import RobotsCache
            self._ch = CompaniesHouse(self._fetcher or Fetcher(RobotsCache(None)), self.key)
        return self._ch

    def _done(self, s: Size, how: str) -> Size:
        self.sources[how] += 1
        return s

    def resolve(self, company: str, text: str = "") -> Size:
        key = norm(company)
        if not key:
            return NONE
        if (o := self.overrides.get(key)) is not None:
            return self._done(replace(o, checked=self.today.isoformat()), "override")
        stamp = self.today.isoformat()
        if (t := from_text(text)) is not None and t.size != UNKNOWN:
            t = replace(t, checked=stamp)
            self.cache.put(key, company, t)
            self.memo[key] = t
            return self._done(t, "ad")
        if key in self.memo:
            return self.memo[key]
        if (c := self.cache.get(key, self.today)) is not None:
            self.memo[key] = c
            return self._done(c, "cache")
        if not self.has_key:
            return self._done(Size(UNKNOWN, "?", f"no {KEY_ENV}: Companies House not asked", stamp), "no key")
        if self.blocked:
            return self._done(Size(UNKNOWN, "?", "Companies House refused a request: not asked again this run", stamp), "blocked")
        if self.lookups >= self.max_lookups:
            self.skipped_for_limit += 1
            return self._done(Size(UNKNOWN, "?", f"not looked up yet ({self.max_lookups} Companies House lookups per run)", stamp), "limit")
        self.lookups += 1
        try:
            got = self._client().lookup(company)
        except Blocked:
            self.blocked = True
            self.notes.append("Companies House refused a request (401 / 403 / 429): it is not asked again this run")
            return self._done(Size(UNKNOWN, "?", "Companies House refused a request", stamp), "blocked")
        except FetchError as e:
            self.notes.append(f"Companies House: {e}")
            return self._done(Size(UNKNOWN, "?", "Companies House could not be reached", stamp), "error")
        if got is None:
            return self._done(Size(UNKNOWN, "?", "Companies House gave no answer", stamp), "error")
        got = replace(got, checked=stamp)
        self.cache.put(key, company, got)
        self.memo[key] = got
        return self._done(got, "companies house")


def stored(db, job: Mapping) -> Size:
    """The size recorded for a job: its own `size` columns, else its inbox score's (written by `careerops shortlist`), else unknown. No network."""
    from ..apply import decided

    def get(k: str) -> str:
        try:
            return str(job[k] or "")
        except (KeyError, IndexError):
            return ""
    if get("size") in (SME, LARGE):
        return Size(get("size"), get("size_label") or "?", get("size_evidence"))
    try:
        r = db.conn.execute("SELECT size, size_label, size_evidence FROM triage_scores WHERE key=?",
                            ("|".join(decided.key_of(get("company"), get("title"))),)).fetchone()
    except Exception:  # noqa: BLE001  (an old database without the columns)
        r = None
    if r and r[0] in (SME, LARGE):
        return Size(r[0], r[1] or "?", r[2] or "")
    return NONE


def bucket_of(cell: str) -> str:
    """`sme` | `large` | `unknown` from a tracker Size cell ("SME (small)", "Large", "Unknown")."""
    c = str(cell or "").strip().lower()
    return SME if c.startswith("sme") else LARGE if c.startswith("large") else UNKNOWN


def inbox_companies(settings) -> list[tuple[str, str]]:
    """(company, ad text) of every ad waiting in the inbox: what the size counts of the queue are made from."""
    from ..apply import decided
    from . import manual

    out = []
    for p in decided.txt_files(decided.inbox_dir(settings)):
        try:
            text, _ = manual.read_jd(p)
        except manual.JDError:
            continue
        if (company := manual.header_fields(text).get("company")):
            out.append((company.strip(), text))
    return out


def counts(sizes: Iterable[Size]) -> Counter:
    c = Counter({SME: 0, LARGE: 0, UNKNOWN: 0})
    c.update(s.size for s in sizes)
    return c


def counts_text(c: Mapping[str, int]) -> str:
    """`SME 14 · Large 22 · Unknown 31`."""
    return f"SME {c.get(SME, 0)} · Large {c.get(LARGE, 0)} · Unknown {c.get(UNKNOWN, 0)}"


def fields(s: Size) -> dict[str, str]:
    """The jobs / triage_scores columns a Size sets."""
    return {"size": s.size, "size_label": s.label, "size_evidence": s.evidence}
