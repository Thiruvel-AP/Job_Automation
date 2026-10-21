"""`careerops tailor --id` and `careerops live` through the real CLI, on the isolated --mock data root (MockLLM, no network,
no browser). Tailoring renders real PDFs, so these need xelatex like every other end-to-end test."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from conftest import FlipLLM

from careerops import cli, mockroot
from careerops.db import DB


pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex (or drive Chromium)


@pytest.fixture
def world(tmp_path, settings, jd):
    """A config whose real paths and mock root are all under tmp_path; returns (config path, real folder, mock folder)."""
    real, mock = tmp_path / "real", tmp_path / "mock"
    raw = settings.raw
    raw["paths"].update({"db": str(real / "careerops.db"), "outputs": str(real / "outputs"),
                         "excel": str(real / "exports" / "companies.xlsx"), "tmp": str(real / "tmp"),
                         "applications": str(real / "Companies"), "applications_auto": str(real / "Auto"),
                         "jds": str(real / "jds"), "live": str(real / "live"),
                         "mock_root": str(mock)})
    (real / "Companies" / "Old Co").mkdir(parents=True)
    (real / "Companies" / "Old Co" / "CV.pdf").write_bytes(b"SUBMITTED")
    (real / "Auto").mkdir()
    raw["apply"] = {**raw["apply"], "mode": "assist"}             # explicit: these tests never depend on what the user set in the real file
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg, real, mock


def _hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        h.update(str(p.relative_to(folder)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------ tailor --id
def test_tailor_id_tailors_exactly_that_job_and_leaves_the_others_triaged(world, capsys):
    cfg, real, mock = world
    other = DB(mock / "careerops.db")
    other.upsert_job({"id": "x:other", "source": "manual", "company": "Other", "title": "AI Engineer",
                      "location": "Bristol, UK", "url": "u", "apply_url": "u", "description": "d" * 900})
    other.update_job("x:other", status="triaged", ceiling_pre=0.99, tier=1, family="ai_engineer")
    other.conn.close()
    cli.main(["--config", str(cfg), "tailor", "--mock", "--id", mockroot.SEED_JOB_ID, "--limit", "5"])
    out = capsys.readouterr().out
    assert "Tailoring 1 job(s) with MockLLM" in out
    db = DB(mock / "careerops.db")
    assert db.job(mockroot.SEED_JOB_ID)["status"] == "needs_review" and db.job("x:other")["status"] == "triaged"


def test_tailor_id_refuses_an_unknown_job_and_a_job_that_is_not_triaged(world, capsys, monkeypatch):
    import argparse

    from careerops.llm import MockLLM
    cfg, real, mock = world
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: MockLLM())          # the real (tmp) DB; no provider is built
    db = DB(real / "careerops.db")
    db.upsert_job({"id": "x:1", "source": "manual", "company": "Acme", "title": "AI Engineer", "location": "Bristol, UK",
                   "url": "u", "apply_url": "u", "description": "d" * 900})
    db.update_job("x:1", status="approved")
    db.conn.close()

    def run(job_id):
        cli.cmd_tailor(argparse.Namespace(config=str(cfg), mock=False, id=job_id, limit=10, one_page=False, cmd="tailor"))
    with pytest.raises(SystemExit) as e:
        run("nope:1")
    assert e.value.code == 1 and "no such job: nope:1" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        run("x:1")
    assert e.value.code == 1 and "x:1 is 'approved', not 'triaged'" in capsys.readouterr().err
    assert DB(real / "careerops.db").job("x:1")["status"] == "approved"


def test_tailor_limit_still_works_without_id(world, capsys):
    cfg, real, mock = world
    cli.main(["--config", str(cfg), "tailor", "--mock", "--limit", "1"])
    assert "Tailoring 1 job(s) with MockLLM" in capsys.readouterr().out


# ------------------------------------------------------------------ live, end to end
JD_HEAD = "Company: Northwind Health AI\nTitle: AI Engineer\nURL: https://example.com/mock-job\nLocation: Bristol, UK\n\n"


def _inbox(mock: Path, jd: str, name="northwind.txt") -> Path:
    d = mock / "jds" / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(JD_HEAD + jd, encoding="utf-8")
    return p


def _answers(monkeypatch, *answers):
    it = iter(answers)
    seen = []

    def fake_input(prompt=""):
        seen.append(prompt)
        return next(it)
    monkeypatch.setattr("builtins.input", fake_input)
    return seen


def test_live_mock_takes_a_jd_through_add_jd_tailor_and_review_and_never_touches_real_data(world, jd, monkeypatch, capsys):
    cfg, real, mock = world
    f = _inbox(mock, jd)
    before = _hash(real)
    seen = _answers(monkeypatch, "a")                                   # the only prompt: approve at the review menu
    cli.main(["--config", str(cfg), "live", "--mock", "--max", "1"])      # a clean run exits 0 (no SystemExit)
    out = capsys.readouterr().out
    assert "--mock implies --dry-run" in out and "Tailoring 1 job(s) with MockLLM" in out
    assert len(seen) == 1 and seen[0].startswith("[a]pprove  [e]dit  [l]etter regenerate")
    assert "[r]eject" not in seen[0]
    db = DB(mock / "careerops.db")
    (job,) = [j for j in db.jobs() if j["source"] == "manual"]
    assert job["status"] == "approved" and job["out_dir"].startswith(str(mock / "outputs"))
    assert f.exists()                                                      # a dry run keeps the JD for the real run
    assert "approved (dry run)" in next((mock / "live").glob("*.md")).read_text()
    assert _hash(real) == before                                           # real paths: byte for byte unchanged
    assert not (real / "careerops.db").exists()


def _kw(job) -> list[str]:
    return [k["term"] for k in json.loads(job["keywords"])]


def test_live_retailor_reuses_the_jobs_first_keywords_and_re_extract_redoes_them(world, jd, monkeypatch, capsys):
    cfg, real, mock = world
    _inbox(mock, jd)
    llm = FlipLLM()                                                    # a different keyword answer on every call
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: llm)
    db = DB(mock / "careerops.db")

    def live(*flags):
        _answers(monkeypatch, "a")                                      # approve at the review menu
        cli.main(["--config", str(cfg), "live", "--mock", "--max", "1", *flags])
        out = capsys.readouterr().out
        (job,) = [j for j in DB(mock / "careerops.db").jobs() if j["source"] == "manual"]
        return out, job
    out1, first = live()
    out2, second = live("--retailor")
    out3, third = live("--retailor")
    assert llm.extractions == 1 and "Tailoring 1 job(s)" in out2 and "Tailoring 1 job(s)" in out3
    assert _kw(first) == _kw(second) == _kw(third)
    assert (first["score"], first["ceiling"]) == (second["score"], second["ceiling"]) == (third["score"], third["ceiling"])
    assert "reusing the extraction frozen on" in out2 and "reusing the extraction frozen on" not in out1
    out4, fourth = live("--retailor", "--re-extract")
    assert llm.extractions == 2 and "extracting again (--re-extract)" in out4 and _kw(fourth) != _kw(first)


def test_live_mock_skip_marks_the_job_skipped_and_leaves_the_jd_because_mock_is_a_dry_run(world, jd, monkeypatch, capsys):
    cfg, real, mock = world
    _inbox(mock, jd)
    _answers(monkeypatch, "s")
    cli.main(["--config", str(cfg), "live", "--mock"])
    db = DB(mock / "careerops.db")
    (job,) = [j for j in db.jobs() if j["source"] == "manual"]
    assert job["status"] == "skipped"
    assert [p.name for p in (mock / "jds" / "inbox").iterdir()] == ["northwind.txt"]      # dry run: never filed
    assert not (mock / "jds" / "done").exists() or not list((mock / "jds" / "done").iterdir())


def test_live_mock_ctrl_c_exits_130_and_the_rerun_resumes(world, jd, monkeypatch, capsys):
    cfg, real, mock = world
    _inbox(mock, jd)

    def interrupt(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "live", "--mock", "--max", "3"])
    assert e.value.code == 130
    out = capsys.readouterr().out
    assert "STOPPED at stage 'review'" in out and f"Resume with:  careerops --config {cfg} live --max 3 --mock" in out
    _answers(monkeypatch, "s")
    cli.main(["--config", str(cfg), "live", "--mock", "--max", "3"])
    assert "Tailoring" not in capsys.readouterr().out                      # resumed at review: not tailored a second time
    assert [j["status"] for j in DB(mock / "careerops.db").jobs() if j["source"] == "manual"] == ["skipped"]


def test_a_tailored_job_skipped_in_review_is_not_reset_by_retriage(world, jd, monkeypatch):
    cfg, real, mock = world
    _inbox(mock, jd)
    _answers(monkeypatch, "s")
    cli.main(["--config", str(cfg), "live", "--mock"])
    from careerops.cli import retriage
    from careerops.config import Settings
    s = mockroot.mock_settings(Settings(raw=yaml.safe_load(cfg.read_text()), root=Path(__file__).resolve().parents[1]))
    db = DB(mock / "careerops.db")
    jid = next(j["id"] for j in db.jobs() if j["source"] == "manual")
    counts = retriage(db, s)
    assert db.job(jid)["status"] == "skipped" and counts["post_tailor"] >= 1


# ------------------------------------------------------------------ the real apply wiring, with only the browser stubbed
def test_live_applies_through_the_real_preflight_and_archive_when_you_say_you_submitted(world, jd, monkeypatch, capsys):
    from careerops.apply import assist as assist_mod
    from careerops.llm import MockLLM
    cfg, real, mock = world
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: MockLLM())
    monkeypatch.setattr(assist_mod, "assist", lambda job, rep, applicant, profile, **kw: {
        "confirmation": "REF-42", "visa": [{"question": "Do you require sponsorship?", "value": "Yes"}], "filled": [],
        "manual": [], "uploaded": [], "required_empty": []})
    inbox = real / "jds" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "northwind.txt").write_text(JD_HEAD + jd, encoding="utf-8")
    old = _hash(real / "Companies" / "Old Co")
    seen = _answers(monkeypatch, "a", "y", "y")                  # approve, open the form, I submitted
    cli.main(["--config", str(cfg), "live", "--max", "1"])       # exit 0: no SystemExit
    out = capsys.readouterr().out
    assert [p.strip().split(" ")[0] for p in seen][-2:] == ["Open", "Did"] and "CHECK VISA ANSWER: Do you require sponsorship? -> Yes" in out
    db = DB(real / "careerops.db")
    (job,) = db.jobs()
    assert job["status"] == "applied" and job["confirmation"] == "REF-42"
    folder = real / "Auto" / "Northwind Health AI" / "AI Engineer"          # written to paths.applications_auto
    assert job["application_dir"] == str(folder) and (folder / "JD.txt").is_file() and (folder / "job.json").is_file()
    assert _hash(real / "Companies" / "Old Co") == old                              # the old submission is untouched
    assert sorted(p.name for p in (real / "Companies").iterdir()) == ["Old Co"]     # paths.applications got nothing new
    summary = next((real / "live").glob("*.md")).read_text()
    assert "- status: applied" in summary and "- archive check: ok (" in summary
    assert not (inbox / "northwind.txt").exists() and len(list((real / "jds" / "done").iterdir())) == 1


def test_live_does_not_archive_when_you_did_not_submit(world, jd, monkeypatch, capsys):
    from careerops.apply import assist as assist_mod
    from careerops.llm import MockLLM
    cfg, real, mock = world
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: MockLLM())
    monkeypatch.setattr(assist_mod, "assist", lambda *a, **k: {"confirmation": "", "visa": []})
    inbox = real / "jds" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "northwind.txt").write_text(JD_HEAD + jd, encoding="utf-8")
    _answers(monkeypatch, "a", "y", "n")
    cli.main(["--config", str(cfg), "live", "--max", "1"])
    (job,) = DB(real / "careerops.db").jobs()
    assert job["status"] == "ready_not_submitted" and job["applied_at"] is None and job["application_dir"] is None
    assert sorted(p.name for p in (real / "Companies").iterdir()) == ["Old Co"] and list((real / "Auto").iterdir()) == []
