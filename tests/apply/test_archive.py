"""A1 application archive. Rule 0: the archive root is append-only.

Everything here runs against tmp_path (the `settings` fixture redirects paths.applications). Most tests use small fake
PDFs and a stand-in for pdftotext; the end-to-end tests at the bottom use real xelatex/pdftotext output.
"""
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook

from careerops import cli
from careerops.apply import archive as arch
from careerops.apply.archive import record_application
from careerops.apply.assist import LABEL_JS, fill_page
from careerops.apply.errors import ArchiveError, ArchiveIncomplete, ArchiveRootMissing, LetterMissing, WrongFileError
from careerops.apply.naming import id6
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.reporting.excel import export
from careerops.tailoring.tailor import tailor_job

OLD = 1_700_000_000_000_000_000                       # a fixed, old mtime (ns) for hand-made fixture files
SEVEN = {"Ada Fixture CV.pdf", "Ada Fixture CV.tex", "Ada Fixture Cover Letter.pdf", "Ada Fixture Cover Letter.tex",
         "JD.txt", "job.json", "report.json"}


# ------------------------------------------------------------------ fixtures and helpers
@pytest.fixture(autouse=True)
def append_only_guard(settings, monkeypatch):
    """Any delete/rename/replace aimed at a path under the archive root fails the test on the spot."""
    root = settings.path("applications").resolve()

    def under(p) -> bool:
        if not isinstance(p, (str, bytes, os.PathLike)):
            return False
        r = Path(os.fsdecode(p)).resolve()
        return r == root or root in r.parents

    def wrap(owner, name, nargs):
        orig = getattr(owner, name)

        def guarded(*a, **k):
            for x in a[:nargs]:
                if under(x):
                    raise AssertionError(f"destructive {name}() on the archive root: {x}")
            return orig(*a, **k)
        monkeypatch.setattr(owner, name, guarded)

    for n, k in (("remove", 1), ("unlink", 1), ("rmdir", 1), ("rename", 2), ("replace", 2)):
        wrap(os, n, k)
    import shutil
    wrap(shutil, "rmtree", 1); wrap(shutil, "move", 2)
    for n, k in (("unlink", 1), ("rmdir", 1), ("rename", 2), ("replace", 2)):
        wrap(Path, n, k)


@pytest.fixture
def fake_pdf_text(monkeypatch):
    """Stand-in for pdftotext on the fake PDFs below (their 'text layer' is just their bytes)."""
    monkeypatch.setattr("careerops.apply.guard.pdf_text", lambda p: Path(p).read_bytes().decode("latin-1"))


@pytest.fixture
def make_job(settings, db, fake_pdf_text):
    def make(job_id="greenhouse:acme:7", company="Lendable", title="AI Engineer", letter=True, letter_company=None,
             description="JD text for the role"):
        out = settings.path("outputs") / re.sub(r"\W+", "_", job_id)
        out.mkdir(parents=True, exist_ok=True)
        cv, cvt = out / "Ada Fixture CV.pdf", out / "Ada Fixture CV.tex"
        cl, clt = out / "Ada Fixture Cover Letter.pdf", out / "Ada Fixture Cover Letter.tex"
        cv.write_bytes(f"%PDF-fake CV for {job_id}\n".encode()); cvt.write_text(f"cv tex {job_id}")
        if letter:
            cl.write_bytes(f"%PDF-fake letter\nDear {letter_company or company} hiring team,\n{job_id}\n".encode())
            clt.write_text(f"letter tex {job_id}")
        rep = {"job_id": job_id, "company": company, "title": title, "out_dir": str(out), "cv_pdf": str(cv),
               "letter_pdf": str(cl), "tier": 1, "headline": "AI Engineer", "ceiling": 0.9, "genuine_gaps": [],
               "letter": {"status": "ok", "problems": [], "score": 0.5, "paragraphs": ["One.", "Two."]},
               "final": {"score": 0.8, "score_stemmed": 0.85, "band": "in", "pages": 1, "hits": [],
                         "wording_misses": [], "misses": [], "chosen_attempt": 1},
               "attempts": [{"attempt": 1, "violations": [], "warnings": [], "selection": {}, "proposal": {}}],
               "context": {"profile": "Profile text"}, "keywords": []}
        (out / "report.json").write_text(json.dumps(rep), encoding="utf-8")
        db.upsert_job({"id": job_id, "source": job_id.split(":")[0], "company": company, "title": title,
                       "location": "London", "url": f"https://example.com/{job_id}", "apply_url": "https://example.com/apply",
                       "description": description})
        db.update_job(job_id, out_dir=str(out), status="approved", family="ai_engineer", tier=1, score=0.8,
                      sponsor_name=company.upper(), sponsor_score=0.97)
        return db.job(job_id), rep
    return make


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict:
    """{relative path: ('dir',) | ('file', size, mtime_ns, sha256)} for every entry under root."""
    snap = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            snap[rel] = ("dir",)
        else:
            st = p.stat()
            snap[rel] = ("file", st.st_size, st.st_mtime_ns, sha(p))
    return snap


def assert_only_additions(before: dict, after: dict) -> list[str]:
    for rel, was in before.items():
        assert rel in after, f"{rel} vanished"
        assert after[rel] == was, f"{rel} changed: {was} -> {after[rel]}"
    return sorted(set(after) - set(before))


def hand_made(root: Path, rel: str, data: bytes = b"hand-made CV") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, ns=(OLD, OLD))
    return p


def flags(db, job_id) -> list[str]:
    return json.loads(db.job(job_id)["flags"] or "[]")


def run_cli(settings, tmp_path, monkeypatch, *argv):
    cfg = tmp_path / "cli-settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: 0)           # never read the real .env in tests
    cli.main(["--config", str(cfg), *argv])


# ------------------------------------------------------------------ 1. new company
def test_new_company_creates_company_role_all_seven_files_and_index_rows(settings, db, make_job):
    root = settings.path("applications")
    job, rep = make_job()
    res = record_application(db, settings, job, rep, confirmation="REF-1")
    role = root / "Lendable" / "AI Engineer"
    assert res.folder == role and res.version == 1
    assert {p.name for p in role.iterdir()} == SEVEN
    assert (role / "Ada Fixture CV.pdf").read_bytes() == Path(rep["cv_pdf"]).read_bytes()
    assert (role / "JD.txt").read_text(encoding="utf-8") == "JD text for the role"
    assert json.loads((role / "report.json").read_text()) == rep
    meta = json.loads((role / "job.json").read_text(encoding="utf-8"))
    assert {"job_id", "url", "apply_url", "source", "family", "tier", "sponsor_name", "sponsor_score", "score",
            "applied_at", "confirmation"} <= set(meta)
    assert meta["job_id"] == job["id"] and meta["confirmation"] == "REF-1" and not meta.get("no_letter")
    row = db.job(job["id"])
    assert row["status"] == "applied" and row["application_dir"] == str(role) and row["confirmation"] == "REF-1"
    assert row["applied_at"]
    idx = {r["folder"]: dict(r) for r in db.conn.execute("SELECT * FROM archive_index")}
    assert idx["Lendable"]["kind"] == "company" and idx["Lendable"]["company_key"] == "lendable"
    assert idx["Lendable/AI Engineer"]["kind"] == "role" and idx["Lendable/AI Engineer"]["job_id"] == job["id"]
    assert idx["Lendable/AI Engineer"]["created_at"]
    ev = [dict(r) for r in db.conn.execute("SELECT * FROM events WHERE job_id=? AND stage='archive'", (job["id"],))]
    assert len(ev) == 1 and "Lendable/AI Engineer" in ev[0]["detail"]


def test_archive_writes_no_marker_files_and_leaves_no_staging(settings, db, make_job):
    job, rep = make_job()
    record_application(db, settings, job, rep)
    assert [p for p in settings.path("applications").rglob("*") if p.name.startswith(".")] == []
    assert not list(settings.path("tmp").glob("archive-*"))
    assert Path(rep["cv_pdf"]).exists()                    # copy, not move: the working copy stays


# ------------------------------------------------------------------ 2. company spelling
def test_existing_company_under_another_spelling_is_reused(settings, db, make_job):
    root = settings.path("applications")
    a, ra = make_job("gh:co:1", company="checkout.com", title="Data Scientist")
    record_application(db, settings, a, ra)
    b, rb = make_job("gh:co:2", company="Checkout.com Ltd", title="ML Engineer")
    res = record_application(db, settings, b, rb)
    assert [p.name for p in root.iterdir()] == ["checkout.com"]
    assert res.folder == root / "checkout.com" / "ML Engineer"
    assert flags(db, "gh:co:2") == []


def test_near_miss_creates_new_folder_and_flags_never_merges(settings, db, make_job):
    root = settings.path("applications")
    hand_made(root, "Blending Spoons/hand.pdf"); hand_made(root, "BCG X/hand.pdf"); hand_made(root, "CGI/hand.pdf")
    a, ra = make_job("gh:bs:1", company="Bending Spoons")
    b, rb = make_job("gh:bcg:1", company="BCG")
    c, rc = make_job("gh:cgi:1", company="CGI Group")
    for j, r in ((a, ra), (b, rb), (c, rc)):
        record_application(db, settings, j, r)
    assert (root / "Bending Spoons" / "AI Engineer").is_dir() and (root / "BCG" / "AI Engineer").is_dir()
    assert flags(db, "gh:bs:1") == ["Possible existing folder: Blending Spoons — merge by hand if same company"]
    assert flags(db, "gh:bcg:1") == ["Possible existing folder: BCG X — merge by hand if same company"]
    assert flags(db, "gh:cgi:1") == []                                  # ratio 0.55: no guess, no flag
    assert [p.name for p in (root / "Blending Spoons").iterdir()] == ["hand.pdf"]


# ------------------------------------------------------------------ 3. role naming
def test_role_naming_collisions_and_hand_made_folders_never_written_into(settings, db, make_job):
    root = settings.path("applications")
    hand = hand_made(root, "Lendable/Python Developer - AI/CV hand.pdf")
    d = date(2026, 10, 5)
    names = []
    for jid, title in (("gh:l:1", "Data Scientist"), ("gh:l:2", "Data Scientist"), ("gh:l:3", "Data Scientist"),
                       ("gh:l:4", "Python Developer - AI")):
        j, r = make_job(jid, company="Lendable", title=title)
        names.append(record_application(db, settings, j, r, applied_on=d).folder.name)
    assert names == ["Data Scientist", "Data Scientist – 2026-10-05", f"Data Scientist – {id6('gh:l:3')}",
                     "Python Developer - AI – 2026-10-05"]
    assert [p.name for p in hand.parent.iterdir()] == ["CV hand.pdf"] and hand.read_bytes() == b"hand-made CV"


def test_id6_collision_gets_numeric_suffix_and_multi_colon_ids_stay_ntfs_safe(settings, db, make_job):
    root = settings.path("applications")
    jid = "greenhouse:acme:7"
    for taken in ("Data Scientist", "Data Scientist – 2026-10-05", f"Data Scientist – {id6(jid)}"):
        (root / "Lendable" / taken).mkdir(parents=True)
    j, r = make_job(jid, company="Lendable", title="Data Scientist")
    res = record_application(db, settings, j, r, applied_on=date(2026, 10, 5))
    assert res.folder.name == f"Data Scientist – {id6(jid)}-2" and ":" not in res.folder.name


@pytest.mark.parametrize("title,expect", [
    ("Data Scientist / ML Engineer", "Data Scientist - ML Engineer"),
    ("AI: R&D?", "AI- R&D-"),
    ("CON", "CON_"),
    ("T" * 200, "T" * 80),
])
def test_unsafe_titles_become_one_valid_folder(settings, db, make_job, title, expect):
    root = settings.path("applications")
    j, r = make_job("gh:n:1", company="Lendable", title=title)
    res = record_application(db, settings, j, r)
    assert res.folder.parent == root / "Lendable" and res.folder.name == expect
    assert [p.name for p in (root / "Lendable").iterdir()] == [expect]
    assert not set('<>:"/\\|?*') & set(res.folder.name) and len(res.folder.name) <= 80


def test_company_name_is_sanitised_too(settings, db, make_job):
    root = settings.path("applications")
    j, r = make_job("gh:n:2", company="R/D: Labs*", title="AI Engineer")
    res = record_application(db, settings, j, r)
    assert res.folder.parent.name == "R-D- Labs-" and [p.name for p in root.iterdir()] == ["R-D- Labs-"]


def test_long_root_shortens_the_role_to_keep_every_path_within_240(settings, db, make_job, tmp_path):
    base = settings.path("applications")
    pad = 240 - len(str(base)) - 1 - 40 - 28 - len("Lendable") - 6
    deep = base / ("p" * (pad - 1))
    deep.mkdir()
    settings.raw["paths"]["applications"] = str(deep)
    j, r = make_job("gh:long:1", company="Lendable", title="T" * 200)
    res = record_application(db, settings, j, r)
    assert 12 <= len(res.folder.name) < 80
    assert max(len(str(p)) for p in deep.rglob("*")) <= 240


def test_path_too_long_even_for_a_short_role_is_refused_before_creating_anything(settings, db, make_job):
    base = settings.path("applications")
    deep = base / ("a" * 80) / ("b" * 80) / ("c" * 80)
    deep.mkdir(parents=True)
    settings.raw["paths"]["applications"] = str(deep)
    j, r = make_job("gh:long:2", company="Lendable", title="AI Engineer")
    with pytest.raises(ArchiveError, match="240"):
        record_application(db, settings, j, r)
    assert list(deep.iterdir()) == [] and db.job("gh:long:2")["status"] == "approved"


# ------------------------------------------------------------------ 4. resubmission
def test_same_job_archived_twice_goes_to_v2_v3_and_originals_are_unchanged(settings, db, make_job):
    j, r = make_job()
    first = record_application(db, settings, j, r)
    before = snapshot(first.folder)
    second = record_application(db, settings, db.job(j["id"]), r)
    third = record_application(db, settings, db.job(j["id"]), r)
    assert (second.folder, second.version) == (first.folder / "v2", 2)
    assert (third.folder, third.version) == (first.folder / "v3", 3)
    assert {p.name for p in second.folder.iterdir()} == SEVEN
    after = snapshot(first.folder)
    assert_only_additions(before, after)
    assert db.job(j["id"])["application_dir"] == str(first.folder / "v3")
    idx = {r_["folder"]: r_["job_id"] for r_ in db.conn.execute("SELECT folder, job_id FROM archive_index")}
    assert idx["Lendable/AI Engineer/v2"] == idx["Lendable/AI Engineer/v3"] == j["id"]


# ------------------------------------------------------------------ 5. adoption
def test_unmarked_hand_made_company_folder_is_adopted_without_touching_it(settings, db, make_job):
    root = settings.path("applications")
    hand = hand_made(root, "Checkout.com/old CV.pdf")
    before = snapshot(root)
    j, r = make_job("gh:co:9", company="Checkout.com Ltd", title="Data Scientist")
    res = record_application(db, settings, j, r)
    assert [p.name for p in root.iterdir()] == ["Checkout.com"]
    assert res.folder == root / "Checkout.com" / "Data Scientist"
    assert hand.read_bytes() == b"hand-made CV" and hand.stat().st_mtime_ns == OLD
    assert not (root / "Checkout.com" / ".company.json").exists()
    assert_only_additions(before, snapshot(root))
    row = db.conn.execute("SELECT * FROM archive_index WHERE folder='Checkout.com'").fetchone()
    assert row["kind"] == "company" and row["company_key"] == "checkoutcom"
    assert flags(db, "gh:co:9") == []


def test_two_unmarked_folders_with_the_same_key_use_the_first_and_flag(settings, db, make_job):
    root = settings.path("applications")
    hand_made(root, "Zone & Co/a.pdf"); hand_made(root, "Zone and Co/b.pdf")
    j, r = make_job("gh:z:1", company="Zone and Co")
    res = record_application(db, settings, j, r)
    assert res.folder.parent.name == "Zone & Co"
    assert flags(db, "gh:z:1") == ["Ambiguous company folders: Zone & Co, Zone and Co"]
    assert [p.name for p in (root / "Zone and Co").iterdir()] == ["b.pdf"]


# ------------------------------------------------------------------ 6. root must exist
def test_missing_archive_root_is_refused_and_nothing_is_created(settings, db, make_job, tmp_path):
    j, r = make_job()
    gone = tmp_path / "NotThere" / "Companies"
    settings.raw["paths"]["applications"] = str(gone)
    with pytest.raises(ArchiveRootMissing, match="careerops init"):
        record_application(db, settings, j, r)
    assert not gone.exists() and not gone.parent.exists() and not settings.path("tmp").exists()
    assert db.job(j["id"])["status"] == "approved" and db.job(j["id"])["application_dir"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0] == 0


# ------------------------------------------------------------------ 7. mark-applied
def test_mark_applied_archives_without_the_assist_and_date_controls_the_folder(settings, db, make_job, tmp_path,
                                                                              monkeypatch):
    root = settings.path("applications")
    a, _ = make_job("gh:m:1", company="Lendable", title="Data Scientist")
    b, _ = make_job("gh:m:2", company="Lendable", title="Data Scientist")
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:m:1", "--ref", "EMAIL-7")
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:m:2", "--date", "2026-03-05")
    assert (root / "Lendable" / "Data Scientist").is_dir()
    assert (root / "Lendable" / "Data Scientist – 2026-03-05").is_dir()
    one, two = db.job("gh:m:1"), db.job("gh:m:2")
    assert one["status"] == two["status"] == "applied" and one["confirmation"] == "EMAIL-7"
    assert two["applied_at"].startswith("2026-03-05")
    assert json.loads((root / "Lendable" / "Data Scientist" / "job.json").read_text())["confirmation"] == "EMAIL-7"
    assert load_workbook(settings.path("excel"))["Jobs"].max_row == 3          # export refreshed


def test_mark_applied_logs_an_applied_outcome_dated_like_the_application_once(settings, db, make_job, tmp_path,
                                                                             monkeypatch):
    make_job("gh:o:1", company="Lendable", title="Data Scientist")
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:o:1", "--date", "2026-03-05")
    rows = [tuple(r) for r in db.conn.execute("SELECT job_id, event, on_date, source FROM outcomes")]
    assert rows == [("gh:o:1", "applied", "2026-03-05", "mark-applied")]
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:o:1", "--date", "2026-03-09")   # a resubmission
    assert db.conn.execute("SELECT COUNT(*) FROM outcomes WHERE job_id='gh:o:1'").fetchone()[0] == 1


def test_mark_applied_unknown_job_or_untailored_job_exits_1(settings, db, make_job, tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "nope:1")
    assert e.value.code == 1
    db.upsert_job({"id": "x:1", "source": "x", "company": "Acme", "title": "T", "description": "d"})
    with pytest.raises(SystemExit) as e:
        run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "x:1")
    assert e.value.code == 1 and db.job("x:1")["status"] == "new"


def test_apply_command_archives_after_confirmation_but_not_after_x(settings, db, make_job, tmp_path, monkeypatch):
    root = settings.path("applications")
    a, ra = make_job("gh:a:1", company="Lendable", title="Data Scientist")
    b, rb = make_job("gh:a:2", company="Monzo", title="Data Scientist")
    answers = {"gh:a:1": "REF-A", "gh:a:2": "x"}
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    monkeypatch.setattr("careerops.apply.assist.assist",
                        lambda job, rep, applicant, profile: {"confirmation": answers[job["id"]], "uploaded": []})
    run_cli(settings, tmp_path, monkeypatch, "apply", "--id", "gh:a:1")
    run_cli(settings, tmp_path, monkeypatch, "apply", "--id", "gh:a:2")
    assert (root / "Lendable" / "Data Scientist" / "job.json").exists() and not (root / "Monzo").exists()
    assert db.job("gh:a:1")["status"] == "applied" and db.job("gh:a:1")["confirmation"] == "REF-A"
    assert db.job("gh:a:2")["status"] == "approved"


# ------------------------------------------------------------------ 8. interrupted archive
def test_interrupted_archive_changes_nothing_old_keeps_partial_folder_and_flags_job(settings, db, make_job,
                                                                                    monkeypatch):
    root = settings.path("applications")
    hand_made(root, "Acme/Existing Role/old.pdf")
    before = snapshot(root)
    j, r = make_job("gh:acme:9", company="Acme", title="AI Engineer")
    real, calls = arch.safe_copy, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(arch, "safe_copy", flaky)
    with pytest.raises(ArchiveIncomplete, match="disk full") as e:
        record_application(db, settings, j, r, confirmation="REF")
    partial = root / "Acme" / "AI Engineer"
    assert e.value.folder == partial and partial.is_dir() and len(list(partial.iterdir())) == 2
    added = assert_only_additions(before, snapshot(root))
    assert added[0] == "Acme/AI Engineer" and len(added) == 3                  # the folder and its two files only
    row = db.job(j["id"])
    assert row["status"] == "applied" and row["application_dir"] == str(partial) and row["confirmation"] == "REF"
    assert "Archive incomplete: Acme/AI Engineer — finish by hand" in flags(db, j["id"])
    assert db.conn.execute("SELECT kind FROM archive_index WHERE folder='Acme/AI Engineer'").fetchone()
    assert not list(settings.path("tmp").glob("archive-*"))


def test_mark_applied_exits_1_when_the_archive_is_incomplete(settings, db, make_job, tmp_path, monkeypatch):
    make_job("gh:acme:10", company="Acme")
    monkeypatch.setattr(arch, "safe_copy", lambda s, d: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(SystemExit) as e:
        run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:acme:10")
    assert e.value.code == 1 and db.job("gh:acme:10")["status"] == "applied"


# ------------------------------------------------------------------ 9. visibility
def test_excel_shows_application_folder_for_applied_rows_only(settings, db, make_job):
    a, ra = make_job("gh:x:1", company="Lendable")
    make_job("gh:x:2", company="Monzo")
    res = record_application(db, settings, a, ra)
    db.update_job("gh:x:2", application_dir="/should/not/show")                  # stale value on a non-applied row
    export(db, settings.path("excel"))
    ws = load_workbook(settings.path("excel"))["Jobs"]
    head = [c.value for c in ws[1]]
    col, st = head.index("Application folder") + 1, head.index("Status") + 1
    got = {ws.cell(r, st).value: ws.cell(r, col).value for r in range(2, ws.max_row + 1)}
    assert got["applied"] == str(res.folder) and not got["approved"]


def test_status_lists_archived_applications_per_company_top_10(settings, db, make_job, tmp_path, monkeypatch, capsys):
    for i in range(2):
        j, r = make_job(f"gh:s:{i}", company="Lendable", title=f"Role {i}")
        record_application(db, settings, j, r)
    for i in range(11):
        j, r = make_job(f"gh:t:{i}", company=f"Company {i:02d}")
        record_application(db, settings, j, r)
    run_cli(settings, tmp_path, monkeypatch, "status")
    out = capsys.readouterr().out
    rows = [ln for ln in out.split("Archived applications")[1].splitlines()[1:] if ln.strip()]
    assert len(rows) == 10 and "Lendable" in rows[0] and rows[0].split()[0] == "2"


def test_init_creates_a_missing_archive_root_and_never_touches_an_existing_one(settings, tmp_path):
    from careerops.setup import init
    for k in ("register_dir", "companies_house_dir", "browser_profile", "logs", "learning"):
        settings.raw["paths"][k] = str(tmp_path / "data" / k)
    root = settings.path("applications")
    keep = hand_made(root, "Acme/cv.pdf")
    init(settings)
    assert keep.read_bytes() == b"hand-made CV"
    settings.raw["paths"]["applications"] = str(tmp_path / "Fresh" / "Companies")
    assert any("Companies" in m for m in init(settings)) and (tmp_path / "Fresh" / "Companies").is_dir()


# ------------------------------------------------------------------ 10. wrong-file guard
class FakeEl:
    def __init__(self, page, tag, typ, label):
        self.page, self.tag, self.typ, self.label, self.value = page, tag, typ, label, ""

    def evaluate(self, js):
        if js == LABEL_JS:
            return self.label
        return self.tag.upper() if "tagName" in js else None

    def get_attribute(self, name):
        return {"type": self.typ if self.tag == "input" else None}.get(name)

    def is_visible(self): return True
    def is_checked(self): return False
    def input_value(self): return self.value
    def fill(self, v): self.value = v
    def set_input_files(self, p): self.page.uploads.append(p)


class FakePage:
    def __init__(self, *fields):
        self.uploads: list[str] = []
        self.els = [FakeEl(self, *f) for f in fields]

    def query_selector_all(self, _sel):
        return self.els


APPLICANT = {"never_fill": ["cover letter", "salary"], "label_map": {}}      # same shape as applicant.yaml


def test_fill_page_uploads_cv_and_letter_and_the_job_guard_passes(settings, db, make_job):
    j, r = make_job()
    page = FakePage(("input", "file", "Resume / CV"), ("input", "file", "Cover letter"))
    fill_page(page, APPLICANT, r["cv_pdf"], r["letter_pdf"], job=j)
    assert page.uploads == [r["cv_pdf"], r["letter_pdf"]]


def test_wrong_jobs_letter_is_refused_and_nothing_is_uploaded_or_archived(settings, db, make_job):
    root = settings.path("applications")
    a, ra = make_job("gh:g:1", company="Lendable")
    b, rb = make_job("gh:g:2", company="Monzo")
    page = FakePage(("input", "file", "Resume / CV"), ("input", "file", "Cover letter"))
    with pytest.raises(WrongFileError):
        fill_page(page, APPLICANT, ra["cv_pdf"], rb["letter_pdf"], job=a)          # inside the wrong out_dir
    assert page.uploads == []
    with pytest.raises(WrongFileError):
        fill_page(page, APPLICANT, rb["cv_pdf"], ra["letter_pdf"], job=a)          # wrong CV
    assert page.uploads == []
    bad = dict(ra, letter_pdf=rb["letter_pdf"])
    with pytest.raises(WrongFileError):
        record_application(db, settings, a, bad)
    assert list(root.iterdir()) == [] and db.job("gh:g:1")["status"] == "approved"
    assert not settings.path("tmp").exists() or not list(settings.path("tmp").iterdir())


def test_letter_for_another_company_inside_this_out_dir_is_refused(settings, db, make_job):
    a, ra = make_job("gh:g:3", company="Lendable", letter_company="Monzo")          # copied from another job's letter
    with pytest.raises(WrongFileError, match="Lendable"):
        record_application(db, settings, a, ra)
    with pytest.raises(WrongFileError):
        fill_page(FakePage(("input", "file", "Cover letter")), APPLICANT, ra["cv_pdf"], ra["letter_pdf"], job=a)
    assert list(settings.path("applications").iterdir()) == []


def test_paths_outside_the_jobs_out_dir_are_refused_even_with_the_right_company(settings, db, make_job, tmp_path):
    a, ra = make_job("gh:g:4", company="Lendable")
    stray = tmp_path / "stray CV.pdf"; stray.write_bytes(b"%PDF Dear Lendable hiring team")
    with pytest.raises(WrongFileError, match="out_dir"):
        record_application(db, settings, a, dict(ra, cv_pdf=str(stray)))
    with pytest.raises(WrongFileError, match="out_dir"):
        record_application(db, settings, a, dict(ra, letter_pdf=str(stray)))


def test_company_match_ignores_case_whitespace_and_punctuation_but_fails_closed(settings, db, make_job, monkeypatch):
    a, ra = make_job("gh:g:5", company="Mind's  Eye AI")
    Path(ra["letter_pdf"]).write_bytes(b"%PDF Dear Minds\n eye ai hiring team")
    record_application(db, settings, a, ra)                                             # passes
    b, rb = make_job("gh:g:6", company="Lendable")
    monkeypatch.setattr("careerops.apply.guard.pdf_text", lambda p: "")              # pdftotext missing/blank
    with pytest.raises(WrongFileError):
        record_application(db, settings, b, rb)


# ------------------------------------------------------------------ 11. letters
def test_record_application_without_a_letter_raises_letter_missing(settings, db, make_job):
    j, r = make_job(letter=False)
    with pytest.raises(LetterMissing):
        record_application(db, settings, j, r)
    assert list(settings.path("applications").iterdir()) == [] and db.job(j["id"])["status"] == "approved"


def test_a_job_approved_with_no_letter_is_archived_without_the_no_letter_flag(settings, db, make_job):
    j, r = make_job("gh:nl:0", company="Lendable", letter=False)
    r["letter"] = {"status": "none", "problems": ["new numbers [9876]"], "score": None, "paragraphs": [], "pages": 0}
    r["letter_pdf"] = None                              # letter.on_failure: none; the user typed 'no letter' in review
    res = record_application(db, settings, j, r)
    meta = json.loads((res.folder / "job.json").read_text())
    assert meta["no_letter"] is True and meta["letter_status"] == "none" and db.job("gh:nl:0")["status"] == "applied"
    assert {p.name for p in res.folder.iterdir()} == {"Ada Fixture CV.pdf", "Ada Fixture CV.tex", "JD.txt", "job.json",
                                                      "report.json"}


def test_a_job_whose_letter_is_off_is_archived_without_a_letter_and_without_the_no_letter_flag(settings, db, make_job):
    j, r = make_job("gh:off:0", company="Lendable", letter=False)
    r["letter"] = {"status": "off", "problems": [], "score": None, "paragraphs": [], "pages": 0, "reason": "letter.mode: off"}
    r["letter_pdf"] = None                              # letter.mode: off: no letter was asked for, and nothing has to be typed
    res = record_application(db, settings, j, r)
    meta = json.loads((res.folder / "job.json").read_text())
    assert meta["no_letter"] is True and meta["letter_status"] == "off" and db.job("gh:off:0")["status"] == "applied"
    assert {p.name for p in res.folder.iterdir()} == {"Ada Fixture CV.pdf", "Ada Fixture CV.tex", "JD.txt", "job.json",
                                                      "report.json"}


def test_a_missing_letter_file_is_still_refused_when_the_report_says_the_letter_is_ok(settings, db, make_job):
    j, r = make_job("gh:nl:00", company="Lendable", letter=False)       # report says status "ok" but the PDF is gone
    with pytest.raises(LetterMissing):
        record_application(db, settings, j, r)


def test_mark_applied_without_letter_exits_1_and_no_letter_overrides_and_records_it(settings, db, make_job, tmp_path,
                                                                                    monkeypatch):
    root = settings.path("applications")
    make_job("gh:nl:1", company="Lendable", letter=False)
    with pytest.raises(SystemExit) as e:
        run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:nl:1")
    assert e.value.code == 1 and db.job("gh:nl:1")["status"] == "approved" and list(root.iterdir()) == []
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:nl:1", "--no-letter")
    role = root / "Lendable" / "AI Engineer"
    assert {p.name for p in role.iterdir()} == {"Ada Fixture CV.pdf", "Ada Fixture CV.tex", "JD.txt", "job.json",
                                                "report.json"}
    assert json.loads((role / "job.json").read_text())["no_letter"] is True
    ev = [r["detail"] for r in db.conn.execute("SELECT detail FROM events WHERE job_id='gh:nl:1' AND stage='archive'")]
    assert any('"no_letter": true' in d for d in ev) and db.job("gh:nl:1")["status"] == "applied"


def test_apply_refuses_a_job_without_a_letter_before_opening_any_form(settings, db, make_job, tmp_path, monkeypatch):
    make_job("gh:nl:2", company="Lendable", letter=False)
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("asked to open a form for a job with no letter"))
    monkeypatch.setattr("careerops.apply.assist.assist", lambda *a, **k: pytest.fail("assist ran"))
    with pytest.raises(SystemExit) as e:
        run_cli(settings, tmp_path, monkeypatch, "apply", "--id", "gh:nl:2")
    assert e.value.code == 1 and db.job("gh:nl:2")["status"] == "approved"


def test_fill_page_puts_the_letter_in_a_cover_letter_text_box_even_though_it_is_in_never_fill(settings, db, make_job):
    j, r = make_job()
    page = FakePage(("textarea", "textarea", "Cover letter"), ("textarea", "textarea", "Expected salary"),
                    ("input", "file", "Resume"))
    rep = fill_page(page, APPLICANT, r["cv_pdf"], r["letter_pdf"], letter_text="Dear Lendable,\n\nOne.\n\nTwo.", job=j)
    assert page.els[0].value == "Dear Lendable,\n\nOne.\n\nTwo." and page.els[1].value == ""
    assert page.uploads == [r["cv_pdf"]] and any("cover" in x.lower() for x in rep["filled"])


def test_fill_page_leaves_a_filled_cover_letter_box_alone_and_handles_forms_with_neither(settings, db, make_job):
    j, r = make_job()
    page = FakePage(("textarea", "textarea", "Cover letter"))
    page.els[0].value = "already typed"
    fill_page(page, APPLICANT, r["cv_pdf"], r["letter_pdf"], letter_text="LETTER", job=j)
    assert page.els[0].value == "already typed"
    none_page = FakePage(("input", "text", "Anything else"))
    fill_page(none_page, APPLICANT, r["cv_pdf"], r["letter_pdf"], letter_text="LETTER", job=j)
    assert none_page.uploads == [] and none_page.els[0].value == ""
    res = record_application(db, settings, j, r)                              # the letter is archived regardless
    assert (res.folder / "Ada Fixture Cover Letter.pdf").exists()


# ------------------------------------------------------------------ 12. THE append-only guarantee
def test_every_archive_scenario_leaves_the_existing_tree_untouched(settings, db, make_job, tmp_path, monkeypatch):
    root = settings.path("applications")
    for rel in ("83zero Ltd/Python Developer - AI/CV hand.pdf", "83zero Ltd/notes.txt",
                "Checkout.com/Data Scientist/CV hand.pdf", "Checkout.com/cover.docx",
                "Zone & Co/a.pdf", "Zone and Co/b.pdf", "Blending Spoons/AI Engineer/x.pdf",
                "BCG X/y.pdf", "Lendable/Data Scientist/z.pdf", "Lendable/loose file.txt"):
        hand_made(root, rel, rel.encode())
    (root / "Empty Co").mkdir()
    before = snapshot(root)

    scenarios = [
        ("gh:1", "Newco", "AI Engineer"),                       # brand-new company
        ("gh:2", "Checkout.com Ltd", "Data Scientist"),         # adopted folder, hand-made role exists
        ("gh:3", "Zone and Co", "Data Scientist"),              # ambiguous companies
        ("gh:4", "Bending Spoons", "AI Engineer"),              # near-miss
        ("gh:5", "83zero", "Python Developer - AI"),            # hand-made role collision
        ("gh:6", "Lendable", "Data Scientist"),                 # collides with hand-made, then date, then id6
        ("gh:7", "Lendable", "Data Scientist"),
        ("gh:8", "Lendable", "Data Scientist"),
        ("gh:9", "Odd Co", "Data Scientist / ML Engineer"),     # unsafe names
        ("gh:10", "Odd Co", "AI: R&D?"),
        ("gh:11", "Odd Co", "CON"),
        ("gh:12", "BCG", "Analyst"),
    ]
    made = {jid: make_job(jid, company=c, title=t) for jid, c, t in scenarios}
    for jid, *_ in scenarios:
        record_application(db, settings, *made[jid], applied_on=date(2026, 10, 5))
    j1 = made["gh:1"]
    record_application(db, settings, db.job("gh:1"), j1[1])                          # v2
    record_application(db, settings, db.job("gh:1"), j1[1])                          # v3
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:12", "--date", "2026-03-05")   # resubmission, via CLI
    no_letter, _ = make_job("gh:13", company="Newco", title="Other", letter=False)
    run_cli(settings, tmp_path, monkeypatch, "mark-applied", "--id", "gh:13", "--no-letter")
    wrong_a, ra = make_job("gh:14", company="Newco", title="Wrong")
    _, rb = make_job("gh:15", company="Other", title="Wrong")
    with pytest.raises(WrongFileError):
        record_application(db, settings, wrong_a, dict(ra, letter_pdf=rb["letter_pdf"]))
    with monkeypatch.context() as m:                                                 # interrupted copy
        real, n = arch.safe_copy, {"n": 0}
        def flaky(s, d):
            n["n"] += 1
            if n["n"] == 2:
                raise OSError("pulled the plug")
            return real(s, d)
        m.setattr(arch, "safe_copy", flaky)
        j16 = make_job("gh:16", company="Newco", title="Interrupted")
        with pytest.raises(ArchiveIncomplete):
            record_application(db, settings, *j16)
    refused = make_job("gh:17", company="Newco", title="No root yet")
    with monkeypatch.context() as m:                                                 # missing root: refused, nothing created
        m.setitem(settings.raw["paths"], "applications", str(tmp_path / "missing"))
        with pytest.raises(ArchiveRootMissing):
            record_application(db, settings, *refused)

    after = snapshot(root)
    added = assert_only_additions(before, after)
    assert added and all(not p.rsplit("/", 1)[-1].startswith(".") for p in added)    # no marker files anywhere
    for rel, was in before.items():                                                  # explicit: size, mtime and hash
        if was[0] == "file":
            st = (root / rel).stat()
            assert (st.st_size, st.st_mtime_ns, sha(root / rel)) == was[1:]
    assert {p.name for p in root.iterdir()} >= {"83zero Ltd", "Checkout.com", "Zone & Co", "Zone and Co", "Newco"}
    assert "Bending Spoons" in {p.name for p in root.iterdir()}                      # new folder beside the hand-made one


# ------------------------------------------------------------------ 13. end to end with the real toolchain
def _fixture_fb(settings, name):
    fb = load_yaml(settings.path("fact_bank"))                 # facts only; the name is overridden in memory
    return {**fb, "identity": {**fb["identity"], "name": name}}


@pytest.mark.slow
def test_real_pdfs_full_style_names_upload_equals_archive_in_name_and_bytes(settings, db, job):
    fb = _fixture_fb(settings, "Ada Quincy Lovelace")
    db.upsert_job(job)
    rep = tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    db.update_job(job["id"], out_dir=rep["out_dir"], status="approved")
    out = Path(rep["out_dir"])
    assert Path(rep["cv_pdf"]).name == "Ada Quincy Lovelace CV.pdf"
    assert Path(rep["letter_pdf"]).name == "Ada Quincy Lovelace Cover Letter.pdf"
    assert sorted(p.name for p in out.iterdir() if p.name != "llm_raw") == sorted(      # llm_raw: the model's raw replies
        ["Ada Quincy Lovelace CV.pdf", "Ada Quincy Lovelace CV.tex", "Ada Quincy Lovelace Cover Letter.pdf",
         "Ada Quincy Lovelace Cover Letter.tex", "report.json"])            # no cv.pdf / letter.pdf left behind
    page = FakePage(("input", "file", "Resume / CV"), ("input", "file", "Cover letter"))
    fill_page(page, APPLICANT, rep["cv_pdf"], rep["letter_pdf"], job=db.job(job["id"]))     # real pdftotext guard
    res = record_application(db, settings, db.job(job["id"]), rep)
    assert len(page.uploads) == 2
    for uploaded in page.uploads:
        archived = res.folder / Path(uploaded).name
        assert archived.name == Path(uploaded).name and sha(archived) == sha(Path(uploaded))
    assert res.folder == settings.path("applications") / "Northwind Health AI" / "AI Engineer"


@pytest.mark.slow
def test_real_pdfs_short_style_uses_first_word_of_the_identity(settings, db, job):
    settings.raw["files"]["name_style"] = "short"
    fb = _fixture_fb(settings, "Ada Quincy Lovelace")
    db.upsert_job(job)
    rep = tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    assert Path(rep["cv_pdf"]).name == "Ada CV.pdf" and Path(rep["letter_pdf"]).name == "Ada Cover Letter.pdf"


@pytest.mark.slow
def test_real_pdfs_identity_with_slash_and_colon_is_sanitised(settings, db, job):
    fb = _fixture_fb(settings, "Ada/Q: Lovelace")
    db.upsert_job(job)
    rep = tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    assert Path(rep["cv_pdf"]).name == "Ada-Q- Lovelace CV.pdf" and Path(rep["cv_pdf"]).exists()
