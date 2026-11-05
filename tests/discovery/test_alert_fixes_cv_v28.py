"""v2.8, CV-Library alerts: the run report shows parsed alerts (company, title, link) with the reason each stayed unresolved; a link on a CV-Library job page is
marked `agency or board-only`, a click-tracker link is said to name no job page, and nothing is fetched from CV-Library. No network: `Net` answers."""
from test_cvlibrary_alert import SENDER, TRACK
from test_gmail_sources import cfg, make, mail_net  # noqa: F401  (a settings copy with the source registry and a signed-in Gmail)

from careerops.discovery import discover as disc

PAGE = "https://www.cv-library.co.uk/job/219034011"


def block(href, title, company, place):
    return (f'<table><tr><td><a href="{href}">{title}</a></td></tr><tr><td>{company}</td></tr><tr><td>{place}</td></tr><tr><td>£40,000 per annum</td></tr></table>')


def read_mail(cfg, db, *blocks, subject="Instant Job Alert: Data Scientist"):
    html = "<html><body><a href=\"https://www.cv-library.co.uk/\">Instant Job Alert</a>" + "".join(blocks) + "</body></html>"
    net = mail_net({"cv1": (SENDER, subject, html)})
    d = make(cfg, db, net)
    d.from_gmail()
    return d, net


def test_the_report_shows_the_parsed_alerts_and_why_each_stayed_unresolved(cfg, db):
    d, net = read_mail(cfg, db, block(f"{TRACK}J1~~/A~/B1~~", "Data Scientist", "Northgate Labs", "Leeds"),
                       block(f"{PAGE}/machine-learning-engineer", "Machine Learning Engineer", "Zeta Works", "Bristol"))
    lines = disc.cvlibrary_lines(d.run.as_json())
    assert lines[0] == "CV-Library: 1 alert mail(s) read, 2 alert(s) parsed, 0 resolved, 2 unresolved"
    text = "\n".join(lines)
    assert "2 parsed alert(s) that stayed unresolved, with the reason:" in text
    assert "1. Northgate Labs | Data Scientist | https://clicks.cv-library.co.uk/f/a/J1~~/A~/B1~~ [click-tracker link]" in text
    assert f"2. Zeta Works | Machine Learning Engineer | {PAGE} [agency or board-only]" in text
    assert "failed: no public job board known for Northgate Labs" in text and "failed: no public job board known for Zeta Works" in text
    assert "note: a CV-Library job page: agency or board-only (the employer's own board was not found; CV-Library pages are never fetched)" in text
    assert "note: a CV-Library click-tracker link: it names no job page (open the alert email to see the job); never fetched" in text
    assert "     - board_link: clicks.cv-library.co.uk is never fetched by this tool, and the link names no employer board" in text
    assert not [u for u in net.urls(True) if "cv-library" in u]


def test_at_most_five_are_shown_and_the_head_counts_all_of_them(cfg, db):
    d, _ = read_mail(cfg, db, *(block(f"{TRACK}J{n}~~/A~/B{n}~~", "Data Scientist", f"Company {n}", "Leeds") for n in range(1, 8)))
    lines = disc.cvlibrary_lines(d.run.as_json())
    assert lines[0] == "CV-Library: 1 alert mail(s) read, 7 alert(s) parsed, 0 resolved, 7 unresolved"
    assert len([line for line in lines if line[:5].strip().rstrip(".").isdigit()]) == 5


def test_what_never_reached_the_resolver_is_counted_beside_them(cfg, db):
    d, _ = read_mail(cfg, db, block(f"{TRACK}J1~~/A~/B1~~", "Data Scientist", "Northgate Labs", "Leeds"),
                     block(f"{TRACK}J2~~/A~/B2~~", "Call Handler", "Frontier Consulting", "London"))
    head = disc.cvlibrary_lines(d.run.as_json())[0]
    assert head == "CV-Library: 1 alert mail(s) read, 2 alert(s) parsed, 0 resolved, 1 unresolved, role filtered 1"


def test_a_mail_with_no_readable_job_is_said(cfg, db):
    d, _ = read_mail(cfg, db, "<p>Your weekly summary</p>", subject="Your CV-Library job alert")
    (head,) = disc.cvlibrary_lines(d.run.as_json())
    assert head.startswith("CV-Library: 1 alert mail(s) read, 0 alert(s) parsed, 0 resolved, 0 unresolved; no job could be read from them (first subject: Your CV-Library job alert)")


def test_nothing_is_printed_for_a_run_that_saw_no_cv_library(cfg, db):
    assert disc.cvlibrary_lines({"sources": {}, "by_source": {"linkedin": {"mails": 2, "parsed": 5}}, "unresolved": []}) == []


def test_the_saved_report_has_the_cv_library_lines(cfg, db):
    d, _ = read_mail(cfg, db, block(f"{PAGE}/x", "Data Scientist", "Zeta Works", "Bristol"))
    disc.save_run(cfg, d.run)
    out = disc.format_report(cfg)
    assert "CV-Library: 1 alert mail(s) read, 1 alert(s) parsed, 0 resolved, 1 unresolved" in out and "[agency or board-only]" in out
