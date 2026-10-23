"""careerops/factbank_append.py: the one writer of the fact bank file (fact_bank.yaml). It appends a bullet the user typed, word for word, and only
after a `y` for that exact entry; a dated backup is written first; every existing line stays byte-identical; the file must load and
validate afterwards (no duplicate ids, deny list passes) or the backup is put back. Tests use tmp copies, never the real bank."""
from datetime import date, datetime

import pytest
import yaml

from careerops import factbank_append as fa

BANK = """\
# fact_bank.yaml: the user's own facts
identity:
  name: Ada Lovelace   # not real

skills:
  core: [Python, SQL]

experience:
  - id: az                              # the dissertation
    title: Data Scientist
    org: Northwind
    bullets:
      - id: az1
        text: "Built a tool-calling LLM agent."
        terms: [llm, agent]
      - id: az2                          # NEW from LinkedIn
        text: "Deployed it on AWS Lambda."
        result: "on AWS Lambda"
        terms: [aws, lambda]
      # a note that sits between bullets

  - id: dc
    title: Data Consultant
    org: Pro Bono
    bullets:
      - id: dc1
        text: "Prototyped an MCP tool."
        terms: [mcp]
      - id: dc7
        verbatim: true
        text: "Proposed the request flow."
        terms: [architecture]

# ---- projects ----
projects:
  - id: gtai
    name: GTAI graph
    bullets:
      - id: p_gg1
        text: "Merged 14 files."
        terms: [graph]
      - id: p_gg5
        text: "Debugged a hash bug."
        terms: [debugging]
  - id: lloyds
    name: Datathon
    bullets:
      - id: p_ll1
        text: "Detected outliers."
        terms: [anomaly detection]

education:
  - degree: MSc

deny_terms:
  - kubernetes
  - terraform
"""
NOW = datetime(2026, 10, 23, 14, 5, 9)


@pytest.fixture
def bank(tmp_path):
    p = tmp_path / "profile" / "fact_bank.yaml"
    p.parent.mkdir()
    p.write_bytes(BANK.encode("utf-8"))
    return p


def add(bank, text="Used Airflow to schedule the nightly retraining of a churn model, which cut manual reruns to zero.", target="az", keyword="Airflow", confirmed="y"):
    return fa.append_fact(bank, target, text, keyword, confirmed=confirmed, now=NOW)


def lines(p):
    return p.read_bytes().decode("utf-8").splitlines(keepends=True)


def test_it_appends_the_bullet_to_the_end_of_that_role_with_the_next_id(bank):
    done = add(bank)
    assert done.bullet_id == "az3" and done.target == "az"
    fb = yaml.safe_load(bank.read_text(encoding="utf-8"))
    az = fb["experience"][0]["bullets"]
    assert [b["id"] for b in az] == ["az1", "az2", "az3"]
    assert [b["id"] for b in fb["experience"][1]["bullets"]] == ["dc1", "dc7"]                     # nothing went to the other role


def test_the_new_bullet_has_the_users_exact_text_a_source_stamp_and_the_keyword_as_its_term(bank):
    text = 'Used Airflow: "nightly" retraining, 40% fewer reruns (£0 spend); naïve → robust.'
    add(bank, text=text)
    b = yaml.safe_load(bank.read_text(encoding="utf-8"))["experience"][0]["bullets"][-1]
    assert b == {"id": "az3", "text": text, "terms": ["Airflow"], "source": "user_20261023"}


def test_every_existing_line_is_byte_identical(bank):
    before = lines(bank)
    add(bank)
    after = lines(bank)
    added = len(after) - len(before)
    assert added == 4                                                                         # - id, text, terms, source
    at = next(i for i, (a, b) in enumerate(zip(before, after)) if a != b)
    assert after[:at] + after[at + added:] == before                                          # remove the new block and the file is the old file
    assert "".join(after[at:at + added]).startswith("      - id: az3\n")                       # the file's own indentation


def test_the_block_goes_right_after_the_last_bullet_of_the_role_and_before_what_follows_it(bank):
    add(bank)
    text = bank.read_text(encoding="utf-8")
    assert "        terms: [aws, lambda]\n      - id: az3\n" in text                          # directly after az2's last line
    assert text.index("- id: az3") < text.index("# a note that sits between bullets") < text.index("  - id: dc\n")


def test_a_project_continues_its_own_id_prefix(bank):
    done = add(bank, target="gtai", keyword="Leiden")
    assert done.bullet_id == "p_gg6"                                                          # prefix of the project's own bullets, after the highest number
    done = add(bank, target="lloyds", keyword="MLflow")
    assert done.bullet_id == "p_ll2"


def test_a_dated_backup_is_written_first_and_holds_the_old_file(bank):
    seen = {}
    real = fa._write_atomic

    def spy(path, data):
        seen["backups"] = sorted((bank.parent / "backups").glob("fact_bank.*.yaml"))
        seen["bytes"] = [p.read_bytes() for p in seen["backups"]]
        real(path, data)

    old = bank.read_bytes()
    fa._write_atomic = spy
    try:
        done = add(bank)
    finally:
        fa._write_atomic = real
    assert [p.name for p in seen["backups"]] == ["fact_bank.20261023-140509.yaml"]            # it existed before the bank was touched
    assert seen["bytes"] == [old]
    assert done.backup == bank.parent / "backups" / "fact_bank.20261023-140509.yaml"
    assert done.backup.read_bytes() == old and bank.read_bytes() != old


def test_a_second_backup_in_the_same_second_does_not_overwrite_the_first(bank):
    one = add(bank)
    two = add(bank, target="dc", keyword="dbt-free")
    assert one.backup != two.backup and one.backup.exists() and two.backup.exists()
    assert one.backup.read_bytes() != two.backup.read_bytes()


@pytest.mark.parametrize("answer", ["", "n", "N", "no", "yes", "e", " ", None])
def test_it_never_writes_without_a_y(bank, answer):
    old = bank.read_bytes()
    with pytest.raises(fa.NotConfirmed):
        add(bank, confirmed=answer)
    assert bank.read_bytes() == old and not (bank.parent / "backups").exists()


def test_a_deny_listed_term_rolls_the_file_back_and_keeps_the_backup(bank):
    old = bank.read_bytes()
    with pytest.raises(fa.AppendError, match="(?i)terraform"):
        add(bank, text="Provisioned the cluster with Terraform.", keyword="Terraform")
    assert bank.read_bytes() == old                                                           # restored, byte for byte
    backups = list((bank.parent / "backups").glob("fact_bank.*.yaml"))
    assert len(backups) == 1 and backups[0].read_bytes() == old


def test_a_duplicate_id_rolls_the_file_back(bank, monkeypatch):
    old = bank.read_bytes()
    monkeypatch.setattr(fa, "_next_id", lambda *a, **k: "az1")
    with pytest.raises(fa.AppendError, match="(?i)duplicate"):
        add(bank)
    assert bank.read_bytes() == old


def test_a_bank_that_fails_to_load_afterwards_is_rolled_back(bank, monkeypatch):
    old = bank.read_bytes()
    monkeypatch.setattr(fa, "_block", lambda *a, **k: ["      - id: az3\n", "        text: [unclosed\n"])
    with pytest.raises(fa.AppendError):
        add(bank)
    assert bank.read_bytes() == old


def test_a_problem_the_bank_already_had_does_not_block_a_good_entry(bank):
    bank.write_bytes(BANK.replace("Deployed it on AWS Lambda.", "Deployed it with Kubernetes.").encode("utf-8"))   # an old bullet that breaks the deny list
    done = add(bank)
    assert done.bullet_id == "az3"                                                            # only a NEW problem rolls back


def test_an_unknown_role_is_refused_before_anything_is_written(bank):
    old = bank.read_bytes()
    with pytest.raises(fa.AppendError, match="nope"):
        add(bank, target="nope")
    assert bank.read_bytes() == old and not (bank.parent / "backups").exists()


@pytest.mark.parametrize("text", ["", "   ", "two\nlines"])
def test_empty_or_multiline_text_is_refused(bank, text):
    old = bank.read_bytes()
    with pytest.raises(fa.AppendError):
        add(bank, text=text)
    assert bank.read_bytes() == old


def test_crlf_files_stay_crlf(tmp_path):
    p = tmp_path / "fact_bank.yaml"
    p.write_bytes(BANK.replace("\n", "\r\n").encode("utf-8"))
    add(p)
    data = p.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n")                                           # no bare LF was introduced
    assert [b["id"] for b in yaml.safe_load(data)["experience"][0]["bullets"]] == ["az1", "az2", "az3"]


def test_the_edited_text_is_what_is_written_when_the_user_retyped_it(bank):
    add(bank, text="My own words, retyped.")
    assert yaml.safe_load(bank.read_text(encoding="utf-8"))["experience"][0]["bullets"][-1]["text"] == "My own words, retyped."


def test_refresh_reloads_a_bank_held_in_memory_in_place(bank):
    held = yaml.safe_load(bank.read_text(encoding="utf-8"))
    alias = held
    add(bank)
    fa.refresh(held, bank)
    assert alias is held and [b["id"] for b in alias["experience"][0]["bullets"]] == ["az1", "az2", "az3"]


def test_the_default_clock_stamps_today(bank):
    done = fa.append_fact(bank, "az", "Used Airflow every night.", "Airflow", confirmed="y")
    assert done.bullet_id == "az3"
    src = yaml.safe_load(bank.read_text(encoding="utf-8"))["experience"][0]["bullets"][-1]["source"]
    assert src == "user_" + date.today().strftime("%Y%m%d")
