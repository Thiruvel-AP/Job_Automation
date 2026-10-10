"""init/doctor, .env loading, schema, and the milestone-stub CLI contract."""
import os

import pytest

from careerops import cli
from careerops.db import DB
from careerops.setup import fact_bank_open_items, init, load_dotenv


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


# `companies --snapshot` is built (M1, tests/discovery/test_register_snapshots.py, with HTTP mocked); running it here would
# reach the real GOV.UK site.
@pytest.mark.parametrize("argv", [["companies", "--queue"], ["learning"]])     # --resolve is built since M3 (tests/discovery/test_ats.py)
def test_unbuilt_milestones_fail_cleanly(argv, settings, tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "settings.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump(settings.raw))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), *argv])
    assert e.value.code == 2 and "Not built yet" in capsys.readouterr().out


# ---------------------------------------------------------------- CV-2: `# CONFIRM` is an open item, like TODO
def test_fact_bank_check_counts_confirm_markers_like_todos(tmp_path):
    fb = tmp_path / "fact_bank.yaml"
    fb.write_text(
        '# Lines marked "# CONFIRM" are open questions.\n'              # prose about the marker: not an item
        "identity:\n"
        "  # CONFIRM: which job title would a reference-checker see?\n"
        "  dates: May 2026 - Sep 2026   # CONFIRM: is it still live?\n"
        "  name: TODO\n"
        "  headline: Data Scientist\n", encoding="utf-8")
    assert fact_bank_open_items(fb) == 3


def test_fact_bank_check_is_clean_without_markers_and_negative_without_a_file(tmp_path):
    fb = tmp_path / "fact_bank.yaml"
    fb.write_text("identity:\n  name: Ada Lovelace   # FIXED: from the CV\n", encoding="utf-8")
    assert fact_bank_open_items(fb) == 0
    assert fact_bank_open_items(tmp_path / "missing.yaml") == -1


def test_fixture_fact_bank_has_no_open_items():
    from conftest import fixture
    assert fact_bank_open_items(fixture("tailoring", "fact_bank_fixture.yaml")) == 0


# ---------------------------------------------------------------- M5: the calibration folder
def test_settings_name_the_calibration_folder(settings):
    from careerops.config import load_settings
    assert load_settings().raw["paths"]["calibration"] == "data/calibration"


def test_init_creates_the_calibration_folders_and_leaves_what_is_in_them_alone(settings, tmp_path):
    for k in ("register_dir", "companies_house_dir", "outputs", "browser_profile", "logs"):
        settings.raw["paths"][k] = str(tmp_path / "data" / k)
    cal = settings.path("calibration")
    assert not cal.exists()
    made = init(settings)
    assert (cal / "jds").is_dir() and (cal / "reference").is_dir() and any("jds" in m for m in made)
    (cal / "jds" / "x.txt").write_text("my JD", encoding="utf-8")
    (cal / "manifest.yaml").write_text("- id: a\n", encoding="utf-8")
    assert [m for m in init(settings) if "calibration" in m] == []                    # idempotent
    assert (cal / "jds" / "x.txt").read_text(encoding="utf-8") == "my JD"
    assert (cal / "manifest.yaml").read_text(encoding="utf-8") == "- id: a\n"


# ---------------------------------------------------------------- local Ollama: the model is built and its context is big enough
class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(str(self.status))

    def json(self):
        return self.data


OLLAMA = {"name": "ollama", "base_url": "http://localhost:11434/v1", "api_key_env": None, "model": "qwen2.5-7b-8k",
          "context_tokens": 8192}
CREATE = "ollama create qwen2.5-7b-8k -f config/ollama/Modelfile"


def _ollama(monkeypatch, models=("qwen2.5-7b-8k:latest",), parameters="num_ctx                        8192\n"):
    """Fake Ollama: GET /api/tags lists `models`; POST /api/show reports `parameters`. Returns the requests made."""
    asked = []

    def get(url, timeout):
        asked.append(("GET", url))
        return _Resp({"models": [{"name": m, "model": m} for m in models]})

    def post(url, json, timeout):
        asked.append(("POST", url, json))
        return _Resp({"parameters": parameters, "model_info": {"qwen2.context_length": 32768}})

    monkeypatch.setattr("careerops.setup.requests.get", get)
    monkeypatch.setattr("careerops.setup.requests.post", post)
    return asked


def test_doctor_passes_a_built_model_with_enough_context(monkeypatch):
    from careerops.setup import ollama_checks
    asked = _ollama(monkeypatch)
    checks = ollama_checks(OLLAMA)
    assert [ok for _, ok, _ in checks] == [True, True, True]
    assert ("GET", "http://localhost:11434/api/tags") in asked
    assert ("POST", "http://localhost:11434/api/show", {"model": "qwen2.5-7b-8k"}) in asked
    assert any("num_ctx 8192 >= context_tokens 8192" in name for name, _, _ in checks)


def test_doctor_flags_a_model_that_is_not_listed_and_says_how_to_build_it(monkeypatch):
    from careerops.setup import ollama_checks
    asked = _ollama(monkeypatch, models=("qwen2.5:7b-instruct", "llama3:latest"))   # the base model is not enough
    bad = [(n, h) for n, ok, h in ollama_checks(OLLAMA) if not ok]
    assert len(bad) == 1 and "qwen2.5-7b-8k" in bad[0][0] and bad[0][1] == CREATE
    assert not any(a[0] == "POST" for a in asked)                 # nothing to ask /api/show about


def test_doctor_flags_a_num_ctx_below_context_tokens(monkeypatch):
    from careerops.setup import ollama_checks
    _ollama(monkeypatch, parameters="num_ctx                        4096\nstop                           <|im_end|>\n")
    bad = [(n, h) for n, ok, h in ollama_checks(OLLAMA) if not ok]
    assert len(bad) == 1 and "num_ctx 4096 >= context_tokens 8192" in bad[0][0] and CREATE in bad[0][1]


def test_doctor_treats_a_model_with_no_num_ctx_as_ollamas_4096_default(monkeypatch):
    from careerops.setup import ollama_checks
    _ollama(monkeypatch, parameters="stop                           <|im_end|>\n")
    bad = [n for n, ok, _ in ollama_checks(OLLAMA) if not ok]
    assert bad == ["ollama num_ctx 4096 >= context_tokens 8192"]


def test_doctor_accepts_a_larger_num_ctx_and_an_explicit_tag(monkeypatch):
    from careerops.setup import ollama_checks
    _ollama(monkeypatch, models=("qwen2.5-7b-8k:v2",), parameters="num_ctx 16384\n")
    assert all(ok for _, ok, _ in ollama_checks({**OLLAMA, "model": "qwen2.5-7b-8k:v2"}))


def test_doctor_reports_a_server_that_does_not_answer(monkeypatch):
    import requests
    from careerops.setup import ollama_checks

    def down(url, timeout):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("careerops.setup.requests.get", down)
    checks = ollama_checks(OLLAMA)
    assert len(checks) == 1 and checks[0][1] is False and CREATE in checks[0][2]


@pytest.mark.slow
def test_doctor_runs_the_ollama_checks_for_a_provider_without_a_key(settings, monkeypatch):
    from careerops.setup import doctor
    _ollama(monkeypatch, models=())
    settings.raw["llm"]["providers"] = [OLLAMA]
    settings.raw["llm"]["models"] = {}
    # ping=False: the per-task model requests are tested in tests/llm/test_router_models.py; this is the provider's own checks
    names = [(n, ok) for n, ok, _ in doctor(settings, ping=False) if "ollama" in n and "not asked" not in n]
    assert names == [("ollama at http://localhost:11434/v1", True), ("ollama model qwen2.5-7b-8k is built", False)]
