"""Deterministic stand-in for the LLM router, used by tests and `--mock` runs.

It receives each task's structured context instead of a prompt, so the whole pipeline is exercisable
without a model. Handlers can be overridden per task (tests use this to simulate a fabricating model).
"""
from __future__ import annotations

from typing import Callable

from .router import LLMError


class MockLLM:
    """Deterministic: picks bullets by keyword overlap, never rephrases. Good enough to test plumbing."""

    def __init__(self, handlers: dict[str, Callable[[dict], dict]] | None = None):
        self.handlers = handlers or {}
        self.calls: list[str] = []

    def json(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        self.calls.append(task)
        ctx = context or {}
        if task in self.handlers:
            return self.handlers[task](ctx)
        if task == "extract_keywords":
            return {"required": ctx.get("fallback_required", []), "preferred": ctx.get("fallback_preferred", []),
                    "company_hook": ""}
        if task == "tailor_cv":
            kws = [k["term"] for k in ctx["keywords"]]
            def overlap(b):
                t = (b["text"] + " " + " ".join(b.get("terms", []))).lower()
                return sum(k in t for k in kws)
            exp = {e["id"]: [b["id"] for b in sorted(e["bullets"], key=overlap, reverse=True)[:3]]
                   for e in ctx["experience"]}
            projs = sorted(ctx["projects"], key=lambda p: sum(overlap(b) for b in p["bullets"]), reverse=True)[:3]
            return {"profile": ". ".join(ctx["profile_facts"][:2]) + ".",
                    "experience": exp,
                    "projects": [p["id"] for p in projs],
                    "rephrase": {}}
        if task == "cover_letter":
            return {"paragraphs": [
                f"{ctx['company']} is hiring a {ctx['title']}, and my recent work maps directly onto it.",
                ctx["evidence"][0] if ctx["evidence"] else "",
                "Could we find twenty minutes next week to talk about where I could contribute first?",
            ]}
        raise LLMError(f"MockLLM has no handler for task {task}")
