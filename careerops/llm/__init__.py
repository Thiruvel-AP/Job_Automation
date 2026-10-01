"""LLM access: OpenAI-compatible router with quota-aware fallback, plus a deterministic mock."""
from .mock import MockLLM
from .router import LLMError, Router, extract_json

__all__ = ["LLMError", "MockLLM", "Router", "extract_json"]
