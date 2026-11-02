"""v2.5 Phase 3: skips are permanent. The decision log (data/state/decisions.jsonl) keeps every skip with its keys; discover, the inbox intake,
shortlist and live never bring a skipped listing back; only `careerops unskip --id` does. The Anthropic Forward Deployed Engineer case: London
(5423029008) skipped, Munich (5391016008) and Paris (5391021008) are other jobs and stay. No network, no model."""
import json

import pytest
import yaml
from test_decided import GC_JD, TITLE, d, fresh_run, no_network, names  # noqa: F401  (fixtures and helpers of the decided tests)
from test_live import Fake, archive, inbox, put, session, yes  # noqa: F401

from careerops import cli
from careerops.apply import decided, decisions
from careerops.discovery import intake, manual, shortlist
from careerops.discovery.candidate import Candidate
from careerops.review import workflow

FDE = "Forward Deployed Engineer"
LONDON, MUNICH, PARIS = "5423029008", "5391016008", "5391021008"


def gh(job_id, location="London, UK", query=""):
    url = f"https://job-boards.greenhouse.io/anthropic/jobs/{job_id}{query}"
    return Candidate(source="greenhouse", company="Anthropic", title=FDE, location=location, url=url, description=GC_JD + f" Job {job_id}.",
                     posted="2026-10-20T09:00:00+00:00", posted_source="greenhouse.first_published", ext_id=job_id, board="greenhouse:anthropic",
                     apply_url=url, apply_kind="greenhouse")


@pytest.fixture
def cfg(settings, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return path


@pytest.fixture
def fde(db, settings, jd):
    """The London FDE as it was on 2 Nov: in the database (needs_review) and approved at the shortlist on 27 Oct, never skipped."""
    url = f"https://job-boards.greenhouse.io/anthropic/jobs/{LONDON}"
    jid = manual.add_jd(db, jd, company="Anthropic", title=FDE, url=url, location="London, UK")
    db.update_job(jid, status="needs_review")
    folder = settings.path("jds") / "approved" / "2026-10-27"
    folder.mkdir(parents=True)
    (folder / f"anthropic-forward-deployed-engineer-{LONDON}.txt").write_text(f"Company: Anthropic\nTitle: {FDE}\nURL: {url}\nLocation: London, UK\n\n{jd}",
                                                                               encoding="utf-8")
    return jid


def test_skip_id_stores_the_skip_with_its_keys_and_takes_the_jd_out_of_the_approved_folder(db, settings, cfg, fde, capsys):
    assert len(shortlist.approved_files(settings, db)) == 1
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    assert db.job(fde)["status"] == "skipped" and db.job(fde)["reason"] == "skipped (user)"
    (rec,) = decisions.records(settings)
    assert rec["decision"] == "skipped_user" and f"ats:greenhouse:anthropic:{LONDON}" in rec["keys"] and f"id:{fde}" in rec["keys"]
    assert shortlist.approved_files(settings, db) == [] and names(settings.path("jds_done") / "skipped") == [f"anthropic-forward-deployed-engineer-{LONDON}.txt"]


def test_why_shows_the_skip_and_the_key_that_matched(db, settings, cfg, fde, capsys):
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "why", f"ats:greenhouse:anthropic:{LONDON}"])
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "skipped_user" in x)
    assert f"ats ats:greenhouse:anthropic:{LONDON}" in line.split("matched by:")[1] and fde in line and "now: decided: skipped" in out


def test_a_skip_overrides_an_earlier_approval_even_while_the_file_is_still_there(db, settings, fde):
    decided.record(settings, db.job(fde), decided.SKIPPED, "skipped (user)")             # the log says skipped; the file was not moved yet
    assert shortlist.approved_files(settings, db) == [] and len(shortlist.approved_files(settings)) == 1


def test_two_runs_of_discover_do_not_bring_the_skipped_job_back_and_munich_and_paris_stay(db, settings, cfg, fde, d, inbox):
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    for _ in range(2):
        run = fresh_run(d, settings, db)
        assert run.accept(gh(LONDON), "ats") == "dropped"                                 # the re-fetched ad: new text, new hash, same job id
        assert run.run.sources["ats"]["already_decided"] == 1
    known = decided.load(settings, db)
    for job_id, place in ((MUNICH, "Munich, Germany"), (PARIS, "Paris, France")):         # other Greenhouse ids: never hidden by the skip
        url = f"https://job-boards.greenhouse.io/anthropic/jobs/{job_id}"
        assert known.hit("Anthropic", FDE, url, place) is None and known.repost("Anthropic", FDE, url, place) == ""     # another country: no label
    run = fresh_run(d, settings, db)
    assert run.accept(gh("5423029999", "Bristol, UK"), "ats") == "written", run.run.sources["ats"]      # another UK posting: shown...
    (new,) = inbox.glob("*.txt")
    assert "Possible repost: possible repost of a job you skipped on" in new.read_text(encoding="utf-8")       # ... with the label


def test_the_same_job_id_with_a_tracking_query_is_not_readmitted(db, settings, cfg, fde, d):
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    run = fresh_run(d, settings, db)
    assert run.accept(gh(LONDON, query="?utm_source=linkedin&gh_src=abc"), "ats") == "dropped"


def test_unskip_brings_it_back(db, settings, cfg, fde, d):
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    cli.main(["--config", str(cfg), "unskip", "--id", fde])
    assert decisions.records(settings)[-1]["decision"] == "unskipped"
    assert decided.load(settings, db).hit("Anthropic", FDE, f"https://job-boards.greenhouse.io/anthropic/jobs/{LONDON}", "London, UK") is None
    assert db.job(fde)["status"] in ("needs_review", "new")


def test_s_in_careerops_review_stores_the_skip_and_moves_the_jd_out_of_approved(db, settings, fde, monkeypatch):
    monkeypatch.setattr(workflow, "review_job", lambda *a, **k: "skipped")                 # a plain [s]: no gap report
    workflow.run(db, settings, {})
    assert db.job(fde)["status"] == "skipped" and db.job(fde)["reason"] == "skipped (user)"
    assert decisions.records(settings)[0]["decision"] == "skipped_user"
    assert shortlist.approved_files(settings) == [] and names(settings.path("jds_done") / "skipped")


def test_the_migration_backfills_the_log_for_decided_jobs_and_sweeps_the_approved_folder(db, settings, cfg, fde, capsys):
    db.update_job(fde, status="skipped", reason="skipped in live review")                 # decided in the database, no line in the log yet
    assert decisions.records(settings) == []
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])
    out = capsys.readouterr().out
    (rec,) = decisions.records(settings)
    assert rec["job_id"] == fde and rec["decision"] == "skipped_user" and rec["reason"].startswith("backfill")
    assert "decision log: 1 line(s) written" in out and shortlist.approved_files(settings) == []
    cli.main(["--config", str(cfg), "retriage", "--decided", "--apply"])                  # idempotent: no second backfill line
    assert len(decisions.records(settings)) == 1


def test_the_inbox_intake_refuses_a_skipped_job(db, settings, cfg, fde, monkeypatch):
    cli.main(["--config", str(cfg), "skip", "--id", fde])
    ad = intake.Ad(company="Anthropic", title=FDE, url=f"https://job-boards.greenhouse.io/anthropic/jobs/{LONDON}?utm_source=x", location="London, UK",
                   text=GC_JD, source="greenhouse", ad_id=LONDON)
    monkeypatch.setattr(intake, "fetch_ad", lambda *a, **k: ad)
    with pytest.raises(intake.IntakeError, match="you skipped this job on"):
        intake.add_url(settings, ad.url)
    assert not list((settings.path("jds") / "inbox").glob("*.txt")) if (settings.path("jds") / "inbox").exists() else True


def test_two_greenhouse_jobs_with_the_same_title_never_hide_each_other_in_live(db, settings, inbox, archive, jd):
    for n, (job_id, place) in enumerate(((LONDON, "London, UK"), ("5423029999", "London, UK")), 1):
        (inbox / f"{n}.txt").write_text(f"Company: Anthropic\nTitle: {TITLE}\nURL: https://job-boards.greenhouse.io/anthropic/jobs/{job_id}\n"
                                        f"Location: {place}\n\n{jd}\nPosting {n}.\n", encoding="utf-8")
    fake = Fake(db, settings, review="skipped")
    session(db, settings, fake)[0].run()
    reviewed = [c[1] for c in fake.calls if c[0] == "review"]
    assert len(reviewed) == 2 and len(set(reviewed)) == 2                                # skipping one did not hide the other: both asked
    ats = sorted(k for rec in decisions.records(settings) for k in rec["keys"] if k.startswith("ats:"))
    assert ats == [f"ats:greenhouse:anthropic:{LONDON}", "ats:greenhouse:anthropic:5423029999"]


def test_a_possible_repost_is_labelled_in_live_and_auto_mode_leaves_it_for_you(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Acme", title=TITLE, n=1)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()
    put(inbox, "b.txt", jd, company="Acme", title=TITLE, n=2)                               # same company, title and country; another address
    fake = Fake(db, settings, review="skipped")
    gated = []
    hooks = fake.hooks()
    hooks.auto_gate = lambda jid: gated.append(jid) or pytest.fail("auto mode never gates a possible repost")
    from careerops.apply.live import Live
    lines: list[str] = []
    Live(db, settings, hooks, out=lines.append, resume_cmd="careerops live").run()
    assert any("possible repost of a job you skipped on" in line for line in lines) and gated == []
    assert any("auto mode leaves a possible repost for you" in line for line in lines)
