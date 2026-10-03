"""LLM router over OpenAI-compatible endpoints (Groq, Ollama, any other) with quota-aware fallback.

HTTP errors keep their status and (truncated) body in the LLMError and in an `events` row. A 429 is retried after the
provider's Retry-After, else 2 s, 4 s, 8 s, before the router falls through to the next provider; a 400 on a request
carrying `response_format` is retried once without it.

The deterministic stand-in for tests and `--mock` runs lives in `mock.py`.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..db import DB

BACKOFF_S = (2, 4, 8)      # waits before the 1st, 2nd and 3rd retry of a 429 that names no Retry-After
BODY_MAX = 500             # chars of an error response body kept in messages and events; headers are never kept


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # reasoning models
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(cand)
    except json.JSONDecodeError as e:
        raise LLMError(f"model did not return valid JSON: {e}: {text[:300]}") from e


def retry_after_s(r: requests.Response) -> float | None:
    """Seconds named by a Retry-After header (delta-seconds or HTTP date); None when absent or unreadable."""
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        return max(float(v), 0.0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


class Provider:
    def __init__(self, cfg: dict, db: DB, temperature: float, timeout: int, max_wait: float = 60.0):
        self.cfg, self.db, self.temperature, self.timeout, self.max_wait = cfg, db, temperature, timeout, max_wait
        self.name = cfg["name"]
        self._last = 0.0
        self.json_mode = bool(cfg.get("json_mode"))
        self.json_mode_rejected = False     # set when a 400 on response_format was cured by dropping it
        self.last_model: str | None = None  # model id the server reported in its last good response

    def available(self) -> bool:
        env = self.cfg.get("api_key_env")
        if env and not os.environ.get(env):
            return False
        return self.db.llm_calls_today(self.name) < self.cfg.get("rpd", 10**9)

    def _post(self, body: dict) -> requests.Response:
        gap = 60.0 / max(self.cfg.get("rpm", 60), 1)
        wait = self._last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json"}
        env = self.cfg.get("api_key_env")
        if env:
            headers["Authorization"] = f"Bearer {os.environ[env]}"
        self._last = time.time()
        r = requests.post(f"{self.cfg['base_url'].rstrip('/')}/chat/completions",
                          headers=headers, json=body, timeout=self.timeout)
        self.db.llm_count(self.name)
        return r

    def _send(self, body: dict) -> requests.Response:
        """POST, retrying a 429 up to len(BACKOFF_S) times. A Retry-After longer than max_wait (a spent daily quota,
        say) is not waited for: the 429 is returned at once so the router can try the next provider."""
        for i in range(len(BACKOFF_S) + 1):
            r = self._post(body)
            if r.status_code != 429 or i == len(BACKOFF_S):
                return r
            wait = retry_after_s(r)
            wait = BACKOFF_S[i] if wait is None else wait
            if wait > self.max_wait:
                return r
            _sleep(wait)
        return r  # pragma: no cover — the loop always returns

    def _body_text(self, r: requests.Response) -> str:
        text = r.text or ""
        key = os.environ.get(self.cfg.get("api_key_env") or "")
        if key:
            text = text.replace(key, "***")       # some APIs echo the key back in their error message
        return text[:BODY_MAX]

    def _record(self, r: requests.Response, note: str = "") -> str:
        """Log one HTTP error to `events` (status + truncated body only) and return the message for the LLMError."""
        body = self._body_text(r)
        detail = {"provider": self.name, "status": r.status_code, "body": body}
        if note:
            detail["note"] = note
        self.db.log("", "llm_error", detail)
        return f"{self.name}: HTTP {r.status_code}: {body}"

    def chat(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.cfg["model"],
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self._send(body)
        if r.status_code == 400 and "response_format" in body:
            self._record(r, "retrying without response_format")
            plain = {k: v for k, v in body.items() if k != "response_format"}
            r = self._send(plain)
            if r.ok:
                try:
                    extract_json(r.json()["choices"][0]["message"]["content"])
                except (LLMError, ValueError, KeyError, IndexError, TypeError):
                    pass                         # worked over HTTP but gave no JSON: not proof of anything
                else:
                    self.json_mode_rejected, self.json_mode = True, False   # no point sending it again this run
        if not r.ok:
            raise LLMError(self._record(r), r.status_code)
        data = r.json()
        self.last_model = data.get("model")
        return data["choices"][0]["message"]["content"]


class Router:
    def __init__(self, settings, db: DB):
        c = settings["llm"]
        self.providers = [Provider(p, db, c.get("temperature", 0.3), c.get("timeout_s", 120),
                                   c.get("max_retry_wait_s", 60)) for p in c["providers"]]

    def json(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        errors = []
        for p in self.providers:
            if not p.available():
                errors.append(f"{p.name}: unavailable (no key or daily quota used)")
                continue
            for attempt in range(2):
                try:
                    return extract_json(p.chat(system, user))
                except (LLMError, requests.RequestException) as e:
                    errors.append(str(e))
                    if getattr(e, "status", None) == 429:
                        break          # quota — already retried with back-off, next provider
                    _sleep(2 * (attempt + 1))
        raise LLMError("all providers failed: " + " | ".join(errors))
