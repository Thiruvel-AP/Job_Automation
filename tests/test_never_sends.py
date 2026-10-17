"""career-ops drafts; you send. No code of the package may send an e-mail, and the Google client refuses a send even if some future code
asks for one. This file is the tripwire: it fails the build on any of the strings below appearing in careerops/ (or scripts/)."""
from pathlib import Path

import pytest

import careerops
from careerops.googleapi import GoogleAPI, GoogleError

ROOT = Path(careerops.__file__).resolve().parent.parent
FORBIDDEN = [
    "smtplib",                  # the standard-library SMTP client
    "SMTP_SSL",                 # the old CompanyMail_Script.py way of sending
    "messages().send",          # google-api-python-client: users().messages().send(...)
    "drafts().send",            # google-api-python-client: users().drafts().send(...)
    "messages/send",            # the REST endpoints behind them
    "drafts/send",
    "messages:send",
    "drafts:send",
    ".sendmail(",
    ".send_message(",
]


def sources():
    for folder in ("careerops", "scripts"):
        for p in sorted((ROOT / folder).rglob("*")):
            if p.is_file() and p.suffix in (".py", ".sh") and "__pycache__" not in p.parts:
                yield p


def test_no_file_of_the_package_or_scripts_can_send_mail():
    hits = [f"{p.relative_to(ROOT)}: {pat}" for p in sources() for pat in FORBIDDEN if pat in p.read_text(encoding="utf-8", errors="replace")]
    assert not hits, "e-mail sending found (career-ops only creates drafts):\n" + "\n".join(hits)


def test_the_tripwire_really_scans_the_modules_that_talk_to_gmail():
    names = {p.name for p in sources()}
    assert {"outreach.py", "googleapi.py", "gmail.py", "contacts.py", "tracker.py"} <= names


@pytest.mark.parametrize("url", [
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
    "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send/",
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages:send",
])
def test_the_google_client_refuses_a_send_before_anything_leaves_the_machine(settings, url):
    api = GoogleAPI(settings, request=lambda *a, **k: pytest.fail("a request was made"), token="t", sleep=lambda s: None)
    with pytest.raises(GoogleError, match="never sends mail"):
        api.call("POST", url, body={"raw": "x"})
    assert api.calls == 0


@pytest.mark.parametrize("url", ["https://evil.example/gmail/v1/users/me/drafts", "http://gmail.googleapis.com/gmail/v1/users/me/drafts"])
def test_the_google_client_only_talks_to_google_over_https(settings, url):
    api = GoogleAPI(settings, request=lambda *a, **k: pytest.fail("a request was made"), token="the-token", sleep=lambda s: None)
    with pytest.raises(GoogleError, match="not a Google API address") as e:
        api.call("POST", url, body={})
    assert "the-token" not in str(e.value)
