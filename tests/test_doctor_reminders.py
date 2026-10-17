"""`careerops doctor` reminders: (1) from 2026-11-24 the fact bank's MSc note must not still say "Expected"; (2) the Gmail sign-in is
re-done once it is more than 6 days old. Both are warnings (they never fail the doctor) and appear only when they apply."""
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from careerops import setup
from careerops.discovery import gmail

NOW = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def bank(settings, tmp_path):
    def write(*education):
        p = tmp_path / "fact_bank.yaml"
        p.write_text(yaml.safe_dump({"education": list(education)}), encoding="utf-8")
        settings.raw["paths"]["fact_bank"] = str(p)
        return p
    return write


MSC_EXPECTED = {"degree": "MSc Data Science", "org": "University of Bristol", "note": "Distinction (Expected)"}


@pytest.mark.parametrize("today, warns", [(date(2026, 11, 24), True), (date(2026, 12, 1), True), (date(2027, 3, 1), True),
                                          (date(2026, 11, 23), False), (date(2026, 10, 15), False)])
def test_the_msc_note_warns_from_24_november_while_it_still_says_expected(settings, bank, today, warns):
    bank(MSC_EXPECTED)
    got = [c for c in setup.reminder_checks(settings, today=today, now=NOW) if "MSc" in c[0]]
    assert bool(got) is warns
    if warns:
        name, status, hint = got[0]
        assert status == setup.WARN and 'still says "Distinction (Expected)"' in name and "never does" in hint


def test_the_msc_note_is_quiet_once_the_grade_is_in_or_for_another_degree(settings, bank):
    bank({**MSC_EXPECTED, "note": "Distinction"})
    assert setup.reminder_checks(settings, today=date(2026, 12, 1), now=NOW) == []
    bank({"degree": "B.E. Electrical", "note": "Distinction (Expected)"})
    assert setup.reminder_checks(settings, today=date(2026, 12, 1), now=NOW) == []
    bank({"degree": "msc data science", "note": "distinction (expected)"})                 # case does not matter
    assert len(setup.reminder_checks(settings, today=date(2026, 12, 1), now=NOW)) == 1


def test_the_reminder_never_writes_the_fact_bank(settings, bank):
    p = bank(MSC_EXPECTED)
    before = p.read_bytes()
    setup.reminder_checks(settings, today=date(2026, 12, 1), now=NOW)
    assert p.read_bytes() == before


def token(settings, **fields):
    d = settings.path("secrets")
    d.mkdir(parents=True, exist_ok=True)
    p = d / gmail.TOKEN_FILE
    p.write_text(json.dumps({"refresh_token": "rt", "scope": gmail.SCOPE, **fields}), encoding="utf-8")
    return p


@pytest.mark.parametrize("days, warns", [(7.0, True), (6.5, True), (6.0, False), (2.0, False)])
def test_the_gmail_sign_in_warns_when_it_is_more_than_6_days_old(settings, bank, days, warns):
    bank(MSC_EXPECTED)
    token(settings, granted_at=(NOW - timedelta(days=days)).isoformat())
    got = [c for c in setup.reminder_checks(settings, today=date(2026, 10, 15), now=NOW) if "Gmail" in c[0]]
    assert bool(got) is warns
    if warns:
        assert got[0][1] == setup.WARN and "--gmail-auth" in got[0][2] and f"{days:.0f} days old" in got[0][0]


def test_a_token_without_granted_at_is_aged_by_its_file(settings, bank):
    bank(MSC_EXPECTED)
    p = token(settings)
    old = (NOW - timedelta(days=8)).timestamp()
    os.utime(p, (old, old))
    assert any("Gmail sign-in is 8 days old" in c[0] for c in setup.reminder_checks(settings, today=date(2026, 10, 15), now=NOW))


def test_no_token_no_warning(settings, bank):
    bank(MSC_EXPECTED)
    assert setup.reminder_checks(settings, today=date(2026, 10, 15), now=NOW) == [] and gmail.token_age_days(settings, NOW) is None


def test_a_refresh_keeps_the_grant_time(settings):
    """A refresh rewrites the token file; the age is of the grant, so it keeps granted_at."""
    token(settings, granted_at="2026-11-01T00:00:00+00:00", access_token="old", expires_at=0)
    gmail._write_token(settings, {**gmail._read_token(settings), "access_token": "new"})
    assert gmail._read_token(settings)["granted_at"] == "2026-11-01T00:00:00+00:00"
    assert gmail.token_age_days(settings, datetime(2026, 11, 8, tzinfo=timezone.utc)) == pytest.approx(7.0)


def test_authorize_writes_the_grant_time(settings):
    from conftest import FIX

    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text((FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8"), encoding="utf-8")
    import threading
    import urllib.request

    class Reply:
        ok = True
        status_code = 200

        def json(self):
            return {"refresh_token": "rt", "access_token": "at", "expires_in": 3600, "scope": gmail.SCOPE}

    def browser(url):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        target = f"{q['redirect_uri'][0]}?code=c&state={q['state'][0]}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()

    gmail.authorize(settings, open_browser=browser, post=lambda *a, **k: Reply(), say=lambda s: None)
    tok = gmail._read_token(settings)
    assert datetime.fromisoformat(tok["granted_at"]).tzinfo is not None and gmail.token_age_days(settings) < 0.01


@pytest.mark.slow          # doctor looks for playwright's browser
def test_doctor_includes_the_reminders_and_a_warning_does_not_fail_it(settings, monkeypatch):
    monkeypatch.setattr(setup, "reminder_checks", lambda s: [("a reminder", setup.WARN, "do the thing")])
    checks = setup.doctor(settings, ping=False)
    assert ("a reminder", setup.WARN, "do the thing") in checks
