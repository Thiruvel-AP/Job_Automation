"""v3.8: the Gmail label source (`discover --gmail-label`): label lookup by name, the type of each email, the job cards, applied-through-LinkedIn memory,
dedupe, resolution through the employer's board, and the one rule that matters most: no request to linkedin.com, ever. Every email here is made by hand;
no real mail, no network (`Net` answers), no real settings."""
import argparse
import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests

from careerops import cli, setup
from careerops.config import ROOT
from careerops.discovery import gmail, intake
from careerops.discovery import linkedin_label as ll
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net, fixture_json
from test_discover import TODAY, cfg, make, read_inbox, signed_in            # noqa: F401  (fixtures)

LABELS = [{"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "Label_7", "name": "LinkedIn"},
          {"id": "Label_9", "name": "Jobs/LinkedIn"}, {"id": "Label_3", "name": "Receipts"}]
ALERT_FROM = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
REC_FROM = "LinkedIn <jobs-noreply@linkedin.com>"
NEWER = TODAY - timedelta(hours=3)


def ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def card(job_id, title, company, place, *extra, link="https://www.linkedin.com/comm/jobs/view/{id}?trackingId=zz%3D%3D&amp;refId=q"):
    href = link.format(id=job_id)
    company_line = f"<p>{company} · {place}</p>" if company else ""
    return (f'<tr><td><a href="{href}">{title}</a>{company_line}' + "".join(f"<p>{e}</p>" for e in extra)
            + f'<a href="{href}">View job</a></td></tr>')


def email_html(*cards, head="Your job alert for data scientist in United Kingdom"):
    return (f"<html><body><p>{head}</p><table>{''.join(cards)}</table>"
            '<p><a href="https://www.linkedin.com/comm/jobs/search?keywords=data">See all jobs on LinkedIn</a></p>'
            '<p><a href="https://www.linkedin.com/e/v2?e=unsubscribe">Unsubscribe</a></p></body></html>')


def label_net(mails: dict, labels=LABELS, extra=None) -> Net:
    """mails: id -> (from, subject, html, received datetime). The label list, the message list of label Label_7 and each message."""
    net = Net(strict=True)
    for mid, (frm, subject, html, when) in mails.items():
        net.add(f"/messages/{mid}", {"id": mid, "internalDate": ms(when), "payload": {
            "mimeType": "text/html", "headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}],
            "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}})

    def listing(url, params=None, **kw):
        assert params["labelIds"] in (["Label_7"], ["Label_9"]), params                  # by label id, never a q="label:..." string
        assert "label:" not in str(params.get("q", ""))
        return {"messages": [{"id": m} for m in mails]}

    net.add("users/me/labels", {"labels": labels})
    net.add("gmail.googleapis.com/gmail/v1/users/me/messages", listing)
    for part, reply in reversed(list((extra or {}).items())):
        net.routes.insert(0, (part, reply))
    return net


def run(cfg, db, net, **kw):
    d = make(cfg, db, net, **kw)
    d.from_gmail_label(["LinkedIn"], "30d")
    return d


def results(d):
    return {(r["company"], r["title"]): r["result"] for r in d.label_rows}


@pytest.fixture(autouse=True)
def no_linkedin_anywhere(monkeypatch):
    """Whatever else happens, nothing in the process may reach a host ending in linkedin.com (requests, any session, intake's own fetcher)."""
    def guard(real):
        def f(*a, **kw):
            url = str(kw.get("url") or next((x for x in a if isinstance(x, str) and "://" in x), ""))
            assert not (urlparse(url).hostname or "").endswith("linkedin.com"), f"a request went to {url}"
            return real(*a, **kw)
        return f
    for name in ("get", "post", "request", "head"):
        monkeypatch.setattr(requests, name, guard(getattr(requests, name)))
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: pytest.fail(f"a session request went to {url}"))

    def boom(url, **kw):
        raise AssertionError(f"intake fetched {url}")
    monkeypatch.setattr(intake, "_get", boom)


# ------------------------------------------------------------------ 1. the label, looked up by name
def test_a_label_is_found_by_its_name_and_a_nested_name_works():
    assert gmail.find_label(LABELS, "LinkedIn")["id"] == "Label_7"
    assert gmail.find_label(LABELS, "Jobs/LinkedIn")["id"] == "Label_9"
    assert gmail.find_label(LABELS, "jobs / linkedin")["id"] == "Label_9"               # case and spaces around the slash do not matter
    assert gmail.find_label(LABELS, "Jobs") is None and gmail.find_label(LABELS, "Missing") is None


def test_messages_are_listed_by_label_id_and_the_scope_stays_read_only(signed_in):
    net = label_net({"m1": (ALERT_FROM, "3 new jobs for data scientist", email_html(), NEWER)})
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    ids = gmail.list_label_message_ids(f, "at", "Label_9", 10, after=1700000000)
    assert ids == ["m1"]
    params = [r["params"] for r in net.requests if r["params"]][-1]
    assert params["labelIds"] == ["Label_9"] and params["q"] == "after:1700000000"
    assert gmail.SCOPE.endswith("/gmail.readonly")                                        # labels.list and messages.list/get work under it: no scope change
    gmail._check_scope(gmail.SCOPE)                                                       # the existing token (read-only) is enough for the label source


def test_a_missing_label_is_a_failure_that_names_the_labels_that_exist(signed_in, db):
    d = make(signed_in, db, label_net({}))
    d.from_gmail_label(["Nope"], "30d")
    assert any("Gmail label 'Nope' not found" in f and "Jobs/LinkedIn" in f and "Receipts" in f for f in d.run.failures)


def test_doctor_prints_the_missing_label_and_the_labels_there_are(signed_in):
    signed_in.raw["discover"]["gmail"]["labels"] = ["LinkedIn", "Gone"]
    net = label_net({})
    f = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    checks = setup.gmail_label_checks(signed_in, True, fetcher=f)
    assert ("Gmail label 'LinkedIn'", True, "") in checks
    name, ok, hint = checks[1]
    assert name == "Gmail label 'Gone' not found" and ok is False and "Receipts" in hint and "Jobs/LinkedIn" in hint
    assert setup.gmail_label_checks(signed_in, False) == []                              # --no-ping: no call


def test_doctor_asks_nothing_when_not_signed_in(cfg):
    assert setup.gmail_label_checks(cfg, True) == []


# ------------------------------------------------------------------ the type of each email
@pytest.mark.parametrize("frm, subject, kind", [
    (ALERT_FROM, "3 new jobs for data scientist in United Kingdom", ll.JOB_ALERT),
    (ALERT_FROM, "“AI engineer”: 12 new jobs", ll.JOB_ALERT),
    (REC_FROM, "Jobs you may be interested in", ll.JOB_RECOMMENDATION),
    (REC_FROM, "Acme Analytics is hiring: Data Scientist", ll.JOB_RECOMMENDATION),
    (REC_FROM, "Your application was sent to Acme Analytics Ltd", ll.APPLICATION_SENT),
    (REC_FROM, "Acme Analytics Ltd viewed your application", ll.APPLICATION_UPDATE),
    (REC_FROM, "Your application to Data Scientist at Acme Analytics Ltd was not selected", ll.APPLICATION_UPDATE),
    ("A Recruiter <messaging-digest-noreply@linkedin.com>", "Priya sent you a message", ll.OTHER),
    ("LinkedIn News <news@linkedin.com>", "Your weekly newsletter", ll.OTHER),
    ("Mum <mum@gmail.com>", "3 new jobs for you", ll.OTHER)])                            # a sender that is not LinkedIn is never a job email
def test_each_email_is_sorted_by_sender_and_subject(frm, subject, kind):
    assert ll.classify(frm, subject) == kind


# ------------------------------------------------------------------ 2-3. cards
def test_an_alert_with_three_cards_gives_three_jobs_with_their_fields(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, England, United Kingdom"),
                      card("3999999999", "Senior AI Engineer", "Delta Works", "Bristol, England, United Kingdom", "£70,000 - £85,000", "Hybrid"),
                      card("3888888888", "Machine Learning Engineer", "Kite Data", "Remote", "Easy Apply"))
    d = run(signed_in, db, label_net({"m1": (ALERT_FROM, "3 new jobs for data scientist", html, NEWER)}))
    assert len(d.label_rows) == 3
    rows = {r["title"]: r for r in d.label_rows}
    assert (rows["Data Scientist"]["company"], rows["Data Scientist"]["location"], rows["Data Scientist"]["job_id"]) == (
        "Orbit Labs", "London, England, United Kingdom", "3912345678")
    assert rows["Senior AI Engineer"]["salary"].startswith("£70,000") and rows["Senior AI Engineer"]["work_type"] == "hybrid"
    assert rows["Machine Learning Engineer"]["easy_apply"] and rows["Machine Learning Engineer"]["job_id"] == "3888888888"
    stored = db.conn.execute("SELECT source, linkedin_job_id, company, title, location, link FROM linkedin_cards ORDER BY id").fetchall()
    assert [r["linkedin_job_id"] for r in stored] == ["3912345678", "3999999999", "3888888888"] and {r["source"] for r in stored} == {"linkedin_email"}
    assert stored[0]["link"].startswith("https://www.linkedin.com/jobs/view/3912345678")      # kept as text, canonical, never requested
    assert d.label_kinds[ll.JOB_ALERT] == 1


def test_a_current_job_id_link_gives_the_job_id():
    item = gmail.parse_alert("linkedin", email_html(card("x", "Data Scientist", "Orbit Labs", "London",
                                                       link="https://www.linkedin.com/comm/jobs/search?currentJobId=3777777777&amp;t=1")))
    assert [(i.title, i.job_id) for i in item] == [("Data Scientist", "3777777777")]


def test_a_card_without_a_company_is_counted_as_unparsed_and_dropped(signed_in, db):
    html = email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK"), card("3922222222", "AI Engineer", "", ""),
                      card("3933333333", "Applied Scientist", "Delta Works", "London, UK"))
    d = run(signed_in, db, label_net({"m1": (ALERT_FROM, "3 new jobs", html, NEWER)}))
    assert d.label_unparsed == 1 and len(d.label_rows) == 2 and d.run.sources["gmail_label"]["unparsed"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM linkedin_cards").fetchone()[0] == 2


# ------------------------------------------------------------------ 4. application_sent
def test_application_sent_marks_applied_and_creates_no_job_and_a_card_for_it_is_already_applied(signed_in, db):
    sent = email_html(card("3944444444", "Data Scientist", "", ""), head="Your application was sent")
    cards = email_html(card("3955555555", "Data Scientist", "Orbit Labs", "London, UK"), card("3966666666", "AI Engineer", "Delta Works", "London, UK"))
    mails = {"m1": (ALERT_FROM, "2 new jobs for data scientist", cards, NEWER),                      # the card is read in the same run, before the sent mail by time
             "m2": (REC_FROM, "Your application was sent to Orbit Labs", sent, NEWER - timedelta(days=1))}
    d = run(signed_in, db, label_net(mails))
    applied = db.conn.execute("SELECT company, title, status, channel, linkedin_job_id FROM linkedin_applied").fetchall()
    assert [tuple(r) for r in applied] == [("Orbit Labs", "Data Scientist", "applied", "LinkedIn", "3944444444")]
    assert db.jobs() == [] and not list((signed_in.path("jds") / "inbox").glob("*.txt"))                # no job was created
    assert results(d)[("Orbit Labs", "Data Scientist")] == "already applied (LinkedIn)"
    assert results(d)[("Delta Works", "AI Engineer")] == "needs_jd"
    # a later run, a later email: still known
    later = {"m3": (ALERT_FROM, "1 new job", email_html(card("3977777777", "Data Scientist (m/f/d)", "Orbit Labs Ltd", "London, UK")), TODAY)}
    d2 = run(signed_in, db, label_net(later))
    assert results(d2) == {("Orbit Labs Ltd", "Data Scientist (m/f/d)"): "already applied (LinkedIn)"}
    assert [r["linkedin_job_id"] for r in db.conn.execute("SELECT * FROM linkedin_applied")] == ["3944444444"]


def test_the_pipeline_knows_the_linkedin_application_from_the_next_run_on(signed_in, db):
    run(signed_in, db, label_net({"m1": (REC_FROM, "Your application was sent to Orbit Labs",
                                         email_html(card("3944444444", "Data Scientist", "", "")), NEWER)}))
    assert make(signed_in, db, Net()).known.where("Orbit Labs", "Data Scientist", "London") == "already applied (LinkedIn)"


def test_application_update_changes_the_status_only(signed_in, db):
    sent = email_html(card("3944444444", "Data Scientist", "", ""))
    mails = {"m1": (REC_FROM, "Your application was sent to Orbit Labs", sent, NEWER - timedelta(days=2)),
             "m2": (REC_FROM, "Orbit Labs viewed your application", email_html(), NEWER),
             "m3": (REC_FROM, "Your application to Data Scientist at Unknown Co was not selected", email_html(), NEWER)}
    d = run(signed_in, db, label_net(mails))
    assert [tuple(r) for r in db.conn.execute("SELECT company, status FROM linkedin_applied")] == [("Orbit Labs", "viewed")]
    c = d.run.sources["gmail_label"]
    assert c["status_updated"] == 1 and c["status_update_unmatched"] == 1 and c["applied_recorded"] == 1          # an update never invents a record
    assert db.jobs() == []


def test_other_emails_are_counted_and_logged_never_parsed_as_jobs(signed_in, db):
    mails = {"m1": ("Priya <messaging-digest-noreply@linkedin.com>", "Priya sent you a message",
                    email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK")), NEWER)}
    d = run(signed_in, db, label_net(mails))
    assert d.label_rows == [] and d.label_kinds == {ll.OTHER: 1} and any("Priya sent you a message" in n for n in d.run.notes)


# ------------------------------------------------------------------ 5. dedupe
def test_the_same_job_id_in_two_emails_is_one_job(signed_in, db):
    one = email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK"))
    mails = {"m1": (ALERT_FROM, "1 new job", one, NEWER - timedelta(days=20)), "m2": (REC_FROM, "Jobs you may be interested in", one, NEWER)}
    d = run(signed_in, db, label_net(mails))
    assert sorted(r["result"] for r in d.label_rows) == ["duplicate of 3911111111", "needs_jd"]
    assert [r["received"] for r in d.label_rows if r["result"] == "needs_jd"] == [(NEWER - timedelta(days=20)).isoformat(timespec="seconds")]   # the first sighting is the job


def test_the_same_company_and_title_within_14_days_is_one_job_but_not_after_that(signed_in, db):
    a = email_html(card("3911111111", "Data Scientist", "Orbit Labs Ltd", "London, UK"))
    b = email_html(card("3922222222", "Data Scientist", "ORBIT LABS", "London, UK"))
    close = run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", a, NEWER - timedelta(days=5)), "m2": (ALERT_FROM, "1 new job", b, NEWER)}))
    assert sorted(r["result"] for r in close.label_rows) == ["duplicate of 3911111111", "needs_jd"]


def test_the_same_company_and_title_more_than_14_days_apart_is_two_jobs(signed_in, db):
    a = email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK"))
    b = email_html(card("3922222222", "Data Scientist", "Orbit Labs", "London, UK"))
    far = run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", a, NEWER - timedelta(days=25)), "m2": (ALERT_FROM, "1 new job", b, NEWER)}))
    assert [r["result"] for r in far.label_rows] == ["needs_jd", "needs_jd"]


def test_a_job_already_in_the_database_is_a_duplicate_of_it(signed_in, db):
    from careerops.discovery import manual
    manual.add_jd(db, "x" * 600, company="Orbit Labs", title="Data Scientist", url="https://orbit.example/1", location="London")
    d = run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK")), NEWER)}))
    assert results(d) == {("Orbit Labs", "Data Scientist"): "duplicate of already in the database"}


# ------------------------------------------------------------------ 6. resolution through the employer's board
def test_a_matching_greenhouse_posting_gives_the_full_ad_from_the_board(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Acme Analytics Ltd", "London, England, United Kingdom"))
    net = label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER)},
                    extra={"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = run(signed_in, db, net)
    assert results(d) == {("Acme Analytics Ltd", "Data Scientist"): "resolved: greenhouse/acme"}
    (path, ) = list((signed_in.path("jds") / "inbox").glob("*.txt"))
    text = path.read_text(encoding="utf-8")
    assert "Source: greenhouse" in text and "Apply kind: greenhouse" in text            # the ad is the board's, not the email's
    assert "LinkedIn job id: 3912345678" in text and "Found via: linkedin_email" in text and "Posted via: gmail_label" in text
    assert "linkedin.com" not in text.split("\n\n", 1)[0].replace("Found via", "")
    assert db.conn.execute("SELECT job_file, result FROM linkedin_cards").fetchone()[:] == (path.name, "resolved: greenhouse/acme")


def test_a_card_with_no_matching_posting_goes_to_needs_jd_with_its_link(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, England, United Kingdom"))
    d = run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER)}))
    assert results(d) == {("Orbit Labs", "Data Scientist"): "needs_jd"}
    d.finish()
    queue = (signed_in.path("jds") / "manual_queue.csv").read_text(encoding="utf-8")
    assert "linkedin_email" in queue and "linkedin.com/jobs/view/3912345678" in queue


def test_an_easy_apply_card_is_always_needs_jd_even_when_the_board_has_the_job(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Acme Analytics Ltd", "London, England, United Kingdom", "Easy Apply"))
    net = label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER)},
                    extra={"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    d = run(signed_in, db, net)
    assert results(d) == {("Acme Analytics Ltd", "Data Scientist"): "needs_jd"}
    assert not list((signed_in.path("jds") / "inbox").glob("*.txt")) and not any("boards/acme" in u for u in net.urls())      # the board was not even asked


def test_a_title_out_of_the_role_families_is_filtered_before_anything_is_asked(signed_in, db):
    html = email_html(card("3888888888", "Retail Store Manager", "Shopco", "Leeds, England, United Kingdom"))
    net = label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER)})
    d = run(signed_in, db, net)
    (res,) = results(d).values()
    assert res.startswith("filtered: role") and not any("greenhouse" in u for u in net.urls())


def test_token_set_ratio_ignores_word_order_and_repeats():
    assert ll.token_set_ratio("Machine Learning Engineer", "Engineer, Machine Learning") == 1.0
    assert ll.token_set_ratio("Data Scientist", "Data Scientist (m/f/d)") >= 0.85
    assert ll.token_set_ratio("Data Scientist", "Marketing Manager") < 0.5


# ------------------------------------------------------------------ 7. never linkedin.com
def test_no_request_to_linkedin_is_ever_made_over_the_whole_discover_path(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Acme Analytics Ltd", "London, England, United Kingdom"),
                      card("3999999999", "AI Engineer", "Orbit Labs", "Bristol, England, United Kingdom"),
                      card("3888888888", "Machine Learning Engineer", "Kite Data", "Remote", "Easy Apply"))
    mails = {"m1": (ALERT_FROM, "3 new jobs", html, NEWER),
             "m2": (REC_FROM, "Your application was sent to Delta Works", email_html(card("3944444444", "Applied Scientist", "", "")), NEWER),
             "m3": (REC_FROM, "Jobs you may be interested in", html, NEWER - timedelta(days=1))}
    net = label_net(mails, extra={"boards/acme/jobs": fixture_json("gh_jobs_acme.json"), "boards/acme": fixture_json("gh_board_acme.json")})
    run(signed_in, db, net)
    d2 = run(signed_in, db, net)                                                     # and again: the second run reads nothing new
    assert d2.label_rows == []
    assert not any((urlparse(r["url"]).hostname or "").endswith("linkedin.com") for r in net.requests)
    assert {urlparse(r["url"]).hostname for r in net.requests} <= {"gmail.googleapis.com", "boards-api.greenhouse.io"}


# ------------------------------------------------------------------ 8. messages are read once
def test_a_message_already_processed_is_not_fetched_again(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, UK"))
    net = label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER), "m2": (ALERT_FROM, "1 new job", email_html(), NEWER)})
    run(signed_in, db, net)
    first = [u for u in net.urls() if "/messages/m" in u]
    assert sorted(first) == [u for u in sorted(first)] and len(first) == 2
    assert db.conn.execute("SELECT COUNT(*) FROM gmail_label_messages").fetchone()[0] == 2
    d2 = run(signed_in, db, net)
    assert len([u for u in net.urls() if "/messages/m" in u]) == 2 and d2.run.sources["gmail_label"]["message_already_read"] == 2
    d3 = make(signed_in, db, net)                                                # a new run object, the same database: still nothing fetched
    d3.from_gmail_label(["LinkedIn"], "30d")
    assert len([u for u in net.urls() if "/messages/m" in u]) == 2


def test_a_dry_run_writes_nothing_and_remembers_nothing(signed_in, db):
    html = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, UK"))
    d = make(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", html, NEWER)}), dry_run=True)
    d.from_gmail_label(["LinkedIn"], "30d")
    assert len(d.label_rows) == 1
    assert all(db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in ("gmail_label_messages", "linkedin_cards", "linkedin_applied"))
    assert not (signed_in.path("samples") / "linkedin").exists()


# ------------------------------------------------------------------ samples, report, queue, CLI
def test_samples_are_sanitised_and_git_ignored(signed_in, db):
    first, last = ll.user_names(signed_in)[:2]                          # the fictional applicant of tests/fixtures/profile, never the real one
    body = (f'<p>Hi {first} {last}, jobs for you. Mail me at thiru.a@example.org or call +44 7700 900123.</p>'
            + email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, UK")))
    run(signed_in, db, label_net({"m1": (ALERT_FROM, f"{first}, 1 new job", body, NEWER)}))
    path = ll.sample_path(signed_in, ll.JOB_ALERT)
    text = path.read_text(encoding="utf-8")
    assert first not in text and last not in text and "thiru.a@example.org" not in text and "7700" not in text and "trackingId" not in text
    assert "<name>" in text and "<email>" in text and "<phone>" in text and "Data Scientist" in text
    rel = "data/samples/linkedin/job_alert.txt"
    assert subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT).returncode == 0


def test_the_report_is_one_table_newest_first_with_counts(signed_in, db):
    mails = {"m1": (ALERT_FROM, "2 new jobs", email_html(card("3911111111", "Data Scientist", "Orbit Labs", "London, UK"),
                                                       card("3922222222", "AI Engineer", "Delta Works", "London, UK")), NEWER),
             "m2": (ALERT_FROM, "1 new job", email_html(card("3933333333", "Applied Scientist", "Kite Data", "London, UK")), NEWER - timedelta(days=9))}
    d = run(signed_in, db, label_net(mails))
    out = "\n".join(cli._label_report(d))
    lines = out.splitlines()
    head = next(l for l in lines if l.startswith("received"))
    assert [c.strip() for c in head.split("|")] == ["received", "title", "company", "location", "sponsor", "result"]
    body = [l for l in lines if "needs_jd" in l and "|" in l]
    assert len(body) == 3 and "Kite Data" in body[-1]                                # the 9-day-old card is last
    assert "emails by type: job_alert 2" in out and "needs_jd 3" in out and "cards by age: <= 24 h 2, <= 72 h 0, <= 7 d 0, older 1" in out


def test_queue_source_lists_the_needs_jd_cards_with_their_links(signed_in, db, capsys):
    html = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, UK"), card("3999999999", "AI Engineer", "Kite Data", "London, UK", "Easy Apply"))
    run(signed_in, db, label_net({"m1": (ALERT_FROM, "2 new jobs", html, NEWER)}))
    cli._queue_source(signed_in, db, argparse.Namespace(json=False))
    out = capsys.readouterr().out
    assert "2 card(s)" in out and "https://www.linkedin.com/jobs/view/3912345678" in out and "https://www.linkedin.com/jobs/view/3999999999" in out
    assert "[Easy Apply]" in out and "careerops add-jd" in out
    cli._queue_source(signed_in, db, argparse.Namespace(json=True))
    assert {r["linkedin_job_id"] for r in json.loads(capsys.readouterr().out)} == {"3912345678", "3999999999"}


def test_the_command_line_takes_the_flag_with_and_without_a_name():
    seen = []
    orig = cli._discover_inbox
    cli._discover_inbox = lambda args, asked: seen.append((args.gmail_label, args.since, list(asked)))
    try:
        cli.main(["discover", "--gmail-label"])
        cli.main(["discover", "--gmail-label", "LinkedIn", "--since", "7d"])
        cli.main(["discover", "--gmail-label", "--report"])
    finally:
        cli._discover_inbox = orig
    assert seen == [("*", None, ["gmail_label"]), ("LinkedIn", "7d", ["gmail_label"]), ("*", None, ["gmail_label"])]


def test_the_twice_daily_script_includes_the_label():
    assert "--gmail-label" in (ROOT / "scripts" / "discover.sh").read_text(encoding="utf-8")


def test_since_is_read_as_hours_days_or_weeks():
    assert ll.parse_since("7d") == timedelta(days=7) and ll.parse_since("12h") == timedelta(hours=12) and ll.parse_since(None) == timedelta(days=30)
    with pytest.raises(ValueError):
        ll.parse_since("soon")
