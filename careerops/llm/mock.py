"""Deterministic stand-in for the LLM router, used by tests and `--mock` runs.

It receives each task's structured context instead of a prompt, so the whole pipeline is exercisable
without a model. Handlers can be overridden per task (tests use this to simulate a fabricating model).
"""
from __future__ import annotations

import json
from typing import Callable

from .router import LLMError


class MockLLM:
    """Deterministic: never rewords a bullet. Good enough to test plumbing. (No profile and no hook task: both are assembled in code.)"""

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
        if task == "plain_bullet":                                          # v3.4: plumbing only, never a real plain version
            return {"plain": ctx.get("original") or ""}
        if task == "cover_letter" and ctx.get("mode") == "fluency":         # v3.4: the mock never rewords: the assembled draft as it is
            return dict(ctx.get("draft") or {})
        if task == "free_text_answer" and ctx.get("mode") == "fluency_motivation":
            return {"answer": ctx.get("draft") or ""}
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
        if task == "free_text_answer":                         # the first chosen source as it stands: plumbing only, never a real answer
            src = ctx.get("sources") or []
            return {"answer": src[0]["text"] if src else "", "ad_phrases": []}
        if task == "judge":                                    # a fixed second opinion: plumbing only, never a real judgement
            return {"would_send": True, "score": 4, "issues": []}
        if task == "job_eval":                                 # v3.2: a fixed, middling evaluation: plumbing only (Medium confidence, never 4.5+)
            dim = {"score": 3, "status": "partial", "source": "mock evaluation"}
            return {"dimensions": {k: dict(dim) for k in ("cv_match", "north_star", "comp", "culture", "red_flags")}, "global": 3.8,
                    "confidence": "Medium", "red_flags": [], "legitimacy": "medium", "top3_checks": ["mock: no real evaluation was made"]}
        raise LLMError(f"MockLLM has no handler for task {task}")
