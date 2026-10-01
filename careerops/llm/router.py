"""LLM router over OpenAI-compatible endpoints (Groq, Ollama, any other) with quota-aware fallback.

The deterministic stand-in for tests and `--mock` runs lives in `mock.py`.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

from ..db import DB


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # reasoning models
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(cand)
    except json.JSONDecodeError as e:
        raise LLMError(f"model did not return valid JSON: {e}: {text[:300]}") from e


class Provider:
    def __init__(self, cfg: dict, db: DB, temperature: float, timeout: int):
        self.cfg, self.db, self.temperature, self.timeout = cfg, db, temperature, timeout
        self.name = cfg["name"]
        self._last = 0.0

    def available(self) -> bool:
        env = self.cfg.get("api_key_env")
        if env and not os.environ.get(env):
            return False
        return self.db.llm_calls_today(self.name) < self.cfg.get("rpd", 10**9)

    def chat(self, system: str, user: str) -> str:
        gap = 60.0 / max(self.cfg.get("rpm", 60), 1)
        wait = self._last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json"}
        env = self.cfg.get("api_key_env")
        if env:
            headers["Authorization"] = f"Bearer {os.environ[env]}"
        body: dict[str, Any] = {
            "model": self.cfg["model"],
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.cfg.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
        self._last = time.time()
        r = requests.post(f"{self.cfg['base_url'].rstrip('/')}/chat/completions",
                          headers=headers, json=body, timeout=self.timeout)
        self.db.llm_count(self.name)
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMError(f"{self.name}: HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class Router:
    def __init__(self, settings, db: DB):
        c = settings["llm"]
        self.providers = [Provider(p, db, c.get("temperature", 0.3), c.get("timeout_s", 120)) for p in c["providers"]]

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
                    if "HTTP 429" in str(e):
                        break          # quota — next provider, don't hammer
                    time.sleep(2 * (attempt + 1))
        raise LLMError("all providers failed: " + " | ".join(errors))
