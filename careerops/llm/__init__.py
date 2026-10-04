"""LLM access: OpenAI-compatible router with quota-aware fallback, plus a deterministic mock."""
from .mock import MockLLM
from .router import BudgetExhausted, LLMError, Router, TaskUnavailable, extract_json

__all__ = ["BudgetExhausted", "LLMError", "MockLLM", "Router", "TaskUnavailable", "extract_json"]
