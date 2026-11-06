"""config/profile/blacklist.yaml: employers and sites you will not apply to. It is YOURS: this module only reads it (it has no write call).

  blacklist:
    - {name: Acme Recruitment, scope: company, since: 2026-03-01, reason: ghosted me twice}
    - {name: ibm.com,          scope: domain,  since: 2025-11-20, reason: unpaid take-home task}

  scope company  `name` equals the job's company (case, punctuation, "(...)" and a trailing Ltd / plc / GmbH ignored; never a substring, so
                 "Acme" is not "Acme Recruitment")
  scope domain   the job's address (or its apply address) is on `name` or a subdomain of it: careers.ibm.com is ibm.com, notibm.com is not

A hit stops the job before tailoring: `<company> is on your blacklist since <date>: <reason>. Apply anyway? [y/N]`. A file that cannot be read is an
error (BlacklistError), never an empty blacklist: nothing is tailored on the guess that nobody is blacklisted.

v3.1, reason `training_scheme`: a fee-charging training course advertised as a job is not a job. Built into the code (your blacklist.yaml is not touched):
  * the companies in TRAINING_SCHEME_COMPANIES (ITOL Recruit: "Please note this is a training course and fees apply", "Job guaranteed ... or get your money back");
  * any ad whose text holds one of TRAINING_SCHEME_PHRASES, or both words of a TRAINING_SCHEME_PAIRS pair.
`training_scheme(company, text)` gives the evidence (the company, or the phrase as the ad writes it); the shortlist reports it as `training scheme (fees)`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from ..apply.naming import folder_key

FILE = "blacklist.yaml"
SCOPES = ("company", "domain")
TRAINING_SCHEME = "training_scheme"                       # the blacklist reason code of a fee-charging training course (v3.1)
TRAINING_SCHEME_LABEL = "training scheme (fees)"
TRAINING_SCHEME_COMPANIES = ("ITOL Recruit",)             # sells AI / data "traineeships" with fees as jobs (seen 5 Nov 2026: 5 of 8 shortlist rows)
TRAINING_SCHEME_PHRASES = ("training course and fees apply", "fees apply", "course fee", "job guarantee", "job guaranteed", "money back")
TRAINING_SCHEME_PAIRS = (("funded training", "fee"), ("traineeship", "fee"))


class BlacklistError(ValueError):
    """blacklist.yaml cannot be understood (one line, fit to print)."""


@dataclass(frozen=True)
class Entry:
    name: str
    scope: str = "company"
    since: str = ""
    reason: str = ""


def path_for(settings) -> Path:
    """The file sits beside the fact bank, in config/profile/."""
    return settings.path("fact_bank").parent / FILE


def _host(text: str) -> str:
    t = str(text or "").strip().lower()
    host = urlparse(t if "//" in t else "//" + t).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _on(host: str, domain: str) -> bool:
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def _since(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return (value.date() if isinstance(value, datetime) else value).isoformat()
    return str(value or "").strip()


@dataclass
class Blacklist:
    entries: list[Entry]

    def hit(self, company: str, *urls: str) -> Entry | None:
        """The first entry that covers this company or one of these addresses, or None."""
        family = folder_key(company or "")
        hosts = [h for h in (_host(u) for u in urls if u) if h]
        for e in self.entries:
            if e.scope == "company":
                if family and folder_key(e.name) == family:
                    return e
            elif any(_on(h, _host(e.name)) for h in hosts):
                return e
        return None


def _phrase_rx(phrase: str) -> re.Pattern[str]:
    """A phrase on word boundaries, any run of spaces between its words; a last word may take a plural (fee -> fees)."""
    words = phrase.split()
    return re.compile(r"(?<![\w])" + r"\s+".join(re.escape(w) for w in words) + r"s?(?![\w])", re.I)


_SCHEME_PHRASES = [(p, _phrase_rx(p)) for p in TRAINING_SCHEME_PHRASES]
_SCHEME_PAIRS = [((a, _phrase_rx(a)), (b, _phrase_rx(b))) for a, b in TRAINING_SCHEME_PAIRS]


def training_scheme(company: str, text: str) -> str | None:
    """Why this ad is a fee-charging training scheme, not a job: `ITOL Recruit is a known training-scheme seller`, or `the ad says "fees apply"`
    (the words as the ad writes them). None when it is not one."""
    fam = folder_key(company or "")
    if fam and any(folder_key(c) == fam for c in TRAINING_SCHEME_COMPANIES):
        return f"{company} sells fee-charging training courses as jobs"
    body = str(text or "")
    for _, rx in _SCHEME_PHRASES:
        if (m := rx.search(body)):
            return f'the ad says "{" ".join(m.group(0).split())}"'
    for (a, ra), (b, rb) in _SCHEME_PAIRS:
        if (ma := ra.search(body)) and (mb := rb.search(body)):
            return f'the ad says "{" ".join(ma.group(0).split())}" and "{" ".join(mb.group(0).split())}"'
    return None


def question(entry: Entry, company: str) -> str:
    since = f" since {entry.since}" if entry.since else ""
    reason = f": {entry.reason}" if entry.reason else ""
    return f"{company} is on your blacklist{since}{reason}. Apply anyway? [y/N] "


def load(path: Path) -> Blacklist:
    """The blacklist in `path`; a missing file is an empty one. Raises BlacklistError for a file that cannot be understood."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Blacklist([])
    except (OSError, yaml.YAMLError) as e:
        raise BlacklistError(f"{Path(path).name} cannot be read ({str(e).splitlines()[0][:100] if str(e) else type(e).__name__})") from None
    rows = raw.get("blacklist") if isinstance(raw, dict) else raw
    if rows is None:
        return Blacklist([])
    if not isinstance(rows, list):
        raise BlacklistError(f"{Path(path).name}: `blacklist` must be a list of entries")
    out = []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise BlacklistError(f"{Path(path).name}: entry {n} is not a {{name, scope, since, reason}} mapping")
        name = str(row.get("name") or "").strip()
        scope = str(row.get("scope") or "company").strip().lower()
        if not name:
            raise BlacklistError(f"{Path(path).name}: entry {n} has no name")
        if scope not in SCOPES:
            raise BlacklistError(f"{Path(path).name}: entry {n} ({name}) has scope {scope!r}; it must be company or domain")
        out.append(Entry(name, scope, _since(row.get("since")), str(row.get("reason") or "").strip()))
    return Blacklist(out)
