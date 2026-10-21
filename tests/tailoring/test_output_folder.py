"""The output folder of a tailored job: `<Company>_<Role, truncated>_<job id>` with the id never cut, and a swap that survives a
Windows lock (the drive is NTFS, reached from WSL, where a rename of a folder can fail with PermissionError)."""
import os
from pathlib import Path

import pytest

from careerops.llm import MockLLM
from careerops.config import load_yaml
from careerops.tailoring import tailor as tailor_mod
from careerops.tailoring.tailor import folder_name, tailor_job


def job_of(company: str, title: str, jid: str) -> dict:
    return {"id": jid, "company": company, "title": title}


def test_the_job_id_is_the_end_of_the_folder_name_and_is_never_truncated():
    name = folder_name(job_of("Graphcore Early Careers", "Graduate Machine Learning Engineer 2027 - Bristol or Cambridge", "ashby:graphcore:2027"))
    assert name.endswith("_2027") and name.startswith("Graphcore_Early_Careers_Graduate_Machine_Learning_")
    long_id = "e3b0c442-98fc-1c14-9afb-f4c8996fb924"
    name = folder_name(job_of("Graphcore Early Careers", "Graduate Machine Learning Engineer " * 3, f"ashby:graphcore-early-careers:{long_id}"))
    assert name.endswith("_" + long_id.replace("-", "_"))


def test_a_short_name_is_what_it_always_was():
    assert folder_name(job_of("Limbic", "Senior AI ML Engineer", "manual:177a9a1afcc1")) == "Limbic_Senior_AI_ML_Engineer_177a9a1afcc1"
    assert folder_name(job_of("Northwind Health AI", "AI Engineer", "manual:northwind1")) == "Northwind_Health_AI_AI_Engineer_northwind1"


def test_two_long_titles_at_one_company_get_two_folders():
    title = "Early Careers Graduate Machine Learning Engineer Programme in the Research Group"
    a = folder_name(job_of("Graphcore", title + " 2026", "greenhouse:graphcore:6001"))
    b = folder_name(job_of("Graphcore", title + " 2027", "greenhouse:graphcore:6002"))
    assert a != b and a.endswith("_6001") and b.endswith("_6002")
    assert max(len(a), len(b)) <= 60 + 1 + len("6001")


def make_work(root: Path, name: str = "Acme_Role_1") -> tuple[Path, Path]:
    work = root / (name + ".partial")
    (work / "llm_raw").mkdir(parents=True)
    (work / "CV.pdf").write_bytes(b"cv")
    (work / "report.json").write_text("{}", encoding="utf-8")
    (work / "llm_raw" / "a.json").write_text("{}", encoding="utf-8")
    return work, root / name


def locked_rename(monkeypatch, waits):
    real = os.replace

    def replace(src, dst, *a, **k):
        if str(src).endswith(".partial"):
            raise PermissionError(13, "Permission denied", str(src), None, str(dst))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(tailor_mod.os, "replace", replace)
    monkeypatch.setattr(tailor_mod.time, "sleep", waits.append)


def test_a_rename_that_works_is_a_plain_rename(tmp_path):
    work, final = make_work(tmp_path)
    tailor_mod.swap_in(work, final, tmp_path, out=lambda m: None)
    assert (final / "CV.pdf").read_bytes() == b"cv" and not work.exists()


def test_a_locked_rename_is_retried_three_times_a_second_apart_then_the_files_are_copied(tmp_path, monkeypatch):
    work, final = make_work(tmp_path)
    waits, said = [], []
    locked_rename(monkeypatch, waits)
    tailor_mod.swap_in(work, final, tmp_path, out=said.append)
    assert waits == [1.0, 1.0, 1.0]
    assert sorted(p.relative_to(final).as_posix() for p in final.rglob("*") if p.is_file()) == ["CV.pdf", "llm_raw/a.json", "report.json"]
    assert (final / "CV.pdf").read_bytes() == b"cv" and not work.exists()
    assert not (tmp_path / "Acme_Role_1.old").exists()
    assert any("copied" in m for m in said)


def test_the_file_windows_still_holds_is_named(tmp_path, monkeypatch):
    work, final = make_work(tmp_path)
    locked_rename(monkeypatch, [])
    real_rmtree, said = tailor_mod.shutil.rmtree, []

    def rmtree(path, *a, **k):
        for p in Path(path).rglob("*"):                    # everything goes but the PDF a viewer has open
            if p.is_file() and p.name != "CV.pdf":
                p.unlink()
        for p in sorted(Path(path).rglob("*"), reverse=True):
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    monkeypatch.setattr(tailor_mod.shutil, "rmtree", rmtree)
    tailor_mod.swap_in(work, final, tmp_path, out=said.append)
    assert (final / "CV.pdf").is_file()
    assert any("CV.pdf" in m and "locked" in m for m in said)


def test_an_earlier_folder_is_replaced_even_when_windows_will_not_move_it_aside(tmp_path, monkeypatch):
    work, final = make_work(tmp_path)
    final.mkdir()
    (final / "CV.pdf").write_bytes(b"old")
    real = os.replace
    monkeypatch.setattr(tailor_mod.os, "replace", lambda s, d, *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied", str(s)))
                        if str(s) in (str(final), str(work)) else real(s, d, *a, **k))
    monkeypatch.setattr(tailor_mod.time, "sleep", lambda s: None)
    tailor_mod.swap_in(work, final, tmp_path, out=lambda m: None)
    assert (final / "CV.pdf").read_bytes() == b"cv" and (final / "report.json").is_file() and not work.exists()


@pytest.mark.slow
def test_a_locked_rename_still_ends_in_a_complete_output_folder(settings, db, job, monkeypatch):
    fb = load_yaml(settings.path("fact_bank"))
    locked_rename(monkeypatch, [])
    rep = tailor_job(job, fb, MockLLM(), settings, settings.path("outputs"))
    out = Path(rep["out_dir"])
    assert out.name == folder_name(job) and Path(rep["cv_pdf"]).is_file() and (out / "report.json").is_file()
    assert not (out.parent / (out.name + ".partial")).exists()
