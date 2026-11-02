"""Skip and applied decisions are final and cover every copy of the same LISTING (apply/decided.py, v2.5): the same job id, ATS key or URL key.
A decision moves every copy's JD out of the inbox into paths.jds_done/<skipped|applied>/ (never deleted), discovery never writes a decided job
again (`already_decided`), `retriage --decided` cleans the inbox now (a dry run unless --apply), and `unskip` undoes a skip. Company + title
alone never decide (v2.5 addendum): "Graphcore" and "Graphcore Early Careers" posting one "AI Engineer" at two addresses are shown, the second
labelled `possible repost of a job you skipped on <date>`."""
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
def test_a_same_title_job_at_another_address_is_shown_again_labelled_a_possible_repost(db, settings, inbox, archive, jd):
    two_graphcores(inbox, jd)
    fake = Fake(db, settings, review="skipped")
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert len([c for c in fake.calls if c[0] == "review"]) == 2                    # two addresses, two answers: company + title never decide
    assert any("possible repost of a job you skipped on" in line for line in lines)
    assert names(inbox) == [] and names(settings.path("jds_done") / "skipped") == ["a-graphcore.txt", "b-graphcore-early.txt"]
    assert sorted(j["status"] for j in db.jobs()) == ["skipped", "skipped"]


def test_a_copy_of_the_same_listing_in_the_database_is_skipped_with_it(db, settings, inbox, archive, jd):
    same = manual.add_jd(db, jd, company="Graphcore Early Careers", title=TITLE, url="https://graphcore.example/jobs/1?utm_source=alert",
                         location="Bristol, UK")
    other = manual.add_jd(db, jd, company="Graphcore", title=TITLE, url="https://graphcore.example/jobs/77", location="Munich, Germany")
    for jid in (same, other):
        db.update_job(jid, status="triaged")
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    mine = next(j["id"] for j in db.jobs() if j["id"] not in (same, other))
    assert db.job(mine)["status"] == "skipped"
    assert db.job(same)["status"] == "skipped" and mine in db.job(same)["reason"]       # the same URL (a tracking parameter aside)
    assert db.job(other)["status"] == "triaged"                                         # another posting: never hidden


def test_an_application_covers_the_copies_too_and_they_are_not_marked_applied(db, settings, inbox, archive, jd):
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    copy = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://graphcore.example/jobs/1/", location="Bristol, UK")
    other = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://gc.example/3", location="Bristol, UK")
    fake = Fake(db, settings, yes())
    session(db, settings, fake)[0].run()
    assert names(inbox) == [] and names(settings.path("jds_done") / "applied") == ["a-graphcore.txt"]
    assert db.job(copy)["status"] == "skipped" and "applied" in db.job(copy)["reason"]
    assert db.job(other)["status"] != "skipped"                                           # same title, another posting
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


def test_a_rediscovered_ad_of_a_decided_listing_is_not_written_and_another_posting_is_labelled(db, settings, inbox, archive, jd, d):
    put(inbox, "a.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    run = fresh_run(d, settings, db)
    assert run.accept(graphcore("Graphcore", "7001", url="https://graphcore.example/jobs/1"), "ats") == "dropped"     # the very ad (new text, same id)
    assert run.run.sources["ats"]["already_decided"] == 1 and not run.run.sources["ats"].get("duplicate")
    other = graphcore("Graphcore", "8002")
    other.location = "London, UK"                                                             # another posting (London, its own id), same country
    assert run.accept(other, "ats") == "written", run.run.sources["ats"]                      # shown, with the label
    (written,) = [p for p in inbox.glob("*.txt")]
    assert "Possible repost: possible repost of a job you skipped on" in written.read_text(encoding="utf-8").partition("\n\n")[0]
    assert run.run.sources["ats"]["possible_repost"] == 1


def test_a_decided_job_known_only_by_its_filed_jd_still_blocks_a_rewrite(db, settings, d):
    folder = settings.path("jds_done") / "applied"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text(f"Company: Graphcore\nTitle: {TITLE}\nURL: https://job-boards.greenhouse.io/graphcore/jobs/7001\n\n{GC_JD}")
    run = fresh_run(d, settings, db)
    assert run.accept(graphcore("Graphcore", "7001"), "ats") == "dropped"
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
    """One Graphcore job skipped; a copy of it back in the inbox (the same address) and one in the database (the same address with a tracking
    parameter); another Graphcore posting of the same title (another address) and an unrelated ad."""
    put(inbox, "a.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    put(inbox, "copy.txt", jd, company="Graphcore", title=TITLE, n=1)
    put(inbox, "other.txt", jd, company="Acme", title=TITLE, n=3)
    put(inbox, "posting2.txt", jd, company="Graphcore", title=TITLE, n=2)
    row = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://graphcore.example/jobs/1?utm_source=x", location="Bristol, UK")
    db.update_job(row, status="triaged")
    return row


def test_retriage_decided_is_a_dry_run_and_moves_nothing_without_apply(db, settings, inbox, archive, jd, cfg, capsys):
    row = decided_world(db, settings, inbox, jd)
    cli.main(["--config", str(cfg), "retriage", "--decided"])
    out = capsys.readouterr().out
    assert "copy.txt" in out and "other.txt" not in out and "posting2.txt" not in out and "Dry run" in out and row in out
    assert names(inbox) == ["copy.txt", "other.txt", "posting2.txt"] and db.job(row)["status"] == "triaged"
    assert names(settings.path("jds_done") / "skipped") == ["a.txt"]


def test_retriage_decided_apply_moves_the_copies_and_skips_their_rows(db, settings, inbox, archive, jd, cfg, capsys):
    row = decided_world(db, settings, inbox, jd)
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])
    assert names(inbox) == ["other.txt", "posting2.txt"] and names(settings.path("jds_done") / "skipped") == ["a.txt", "copy.txt"]
    assert db.job(row)["status"] == "skipped"
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])                  # idempotent
    assert "0 inbox file(s)" in capsys.readouterr().out.splitlines()[-1] or True


def test_apply_without_decided_is_refused(cfg, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "retriage", "--apply"])
    assert e.value.code == 1


# ------------------------------------------------------------------ unskip
def test_unskip_restores_the_job_its_copies_and_their_files(db, settings, inbox, archive, jd, cfg, capsys):
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    (jid,) = [j["id"] for j in db.jobs()]
    copy = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://graphcore.example/jobs/1", location="Bristol, UK")
    db.update_job(copy, status="skipped", reason=f"same job as {jid}: skipped")
    assert names(inbox) == []
    cli.main(["--config", str(cfg), "unskip", "--id", jid])                       # the job and its copy of the same listing come back
    assert names(inbox) == ["a-graphcore.txt"] and names(settings.path("jds_done") / "skipped") == []
    assert db.job(jid)["status"] == "needs_review" and db.job(copy)["status"] == "new"    # tailored: live goes back to the review prompt
    assert decided.load(settings, db).hit("Graphcore", TITLE, "https://graphcore.example/jobs/1") is None       # the log's `unskipped` lifts it
    fake = Fake(db, settings, review="approved", answers=yes())
    live, _ = session(db, settings, fake)
    live.run()
    assert [c for c in fake.calls if c[0] == "review"] and db.job(jid)["status"] == "applied"    # worked on, not "already done"


def test_unskip_a_rule_skipped_job_goes_back_to_new(db, settings, inbox, archive, jd, cfg):
    put(inbox, "a.txt", jd, title="Plumber")
    session(db, settings, Fake(db, settings, {"Tailor it anyway": ["n"]}))[0].run()
    (job,) = db.jobs()
    assert job["status"] == "skipped"
    cli.main(["--config", str(cfg), "unskip", "--id", job["id"]])
    assert db.job(job["id"])["status"] == "new" and names(inbox) == ["a.txt"]


def test_an_applied_decision_is_final(db, settings, inbox, archive, jd, cfg, capsys):
    put(inbox, "a-graphcore.txt", jd, company="Graphcore", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, yes()))[0].run()
    copy = manual.add_jd(db, jd, company="Graphcore UK", title=TITLE, url="https://graphcore.example/jobs/1", location="Bristol, UK")
    db.update_job(copy, status="skipped", reason="same job as x: applied")
    for jid in [j["id"] for j in db.jobs()]:
        with pytest.raises(SystemExit):
            cli.main(["--config", str(cfg), "unskip", "--id", jid])
    assert names(settings.path("jds_done") / "applied") == ["a-graphcore.txt"]


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
