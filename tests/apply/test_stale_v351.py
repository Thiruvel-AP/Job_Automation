"""9 Nov 2026 dry run: the RightShip form got a CV from 31 Oct and a letter from 3 Nov, older than v3.3 / v3.4 and the Education-first order.
apply/stale.py compares the job's CV and letter PDFs with the last commit that changed the tailoring, letter or template code: `live` (dry runs
included) tailors the job again, or with apply.stale_outputs: stop ends it with "outputs are older than the code: run with --retailor";
`careerops apply` refuses before a browser opens. No git in tests: CAREEROPS_CODE_TIME stands in for the commit time."""
import json
import os
from types import SimpleNamespace

import pytest

from careerops.apply import assist as assist_mod
from careerops.apply import stale
from careerops.apply.errors import ArchiveError, WrongFileError
from test_live import Fake, inbox, put, archive, session  # noqa: F401  (fixtures and helpers of the live tests)

CODE = 1_800_000_000.0                 # the last commit to the tailoring code
OLD, NEW = CODE - 9 * 86400, CODE + 3600


def pdfs(folder, cv_time, letter_time=None, letter=True):
    folder.mkdir(parents=True, exist_ok=True)
    cv, lt = folder / "Ada CV.pdf", folder / "Ada Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    os.utime(cv, (cv_time, cv_time))
    rep = {"cv_pdf": str(cv), "letter_pdf": str(lt), "letter": {"status": "ok" if letter else "off"}}
    if letter:
        lt.write_bytes(b"%PDF-1.4 letter")
        t = letter_time if letter_time is not None else cv_time
        os.utime(lt, (t, t))
    (folder / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    return rep


@pytest.fixture
def code_time(monkeypatch):
    monkeypatch.setenv(stale.ENV, str(CODE))


# ------------------------------------------------------------------ the check
def test_outputs_older_than_the_code_are_stale_and_newer_ones_are_not(tmp_path, code_time):
    job = {"id": "j1", "out_dir": str(tmp_path / "o")}
    pdfs(tmp_path / "o", OLD, OLD)
    found = stale.check(job)
    assert found is not None and stale.STOP_LINE in found.line() and [f[0] for f in found.files] == ["CV", "letter"]
    pdfs(tmp_path / "o", NEW, NEW)
    assert stale.check(job) is None


def test_one_old_file_is_enough(tmp_path, code_time):
    job = {"id": "j1", "out_dir": str(tmp_path / "o")}
    pdfs(tmp_path / "o", NEW, OLD)                                   # a fresh CV with the 3 Nov letter
    assert stale.check(job) is not None


def test_a_missing_cv_is_stale_and_a_letter_that_was_never_made_is_not_looked_for(tmp_path, code_time):
    job = {"id": "j1", "out_dir": str(tmp_path / "o")}
    rep = pdfs(tmp_path / "o", NEW, letter=False)
    assert stale.check(job) is None                                  # letter off: only the CV counts
    os.remove(rep["cv_pdf"])
    found = stale.check(job)
    assert found is not None and "CV missing" in found.line()


def test_no_report_or_no_known_code_time_checks_nothing(tmp_path, monkeypatch):
    assert stale.check({"id": "j1", "out_dir": str(tmp_path)}, code=stale.CodeChange(CODE)) is None
    pdfs(tmp_path / "o", OLD)
    monkeypatch.setenv(stale.ENV, "off")
    assert stale.check({"id": "j1", "out_dir": str(tmp_path / "o")}) is None


def test_the_git_line_is_read_and_a_failure_says_so(monkeypatch):
    c = stale.parse("1762600000 fb404ed Put Education first with a compact block until Sep 2027\n")
    assert c.time == 1762600000 and c.commit.startswith("fb404ed Put Education")
    assert stale.parse("") is None and stale.parse("fatal: not a git repository") is None
    monkeypatch.delenv(stale.ENV, raising=False)
    monkeypatch.setattr(stale, "_git", lambda root: None)
    assert "git log failed" in stale.unknown()


def test_the_code_paths_are_tailoring_letter_and_templates():
    assert "careerops/tailoring" in stale.CODE_PATHS and "templates" in stale.CODE_PATHS


def test_require_fresh_refuses_with_the_retailor_line(tmp_path, code_time):
    rep = pdfs(tmp_path / "o", OLD)
    with pytest.raises(stale.StaleOutputs, match="outputs are older than the code: run with --retailor"):
        stale.require_fresh({"id": "j1"}, rep["cv_pdf"], rep["letter_pdf"])
    assert issubclass(stale.StaleOutputs, WrongFileError) and issubclass(stale.StaleOutputs, ArchiveError)
    fresh = pdfs(tmp_path / "n", NEW)
    stale.require_fresh({"id": "j2"}, fresh["cv_pdf"], fresh["letter_pdf"])


def test_the_policy_setting(settings):
    assert stale.policy(settings) == "retailor"
    settings.raw["apply"]["stale_outputs"] = "stop"
    assert stale.policy(settings) == "stop"
    settings.raw["apply"]["stale_outputs"] = "whatever"
    assert stale.policy(settings) == "retailor"


# ------------------------------------------------------------------ careerops apply: refused before a browser opens
def test_assist_refuses_stale_outputs_before_a_browser_opens(tmp_path, code_time, monkeypatch):
    monkeypatch.setattr(assist_mod, "check_job_files", lambda *a, **k: None)
    plan = SimpleNamespace(work=SimpleNamespace(line=lambda: "W=GB"), describe=lambda: "plan")
    monkeypatch.setattr(assist_mod, "require_plan", lambda job, applicant: plan)
    rep = pdfs(tmp_path / "o", OLD)
    job = {"id": "manual:x1", "apply_url": "https://acme.example/apply", "url": "https://acme.example/job", "out_dir": str(tmp_path / "o")}
    with pytest.raises(stale.StaleOutputs, match="run with --retailor"):
        assist_mod.assist(job, rep, {}, tmp_path / "profile", launch=lambda d: pytest.fail("a browser was opened"), ask=lambda p: "")


# ------------------------------------------------------------------ careerops live: tailored again, dry runs included
def tailored_job(db, settings, inbox, fake, status="approved", when=OLD):
    """Pass the file through live once (dry run, tailored by the fake), then age its outputs and set its status."""
    put(inbox, "a.txt", "We build LLM agents. " * 40)
    live, _ = session(db, settings, fake, dry_run=True)
    live.run()
    jid = db.conn.execute("SELECT id FROM jobs").fetchone()[0]
    out = settings.path("outputs") / "x"
    pdfs(out, when)
    db.update_job(jid, status=status, out_dir=str(out))
    fake.calls.clear()
    return jid


def test_live_dry_run_tailors_a_stale_approved_job_again(db, settings, inbox, archive, code_time):
    fake = Fake(db, settings, review="approved")
    jid = tailored_job(db, settings, inbox, fake)
    live, lines = session(db, settings, fake, dry_run=True)
    live.run()
    assert ("tailor", jid) in fake.calls and ("review", jid) in fake.calls, fake.calls      # tailored and reviewed again
    assert any(stale.STOP_LINE in l and "tailored again" in l for l in lines), lines
    events = [row[-1] for row in db.conn.execute("SELECT * FROM events WHERE job_id=?", (jid,)).fetchall()]
    assert any("stale outputs: sent back to triaged" in str(e) for e in events), events


def test_live_leaves_fresh_outputs_alone(db, settings, inbox, archive, code_time):
    fake = Fake(db, settings, review="approved")
    jid = tailored_job(db, settings, inbox, fake, when=NEW)
    live, lines = session(db, settings, fake, dry_run=True)
    live.run()
    assert ("tailor", jid) not in fake.calls and not any(stale.STOP_LINE in l for l in lines)


def test_live_with_stop_ends_the_job_without_filling_anything(db, settings, inbox, archive, code_time):
    settings.raw["apply"]["stale_outputs"] = "stop"
    fake = Fake(db, settings, review="approved", answers={"Open the application form": ["y"]})
    jid = tailored_job(db, settings, inbox, fake)
    live, lines = session(db, settings, fake)
    live.run()
    assert not [c for c in fake.calls if c[0] in ("tailor", "preflight", "assist")], fake.calls
    assert db.job(jid)["status"] == "approved" and any("STOPPED: outputs are older than the code: run with --retailor" in l for l in lines)
    assert [r.status for r in live.results] == ["stale outputs"] and inbox.joinpath("a.txt").exists()
