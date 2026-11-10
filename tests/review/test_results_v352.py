"""v3.5.2 `careerops results`: the "Needs a result" bullets one at a time; the user types the result (or s / q); nothing is drafted and no model is
called; `y` saves it as the bullet's `result:` through factbank_append.add_field with a dated backup; the bullet's text never changes. A copy of the
fictional fixture bank in tmp_path."""
import shutil
from datetime import datetime

import pytest
import yaml

from careerops import factbank_append as F
from careerops.review import results
from conftest import FIX

BANK = FIX / "profile" / "fact_bank.yaml"
NOW = datetime(2026, 11, 10, 14, 12, 33)


@pytest.fixture
def bank_copy(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    shutil.copy(BANK, p)
    return p


def answers(*replies):
    q = list(replies)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return q.pop(0) if q else ""
    ask.asked = asked
    return ask


def bullet(path, bid):
    fb = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(b for sec in ("experience", "projects") for e in fb[sec] for b in e["bullets"] if b["id"] == bid)


def test_a_typed_result_is_saved_after_y_with_a_backup_and_the_text_unchanged(bank_copy):
    before = bank_copy.read_bytes()
    text = bullet(bank_copy, "az1")["text"]
    out = []
    stats = results.run(bank_copy, ask=answers("used by the whole research team", "y"), out=out.append, ids={"az1"}, now=NOW)
    assert stats["saved"] == ["az1"]
    az1 = bullet(bank_copy, "az1")
    assert az1["result"] == "used by the whole research team" and az1["text"] == text
    backup = bank_copy.parent / "backups" / "fact_bank.20261110-141233.yaml"
    assert backup.read_bytes() == before
    added = [l for l in bank_copy.read_text(encoding="utf-8").splitlines() if l not in before.decode("utf-8").splitlines()]
    assert len(added) == 1 and added[0].strip() == 'result: "used by the whole research team"'


@pytest.mark.parametrize("replies", [("s",), ("",), ("a result", "n"), ("a result", ""), ("a result", "yes")])
def test_skip_blank_or_anything_but_y_saves_nothing(bank_copy, replies):
    before = bank_copy.read_bytes()
    stats = results.run(bank_copy, ask=answers(*replies), out=lambda _: None, ids={"az1"})
    assert bank_copy.read_bytes() == before and not (bank_copy.parent / "backups").exists() and stats["skipped"] == ["az1"]


def test_q_stops_and_one_bullet_at_a_time_in_bank_order(bank_copy):
    ask = answers("s", "q")
    stats = results.run(bank_copy, ask=ask, out=lambda _: None)
    assert len(ask.asked) == 2 and stats["skipped"] == ["az1"] and stats["left"] == len(results.needing(yaml.safe_load(bank_copy.read_text(encoding="utf-8")))) - 1


def test_a_bullet_that_has_a_result_is_not_asked_and_nothing_is_drafted(bank_copy, monkeypatch):
    results.run(bank_copy, ask=answers("now in daily use", "y"), out=lambda _: None, ids={"az2"}, now=NOW)
    assert "az2" not in [b["id"] for _, b in results.needing(yaml.safe_load(bank_copy.read_text(encoding="utf-8")))]
    import careerops.llm as llm_pkg
    monkeypatch.setattr(llm_pkg, "MockLLM", lambda *a, **k: pytest.fail("results never calls a model"))
    src = (results.__file__)
    assert "llm" not in open(src, encoding="utf-8").read().split('"""', 2)[2]


def test_the_writer_refuses_a_result_on_a_hook_entry_and_a_second_result(bank_copy):
    fb = yaml.safe_load(bank_copy.read_text(encoding="utf-8"))
    hook_ids = [str(h["id"]) for h in fb.get("hook_library") or []]
    if hook_ids:
        with pytest.raises(F.AppendError, match="not a bullet"):
            F.add_field(bank_copy, hook_ids[0], "result", "x", confirmed="y", now=NOW)
    F.add_field(bank_copy, "az3", "result", "deployed", confirmed="y", now=NOW)
    with pytest.raises(F.AppendError, match="already has"):
        F.add_field(bank_copy, "az3", "result", "again", confirmed="y", now=datetime(2026, 11, 10, 15, 0, 0))
    with pytest.raises(F.NotConfirmed):
        F.add_field(bank_copy, "az4", "result", "x", confirmed="n", now=NOW)


def test_the_cli_refuses_without_a_terminal(tmp_path, settings, monkeypatch, capsys):
    from careerops import cli
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg), "results"])
    assert e.value.code != 0 and "terminal" in capsys.readouterr().err
