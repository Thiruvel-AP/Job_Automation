"""Fix round after v2.5: `careerops live --id manual:ff68fffd015a` works on a job the database already knows. No network, no model."""
import pytest
from test_intake import _cfg  # noqa: F401  (helper)

from careerops import cli
from careerops.db import DB
from careerops.discovery import intake, manual

EU_URL = "https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101"
JID = "manual:ff68fffd015a"
TEXT = "Build and evaluate LLM agents for maritime safety. " * 20


def ad_of_job():
    return intake.Ad(company="RightShip", title="AI Engineer", url=EU_URL, location="London, UK", text=TEXT, source="greenhouse", ad_id="4982444101")


@pytest.fixture
def known(settings, db):
    """The job in the database, as `live` left it."""
    assert manual.add_jd(db, TEXT, company="RightShip", title="AI Engineer", url=EU_URL, location="London, UK") == JID
    return db


def test_the_file_of_a_known_job_is_found_in_the_inbox(settings, known):
    path, _ = intake.write_inbox(settings.path("jds") / "inbox", ad_of_job())
    assert intake.file_for_job(settings, known, JID) == (path, False)


def test_or_in_the_approved_folder_when_you_approved_it(settings, known):
    day = settings.path("jds") / "approved" / "2026-11-02"
    path, _ = intake.write_inbox(day, ad_of_job())
    assert intake.file_for_job(settings, known, JID) == (path, False)


def test_a_file_that_was_filed_away_is_written_again_from_the_stored_ad(settings, known):
    path, created = intake.file_for_job(settings, known, JID)
    assert created and path.parent == settings.path("jds") / "inbox"
    head = path.read_text(encoding="utf-8").splitlines()[:3]
    assert head == ["Company: RightShip", "Title: AI Engineer", f"URL: {EU_URL}"]
    from careerops.apply import decided
    assert decided.file_job_id(path) == JID                         # the same job, so live resumes it instead of meeting a second one
    assert intake.file_for_job(settings, known, JID) == (path, False)


def test_an_unknown_id_is_refused_in_one_line(settings, db):
    with pytest.raises(intake.IntakeError, match="no job manual:nope in the database") as e:
        intake.file_for_job(settings, db, "manual:nope")
    assert "\n" not in str(e.value)


@pytest.mark.parametrize("status, word", [("skipped", "skipped"), ("applied", "applied to")])
def test_a_job_you_skipped_or_applied_to_is_not_reopened(settings, known, status, word):
    known.update_job(JID, status=status)
    with pytest.raises(intake.IntakeError, match=word):
        intake.file_for_job(settings, known, JID)
    assert not (settings.path("jds") / "inbox").exists() or not list((settings.path("jds") / "inbox").iterdir())


def test_a_job_with_no_stored_ad_text_is_refused(settings, db):
    db.upsert_job({"id": "manual:short", "source": "manual", "company": "Acme", "title": "Data Scientist", "description": "x", "url": "https://acme.com/1"})
    with pytest.raises(intake.IntakeError, match="no stored ad text"):
        intake.file_for_job(settings, db, "manual:short")


class FakeLive:
    seen: dict = {}

    def __init__(self, db, s, hooks, **kw):
        FakeLive.seen = kw

    def run(self):
        return 0


def test_live_id_works_on_that_one_file_and_a_rerun_is_the_same_command(settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    db = DB(settings.path("db"))
    manual.add_jd(db, TEXT, company="RightShip", title="AI Engineer", url=EU_URL, location="London, UK")
    db.conn.close()
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    cli.main(["live", "--id", JID, "--max", "2"])
    only = FakeLive.seen["only"]
    assert only.exists() and only.parent == settings.path("jds") / "inbox"
    assert FakeLive.seen["resume_cmd"] == f"careerops live --max 2 --id {JID}"
    assert "Written again from the stored ad:" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--url", EU_URL], ["--clipboard"]])
def test_id_with_url_or_clipboard_is_refused(settings, monkeypatch, capsys, extra):
    _cfg(monkeypatch, settings)
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--id", JID, *extra])
    assert e.value.code == 1 and "use one of them" in capsys.readouterr().err


def test_live_id_of_an_unknown_job_stops_before_anything_runs(settings, monkeypatch, capsys):
    _cfg(monkeypatch, settings)
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    FakeLive.seen = {}
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--id", "manual:nope"])
    assert e.value.code == 1 and "no job manual:nope" in capsys.readouterr().err and FakeLive.seen == {}
