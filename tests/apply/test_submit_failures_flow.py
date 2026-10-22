"""The failure count and the hand-off in the assist (a human submits) and in `live`: the job ends `handed_off`, `mark-applied` works as before, and a
third attempt never opens a browser. Fake pages and fake hooks only."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from careerops import autoapply, cli
from careerops.apply import autolog, failures as F
from careerops.apply.live import FINISHED, HANDED_OFF
from test_handoff import APPLICANT, Page, assisted, world  # noqa: F401  (assisted and world are fixtures)
from test_live import Fake, archive, filed, inbox, put, session  # noqa: F401  (archive and inbox are fixtures)

NOW = datetime(2026, 10, 22, 9, 30, 0)
URL = "https://boards.greenhouse.io/acme/jobs/1"


# ------------------------------------------------------------------ the assist
@pytest.fixture
def log(settings):
    return F.Failures(settings, now=lambda: NOW)


def test_the_assist_counts_form_not_found_and_offers_the_hand_off_at_the_second(assisted, world, log, capsys):
    first, closed = assisted(Page(form_after_ms=None), handoff=world.h, failures=log)
    assert log.count(world.job["id"]) == 1 and not first.get("handoff") and world.opened == []
    out = capsys.readouterr().out
    assert "[h] hand off" not in out
    second, closed2 = assisted(Page(form_after_ms=None), handoff=world.h, failures=log, answer="h")
    assert log.count(world.job["id"]) == 2 and second.get("handoff") and world.opened == [URL] and closed2 == [1]
    assert "Failed attempt 2 of 2 on this job" in capsys.readouterr().out
    assert (world.out / "answers_to_paste.txt").is_file() and world.clips == ["Ada Lovelace"]


def test_a_third_assist_attempt_opens_no_browser(assisted, world, log):
    for _ in range(2):
        assisted(Page(form_after_ms=None), handoff=world.h, failures=log, answer="")
    r, closed = assisted(Page(), handoff=world.h, failures=log, launch=False)
    assert r["handoff"] and "no third attempt" in r["handoff"] and world.opened == [URL] and log.count(world.job["id"]) == 2


def test_the_assist_goes_straight_to_the_hand_off_for_a_listed_host(assisted, world, log):
    r, closed = assisted(Page(url="https://jobs.lever.co/valarian/abc"), handoff=world.h, failures=log, launch=False, url="https://jobs.lever.co/valarian/abc")
    assert r["handoff"] and "apply.handoff_hosts" in r["handoff"] and world.opened == ["https://jobs.lever.co/valarian/abc"] and log.count(world.job["id"]) == 0


# ------------------------------------------------------------------ live
class HandOffFake(Fake):
    """Hooks whose auto-apply (or assist) ends in a hand-off."""

    def __init__(self, db, settings, **kw):
        super().__init__(db, settings, **kw)
        self.handoff_text = "HAND-OFF: 2 failed attempts on this job: FORM NOT FOUND x"

    def auto_gate(self, jid):
        from careerops.apply.autogate import Gate, Gates
        return Gates([Gate("ATS", True)])

    def auto_apply(self, jid):
        self._enter("auto_apply", jid)
        return autoapply.AutoResult(autolog.NEEDS_USER, self.handoff_text, handed_off=True)

    def hooks(self):
        h = super().hooks()
        h.auto_gate, h.auto_apply = self.auto_gate, self.auto_apply
        return h


def test_a_job_handed_off_by_the_auto_apply_ends_handed_off_with_no_review_prompt(db, settings, inbox, archive, jd):
    f = put(inbox, "valarian.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    fake = HandOffFake(db, settings)
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    jid = next(j for n, j in fake.calls if n == "tailor")
    row = db.job(jid)
    assert row["status"] == HANDED_OFF and "2 failed attempts" in row["reason"]
    assert [r.status for r in live.results] == ["handed_off"] and "handed_off" in FINISHED
    assert fake.names() == ["tailor", "preflight", "auto_apply"] and fake.prompts == []
    assert any(f"mark-applied --id {jid}" in l for l in lines) and not f.exists()          # finished: the JD goes to done/


def test_a_hand_off_from_the_assist_ends_handed_off_unless_the_person_says_they_submitted(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    fake = Fake(db, settings, answers={"Open the application form": ["y"], "Did YOU submit": ["n"]})
    fake.assist_result = {"confirmation": "", "visa": [], "handoff": "jobs.lever.co is on the hand-off list (apply.handoff_hosts in settings.yaml)"}
    live, lines = session(db, settings, fake)
    live.run()
    jid = next(j for n, j in fake.calls if n == "tailor")
    assert db.job(jid)["status"] == HANDED_OFF and "mark-applied" in "\n".join(lines)


def test_a_hand_off_the_person_then_submitted_by_hand_is_applied_as_before(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    fake = Fake(db, settings, answers={"Open the application form": ["y"], "Did YOU submit": ["y"]})
    fake.assist_result = {"confirmation": "REF-9", "visa": [], "handoff": "a bot check"}
    session(db, settings, fake)[0].run()
    jid = next(j for n, j in fake.calls if n == "tailor")
    assert db.job(jid)["status"] == "applied"


def test_mark_applied_works_on_a_handed_off_job(settings, db, job, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.json").write_text(json.dumps({"cv_pdf": "x", "letter": {"status": "off"}}), encoding="utf-8")
    db.upsert_job({k: job[k] for k in ("id", "source", "company", "title", "location", "url", "apply_url", "description")})
    db.update_job(job["id"], status=HANDED_OFF, out_dir=str(out), reason="HAND-OFF: x")
    monkeypatch.setattr(cli, "_ctx", lambda args: (settings, db))
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(cli, "export", lambda *a, **k: None)
    calls = []

    def fake_archive(db_, s, job_, rep, confirmation, applied_on, no_letter):
        calls.append(job_["id"])
        db_.update_job(job_["id"], status="applied")
        return True
    monkeypatch.setattr(cli, "_archive", fake_archive)
    cli.main(["mark-applied", "--id", job["id"], "--ref", "REF-1"])
    assert calls == [job["id"]] and db.job(job["id"])["status"] == "applied"
