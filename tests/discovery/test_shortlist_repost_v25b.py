"""Fix round after v2.5: a "possible repost of a job you skipped" is never approved by [a]; it needs its number typed. The line says the real skip date (the
decision log, the database, or the time the JD was filed in jds_done/skipped/), never `?`."""
import os
import re
from datetime import datetime

from test_shortlist import build, fb, inbox, roles, run_with  # noqa: F401  (fb, inbox, roles are fixtures)
from triage_fakes import write_ad

from careerops.apply import decided, decisions
from careerops.apply import listing_keys as LK
from careerops.discovery import shortlist

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def skipped_in_db(db, jid="manual:g1"):
    db.upsert_job({"id": jid, "source": "manual", "company": "Gone", "title": "Data Scientist", "description": "x"})
    db.update_job(jid, status="skipped")


def number_of(plan, company):
    return next(n for n, r in enumerate(plan.rows, 1) if r.job["company"] == company)


def two_ads(inbox):
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    write_ad(inbox, "fresh.txt", company="Fresh", age_days=1)


def test_a_does_not_approve_a_possible_repost_but_approves_the_rest(settings, db, fb, inbox):
    skipped_in_db(db)
    two_ads(inbox)
    plan, out, seen = run_with(settings, db, fb, "a", "q")
    approved = sorted(p.name for p in (settings.path("jds") / "approved" / "2026-10-27").iterdir())
    assert approved == ["fresh.txt"] and (inbox / "again.txt").exists()                  # Fresh went; the repost stayed in the inbox, still undecided
    text = "\n".join(out)
    assert "[a] did not approve these" in text and "Gone | Data Scientist: possible repost of a job you skipped on" in text
    assert "[a] approve all (not the 1 possible repost(s): type their numbers)" in seen[0]


def test_its_number_approves_it_explicitly(settings, db, fb, inbox):
    skipped_in_db(db)
    two_ads(inbox)
    n = number_of(build(settings, db, fb), "Gone")
    run_with(settings, db, fb, "a", str(n))
    approved = sorted(p.name for p in (settings.path("jds") / "approved" / "2026-10-27").iterdir())
    assert approved == ["again.txt", "fresh.txt"]


def test_a_alone_with_only_a_repost_left_says_so_and_approves_nothing(settings, db, fb, inbox):
    skipped_in_db(db)
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    _, out, _ = run_with(settings, db, fb, "a", "q")
    assert (inbox / "again.txt").exists() and not (settings.path("jds") / "approved").exists()
    assert "nothing to approve: only possible reposts are left" in "\n".join(out)


def test_without_a_repost_a_is_unchanged(settings, db, fb, inbox):
    two_ads(inbox)
    _, out, seen = run_with(settings, db, fb, "a")
    assert len(list((settings.path("jds") / "approved" / "2026-10-27").iterdir())) == 2
    assert "possible repost" not in "\n".join(out) and seen[0].startswith("[a] approve all · [1 3 5]")


def test_x_still_skips_the_reposts_left_in_the_list(settings, db, fb, inbox):
    skipped_in_db(db)
    two_ads(inbox)
    run_with(settings, db, fb, "a", "x")
    assert not (inbox / "again.txt").exists() and (settings.path("jds_done") / "skipped" / "again.txt").exists()


# ------------------------------------------------------------------ the date
def test_the_repost_of_a_row_skipped_in_the_database_carries_its_date(settings, db, fb, inbox):
    skipped_in_db(db)
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    row = build(settings, db, fb).rows[0]
    assert DATE.search(row.repost) and "?" not in row.repost and "(manual:g1)" in row.repost


def test_the_repost_of_an_ad_you_skipped_at_the_shortlist_carries_the_day_it_was_filed(settings, db, fb, inbox):
    old = write_ad(settings.path("jds_done") / "skipped", "old.txt", company="Gone", url="https://example.com/jobs/1")
    reason = old.with_name("old.reason.txt")
    reason.write_text(shortlist.SKIP_REASON + "\n", encoding="utf-8")
    stamp = datetime(2026, 10, 20, 12, 0).timestamp()
    os.utime(reason, (stamp, stamp))
    write_ad(inbox, "again.txt", company="Gone", age_days=1, url="https://example.com/jobs/2")        # same company and title, another address
    row = build(settings, db, fb).rows[0]
    assert "possible repost of a job you skipped on 2026-10-20" in row.repost and "?" not in row.repost
    assert "[possible repost of a job you skipped on 2026-10-20" in row.tags()


def test_the_decision_log_date_is_used_when_the_log_has_the_job(settings, db, fb, inbox):
    old = LK.keys_for("Gone", "Data Scientist", "https://example.com/jobs/1", None, "London, UK", None)
    decisions.append(settings, old, decisions.SKIPPED, "skipped in review", job_id="manual:old1", ts="2026-09-14T09:30:00")
    write_ad(inbox, "again.txt", company="Gone", age_days=1, url="https://example.com/jobs/2")
    row = build(settings, db, fb).rows[0]
    assert row.repost == "possible repost of a job you skipped on 2026-09-14 (manual:old1)"


def test_a_skip_without_a_recorded_date_says_so_instead_of_a_question_mark():
    assert decisions.repost_label(False, "", "") == "possible repost of a job you skipped (date not recorded)"
    assert decisions.repost_label(True, "2026-10-01", "manual:x") == "possible repost of a job you applied to on 2026-10-01 (manual:x)"
    assert "?" not in decisions.repost_label(False, "", "") + decisions.repost_label(True, "", "manual:x")


def test_a_row_with_no_update_time_takes_the_time_of_its_newest_event(settings, db):
    skipped_in_db(db)
    db.conn.execute("UPDATE jobs SET updated_at = NULL WHERE id = 'manual:g1'")
    db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", ("manual:g1", "2026-10-02T08:00:00", "live", "skipped in review"))
    db.conn.commit()
    assert decided.decided_on(db, db.job("manual:g1")) == "2026-10-02T08:00:00"
