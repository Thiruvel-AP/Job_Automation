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
    return ""


def visa_block(applicant: dict, country: str) -> dict | None:
    """The applicant.yaml block for a country: `visa_by_country: {Canada: {summary: ..., answers: {...}}}` (the key may be the name or
    the code). None when there is no non-empty block for it. The UK is the top-level `visa:` block and is not looked up here."""
    want = canon(country)
    for key, block in ((applicant or {}).get("visa_by_country") or {}).items():
        if want and canon(key) == want and isinstance(block, dict) and block:
            return block
    return None
