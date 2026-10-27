"""config/profile/blacklist.yaml: employers and sites you will not apply to. It is YOURS: this module only reads it (it has no write call).

  blacklist:
    - {name: Acme Recruitment, scope: company, since: 2026-03-01, reason: ghosted me twice}
    - {name: ibm.com,          scope: domain,  since: 2025-11-20, reason: unpaid take-home task}

  scope company  `name` equals the job's company (case, punctuation, "(...)" and a trailing Ltd / plc / GmbH ignored; never a substring, so
                 "Acme" is not "Acme Recruitment")
  scope domain   the job's address (or its apply address) is on `name` or a subdomain of it: careers.ibm.com is ibm.com, notibm.com is not

A hit stops the job before tailoring: `<company> is on your blacklist since <date>: <reason>. Apply anyway? [y/N]`. A file that cannot be read is an
error (BlacklistError), never an empty blacklist: nothing is tailored on the guess that nobody is blacklisted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from ..apply.naming import folder_key

FILE = "blacklist.yaml"
SCOPES = ("company", "domain")


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
