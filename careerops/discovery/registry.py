"""The source registry (config/sources.yaml): every place jobs are looked for, and how.

One `Source` per entry (id, name, url, country, method, and the optional fields the method needs). `load(settings)` reads and checks the
file (a bad entry raises RegistryError naming it; a missing file is an empty registry, so a config without one still works). The
registry answers three questions for the rest of discovery:

  * which source sent this alert email                        `match_sender(from_header, subject)`
  * which source a job that was just found belongs to         `attribute(candidate)` (for the "new jobs" column of the health table)
  * which job boards are named by token                       `boards()`

Health checks of the sources are in health.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import countries
from . import region as region_mod

ATS_METHODS = ("greenhouse", "ashby", "lever")
MORE_METHODS = ("workable", "smartrecruiters", "teamtailor", "pinpoint", "recruitee", "personio", "workday")        # v2.6: more_ats.py (+ workday.py), boards found per company
VC_METHODS = ("getro", "consider")                                                                                # v2.6: multi-employer VC portfolio boards (more_ats.vc_read)
METHODS = ("greenhouse", "ashby", "lever", "reed", "jobbank_csv", "gmail_alerts", "dedupe_only", *MORE_METHODS, *VC_METHODS)
PARSERS = ("linkedin", "cvlibrary", "indeed", "reed")           # the dedicated alert parsers (gmail.py); others use the generic one
ID_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
TOKEN_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
DOMAIN_RX = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")


class RegistryError(ValueError):
    """config/sources.yaml is wrong. One line, names the entry."""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    country: str                          # ISO code of the country it lists jobs for, or "multi"
    method: str
    token: str = ""
    domains: tuple[str, ...] = ()
    subject_contains: tuple[str, ...] = ()
    parser: str = ""
    dataset: str = ""
    enabled: bool = True
    note: str = ""
    regions: tuple[str, ...] = ()         # `regions:` in sources.yaml: the regions this source is for (uk, ie, eu, us, au, ca, de ... or any); none = [any]

    @property
    def region_tags(self) -> tuple[str, ...]:
        """The tags the region filter reads: what sources.yaml says, `any` when it says nothing (discovery/region.py: source_runs)."""
        return self.regions or ("any",)

    @property
    def home_country(self) -> str:
        """The canonical country name when the source lists one country's jobs, else ''."""
        return "" if self.country.lower() == countries.MULTI else countries.canon(self.country)

    @property
    def kind(self) -> str:
        """What the gmail code calls this source: its dedicated parser's name, else its id."""
        return self.parser or self.id

    def matches_domain(self, domain: str) -> bool:
        return any(domain == d or domain.endswith("." + d) for d in self.domains)


@dataclass
class Registry:
    sources: list[Source] = field(default_factory=list)

    def __iter__(self):
        return iter(self.sources)

    def __len__(self) -> int:
        return len(self.sources)

    def get(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def by_kind(self, kind: str) -> Source | None:
        return next((s for s in self.sources if s.enabled and s.kind == kind), None) or self.get(kind)

    def active(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def with_method(self, *methods: str) -> list[Source]:
        return [s for s in self.active() if s.method in methods]

    def alert_sources(self, only: list[str] | None = None) -> list[Source]:
        """Sources whose alert emails are read: any enabled source with sender domains (optionally only these ids or kinds)."""
        out = [s for s in self.active() if s.domains]
        return [s for s in out if s.id in only or s.kind in only] if only else out

    # ---- alert emails
    def match_sender(self, from_header: str, subject: str = "", only: list[str] | None = None) -> Source | None:
        """The source an alert email belongs to: its sender domain is one of the source's domains (the longest match wins) and, when the
        source names subject words, the subject has one of them. A source without subject words loses to one whose words match."""
        m = re.search(r"@([\w.-]+)", from_header or "")
        domain = (m.group(1) if m else "").lower()
        if not domain:
            return None
        subj = (subject or "").lower()
        best: tuple[int, int, Source] | None = None
        for s in self.alert_sources(only):
            hit = [d for d in s.domains if domain == d or domain.endswith("." + d)]
            if not hit:
                continue
            if s.subject_contains and not any(w.lower() in subj for w in s.subject_contains):
                continue
            rank = (len(max(hit, key=len)), 1 if s.subject_contains else 0, s)
            if best is None or rank[:2] > best[:2]:
                best = rank
        return best[2] if best else None

    def vc(self) -> list[Source]:
        """The enabled VC portfolio boards (getro / consider): `url` is the board's page, `token` its collection / board id when the page does not carry it."""
        return [s for s in self.active() if s.method in VC_METHODS]

    # ---- boards named by token
    def boards(self) -> list:
        """ats.Board for every enabled greenhouse / ashby / lever entry that has a token."""
        from .ats import Board                  # lazy: ats imports modules that import this one's neighbours
        return [Board(s.method, s.token, s.name, "registry") for s in self.active() if s.method in ATS_METHODS and s.token]

    # ---- which source a found job came from
    def attribute(self, c) -> Source | None:
        """The registry source of a Candidate: the alert it came from, else the token board it was read from, else the entry for its
        method (reed, cv-library, a tokenless greenhouse / ashby / lever entry). None for what the registry does not list (adzuna)."""
        if c.origin.startswith("gmail:"):
            return self.by_kind(c.origin.split(":", 1)[1])
        if c.origin.startswith("vc:"):                               # an ad found through a VC portfolio board is credited to that board
            return self.get(c.origin.split(":", 1)[1])
        if c.board:
            ats, _, slug = c.board.partition(":")
            if ats in VC_METHODS:                                  # a VC board's jobs are credited to its source (the board's slug is the source id)
                return self.get(slug)
            for s in self.with_method(*ATS_METHODS):
                if s.token and s.method == ats.replace("lever_eu", "lever") and s.token.lower() == slug.lower():
                    return s
        kind = c.source.replace("lever_eu", "lever")
        if kind == "cvlibrary":
            return self.by_kind("cvlibrary")
        return next((s for s in self.with_method(kind) if not s.token), None) if kind in METHODS else None

    def label_source(self, label: str) -> Source | None:
        """The source behind a legacy fetch label ('greenhouse:slug', 'lever-eu:slug', 'ashby:slug', 'adzuna:query')."""
        kind, _, slug = label.partition(":")
        kind = kind.replace("lever-eu", "lever")
        for s in self.with_method(*ATS_METHODS):
            if s.method == kind and s.token and s.token.lower() == slug.lower():
                return s
        return next((s for s in self.with_method(kind) if not s.token), None)


def _tuple(entry: dict, key: str, where: str, lower: bool = False) -> tuple[str, ...]:
    v = entry.get(key) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise RegistryError(f"{where}: {key} must be a list of words")
    return tuple(x.strip().lower() if lower else x.strip() for x in v)


def parse(raw: object, source_file: str = "sources.yaml") -> Registry:
    items = (raw or {}).get("sources") if isinstance(raw, dict) else None
    if items is None:
        return Registry()
    if not isinstance(items, list):
        raise RegistryError(f"{source_file}: 'sources' must be a list")
    out: list[Source] = []
    for n, e in enumerate(items, 1):
        if not isinstance(e, dict):
            raise RegistryError(f"{source_file}: entry {n} is not a mapping")
        where = f"{source_file}: entry {n} ({e.get('id', '?')})"
        for key in ("id", "name", "url", "country", "method"):
            if not str(e.get(key) or "").strip():
                raise RegistryError(f"{where}: '{key}' is missing")
        sid, method, country = str(e["id"]).strip(), str(e["method"]).strip(), str(e["country"]).strip()
        if not ID_RX.match(sid):
            raise RegistryError(f"{where}: id must be lower-case letters, digits and '-'")
        if any(s.id == sid for s in out):
            raise RegistryError(f"{where}: id '{sid}' is used twice")
        if method not in METHODS:
            raise RegistryError(f"{where}: method '{method}' is not one of {', '.join(METHODS)}")
        if country.lower() != countries.MULTI and not countries.canon(country):
            raise RegistryError(f"{where}: country '{country}' is not a country code this tool knows (or 'multi')")
        if not re.match(r"^https?://", str(e["url"])):
            raise RegistryError(f"{where}: url must start with http:// or https://")
        domains = _tuple(e, "domains", where, lower=True)
        bad = [d for d in domains if not DOMAIN_RX.match(d)]
        if bad:
            raise RegistryError(f"{where}: {bad[0]!r} is not a domain name")
        if method == "gmail_alerts" and not domains:
            raise RegistryError(f"{where}: a gmail_alerts source needs 'domains' (the alert emails' sender domains)")
        token = str(e.get("token") or "").strip()
        if token and method not in (*ATS_METHODS, *VC_METHODS):
            raise RegistryError(f"{where}: 'token' only goes with greenhouse, ashby, lever, getro or consider")
        if token and not TOKEN_RX.match(token):
            raise RegistryError(f"{where}: token {token!r} is not a board token")
        parser = str(e.get("parser") or "").strip()
        if parser and parser not in PARSERS:
            raise RegistryError(f"{where}: parser must be one of {', '.join(PARSERS)}")
        regions = _tuple(e, "regions", where, lower=True)
        for tag in regions:
            try:
                region_mod.get(tag)
            except region_mod.RegionError:
                raise RegistryError(f"{where}: regions: {tag!r} is not a region (uk, ie, eu, us, any or a two-letter country code such as au, ca, de)") from None
        dataset = str(e.get("dataset") or "").strip()
        if method == "jobbank_csv" and not re.fullmatch(r"[0-9a-f-]{36}", dataset):
            raise RegistryError(f"{where}: a jobbank_csv source needs 'dataset' (the open.canada.ca dataset id)")
        out.append(Source(id=sid, name=str(e["name"]).strip(), url=str(e["url"]).strip(), country=country, method=method, token=token,
                          domains=domains, subject_contains=_tuple(e, "subject_contains", where), parser=parser, dataset=dataset,
                          enabled=bool(e.get("enabled", True)), note=str(e.get("note") or "").strip(),
                          regions=regions))
    return Registry(out)


def load(settings) -> Registry:
    """The registry in paths.sources (config/sources.yaml). A missing file or no `paths.sources` key is an empty registry."""
    key = (settings.raw.get("paths") or {}).get("sources")
    if not key:
        return Registry()
    path: Path = settings.path("sources")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Registry()
    except (OSError, yaml.YAMLError) as e:
        raise RegistryError(f"{path.name} cannot be read ({type(e).__name__})") from None
    return parse(raw, path.name)
