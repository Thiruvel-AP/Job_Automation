"""Fix round 3 after v2.5: the decision log's backfill is applied for real, `why` finds an OLD id (one the database and the folders no longer hold) from the
decision log and the events table and prints its keys and source, discover checks every key against the inbox, approved/ and jds_done/ before it writes an
ad, and `careerops skip --url` exists. Real examples: Deliveroo manual:e68c252102ba (8202372, skipped 29 Oct), Deliveroo 8242432, OpenAI 7be58f3a,
Databricks gh_jid 8927390002 (all three written to the inbox again although they sat in approved/). No network, no model."""
import json
import re

import pytest
import yaml
from test_decided import GC_JD, d, fresh_run, names  # noqa: F401  (fixtures and helpers of the decided tests)
from test_skip_gaps_v25c import (NEW_TITLE, NEW_URL, OLD_URL, TITLE, approved_file, cfg, deliveroo, fb, inbox, roles, skipped_file)  # noqa: F401
from triage_fakes import write_ad

from careerops import cli
from careerops.apply import decided, decisions, targets
from careerops.apply import listing_keys as LK

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def lines(settings) -> int:
    p = decisions.path(settings)
    return len(p.read_text(encoding="utf-8").splitlines()) if p.is_file() else 0


# ------------------------------------------------------------------ 1: the backfill gives every past skip a dated line with all its keys
def test_backfill_writes_one_dated_line_per_old_skip_with_all_keys_and_a_second_run_adds_nothing(db, settings, cfg, capsys):
    f = skipped_file(settings, when=(2026, 10, 29, 16, 55))
    assert lines(settings) == 0
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    assert "1 line(s) written" in capsys.readouterr().out and lines(settings) == 1
    (rec,) = decisions.records(settings)
    assert rec["ts"].startswith("2026-10-29T16:55") and rec["decision"] == "skipped_user"                 # the real date, not today's
    jid = decided.file_job_id(f)
    assert rec["job_id"] == jid and rec["keys"][0] == f"id:{jid}"
    assert "ats:greenhouse:deliveroo:8202372" in rec["keys"] and LK.url_key(OLD_URL) in rec["keys"] and any(k.startswith("deliveroo|") for k in rec["keys"])
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    assert "0 line(s) written" in capsys.readouterr().out and lines(settings) == 1


# ------------------------------------------------------------------ 2: why finds an old id
def _file_with_source(settings, source="greenhouse"):
    p = skipped_file(settings, when=(2026, 10, 29, 16, 55))
    p.write_text(p.read_text(encoding="utf-8").replace("\nLocation:", f"\nSource: {source}\nLocation:", 1), encoding="utf-8")
    return p


def test_why_of_a_backfilled_old_id_prints_its_record_its_keys_and_its_source(db, settings, cfg, capsys):
    f = _file_with_source(settings)
    jid = decided.file_job_id(f)
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "why", jid])                                          # an id: no database row, not a file name, not a link
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "skipped_user" in x)
    assert DATE.match(line.strip()) and line.strip().startswith("2026-10-29T16:55") and "matched by: id id:" + jid in line
    assert "ats:greenhouse:deliveroo:8202372" in out and LK.url_key(OLD_URL) in out and "deliveroo|" in out          # its keys
    assert "source: jds_done/skipped/" in out and "Source 'greenhouse'" in out                                       # where it came from
    assert "now: decided: skipped" in out


def test_why_of_an_old_id_whose_file_is_gone_still_answers_from_the_log(db, settings, cfg, capsys):
    f = _file_with_source(settings)
    jid = decided.file_job_id(f)
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    f.unlink()                                                                           # (the test's own copy; nothing in code deletes)
    capsys.readouterr()
    cli.main(["--config", str(cfg), "why", jid])
    out = capsys.readouterr().out
    assert "skipped_user" in out and "ats:greenhouse:deliveroo:8202372" in out and "no job" not in out.lower().split("matched")[0]


def test_why_of_an_old_id_names_the_alert_it_came_from(db, settings, cfg, capsys):
    f = skipped_file(settings)
    jid = decided.file_job_id(f)
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    q = settings.path("jds") / "manual_queue.csv"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text("ts,source,company,title,location,link,reason,apply_kind\n"
                 f"2026-10-15T13:38:23+00:00,linkedin,Deliveroo,{TITLE},London,https://www.linkedin.com/jobs/view/1,no public board,\n", encoding="utf-8")
    capsys.readouterr()
    cli.main(["--config", str(cfg), "why", jid])
    assert "manual_queue.csv: alert from linkedin on 2026-10-15" in capsys.readouterr().out


def test_why_of_an_id_from_the_events_table_alone(db, settings, cfg, capsys):
    db.conn.execute("INSERT INTO events (job_id, ts, stage, detail) VALUES (?, ?, ?, ?)", ("manual:0123456789ab", "2026-10-21T10:00:00", "skip", "you skipped it"))
    db.conn.commit()
    cli.main(["--config", str(cfg), "why", "manual:0123456789ab"])
    out = capsys.readouterr().out
    assert "event: 2026-10-21T10:00:00  skip  you skipped it" in out and "no decision recorded" in out


def test_why_of_an_id_nothing_remembers_is_still_refused(db, settings, cfg, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "why", "manual:ffffffffffff"])
    assert "no job, link or JD file matches" in capsys.readouterr().err


# ------------------------------------------------------------------ 3: discover checks every key before it writes
def test_an_approved_job_is_not_written_to_the_inbox_again(db, settings, inbox, d):
    approved_file(settings, "deliveroo-economist-8242432.txt", url=NEW_URL, title=NEW_TITLE)
    run = fresh_run(d, settings, db)
    assert run.accept(deliveroo("8242432", NEW_TITLE, NEW_URL), "ats") == "dropped"
    assert list(inbox.glob("*.txt")) == [] and run.run.sources["ats"]["duplicate"] == 1


def test_the_same_address_in_another_form_in_approved_is_a_duplicate_even_when_the_title_changed(db, settings, inbox, d):
    approved_file(settings, "x.txt", url=NEW_URL, title="Something Else Entirely")
    run = fresh_run(d, settings, db)
    assert run.accept(deliveroo("8242432", NEW_TITLE, NEW_URL + "?gh_src=abc"), "ats") == "dropped"
    assert run.run.sources["ats"]["duplicate"] == 1 and list(inbox.glob("*.txt")) == []


def test_a_file_in_jds_done_blocks_by_its_keys_and_is_counted_a_duplicate(db, settings, inbox, d):
    closed = settings.path("jds_done") / "closed"
    write_ad(closed, "gone.txt", company="Deliveroo", title="Old Name", url=NEW_URL, age_days=1)
    run = fresh_run(d, settings, db)
    assert run.accept(deliveroo("8242432", NEW_TITLE, NEW_URL), "ats") == "dropped"
    assert run.run.sources["ats"]["duplicate"] == 1 and list(inbox.glob("*.txt")) == []


def test_a_file_already_in_the_inbox_by_its_url_is_a_duplicate(db, settings, inbox, d):
    write_ad(inbox, "there.txt", company="Deliveroo", title="Another Spelling", url=NEW_URL, age_days=1)
    run = fresh_run(d, settings, db)
    assert run.accept(deliveroo("8242432", NEW_TITLE, NEW_URL), "ats") == "dropped"
    assert run.run.sources["ats"]["duplicate"] == 1 and names(inbox) == ["there.txt"]


def test_a_different_posting_is_still_written(db, settings, inbox, d):
    approved_file(settings, "x.txt", url=OLD_URL, title=TITLE)
    run = fresh_run(d, settings, db)
    other = deliveroo("8999999", "Machine Learning Engineer", "https://job-boards.greenhouse.io/deliveroo/jobs/8999999")
    assert run.accept(other, "ats") == "written" and len(list(inbox.glob("*.txt"))) == 1


# ------------------------------------------------------------------ 4: skip --url
def test_skip_has_a_url_option(db, settings, cfg, capsys):
    f = approved_file(settings)
    cli.main(["--config", str(cfg), "skip", "--url", "https://job-boards.greenhouse.io/deliveroo/jobs/8202372?gh_src=x"])
    assert "decision log: skipped_user" in capsys.readouterr().out and not f.exists()
    assert len(decisions.records(settings)) == 1


def test_skip_with_nothing_named_says_so(db, settings, cfg, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "skip"])
    assert "--url" in capsys.readouterr().err
