"""Fix round 2 after v2.5: the skip gaps. A job that is only a JD file (skipped or approved at the shortlist, never in the database) is found by its link or
its file name; `careerops skip` moves it AND writes the decision-log line; the shortlist's own [s] / [x] write it too; `careerops backfill-decisions`
fills what the log lacks, dated by the real event; and the same ATS triple is "decided", never a "possible repost". Real examples: Deliveroo
8202372 (skipped) vs 8242432 (new), Databricks gh_jid 8884753002 (skipped) vs 8927390002 (new). No network, no model."""
import json
import os
import re
from datetime import datetime

import pytest
import yaml
from test_decided import GC_JD, d, fresh_run, names  # noqa: F401  (fixtures and helpers of the decided tests)
from triage_fakes import NOW, write_ad

from careerops import cli
from careerops.apply import decided, decisions, targets
from careerops.apply import listing_keys as LK
from careerops.config import load_yaml
from careerops.discovery import blacklist, gates, manual, shortlist
from careerops.discovery.candidate import Candidate
from conftest import PROFILE_FIX

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
OLD_URL = "https://boards.greenhouse.io/deliveroo/jobs/8202372"            # skipped at the shortlist on 23 Oct: "Senior Data Scientist"
NEW_URL = "https://job-boards.greenhouse.io/deliveroo/jobs/8242432"
TITLE, NEW_TITLE = "Data Scientist", "Data Scientist (Economist)"        # the real ones are "Senior ...": the test settings exclude that level


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb, monkeypatch, tmp_path):
    gates.load_roles(settings, fb)
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")


@pytest.fixture
def inbox(settings):
    return settings.path("jds") / "inbox"


def build(settings, db, fb, **kw):
    return shortlist.build(settings, db, fb, now=NOW, archive=[], **kw)


def run_with(settings, db, fb, *answers):
    it = iter(answers)
    out = []
    plan = shortlist.run(settings, db, fb, ask=lambda prompt: next(it), out=out.append, now=NOW, archive=[])
    return plan, out


@pytest.fixture
def cfg(settings, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return path


def approved_file(settings, name="deliveroo-senior-data-scientist-8202372.txt", *, url=OLD_URL, title=TITLE, day="2026-11-03"):
    """An ad approved at the shortlist and never in the database."""
    return write_ad(settings.path("jds") / "approved" / day, name, company="Deliveroo", title=title, url=url, age_days=1)


def skipped_file(settings, name="deliveroo-senior-data-scientist-8202372.txt", *, url=OLD_URL, title=TITLE, when=(2026, 10, 23, 12, 0), reason=shortlist.SKIP_REASON,
                 company="Deliveroo"):
    p = write_ad(settings.path("jds_done") / "skipped", name, company=company, title=title, url=url, age_days=1)
    r = p.with_name(p.stem + ".reason.txt")
    r.write_text(reason + "\n", encoding="utf-8")
    stamp = datetime(*when).timestamp()
    os.utime(r, (stamp, stamp))
    return p


def deliveroo(ext_id, title, url):
    return Candidate(source="greenhouse", company="Deliveroo", title=title, location="London, UK", url=url, description=GC_JD,
                     posted="2026-10-20T09:00:00+00:00", posted_source="greenhouse.first_published", ext_id=ext_id, board="greenhouse:deliveroo",
                     apply_url=url, apply_kind="greenhouse")


# ------------------------------------------------------------------ 2 + 4: skip by link, by file name; the log line; the next shortlist and discover
def test_a_job_that_is_only_a_file_in_approved_is_skipped_by_its_url(db, settings, cfg, inbox, d, capsys):
    f = approved_file(settings)
    assert db.jobs() == [] and shortlist.approved_files(settings, db) == [f]
    cli.main(["--config", str(cfg), "skip", "https://job-boards.greenhouse.io/deliveroo/jobs/8202372?gh_src=x"])      # another form of the same address
    out = capsys.readouterr().out
    assert "decision log: skipped_user" in out and "jds_done/skipped/deliveroo-senior-data-scientist-8202372.txt" in out
    assert not f.exists() and [n for n in names(settings.path("jds_done") / "skipped") if not n.endswith(".reason.txt")] == [f.name]
    assert (settings.path("jds_done") / "skipped" / f.with_suffix(".reason.txt").name).read_text(encoding="utf-8").startswith("skipped (user)")
    (rec,) = decisions.records(settings)                                                                       # the decision line exists
    assert rec["decision"] == "skipped_user" and rec["job_id"] == decided.file_job_id(settings.path("jds_done") / "skipped" / f.name)
    assert rec["keys"][0] == f"id:{rec['job_id']}" and "ats:greenhouse:deliveroo:8202372" in rec["keys"]
    assert LK.url_key(OLD_URL) in rec["keys"] and any(k.startswith("deliveroo|data scientist|") for k in rec["keys"])
    assert shortlist.approved_files(settings, db) == []
    # the next shortlist: the same posting back in the inbox (re-fetched, another address form) is decided, not offered, not a repost
    write_ad(inbox, "again.txt", company="Deliveroo", title=TITLE, url="https://job-boards.eu.greenhouse.io/deliveroo/jobs/8202372", age_days=1)
    plan = build(settings, db, fb_of(settings))
    assert [r.name for r in plan.rows] == [] and plan.decided == 1
    # and discover leaves it out
    run = fresh_run(d, settings, db)
    assert run.accept(deliveroo("8202372", TITLE, OLD_URL), "ats") == "dropped" and run.run.sources["ats"]["already_decided"] == 1


def fb_of(settings):
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


def test_it_is_skipped_by_its_file_name_too_and_a_second_skip_writes_no_second_line(db, settings, cfg, capsys):
    f = approved_file(settings)
    cli.main(["--config", str(cfg), "skip", f.stem])                                                            # no .txt
    assert not f.exists() and len(decisions.records(settings)) == 1
    capsys.readouterr()
    cli.main(["--config", str(cfg), "skip", "deliveroo-senior-data-scientist-8202372.txt"])                   # now it is in jds_done/: nothing live to move
    assert "already skipped on" in capsys.readouterr().out and len(decisions.records(settings)) == 1


def test_a_bare_link_with_no_file_and_no_row_is_still_recorded_by_its_keys(db, settings, cfg):
    cli.main(["--config", str(cfg), "skip", "https://job-boards.greenhouse.io/deliveroo/jobs/8999999"])
    (rec,) = decisions.records(settings)
    assert rec["decision"] == "skipped_user" and "ats:greenhouse:deliveroo:8999999" in rec["keys"]
    assert decided.load(settings, db).hit("Deliveroo", "Whatever", "https://boards.greenhouse.io/deliveroo/jobs/8999999") == "skipped"


def test_skip_with_a_database_job_named_by_its_file_skips_the_row_and_the_file(db, settings, cfg, jd):
    f = approved_file(settings)
    jid = manual.add_jd(db, jd, company="Deliveroo", title=TITLE, url=OLD_URL, location="London, UK")
    db.update_job(jid, status="needs_review")
    cli.main(["--config", str(cfg), "skip", f.name])
    assert db.job(jid)["status"] == "skipped" and not f.exists()
    assert [r["decision"] for r in decisions.records(settings)] == ["skipped_user"]


@pytest.mark.parametrize("what", ["nothing-called-this", "manual:nope"])
def test_an_unknown_name_is_refused_in_one_line(db, settings, cfg, capsys, what):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "skip", what])
    assert e.value.code == 1 and "no job, link or JD file matches" in capsys.readouterr().err and decisions.records(settings) == []


def test_skip_id_still_works(db, settings, cfg, jd):
    jid = manual.add_jd(db, jd, company="Deliveroo", title=TITLE, url=OLD_URL, location="London, UK")
    cli.main(["--config", str(cfg), "skip", "--id", jid])
    assert db.job(jid)["status"] == "skipped" and len(decisions.records(settings)) == 1


def test_unskip_brings_a_file_only_skip_back_and_lifts_the_decision(db, settings, cfg, inbox):
    f = approved_file(settings)
    cli.main(["--config", str(cfg), "skip", f.name])
    cli.main(["--config", str(cfg), "unskip", "--id", f.name])
    assert names(inbox) == [f.name] and [r["decision"] for r in decisions.records(settings)] == ["skipped_user", "unskipped"]
    assert decided.load(settings, db).hit("Deliveroo", TITLE, OLD_URL) is None


# ------------------------------------------------------------------ 2: why by link and by file name
def test_why_finds_a_job_that_is_only_a_file_by_link_and_by_name_and_shows_the_dated_line(db, settings, cfg, capsys):
    f = approved_file(settings)
    cli.main(["--config", str(cfg), "skip", f.name])
    capsys.readouterr()
    for what in (OLD_URL, f.name, "ats:greenhouse:deliveroo:8202372"):
        cli.main(["--config", str(cfg), "why", what])
        out = capsys.readouterr().out
        line = next(x for x in out.splitlines() if "skipped_user" in x)
        assert DATE.match(line.strip()) and "?" not in line and "now: decided: skipped" in out
        assert "jds_done/skipped" in out or what.startswith("ats:")


def test_why_of_an_unknown_link_says_nothing_is_recorded(db, settings, cfg, capsys):
    cli.main(["--config", str(cfg), "why", "https://job-boards.greenhouse.io/deliveroo/jobs/1"])
    assert "no decision recorded" in capsys.readouterr().out


# ------------------------------------------------------------------ 3: the same ATS triple is decided, never a repost
def test_the_same_ats_triple_is_decided_and_never_listed_or_labelled_a_repost(db, settings, inbox):
    skipped_file(settings, url=OLD_URL, title=TITLE)
    write_ad(inbox, "new.txt", company="Deliveroo", title=NEW_TITLE, url="https://job-boards.eu.greenhouse.io/deliveroo/jobs/8202372?gh_src=y", age_days=1)
    plan = build(settings, db, fb_of(settings))
    assert plan.rows == [] and plan.decided == 1
    known = decided.load(settings, db)
    assert known.hit("Deliveroo", NEW_TITLE, NEW_URL.replace("8242432", "8202372")) == "skipped" and known.repost("Deliveroo", NEW_TITLE, NEW_URL.replace("8242432", "8202372")) == ""


def test_another_triple_with_the_same_title_is_only_a_possible_repost_with_the_real_date(db, settings, inbox):
    """Deliveroo: 8202372 "Senior Data Scientist" (skipped 23 Oct) and 8242432 "Senior Data Scientist (Economist)": two postings, the same fingerprint."""
    skipped_file(settings, url=OLD_URL, title=TITLE)
    write_ad(inbox, "new.txt", company="Deliveroo", title=NEW_TITLE, url=NEW_URL, age_days=1)
    plan = build(settings, db, fb_of(settings))
    (row,) = plan.rows
    assert plan.decided == 0 and row.repost.startswith("possible repost of a job you skipped on 2026-10-23 (manual:") and "?" not in row.repost


def test_databricks_company_domain_links_decide_by_the_url_so_another_gh_jid_is_only_a_repost(db, settings, inbox):
    old = "https://databricks.com/company/careers/open-positions/job?gh_jid=8884753002"
    new = "https://databricks.com/company/careers/open-positions/job?gh_jid=8927390002"
    assert LK.keys_for("Databricks", "x", old).ats == ""                                  # no ATS triple: the board is not in the address
    skipped_file(settings, "databricks-8884753002.txt", url=old, title="Machine Learning Engineer (Technical Data Architect)", company="Databricks")
    write_ad(inbox, "same.txt", company="Databricks", title="Machine Learning Engineer", url=old + "&utm_source=x", age_days=1)
    write_ad(inbox, "other.txt", company="Databricks", title="Machine Learning Engineer", url=new, age_days=1)
    plan = build(settings, db, fb_of(settings))
    assert [r.path.name for r in plan.rows] == ["other.txt"] and plan.decided == 1
    assert plan.rows[0].repost.startswith("possible repost of a job you skipped on 2026-10-23")


# ------------------------------------------------------------------ 1: the backfill
def db_events(db, jid, *events):
    for ts, stage, detail in events:
        db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (jid, ts, stage, detail))
    db.conn.commit()


def add_job(db, jid, status, reason, updated, url, title=TITLE, applied_at=None, company="Deliveroo"):
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": title, "description": "x", "url": url, "location": "London, UK"})
    db.update_job(jid, status=status, reason=reason)
    db.conn.execute("UPDATE jobs SET updated_at = ?, applied_at = ? WHERE id = ?", (updated, applied_at, jid))
    db.conn.commit()


def seed(db, settings):
    add_job(db, "manual:u1", "skipped", "skipped in live review", "2026-10-25T10:00:00", "https://boards.greenhouse.io/acme/jobs/111", company="Acme")
    db_events(db, "manual:u1", ("2026-10-21T14:00:00", "tailor", "{}"), ("2026-10-21T22:59:32", "live", "skipped in review"))   # skipped on the 21st, touched on the 25th
    add_job(db, "manual:a1", "skipped (ats)", "ATS S < 50", "2026-10-24T17:30:43", "https://jobs.lever.co/acme/0f3b2c1d-5a6e-4b7f-8c9d-1e2f3a4b5c6d", company="Acme")
    db_events(db, "manual:a1", ("2026-10-24T17:30:18", "live", "{}"), ("2026-10-24T17:30:40", "tailor", "{}"))
    add_job(db, "manual:p1", "applied", None, "2026-10-26T09:00:00", "https://boards.greenhouse.io/acme/jobs/222", applied_at="2026-10-20", company="Acme")
    add_job(db, "manual:n1", "needs_review", None, "2026-10-26T09:00:00", "https://boards.greenhouse.io/acme/jobs/333", company="Acme")


def test_the_backfill_dates_every_line_by_its_real_event_and_writes_all_keys(db, settings):
    seed(db, settings)
    lines = {r["job_id"]: r for r in targets.backfill(db, settings, apply=True)}
    assert set(lines) == {"manual:u1", "manual:a1", "manual:p1"}                                           # not the job still in review
    assert lines["manual:u1"]["ts"] == "2026-10-21T22:59:32" and lines["manual:u1"]["decision"] == "skipped_user"      # not updated_at (the 25th)
    assert lines["manual:a1"]["ts"] == "2026-10-24T17:30:40" and lines["manual:a1"]["decision"] == "skipped_ats"
    assert lines["manual:p1"]["ts"] == "2026-10-20T00:00:00" and lines["manual:p1"]["decision"] == "applied"
    written = {r["job_id"]: r for r in decisions.records(settings)}
    k = written["manual:u1"]["keys"]
    assert k[0] == "id:manual:u1" and "ats:greenhouse:acme:111" in k and LK.url_key("https://boards.greenhouse.io/acme/jobs/111") in k and k[-1].startswith("acme|")
    assert any(x.startswith("ats:lever:acme:") for x in written["manual:a1"]["keys"])
    assert written["manual:u1"]["ts"] == "2026-10-21T22:59:32"


def test_a_dry_run_writes_nothing_and_a_second_run_finds_nothing_new(db, settings, cfg, capsys):
    seed(db, settings)
    cli.main(["--config", str(cfg), "backfill-decisions"])
    out = capsys.readouterr().out
    assert "3 line(s) would be written" in out and "2026-10-21T22:59:32" in out and decisions.records(settings) == []
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    assert "3 line(s) written" in capsys.readouterr().out and len(decisions.records(settings)) == 3
    cli.main(["--config", str(cfg), "backfill-decisions", "--apply"])
    assert "0 line(s) written" in capsys.readouterr().out and len(decisions.records(settings)) == 3


def test_a_line_that_is_already_there_is_not_written_again(db, settings):
    seed(db, settings)
    decisions.append(settings, LK.keys_of_row(db.job("manual:u1")), decisions.SKIPPED_USER, "skipped (user)", "manual:u1", ts="2026-11-03T12:07:11")
    assert {r["job_id"] for r in targets.backfill(db, settings)} == {"manual:a1", "manual:p1"}


def test_the_backfill_covers_shortlist_skips_that_were_only_files_dated_by_the_shortlist_log(db, settings, cfg, inbox, capsys):
    skipped_file(settings, url=OLD_URL, title=TITLE)                                                          # file time: 23 Oct
    skipped_file(settings, "deliveroo-dq-8206309.txt", url="https://job-boards.greenhouse.io/deliveroo/jobs/8206309", title="Analytics Engineering Manager",
                 reason="disqualified, level: level excluded (\\bmanager\\b)")
    log = shortlist.log_path(settings)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"ts": "2026-10-28T09:15:00+00:00", "event": "skipped", "key": shortlist._key("Deliveroo", TITLE), "company": "Deliveroo", "title": TITLE}) + "\n",
                   encoding="utf-8")
    by_job = {r["source"].split("/")[-1]: r for r in targets.backfill(db, settings)}
    a, b = by_job["deliveroo-senior-data-scientist-8202372.txt"], by_job["deliveroo-dq-8206309.txt"]
    assert a["ts"].startswith("2026-10-28") and a["decision"] == "skipped_user" and "ats:greenhouse:deliveroo:8202372" in a["keys"]      # the log's date beats the file's
    assert b["decision"] == "skipped" and b["ts"].startswith("2026-10-23")                                      # a rule skip, dated by its reason file
    # then `why` and the shortlist show the real date, never "?"
    cli.main(["--config", str(cfg), "why", OLD_URL])
    assert "2026-10-28T" in capsys.readouterr().out
    write_ad(inbox, "new.txt", company="Deliveroo", title=NEW_TITLE, url=NEW_URL, age_days=1)
    (row,) = build(settings, db, fb_of(settings)).rows
    assert "on 2026-10-28" in row.repost and "?" not in row.repost


def test_the_sweep_with_apply_backfills_files_too(db, settings):
    skipped_file(settings)
    plan = decided.sweep(db, settings, apply=True)
    assert len(plan.backfilled) == 1 and plan.backfilled[0]["decision"] == "skipped_user"


# ------------------------------------------------------------------ the shortlist's own skips are logged, and its unskip lifts them
def test_the_shortlists_s_writes_the_decision_line_and_unskip_shortlist_lifts_it(db, settings, inbox):
    write_ad(inbox, "a.txt", company="Gone", age_days=1, url="https://boards.greenhouse.io/gone/jobs/4001")
    write_ad(inbox, "b.txt", company="Kept", age_days=1)
    plan = build(settings, db, fb_of(settings))
    n = next(i for i, r in enumerate(plan.rows, 1) if r.job["company"] == "Gone")
    run_with(settings, db, fb_of(settings), f"s {n}", "q")
    (rec,) = decisions.records(settings)
    assert rec["decision"] == "skipped_user" and "ats:greenhouse:gone:4001" in rec["keys"] and rec["job_id"] and DATE.match(rec["ts"])
    assert decided.load(settings, db).hit("Gone", "Data Scientist", "https://job-boards.greenhouse.io/gone/jobs/4001") == "skipped"
    shortlist.restore(settings, match="Gone")
    assert [r["decision"] for r in decisions.records(settings)] == ["skipped_user", "unskipped"]
    assert decided.load(settings, db).hit("Gone", "Data Scientist", "https://job-boards.greenhouse.io/gone/jobs/4001") is None


def test_x_logs_every_job_it_skips(db, settings, inbox):
    for c in ("Alpha", "Beta", "Gamma"):
        write_ad(inbox, f"{c}.txt", company=c, age_days=1)
    run_with(settings, db, fb_of(settings), "x")
    assert len(decisions.records(settings)) == 3 and {r["decision"] for r in decisions.records(settings)} == {"skipped_user"}

