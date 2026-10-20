"""Review: the `Apply: <kind> <url>` line, the `LEVEL:` flag, and `[w]`, which opens the apply page in the default Windows browser (wslview,
else explorer.exe). [w] only opens a page: it never clicks, fills or submits, and the pipeline makes no request of its own to it."""
import subprocess

import pytest
import requests

from careerops.review import workflow as review


class Opener:
    def __init__(self, monkeypatch, tools=("wslview", "explorer.exe")):
        self.popen, self.printed = [], []
        monkeypatch.setattr(review.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
        monkeypatch.setattr(review, "EXPLORER_FALLBACK", "/nonexistent/explorer.exe")
        monkeypatch.setattr(review.subprocess, "Popen", lambda cmd, *a, **k: self.popen.append(cmd))
        monkeypatch.setattr(review.subprocess, "run", lambda *a, **k: pytest.fail("[w] runs nothing but the opener"))
        monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("[w] makes no request of its own"))
        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("[w] makes no request of its own"))
        monkeypatch.setattr("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a))))


def test_wslview_opens_the_page_in_the_default_windows_browser(monkeypatch):
    o = Opener(monkeypatch)
    assert review.open_url("https://job-boards.greenhouse.io/x/jobs/1") is True
    assert o.popen == [["wslview", "https://job-boards.greenhouse.io/x/jobs/1"]]


def test_without_wslview_explorer_exe_opens_it(monkeypatch):
    o = Opener(monkeypatch, tools=("explorer.exe",))
    assert review.open_url("https://jobs.lever.co/a/b") is True
    assert o.popen == [["/usr/bin/explorer.exe", "https://jobs.lever.co/a/b"]]


def test_without_either_the_link_is_printed_to_open_by_hand(monkeypatch):
    o = Opener(monkeypatch, tools=())
    assert review.open_url("https://jobs.lever.co/a/b") is False
    assert o.popen == [] and o.printed == ["  open manually: https://jobs.lever.co/a/b"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "", "not a link"])
def test_only_web_links_are_ever_handed_to_a_program(monkeypatch, url):
    o = Opener(monkeypatch)
    assert review.open_url(url) is False
    assert o.popen == []


def test_a_job_with_no_page_says_so_in_the_apply_line():
    from careerops.discovery import applykind
    assert applykind.line(None).startswith("Apply: none")


# ------------------------------------------------------------------ the review screen (renders a CV: slow)
@pytest.mark.slow
def test_the_screen_shows_the_apply_line_and_the_level_and_w_opens_the_page(tailored, settings, db, job, monkeypatch, capsys):
    _, fb, _ = tailored
    db.update_job(job["id"], title="Senior AI Engineer", apply_kind="greenhouse", apply_url="https://job-boards.greenhouse.io/x/jobs/1")
    opened = []
    monkeypatch.setattr(review, "open_url", lambda url: opened.append(url) or True)
    answers = iter(["w", "s"])
    prompts = []
    out = review.review_job(db, db.job(job["id"]), settings, fb, ask=lambda p="": prompts.append(p) or next(answers))
    text = capsys.readouterr().out
    assert out == "skipped" and opened == ["https://job-boards.greenhouse.io/x/jobs/1"]
    assert "Apply: greenhouse https://job-boards.greenhouse.io/x/jobs/1" in text
    assert "LEVEL: senior" in text and "x L 0.60" in text
    assert "[w] open apply page" in prompts[0]
    assert db.job(job["id"])["status"] == "needs_review"                       # opening a page changes nothing


@pytest.mark.slow
def test_w_on_a_job_without_a_page_prints_that_and_opens_nothing(tailored, settings, db, job, monkeypatch, capsys):
    _, fb, _ = tailored
    db.update_job(job["id"], url=None, apply_url=None)
    monkeypatch.setattr(review, "open_url", lambda url: pytest.fail("nothing to open"))
    answers = iter(["w", "s"])
    review.review_job(db, db.job(job["id"]), settings, fb, ask=lambda p="": next(answers))
    assert "no application page" in capsys.readouterr().out
