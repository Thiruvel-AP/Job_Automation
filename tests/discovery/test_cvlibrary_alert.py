"""CV-Library "Instant Job Alert" mails (the first live run read 0 jobs from 19 of them). A real alert, saved raw with the personal data removed
(tests/fixtures/discovery/cvlibrary_alert_2026-10-21.html), shows why: every link in it is a click-tracking redirect on clicks.cv-library.co.uk,
none is a /job/<id> page. The parser now reads the job from its place in the mail (title link, then company, place, salary, "Apply now"). Nothing
here, or in the parser, fetches a CV-Library address."""
from pathlib import Path

from test_gmail_sources import cfg, make, mail_net  # noqa: F401  (fixtures and helpers of the alert tests)

from careerops.discovery import gmail
from conftest import FIX

ALERT = (Path(__file__).resolve().parents[1] / "fixtures" / "discovery" / "cvlibrary_alert_2026-10-21.html").read_text(encoding="utf-8")
SENDER = "CV-Library <Admin@careers.cv-library.co.uk>"
TRACK = "https://clicks.cv-library.co.uk/f/a/"


def test_the_real_alert_yields_its_job_with_title_company_place_and_link():
    items = gmail.parse_alert("cvlibrary", ALERT, "", "m1")
    assert len(items) >= 1
    (job,) = items
    assert (job.title, job.company, job.location) == ("Fully remote cosmetic clinic group call handler", "Frontier Consulting",
                                                     "City of London, Greater London")
    assert job.link.startswith(TRACK) and job.kind == "cvlibrary" and job.message_id == "m1"


def test_the_headers_buttons_and_footer_of_the_mail_are_not_jobs():
    titles = [i.title for i in gmail.parse_alert("cvlibrary", ALERT)]
    assert not [t for t in titles if gmail.NOISE.match(t)]
    assert "Instant Job Alert" not in titles and "Apply now" not in titles and "Manage your preferences" not in titles


def test_a_digest_with_several_jobs_gives_each_of_them_once():
    def block(n, title, company, place):
        return (f'<table><tr><td><a href="{TRACK}J{n}~~/A~/B{n}~~">{title}</a></td></tr>'
                f"<tr><td>{company}</td></tr><tr><td>{place}</td></tr><tr><td>£40,000 per annum</td></tr>"
                f'<tr><td><a href="{TRACK}APPLY{n}~~/A~/B~~">Apply now</a></td></tr></table>')
    html = ('<html><body><a href="https://www.cv-library.co.uk/">Instant Job Alert</a>'
            + block(1, "Data Scientist", "Acme Analytics", "Bristol") + block(2, "Machine Learning Engineer", "Northgate Labs", "Leeds")
            + f'<a href="{TRACK}PREF~~/A~/B~~">Manage your preferences</a><p>to modify or deactivate Job Alerts.</p>'
            + f'<a href="{TRACK}UNS~~/A~/B~~">unsubscribe here.</a></body></html>')
    items = gmail.parse_alert("cvlibrary", html)
    assert [(i.title, i.company, i.location) for i in items] == [("Data Scientist", "Acme Analytics", "Bristol"),
                                                                 ("Machine Learning Engineer", "Northgate Labs", "Leeds")]


def test_a_tracking_link_with_no_company_or_place_after_it_is_not_taken_for_a_job():
    html = f'<html><body><a href="{TRACK}X~~/A~/B~~">See what is new on CV-Library</a><p>Thanks for reading.</p></body></html>'
    assert gmail.parse_alert("cvlibrary", html) == []


def test_the_other_cv_library_link_forms_still_read_and_a_look_alike_host_does_not():
    assert gmail.job_link("cvlibrary", "https://www.cv-library.co.uk/job/219034011/data-scientist?x=1") == "https://www.cv-library.co.uk/job/219034011"
    assert gmail.job_link("cvlibrary", "https://clicks.cv-library.co.uk.evil.example.com/f/a/abc~~/A~/B~~") is None
    assert gmail.job_link("cvlibrary", "https://example.com/f/a/abc~~/A~/B~~") is None
    assert gmail.job_link("cvlibrary", "https://clicks.cv-library.co.uk/not-a-tracking-path") is None


def test_discover_reads_a_job_from_the_alert_and_never_fetches_cv_library(cfg, db):
    net = mail_net({"cv1": (SENDER, "Instant Job Alert: Fully remote cosmetic clinic group call handler", ALERT)})
    d = make(cfg, db, net)
    d.from_gmail()
    assert d.run.by_source["cv-library"]["mails"] == 1 and d.run.by_source["cv-library"]["parsed"] == 1
    assert d.run.sources["gmail"]["alerts_cvlibrary"] == 1
    assert not [u for u in net.urls(True) if "cv-library" in u]
