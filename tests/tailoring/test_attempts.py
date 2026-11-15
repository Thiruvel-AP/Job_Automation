"""13 Nov 2026 ([r] took a job from ATS strict 59.1 to 63.4 and back to 59.1): a re-tailoring never replaces an earlier output. Every later one is
`<job>/attempts/<n>/`, the report names the attempt with the highest strict ATS (a tie keeps the earlier one), the database follows it, and nothing is
deleted, moved or overwritten. No model and no xelatex: `_tailor` is replaced by a fake that writes a report and a CV file."""
from __future__ import annotations

from pathlib import Path

import pytest

from careerops import cli, pathstore
from careerops.tailoring import attempts, letter_reuse
from careerops.tailoring import tailor as tailor_mod
from careerops.tailoring.tailor import folder_name, tailor_job


def fake_tailor(strictness: list[float | None]):
    """A stand-in for `_tailor` that scores the successive runs `strictness` (None = a report without an ATS block)."""
    runs = iter(strictness)

    def fake(job, fb, llm, settings, out_dir, final_dir, *a, **k):
        strict = next(runs)
        out_dir.mkdir(parents=True)
        (out_dir / "CV.pdf").write_text(f"cv scoring {strict}")
        rep = {"out_dir": str(final_dir), "cv_pdf": str(final_dir / "CV.pdf"), "ats": {"strict": strict} if strict is not None else {}}
        pathstore.write_report(out_dir / "report.json", rep)
        return rep
    return fake


JOB = {"id": "manual:j1", "company": "Acme", "title": "Data Scientist"}


def run(tmp_path, monkeypatch, scores: list[float | None]) -> list[dict]:
    monkeypatch.setattr(tailor_mod, "_tailor", fake_tailor(scores))
    return [tailor_job(JOB, {}, None, {}, tmp_path) for _ in scores]


def test_the_first_tailoring_is_the_job_folder_and_later_ones_are_numbered_attempts(tmp_path, monkeypatch):
    reps = run(tmp_path, monkeypatch, [59.1, 63.4, 59.1])
    job_dir = tmp_path / folder_name(JOB)
    assert [r["attempt"] for r in reps] == [0, 1, 2]
    assert [Path(r["out_dir"]) for r in reps] == [job_dir, job_dir / "attempts" / "1", job_dir / "attempts" / "2"]
    assert attempts.numbers(job_dir) == [0, 1, 2] and attempts.next_number(job_dir) == 3


def test_a_lower_re_tailoring_keeps_current_attempt_on_the_earlier_one_and_both_folders_stay(tmp_path, monkeypatch):
    first, second, third = run(tmp_path, monkeypatch, [63.4, 59.1, 63.4])
    job_dir = tmp_path / folder_name(JOB)
    assert (first["current_attempt"], second["current_attempt"]) == (0, 0)          # 59.1 < 63.4: attempt 0 stays current
    assert third["current_attempt"] == 0                                             # a tie keeps the earlier one
    assert (job_dir / "CV.pdf").read_text() == "cv scoring 63.4" and (job_dir / "attempts" / "1" / "CV.pdf").read_text() == "cv scoring 59.1"
    assert (job_dir / "attempts" / "2" / "CV.pdf").is_file()
    assert attempts.current_number(job_dir) == 0 and attempts.current_dir(job_dir) == job_dir
    on_disk = pathstore.read_report(job_dir / "attempts" / "1" / "report.json")
    assert on_disk["attempt"] == 1 and on_disk["current_attempt"] == 0


def test_a_higher_re_tailoring_becomes_current_and_the_earlier_one_is_untouched(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, [59.1])
    job_dir = tmp_path / folder_name(JOB)
    before = (job_dir / "report.json").read_bytes()
    monkeypatch.setattr(tailor_mod, "_tailor", fake_tailor([63.4, 60.0]))
    better = tailor_job(JOB, {}, None, {}, tmp_path)
    worse = tailor_job(JOB, {}, None, {}, tmp_path)
    assert better["current_attempt"] == 1 and worse["current_attempt"] == 1
    assert attempts.current_dir(job_dir) == job_dir / "attempts" / "1"
    assert (job_dir / "report.json").read_bytes() == before and (job_dir / "CV.pdf").read_text() == "cv scoring 59.1"


def test_a_failed_re_tailoring_leaves_every_earlier_attempt_whole(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, [63.4, 59.1])
    job_dir = tmp_path / folder_name(JOB)

    def boom(job, fb, llm, settings, out_dir, final_dir, *a, **k):
        out_dir.mkdir(parents=True)
        raise RuntimeError("xelatex")
    monkeypatch.setattr(tailor_mod, "_tailor", boom)
    with pytest.raises(RuntimeError):
        tailor_job(JOB, {}, None, {}, tmp_path)
    assert attempts.numbers(job_dir) == [0, 1] and (job_dir / "CV.pdf").read_text() == "cv scoring 63.4"
    assert not (tmp_path / (folder_name(JOB) + ".partial")).exists()


def test_an_attempt_number_is_never_reused_even_when_its_folder_has_no_report(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, [50.0])
    job_dir = tmp_path / folder_name(JOB)
    (job_dir / "attempts" / "4").mkdir(parents=True)
    assert attempts.next_number(job_dir) == 5
    rep = run(tmp_path, monkeypatch, [51.0])[0]
    assert rep["attempt"] == 5 and (job_dir / "attempts" / "4").is_dir()


def test_placing_an_attempt_refuses_an_existing_folder(tmp_path):
    work = tmp_path / "x.partial"
    work.mkdir()
    dest = tmp_path / "job" / "attempts" / "1"
    dest.mkdir(parents=True)
    (dest / "CV.pdf").write_text("kept")
    with pytest.raises(FileExistsError):
        tailor_mod.place_attempt(work, dest, tmp_path)
    assert (dest / "CV.pdf").read_text() == "kept"


def test_a_report_without_a_score_loses_to_one_with_a_score(tmp_path, monkeypatch):
    reps = run(tmp_path, monkeypatch, [None, 40.0, None])
    assert [r["current_attempt"] for r in reps] == [0, 1, 1]


def test_the_choice_is_highest_strict_then_earliest():
    assert attempts.choose({0: 59.1, 1: 63.4, 2: 63.4}) == 1
    assert attempts.choose({0: 63.4, 1: 59.1}) == 0
    assert attempts.choose({0: None, 1: None}) == 0
    assert attempts.job_folder(Path("o/job/attempts/3")) == Path("o/job") and attempts.job_folder(Path("o/job")) == Path("o/job")


def test_a_lower_attempt_leaves_the_database_on_the_current_one(settings, db, job):
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir="outputs/job", score=0.634)
    row = db.job(job["id"])
    lower = {"attempt": 2, "current_attempt": 1, "out_dir": "outputs/job/attempts/2"}
    assert cli.record_tailoring(db, settings, row, lower) == "needs_review"
    after = db.job(job["id"])
    assert after["out_dir"] == "outputs/job" and after["score"] == 0.634


def test_other_jobs_letters_are_read_from_their_current_attempt(tmp_path, monkeypatch):
    body = {"status": "ok", "paragraphs": {"P1": "x"}}
    monkeypatch.setattr(letter_reuse, "body_of", lambda letter: [letter["text"]])
    job_dir = tmp_path / "Other_Job_1"
    for n, text in ((0, "the first letter"), (1, "the better letter")):
        folder = attempts.folder_of(job_dir, n)
        folder.mkdir(parents=True, exist_ok=True)
        pathstore.write_report(folder / "report.json", {"ats": {"strict": 50 + 10 * n}, "attempt": n, "current_attempt": 1 if n else 0,
                                                       "letter": {**body, "text": text}})
    got = list(letter_reuse._outputs(tmp_path, set()))
    assert [e.name for e in got] == ["Other_Job_1"] and got[0].text == "the better letter"


def test_a_lower_attempts_letter_is_not_compared_with_its_own_job(tmp_path):
    job_dir = tmp_path / "Acme_DS_1"
    assert attempts.job_name(job_dir / "attempts" / "2") == "Acme_DS_1" and attempts.job_name(job_dir) == "Acme_DS_1"
