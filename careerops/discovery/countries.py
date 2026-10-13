"""Which country a place or a source is in, for the UK gate and the `listed_only` rule (discover.py, live.py, priority.py).

`canon("CA")` / `canon("Deutschland")` -> the one name used everywhere ("Canada", "Germany"); "" when it is not a country this table knows.
`detect("Toronto, ON")` -> "Canada": a place text read for a country name, then a region code (", ON", ", NY", ", NSW"), then a major
city. The table is deliberately short: a place it cannot read gives "" and the job is dropped as outside the UK, as before, rather
than guessed at. London, Ontario is Canada (regions are read before cities).
"""
from __future__ import annotations

import re

UK = "United Kingdom"
MULTI = "multi"            # a source that lists jobs in many countries (LinkedIn, an ATS): its country is read per job

# canonical name -> aliases (lower case). Codes are ISO 3166-1 alpha-2.
_NAMES: dict[str, tuple[str, ...]] = {
    UK: ("gb", "uk", "u.k.", "gbr", "great britain", "britain", "england", "scotland", "wales", "northern ireland"),
    "United States": ("us", "u.s.", "usa", "u.s.a.", "united states of america", "america"),
    "Canada": ("ca", "can"),
    "Australia": ("au", "aus"),
    "New Zealand": ("nz", "nzl", "aotearoa"),
    "Germany": ("de", "deu", "deutschland"),
    "Netherlands": ("nl", "nld", "the netherlands", "holland", "nederland"),
    "Ireland": ("ie", "irl", "republic of ireland", "eire"),
    "Singapore": ("sg", "sgp"),
    "France": ("fr", "fra"),
    "Spain": ("es", "esp", "españa"),
    "Italy": ("it", "ita", "italia"),
    "Switzerland": ("ch", "che", "schweiz", "suisse"),
    "Sweden": ("se", "swe", "sverige"),
    "Denmark": ("dk", "dnk", "danmark"),
    "Norway": ("no", "nor", "norge"),
    "Finland": ("fi", "fin", "suomi"),
    "Poland": ("pl", "pol", "polska"),
    "Portugal": ("pt", "prt"),
    "Belgium": ("be", "bel", "belgique", "belgië"),
    "Austria": ("at", "aut", "österreich"),
    "India": ("in", "ind"),
    "Japan": ("jp", "jpn"),
    "United Arab Emirates": ("ae", "uae"),
    "Israel": ("il", "isr"),
    "Brazil": ("br", "bra", "brasil"),
    "Mexico": ("mx", "mex", "méxico"),
    "South Africa": ("za", "zaf"),
    "China": ("cn", "chn"),
    "Hong Kong": ("hk", "hkg"),
    "South Korea": ("kr", "kor", "korea"),
}
_ALIAS: dict[str, str] = {}
for _name, _aliases in _NAMES.items():
    _ALIAS[_name.lower()] = _name
    for _a in _aliases:
        _ALIAS[_a] = _name

# a place text is searched for these (never the two-letter codes: "in", "no", "it" are words)
_NAME_WORDS = sorted({a for a in _ALIAS if len(a) > 3 or a in ("usa", "uae", "u.s.")}, key=len, reverse=True)
_NAME_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(a) for a in _NAME_WORDS) + r")(?![\w])", re.I)
_UK_RX = re.compile(r"(?<![\w])(united kingdom|uk|u\.k\.|great britain|britain|england|scotland|wales|northern ireland)(?![\w])", re.I)
_US_REMOTE = re.compile(r"(?<![\w])(us|u\.s\.)(?![\w])", re.I)

_CA_CODES = r"ON|BC|QC|AB|MB|SK|NS|NB"            # not NL (Netherlands) or PE
_US_CODES = ("AL AK AZ AR CA CO CT FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
             "TN TX UT VT VA WA WV WI WY DC").replace(" ", "|")
_AU_CODES = r"NSW|VIC|QLD|ACT|TAS|NT"
_REGION_RX = [        # read before the cities: London, Ontario is Canada
    (re.compile(rf"[,(]\s*({_CA_CODES})\b(?!\w)"), "Canada"),          # case-sensitive: ", ON" is Ontario, ", on" is not
    (re.compile(r"\b(ontario|quebec|québec|british columbia|alberta|manitoba|nova scotia|saskatchewan)\b", re.I), "Canada"),
    (re.compile(rf"[,(]\s*({_AU_CODES})\b(?!\w)"), "Australia"),
    (re.compile(r"\b(new south wales|queensland|victoria,? australia|western australia|south australia|tasmania)\b", re.I), "Australia"),
]
_US_REGION_RX = [     # read after the cities: ", IN" after Bangalore is India, after Springfield it is Indiana
    (re.compile(rf"[,(]\s*({_US_CODES})\b(?!\w)"), "United States"),
    (re.compile(r"\b(california|texas|new york|massachusetts|washington,? d\.?c\.?|colorado|illinois|florida|virginia|oregon)\b", re.I),
     "United States"),
]
_CITIES: dict[str, tuple[str, ...]] = {
    "United States": ("san francisco", "new york", "nyc", "seattle", "austin", "boston", "chicago", "los angeles", "mountain view",
                      "palo alto", "menlo park", "sunnyvale", "san jose", "san diego", "denver", "atlanta", "miami", "pittsburgh",
                      "redwood city", "bay area", "washington dc"),
    "Canada": ("toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary", "waterloo", "edmonton", "winnipeg"),
    "Australia": ("sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra"),
    "New Zealand": ("auckland", "wellington", "christchurch"),
    "Germany": ("berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "köln", "düsseldorf", "dusseldorf", "stuttgart"),
    "Netherlands": ("amsterdam", "rotterdam", "utrecht", "eindhoven", "the hague", "den haag"),
    "Ireland": ("dublin", "cork", "galway", "limerick"),
    "Singapore": ("singapore",),
    "France": ("paris", "lyon"),
    "Spain": ("madrid", "barcelona"),
    "Italy": ("milan", "milano", "rome", "roma"),
    "Switzerland": ("zurich", "zürich", "geneva", "lausanne", "basel"),
    "Sweden": ("stockholm", "gothenburg"),
    "Denmark": ("copenhagen",),
    "Norway": ("oslo",),
    "Finland": ("helsinki",),
    "Poland": ("warsaw", "krakow", "kraków", "wroclaw"),
    "Portugal": ("lisbon", "porto"),
    "Belgium": ("brussels",),
    "Austria": ("vienna",),
    "India": ("bangalore", "bengaluru", "hyderabad", "mumbai", "pune", "new delhi", "gurgaon", "chennai"),
    "Japan": ("tokyo",),
    "Israel": ("tel aviv",),
    "United Arab Emirates": ("dubai", "abu dhabi"),
    "Brazil": ("são paulo", "sao paulo"),
    "Mexico": ("mexico city",),
    "South Africa": ("cape town", "johannesburg"),
    "Hong Kong": ("hong kong",),
    "China": ("shanghai", "beijing"),
    "South Korea": ("seoul",),
}
_CITY_RX = [(re.compile(r"(?<![\w])(" + "|".join(re.escape(c) for c in cities) + r")(?![\w])", re.I), country)
            for country, cities in _CITIES.items()]
# UK towns are read last: "Birmingham, AL" is read as Alabama by the US region codes before it gets here, "London, Ontario" as Canada
_UK_CITIES = ("london", "manchester", "birmingham", "bristol", "leeds", "edinburgh", "glasgow", "cardiff", "belfast", "liverpool",
              "sheffield", "newcastle", "nottingham", "cambridge", "oxford", "southampton", "leicester", "brighton", "aberdeen",
              "milton keynes", "coventry", "norwich", "swansea", "exeter", "dundee", "inverness")
_UK_CITY_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(c) for c in _UK_CITIES) + r")(?![\w])", re.I)
_US_WORD = re.compile(r"(?<![\w])(US|U\.S\.|USA|U\.S\.A\.)(?![\w])")        # case-sensitive: "join us" is not a country


def canon(value: str | None) -> str:
    """The canonical country name for a name or code ("CA", "canada", "Deutschland"); "" for anything else (including "multi")."""
    return _ALIAS.get(" ".join(str(value or "").lower().replace("_", " ").split()), "")


def is_uk(value: str | None) -> bool:
    return canon(value) == UK


def detect(place: str | None) -> str:
    """The country a place text names or implies: a country name, else a region code, else a major city. "" when none."""
    text = " ".join(str(place or "").split())
    if not text:
        return ""
    if _UK_RX.search(text):
        return UK
    m = _NAME_RX.search(text)
    if m:
        return _ALIAS[m.group(1).lower()]
    if _US_REMOTE.search(text) and re.search(r"remote", text, re.I):
        return "United States"
    for rx, country in _REGION_RX:
        if rx.search(text):
            return country
    for rx, country in _CITY_RX:
        if rx.search(text):
            return country
    for rx, country in _US_REGION_RX:
        if rx.search(text):
            return country
    if _UK_CITY_RX.search(text):
        return UK
    return ""


# ------------------------------------------------------------------ regions ("remote, EU only"): which of the known countries each covers
EUROPE = frozenset({"GB", "IE", "DE", "NL", "FR", "ES", "IT", "CH", "SE", "DK", "NO", "FI", "PL", "PT", "BE", "AT"})
EU = frozenset({"IE", "DE", "NL", "FR", "ES", "IT", "SE", "DK", "FI", "PL", "PT", "BE", "AT"})
REGIONS: dict[str, tuple[frozenset, "re.Pattern[str]"]] = {
    "EU": (EU, re.compile(r"(?<![\w])(?:eu|e\.u\.|european union)(?![\w])", re.I)),
    "EEA": (EU | {"NO"}, re.compile(r"(?<![\w])eea(?![\w])", re.I)),
    "EMEA": (EUROPE | {"AE", "IL", "ZA"}, re.compile(r"(?<![\w])emea(?![\w])", re.I)),
    "Europe": (EUROPE, re.compile(r"(?<![\w])(?:europe|european)(?!\s+union)(?![\w])", re.I)),
    "North America": (frozenset({"US", "CA", "MX"}), re.compile(r"(?<![\w])north america\w*(?![\w])", re.I)),
    "Americas": (frozenset({"US", "CA", "MX", "BR"}), re.compile(r"(?<![\w])americas(?![\w])", re.I)),
    "LATAM": (frozenset({"MX", "BR"}), re.compile(r"(?<![\w])(?:latam|latin america)(?![\w])", re.I)),
    "APAC": (frozenset({"AU", "NZ", "SG", "IN", "JP", "CN", "HK", "KR"}), re.compile(r"(?<![\w])(?:apac|asia[- ]pacific)(?![\w])", re.I)),
}


def region_key(value: str | None) -> str:
    """The canonical key of a region ("eu" -> "EU", "emea" -> "EMEA"); "" when it is not one."""
    v = " ".join(str(value or "").split()).lower()
    return next((k for k in REGIONS if k.lower() == v), "")


def place_code(value: str | None) -> str:
    """The code of a country ("Ireland" -> "IE") or the key of a region ("emea" -> "EMEA"); "" when it is neither."""
    return code(value) or region_key(value)


def place_name(value: str | None) -> str:
    """How a place reads in a sentence: a country's name, a region's key."""
    return region_key(value) or canon(value)


def covers(place: str | None, home: str | None) -> bool:
    """Does this place (a country or a region, by name or code) include the country `home`?"""
    p, h = place_code(place), code(home)
    if not p or not h:
        return False
    return p == h or (p in REGIONS and h in REGIONS[p][0])


def home_words(home: str | None) -> re.Pattern[str] | None:
    """A pattern for the ways a text names the country `home` (the name, its other names and its code), for hiding it from what is shown."""
    name = canon(home)
    if not name:
        return None
    aliases = _NAMES.get(name, ())
    words = sorted({name.lower(), *(a for a in aliases if len(a) > 2)}, key=len, reverse=True)
    short = sorted({a.upper() for a in aliases if len(a) <= 2} | {code(name)})            # "UK", "GB": exact capitals only ("in" is a word)
    body = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![\w])(?:(?i:{body})|{'|'.join(re.escape(s) for s in short if s)})(?![\w])")


def code(value: str | None) -> str:
    """The ISO code of a country given by name or code ("Ireland" -> "IE", "uk" -> "GB"); "" when it is not one this tool knows."""
    name = canon(value)
    return next((a.upper() for a in _NAMES.get(name, ()) if len(a) == 2), "") if name else ""


def name_of(value: str | None) -> str:
    """The canonical name of a country given by name or code (canon, spelled for what reads as a name)."""
    return canon(value)


def mentions(text: str | None, cities: bool = False, regions: bool = False) -> list[str]:
    """Every country a free text names, in the order it names them, each once: country names and codes written in capitals (US, UK),
    Canadian / Australian regions and, with `cities`, major cities. With `regions`, regions (EU, EMEA, Europe ...) are listed too, by
    their key. For ad sentences, where "join us" and "our London office" must not decide anything, the default reads names only."""
    t = " ".join(str(text or "").split())
    found: list[tuple[int, str]] = [(m.start(), UK) for m in _UK_RX.finditer(t)]
    masked = _UK_RX.sub(lambda m: " " * len(m.group(0)), t)             # "Northern Ireland" is not Ireland
    if regions:
        for key, (_, rx) in REGIONS.items():
            found += [(m.start(), key) for m in rx.finditer(masked)]
            masked = rx.sub(lambda m: " " * len(m.group(0)), masked)             # "North America" is not "America" the country
    found += [(m.start(), _ALIAS[m.group(1).lower()]) for m in _NAME_RX.finditer(masked)]
    found += [(m.start(), "United States") for m in _US_WORD.finditer(masked)]
    if cities:
        for rx, country in [*_REGION_RX, *_CITY_RX, *_US_REGION_RX]:
            found += [(m.start(), country) for m in rx.finditer(masked)]
        found += [(m.start(), UK) for m in _UK_CITY_RX.finditer(masked)]
    out: list[str] = []
    for _, country in sorted(found):
        if country not in out:
            out.append(country)
    return out


def visa_block(applicant: dict, country: str) -> dict | None:
    """The applicant.yaml block for a country: `visa.by_country.<country>` (the key is a code or a name: GB, Ireland). None when there
    is no non-empty block for exactly that country; `visa.by_country.default` is not a country and never counts as one."""
    want = canon(country)
    by_country = (((applicant or {}).get("visa") or {}).get("by_country")) or {}
    for key, block in by_country.items():
        if want and canon(key) == want and isinstance(block, dict) and block:
            return block
    return None
