"""Inbox ads for the triage, shortlist and live-approval tests: a JD file with the header lines discover writes, a body that names the keywords asked for, and
an age. `NOW` is the test clock."""
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
FILLER = ("We are a small team building data products for clinical researchers in London. You will work with scientists, engineers and product "
          "managers to turn research questions into reliable tools. We value clear writing, careful testing and honest reporting of results. ")


def jd_body(required=("Python", "SQL", "scikit-learn"), extra: str = "") -> str:
    """An ad body whose Requirements section asks for `required` (the taxonomy scan reads them as required keywords)."""
    return (f"About the team\n{FILLER * 2}\n\nRequirements:\n" + "".join(f"- {t}\n" for t in required) + f"\n{extra}\n"
            "What we offer\nA friendly team and flexible hours.\n")


def ad_text(*, company="Acme", title="Data Scientist", age_days=2.0, salary=None, url=None, required=("Python", "SQL", "scikit-learn"), extra="",
            apply_kind=None, country=None, agency=False, base=None) -> str:
    seen = ((base or NOW) - timedelta(days=age_days)).isoformat(timespec="seconds")
    head = [f"Company: {company}", f"Title: {title}", f"URL: {url or 'https://boards.greenhouse.io/' + company.lower().replace(' ', '') + '/jobs/' + str(zlib.crc32((company + title).encode()) % 100000 + 1000)}",
            "Location: London, UK", f"First seen: {seen}"]
    if salary:
        head.append(f"Salary: {salary}")
    if apply_kind:
        head.append(f"Apply kind: {apply_kind}")
    if country:
        head.append(f"Country: {country}")
    if agency:
        head.append("Agency: yes")
    return "\n".join(head) + "\n\n" + jd_body(required, extra)


def write_ad(folder: Path, name: str, **kw) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(ad_text(**kw), encoding="utf-8")
    return p


class FakeIndex:
    """A sponsor register index that knows the companies in `exact`."""

    def __init__(self, exact=()):
        self.exact = {e.lower() for e in exact}

    def lookup(self, company, threshold=0.92):
        from types import SimpleNamespace
        return SimpleNamespace(label="exact (Skilled Worker A)" if company.lower() in self.exact else "none")
