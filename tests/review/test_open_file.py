"""Review's `o`: in WSL a PDF is opened by Windows' explorer.exe with the path `wslpath -w` gives; elsewhere as before."""
import subprocess

import pytest

from careerops.review import workflow as review

ALL = ("wslpath", "explorer.exe", "wslview", "xdg-open")


class Spy:
    def __init__(self, monkeypatch, *, wsl=True, tools=ALL, wslpath_out="D:\\x\\CV.pdf\n", wslpath_fails=False):
        self.popen, self.run, self.printed = [], [], []
        monkeypatch.setattr(review, "in_wsl", lambda: wsl)
        monkeypatch.setattr(review.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
        monkeypatch.setattr(review, "EXPLORER_FALLBACK", "/nonexistent/explorer.exe")       # never the real one

        def run(cmd, **kw):
            self.run.append(cmd)
            if wslpath_fails:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=wslpath_out, stderr="")
        monkeypatch.setattr(review.subprocess, "run", run)
        monkeypatch.setattr(review.subprocess, "Popen", lambda cmd, *a, **k: self.popen.append(cmd))
        monkeypatch.setattr(review.platform, "system", lambda: "Linux")
        monkeypatch.setattr("builtins.print", lambda *a, **k: self.printed.append(" ".join(map(str, a))))


def test_in_wsl_the_pdf_is_opened_by_explorer_with_the_windows_path(monkeypatch):
    spy = Spy(monkeypatch)
    review.open_file("/mnt/d/VS_Codes/out/CV.pdf")
    assert spy.run == [["wslpath", "-w", "/mnt/d/VS_Codes/out/CV.pdf"]]
    assert spy.popen == [["/usr/bin/explorer.exe", "D:\\x\\CV.pdf"]]               # not wslview, not xdg-open


def test_explorer_is_preferred_over_wslview(monkeypatch):
    spy = Spy(monkeypatch, tools=("wslpath", "explorer.exe", "wslview"))
    review.open_file("/mnt/d/a.pdf")
    assert [c[0] for c in spy.popen] == ["/usr/bin/explorer.exe"]


@pytest.mark.parametrize("kw", [dict(wsl=False), dict(tools=("wslview",)), dict(tools=("wslpath", "wslview")),
                                dict(wslpath_fails=True), dict(wslpath_out="\n")])
def test_without_wsl_or_a_tool_or_a_windows_path_the_old_behaviour_is_used(monkeypatch, kw):
    spy = Spy(monkeypatch, **{"tools": ALL, **kw})
    review.open_file("/mnt/d/a.pdf")
    assert spy.popen == [["wslview", "/mnt/d/a.pdf"]]


def test_a_plain_linux_box_uses_xdg_open_and_with_nothing_prints_the_path(monkeypatch):
    spy = Spy(monkeypatch, wsl=False, tools=("xdg-open",))
    review.open_file("/home/me/a.pdf")
    assert spy.popen == [["xdg-open", "/home/me/a.pdf"]]
    spy = Spy(monkeypatch, wsl=False, tools=())
    review.open_file("/home/me/a.pdf")
    assert spy.popen == [] and spy.printed == ["  open manually: /home/me/a.pdf"]


def test_explorer_exits_1_on_success_and_that_code_is_never_read(monkeypatch):
    Spy(monkeypatch)
    assert review.wsl_open("/mnt/d/a.pdf") is True              # Popen only: no wait(), no returncode check


@pytest.mark.parametrize("env, version, want", [({"WSL_DISTRO_NAME": "Ubuntu"}, None, True), ({"WSL_INTEROP": "/run/x"}, None, True),
                                                ({}, "Linux version 5.15.153.1-microsoft-standard-WSL2", True),
                                                ({}, "Linux version 6.8.0-generic", False)])
def test_in_wsl_detection(monkeypatch, tmp_path, env, version, want):
    for var in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    proc = tmp_path / "version"
    if version is not None:
        proc.write_text(version, encoding="utf-8")
    real_path = review.Path
    monkeypatch.setattr(review, "Path", lambda p: proc if p == "/proc/version" else real_path(p))
    assert review.in_wsl() is want
