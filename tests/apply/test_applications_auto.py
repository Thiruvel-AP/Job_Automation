"""paths.applications is the user's own folder of past submissions: READ only (history, company list, duplicate check). The pipeline writes its
archive to paths.applications_auto and nowhere else; both are read. Everything here runs against tmp_path."""
import os
from pathlib import Path

import pytest
import yaml
from test_archive import fake_pdf_text, hand_made, make_job, snapshot       # noqa: F401  (fixtures and helpers of the archive tests)
from test_autogate import names as failed_gates
from test_autogate import run as run_gate
from test_live import Fake, archive, inbox, put, session, yes               # noqa: F401  (fixtures and helpers of the live tests)

from careerops import ats_structure, autoapply, cli, mockroot
from careerops.apply import autogate
from careerops.apply.archive import preflight, record_application
from careerops.apply.errors import ArchiveError
from careerops.apply.live import EXIT_ARCHIVE
from careerops.config import ROOT, Settings
from careerops.discovery import discover, seeds
from careerops.setup import init


@pytest.fixture(autouse=True)
def pdf_is_fine(monkeypatch):
    monkeypatch.setattr(autogate, "pdf_problems", lambda *a, **k: [])
    # v3.3: the structural ATS check reads a real PDF; it has its own tests (tests/test_ats_structure_v33.py)
    monkeypatch.setattr(ats_structure, "for_report", lambda *a, **k: ats_structure.JobStructure(ats_structure.Structure("cv", 100), None, []))


# ------------------------------------------------------------------ the setting
def test_a_settings_file_without_the_key_writes_under_data_never_to_the_users_folder():
    s = Settings(raw={"paths": {"applications": "/home/u/Companies"}}, root=Path("/repo"))
    assert s.path("applications_auto") == Path("/repo/data/applications_auto")
    assert s.path("applications") == Path("/home/u/Companies") and s.archive_roots() == [s.path("applications"), s.path("applications_auto")]


def test_a_key_that_is_set_is_used_and_one_root_is_listed_once(settings, tmp_path):
    assert settings.path("applications_auto") == tmp_path / "Auto" and settings.archive_roots() == [tmp_path / "Companies", tmp_path / "Auto"]
    settings.raw["paths"]["applications_auto"] = settings.raw["paths"]["applications"]
    assert settings.archive_roots() == [tmp_path / "Companies"]


def test_an_unknown_paths_key_is_still_an_error():
    with pytest.raises(KeyError):
        Settings(raw={"paths": {}}, root=Path("/repo")).path("nonsense")


def test_mock_runs_redirect_applications_auto_even_when_the_settings_file_leaves_it_out(settings, tmp_path):
    del settings.raw["paths"]["applications_auto"]
    settings.raw["paths"]["mock_root"] = str(tmp_path / "mock")
    m = mockroot.mock_settings(settings)
    assert m.path("applications_auto") == tmp_path / "mock" / "applications_auto"
    assert m.path("applications") == tmp_path / "mock" / "applications"
    assert not m.path("applications_auto").is_relative_to(ROOT / "data" / "applications_auto")


def test_init_creates_the_pipelines_folder_and_never_the_users_own(settings, tmp_path):
    for k in ("register_dir", "companies_house_dir", "browser_profile", "logs", "learning"):
        settings.raw["paths"][k] = str(tmp_path / "data" / k)
    settings.raw["paths"]["applications_auto"] = str(tmp_path / "Fresh" / "Auto")
    settings.raw["paths"]["applications"] = str(tmp_path / "Fresh" / "Own")
    made = init(settings)
    assert (tmp_path / "Fresh" / "Auto").is_dir() and not (tmp_path / "Fresh" / "Own").exists()
    assert any("Auto" in m for m in made) and not any("Own" in m for m in made)


# ------------------------------------------------------------------ writing
def test_an_application_is_written_to_applications_auto_and_the_users_folder_is_untouched(settings, db, make_job):
    own = settings.path("applications")
    hand_made(own, "Lendable/Old Role/CV.pdf")
    hand_made(own, "Acme/Data Scientist/CV.pdf")
    before = snapshot(own)
    job, rep = make_job(company="Lendable", title="AI Engineer")
    res = record_application(db, settings, job, rep)
    assert res.folder == settings.path("applications_auto") / "Lendable" / "AI Engineer"
    assert (res.folder / "JD.txt").is_file() and db.job(job["id"])["application_dir"] == str(res.folder)
    assert snapshot(own) == before                                           # same company name in both: still nothing added to yours
    assert sorted(p.name for p in own.iterdir()) == ["Acme", "Lendable"]


def test_the_preflight_reads_applications_auto_for_its_root(settings, db, make_job):
    job, rep = make_job()
    assert preflight(db, settings, job, rep).root == settings.path("applications_auto")


@pytest.mark.parametrize("auto", ["Companies/pipeline", "."])
def test_an_applications_auto_inside_or_around_the_users_folder_is_refused_before_anything_is_written(settings, db, make_job, tmp_path, auto):
    job, rep = make_job()
    target = tmp_path / auto if auto != "." else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    settings.raw["paths"]["applications_auto"] = str(target)
    before = snapshot(tmp_path / "Companies")
    with pytest.raises(ArchiveError, match="must not contain each other"):
        preflight(db, settings, job, rep)
    with pytest.raises(ArchiveError):
        record_application(db, settings, job, rep)
    assert snapshot(tmp_path / "Companies") == before and db.job(job["id"])["status"] == "approved"


def test_a_missing_applications_auto_is_refused_and_not_created_by_the_archive(settings, db, make_job, tmp_path):
    job, rep = make_job()
    settings.raw["paths"]["applications_auto"] = str(tmp_path / "NotMadeYet")
    with pytest.raises(ArchiveError, match="careerops init"):
        record_application(db, settings, job, rep)
    assert not (tmp_path / "NotMadeYet").exists()


# ------------------------------------------------------------------ reading: both folders
def test_the_duplicate_check_reads_both_folders(settings, db):
    (settings.path("applications") / "Acme Ltd" / "Data Scientist").mkdir(parents=True)
    assert failed_gates(run_gate(settings, db)) == ["duplicates"]
    (settings.path("applications") / "Acme Ltd").rename(settings.path("applications") / "Gone")
    assert run_gate(settings, db).ok
    (settings.path("applications_auto") / "Acme Ltd" / "Data Scientist").mkdir(parents=True)
    g = run_gate(settings, db, job={"id": "greenhouse:acme-2"})
    assert failed_gates(g) == ["duplicates"] and "Acme Ltd" in g.line()


def test_the_discover_duplicate_list_and_the_company_seeds_read_both_folders(settings):
    (settings.path("applications") / "Monzo").mkdir()
    (settings.path("applications_auto") / "Wise" / "AI Engineer").mkdir(parents=True)
    (settings.path("applications_auto") / "Monzo").mkdir()                                     # in both: listed once
    pairs = discover.archive_pairs(settings.archive_roots())
    assert ("Wise", "AI Engineer") in pairs
    assert seeds.archive_names(settings.archive_roots()) == ["Monzo", "Wise"]
    assert seeds.archive_names(settings.path("applications")) == ["Monzo"]                     # one folder still works
    assert discover.archive_pairs(settings.path("applications_auto")) == [("Wise", "AI Engineer")]
    assert discover.archive_pairs([settings.path("applications"), settings.path("applications") / "missing"]) == []


def test_a_company_in_either_folder_counts_as_seen_before(settings, db):
    seen = autoapply.default_history(db, settings)
    assert seen("Zeta Ltd") is False
    (settings.path("applications_auto") / "Zeta Ltd" / "AI Engineer").mkdir(parents=True)
    assert autoapply.default_history(db, settings)("zeta ltd") is True


def test_import_archive_lists_both_folders_and_writes_neither(settings, db, tmp_path, capsys):
    hand_made(settings.path("applications"), "Old Co/Data Scientist/CV.pdf")
    hand_made(settings.path("applications_auto"), "New Co/AI Engineer/CV.pdf")
    before = (snapshot(settings.path("applications")), snapshot(settings.path("applications_auto")))
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "outcomes", "--import-archive"])
    out = capsys.readouterr().out
    assert "Old Co / Data Scientist" in out and "New Co / AI Engineer" in out
    assert (snapshot(settings.path("applications")), snapshot(settings.path("applications_auto"))) == before


# ------------------------------------------------------------------ live: the user's folder is checked unchanged too
class TouchesYours(Fake):
    """A step that, besides the one allowed archive write, touches the user's own folder."""
    what = "stray"

    def mark_applied(self, jid, confirmation):
        done = super().mark_applied(jid, confirmation)
        own = self.s.path("applications")
        if self.what == "stray":
            (own / "stray.txt").write_text("something else wrote here")
        elif self.what == "modify":
            (own / "Old Co" / "CV.pdf").write_bytes(b"quietly edited")
        else:
            (own / "Old Co" / "CV.pdf").unlink()
        return done


@pytest.mark.parametrize("what,line", [("stray", "STRAY    Companies/stray.txt"), ("modify", "MODIFIED Companies/Old Co/CV.pdf"),
                                       ("delete", "REMOVED  Companies/Old Co/CV.pdf")])
def test_a_change_in_the_users_folder_stops_the_session_even_when_the_pipelines_archive_is_fine(db, settings, inbox, archive, jd, what, line):
    hand_made(settings.path("applications"), "Old Co/CV.pdf", b"submitted")
    put(inbox, "1.txt", jd, company="Acme", n=1)
    put(inbox, "2.txt", jd, company="Beta", n=2)
    fake = TouchesYours(db, settings, yes())
    fake.what = what
    live, lines = session(db, settings, fake)
    assert live.run() == EXIT_ARCHIVE
    assert live.results[0].archive.startswith("VIOLATION") and line in "\n".join(lines)
    assert db.jobs()[1]["status"] == "needs_review"                                              # the second job was never applied to


def test_live_never_moves_a_jd_into_or_out_of_either_folder(db, settings, inbox, archive, jd):
    victim = hand_made(settings.path("applications"), "Old Co/CV.pdf")
    f = put(inbox, "x.txt", jd)
    live, _ = session(db, settings, Fake(db, settings))
    with pytest.raises(RuntimeError, match="archive is never touched"):
        live._move(victim, inbox, "y.txt")
    with pytest.raises(RuntimeError, match="archive is never touched"):
        live._move(f, settings.path("applications"), "x.txt")
    assert victim.read_bytes() == b"hand-made CV" and f.exists() and not (settings.path("applications") / "x.txt").exists()
    assert os.path.exists(victim)
