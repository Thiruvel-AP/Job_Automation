"""init/doctor, .env loading, schema, and the milestone-stub CLI contract."""
import os

import pytest

from careerops import cli
from careerops.db import DB
from careerops.setup import init, load_dotenv


def test_init_is_idempotent(settings, tmp_path, monkeypatch):
    for k in ("register_dir", "companies_house_dir", "outputs", "browser_profile", "logs"):
        settings.raw["paths"][k] = str(tmp_path / "data" / k)
    first = init(settings)
    assert any("register_dir" in m for m in first)
    assert all(settings.path(k).is_dir() for k in ("register_dir", "companies_house_dir", "outputs", "logs"))
    assert [m for m in init(settings) if "data" in m] == []


def test_dotenv_never_overrides(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nA_KEY="from-file"\nB_KEY=from-file\nEMPTY=\n')
    monkeypatch.setenv("B_KEY", "from-shell")
    monkeypatch.delenv("A_KEY", raising=False)
    assert load_dotenv(env) == 1
    assert os.environ["A_KEY"] == "from-file" and os.environ["B_KEY"] == "from-shell"


def test_schema_has_track_a_tables(db: DB):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "sponsors", "companies", "company_events", "register_snapshots", "llm_usage"} <= names


@pytest.mark.parametrize("argv", [["companies", "--snapshot"], ["companies", "--resolve"], ["learning"]])
def test_unbuilt_milestones_fail_cleanly(argv, settings, tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "settings.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump(settings.raw))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), *argv])
    assert e.value.code == 2 and "Not built yet" in capsys.readouterr().out
