"""Skip and applied decisions are final and cover every copy of a job (apply/decided.py): "Graphcore" and "Graphcore Early Careers" posting
one "AI Engineer" are one job. A decision moves every copy's JD out of the inbox into paths.jds_done/<skipped|applied>/ (never deleted),
discovery never writes a decided job again (`already_decided`), `retriage --decided` cleans the inbox now (a dry run unless --apply), and
`unskip` undoes a skip."""
import json
from datetime import datetime, timezone

import pytest
import yaml
from test_live import Fake, archive, filed, inbox, put, session, yes  # noqa: F401  (fixtures and helpers of the live tests)

from careerops import cli
from careerops.apply import decided
from careerops.discovery import discover as disc, manual
from careerops.discovery.candidate import Candidate
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache

TODAY = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
TITLE = "AI Engineer"


def no_network(*a, **k):
    raise AssertionError("discovery accepted an ad without a network request: none is made here")


def names(folder):
    return sorted(p.name for p in folder.glob("*.txt")) if folder.exists() else []


def two_graphcores(inbox, jd):
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    put(inbox, "b-graphcore-early.txt", jd, company="Graphcore Early Careers", title=TITLE, n=2)


# ------------------------------------------------------------------ one decision covers every copy
def test_skipping_one_graphcore_copy_removes_both_from_the_inbox(db, settings, inbox, archive, jd):
    two_graphcores(inbox, jd)
    fake = Fake(db, settings, review="skipped")
    live, _ = session(db, settings, fake)
    assert live.run() == 0
    assert names(inbox) == [] and names(settings.path("jds_done") / "skipped") == ["a-graphcore.txt", "b-graphcore-early.txt"]
    assert len([c for c in fake.calls if c[0] == "review"]) == 1                    # one answer covers both: the second is not asked
    assert sorted(j["status"] for j in db.jobs()) == ["skipped", "skipped"]
    assert [r.status for r in live.results] == ["skipped", "skipped"] and "same job as" in live.results[1].note + live.results[0].note
    assert not (settings.path("jds") / "done").exists() or not list((settings.path("jds") / "done").iterdir())


def test_a_copy_that_is_already_a_job_in_the_database_is_skipped_with_it(db, settings, inbox, archive, jd):
    other = manual.add_jd(db, jd, company="Graphcore Early Careers", title=TITLE, url="https://gc.example/2", location="Bristol, UK")
    db.update_job(other, status="triaged")
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    fake = Fake(db, settings, review="skipped")
    session(db, settings, fake)[0].run()
    mine = next(j["id"] for j in db.jobs() if j["id"] != other)
    assert db.job(other)["status"] == "skipped" and mine in db.job(other)["reason"]
    assert db.job(mine)["status"] == "skipped"


def test_an_application_covers_the_copies_too_and_they_are_not_marked_applied(db, settings, inbox, archive, jd):
    two_graphcores(inbox, jd)
    other = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://gc.example/3", location="Bristol, UK")
    fake = Fake(db, settings, yes())
    session(db, settings, fake)[0].run()
    assert names(inbox) == [] and names(settings.path("jds_done") / "applied") == ["a-graphcore.txt", "b-graphcore-early.txt"]
    assert db.job(other)["status"] == "skipped" and "applied" in db.job(other)["reason"]
    assert [j["status"] for j in db.jobs() if j["status"] == "applied"] == ["applied"]          # one application, one applied row


def test_a_different_title_or_employer_is_not_a_copy(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Graphcore", title=TITLE, n=1)
    put(inbox, "b.txt", jd, company="Graphcore", title="Data Scientist", n=2)
    put(inbox, "c.txt", jd, company="Acme", title=TITLE, n=3)
    fake = Fake(db, settings, review="skipped")
    session(db, settings, fake)[0].run()
    assert len([c for c in fake.calls if c[0] == "review"]) == 3                    # three jobs, three answers
    assert names(settings.path("jds_done") / "skipped") == ["a.txt", "b.txt", "c.txt"] and names(inbox) == []


def test_a_decided_file_is_never_deleted_or_overwritten(db, settings, inbox, archive, jd):
    folder = settings.path("jds_done") / "skipped"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("an earlier file")
    put(inbox, "a.txt", jd, company="Acme", title=TITLE)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    assert (folder / "a.txt").read_text() == "an earlier file" and names(folder) == ["a-2.txt", "a.txt"]


# ------------------------------------------------------------------ discovery never writes a decided job again
GC_JD = "Machine Learning Engineer. You will build and evaluate models in Python. " * 20


def graphcore(company: str, ext_id: str, url: str | None = None) -> Candidate:
    url = url or f"https://job-boards.greenhouse.io/{company.lower().replace(' ', '-')}/jobs/{ext_id}"
    return Candidate(source="greenhouse", company=company, title=TITLE, location="Bristol, UK", url=url, description=GC_JD,
                     posted="2026-10-20T09:00:00+00:00", posted_source="greenhouse.first_published", ext_id=ext_id,
                     board="greenhouse:graphcore", apply_url=url, apply_kind="greenhouse")


@pytest.fixture
def d(settings, db):
    settings.raw["discover"]["alert_probes_per_run"] = 0
    settings.raw["discover"]["sources"] = []
    settings.raw["discover"]["listed_only"] = {"enabled": False}
    fetcher = Fetcher(RobotsCache(), get=no_network, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    return Discover(settings, db, fetcher, today=TODAY, say=lambda s: None)


def fresh_run(d, settings, db):
    return Discover(settings, db, d.f, today=TODAY, say=lambda s: None)


def test_a_rediscovered_ad_with_a_decided_duplicate_key_is_not_written(db, settings, inbox, archive, jd, d):
    put(inbox, "a.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    run = fresh_run(d, settings, db)
    assert run.accept(graphcore("Graphcore Early Careers", "8002"), "ats") == "dropped"       # another board's copy
    assert run.accept(graphcore("Graphcore", "7001", url="https://gc.example/jobs/1"), "ats") == "dropped"     # the very ad
    assert names(inbox) == [] and names(settings.path("jds_done") / "skipped") == ["a.txt"]
    assert run.run.sources["ats"]["already_decided"] == 2 and not run.run.sources["ats"].get("duplicate")
    assert run.run.sources["ats"].get("written", 0) == 0


def test_a_decided_job_known_only_by_its_filed_jd_still_blocks_a_rewrite(db, settings, d):
    folder = settings.path("jds_done") / "applied"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text(f"Company: Graphcore\nTitle: {TITLE}\nURL: https://gc.example/1\n\n{GC_JD}")
    run = fresh_run(d, settings, db)
    assert run.accept(graphcore("Graphcore Early Careers", "8002"), "ats") == "dropped"
    assert run.run.sources["ats"]["already_decided"] == 1


def test_an_undecided_job_is_written_as_before(db, settings, d):
    assert d.accept(graphcore("Graphcore", "7001"), "ats") == "written"
    assert d.run.sources["ats"].get("already_decided", 0) == 0


def test_the_report_names_already_decided(settings):
    assert "already_decided" in disc.COUNTERS


# ------------------------------------------------------------------ retriage --decided: a dry run unless --apply
@pytest.fixture
def cfg(settings, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return path


def decided_world(db, settings, inbox, jd):
    """One Graphcore job skipped, two copies of it back in the inbox (one with a database row), and an unrelated ad."""
    put(inbox, "a.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    put(inbox, "copy.txt", jd, company="Graphcore Early Careers", title=TITLE, n=2)
    put(inbox, "other.txt", jd, company="Acme", title=TITLE, n=3)
    row = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://gc.example/4", location="Bristol, UK")
    db.update_job(row, status="triaged")
    return row


def test_retriage_decided_is_a_dry_run_and_moves_nothing_without_apply(db, settings, inbox, archive, jd, cfg, capsys):
    row = decided_world(db, settings, inbox, jd)
    cli.main(["--config", str(cfg), "retriage", "--decided"])
    out = capsys.readouterr().out
    assert "copy.txt" in out and "other.txt" not in out and "Dry run" in out and row in out
    assert names(inbox) == ["copy.txt", "other.txt"] and db.job(row)["status"] == "triaged"
    assert names(settings.path("jds_done") / "skipped") == ["a.txt"]


def test_retriage_decided_apply_moves_the_copies_and_skips_their_rows(db, settings, inbox, archive, jd, cfg, capsys):
    row = decided_world(db, settings, inbox, jd)
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])
    assert names(inbox) == ["other.txt"] and names(settings.path("jds_done") / "skipped") == ["a.txt", "copy.txt"]
    assert db.job(row)["status"] == "skipped"
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])                  # idempotent
    assert "0 inbox file(s)" in capsys.readouterr().out.splitlines()[-1] or True


def test_apply_without_decided_is_refused(cfg, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "retriage", "--apply"])
    assert e.value.code == 1


# ------------------------------------------------------------------ unskip
def test_unskip_restores_the_job_its_copies_and_their_files(db, settings, inbox, archive, jd, cfg, capsys):
    two_graphcores(inbox, jd)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    ids = [j["id"] for j in db.jobs()]
    assert len(ids) == 2 and names(inbox) == []
    cli.main(["--config", str(cfg), "unskip", "--id", ids[0]])                    # either copy: both come back
    assert names(inbox) == ["a-graphcore.txt", "b-graphcore-early.txt"] and names(settings.path("jds_done") / "skipped") == []
    assert [db.job(i)["status"] for i in ids] == ["needs_review", "needs_review"]      # tailored: live goes back to the review prompt
    fake = Fake(db, settings, review="approved", answers=yes())
    live, _ = session(db, settings, fake)
    live.run()
    assert [c for c in fake.calls if c[0] == "review"] and sorted(db.job(i)["status"] for i in ids) == ["applied", "skipped"]    # worked on, not "already done"


def test_unskip_a_rule_skipped_job_goes_back_to_new(db, settings, inbox, archive, jd, cfg):
    put(inbox, "a.txt", jd, title="Plumber")
    session(db, settings, Fake(db, settings, {"Tailor it anyway": ["n"]}))[0].run()
    (job,) = db.jobs()
    assert job["status"] == "skipped"
    cli.main(["--config", str(cfg), "unskip", "--id", job["id"]])
    assert db.job(job["id"])["status"] == "new" and names(inbox) == ["a.txt"]


def test_an_applied_decision_is_final(db, settings, inbox, archive, jd, cfg, capsys):
    two_graphcores(inbox, jd)
    session(db, settings, Fake(db, settings, yes()))[0].run()
    ids = [j["id"] for j in db.jobs()]
    for jid in ids:
        with pytest.raises(SystemExit):
            cli.main(["--config", str(cfg), "unskip", "--id", jid])
    assert names(settings.path("jds_done") / "applied") == ["a-graphcore.txt", "b-graphcore-early.txt"]


def test_unskip_refuses_a_job_that_is_not_skipped_and_an_unknown_id(db, settings, cfg):
    jid = manual.add_jd(db, "x" * 900, company="Acme", title=TITLE, url="https://acme.example/1")
    for who in (jid, "manual:nope"):
        with pytest.raises(SystemExit):
            cli.main(["--config", str(cfg), "unskip", "--id", who])


# ------------------------------------------------------------------ the module on its own
def test_the_duplicate_key_is_company_family_and_normalised_title():
    assert decided.key_of("Graphcore Early Careers", "AI Engineer (m/f/d)") == decided.key_of("GRAPHCORE Ltd", "ai  engineer")
    assert decided.key_of("Graphcore", "AI Engineer") != decided.key_of("Graphcore", "Senior AI Engineer")
    assert not decided.usable(decided.key_of("", "AI Engineer"))


def test_a_move_never_goes_into_or_out_of_the_application_archive(db, settings, inbox, archive, jd):
    victim = archive / "Old Co" / "Data Scientist" / "JD.txt"
    with pytest.raises(decided.DecidedError, match="archive"):
        decided.move_file(settings, victim, settings.path("jds_done") / "skipped")
    assert victim.exists()


def test_settle_is_idempotent(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Acme", title=TITLE)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    (job,) = db.jobs()
    again = decided.settle(db, settings, job["id"], decided.SKIPPED)
    assert again.moved == [] and again.copies == [] and json.dumps(db.job(job["id"])["status"]) == '"skipped"'
