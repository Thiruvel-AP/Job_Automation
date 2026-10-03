"""Pace calls to a provider's rate limits so a 429 is the exception, not the pacing mechanism.

Groq answers every call with `x-ratelimit-remaining-tokens` / `-reset-tokens` (per-minute token budget) and
`x-ratelimit-remaining-requests` / `-reset-requests` (daily request budget); the resets are Go-style durations
("1m26.4s", "2.085s", "250ms"). Groq admits a request against the token budget as input + max_tokens, so that is the
cost estimated before each call. Until a provider has sent such headers (and for providers that never do, e.g. a local
Ollama), a local sliding one-minute window built from the configured `tpm` and `rpm` stands in.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Callable, Mapping

CHARS_PER_TOKEN = 3.5      # no tokenizer dependency; English is ~4, JSON/LaTeX denser, so 3.5 errs towards waiting
WINDOW_S = 60.0
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def estimate_tokens(*texts: str) -> int:
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN) + 1


def parse_duration(v: str | None) -> float | None:
    """'1m26.4s' -> 86.4, '2.085s' -> 2.085, '250ms' -> 0.25; a bare number is seconds; None if unreadable."""
    if not v:
        return None
    v = v.strip()
    try:
        return float(v)
    except ValueError:
        pass
    parts = _DURATION.findall(v)
    if not parts or "".join(n + u for n, u in parts) != v:
        return None
    return sum(float(n) * _UNIT[u] for n, u in parts)


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Pacer:
    """One per provider. `wait_needed(cost)` says how long to sleep before sending; `sent` / `received` keep it informed."""

    def __init__(self, tpm: int | None, rpm: int | None, clock: Callable[[], float] = time.time):
        self.tpm, self.rpm, self.clock = tpm or None, rpm or None, clock
        self._sent: deque[tuple[float, int]] = deque()          # (time, estimated tokens) in the last minute
        self.remaining_tokens: int | None = None                # from headers; None until the provider sends them
        self.tokens_reset_at = 0.0
        self.token_limit: int | None = None
        self.remaining_requests: int | None = None
        self.requests_reset_at = 0.0

    # ---- before a call
    def wait_needed(self, cost: int) -> float:
        now = self.clock()
        while self._sent and self._sent[0][0] <= now - WINDOW_S:
            self._sent.popleft()
        need = 0.0
        limit = self.token_limit or self.tpm
        cost = min(cost, limit) if limit else cost              # a call bigger than the whole budget just gets an empty one
        if self.remaining_tokens is not None:                   # the provider's own count wins while it is current
            if now < self.tokens_reset_at and cost > self.remaining_tokens:
                need = self.tokens_reset_at - now
        elif self.tpm:
            over = sum(t for _, t in self._sent) + cost - self.tpm
            acc = 0
            for at, t in self._sent:
                acc += t
                if acc >= over > 0:
                    need = max(need, at + WINDOW_S - now)
                    break
        if self.rpm and len(self._sent) >= self.rpm:
            need = max(need, self._sent[len(self._sent) - self.rpm][0] + WINDOW_S - now)
        if self.remaining_requests is not None and self.remaining_requests < 1 and now < self.requests_reset_at:
            need = max(need, self.requests_reset_at - now)
        return max(need, 0.0)

    def sent(self, cost: int) -> None:
        self._sent.append((self.clock(), cost))

    # ---- after a call
    def received(self, headers: Mapping[str, str]) -> None:
        now = self.clock()
        rem, reset = _int(headers.get("x-ratelimit-remaining-tokens")), parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if rem is not None:
            self.remaining_tokens, self.tokens_reset_at = rem, now + (reset or 0.0)
            self.token_limit = _int(headers.get("x-ratelimit-limit-tokens")) or self.token_limit
        rem, reset = _int(headers.get("x-ratelimit-remaining-requests")), parse_duration(headers.get("x-ratelimit-reset-requests"))
        if rem is not None:
            self.remaining_requests, self.requests_reset_at = rem, now + (reset or 0.0)
