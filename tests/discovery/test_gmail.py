"""discovery/gmail.py: the read-only Gmail API (loopback + PKCE sign-in, token refresh, message list/get) and the parsers for the
LinkedIn / CV-Library / Indeed / Reed alert emails. No network: sign-in talks to a loopback server this test opens, Google's token
endpoint is a function, and Gmail's API is `Net`. The alert fixtures are hand-made; the parsers have not met a real mailbox."""
import base64
import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from careerops.discovery import gmail
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from conftest import FIX
from netfake import Net, Resp, fixture_text

CLIENT = (FIX / "discover" / "gmail_client.json").read_text(encoding="utf-8")


@pytest.fixture
def secrets(settings):
    d = settings.path("secrets")
    d.mkdir(parents=True)
    (d / gmail.CLIENT_FILE).write_text(CLIENT, encoding="utf-8")
    return d


class Token:
    """Google's token endpoint: remembers what it was sent, answers as scripted."""

    def __init__(self, body=None, status=200):
        self.body = body or {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": " ".join(gmail.SCOPES),
                             "token_type": "Bearer"}
        self.status, self.sent = status, []

    def __call__(self, url, data=None, timeout=None, **kw):
        self.sent.append((url, dict(data)))
        r = Resp(self.body, self.status)
        return r


def browser_that_grants(state_override=None, error=None):
    """An `open_browser` that plays the user: reads the redirect_uri and state from the consent URL and calls the redirect."""
    seen = {}

    def open_browser(url):
        q = parse_qs(urlparse(url).query)
        seen.update({k: v[0] for k, v in q.items()}, url=url)
        redirect = q["redirect_uri"][0]
        if error:
            target = f"{redirect}/?error={error}"
        else:
            target = f"{redirect}/?code=the-code&state={state_override or q['state'][0]}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
    open_browser.seen = seen
    return open_browser


# ------------------------------------------------------------------ the client file
def test_a_missing_client_file_says_how_to_make_one(settings):
    with pytest.raises(gmail.GmailAuthError, match="gmail_client.json not found.*Getting Gmail access"):
        gmail.client_config(settings)


def test_the_client_file_must_have_an_id_and_a_secret_and_a_google_token_address(settings, secrets):
    (secrets / gmail.CLIENT_FILE).write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
    with pytest.raises(gmail.GmailAuthError, match="no client_id / client_secret"):
        gmail.client_config(settings)
    (secrets / gmail.CLIENT_FILE).write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y",
                                                                       "token_uri": "https://evil.example.com/token"}}), encoding="utf-8")
    with pytest.raises(gmail.GmailAuthError, match="not a Google address"):
        gmail.client_config(settings)


def test_the_secrets_folder_is_inside_the_git_ignored_data_folder():
    from careerops.config import load_settings, ROOT
    s = load_settings()
    assert s.path("secrets").is_relative_to(ROOT / "data")
    assert "data/" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


# ------------------------------------------------------------------ sign-in
def test_sign_in_asks_for_the_three_scopes_with_pkce_and_keeps_a_private_token_file(settings, secrets):
    token, browser, said = Token(), browser_that_grants(), []
    gmail.authorize(settings, open_browser=browser, post=token, say=said.append, timeout_s=20)
    q = browser.seen
    assert gmail.SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    assert set(q["scope"].split()) == set(gmail.SCOPES) == {
        "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/spreadsheets"}
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code" and q["access_type"] == "offline"
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["client_id"].startswith("1234-abc")
    url, data = token.sent[0]
    assert url == "https://oauth2.googleapis.com/token" and data["code"] == "the-code" and data["grant_type"] == "authorization_code"
    challenge = base64.urlsafe_b64encode(__import__("hashlib").sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]                                    # the verifier is the one the challenge was made from
    saved = json.loads((secrets / gmail.TOKEN_FILE).read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "rt-1" and set(saved["scope"].split()) == set(gmail.SCOPES)
    assert oct((secrets / gmail.TOKEN_FILE).stat().st_mode & 0o777) == "0o600"
    assert not any("rt-1" in line or "at-1" in line or "GOCSPX" in line for line in said)        # nothing secret is printed
    assert any("never send" in line for line in said)


def test_a_wider_scope_than_the_three_is_refused_and_no_token_is_kept(settings, secrets):
    token = Token({"access_token": "a", "refresh_token": "r", "expires_in": 60,
                   "scope": " ".join(gmail.SCOPES) + " https://mail.google.com/"})
    with pytest.raises(gmail.GmailAuthError, match="only .*gmail.readonly.* are accepted"):
        gmail.authorize(settings, open_browser=browser_that_grants(), post=token, say=lambda s: None, timeout_s=20)
    assert not (secrets / gmail.TOKEN_FILE).exists()


def test_a_sign_in_where_a_box_was_unticked_is_refused_and_no_token_is_kept(settings, secrets):
    token = Token({"access_token": "a", "refresh_token": "r", "expires_in": 60, "scope": gmail.SCOPE + " " + gmail.SCOPE_SHEETS})
    with pytest.raises(gmail.GmailAuthError, match="lacks .*gmail.compose.*tick every box"):
        gmail.authorize(settings, open_browser=browser_that_grants(), post=token, say=lambda s: None, timeout_s=20)
    assert not (secrets / gmail.TOKEN_FILE).exists()


def test_a_token_from_before_the_new_scopes_still_reads_alerts_but_is_refused_for_drafts_and_the_sheet(settings, secrets):
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "x", "expires_at": 9e12, "scope": gmail.SCOPE})
    assert gmail.access_token(settings, post=lambda *a, **k: pytest.fail("asked")) == "x"
    for need in (gmail.SCOPE_COMPOSE, gmail.SCOPE_SHEETS):
        with pytest.raises(gmail.GmailAuthError, match="lacks .*--gmail-auth"):
            gmail.access_token(settings, need=(need,))
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "y", "expires_at": 9e12, "scope": " ".join(gmail.SCOPES)})
    assert gmail.access_token(settings, need=(gmail.SCOPE_COMPOSE, gmail.SCOPE_SHEETS)) == "y"


def test_a_wrong_state_or_a_refusal_in_the_browser_saves_nothing(settings, secrets):
    with pytest.raises(gmail.GmailAuthError, match="no valid answer"):
        gmail.authorize(settings, open_browser=browser_that_grants(state_override="forged"), post=Token(), say=lambda s: None, timeout_s=20)
    with pytest.raises(gmail.GmailAuthError, match="access_denied"):
        gmail.authorize(settings, open_browser=browser_that_grants(error="access_denied"), post=Token(), say=lambda s: None, timeout_s=20)
    assert not (secrets / gmail.TOKEN_FILE).exists()


def test_google_refusing_the_code_saves_nothing(settings, secrets):
    with pytest.raises(gmail.GmailAuthError, match="refused the sign-in"):
        gmail.authorize(settings, open_browser=browser_that_grants(), post=Token({"error": "invalid_grant"}, 400), say=lambda s: None, timeout_s=20)
    assert not (secrets / gmail.TOKEN_FILE).exists()


def test_a_token_is_refreshed_when_it_is_about_to_expire_and_reused_when_it_is_not(settings, secrets):
    gmail._write_token(settings, {"refresh_token": "rt-1", "access_token": "old", "expires_at": 1000.0, "scope": gmail.SCOPE})
    token = Token({"access_token": "new", "expires_in": 3600})
    assert gmail.access_token(settings, post=token, clock=lambda: 900.0) == "new" and token.sent[0][1]["grant_type"] == "refresh_token"
    assert json.loads((secrets / gmail.TOKEN_FILE).read_text(encoding="utf-8"))["refresh_token"] == "rt-1"       # kept
    assert gmail.access_token(settings, post=lambda *a, **k: pytest.fail("asked"), clock=lambda: 1000.0) == "new"
    gmail._write_token(settings, {"refresh_token": "rt-1", "access_token": "x", "expires_at": 5000.0, "scope": gmail.SCOPE})
    assert gmail.access_token(settings, post=lambda *a, **k: pytest.fail("asked"), clock=lambda: 100.0) == "x"


def test_not_signed_in_or_a_refused_refresh_says_to_run_gmail_auth(settings, secrets):
    with pytest.raises(gmail.GmailAuthError, match="--gmail-auth"):
        gmail.access_token(settings)
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "x", "expires_at": 0, "scope": gmail.SCOPE})
    with pytest.raises(gmail.GmailAuthError, match="refused to refresh.*--gmail-auth"):
        gmail.access_token(settings, post=Token({"error": "invalid_grant"}, 400))
    gmail._write_token(settings, {"refresh_token": "rt", "access_token": "x", "expires_at": 0, "scope": gmail.SCOPE + " other"})
    with pytest.raises(gmail.GmailAuthError, match="only .*gmail.readonly.* are accepted"):
        gmail.access_token(settings)


# ------------------------------------------------------------------ the API
def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def message(mid, frm, html, subject="Job alert"):
    return {"id": mid, "payload": {"mimeType": "multipart/alternative", "headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}],
                                   "parts": [{"mimeType": "text/plain", "body": {"data": b64("plain")}},
                                             {"mimeType": "text/html", "body": {"data": b64(html)}}]}}


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def test_the_query_names_the_four_senders_and_the_window(settings):
    q = gmail.build_query(settings)
    for d in ("linkedin.com", "cv-library.co.uk", "indeed.com", "reed.co.uk"):
        assert f"from:{d}" in q
    assert q.endswith("newer_than:14d")
    settings.raw["discover"]["gmail"].update(senders=["reed"], query_days=3)
    assert gmail.build_query(settings) == "(from:reed.co.uk) newer_than:3d"


def test_only_get_requests_with_the_bearer_token_are_made_to_gmail(settings):
    net = (Net().add("/messages/m1", message("m1", "LinkedIn <jobalerts-noreply@linkedin.com>", fixture_text("alert_linkedin.html")))
           .add("/messages", {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": None}))
    f = fetcher(net)
    ids = gmail.list_message_ids(f, "tok", "q", 10)
    msg = gmail.get_message(f, "tok", "m1")
    assert ids == ["m1", "m2"] and msg["id"] == "m1"
    assert all(r["headers"]["Authorization"] == "Bearer tok" for r in net.requests)
    assert all(r["url"].startswith("https://gmail.googleapis.com/gmail/v1/users/me/") for r in net.requests)
    assert not any(u.endswith("robots.txt") for u in net.urls(True))
    assert net.requests[0]["params"]["q"] == "q"


def test_the_list_pages_until_the_limit(settings):
    pages = iter([{"messages": [{"id": f"a{i}"} for i in range(50)], "nextPageToken": "p2"}, {"messages": [{"id": f"b{i}"} for i in range(50)]}])
    net = Net().add("/messages", lambda url, **kw: Resp(next(pages)))
    ids = gmail.list_message_ids(fetcher(net), "tok", "q", 60)
    assert len(ids) == 60 and net.requests[1]["params"]["pageToken"] == "p2" and net.requests[1]["params"]["maxResults"] == 10


def test_message_text_reads_headers_and_both_bodies():
    frm, subject, html, plain = gmail.message_text(message("m", "Reed <noreply@reed.co.uk>", "<b>hi</b>", "Your alert"))
    assert (frm, subject, html, plain) == ("Reed <noreply@reed.co.uk>", "Your alert", "<b>hi</b>", "plain")


@pytest.mark.parametrize("frm, kind", [("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "linkedin"), ("CV-Library <alerts@cv-library.co.uk>", "cvlibrary"),
                                       ("Indeed <alert@indeed.com>", "indeed"), ("Indeed <alert@mail.indeed.co.uk>", "indeed"),
                                       ("Reed <jobalerts@reed.co.uk>", "reed"), ("Mum <mum@gmail.com>", None),
                                       ("Phish <a@notlinkedin.com>", None), ("x@linkedin.com.evil.example", None)])
def test_sender_kind(frm, kind):
    assert gmail.sender_kind(frm) == kind


# ------------------------------------------------------------------ the alert parsers
def test_linkedin_alert_gives_title_company_place_and_a_canonical_link_never_fetched():
    items = gmail.parse_alert("linkedin", fixture_text("alert_linkedin.html"))
    assert [(i.title, i.company, i.location) for i in items] == [
        ("Data Scientist", "Acme Analytics Ltd", "London, England, United Kingdom"),
        ("Senior AI Engineer", "Unknown Startup Ltd", "Bristol, England, United Kingdom"),
        ("Retail Store Manager", "Shopco", "Leeds, England, United Kingdom")]
    assert items[0].link == "https://www.linkedin.com/jobs/view/3912345678"          # no tracking, no /comm/
    assert all(i.kind == "linkedin" for i in items)


def test_cv_library_alert_keeps_direct_job_links_and_skips_tracking_redirects_and_account_links():
    items = gmail.parse_alert("cvlibrary", fixture_text("alert_cvlibrary.html"))
    assert [(i.title, i.company, i.location, i.link) for i in items] == [
        ("Data Scientist", "Brightwell Analytics Ltd", "Bristol", "https://www.cv-library.co.uk/job/219034011"),
        ("Machine Learning Engineer", "Northgate Recruitment Solutions", "Leeds", "https://www.cv-library.co.uk/job/219000222")]


def test_indeed_alert_gives_a_pointer_link_from_the_jk_and_ignores_the_search_link():
    items = gmail.parse_alert("indeed", fixture_text("alert_indeed.html"))
    assert [(i.title, i.company, i.location, i.link) for i in items] == [
        ("Data Scientist", "Acme Analytics Ltd", "London", "https://uk.indeed.com/viewjob?jk=0123456789abcdef"),
        ("Machine Learning Engineer", "Beta Labs", "Manchester", "https://uk.indeed.com/viewjob?jk=fedcba9876543210")]


def test_reed_alert_gives_the_job_id_link():
    items = gmail.parse_alert("reed", fixture_text("alert_reed.html"))
    assert [(i.title, i.company, i.location, i.link) for i in items] == [
        ("Data Scientist", "Birch Health", "Leeds", "https://www.reed.co.uk/jobs/5001"),
        ("AI Engineer", "Cedar Labs", "Edinburgh", "https://www.reed.co.uk/jobs/5555")]


def test_an_email_with_no_job_links_or_a_foreign_one_gives_nothing():
    assert gmail.parse_alert("linkedin", "<p>Hello</p>") == [] and gmail.parse_alert("reed", "") == []
    assert gmail.parse_alert("linkedin", '<a href="https://evil.example.com/jobs/view/3912345678">Data Scientist</a>') == []
    assert gmail.job_link("cvlibrary", "https://www.cv-library.co.uk.evil.example.com/job/1") is None
    assert gmail.job_link("linkedin", "javascript:alert(1)") is None


def test_the_message_id_is_kept_on_each_item():
    assert {i.message_id for i in gmail.parse_alert("reed", fixture_text("alert_reed.html"), message_id="m9")} == {"m9"}
