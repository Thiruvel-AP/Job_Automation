"""The archive snapshot is read-only and tells a permitted addition from deletion, modification and a stray file."""
import os
from pathlib import Path

from careerops.apply import snapshot as snap


def _archive(tmp_path) -> Path:
    root = tmp_path / "Companies"
    (root / "Old Co" / "Data Scientist").mkdir(parents=True)
    (root / "Old Co" / "Data Scientist" / "CV.pdf").write_bytes(b"submitted")
    (root / "Old Co" / "Data Scientist" / "JD.txt").write_bytes(b"jd")
    return root


def test_an_unchanged_archive_is_ok(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    assert set(a) == {"Old Co", "Old Co/Data Scientist", "Old Co/Data Scientist/CV.pdf", "Old Co/Data Scientist/JD.txt"}
    assert a["Old Co/Data Scientist/CV.pdf"][:2] == ("f", 9)
    c = snap.compare(a, snap.take(root))
    assert c.ok and c.summary() == "ok (unchanged)"


def test_taking_a_snapshot_changes_nothing(tmp_path):
    root = _archive(tmp_path)
    before = {p: os.lstat(p).st_mtime_ns for p in root.rglob("*")}
    snap.take(root)
    assert {p: os.lstat(p).st_mtime_ns for p in root.rglob("*")} == before


def test_a_missing_root_is_an_empty_snapshot(tmp_path):
    assert snap.take(tmp_path / "nope") == {}


def test_a_removed_file_is_found(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Old Co" / "Data Scientist" / "JD.txt").unlink()
    c = snap.compare(a, snap.take(root))
    assert c.removed == ["Old Co/Data Scientist/JD.txt"] and not c.ok and "VIOLATION" in c.summary()


def test_a_removed_folder_is_found(tmp_path):
    root = tmp_path / "Companies"
    (root / "Empty Co").mkdir(parents=True)
    a = snap.take(root)
    (root / "Empty Co").rmdir()
    assert snap.compare(a, snap.take(root)).removed == ["Empty Co"]


def test_a_renamed_file_is_a_removal_and_a_stray_addition(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    os.rename(root / "Old Co" / "Data Scientist" / "CV.pdf", root / "Old Co" / "Data Scientist" / "CV2.pdf")
    c = snap.compare(a, snap.take(root))
    assert c.removed == ["Old Co/Data Scientist/CV.pdf"] and c.stray == ["Old Co/Data Scientist/CV2.pdf"]


def test_a_changed_size_or_mtime_is_a_modification(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Old Co" / "Data Scientist" / "CV.pdf").write_bytes(b"submitted, edited")        # size
    p = root / "Old Co" / "Data Scientist" / "JD.txt"
    os.utime(p, ns=(a["Old Co/Data Scientist/JD.txt"][2] + 10**9,) * 2)                      # mtime only
    c = snap.compare(a, snap.take(root))
    assert c.modified == ["Old Co/Data Scientist/CV.pdf", "Old Co/Data Scientist/JD.txt"]


def test_a_folder_whose_mtime_changes_because_of_an_addition_is_not_a_modification(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Old Co" / "New Role").mkdir()
    (root / "Old Co" / "New Role" / "CV.pdf").write_bytes(b"x")
    c = snap.compare(a, snap.take(root), allowed=("Old Co", "New Role"))
    assert c.ok and c.added == ["Old Co/New Role", "Old Co/New Role/CV.pdf"]


def test_a_new_company_folder_and_its_role_folder_are_the_one_allowed_addition(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Acme" / "AI Engineer" / "v2").mkdir(parents=True)
    (root / "Acme" / "AI Engineer" / "v2" / "CV.pdf").write_bytes(b"x")
    assert snap.compare(a, snap.take(root), allowed=("Acme", "AI Engineer")).ok


def test_any_other_addition_is_stray(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Acme" / "AI Engineer").mkdir(parents=True)
    (root / "Acme" / "AI Engineer" / "CV.pdf").write_bytes(b"x")
    (root / "notes.txt").write_text("stray at the root")
    (root / "Acme" / "loose.txt").write_text("stray in the company folder")
    (root / "Old Co" / "Data Scientist" / "extra.pdf").write_bytes(b"x")
    (root / "Other" / "Role").mkdir(parents=True)
    c = snap.compare(a, snap.take(root), allowed=("Acme", "AI Engineer"))
    assert sorted(c.stray) == ["Acme/loose.txt", "Old Co/Data Scientist/extra.pdf", "Other", "Other/Role", "notes.txt"]
    assert not c.ok


def test_when_nothing_was_archived_no_addition_at_all_is_allowed(tmp_path):
    root = _archive(tmp_path)
    a = snap.take(root)
    (root / "Acme").mkdir()
    assert snap.compare(a, snap.take(root), allowed=None).stray == ["Acme"]


def test_role_folder_is_read_from_the_applications_dir(tmp_path):
    root = tmp_path / "Companies"
    assert snap.role_folder(root, root / "Acme" / "AI Engineer") == ("Acme", "AI Engineer")
    assert snap.role_folder(root, root / "Acme" / "AI Engineer" / "v2") == ("Acme", "AI Engineer")
    assert snap.role_folder(root, root / "Acme") is None and snap.role_folder(root, tmp_path / "elsewhere" / "a" / "b") is None
    assert snap.role_folder(root, None) is None
