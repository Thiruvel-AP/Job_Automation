"""live and the gap report: `s` at the gap report ends the job as `skipped (user)` (its JD goes to jds_done/skipped/), and a job that used typed
facts is offered the save at the end, whether or not the application was sent. The steps that cost money are fakes (see test_live.py)."""
import pytest

from careerops.apply.live import FINISHED, Hooks
from test_live import Fake, archive, filed, inbox, put, session, yes     # noqa: F401  (fixtures and helpers of the live tests)


class FactsFake(Fake):
    """Fake whose hooks include save_facts; it records (job id, sent, why) per call."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saved: list[tuple[str, bool, str]] = []

    def save_facts(self, jid, sent, why):
        self.calls.append(("save_facts", jid))
        self.saved.append((jid, sent, why))

    def hooks(self) -> Hooks:
        h = super().hooks()
        h.save_facts = self.save_facts
        return h


def test_skipped_user_is_a_finished_status_and_files_the_jd_as_skipped(db, settings, inbox, archive, jd):
    assert "skipped (user)" in FINISHED
    put(inbox, "one.txt", jd)
    fake = FactsFake(db, settings, review="skipped (user)")
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    [job] = db.jobs()
    assert (job["status"], job["reason"]) == ("skipped", "skipped (user)")
    assert live.results[0].status == "skipped (user)"
    assert [p.name for p in filed(settings, "skipped")] == ["one.txt"] and not (inbox / "one.txt").exists()
    assert fake.names() == ["tailor", "review", "save_facts"]                           # no form was opened


def test_a_plain_skip_is_unchanged(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = Fake(db, settings, review="skipped")
    live, _ = session(db, settings, fake)
    live.run()
    assert live.results[0].status == "skipped" and db.jobs()[0]["reason"] == "skipped in live review"


def test_after_an_application_the_save_is_offered_as_sent(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = FactsFake(db, settings, yes())
    live, _ = session(db, settings, fake)
    live.run()
    assert fake.names()[-2:] == ["mark_applied", "save_facts"]                          # after mark-applied, never before
    assert fake.saved == [(db.jobs()[0]["id"], True, "")]


def test_a_form_not_submitted_still_offers_the_save_but_as_not_sent(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = FactsFake(db, settings, {"Open the application form": ["y"], "Did YOU submit": ["n"]})
    live, _ = session(db, settings, fake)
    live.run()
    [(jid, sent, why)] = fake.saved
    assert sent is False and "not submit" in why and db.job(jid)["status"] == "ready_not_submitted"


def test_a_handoff_offers_the_save_as_not_sent(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = FactsFake(db, settings, {"Open the application form": ["y"], "Did YOU submit": ["n"]})
    fake.assist_result = {"confirmation": "x", "visa": [], "handoff": "the form would not take the upload"}
    live, _ = session(db, settings, fake)
    live.run()
    [(jid, sent, why)] = fake.saved
    assert sent is False and "hand" in why.lower() and db.job(jid)["status"] == "handed_off"


def test_a_file_that_was_rejected_offers_nothing(db, settings, inbox, archive):
    (inbox / "bad.txt").write_text("too short")
    fake = FactsFake(db, settings)
    session(db, settings, fake)[0].run()
    assert fake.saved == []


def test_a_session_without_the_hook_still_works(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)
    fake = Fake(db, settings, yes())
    live, _ = session(db, settings, fake)
    assert live.run() == 0 and db.jobs()[0]["status"] == "applied"


def test_a_save_that_is_interrupted_does_not_undo_the_job(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd)

    class Stops(FactsFake):
        def save_facts(self, jid, sent, why):
            raise KeyboardInterrupt

    fake = Stops(db, settings, yes())
    live, lines = session(db, settings, fake)
    assert live.run() == 0                                                              # the application is recorded; only the offer was cut short
    assert db.jobs()[0]["status"] == "applied" and any("still provisional" in l for l in lines)
