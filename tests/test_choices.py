"""The region and size questions (careerops/choices.py): the precedence flag > last choice > settings > default, a prompt only at a terminal and once a day, and
no prompt (and nothing written) in a scheduled / --auto / piped run."""
import builtins
import json
import sys
from argparse import Namespace
from datetime import date, timedelta

import pytest

from careerops import choices, cli

DAY = date(2026, 10, 31)
REGION_MENU = "  [1] UK   [2] UK + Ireland   [3] EU   [4] US   [5] All regions   [6] type codes (e.g. uk,ie)"


class Typed:
    """`ask` for the prompts: answers in order, remembers the prompts, and fails if asked more than it has answers for."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        assert self.answers, f"asked {prompt!r} with no answer left"
        return self.answers.pop(0)


def never(prompt):
    raise AssertionError(f"a prompt appeared: {prompt!r}")


def region(settings, **kw):
    kw.setdefault("today", DAY)
    return choices.resolve_region(settings, out=kw.pop("out", lambda s: None), **kw)


def state(settings) -> dict:
    return json.loads(choices.state_path(settings).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ region: the chain
def test_with_nothing_set_the_region_is_uk(settings):
    c = region(settings)
    assert c.value == ("uk",) and c.source == "default" and c.line("region") == "region: uk (from default)"


def test_settings_then_last_choice_then_flag_each_beat_the_one_before(settings):
    settings.raw.setdefault("targeting", {})["region"] = "ie"
    assert region(settings).value == ("ie",) and region(settings).source == "settings targeting.region"
    choices.write_state(settings, region="eu")
    assert region(settings).value == ("eu",) and region(settings).source == "last choice"
    c = region(settings, flag="us")
    assert c.value == ("us",) and c.source == "--region flag"


def test_settings_may_list_several_regions(settings):
    settings.raw.setdefault("targeting", {})["region"] = ["uk", "ie"]
    assert region(settings).value == ("uk", "ie")


def test_a_flag_is_for_that_command_only_and_is_never_remembered(settings):
    region(settings, flag="us")
    assert not choices.state_path(settings).exists()
    choices.write_state(settings, region="uk,ie")
    region(settings, flag="us")
    assert state(settings)["region"] == "uk,ie"


def test_a_bad_flag_or_a_stale_state_is_refused_or_passed_over(settings):
    with pytest.raises(choices.ChoiceError, match="--region.*unknown region"):
        region(settings, flag="narnia")
    choices.write_state(settings, region="narnia")                       # a region that no longer exists: not an error, the chain goes on
    assert region(settings).value == ("uk",)


# ------------------------------------------------------------------ region: the prompt
def test_a_terminal_is_asked_once_and_the_answer_is_remembered(settings):
    out = []
    ask = Typed("2")
    c = region(settings, interactive=True, ask=ask, out=out.append)
    assert c.value == ("uk", "ie") and c.source == "your answer"
    assert out[0].startswith("Target region (Enter = UK, default):") and REGION_MENU in out[0]
    assert state(settings)["region"] == "uk,ie" and state(settings)["region_asked"] == "2026-10-31"
    again = region(settings, interactive=True, ask=never)                # the same day: not asked again
    assert again.value == ("uk", "ie") and again.source == "today's choice"


def test_the_next_day_it_is_asked_again_with_the_last_choice_as_the_default(settings):
    region(settings, interactive=True, ask=Typed("3"))
    out = []
    ask = Typed("")
    c = region(settings, interactive=True, ask=ask, out=out.append, today=DAY + timedelta(days=1))
    assert c.value == ("eu",) and "Enter = EU (27), last used" in out[0] and len(ask.prompts) == 1                  # Enter takes the default


@pytest.mark.parametrize("answer,want", [("1", ("uk",)), ("2", ("uk", "ie")), ("3", ("eu",)), ("4", ("us",)), ("5", ("any",)), ("uk,ie", ("uk", "ie")), ("DE", ("de",))])
def test_every_menu_answer(settings, answer, want):
    assert region(settings, interactive=True, ask=Typed(answer)).value == want


def test_option_6_asks_for_codes_and_a_bad_answer_asks_again(settings):
    out = []
    c = region(settings, interactive=True, ask=Typed("6", "narnia", "uk,ie"), out=out.append)
    assert c.value == ("uk", "ie") and any("unknown region" in o for o in out)


def test_no_answer_at_the_prompt_falls_back_to_the_default_and_remembers_nothing(settings):
    def eof(prompt):
        raise EOFError
    c = region(settings, interactive=True, ask=eof)
    assert c.value == ("uk",) and not choices.state_path(settings).exists()


def test_without_a_terminal_nothing_is_asked_and_nothing_is_written(settings):
    out = []
    choices.write_state(settings, region="ie", region_asked="2026-10-30")
    c = region(settings, interactive=False, ask=never, out=out.append)
    assert c.value == ("ie",) and out == [] and state(settings) == {"region": "ie", "region_asked": "2026-10-30"}


# ------------------------------------------------------------------ size
def size(settings, **kw):
    kw.setdefault("today", DAY)
    return choices.resolve_size(settings, out=kw.pop("out", lambda s: None), **kw)


def test_the_size_chain_is_flag_then_last_then_settings_then_both(settings):
    assert size(settings).value == "both" and size(settings).source == "default"
    settings.raw.setdefault("targeting", {})["size"] = "sme-first"
    assert size(settings).value == "sme-first" and size(settings).source == "settings targeting.size"
    choices.write_state(settings, size="large")
    assert size(settings).value == "large" and size(settings).source == "last choice"
    assert size(settings, flag="SME").value == "sme" and state(settings)["size"] == "large"
    with pytest.raises(choices.ChoiceError, match="not a company size"):
        size(settings, flag="medium")


def test_the_prompt_shows_the_queue_sizes_before_the_choice(settings):
    out, order = [], []
    ask = Typed("1")
    ask_wrapped = lambda p: (order.append("ask"), ask(p))[1]               # noqa: E731
    c = size(settings, interactive=True, ask=ask_wrapped, out=lambda s: (order.append("out"), out.append(s)), counts=lambda: {"sme": 14, "large": 22, "unknown": 31})
    assert c.value == "sme" and c.source == "your answer"
    shown = "\n".join(out)
    assert "Company size to target (Enter = Both, default):" in shown
    assert "[1] SME (<250 staff)   [2] Large (250+)   [3] Both, SME first   [4] Both, large first   [5] Both, no preference" in shown
    assert "sizes in queue: SME 14 · Large 22 · Unknown 31" in shown
    assert order[:2] == ["out", "out"] and order[2] == "ask"                # the counts are printed before the question is put


@pytest.mark.parametrize("answer,want", [("1", "sme"), ("2", "large"), ("3", "sme-first"), ("4", "large-first"), ("5", "both"), ("", "both"), ("sme-first", "sme-first")])
def test_every_size_answer(settings, answer, want):
    assert size(settings, interactive=True, ask=Typed(answer)).value == want


def test_the_size_is_asked_once_a_day_and_never_without_a_terminal(settings):
    size(settings, interactive=True, ask=Typed("2"))
    assert size(settings, interactive=True, ask=never).source == "today's choice"
    assert size(settings, interactive=False, ask=never).value == "large"


def test_the_sponsorship_note_is_printed_once_for_sme_sme_first_and_both_and_not_for_large(settings):
    out = []
    size(settings, interactive=True, ask=Typed("1"), out=out.append)
    assert out.count(choices.SIZE_NOTE) == 1
    size(settings, flag="sme", out=out.append)                                       # the same day: not again
    assert out.count(choices.SIZE_NOTE) == 1
    other = []
    choices.write_state(settings, size_note_day=None)
    size(settings, flag="large", out=other.append)
    assert choices.SIZE_NOTE not in other
    size(settings, flag="both", out=other.append)
    assert other.count(choices.SIZE_NOTE) == 1


def test_the_note_text_is_exactly_as_written():
    assert choices.SIZE_NOTE == ("Note: UK small sponsors pay a lower Immigration Skills Charge (£480 vs £1,320 for the first year, rates from\n"
                                 "16 Dec 2025), but fewer SMEs hold a sponsor licence. The sponsor check still applies to every job.")


def test_a_non_uk_region_prints_the_visa_line():
    assert choices.non_uk_line(("uk",)) is None and choices.non_uk_line(("uk", "ie")) == "non-UK region: visa/right-to-work not checked"
    assert choices.non_uk_line(("any",)) and choices.non_uk_line(("de",))


# ------------------------------------------------------------------ the command line helper
def _args(**kw):
    return Namespace(**{"region": None, "yes": False, "mock": False, **kw})


def test_a_piped_or_scheduled_command_prints_the_region_and_never_prompts(settings, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(builtins, "input", never)
    choices.write_state(settings, region="ie")
    c = cli._region_choice(_args(), settings)
    out = capsys.readouterr().out
    assert c.value == ("ie",) and "region: ie (from last choice)" in out and "non-UK region: visa/right-to-work not checked" in out


@pytest.mark.parametrize("kw", [{"yes": True}, {"region": "eu"}, {"mock": True}])
def test_yes_a_flag_and_mock_each_switch_the_prompt_off_at_a_terminal(settings, monkeypatch, kw):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", never)
    cli._region_choice(_args(**kw), settings)


def test_auto_switches_the_prompt_off_at_a_terminal(settings, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", never)
    assert cli._region_choice(_args(), settings, auto=True).value == ("uk",)


def test_a_terminal_without_a_flag_is_asked(settings, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", Typed("4"))
    assert cli._region_choice(_args(), settings).value == ("us",)
    out = capsys.readouterr().out
    assert "Target region" in out and "non-UK region: visa/right-to-work not checked" in out
