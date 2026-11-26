"""v3.9.3: the region and company size are asked once and saved in `.env` (careerops/prefs.py, choices.py `env=`), `--region` / `--size` are for one run
unless `--save`, and `careerops prefs` lists / sets them. Every test works on a temporary `.env` (CAREEROPS_ENV_FILE), never the real one."""
import builtins
import os
import sys
from argparse import Namespace
from datetime import date, datetime

import pytest

from careerops import choices, cli, prefs

DAY = date(2026, 11, 18)
SECRETS = ("# copy to .env and fill in; never commit\n"
           "GROQ_API_KEY=gsk_fake_0123456789\n"
           "REED_API_KEY=\"fake reed key\"\n"
           "\n"
           "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-fake\n")


class Typed:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        assert self.answers, f"asked {prompt!r} with no answer left"
        return self.answers.pop(0)


def never(prompt=""):
    raise AssertionError(f"a prompt appeared: {prompt!r}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_bytes(SECRETS.encode())
    monkeypatch.setenv(prefs.ENV_FILE_VAR, str(p))
    return p


def backups(env):
    return sorted(env.parent.glob(".env.bak-*"))


def _args(**kw):
    return Namespace(**{"region": None, "yes": False, "mock": False, "save": False, "dry_run": False, **kw})


# ------------------------------------------------------------------ 1. a missing key is asked once and appended
def test_a_missing_key_prompts_once_and_appends_the_key(settings, env):
    ask, out = Typed("3"), []
    c = choices.resolve_region(settings, interactive=True, ask=ask, out=out.append, today=DAY, env=env)
    assert c.value == ("eu",) and len(ask.prompts) == 1
    assert env.read_bytes() == (SECRETS + "CAREEROPS_REGION=eu\n").encode()                  # appended at the end; nothing before it moved
    assert [b.read_bytes() for b in backups(env)] == [SECRETS.encode()]                       # the backup was taken first
    assert not any("gsk_fake" in line or "sk-ant" in line for line in out)                    # the other lines are never printed

    ask = Typed("3")                                                                           # size: [3] = Both, SME first
    s = choices.resolve_size(settings, interactive=True, ask=ask, out=out.append, today=DAY, env=env)
    assert s.value == "sme-first" and len(ask.prompts) == 1
    assert env.read_bytes().endswith(b"CAREEROPS_REGION=eu\nCAREEROPS_COMPANY_SIZE=both_sme_first\n")


# ------------------------------------------------------------------ 2. the second run does not prompt
def test_a_second_run_does_not_prompt(settings, env, monkeypatch, capsys):
    choices.resolve_region(settings, interactive=True, ask=Typed("1"), out=lambda s: None, today=DAY, env=env)
    choices.resolve_size(settings, interactive=True, ask=Typed("5"), out=lambda s: None, today=DAY, env=env)
    before = env.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", never)
    c = cli._region_choice(_args(), settings)
    s = choices.resolve_size(settings, interactive=True, ask=never, today=DAY, env=env)
    assert c.value == ("uk",) and s.value == "both"
    assert "region: uk (saved in .env; change with careerops prefs)" in capsys.readouterr().out
    assert s.line("size") == "size: both (saved in .env; change with careerops prefs)"
    assert env.read_bytes() == before


def test_the_size_names_of_the_env_file_and_an_inline_comment_are_read(settings, env):
    env.write_bytes(SECRETS.encode() + b"CAREEROPS_REGION=uk,ie\nCAREEROPS_COMPANY_SIZE=both_large_first        # sme | large | both\n")
    assert choices.resolve_region(settings, env=env, ask=never, interactive=True).value == ("uk", "ie")
    assert choices.resolve_size(settings, env=env, ask=never, interactive=True).value == "large-first"
    assert choices.parse_size("both_sme_first") == "sme-first"


# ------------------------------------------------------------------ 3. a flag is for one run; --save writes it
def test_a_region_flag_without_save_leaves_the_env_file_unchanged(settings, env, monkeypatch, capsys):
    env.write_bytes(SECRETS.encode() + b"CAREEROPS_REGION=uk\n")
    before, stat = env.read_bytes(), os.stat(env)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", never)
    assert cli._region_choice(_args(region="eu"), settings).value == ("eu",)
    assert "region: eu (from --region flag)" in capsys.readouterr().out
    assert env.read_bytes() == before and os.stat(env).st_mtime_ns == stat.st_mtime_ns and backups(env) == []
    assert choices.resolve_size(settings, flag="sme", env=env, out=lambda s: None).value == "sme" and env.read_bytes() == before


def test_a_flag_with_save_replaces_only_its_line(settings, env, capsys):
    env.write_bytes(SECRETS.encode() + b"CAREEROPS_REGION=uk\n")
    cli._region_choice(_args(region="eu", save=True), settings)
    assert env.read_bytes() == SECRETS.encode() + b"CAREEROPS_REGION=eu\n" and len(backups(env)) == 1
    assert "saved CAREEROPS_REGION=eu in .env (was uk)" in capsys.readouterr().out
    cli._region_choice(_args(region="eu", save=True, yes=True), settings)                    # the same value again: no write, no new backup
    assert len(backups(env)) == 1
    cli._region_choice(_args(region="us", save=True, dry_run=True), settings)                # a dry run writes nothing
    assert env.read_bytes().endswith(b"CAREEROPS_REGION=eu\n")


# ------------------------------------------------------------------ 4. careerops prefs
def test_prefs_set_takes_a_backup_and_changes_only_that_line(env, capsys):
    crlf = (SECRETS.replace("\n", "\r\n") + "CAREEROPS_REGION=uk\r\nCAREEROPS_COMPANY_SIZE=both    # sme | large\r\nHUNTER_API_KEY=hunter-fake\r\n").encode()
    env.write_bytes(crlf)
    mode = os.stat(env).st_mode
    cli.main(["prefs", "set", "size", "sme"])
    out = capsys.readouterr().out
    assert "size: both -> sme" in out and "backup .env.bak-" in out
    assert [b.read_bytes() for b in backups(env)] == [crlf]
    after = env.read_bytes().split(b"\r\n")
    want = crlf.split(b"\r\n")
    changed = [i for i, (a, b) in enumerate(zip(want, after)) if a != b]
    assert len(after) == len(want) and len(changed) == 1 and after[changed[0]] == b"CAREEROPS_COMPANY_SIZE=sme"
    assert os.stat(env).st_mode == mode
    assert not any(s in out for s in ("gsk_fake", "fake reed key", "sk-ant-oat-fake", "hunter-fake"))

    cli.main(["prefs", "set", "region", "eu"])
    assert "region: uk -> eu" in capsys.readouterr().out
    cli.main(["prefs"])
    listed = capsys.readouterr().out
    assert "region: eu" in listed and "size:   sme" in listed and "gsk_fake" not in listed and "hunter-fake" not in listed


def test_prefs_set_of_a_missing_key_appends_it_and_a_bad_value_is_refused(env, capsys):
    cli.main(["prefs", "set", "size", "both_large_first"])
    assert env.read_bytes() == SECRETS.encode() + b"CAREEROPS_COMPANY_SIZE=both_large_first\n"
    with pytest.raises(SystemExit):
        cli.main(["prefs", "set", "region", "narnia"])
    with pytest.raises(SystemExit):
        cli.main(["prefs", "set", "size", "medium"])
    assert env.read_bytes() == SECRETS.encode() + b"CAREEROPS_COMPANY_SIZE=both_large_first\n"
    cli.main(["prefs"])
    assert "region: not set" in capsys.readouterr().out


def test_set_value_backup_names_never_collide(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    now = datetime(2026, 11, 18, 9, 0, 0)
    prefs.set_value(p, prefs.REGION_KEY, "uk", now)
    prefs.set_value(p, prefs.REGION_KEY, "eu", now)
    assert [b.name for b in sorted(tmp_path.glob(".env.bak-*"))] == [".env.bak-20261118-090000", ".env.bak-20261118-090000-2"]
    assert p.read_text(encoding="utf-8") == "A=1\nCAREEROPS_REGION=eu\n"


# ------------------------------------------------------------------ 5. no terminal and no key: no prompt, the defaults, a warning
def test_no_terminal_and_no_key_uses_the_defaults_without_a_prompt(settings, env, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(builtins, "input", never)
    c = cli._region_choice(_args(), settings)
    s = choices.resolve_size(settings, interactive=False, ask=never, env=env)
    out = capsys.readouterr().out
    assert c.value == ("uk",) and s.value == "both"
    assert "region: uk (from default)" in out and "warning: CAREEROPS_REGION is not set in .env" in out
    assert env.read_bytes() == SECRETS.encode() and backups(env) == []


def test_mock_never_reads_or_writes_the_env_file(settings, env, monkeypatch):
    env.write_bytes(b"CAREEROPS_REGION=us\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", never)
    assert cli._region_choice(_args(mock=True), settings).value == ("uk",)
    assert env.read_bytes() == b"CAREEROPS_REGION=us\n"


def test_a_missing_env_file_is_created_owner_only(settings, tmp_path, monkeypatch):
    p = tmp_path / "fresh" / ".env"
    p.parent.mkdir()
    choices.resolve_region(settings, interactive=True, ask=Typed(""), out=lambda s: None, today=DAY, env=p)
    assert p.read_text(encoding="utf-8") == "CAREEROPS_REGION=uk\n"
    if os.name == "posix" and (os.stat(p).st_mode & 0o777) != 0o777:                         # NTFS through WSL reports 777 for every file
        assert os.stat(p).st_mode & 0o077 == 0
