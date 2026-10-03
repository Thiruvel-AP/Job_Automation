"""Keep the raw model reply of every LLM call, so a shape problem can be inspected afterwards.

Wraps an LLM (Router, MockLLM): each `json()` call writes the reply text exactly as the model sent it to
<folder>/<task>-<n>.json (the text may not be valid JSON; that is often the point). Everything else (usage, calls,
providers, begin_job ...) is passed through to the wrapped LLM. The folder lives under data/outputs/<job>/llm_raw/,
which is git-ignored with the rest of data/.
"""
from __future__ import annotations

from pathlib import Path

ATTEMPT_TASKS = ("tailor_cv", "tailor_profile")      # named by tailoring attempt: tailor_cv-2.json


class RawRecorder:
    def __init__(self, inner, folder: Path):
        self._inner, self.folder = inner, Path(folder)
        self.attempt: int | None = None               # set by the tailoring loop
        self._counts: dict[str, int] = {}

    def __getattr__(self, name):                      # only called for what the wrapper does not define itself
        return getattr(self._inner, name)

    def json(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        try:
            return self._inner.json(task, system, user, context=context)
        finally:                                      # also when the reply was not valid JSON and the call raised
            self._save(task)

    def _save(self, task: str) -> None:
        raw = getattr(self._inner, "last_raw", None)
        if raw is None:
            return                                    # no reply at all (network error, no provider)
        self._counts[task] = n = self._counts.get(task, 0) + 1
        key = self.attempt if task in ATTEMPT_TASKS and self.attempt else n
        self.folder.mkdir(parents=True, exist_ok=True)
        path, extra = self.folder / f"{task}-{key}.json", 1
        while path.exists():                          # a retry in the same attempt, or a later regeneration
            extra += 1
            path = self.folder / f"{task}-{key}-{extra}.json"
        path.write_text(raw if isinstance(raw, str) else str(raw), encoding="utf-8")
