"""The two questions asked before the day's work: which REGION to target, and which COMPANY SIZE. Both have a remembered default and a flag, so a question never
blocks a scheduled run.

Precedence, region (A3) and size (B3) alike:   the flag (`--region uk,ie` / `--size sme`)
                                               -> the last choice in data/state/targeting.json
                                               -> `targeting.region` / `targeting.size` in settings.yaml
                                               -> `uk` / `both`.
A prompt appears only when stdin is a terminal, `--yes` and `--auto` are not set and no flag was given, and only once a day: later commands that day reuse
the answer (`--region` / `--size` change it). Without a terminal (cron, `--auto`, a pipe) nothing is asked and the line `region: uk (from last choice)` says
what was used. A flag is for that command only: it is never written to targeting.json, so a scheduled `--region uk` cannot overwrite what you picked.
data/state/targeting.json is the program's own memory (settings.yaml is never written).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping

from .discovery import region as region_mod
from .discovery import size as size_mod

STATE_FILE = "targeting.json"
SIZE_MODES = ("sme", "large", "both", "sme-first", "large-first")
DEFAULT_SIZE = "both"
SIZE_NOTE = ("Note: UK small sponsors pay a lower Immigration Skills Charge (£480 vs £1,320 for the first year, rates from\n"
             "16 Dec 2025), but fewer SMEs hold a sponsor licence. The sponsor check still applies to every job.")
SIZE_NOTE_MODES = ("sme", "sme-first", "both")                       # the picks the note is printed for
NON_UK_NOTE = "non-UK region: visa/right-to-work not checked"
REGION_MENU = {"1": "uk", "2": "uk,ie", "3": "eu", "4": "us", "5": "any"}
SIZE_MENU = {"1": "sme", "2": "large", "3": "sme-first", "4": "large-first", "5": "both"}
SIZE_NAMES = {"sme": "SME", "large": "Large", "both": "Both, no preference", "sme-first": "Both, SME first", "large-first": "Both, large first"}


class ChoiceError(ValueError):
    """A region or size that cannot be used (one line, fit to print)."""


@dataclass(frozen=True)
class Choice:
    value: object                  # region: tuple of region keys; size: one of SIZE_MODES
    source: str                    # where it came from: "--region flag", "today's choice", "last choice", "settings targeting.region", "default", "your answer"

    @property
    def text(self) -> str:
        return ",".join(self.value) if isinstance(self.value, tuple) else str(self.value)

    def line(self, what: str) -> str:
        return f"{what}: {self.text} (from {self.source})"


# ------------------------------------------------------------------ the remembered choice
def state_path(settings) -> Path:
    return settings.path("state") / STATE_FILE


def read_state(settings) -> dict:
    try:
        raw = json.loads(state_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_state(settings, **changes) -> None:
    """Merge `changes` into targeting.json (a few short lines, written whole). A state that cannot be written only means the question is asked again."""
    path = state_path(settings)
    state = {**read_state(settings), **changes}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _targeting(settings, key: str):
    raw = (getattr(settings, "raw", None) or {}).get("targeting")
    return raw.get(key) if isinstance(raw, dict) else None


def _today(today: date | None) -> str:
    return (today or date.today()).isoformat()


# ------------------------------------------------------------------ region
def region_default(settings, defs=None) -> Choice:
    """The region with no flag and no prompt: the last choice, else targeting.region, else uk. A value that no longer parses is passed over."""
    last = read_state(settings).get("region")
    for value, source in ((last, "last choice"), (_targeting(settings, "region"), "settings targeting.region")):
        if value:
            try:
                return Choice(region_mod.parse_selection(value, defs), source)
            except region_mod.RegionError:
                continue
    return Choice((region_mod.DEFAULT_REGION,), "default")


def _menu_label(sel: tuple[str, ...], defs=None) -> str:
    return region_mod.label_of(sel, defs).replace("United Kingdom + Ireland", "UK + Ireland").replace("United Kingdom", "UK")


def resolve_region(settings, *, flag: str | None = None, interactive: bool = False, ask: Callable[[str], str] | None = None,
                   out: Callable[[str], None] = print, today: date | None = None, defs=None) -> Choice:
    """The region(s) for this command (see the module text). Raises ChoiceError for a bad flag."""
    if flag:
        try:
            return Choice(region_mod.parse_selection(flag, defs), "--region flag")
        except region_mod.RegionError as e:
            raise ChoiceError(f"--region: {e}") from None
    default = region_default(settings, defs)
    state = read_state(settings)
    if not (interactive and ask):
        return default
    if state.get("region_asked") == _today(today) and default.source == "last choice":
        return Choice(default.value, "today's choice")
    why = {"last choice": "last used", "default": "default"}.get(default.source, "from settings")
    out(f"Target region (Enter = {_menu_label(default.value, defs)}, {why}):\n"
        "  [1] UK   [2] UK + Ireland   [3] EU   [4] US   [5] All regions   [6] type codes (e.g. uk,ie)")
    for _ in range(3):
        try:
            answer = ask("> ").strip()
            if answer == "6":
                answer = ask("codes (e.g. uk,ie): ").strip()
            picked = default.value if not answer else region_mod.parse_selection(REGION_MENU.get(answer, answer), defs)
        except (EOFError, KeyboardInterrupt):
            return default
        except region_mod.RegionError as e:
            out(f"  {e}")
            continue
        write_state(settings, region=",".join(picked), region_asked=_today(today))
        return Choice(picked, "your answer")
    return default


# ------------------------------------------------------------------ size
def parse_size(value) -> str:
    v = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if v not in SIZE_MODES:
        raise ChoiceError(f"{value!r} is not a company size: use {', '.join(SIZE_MODES)}")
    return v


def size_default(settings) -> Choice:
    for value, source in ((read_state(settings).get("size"), "last choice"), (_targeting(settings, "size"), "settings targeting.size")):
        if value:
            try:
                return Choice(parse_size(value), source)
            except ChoiceError:
                continue
    return Choice(DEFAULT_SIZE, "default")


def resolve_size(settings, *, flag: str | None = None, interactive: bool = False, ask: Callable[[str], str] | None = None,
                 out: Callable[[str], None] = print, today: date | None = None, counts: Callable[[], Mapping[str, int]] | None = None) -> Choice:
    """The company-size mode for this command. `counts()` is called only when the prompt is shown: the sizes of the queue are printed BEFORE the choice."""
    if flag:
        got = Choice(parse_size(flag), "--size flag")
        return _noted(settings, got, out, today, picked=True)
    default = size_default(settings)
    state = read_state(settings)
    if not (interactive and ask):
        return default
    if state.get("size_asked") == _today(today) and default.source == "last choice":
        return Choice(default.value, "today's choice")
    why = {"last choice": "last used", "default": "default"}.get(default.source, "from settings")
    menu = ("  [1] SME (<250 staff)   [2] Large (250+)   [3] Both, SME first   [4] Both, large first   [5] Both, no preference")
    out(f"Company size to target (Enter = {SIZE_NAMES[default.value].replace('Both, no preference', 'Both')}, {why}):\n{menu}")
    if counts is not None:
        out(f"  sizes in queue: {size_mod.counts_text(counts())}")
    for _ in range(3):
        try:
            answer = ask("> ").strip().lower()
            picked = default.value if not answer else parse_size(SIZE_MENU.get(answer, answer))
        except (EOFError, KeyboardInterrupt):
            return default
        except ChoiceError as e:
            out(f"  {e}")
            continue
        write_state(settings, size=picked, size_asked=_today(today))
        return _noted(settings, Choice(picked, "your answer"), out, today, picked=True)
    return default


def _noted(settings, choice: Choice, out: Callable[[str], None], today: date | None, picked: bool) -> Choice:
    """Print the sponsorship note once a day when `sme`, `sme-first` or `both` is picked (text exactly as written in SIZE_NOTE)."""
    if picked and choice.value in SIZE_NOTE_MODES and read_state(settings).get("size_note_day") != _today(today):
        out(SIZE_NOTE)
        write_state(settings, size_note_day=_today(today))
    return choice


def non_uk_line(selection: tuple[str, ...], defs=None) -> str | None:
    """The one line printed when the region covers countries outside the UK: the sponsor register is UK-only, so S = 0.75 (unknown) for those jobs."""
    if region_mod.ANY in selection:
        return NON_UK_NOTE
    return NON_UK_NOTE if region_mod.iso_of(selection, defs) - {"GB"} else None
