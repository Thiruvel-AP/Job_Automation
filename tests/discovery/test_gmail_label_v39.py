"""v3.9: a label email is marked read only AFTER it was classified and parsed. A job email with 0 cards, or one whose parse raised, stays unread
(logged as `unparsed`) and is fetched again; `--reread` ignores the marks; the one-time repair clears the marks written before v3.9 that had no
stored type; a re-read card is updated, not added twice. All mail is made by hand, no network."""
import pytest

from careerops import cli
from careerops.discovery import gmail
from careerops.discovery import linkedin_label as ll
from test_gmail_label_v38 import ALERT_FROM, NEWER, card, email_html, label_net, run
from test_discover import cfg, make, signed_in            # noqa: F401  (fixtures)

GOOD = email_html(card("3912345678", "Data Scientist", "Orbit Labs", "London, UK"))
EMPTY = email_html()


def fetches(net):
    return [u for u in net.urls() if "/messages/m" in u]


def rows(db):
    return {r["message_id"]: dict(r) for r in db.conn.execute("SELECT * FROM gmail_label_messages")}


def test_a_message_is_stored_with_type_cards_and_parse_time_after_parsing(signed_in, db):
    run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER)}))
    r = rows(db)["m1"]
    assert r["kind"] == ll.JOB_ALERT and r["cards_parsed"] == 1 and r["label"] == "LinkedIn" and r["parsed_at"]


def test_a_job_alert_with_no_cards_stays_unread_is_retried_and_logged_as_unparsed(signed_in, db):
    net = label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER), "m2": (ALERT_FROM, "3 new jobs", EMPTY, NEWER)})
    d1 = run(signed_in, db, net)
    assert d1.run.sources["gmail_label"]["unparsed_mails"] == 1 and d1.label_unparsed_mails == [(ALERT_FROM, "3 new jobs")]
    assert rows(db)["m2"]["cards_parsed"] == 0
    d2 = run(signed_in, db, net)
    assert fetches(net).count("https://gmail.googleapis.com/gmail/v1/users/me/messages/m2") == 2      # m2 again, m1 not
    assert fetches(net).count("https://gmail.googleapis.com/gmail/v1/users/me/messages/m1") == 1
    assert d2.run.sources["gmail_label"]["message_already_read"] == 1 and d2.run.sources["gmail_label"]["unparsed_mails"] == 1


def test_a_parse_that_raises_leaves_the_message_unread_and_the_others_are_still_read(signed_in, db, monkeypatch):
    real = gmail.parse_alert

    def parse(kind, html, text="", mid="", **kw):
        if mid == "m1":
            raise ValueError("layout changed")
        return real(kind, html, text, mid, **kw)
    monkeypatch.setattr(gmail, "parse_alert", parse)
    net = label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER), "m2": (ALERT_FROM, "1 new job", GOOD.replace("3912345678", "3911111111"), NEWER)})
    d = run(signed_in, db, net)
    assert "m1" not in rows(db) and rows(db)["m2"]["cards_parsed"] == 1
    assert d.run.sources["gmail_label"]["unparsed_mails"] == 1 and any("layout changed" in n for n in d.run.notes)


def test_reread_ignores_the_read_marks_and_does_not_duplicate_cards(signed_in, db):
    net = label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER)})
    run(signed_in, db, net)
    assert db.conn.execute("SELECT COUNT(*) FROM linkedin_cards").fetchone()[0] == 1
    d = make(signed_in, db, net)
    d.from_gmail_label(["LinkedIn"], "30d", reread=True)
    assert len(fetches(net)) == 2 and d.run.sources["gmail_label"]["message_reread"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM linkedin_cards").fetchone()[0] == 1               # updated in place
    assert [r["result"] for r in d.label_rows] and not any(r["result"].startswith("duplicate") for r in d.label_rows)


def test_the_repair_clears_marks_without_a_stored_type_once(signed_in, db):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db.conn:
        db.conn.execute("INSERT INTO gmail_label_messages (message_id, label, kind, received_at, subject, processed_at) VALUES ('m1','LinkedIn','','', 's', ?)", (now,))
        db.conn.execute("INSERT INTO gmail_label_messages (message_id, label, kind, received_at, subject, processed_at) VALUES ('m2','LinkedIn',NULL,'', 's', ?)", (now,))
        db.conn.execute("INSERT INTO gmail_label_messages (message_id, label, kind, received_at, subject, processed_at) VALUES ('m3','LinkedIn','job_alert','', 's', ?)", (now,))
    net = label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER), "m2": (ALERT_FROM, "1 new job", GOOD, NEWER), "m3": (ALERT_FROM, "x", EMPTY, NEWER)})
    d = run(signed_in, db, net)
    assert d.label_repaired == 3                                      # a card email with no stored cards counts as unparsed too
    assert len(fetches(net)) == 3 and rows(db)["m1"]["kind"] == ll.JOB_ALERT
    assert run(signed_in, db, net).label_repaired == 0


def test_a_pre_v39_mark_that_holds_cards_stays_read_and_gets_its_count(signed_in, db):
    from datetime import datetime, timezone
    run(signed_in, db, label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER)}))
    with db.conn:
        db.conn.execute("UPDATE gmail_label_messages SET cards_parsed = NULL, parsed_at = NULL")
    net = label_net({"m1": (ALERT_FROM, "1 new job", GOOD, NEWER)})
    d = run(signed_in, db, net)
    assert d.label_repaired == 0 and fetches(net) == [] and rows(db)["m1"]["cards_parsed"] == 1


def test_with_no_cards_the_report_shows_five_example_senders_and_subjects(signed_in, db, capsys):
    mails = {f"m{i}": (ALERT_FROM, f"subject {i}", EMPTY, NEWER) for i in range(7)}
    d = run(signed_in, db, label_net(mails))
    out = "\n".join(cli._label_report(d))
    assert "no new job cards" in out and "the first emails of the label (sender | subject):" in out
    assert out.split("emails by type")[0].count("jobalerts-noreply@linkedin.com> | subject") == 5 and "unparsed mails" in out
