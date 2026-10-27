"""D2 `careerops followups` (careerops/reporting/followups.py): the applications whose follow-up day has come with no reply, oldest first, with the
contact when one is known. It only reads the database; drafting a follow-up stays in outreach.py."""
from datetime import date

import pytest
import yaml

from careerops import cli
from careerops.reporting import followups, outcomes
from outreach_fakes import make_applied

TODAY = date(2026, 10, 27)


def applied(settings, db, tmp_path, n, company, title, on, **kw):
    job = {"id": f"greenhouse:{company.lower()}:{n}", "source": "greenhouse", "company": company, "title": title, "location": "London, UK",
           "url": f"https://careers.{company.lower()}.com/jobs/{n}", "description": "Python."}
    return make_applied(settings, db, job, tmp_path, applied_at=f"{on}T09:00:00+00:00", **kw)


def person(db, job_id, name, role="", email="", source="jd"):
    db.conn.execute("INSERT INTO contacts (job_id, name, role, email, source, created_at) VALUES (?,?,?,?,?,?)",
                    (job_id, name, role, email or None, source, "2026-10-01T00:00:00+00:00"))
    db.conn.commit()


def ids(rows):
    return [r.job_id for r in rows]


def test_an_application_is_due_seven_days_after_the_day_it_was_sent(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-10")
    [row] = followups.due(db, settings, TODAY)
    assert row.job_id == a["id"] and row.due == date(2026, 10, 17) and row.days_over == 10 and row.applied_on == date(2026, 10, 10)


def test_the_number_of_days_follows_the_outreach_setting(settings, db, tmp_path):
    applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-22")
    assert followups.due(db, settings, TODAY) == []                                  # 22 Oct + 7 days = 29 Oct
    settings.raw["outreach"]["follow_up_days"] = 14
    assert followups.due(db, settings, TODAY) == []
    settings.raw["outreach"]["follow_up_days"] = 3
    assert len(followups.due(db, settings, TODAY)) == 1


def test_the_day_it_falls_due_it_is_listed_and_the_day_before_it_is_not(settings, db, tmp_path):
    applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-20")
    assert [r.days_over for r in followups.due(db, settings, date(2026, 10, 27))] == [0]
    assert followups.due(db, settings, date(2026, 10, 26)) == []


def test_the_oldest_follow_up_comes_first(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-12")
    b = applied(settings, db, tmp_path, 2, "Bolt", "Data Scientist", "2026-10-01")
    c = applied(settings, db, tmp_path, 3, "Cargo", "Data Scientist", "2026-10-05")
    assert ids(followups.due(db, settings, TODAY)) == [b["id"], c["id"], a["id"]]


@pytest.mark.parametrize("event", ["acknowledged", "screen", "interview", "offer", "rejected", "withdrawn"])
def test_an_application_with_any_reply_or_a_withdrawal_is_not_listed(settings, db, tmp_path, event):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-01")
    outcomes.record(db, a["id"], event, "2026-10-15")
    assert followups.due(db, settings, TODAY) == []


def test_the_applied_event_alone_is_not_a_reply(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-01")
    outcomes.record(db, a["id"], "applied", "2026-10-01")
    assert ids(followups.due(db, settings, TODAY)) == [a["id"]]


def test_a_job_that_is_not_applied_and_one_backfilled_from_the_archive_are_not_listed(settings, db, tmp_path):
    applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-01")
    old = applied(settings, db, tmp_path, 2, "Bolt", "Data Scientist", "2025-01-01")
    outcomes.record(db, old["id"], "applied", "2025-01-01", source="archive")
    db.upsert_job({"id": "manual:x", "source": "manual", "company": "Cargo", "title": "Data Scientist", "description": "x"})
    assert [r.company for r in followups.due(db, settings, TODAY)] == ["Acme"]


def test_the_best_contact_is_shown_or_the_lack_of_one(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-01")
    b = applied(settings, db, tmp_path, 2, "Bolt", "Data Scientist", "2026-10-02")
    person(db, a["id"], "Jane Roe", "Recruiter", "jane@acme.com", source="jd")
    person(db, a["id"], "Sam Low", "Hiring manager", "sam@acme.com", source="user")                  # a contact you added ranks first
    rows = {r.company: r for r in followups.due(db, settings, TODAY)}
    assert rows["Acme"].contact == "Sam Low, Hiring manager <sam@acme.com>"
    assert rows["Bolt"].contact == ""
    text = "\n".join(followups.lines(list(rows.values()), TODAY))
    assert "Sam Low, Hiring manager <sam@acme.com>" in text and "no contact known" in text


def test_a_contact_without_an_address_is_shown_by_name(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-01")
    person(db, a["id"], "Jane Roe", "Recruiter")
    assert followups.due(db, settings, TODAY)[0].contact == "Jane Roe, Recruiter (no address yet)"


def test_the_lines_say_company_role_dates_and_how_late_and_point_at_outreach_for_the_draft(settings, db, tmp_path):
    a = applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2026-10-10")
    text = "\n".join(followups.lines(followups.due(db, settings, TODAY), TODAY))
    for part in ("Acme", "Data Scientist", a["id"], "applied 2026-10-10", "due 2026-10-17", "10 days over"):
        assert part in text
    assert f"careerops outreach draft {a['id']}" in text and "nothing is sent" in text.lower()


def test_nothing_due_says_so(settings):
    assert followups.lines([], TODAY) == ["No application is waiting for a follow-up."]


@pytest.fixture
def cfg(tmp_path, settings):
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return p


def test_the_command_prints_the_list_and_writes_nothing(cfg, settings, db, tmp_path, capsys):
    applied(settings, db, tmp_path, 1, "Acme", "Data Scientist", "2000-01-01")
    before = (db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
    cli.main(["--config", str(cfg), "followups"])
    out = capsys.readouterr().out
    assert "Acme" in out and "Data Scientist" in out
    after = (db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
    assert before == after


def test_the_command_runs_on_the_mock_root_and_finds_nothing(cfg, settings, tmp_path, capsys):
    settings.raw["paths"]["mock_root"] = str(tmp_path / "mock")              # never the real data/mock
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "followups", "--mock"])
    assert "No application is waiting" in capsys.readouterr().out
