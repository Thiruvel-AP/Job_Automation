"""ATS resolution (M3) and the boards `careerops discover --ats` reads. Spec: docs/tasks/M3-ats-resolution.md, docs/IMPLEMENTATION.md §4.

For each sponsor-register company: guess board slugs from its name, probe the public Greenhouse / Lever / Ashby APIs, verify the
hit really is this company, and keep the result in the `companies` table (never probing the same company twice inside
`companies.reprobe_after_days`). Precision over recall: a wrong board means applying to the wrong company.

The three APIs are the ones their own documentation publishes for programs, no key needed (checked 2026-10-10):
  Greenhouse  GET boards-api.greenhouse.io/v1/boards/{token}[/jobs?content=true]     (docs.greenhouse.io/job-board.html)
  Lever       GET api.lever.co/v0/postings/{site}?mode=json  (api.eu.lever.co for EU) (github.com/lever/postings-api)
  Ashby       GET api.ashbyhq.com/posting-api/job-board/{board}                        (developers.ashbyhq.com)
Workable's account API (workable.com/spi/v3) needs the owner's token and is not used; its public widget (apply.workable.com/api/v1/widget/accounts/<slug>)
is, with the other providers added in v2.6 (discovery/more_ats.py: Workable, SmartRecruiters, Teamtailor, Recruitee, Personio, Pinpoint, Eploy).
Everything goes through discovery/http.py: robots.txt, one request a second per host, a 401 / 403 / 429 stops the host.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..db import DB, now
from .http import Blocked, FetchError, Fetcher
from .register import normalise
from .sources import html_to_text

# Careers-page signatures → (ats, slug group). Greenhouse/Lever/Ashby are fetchable; the rest are recorded only.
SIGNATURES: dict[str, str] = {
    "greenhouse": r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)",
    "lever": r"jobs(?:\.eu)?\.lever\.co/([\w-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([\w-]+)",
    "workday": r"([\w-]+)\.wd\d+\.myworkdayjobs\.com",
    "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)",
    "workable": r"apply\.workable\.com/([\w-]+)",
    "teamtailor": r"([\w-]+)\.teamtailor\.com",
    "recruitee": r"([\w-]+)\.recruitee\.com",
    "pinpoint": r"([\w-]+)\.pinpointhq\.com",
    "bamboohr": r"([\w-]+)\.bamboohr\.com",
}
FETCHABLE = {"greenhouse", "lever", "lever_eu", "ashby"}
STOP = {"and", "of", "the", "group", "holdings", "services", "solutions", "technologies", "technology", "uk", "international",
        "global"}
UK_COUNTRIES = {"united kingdom", "gb", "uk", "great britain", "gbr"}
SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


@dataclass
class Hit:
    ats: str
    slug: str
    board_name: str | None = None
    uk_jobs: int = 0
    total_jobs: int = 0


@dataclass
class ResolveStats:
    probed: int = 0
    verified: int = 0
    unverified: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)
    backed_off: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"probed {self.probed} · verified {self.verified} · unverified {self.unverified} · not found {self.not_found} · "
                f"errors {len(self.errors)}" + (f" (backed-off hosts: {', '.join(self.backed_off)})" if self.backed_off else ""))


@dataclass
class Posting:
    """One open job on a company's board, in the shape discover works with."""
    ext_id: str
    company: str
    title: str
    location: str
    url: str
    description: str
    posted_at: str | None = None        # the source's own posting time (freshness.py): Greenhouse first_published, Lever createdAt, Ashby publishedAt
    country: str = ""
    listed: bool = True


@dataclass
class Board:
    ats: str                    # greenhouse | lever | lever_eu | ashby
    slug: str
    name: str                   # the company name to use (the register name when it came from the register)
    origin: str = "config"      # config | resolved


# ------------------------------------------------------------------ slugs and verification
def slug_candidates(register_name: str, ch_name: str | None = None) -> list[str]:
    """≤4 deduped guesses, e.g. 'Monzo Bank Ltd' → ['monzobank', 'monzo-bank', 'monzo'] (joined, hyphenated, the first word when it
    is at least 5 letters and the name has more words, the first two words when it has more than two)."""
    out: list[str] = []
    for name in (register_name, ch_name):
        if not name:
            continue
        toks = normalise(re.split(r"\s+t/a\s+|\s+trading as\s+", name, flags=re.I)[0]).split()
        if not toks:
            continue
        cands = ["".join(toks), "-".join(toks)]
        real = [t for t in toks if t not in STOP]
        if real and len(real[0]) >= 5 and len(toks) > 1:
            cands.append(real[0])
        if len(real) > 2:
            cands.append("".join(real[:2]))
        out += [c for c in cands if SLUG_OK.match(c)]
    return list(dict.fromkeys(out))[:4]


TOKEN_STRIP = {"ltd", "limited", "plc", "group", "uk", "holdings", "technologies"}
TOKEN_FIRST_WORD_MIN = 3            # a one- or two-letter first word ("ai", "bt") is a board of some other company far too often


LEGAL_WORDS = {"ltd", "limited", "plc", "llp", "inc", "incorporated", "corp", "corporation", "uk"}


def _name_words(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", re.split(r"\s+t/a\s+|\s+trading as\s+", name, flags=re.I)[0])
    text = text.encode("ascii", "ignore").decode().lower().replace("'", "")
    return re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", " ", text))


def full_name_tokens(name: str) -> set[str]:
    """The slugs that spell the WHOLE name: its words joined and hyphenated, with only a legal ending (Ltd, Limited, plc, UK) and a
    leading 'The' left out. 'Octagon Group' -> {octagongroup, octagon-group}: the slug `octagon` drops a word, so it is a shortened slug
    and the board it finds must prove it is this company (boards.verify_shortened)."""
    words = _name_words(name)
    kept = [w for w in words if w not in LEGAL_WORDS] or words
    if len(kept) > 1 and kept[0] == "the":
        kept = kept[1:]
    return {t for t in ("".join(kept), "-".join(kept)) if t and SLUG_OK.match(t)}


def board_tokens(name: str, hint: str | None = None) -> list[str]:
    """≤4 board-token guesses for a company or folder name, best first: 'Monzo Bank Ltd' → ['monzobank', 'monzo-bank', 'monzo'].
    Lower-case, accents and punctuation removed, Ltd / Limited / plc / Group / UK / Holdings / Technologies dropped; then the words
    joined, hyphenated, the first word (3+ letters, name of more than one word) and the first two words (name of more than two).
    `hint` (a slug already verified for this company) goes first."""
    words = _name_words(name)
    kept = [w for w in words if w not in TOKEN_STRIP] or words
    if len(kept) > 1 and kept[0] == "the":
        kept = kept[1:]
    cands = [hint] if hint else []
    if kept:
        cands += ["".join(kept), "-".join(kept)]
        if len(kept) > 1 and len(kept[0]) >= TOKEN_FIRST_WORD_MIN:
            cands.append(kept[0])
        if len(kept) > 2:
            cands.append("".join(kept[:2]))
    return list(dict.fromkeys(c for c in cands if c and SLUG_OK.match(c)))[:4]


def verify_rule(hit: Hit, company_norm: str, slug_is_full_name: bool) -> tuple[bool, str]:
    """(verified, rule), first match wins: name_exact | name_prefix_uk | full_slug_uk | name_mismatch (rejected) | weak."""
    if hit.board_name:
        bn = normalise(hit.board_name)
        if bn == company_norm:
            return True, "name_exact"
        bt, ct = bn.split(), company_norm.split()
        if bt and ct and (ct[: len(bt)] == bt or bt[: len(ct)] == ct) and hit.uk_jobs >= 1:
            return True, "name_prefix_uk"
        return False, "name_mismatch"
    if slug_is_full_name and hit.uk_jobs >= 1:
        return True, "full_slug_uk"
    return False, "weak"


def verify(hit: Hit, company_norm: str, slug_is_full_name: bool) -> bool:
    """Accept if the board's name equals the company's (or extends it) and it has a UK posting, or a full-name slug has one."""
    return verify_rule(hit, company_norm, slug_is_full_name)[0]


# ------------------------------------------------------------------ the APIs
GH = "https://boards-api.greenhouse.io/v1/boards"
LEVER = {"lever": "https://api.lever.co/v0/postings", "lever_eu": "https://api.eu.lever.co/v0/postings"}
ASHBY = "https://api.ashbyhq.com/posting-api/job-board"


def _uk_text(loc: str, settings=None) -> bool:
    text = str(loc or "").lower()
    if not text.strip():
        return False
    patterns = (settings["gates"]["locations_ok"] if settings is not None else
                [r"united kingdom", r"\buk\b", r"england", r"london", r"scotland", r"wales", r"manchester", r"bristol"])
    return any(p != "remote" and re.search(p, text) for p in patterns)


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


PROVIDERS = ("greenhouse", "lever", "lever_eu", "ashby")


def board_request(ats: str, slug: str) -> tuple[str, dict | None, str]:
    """(url, params, what) of the call that lists a board's postings with their text."""
    from . import more_ats                                       # imported here: more_ats.py imports Board and Posting from this module
    if ats in more_ats.SINGLE:
        return more_ats.endpoint(ats, slug)
    if ats == "greenhouse":
        return f"{GH}/{slug}/jobs", {"content": "true"}, f"Greenhouse board {slug}"
    if ats in LEVER:
        return f"{LEVER[ats]}/{slug}", {"mode": "json"}, f"Lever site {slug}"
    if ats == "ashby":
        return f"{ASHBY}/{slug}", None, f"Ashby board {slug}"
    raise ValueError(f"unknown ATS {ats!r}")


def is_board(ats: str, data) -> bool:
    """Is this 200 reply a real job board? Greenhouse and Ashby answer {"jobs": [...]} (an empty list is a board with no open roles);
    Lever answers a bare list, and an empty one is not told apart from an unknown site, so it needs at least one posting."""
    if ats in LEVER:
        return isinstance(data, list) and len(data) > 0
    return isinstance(data, dict) and isinstance(data.get("jobs"), list) and "error" not in data


def parse_postings(ats: str, data, name: str) -> list[Posting]:
    """The postings in a board reply (see board_request), under the company name `name`."""
    out: list[Posting] = []
    if ats == "greenhouse":
        for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
            out.append(Posting(str(j.get("id")), name, str(j.get("title") or ""), str((j.get("location") or {}).get("name") or ""),
                               str(j.get("absolute_url") or ""), html_to_text(j.get("content")),
                               j.get("first_published")))             # never updated_at: it resets when a job is reposted
    elif ats in LEVER:
        for j in data if isinstance(data, list) else []:
            parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
            parts += [f"{x.get('text', '')}\n{html_to_text(x.get('content'))}" for x in j.get("lists") or []]
            parts.append(j.get("additionalPlain") or "")
            cats = j.get("categories") or {}
            out.append(Posting(str(j.get("id")), name, str(j.get("text") or ""), str(cats.get("location") or ""),
                               str(j.get("hostedUrl") or ""), "\n\n".join(p for p in parts if p).strip(),
                               _ms_to_iso(j.get("createdAt")), str(j.get("country") or "")))
    elif ats == "ashby":
        for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
            loc = j.get("location") or ""
            addr = (j.get("address") or {}).get("postalAddress") or {}
            out.append(Posting(str(j.get("id") or j.get("jobUrl") or ""), name, str(j.get("title") or ""),
                               str(loc.get("name") if isinstance(loc, dict) else loc), str(j.get("jobUrl") or ""),
                               j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), j.get("publishedAt"),
                               str(addr.get("addressCountry") or ""), listed=j.get("isListed") is not False))
    return out


def board_postings(board: Board, fetcher: Fetcher, name_hint: str | None = None, keep=None) -> list[Posting]:
    """Every open posting on the board, with its description (one request: all three APIs return the text in the list).
    [] for a board that does not exist. Raises FetchError / Blocked on a failure. The v2.6 providers (more_ats.py) whose list has no text
    (SmartRecruiters, Eploy) read in full the postings `keep` accepts."""
    from . import more_ats
    if board.ats in more_ats.SINGLE:
        return more_ats.board_postings(board, fetcher, keep)
    if board.ats == "workday":                                  # its own request (a POST) and its own shape: discovery/workday.py
        from . import workday
        return workday.board_postings(board, fetcher)
    url, params, what = board_request(board.ats, board.slug)
    return parse_postings(board.ats, fetcher.json(url, params=params, what=what), board.name)


def probe(slug: str, *, fetcher: Fetcher | None = None, settings=None) -> list[Hit]:
    """Query Greenhouse (board + jobs), Lever (global + EU) and Ashby for one slug; a 404 or an error body is 'not there'.
    A host that blocks stops being asked (http.Fetcher); the hits of the others are still returned."""
    f = fetcher or Fetcher()
    hits: list[Hit] = []

    def attempt(fn):
        try:
            return fn()
        except Blocked:
            return None
        except FetchError:
            return None

    board = attempt(lambda: f.json(f"{GH}/{slug}", what=f"Greenhouse board {slug}"))
    if isinstance(board, dict) and board.get("name") and "error" not in board:
        jobs = attempt(lambda: f.json(f"{GH}/{slug}/jobs", what=f"Greenhouse jobs {slug}"))
        listing = jobs.get("jobs", []) if isinstance(jobs, dict) else []
        hits.append(Hit("greenhouse", slug, str(board["name"]), sum(_uk_text((j.get("location") or {}).get("name"), settings) for j in listing),
                        len(listing)))
    for ats, base in LEVER.items():
        data = attempt(lambda base=base: f.json(f"{base}/{slug}", params={"mode": "json"}, what=f"Lever {slug}"))
        if isinstance(data, list) and data:
            hits.append(Hit(ats, slug, None, sum(_uk_text((j.get("categories") or {}).get("location"), settings)
                                                 or str(j.get("country") or "").upper() == "GB" for j in data), len(data)))
    data = attempt(lambda: f.json(f"{ASHBY}/{slug}", what=f"Ashby board {slug}"))
    if isinstance(data, dict) and isinstance(data.get("jobs"), list) and data["jobs"] and "error" not in data:
        def uk(j):
            addr = (j.get("address") or {}).get("postalAddress") or {}
            loc = j.get("location")
            return (str(addr.get("addressCountry") or "").lower() in UK_COUNTRIES
                    or _uk_text(loc.get("name") if isinstance(loc, dict) else loc, settings))
        hits.append(Hit("ashby", slug, None, sum(uk(j) for j in data["jobs"]), len(data["jobs"])))
    return hits


# ------------------------------------------------------------------ resolve: the companies table
def _in_scope_sql(db: DB, settings) -> tuple[str, list]:
    """The WHERE for the companies worth probing. With the Companies House join (M2) run, only rows with a sector and an active
    company; without it, the A-rated register rows whose name does not look like a public body."""
    c = settings["companies"]
    where = ["removed_on IS NULL", "COALESCE(public_sector, 0) = 0", "COALESCE(probe_status, '') != 'manual'"]
    args: list = []
    ratings = c.get("sponsor_ratings") or ["A"]
    where.append(f"rating IN ({','.join('?' * len(ratings))})")
    args += ratings
    if db.conn.execute("SELECT 1 FROM companies WHERE sector IS NOT NULL LIMIT 1").fetchone():
        where += ["sector IS NOT NULL", "LOWER(COALESCE(ch_status, '')) = 'active'"]
    return " AND ".join(where), args


def resolve(db: DB, settings, limit: int = 300, only_new: bool = False, *, fetcher: Fetcher | None = None) -> ResolveStats:
    """Probe unresolved in-scope companies, newest sponsors first, at ≤1 request/second per host; stop at the first verified hit
    for a company. Each company is written in its own transaction (an interrupted run keeps its progress); a company marked
    `manual` is never touched; a `not_found` one is probed again after companies.reprobe_after_days."""
    f = fetcher or Fetcher()
    c = settings["companies"]
    stats = ResolveStats()
    where, args = _in_scope_sql(db, settings)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=c.get("reprobe_after_days", 30))).isoformat(timespec="seconds")
    again = "" if only_new else " OR (probe_status = 'not_found' AND last_probed < ?) OR probe_status = 'error'"
    q = (f"SELECT norm, town_norm, register_name FROM companies WHERE {where} AND (probe_status IS NULL{again}) "
         f"ORDER BY first_seen DESC, register_name LIMIT ?")
    rows = db.conn.execute(q, [*args, *([] if only_new else [cutoff]), int(limit)]).fetchall()
    pattern = re.compile(c.get("public_sector_pattern") or r"$^", re.I)
    for row in rows:
        name = row["register_name"] or ""
        if pattern.search(name):
            continue
        stats.probed += 1
        norm = normalise(name)
        tried, best, verified, rule, error = [], None, None, "", None
        for slug in slug_candidates(name):
            full = slug in ("".join(norm.split()), "-".join(norm.split()))
            try:
                hits = probe(slug, fetcher=f, settings=settings)
            except Exception as e:  # noqa: BLE001 — one company must not stop the run
                error = str(e)[:200]
                break
            tried.append(slug)
            for h in hits:
                ok, r = verify_rule(h, norm, full)
                if ok:
                    verified, rule = h, r
                    break
                if r == "weak" and best is None:
                    best = (h, r)
            if verified:
                break
        stats.backed_off = sorted(f.backed_off)
        hit, status = (verified, "verified") if verified else ((best[0], "unverified") if best else (None, "error" if error else "not_found"))
        evidence = {"rule": rule or (best[1] if best else ""), "board_name": hit.board_name if hit else None,
                    "uk_jobs": hit.uk_jobs if hit else 0, "total_jobs": hit.total_jobs if hit else 0, "tried": tried}
        if error:
            stats.errors.append(f"{name}: {error}")
        stats.verified += status == "verified"
        stats.unverified += status == "unverified"
        stats.not_found += status == "not_found"
        with db.conn:                                              # one transaction per company
            db.conn.execute(
                "UPDATE companies SET ats=?, ats_slug=?, ats_verified=?, probe_status=?, last_probed=?, ats_evidence=? "
                "WHERE norm=? AND town_norm=?",
                (hit.ats if hit else None, hit.slug if hit else None, 1 if status == "verified" else 0, status, now(),
                 json.dumps(evidence), row["norm"], row["town_norm"]))
    return stats


def detect(url: str, *, fetcher: Fetcher | None = None) -> tuple[str, str] | None:
    """Fetch a careers page once and return (ats, slug) from the first matching signature (the page, then its final URL)."""
    f = fetcher or Fetcher()
    r = f.request(url, what=url)
    for text in (r.text or "", getattr(r, "url", "") or url):
        for ats, rx in SIGNATURES.items():
            m = re.search(rx, text)
            if m:
                return ats, m.group(1)
    return None


# ------------------------------------------------------------------ the boards discover reads
def configured_boards(settings) -> list[Board]:
    """The boards in `sources:` of settings.yaml (the companies you list yourself), under the name given there or the slug's."""
    out: list[Board] = []
    src = settings["sources"]
    for key, ats in (("greenhouse", "greenhouse"), ("lever", "lever"), ("lever_eu", "lever_eu"), ("ashby", "ashby")):
        for e in src.get(key) or []:
            slug, name = (e["slug"], e.get("name") or e["slug"]) if isinstance(e, dict) else (e, e.replace("-", " ").title())
            out.append(Board(ats, str(slug), str(name), "config"))
    from . import more_ats, workday                            # imported here: workday.py and more_ats.py import Board and Posting from this module
    for key in more_ats.SINGLE:                                # sources.workable / smartrecruiters / teamtailor / recruitee / personio / pinpoint: slugs, as above
        for e in src.get(key) or []:
            if key == "eploy":                                 # sources.eploy: [{company, url}]: the tenant's host
                host = urlparse(str(e.get("url") or "")).hostname if isinstance(e, dict) else None
                if host and e.get("company"):
                    out.append(Board("eploy", host.lower(), str(e["company"]), "config"))
                continue
            slug, name = (e["slug"], e.get("name") or e["slug"]) if isinstance(e, dict) else (e, e.replace("-", " ").title())
            out.append(Board(key, str(slug), str(name), "config"))
    out += [e.board() for e in workday.entries(settings)]       # sources.workday: [{company, url}] (discovery/workday.py)
    return out


def resolved_boards(db: DB) -> list[Board]:
    """Boards of register companies that `resolve` verified (or you confirmed), under the REGISTER name, so the sponsor match is exact."""
    return [Board(r["ats"], r["ats_slug"], r["register_name"], "resolved") for r in db.conn.execute(
        "SELECT ats, ats_slug, register_name FROM companies WHERE ats_verified = 1 AND ats IN ('greenhouse','lever','lever_eu','ashby') "
        "AND ats_slug IS NOT NULL AND removed_on IS NULL ORDER BY register_name")]


def all_boards(settings, db: DB | None) -> list[Board]:
    """Configured boards plus verified register-company boards, each (ats, slug) once, the configured name winning."""
    seen, out = set(), []
    for b in [*configured_boards(settings), *(resolved_boards(db) if db is not None else [])]:
        if (b.ats, b.slug) not in seen:
            seen.add((b.ats, b.slug))
            out.append(b)
    return out
