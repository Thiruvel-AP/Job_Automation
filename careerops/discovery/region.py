"""Region targeting: which of the places a job is in count for the region(s) you chose. Zero tokens, deterministic, no network.

    match(location, body, selection)  ->  Match(verdict "in" | "out" | "unknown", evidence, region)

The order, per job (config/regions.yaml defines the regions; it is code-owned):
  1. ISO codes in the structured place become country names ("GB", "GBR" -> United Kingdom; `, ON` or `, MA` stay US / Canadian regions).
  2. `homonym_block` ("Cambridge, MA", "London, Ontario") -> out. It runs before `always_allow`, so "Cambridge" never lets "Cambridge, MA" in.
     The same idea covers any "<city>, <US state>": a qualifier that names another country decides, whatever the city is called.
  3. `always_allow` / `allow_cities` (word boundaries: "India" is not "Indianapolis"), or a country the place names -> in. A place with several
     options ("London or New York", "London / Remote") is in when any one option is.
  4. A place that does not say where the work is ("Remote", "EMEA", no place) -> the AD decides: "right to work in the UK", "UK-based", "UK time
     zone", a country named in it -> in; "must be based in the US", "US-based candidates only", "EU work authorisation required" -> out; both, or
     neither -> unknown (kept, flagged `region?`: missing data is never a penalty).
  5. Anything else -> out.

A job outside the region is PARKED, never skipped: a later `--region` brings it back (`park_queue`). Only the user's [s] makes a skip permanent.
Several regions may be chosen ("uk,ie"): a job passes when it passes any one of them. `any` turns the filter off (the old behaviour).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from ..config import ROOT
from . import countries

IN, OUT, UNKNOWN = "in", "out", "unknown"
ANY = "any"
DEFAULT_REGION = "uk"
REGIONS_FILE = ROOT / "config" / "regions.yaml"
PARKED_STATUS = "parked (region)"
QUEUED_STATUSES = ("new", "triaged", "needs_jd", "needs_country")      # the jobs-table statuses a region may park (a job in hand is never touched)
FLAG = "region?"


class RegionError(ValueError):
    """A region name that is not defined, or a regions.yaml that cannot be read (one line, fit to print)."""


# ------------------------------------------------------------------ countries by ISO code (a port of the reference repo's providers/_country.mjs)
_ISO = """AT AUT Austria|BE BEL Belgium|BG BGR Bulgaria|HR HRV Croatia|CY CYP Cyprus|CZ CZE Czechia;Czech Republic|DK DNK Denmark|EE EST Estonia
|FI FIN Finland|FR FRA France|DE DEU Germany;Deutschland|GR GRC Greece|HU HUN Hungary|IE IRL Ireland;Republic of Ireland|IT ITA Italy|LV LVA Latvia
|LT LTU Lithuania|LU LUX Luxembourg|MT MLT Malta|NL NLD Netherlands;The Netherlands;Holland|PL POL Poland|PT PRT Portugal|RO ROU Romania
|SK SVK Slovakia|SI SVN Slovenia|ES ESP Spain|SE SWE Sweden|GB GBR United Kingdom;Great Britain;Britain;England;Scotland;Wales;Northern Ireland
|US USA United States;United States of America;America|CA CAN Canada|AU AUS Australia|NZ NZL New Zealand|IN IND India|CH CHE Switzerland
|NO NOR Norway|IS ISL Iceland|LI LIE Liechtenstein|SG SGP Singapore|JP JPN Japan|CN CHN China|HK HKG Hong Kong|KR KOR South Korea|IL ISR Israel
|AE ARE United Arab Emirates|BR BRA Brazil|MX MEX Mexico|ZA ZAF South Africa|TR TUR Turkey|UA UKR Ukraine|RU RUS Russia|AR ARG Argentina
|CO COL Colombia|CL CHL Chile|PH PHL Philippines|ID IDN Indonesia|MY MYS Malaysia|TH THA Thailand|VN VNM Vietnam|NG NGA Nigeria|KE KEN Kenya
|EG EGY Egypt|SA SAU Saudi Arabia|PK PAK Pakistan|BD BGD Bangladesh|LK LKA Sri Lanka|RS SRB Serbia"""
NAME_OF: dict[str, str] = {}             # alpha-2 -> country name
A2_OF: dict[str, str] = {}               # alpha-3 -> alpha-2
_ALIASES: dict[str, str] = {}            # lower-case name or alias -> alpha-2
for _row in _ISO.replace("\n", "").split("|"):
    _a2, _a3, _names = _row.split(" ", 2)
    _first, *_more = _names.split(";")
    NAME_OF[_a2], A2_OF[_a3] = _first, _a2
    for _n in (_first, *_more):
        _ALIASES[_n.lower()] = _a2
_NAME_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in sorted(_ALIASES, key=len, reverse=True)) + r")(?![\w])", re.I)
_UK_NAMES = {"united kingdom", "great britain", "britain", "england", "scotland", "wales", "northern ireland"}

_US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
              "TN TX UT VT VA WA WV WI WY DC").split()
_CA_PROVINCES = "ON BC QC AB MB SK NS NB NL PE".split()
_AU_STATES = "NSW VIC QLD ACT TAS NT".split()
STATE_LIKE = frozenset(_US_STATES) | frozenset(_CA_PROVINCES) | frozenset(_AU_STATES)
_US_STATE_NAMES = ("alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
                   "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
                   "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
                   "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
                   "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "west virginia", "wisconsin", "wyoming")
_PROVINCE_NAMES = ("ontario", "quebec", "québec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia", "new brunswick")
_AU_STATE_NAMES = ("new south wales", "queensland", "victoria", "tasmania", "western australia", "south australia")
_SUBNATIONAL = [(re.compile(rf"^(?:{'|'.join(map(re.escape, names))})$", re.I), code) for names, code in
                ((_US_STATE_NAMES, "US"), (_PROVINCE_NAMES, "CA"), (_AU_STATE_NAMES, "AU"))]


def expand_iso(text: str) -> str:
    """The place with ISO country codes written as names: "GB" -> "United Kingdom", "London, GBR" -> "London, United Kingdom", "Remote - DE" -> "Remote - Germany".
    A two-letter tail that is also a US state, Canadian province or Australian state ("Birmingham, AL", "Dublin, OH") is a region, not a country, and is
    left alone (unless the place before it is just "Remote": "Remote, CA" is Canada)."""
    out = []
    for opt in split_options(text):
        m = re.match(r"^(?P<head>.*?)(?P<sep>,|\s[-–]\s|\()\s*(?P<code>[A-Z]{2,3})\)?$", opt) or re.match(r"^(?P<head>)(?P<sep>)(?P<code>[A-Z]{2,3})$", opt)
        if m:
            code = m.group("code")
            a2 = A2_OF.get(code) if len(code) == 3 else code if code in NAME_OF else None
            remote_head = bool(re.search(r"remote\s*$", m.group("head"), re.I))
            if a2 and (len(code) == 3 or code not in STATE_LIKE or remote_head or not m.group("head")):
                head, sep = m.group("head"), m.group("sep")
                opt = f"{head}{sep + ' ' if sep == ',' else sep}{NAME_OF[a2]}".strip() if head else NAME_OF[a2]
                if sep == "(":
                    opt += ")"
        out.append(opt)
    return " or ".join(out)


# ------------------------------------------------------------------ region definitions
@dataclass(frozen=True)
class Region:
    key: str
    label: str
    iso: frozenset[str]                               # alpha-2 codes
    always_allow: tuple[str, ...] = ()
    allow_cities: tuple[str, ...] = ()
    homonym_block: tuple[str, ...] = ()
    ambiguous_remote: tuple[str, ...] = ()

    @property
    def everywhere(self) -> bool:
        return self.key == ANY

    @property
    def country_words(self) -> tuple[str, ...]:
        """always_allow entries that name a country on their own (not "Remote - UK" forms, not a city such as Dublin): the ad body naming one of them
        names the region, and a place that already holds one ("Remote - UK, US") is a list, not a "<city>, <qualifier>"."""
        return tuple(p for p in self.always_allow if not p.lower().startswith("remote") and not p.lower().endswith("remote") and _names_in(p))


DEFAULT_AMBIGUOUS = ("Remote", "Anywhere", "Worldwide", "Global", "EMEA", "Europe", "Remote Europe", "Remote - EMEA")
_CACHE: dict[tuple[str, int], dict[str, Region]] = {}


def _a2s(codes: Iterable) -> frozenset[str]:
    return frozenset(A2_OF.get(str(c).upper(), str(c).upper()) for c in codes if str(c).strip())


def load(path: Path | None = None) -> dict[str, Region]:
    """config/regions.yaml as {key: Region}. A missing or unreadable file is a RegionError naming it."""
    p = Path(path) if path else REGIONS_FILE
    try:
        stamp = (str(p), p.stat().st_mtime_ns)
    except OSError:
        raise RegionError(f"{p.name} is missing: the region definitions are part of the code (config/regions.yaml)") from None
    if stamp in _CACHE:
        return _CACHE[stamp]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise RegionError(f"{p.name} cannot be read ({type(e).__name__})") from None
    if not isinstance(raw, dict):
        raise RegionError(f"{p.name} must be a mapping of region keys")
    defs: dict[str, Region] = {}
    for key, d in raw.items():
        d = d or {}
        if not isinstance(d, dict):
            raise RegionError(f"{p.name}: region {key!r} must be a mapping")

        def words(name: str) -> tuple[str, ...]:
            v = d.get(name) or []
            if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                raise RegionError(f"{p.name}: {key}.{name} must be a list of words")
            return tuple(x.strip() for x in v)
        defs[str(key).lower()] = Region(str(key).lower(), str(d.get("label") or key), _a2s(words("iso")), words("always_allow"), words("allow_cities"),
                                        words("homonym_block"), words("ambiguous_remote") or DEFAULT_AMBIGUOUS)
    _CACHE[stamp] = defs
    return defs


def get(key: str, defs: Mapping[str, Region] | None = None) -> Region:
    """A region by key; a two-letter ISO code that is not a key ("de", "au", "ca") is a region of that one country."""
    defs = defs if defs is not None else load()
    k = str(key).strip().lower()
    if k in defs:
        return defs[k]
    if k.upper() in NAME_OF:
        return Region(k, NAME_OF[k.upper()], frozenset({k.upper()}), (NAME_OF[k.upper()],), ambiguous_remote=DEFAULT_AMBIGUOUS)
    raise RegionError(f"unknown region {key!r}: use one of {', '.join(sorted(defs))} or a two-letter country code (de, au ...)")


def parse_selection(value, defs: Mapping[str, Region] | None = None) -> tuple[str, ...]:
    """"uk,ie" / ["uk", "ie"] -> ("uk", "ie"). "all" and `any` mean every region, and then nothing else matters. Raises RegionError for a name that is not defined."""
    defs = defs if defs is not None else load()
    parts = re.split(r"[,\s]+", value) if isinstance(value, str) else [str(x) for x in (value or [])]
    keys: list[str] = []
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        p = ANY if p == "all" else p
        get(p, defs)
        if p not in keys:
            keys.append(p)
    if not keys:
        raise RegionError("no region given: use one of " + ", ".join(sorted(defs)) + " or a two-letter country code")
    return (ANY,) if ANY in keys else tuple(keys)


def label_of(selection: Sequence[str], defs: Mapping[str, Region] | None = None) -> str:
    """"United Kingdom + Ireland" for a selection."""
    defs = defs if defs is not None else load()
    return " + ".join(get(k, defs).label for k in selection)


def iso_of(selection: Sequence[str], defs: Mapping[str, Region] | None = None) -> frozenset[str]:
    """The alpha-2 codes a selection covers (empty for `any`: it covers everything)."""
    defs = defs if defs is not None else load()
    codes: set[str] = set()
    for k in selection:
        codes |= get(k, defs).iso
    return frozenset(codes)


# ------------------------------------------------------------------ a place text
@dataclass(frozen=True)
class Match:
    verdict: str                  # in | out | unknown
    evidence: str = ""
    region: str = ""              # the key of the region that matched (when in)

    @property
    def flag(self) -> str:
        return FLAG if self.verdict == UNKNOWN else ""


_NONE_PLACES = {"", "n/a", "na", "none", "-", "--", "tbc", "tbd", "unknown", "not specified", "not stated", "various", "multiple"}


def split_options(text: str | None) -> list[str]:
    """The places a multi-location text offers: "London or New York" -> ["London", "New York"]. Commas are not separators ("Bristol, UK" is one place)."""
    t = " ".join(str(text or "").split())
    if t.lower() in _NONE_PLACES:
        return []
    return [o.strip() for o in re.split(r"\s+or\s+|\s*[;|]\s*|\s*/\s*|\s+and\s+|\s*&\s*", t) if o.strip() and o.strip().lower() not in _NONE_PLACES]


def _phrase(p: str) -> re.Pattern[str]:
    """A phrase on word boundaries, a comma or space in it matching either ("Cambridge, MA" also matches "Cambridge MA"); case-insensitive, but a
    short capitalised abbreviation (UK, U.K.) must be capitalised ("uk" is not a country)."""
    body = r"[\s,]+".join(re.escape(w) for w in re.split(r"[\s,]+", p.strip()) if w)
    flags = 0 if (len(p.replace(".", "")) <= 3 and p.upper() == p) else re.I
    return re.compile(rf"(?<![\w]){body}(?![\w])", flags)


def _names_in(text: str) -> list[tuple[str, str]]:
    """[(alpha-2, matched text)] for every country the text names (names, aliases, UK / US written in capitals). "Northern Ireland" is not Ireland."""
    hits: list[tuple[str, str]] = []
    uk_rx = re.compile(r"(?<![\w])(?:northern ireland|great britain|united kingdom|britain|england|scotland|wales)(?![\w])", re.I)
    hits += [("GB", m.group(0)) for m in uk_rx.finditer(text)]
    hits += [("GB", m.group(0)) for m in re.finditer(r"(?<![\w])(?:UK|U\.K\.)(?![\w])", text)]
    masked = uk_rx.sub(lambda m: " " * len(m.group(0)), text)
    hits += [(_ALIASES[m.group(1).lower()], m.group(1)) for m in _NAME_RX.finditer(masked)]
    hits += [("US", m.group(0)) for m in re.finditer(r"(?<![\w])(?:US|U\.S\.|USA|U\.S\.A\.)(?![\w])", masked)]
    return hits


def _groups_in(text: str) -> list[str]:
    """The region groups the text names (EU, EEA, EMEA, Europe, North America ...), by countries.REGIONS key."""
    return [k for k, (_, rx) in countries.REGIONS.items() if rx.search(text)]


def group_codes(key: str, defs: Mapping[str, Region] | None = None) -> frozenset[str]:
    """The alpha-2 codes a group like "EU" or "Europe" covers (the EU is the 27 of regions.yaml)."""
    codes = set(countries.REGIONS[key][0])
    if key in ("EU", "EEA", "Europe", "EMEA"):
        try:
            codes |= (defs if defs is not None else load())["eu"].iso
        except (RegionError, KeyError):
            pass
    if key == "EEA":
        codes |= {"NO", "IS", "LI"}
    return frozenset(codes)


def _city_country(city: str) -> str:
    """alpha-2 of the country a bare city name belongs to ("Bangalore" -> IN), "" when this tool does not know it."""
    name = countries.detect(city)
    return countries.code(name) if name else ""


def _qualifier(text: str, reg_words: Sequence[str]) -> tuple[frozenset[str], str] | None:
    """The country a "<place>, <qualifier>" text is qualified by: ", Ontario" -> CA, ", MA" -> US, ", Australia" -> AU, ", England" -> GB. None when the tail
    is not a qualifier (or the text before it already names this region's country: then it is a list, "Remote - UK, US")."""
    if "," not in text:
        return None
    head, _, tail = text.rpartition(",")
    head, tail = head.strip(), tail.strip().strip("()")
    if not tail:
        return None
    if any(_phrase(w).search(head) for w in reg_words):
        return None
    last = head.rsplit(",", 1)[-1].strip()
    remote_head = bool(re.search(r"remote\s*$", last, re.I))
    if re.fullmatch(r"[A-Z]{2}", tail):
        if tail in _US_STATES or tail in _CA_PROVINCES or tail in _AU_STATES:
            if tail in _CA_PROVINCES and tail not in _US_STATES:
                return frozenset({"CA"}), f"{last}, {tail} (a Canadian province)"
            if tail in _AU_STATES and tail not in _US_STATES:
                return frozenset({"AU"}), f"{last}, {tail} (an Australian state)"
            home = _city_country(last)
            if tail in NAME_OF and (remote_head or (home and home == tail)):
                return frozenset({tail}), f"{last}, {tail} (country code)"            # Toronto, CA / Bangalore, IN / Berlin, DE / Remote, CA
            return frozenset({"US"}), f"{last}, {tail} (a US state)"
        return None
    for rx, a2 in _SUBNATIONAL:
        if rx.match(tail):
            return frozenset({a2}), f"{last}, {tail}"
    names = _names_in(tail)
    if names and len(tail.split()) <= 4:
        return frozenset(a for a, _ in names), f"{last}, {tail}"
    return None


# ------------------------------------------------------------------ one option, one region
def _option(opt: str, reg: Region, defs: Mapping[str, Region]) -> tuple[str, str]:
    """('in' | 'out' | 'ambiguous', evidence) for one place option against one region."""
    if reg.everywhere:
        return IN, "any region"
    for h in reg.homonym_block:
        if _phrase(h).search(opt):
            return OUT, f"homonym_block: {h}"
    q = _qualifier(opt, reg.country_words)
    if q is not None:
        codes, ev = q
        return (IN, f"{ev} is in {reg.label}") if codes & reg.iso else (OUT, f"{ev}: not in {reg.label}")
    for p in reg.always_allow:
        if _phrase(p).search(opt):
            return IN, f"always_allow: {p}"
    for c in reg.allow_cities:
        if _phrase(c).search(opt):
            return IN, f"allow_cities: {c}"
    names = _names_in(opt)
    if names:
        inside = [t for a, t in names if a in reg.iso]
        return (IN, f"names {inside[0]}") if inside else (OUT, f"names {names[0][1]}: not in {reg.label}")
    groups = _groups_in(opt)
    if reg.key == "eu" and any(g in ("EU", "EEA") for g in groups):
        return IN, "names the EU"
    if groups:
        return "ambiguous", f"names {groups[0]}"
    if any(_phrase(a).search(opt) for a in reg.ambiguous_remote):
        return "ambiguous", f"does not say where: {opt}"
    found = countries.detect(opt)
    if found:
        a2 = countries.code(found)
        return (IN, f"{opt} is in {found}") if a2 in reg.iso else (OUT, f"{opt} is in {found}: not in {reg.label}")
    return OUT, f"{opt}: not in {reg.label}"


# ------------------------------------------------------------------ the ad's own text (the country-eligibility filter)
_ELIGIBLE_AFTER = [
    re.compile(r"(?:must|need to|have to|required to|should)\s+(?:currently\s+)?(?:be\s+)?(?:based|located|resident|residing|living|live|reside)\s+(?:in|within)\s+(?:the\s+)?", re.I),
    re.compile(r"(?:right|eligib\w+|authori[sz]ed|permitted|allowed|legally entitled)\s+to\s+work\s+(?:in|within)\s+(?:the\s+)?", re.I),
    re.compile(r"(?:open|available)\s+(?:only\s+)?to\s+(?:candidates|applicants|residents)\s+(?:(?:based|located|living|resident)\s+)?(?:in|within)\s+(?:the\s+)?", re.I),
    re.compile(r"(?:only|solely)\s+(?:considering|accepting|hiring|recruiting)\s+(?:candidates|applicants)\s+(?:(?:based|located|living|resident)\s+)?(?:in|within)\s+(?:the\s+)?", re.I),
    re.compile(r"(?:candidates|applicants|residents)\s+(?:must\s+be\s+)?(?:based|located|living|resident)\s+(?:in|within)\s+(?:the\s+)?", re.I),
    re.compile(r"(?:we\s+(?:can only|only)\s+hire|hiring only|(?:this|the)\s+(?:role|position)\s+is\s+(?:only\s+)?(?:open|available)\s+(?:to|in))\s+(?:candidates\s+(?:in|from)\s+)?(?:the\s+)?", re.I),
]
_ELIGIBLE_BEFORE = [
    re.compile(r"(?:time\s?zones?|business hours|working hours)", re.I),
    re.compile(r"work\s+(?:authori[sz]ation|permit|eligibility|visa)(?:\s+is)?(?:\s+required)?", re.I),
    re.compile(r"(?:citizens?|residents?|nationals?)\s+only", re.I),
]
_BASED_CANDIDATE = re.compile(r"(?<![\w])([A-Za-z.]{2,24}(?:\s[A-Za-z]{3,12})?)[- ]based\s+(?:candidates?|applicants?|only|roles?|positions?|employees?|hires?|talent|residents?)", re.I)
_PLACE_ONLY = re.compile(r"(?:within|in|from|to|for)\s+(?:the\s+)?([A-Za-z.]{2,24}(?:\s[A-Za-z]{3,12}){0,2})\s+only\b", re.I)         # "Remote within the United States only"
_BASED_BARE = re.compile(r"(?<![\w])((?:UK|U\.K\.|US|U\.S\.|USA|EU|United Kingdom|United States))-based(?![\w])")
_CLAUSE_END = re.compile(r"[.;:\n]")


def _statement_codes(text: str, defs: Mapping[str, Region]) -> frozenset[str]:
    codes = {a for a, _ in _names_in(text)}
    for g in _groups_in(text):
        codes |= group_codes(g, defs)
    return frozenset(codes)


def _statements(body: str, defs: Mapping[str, Region]) -> list[tuple[str, frozenset[str]]]:
    """[(quoted text, alpha-2 codes it names)] for every eligibility statement of the ad that names a place."""
    out: list[tuple[str, frozenset[str]]] = []
    for rx in _ELIGIBLE_AFTER:
        for m in rx.finditer(body):
            tail = body[m.end(): m.end() + 70]
            tail = tail[: (_CLAUSE_END.search(tail).start() if _CLAUSE_END.search(tail) else len(tail))]
            if (codes := _statement_codes(tail, defs)):
                out.append((" ".join((m.group(0) + tail).split())[:90], codes))
    for rx in _ELIGIBLE_BEFORE:
        for m in rx.finditer(body):
            head = body[max(0, m.start() - 30): m.start()]
            if (end := list(_CLAUSE_END.finditer(head))):
                head = head[end[-1].end():]
            if (codes := _statement_codes(head, defs)):
                out.append((" ".join((head + m.group(0)).split())[:90], codes))
    for rx in (_BASED_CANDIDATE, _PLACE_ONLY):
        for m in rx.finditer(body):
            if (codes := _statement_codes(m.group(1), defs)):
                out.append((" ".join(m.group(0).split())[:90], codes))
    return out


def body_decision(body: str, regs: Sequence[Region], defs: Mapping[str, Region] | None = None) -> Match:
    """What the ad's text says about where the work is, for a place that does not say: in, out, or unknown (see the module text)."""
    defs = defs if defs is not None else load()
    target = frozenset().union(*(r.iso for r in regs)) if regs else frozenset()
    ins: list[tuple[str, Region | None]] = []
    outs: list[str] = []
    for text, codes in _statements(body or "", defs):
        hit = [r for r in regs if codes & r.iso]
        (ins.append((text, hit[0])) if hit else outs.append(text))
    for m in _BASED_BARE.finditer(body or ""):                         # "UK-based": eligibility only when the place is the region's own
        if (codes := _statement_codes(m.group(1), defs)) and (hit := [r for r in regs if codes & r.iso]):
            ins.append((m.group(0), hit[0]))
    if ins and outs:
        return Match(UNKNOWN, f"the ad says both: \"{ins[0][0]}\" and \"{outs[0]}\"")
    if ins:
        return Match(IN, f'ad: "{ins[0][0]}"', ins[0][1].key if ins[0][1] else "")
    if outs:
        return Match(OUT, f'ad: "{outs[0]}"')
    named = [(a, t) for a, t in _names_in(body or "") if a in target]
    if named:
        reg = next(r for r in regs if named[0][0] in r.iso)
        return Match(IN, f'ad names {named[0][1]}', reg.key)
    return Match(UNKNOWN, "no place in the ad's eligibility text")


# ------------------------------------------------------------------ the verdict for a job
def _match(location: str | None, body: str | None = "", selection: Sequence[str] | str | None = None, *, country: str | None = "",
            defs: Mapping[str, Region] | None = None) -> Match:
    """Is this job in the selected region(s)? `location` is the structured place (a source's `locationName`, the `Location:` header), `country` the
    code or name a source gives for it, `body` the ad's text (read only for a place that does not say where the work is)."""
    defs = defs if defs is not None else load()
    keys = parse_selection(selection if selection is not None else DEFAULT_REGION, defs)
    regs = [get(k, defs) for k in keys]
    if any(r.everywhere for r in regs):
        return Match(IN, "any region", ANY)
    place = ", ".join(x for x in (str(location or "").strip(), str(country or "").strip()) if x)
    text = expand_iso(place)
    options = split_options(text)
    ambiguous = not options
    firsts_out = ""
    for opt in options:
        seen_out, seen_amb = "", False
        for r in regs:
            verdict, ev = _option(opt, r, defs)
            if verdict == IN:
                shown = f'location "{opt}": {ev}' if opt.lower() not in ev.lower() else f'location: {ev}'
                return Match(IN, shown + (f" (from {place})" if text != place else ""), r.key)
            if verdict == "ambiguous":
                seen_amb = True
            elif not seen_out:
                seen_out = ev
        if seen_amb:
            ambiguous = True
        elif seen_out and not firsts_out:
            firsts_out = f'location "{opt}": {seen_out}'
    if ambiguous:
        got = body_decision(str(body or ""), regs, defs)
        if got.verdict != UNKNOWN or not place:
            return got if place else Match(got.verdict, "no place given; " + got.evidence, got.region)
        return Match(UNKNOWN, f'location "{place}" does not say where; {got.evidence}')
    return Match(OUT, firsts_out or f'location "{place}": not in {label_of(keys, defs)}')


def refusal_parks(m: Match, body: str | None, defs: Mapping[str, Region] | None = None) -> Match:
    """v3.0: an EU job (the region that matched lies wholly inside the `eu` region, the UK does not) whose ad says it does not sponsor is PARKED (out, with the sentence as the
    evidence), never skipped: there is no register to check and no visa for it, so it waits for you. A UK job that says "no sponsorship" is untouched: that only lowers the
    sponsorship weight (priority.py), because you can work there now."""
    if m.verdict != IN or not m.region or m.region == ANY or not body:
        return m
    defs = defs if defs is not None else load()
    try:
        eu, here = get("eu", defs).iso, get(m.region, defs).iso
    except RegionError:
        return m
    if not here or not here <= eu:
        return m
    from . import priority                                # imported here: priority.py imports modules that import this one
    said = next((w for w in priority.sponsor_wording(str(body)) if w.kind == "no_sponsorship"), None)
    if said is None:
        return m
    return Match(OUT, f'EU job that refuses sponsorship ("{said.text}"): parked, not skipped; no visa answer for it', m.region)


def match(location: str | None, body: str | None = "", selection: Sequence[str] | str | None = None, *, country: str | None = "",
          defs: Mapping[str, Region] | None = None) -> Match:
    """`_match`, then the v3.0 rule that parks an EU job which refuses sponsorship (refusal_parks)."""
    return refusal_parks(_match(location, body, selection, country=country, defs=defs), body, defs)


def job_match(job: Mapping, selection: Sequence[str] | str | None = None, defs: Mapping[str, Region] | None = None) -> Match:
    """`match` for a job-like dict or row: its location, the country a source gave for it, and its description / text. `work_country` is not used: for a remote
    role it is just the home country, which says nothing about where the employer hires."""
    def get_(k: str) -> str:
        try:
            v = job[k]
        except (KeyError, IndexError):
            return ""
        return str(v or "")
    return match(get_("location"), get_("description") or get_("body"), selection, country=get_("country"), defs=defs)


REPORT_KEYS = ("uk", "ie", "eu", "us")            # the regions a report names a job by, in this order (Ireland before the EU it is part of)


def region_of(job: Mapping, defs: Mapping[str, Region] | None = None) -> str:
    """Which region a job is in, for the tracker's Region column and `careerops patterns`: uk | ie | eu | us, `unknown` (the place does not say), or `other`."""
    unknown = False
    for key in REPORT_KEYS:
        m = job_match(job, (key,), defs)
        if m.verdict == IN:
            return key
        unknown = unknown or m.verdict == UNKNOWN
    return "unknown" if unknown else "other"


# ------------------------------------------------------------------ sources
def source_runs(tags: Sequence[str] | None, selection: Sequence[str], defs: Mapping[str, Region] | None = None) -> bool:
    """Does a source tagged `regions: tags` run for this selection? No tag, `any`, or a selection of `any` always does; otherwise some tag must share a country
    with the selection (a `seek` source tagged [au] runs for `--region au`, a `stepstone` tagged [de] for `--region eu`)."""
    defs = defs if defs is not None else load()
    if not tags or ANY in selection or any(str(t).lower() == ANY for t in tags):
        return True
    chosen = iso_of(selection, defs)
    return any(get(t, defs).iso & chosen for t in tags)


def suggested_tag(country: str) -> list[str]:
    """The `regions:` value for a source that lists one country's jobs ("GB" -> [uk], "IE" -> [ie], "DE" -> [de]); [any] for `multi`."""
    c = str(country or "").strip()
    if not c or c.lower() == countries.MULTI:
        return [ANY]
    a2 = countries.code(c) or c.upper()
    return ["uk" if a2 == "GB" else a2.lower()]


def untagged_lines(registry) -> list[str]:
    """For each source of the registry with no `regions:` tag, the line the user should add to sources.yaml (this tool never edits that file)."""
    return [f"  {s.id}: regions: [{', '.join(suggested_tag(s.country))}]" for s in registry if not getattr(s, "regions", ())]


# ------------------------------------------------------------------ the queue
@dataclass
class Parked:
    parked: int = 0
    back: int = 0
    unknown: int = 0
    kept: int = 0

    def line(self, label: str) -> str:
        return (f"queue vs {label}: {self.parked} parked ({PARKED_STATUS}), {self.back} brought back, {self.unknown} region unknown, "
                f"{self.kept} in region")


def park_queue(db, selection: Sequence[str] | str, defs: Mapping[str, Region] | None = None) -> Parked:
    """Apply the region to the jobs waiting in the database: an out-of-region job gets the status `parked (region)` (the old status is kept in
    `parked_from`, never `skipped`), and a parked job that the selection now covers goes back to its old status. Safe to run again: it only moves a job
    whose answer changed. Every job's `region_match` / `region_evidence` is stored."""
    defs = defs if defs is not None else load()
    stats = Parked()
    with db.conn:
        for row in db.conn.execute("SELECT * FROM jobs WHERE status IN (?,?,?,?,?)", (*QUEUED_STATUSES, PARKED_STATUS)).fetchall():
            m = job_match(row, selection, defs)
            fields = {"region_match": m.verdict, "region_evidence": m.evidence}
            if row["status"] == PARKED_STATUS:
                if m.verdict != OUT:
                    fields.update(status=row["parked_from"] or "new", parked_from=None)
                    stats.back += 1
                else:
                    stats.parked += 1
            elif m.verdict == OUT:
                fields.update(status=PARKED_STATUS, parked_from=row["status"])
                stats.parked += 1
            elif m.verdict == UNKNOWN:
                stats.unknown += 1
            else:
                stats.kept += 1
            db.update_job(row["id"], commit=False, **fields)
    return stats
