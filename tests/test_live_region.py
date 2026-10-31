"""`careerops live` with a region and a size bucket: an approved ad outside the region (or in the other size bucket) is left alone, untouched, and a single ad named with
--url / --clipboard that is outside the region asks `continue? [y/N]` (auto, or no terminal: always no). No network, no browser, nothing is tailored here."""
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from careerops import cli, mockroot
from careerops.apply.live import Live
from careerops.discovery import intake
from triage_fakes import write_ad


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")


@pytest.fixture
def world(tmp_path, settings):
    """A config file with a mock root under tmp_path; returns (config path, the mock settings)."""
    settings.raw["paths"]["mock_root"] = str(tmp_path / "mock")
    settings.raw["apply"] = {**settings.raw["apply"], "mode": "assist"}
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    mock = mockroot.mock_settings(settings)
    mockroot.ensure_dirs(mock)
    return str(cfg), mock


def ad(folder: Path, name: str, company: str, place: str, **kw) -> Path:
    p = write_ad(folder, name, company=company, **kw)
    p.write_text(p.read_text(encoding="utf-8").replace("Location: London, UK", f"Location: {place}"), encoding="utf-8")
    return p


def approved(mock, name, company, place, **kw) -> Path:
    return ad(mock.path("jds") / "approved" / "2026-10-31", name, company, place, **kw)


# ------------------------------------------------------------------ the session's files
def test_the_gate_leaves_a_file_alone_and_says_why(world, db):
    _, mock = world
    lines = []
    live = Live(db, mock, None, out=lines.append, job_filter=lambda f: "outside United Kingdom: x" if "ny" in f.name else None)
    a, b = Path("a.txt"), Path("ny.txt")
    assert live._gated([a, b]) == [a] and lines == ["  left alone: ny.txt: outside United Kingdom: x"]
    assert Live(db, mock, None).job_filter is None and Live(db, mock, None)._gated([a, b]) == [a, b]


def test_an_approved_ad_outside_the_region_is_left_alone(world, capsys):
    cfg, mock = world
    ny = approved(mock, "ny.txt", "Newyork", "New York, NY")
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk"])
    out = capsys.readouterr().out
    assert "region: uk (from --region flag)" in out and "left alone: ny.txt: outside United Kingdom" in out
    assert ny.exists() and "Nothing approved" in out                       # untouched, still approved; nothing was tailored


def test_the_same_ad_is_worked_on_when_the_region_covers_it(world, capsys, monkeypatch):
    cfg, mock = world
    approved(mock, "ny.txt", "Newyork", "New York, NY")
    seen = []
    monkeypatch.setattr(Live, "_by_priority", lambda self, files: seen.extend(files) or [])         # stop before anything is tailored
    cli.main(["--config", cfg, "live", "--mock", "--region", "us"])
    out = capsys.readouterr().out
    assert "left alone" not in out and [p.name for p in seen] == ["ny.txt"]
    assert "non-UK region: visa/right-to-work not checked" in out


def test_an_ad_in_the_other_size_bucket_is_left_alone_and_an_unknown_one_passes(world, capsys, monkeypatch, settings):
    cfg, mock = world
    mock.path("company_size_overrides").write_text("Acme: small\nBigco: large\n", encoding="utf-8")
    approved(mock, "acme.txt", "Acme", "London, UK")
    approved(mock, "bigco.txt", "Bigco", "London, UK")
    approved(mock, "mystery.txt", "Mystery", "London, UK")
    seen = []
    monkeypatch.setattr(Live, "_by_priority", lambda self, files: seen.extend(files) or [])
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk", "--size", "sme"])
    out = capsys.readouterr().out
    assert sorted(p.name for p in seen) == ["acme.txt", "mystery.txt"]
    assert "left alone: bigco.txt: company size Large is not in the chosen bucket (sme)" in out
    seen.clear()
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk", "--size", "large"])
    assert sorted(p.name for p in seen) == ["bigco.txt", "mystery.txt"]
    seen.clear()
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk", "--size", "both"])
    assert len(seen) == 3


def test_live_never_asks_the_size_and_uses_the_last_choice(world, capsys, monkeypatch):
    from careerops import choices
    cfg, mock = world
    choices.write_state(mock, size="large", size_asked="2020-01-01")
    mock.path("company_size_overrides").write_text("Acme: small\n", encoding="utf-8")
    approved(mock, "acme.txt", "Acme", "London, UK")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"a prompt appeared: {prompt!r}"))
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk"])
    assert "left alone: acme.txt: company size SME·small is not in the chosen bucket (large)" in capsys.readouterr().out


def test_live_parks_the_database_queue_outside_the_region(world, capsys):
    cfg, mock = world
    from careerops.db import DB
    db = DB(mock.path("db"))
    db.upsert_job({"id": "x:ny", "source": "manual", "company": "Newyork", "title": "Data Scientist", "location": "New York, NY", "description": "x"})
    db.update_job("x:ny", status="triaged")
    cli.main(["--config", cfg, "live", "--mock", "--region", "uk"])
    assert "parked (region)" in capsys.readouterr().out and db.job("x:ny")["status"] == "parked (region)"
    cli.main(["--config", cfg, "live", "--mock", "--region", "any"])
    assert db.job("x:ny")["status"] == "triaged"


# ------------------------------------------------------------------ one ad you named
@pytest.fixture
def named(world, monkeypatch):
    """`--clipboard` hands live one ad that is in New York."""
    cfg, mock = world
    path = ad(mock.path("jds") / "inbox", "ny.txt", "Newyork", "New York, NY")
    monkeypatch.setattr(intake, "add_clipboard", lambda s, ask: (path, True, SimpleNamespace(company="Newyork", title="Data Scientist", text="x" * 600)))
    return cfg, mock, path


def test_a_named_ad_outside_the_region_asks_and_a_no_stops(named, capsys, monkeypatch):
    cfg, mock, path = named
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": prompts.append(prompt) or "n")
    cli.main(["--config", cfg, "live", "--mock", "--clipboard", "--region", "uk"])
    assert prompts == ["This job is outside United Kingdom: continue? [y/N] "]
    out = capsys.readouterr().out
    assert "Not processed: this job is outside United Kingdom" in out and path.exists()


def test_in_auto_mode_the_answer_is_always_no(named, capsys, monkeypatch):
    cfg, mock, path = named
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"a prompt appeared: {prompt!r}"))
    cli.main(["--config", cfg, "live", "--mock", "--clipboard", "--region", "uk", "--auto", "--no-submit"])
    assert "auto mode never continues" in capsys.readouterr().out


def test_without_a_terminal_the_answer_is_no_too(named, capsys, monkeypatch):
    cfg, mock, path = named
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"a prompt appeared: {prompt!r}"))
    cli.main(["--config", cfg, "live", "--mock", "--clipboard", "--region", "uk"])
    assert "Not processed: this job is outside United Kingdom" in capsys.readouterr().out
