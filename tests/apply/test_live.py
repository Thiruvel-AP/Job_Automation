"""`careerops live`: the loop around add-jd -> tailor -> review -> apply assist, with fakes for everything that costs money,
opens a browser or asks a person. Archive, inbox and database are all under tmp_path; the real archive is never near."""
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from careerops import cli
from careerops.apply.errors import ArchiveError
from careerops.apply.live import EXIT_ARCHIVE, EXIT_BUDGET, EXIT_INTERRUPTED, Hooks, Live
from careerops.discovery import manual

VISA = [{"question": "Do you require sponsorship now or in the future?", "value": "Yes"},
        {"question": "Do you have the right to work in the UK?", "value": None}]


class Fake:
    """Hooks that record the job id they were given and move the job's status the way the real step would."""

    def __init__(self, db, settings, answers=None, review="approved", archive="legit"):
        self.db, self.s = db, settings
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.review_choice, self.archive_mode = review, archive
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.raise_in: dict[str, BaseException] = {}
        self.preflight_error: ArchiveError | None = None
        self.assist_result = {"confirmation": "REF-1", "visa": VISA}

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        for key, queue in self.answers.items():
            if key in prompt:
                if not queue:
                    raise AssertionError(f"prompt answered too often: {prompt}")
                return queue.pop(0)
        raise AssertionError(f"unexpected prompt, no gate is ever answered for the user: {prompt}")

    def _enter(self, name: str, jid: str) -> None:
        self.calls.append((name, jid))
        if name in self.raise_in:
            raise self.raise_in[name]

    def tailor(self, jid):
        self._enter("tailor", jid)
        self.db.update_job(jid, status="needs_review", out_dir=str(self.s.path("outputs") / "x"))

    def review(self, jid):
        self._enter("review", jid)
        if self.review_choice == "approved":
            self.db.update_job(jid, status="approved")
        return self.review_choice

    def preflight(self, jid):
        self._enter("preflight", jid)
        if self.preflight_error:
            raise self.preflight_error

    def assist(self, jid):
        self._enter("assist", jid)
        return dict(self.assist_result)

    def mark_applied(self, jid, confirmation):
        self._enter("mark_applied", jid)
        job = self.db.job(jid)
        root = self.s.path("applications")
        role = root / job["company"] / job["title"]
        role.mkdir(parents=True)
        (role / "CV.pdf").write_bytes(b"cv")
        (role / "job.json").write_text(json.dumps({"confirmation": confirmation}))
        if self.archive_mode == "stray":
            (root / "stray.txt").write_text("something else wrote here")
        elif self.archive_mode == "delete":
            (root / "Old Co" / "Data Scientist" / "CV.pdf").unlink()
        elif self.archive_mode == "modify":
            (root / "Old Co" / "Data Scientist" / "CV.pdf").write_bytes(b"submitted CV, quietly edited")
        elif self.archive_mode == "touch":
            p = root / "Old Co" / "Data Scientist" / "CV.pdf"
            os.utime(p, ns=(p.stat().st_mtime_ns + 5 * 10**9,) * 2)
        self.db.update_job(jid, status="applied", application_dir=str(role))
        return True

    def hooks(self) -> Hooks:
        return Hooks(ask=self.ask, tailor=self.tailor, review=self.review, preflight=self.preflight, assist=self.assist,
                     mark_applied=self.mark_applied)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


@pytest.fixture
def archive(settings):
    """A small archive of 'submitted' applications that no test may change."""
    root = settings.path("applications")
    role = root / "Old Co" / "Data Scientist"
    role.mkdir(parents=True)
    (role / "CV.pdf").write_bytes(b"submitted CV")
    (role / "JD.txt").write_bytes(b"old jd")
    return root


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


@pytest.fixture
def inbox(settings):
    d = settings.path("jds") / "inbox"
    d.mkdir(parents=True)
    return d


def put(inbox: Path, name: str, jd: str, company="Acme", title="AI Engineer", n=1, header=True) -> Path:
    head = f"Company: {company}\nTitle: {title}\nURL: https://{company.lower()}.example/jobs/{n}\nLocation: Bristol, UK\n\n"
    p = inbox / name
    p.write_text((head if header else "") + jd + f"\nPosting {n}.\n", encoding="utf-8")
    return p


def session(db, settings, fake, **kw):
    lines: list[str] = []
    live = Live(db, settings, fake.hooks(), out=lines.append, resume_cmd="careerops live --max 5", **kw)
    return live, lines


def yes(**extra):
    return {"Open the application form": ["y"], "Did YOU submit": ["y"], **extra}


# ------------------------------------------------------------------ validation
def test_missing_empty_tiny_and_non_utf8_files_are_rejected_with_a_reason_and_the_loop_goes_on(db, settings, inbox, archive, jd):
    (inbox / "a_empty.txt").write_bytes(b"")
    (inbox / "b_tiny.txt").write_text("too short to be a job description")
    (inbox / "c_binary.txt").write_bytes(b"\xff\xfe\x00" * 300)
    (inbox / "d_notes.md").write_text("not a .txt file, ignored entirely")
    put(inbox, "e_good.txt", jd)
    fake = Fake(db, settings, yes())
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    rejected = settings.path("jds") / "rejected"
    assert sorted(p.name for p in rejected.iterdir()) == [
        "a_empty.reason.txt", "a_empty.txt", "b_tiny.reason.txt", "b_tiny.txt", "c_binary.reason.txt", "c_binary.txt"]
    assert "0 bytes" in (rejected / "a_empty.reason.txt").read_text()
    assert "not UTF-8" in (rejected / "c_binary.reason.txt").read_text()
    assert (inbox / "d_notes.md").exists() and not (inbox / "a_empty.txt").exists()
    assert any(l.startswith("REJECTED") for l in lines) and not any("Traceback" in l for l in lines)
    assert [n for n, _ in fake.calls if n == "tailor"] == ["tailor"]                  # the good one was still processed


def test_a_file_that_disappears_is_reported_not_raised(db, settings, inbox, archive):
    live, lines = session(db, settings, Fake(db, settings))
    assert live._one(inbox / "gone.txt") is None
    assert live.results[0].status == "rejected (invalid file)" and "no such file" in live.results[0].note


def test_a_rejected_name_is_never_overwritten(db, settings, inbox, archive):
    rejected = settings.path("jds") / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "x.txt").write_text("an earlier rejected file")
    (inbox / "x.txt").write_text("short")
    session(db, settings, Fake(db, settings))[0].run()
    assert (rejected / "x.txt").read_text() == "an earlier rejected file" and (rejected / "x-2.txt").read_text() == "short"
    assert (rejected / "x-2.reason.txt").exists()


# ------------------------------------------------------------------ the job id flows through the steps
def test_the_job_id_returned_by_add_jd_is_what_tailor_review_assist_and_mark_applied_receive(db, settings, inbox, archive, jd):
    put(inbox, "one.txt", jd, company="Acme", n=1)
    fake = Fake(db, settings, yes())
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    want = manual.job_id_for("Acme", "AI Engineer", "https://acme.example/jobs/1")
    assert [j["id"] for j in db.jobs()] == [want]
    assert fake.calls == [("tailor", want), ("review", want), ("preflight", want), ("assist", want), ("mark_applied", want)]
    assert db.job(want)["status"] == "applied"
    done = settings.path("jds") / "done"
    assert [p.name for p in done.iterdir()] == [f"{want.replace(':', '_')}-one.txt"] and not (inbox / "one.txt").exists()


def test_missing_company_and_title_are_asked_for_and_a_blank_answer_rejects_the_file(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, header=False)
    put(inbox, "b.txt", jd, header=False, n=2)
    fake = Fake(db, settings, yes(**{"company?": ["Beta Ltd", ""], "job title?": ["AI Engineer"],
                                     "application URL?": ["https://beta.example/1"]}))
    live, _ = session(db, settings, fake)
    live.run()
    assert [j["company"] for j in db.jobs()] == ["Beta Ltd"]
    assert (settings.path("jds") / "rejected" / "b.reason.txt").read_text().strip() == "no company or title given"


def test_a_job_the_rules_skip_is_only_tailored_when_the_user_says_so(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, title="Plumber")
    fake = Fake(db, settings, {"Tailor it anyway": ["n"]})
    live, _ = session(db, settings, fake)
    live.run()
    assert fake.calls == [] and live.results[0].status == "skipped (rules)"
    assert [p.name for p in (settings.path("jds") / "done").iterdir()][0].endswith("-a.txt")
    put(inbox, "b.txt", jd, title="Plumber", n=2)
    fake2 = Fake(db, settings, yes(**{"Tailor it anyway": ["y"]}))
    session(db, settings, fake2)[0].run()
    assert fake2.names() == ["tailor", "review", "preflight", "assist", "mark_applied"]


def test_an_existing_job_past_triage_is_not_reset_by_the_same_jd_again(db, settings, inbox, archive, jd):
    jid = manual.add_jd(db, jd, company="Acme", title="AI Engineer", url="https://acme.example/jobs/1", location="Bristol, UK")
    db.update_job(jid, status="needs_review")
    put(inbox, "a.txt", jd)
    fake = Fake(db, settings)
    session(db, settings, fake)[0].run()
    assert fake.calls == [] and db.job(jid)["status"] == "needs_review"
    assert "already exists" in (settings.path("jds") / "rejected" / "a.reason.txt").read_text()


# ------------------------------------------------------------------ the archive guard
@pytest.mark.parametrize("mode", ["delete", "modify", "touch", "stray"])
def test_a_deletion_a_modification_or_a_stray_addition_stops_the_whole_loop(db, settings, inbox, archive, jd, mode):
    put(inbox, "1.txt", jd, company="Acme", n=1)
    put(inbox, "2.txt", jd, company="Beta", n=2)
    fake = Fake(db, settings, yes(), archive=mode)
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_ARCHIVE
    assert [j["company"] for j in db.jobs()] == ["Acme"]                       # the second JD was never started
    assert (inbox / "2.txt").exists() and (inbox / "1.txt").exists()          # nor was the first one filed away as done
    assert not (settings.path("jds") / "done").exists() or not list((settings.path("jds") / "done").iterdir())
    r = live.results[0]
    assert r.archive.startswith("VIOLATION")
    text = "\n".join(lines)
    assert {"delete": "REMOVED  Old Co/Data Scientist/CV.pdf", "modify": "MODIFIED Old Co/Data Scientist/CV.pdf",
            "touch": "MODIFIED Old Co/Data Scientist/CV.pdf", "stray": "STRAY    stray.txt"}[mode] in text
    assert "Look at the archive by hand" in text
    summary = next((settings.path("live")).glob("*.md")).read_text()
    assert "VIOLATION" in summary and "Stopped:" in summary


def test_the_jobs_own_new_company_and_role_folder_is_the_one_allowed_addition(db, settings, inbox, archive, jd):
    before = tree(archive)
    put(inbox, "1.txt", jd, company="Acme", n=1)
    put(inbox, "2.txt", jd, company="Beta", n=2)
    fake = Fake(db, settings, yes(**{"Open the application form": ["y", "y"], "Did YOU submit": ["y", "y"]}))
    live, _ = session(db, settings, fake)
    assert live.run() == 0
    assert [r.archive for r in live.results] == ["ok (4 path(s) added)"] * 2
    after = tree(archive)
    assert {k: v for k, v in after.items() if k.startswith("Old Co")} == {k: v for k, v in before.items()}
    assert sorted(set(k.split("/")[0] for k in after)) == ["Acme", "Beta", "Old Co"]


def test_a_dry_run_that_somehow_adds_to_the_archive_also_stops(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings)

    def sneaky(jid):
        Fake.tailor(fake, jid)
        (archive / "Acme").mkdir()
    fake.tailor = sneaky
    live, _ = session(db, settings, fake, dry_run=True)
    assert live.run() == EXIT_ARCHIVE


def test_the_snapshot_is_taken_before_add_jd_and_compared_after_the_last_step(db, settings, inbox, archive, jd, monkeypatch):
    order = []
    from careerops.apply import live as live_mod, snapshot
    real = snapshot.take
    monkeypatch.setattr(live_mod.snapshot, "take", lambda root: order.append("snapshot") or real(root))
    orig_add = manual.add_jd
    monkeypatch.setattr(live_mod.manual, "add_jd", lambda *a, **k: order.append("add_jd") or orig_add(*a, **k))
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    live, _ = session(db, settings, fake)
    live.run()
    assert order == ["snapshot", "add_jd", "snapshot"]


# ------------------------------------------------------------------ dry run and the 'did you submit' gate
def test_dry_run_never_touches_apply_and_leaves_the_jd_in_the_inbox_for_later(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings)                                           # any apply prompt would fail the test
    live, lines = session(db, settings, fake, dry_run=True)
    assert live.run() == 0
    assert fake.names() == ["tailor", "review"]
    jid = db.jobs()[0]["id"]
    assert db.job(jid)["status"] == "approved" and live.results[0].status == "approved (dry run)"
    assert (inbox / "1.txt").exists()
    # the real run later picks the job up at the apply step: no second tailoring, no second review
    fake2 = Fake(db, settings, yes())
    live2, _ = session(db, settings, fake2)
    assert live2.run() == 0
    assert fake2.names() == ["preflight", "assist", "mark_applied"] and db.job(jid)["status"] == "applied"


def test_a_dry_run_never_files_a_jd_or_marks_it_done_whatever_the_review_says(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, review="skipped")
    live, _ = session(db, settings, fake, dry_run=True)
    assert live.run() == 0
    jid = db.jobs()[0]["id"]
    assert db.job(jid)["status"] == "skipped" and live.results[0].status == "skipped"
    assert (inbox / "1.txt").exists()
    assert not (settings.path("jds") / "done").exists() or not list((settings.path("jds") / "done").iterdir())
    state = json.loads((settings.path("live") / "state.json").read_text()) if (settings.path("live") / "state.json").exists() else {"jobs": {}}
    assert not any(v.get("done") for v in state["jobs"].values())
    # the same JD, run live: the job is not redone (it was skipped), and only now is the JD filed as done
    fake2 = Fake(db, settings)
    live2, _ = session(db, settings, fake2)
    assert live2.run() == 0 and fake2.calls == []
    assert not (inbox / "1.txt").exists() and len(list((settings.path("jds") / "done").iterdir())) == 1


def test_a_dry_run_leaves_an_already_done_copy_where_it_is(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    session(db, settings, Fake(db, settings, review="skipped"))[0].run()                 # live: done, filed
    done_file = next((settings.path("jds") / "done").iterdir())
    (inbox / "again.txt").write_bytes(done_file.read_bytes())
    live, lines = session(db, settings, Fake(db, settings), dry_run=True)
    assert live.run() == 0 and (inbox / "again.txt").exists() and any("already done" in l for l in lines)


@pytest.mark.parametrize("answer", ["n", "N", "", "no", "maybe", " "])
def test_anything_but_y_never_calls_mark_applied(db, settings, inbox, archive, jd, answer):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes(**{"Did YOU submit": [answer]}))
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert "mark_applied" not in fake.names()
    jid = db.jobs()[0]["id"]
    assert db.job(jid)["status"] == "ready_not_submitted" and db.job(jid)["applied_at"] is None
    assert live.results[0].status == "ready_not_submitted"
    assert tree(archive).keys() == {"Old Co", "Old Co/Data Scientist", "Old Co/Data Scientist/CV.pdf",
                                    "Old Co/Data Scientist/JD.txt"}
    assert list((settings.path("jds") / "done").iterdir())                    # the job is finished: its JD is filed


def test_declining_to_open_the_form_never_runs_the_assist(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, {"Open the application form": [""]})
    session(db, settings, fake)[0].run()
    assert fake.names() == ["tailor", "review", "preflight"]
    assert db.jobs()[0]["status"] == "ready_not_submitted"


def test_an_x_from_the_assist_means_not_submitted_and_the_question_is_not_even_asked(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, {"Open the application form": ["y"]})
    fake.assist_result = {"confirmation": "x", "visa": VISA}
    session(db, settings, fake)[0].run()
    assert "mark_applied" not in fake.names() and db.jobs()[0]["status"] == "ready_not_submitted"


def test_a_job_without_a_url_is_applied_by_hand_and_asks_only_if_you_submitted(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd, header=False)
    fake = Fake(db, settings, {"company?": ["Acme"], "job title?": ["AI Engineer"], "application URL?": [""],
                               "Did YOU submit": ["y"]})
    jd_file = inbox / "1.txt"
    jd_file.write_text("Location: Bristol, UK\n" + jd_file.read_text())
    session(db, settings, fake)[0].run()
    assert fake.names() == ["tailor", "review", "preflight", "mark_applied"]            # no assist: there is no form to open


def test_a_refused_preflight_leaves_the_job_approved_and_the_jd_in_the_inbox(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.preflight_error = ArchiveError("archive root missing")
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert fake.names() == ["tailor", "review", "preflight"] and db.jobs()[0]["status"] == "approved"
    assert live.results[0].status == "apply_refused" and (inbox / "1.txt").exists()
    assert any("REFUSED" in l for l in lines)


def test_every_visa_line_is_printed_and_ends_up_in_the_summary(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    live, lines = session(db, settings, fake)
    live.run()
    want = ["CHECK VISA ANSWER: Do you require sponsorship now or in the future? -> Yes",
            "CHECK VISA ANSWER: Do you have the right to work in the UK? -> EMPTY"]
    printed = [l.strip() for l in lines if "CHECK VISA ANSWER" in l]
    assert [p for p in printed if p in want] == want
    summary = next(settings.path("live").glob("*.md")).read_text()
    assert all(w in summary for w in want)


# ------------------------------------------------------------------ review choices
def test_a_skip_leaves_the_job_skipped_and_moves_the_jd_to_done_with_the_job_id_as_prefix(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, review="skipped")
    live, _ = session(db, settings, fake)
    assert live.run() == 0
    job = db.jobs()[0]
    assert job["status"] == "skipped" and fake.names() == ["tailor", "review"]
    assert [p.name for p in (settings.path("jds") / "done").iterdir()] == [f"{job['id'].replace(':', '_')}-1.txt"]


def test_quit_at_the_review_prompt_stops_the_session_and_keeps_the_job_in_review(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    put(inbox, "2.txt", jd, company="Beta", n=2)
    fake = Fake(db, settings, review="quit")
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert [j["status"] for j in db.jobs()] == ["needs_review"] and (inbox / "1.txt").exists() and (inbox / "2.txt").exists()
    assert any("Resume with:  careerops live --max 5" in l for l in lines)


def test_a_budget_stop_leaves_the_job_triaged_and_exits_1(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings)
    fake.tailor = lambda jid: fake.calls.append(("tailor", jid))             # tailor_jobs returned without tailoring
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_BUDGET
    assert db.jobs()[0]["status"] == "triaged" and (inbox / "1.txt").exists()


def test_a_pending_letter_is_not_finished_work(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings)
    fake.tailor = lambda jid: (fake.calls.append(("tailor", jid)), db.update_job(jid, status="letter_pending"))
    live, _ = session(db, settings, fake)
    assert live.run() == 0
    assert live.results[0].status == "letter_pending" and (inbox / "1.txt").exists() and fake.names() == ["tailor"]


def test_max_limits_the_files_and_rejected_files_do_not_count(db, settings, inbox, archive, jd):
    (inbox / "00_bad.txt").write_text("tiny")
    for i in range(1, 4):
        put(inbox, f"{i}.txt", jd, company=f"Co{i}", n=i)
    fake = Fake(db, settings, review="skipped")
    live, lines = session(db, settings, fake, max_jobs=2)
    live.run()
    assert [j["company"] for j in db.jobs()] == ["Co1", "Co2"] and (inbox / "3.txt").exists()
    assert any("--max 2 reached" in l for l in lines)


def test_files_are_processed_in_name_order(db, settings, inbox, archive, jd):
    for name, co in (("b.txt", "Bee"), ("a.txt", "Ay"), ("c.txt", "Cee")):
        put(inbox, name, jd, company=co, n=ord(name[0]))
    fake = Fake(db, settings, review="skipped")
    session(db, settings, fake)[0].run()
    assert [db.job(j)["company"] for _, j in fake.calls if _ == "tailor"] == ["Ay", "Bee", "Cee"]


# ------------------------------------------------------------------ resume
def test_a_completed_jd_is_skipped_by_content_hash_even_under_another_name(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    session(db, settings, fake)[0].run()
    assert len(fake.calls) == 5
    (inbox / "again.txt").write_bytes((settings.path("jds") / "done" / next(
        (settings.path("jds") / "done").iterdir()).name).read_bytes())          # the same bytes, dropped in again
    fake2 = Fake(db, settings)                                                    # must not be called at all
    live2, lines = session(db, settings, fake2)
    assert live2.run() == 0
    assert fake2.calls == [] and not (inbox / "again.txt").exists()
    assert any("already done" in l for l in lines) and len(db.jobs()) == 1
    assert len(list((settings.path("jds") / "done").iterdir())) == 2              # the copy was filed, not processed


def test_ctrl_c_records_where_it_stopped_and_prints_the_resume_command_and_a_rerun_continues(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["review"] = KeyboardInterrupt()
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_INTERRUPTED
    jid = db.jobs()[0]["id"]
    text = "\n".join(lines)
    assert "STOPPED at stage 'review' of 1.txt" in text and f"job {jid}, status 'needs_review'" in text
    assert "Resume with:  careerops live --max 5" in text and (inbox / "1.txt").exists()
    assert "careerops mark-applied" not in text                                  # review is before any form
    state = json.loads((settings.path("live") / "state.json").read_text())
    assert next(iter(state["jobs"].values()))["job_id"] == jid and not next(iter(state["jobs"].values()))["done"]
    assert list(settings.path("live").glob("*.md"))                               # the summary was written anyway
    # rerun: the same job, not a new one; it resumes at review, no second add or tailor
    fake2 = Fake(db, settings, yes())
    live2, _ = session(db, settings, fake2)
    assert live2.run() == 0
    assert fake2.names() == ["review", "preflight", "assist", "mark_applied"] and len(db.jobs()) == 1
    assert db.job(jid)["status"] == "applied" and not (inbox / "1.txt").exists()


def test_ctrl_c_during_the_assist_says_to_run_mark_applied_if_you_already_submitted(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["assist"] = KeyboardInterrupt()
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_INTERRUPTED
    jid = db.jobs()[0]["id"]
    assert f"If you already pressed Submit in the browser, run: careerops mark-applied --id {jid}" in "\n".join(lines)
    assert db.jobs()[0]["status"] == "approved"                                    # nothing was recorded as sent


def test_an_unanswered_prompt_is_a_stop_not_an_answer(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["review"] = EOFError()
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_INTERRUPTED and "mark_applied" not in fake.names()


def test_a_ctrl_c_that_leaves_the_archive_changed_exits_with_the_archive_code(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())

    def interrupted_after_a_write(jid):
        (archive / "Old Co" / "Data Scientist" / "JD.txt").unlink()
        raise KeyboardInterrupt()
    fake.assist = interrupted_after_a_write
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_ARCHIVE
    assert any("REMOVED  Old Co/Data Scientist/JD.txt" in l for l in lines)


def test_a_started_job_not_yet_added_to_the_db_by_a_crashed_run_is_added_once(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    fake = Fake(db, settings, yes())
    fake.raise_in["tailor"] = KeyboardInterrupt()
    session(db, settings, fake)[0].run()
    session(db, settings, Fake(db, settings, yes()))[0].run()
    assert len(db.jobs()) == 1 and db.conn.execute("SELECT COUNT(*) FROM events WHERE stage='live'").fetchone()[0] == 1


def test_a_corrupt_state_file_stops_before_anything_is_touched(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    settings.path("live").mkdir(parents=True)
    (settings.path("live") / "state.json").write_text("{not json")
    fake = Fake(db, settings)
    live, lines = session(db, settings, fake)
    assert live.run() == 2 and fake.calls == [] and (inbox / "1.txt").exists() and db.jobs() == []


# ------------------------------------------------------------------ the summary
def test_the_session_summary_has_id_company_role_status_sponsor_flag_visa_lines_and_the_archive_check(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd, company="Acme")
    fake = Fake(db, settings, yes())
    live, lines = session(db, settings, fake)
    live.run()
    (path,) = settings.path("live").glob("*.md")
    text = path.read_text()
    jid = db.jobs()[0]["id"]
    for want in (f"- id: {jid}", "- company: Acme", "- role: AI Engineer", "- status: applied", "- sponsor register: ",
                 "- archive check: ok (4 path(s) added)", "CHECK VISA ANSWER: Do you require sponsorship"):
        assert want in text, want
    assert text.strip() in "\n".join(lines)                                       # also printed to the terminal


# ------------------------------------------------------------------ safety
def test_live_never_moves_anything_into_or_out_of_the_archive(db, settings, inbox, archive, jd):
    live, _ = session(db, settings, Fake(db, settings))
    victim = archive / "Old Co" / "Data Scientist" / "CV.pdf"
    with pytest.raises(RuntimeError, match="archive is never touched"):
        live._move(victim, settings.path("jds") / "done", "x.txt")
    f = inbox / "ok.txt"
    f.write_text("x")
    with pytest.raises(RuntimeError, match="archive is never touched"):
        live._move(f, archive, "x.txt")
    assert victim.read_bytes() == b"submitted CV" and f.exists()


def test_live_refuses_yes_and_max_below_one(capsys):
    for argv in (["live", "--yes"], ["live", "-y"], ["live", "--max", "0"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 1
    assert "never answers a gate for you" in capsys.readouterr().err


def test_live_source_has_no_submit_or_send_code():
    src = (Path(__file__).resolve().parents[2] / "careerops" / "apply" / "live.py").read_text(encoding="utf-8")
    for word in (".click(", "smtplib", "sendmail", "requests.", "page.", "sync_playwright"):
        assert word not in src


# ------------------------------------------------------------------ --retailor: the same JD again after a fix
def test_retailor_tailors_a_started_job_again_instead_of_resuming_it(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    first = Fake(db, settings)
    session(db, settings, first, dry_run=True)[0].run()                       # tailored, approved, left in the inbox
    jid = db.jobs()[0]["id"]
    assert first.names() == ["tailor", "review"] and db.job(jid)["status"] == "approved"
    plain = Fake(db, settings)
    live, _ = session(db, settings, plain, dry_run=True)
    live.run()
    assert plain.names() == []                                                # a plain rerun resumes: nothing to redo
    again = Fake(db, settings)
    live2, lines = session(db, settings, again, dry_run=True, retailor=True)
    assert live2.run() == 0
    assert again.names() == ["tailor", "review"] and all(j == jid for _, j in again.calls)     # same job id, tailored again
    assert len(db.jobs()) == 1 and any("--retailor: 'approved' -> 'triaged'" in l for l in lines)
    assert any(e["detail"] == "retailor: sent back to triaged" for e in db.conn.execute("SELECT detail FROM events WHERE stage='live'"))


def test_retailor_never_touches_an_applied_job(db, settings, inbox, archive, jd):
    put(inbox, "1.txt", jd)
    session(db, settings, Fake(db, settings, yes()))[0].run()                  # applied; its JD is filed
    jid = db.jobs()[0]["id"]
    done = next((settings.path("jds") / "done").iterdir())
    (inbox / "again.txt").write_bytes(done.read_bytes())
    fake = Fake(db, settings)
    session(db, settings, fake, retailor=True)[0].run()
    assert fake.calls == [] and db.job(jid)["status"] == "applied"


def test_retailor_is_a_live_option_but_not_part_of_the_resume_command():
    import argparse
    from careerops import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["live", "--yes", "--retailor"])
    assert e.value.code == 1                                                    # refused as --yes, not as an unknown option
