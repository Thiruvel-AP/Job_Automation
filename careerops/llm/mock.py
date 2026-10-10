"""Deterministic stand-in for the LLM router, used by tests and `--mock` runs.

It receives each task's structured context instead of a prompt, so the whole pipeline is exercisable
without a model. Handlers can be overridden per task (tests use this to simulate a fabricating model).
"""
from __future__ import annotations

import json
from typing import Callable

from .router import LLMError


class MockLLM:
    """Deterministic: never rewords a bullet, picks the first two profile facts. Good enough to test plumbing."""

    def __init__(self, handlers: dict[str, Callable[[dict], dict]] | None = None):
        self.handlers = handlers or {}
        self.calls: list[str] = []
        self.usage: list[dict] = []        # the router records token usage here; the mock never has any
        self.last_raw: str | None = None   # JSON text of the last answer, as a model would have returned it

    def begin_job(self, est_tokens: int, task: str | None = None) -> list[str]:    # a mock has no token budget
        return ["mock"]

    def end_job(self) -> None:
        pass

    def json(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        self.last_raw = None
        out = self._answer(task, system, user, context)
        self.last_raw = json.dumps(out)
        return out

    @staticmethod
    def _profile(headline: str, facts: list[str]) -> str:
        """"<headline> with <fact>. <fact>", cut to 40 words so the profile guard (<= 45) always passes."""
        text = f"{headline} with " + ". ".join(f.rstrip(".") for f in facts)
        return " ".join(text.split()[:40]).rstrip(".,;") + "."

    def _answer(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        self.calls.append(task)
        ctx = context or {}
        if task in self.handlers:
            return self.handlers[task](ctx)
        if task == "extract_keywords":
            return {"required": ctx.get("fallback_required", []), "preferred": ctx.get("fallback_preferred", []),
                    "company_hook": "", "team": ""}
        if task == "rephrase_bullets":
            return {}                                          # the mock never rewords: every bullet keeps its text
        if task == "tailor_profile":
            if "previous" in ctx:                              # the guard's one retry: just the profile text
                return {"profile": self._profile(ctx["headline"], ctx["profile_facts"][:2])}
            facts = ctx["facts"][:2]
            return {"facts": [f["id"] for f in facts], "profile": self._profile(ctx["headline"], [f["text"] for f in facts])}
        if task == "cover_letter":
            gaps = ctx.get("gaps") or []
            return {"paragraphs": [
                f"{ctx['company']} is hiring a {ctx['title']}, and my recent work maps directly onto it.",
                # first person and short: the guard rejects a bare bullet and a sentence that is >= 70% the bullet's words
                (lambda e: "A concrete example from my own work is that I " + " ".join(e.split()[:6]).rstrip(" ,;:(")
                           + ".")(ctx["evidence"][0]) if ctx["evidence"] else "",
                # optional gap sentence: one required tool the candidate lacks, stated plainly, no apology
                *([f"I have not used {gaps[0]} yet, and I would pick it up quickly "
                   f"alongside the tools I already use."] if gaps else []),
                "Could we find twenty minutes next week to talk about where I could contribute first?",
            ]}
        if task == "judge":                                    # a fixed second opinion: plumbing only, never a real judgement
            return {"would_send": True, "score": 4, "issues": []}
        raise LLMError(f"MockLLM has no handler for task {task}")
