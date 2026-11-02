"""LLM router over OpenAI-compatible endpoints (Groq, Ollama, any other) with quota-aware fallback.

HTTP errors keep their status and (truncated) body in the LLMError and in an `events` row. A 429 is retried after the
provider's Retry-After, else 2 s, 4 s, 8 s, before the router falls through to the next provider; a 400 on a request
carrying `response_format` is retried once without it.

A task listed in `llm.task_providers` (cover_letter: [groq]) runs only on those providers. When none of them can take
the call (no key, daily budget spent, rate limited, context too small) the router raises TaskUnavailable instead of
falling through to another provider: the caller keeps the work for later.

A task listed in `llm.models` (cover_letter: {provider: groq, model: openai/gpt-oss-120b}) uses that model instead of the
provider's own `model` when it runs on that provider. A provider limits each model separately (Groq's tpm / tpd / rpd are
per model), so a non-default model is counted, paced and budgeted under its own key "<provider>/<model>".

A request is never sent to a provider whose `context_tokens` it would overflow (estimated input + max_tokens): that
provider is skipped, the reason is logged, and the next one is tried. `Router(only=name)` pins every call to one provider
with no fallback.

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
from .pacing import Pacer, estimate_tokens

DEFAULT_TPM, DEFAULT_RPM = 8000, 30     # used when a provider's config names no tpm / rpm (Groq free tier)
DEFAULT_MAX_TOKENS = 1000               # completion cap for a task with no llm.max_tokens entry and no `default`
BACKOFF_S = (2, 4, 8)      # waits before the 1st, 2nd and 3rd retry of a 429 that names no Retry-After
BODY_MAX = 500             # chars of an error response body kept in messages and events; headers are never kept


class BudgetExhausted(RuntimeError):
    """No provider can take a whole job within its daily token budget (tpd); raised before the job starts."""


class TaskUnavailable(RuntimeError):
    """A task restricted to certain providers (llm.task_providers) has none that can take it right now. Not an error in the
    model's answer: the work should wait (a job becomes `letter_pending`), never go to a provider the task excludes."""


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _pace_sleep(seconds: float) -> None:      # separate from _sleep so tests can watch retries and pacing apart
    time.sleep(seconds)


def _clock() -> float:
    return time.time()


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
    def __init__(self, cfg: dict, db: DB, temperature: float, timeout: int, max_wait: float = 60.0,
                 max_tokens: dict | None = None, seed: int | None = None):
        self.cfg, self.db, self.temperature, self.timeout, self.max_wait = cfg, db, temperature, timeout, max_wait
        self.seed = seed                    # sent as `seed` when set: the same prompt then gives the same answer (best effort)
        self.name = cfg["name"]
        self.max_tokens = max_tokens or {}
        # tpm/rpm default to Groq's free tier when the config names none; null/0 means no limit (e.g. local Ollama)
        self.pacer = Pacer(cfg.get("tpm", DEFAULT_TPM), cfg.get("rpm", DEFAULT_RPM), clock=lambda: _clock())
        self._pacers: dict[str, Pacer] = {}     # one per non-default model a task asked for (llm.models)
        self.json_mode = bool(cfg.get("json_mode"))
        self.reasoning_effort, self.include_reasoning = self.reasoning_for(str(cfg.get("model", "")))
        self.json_mode_rejected = False     # set when a 400 on response_format was cured by dropping it
        self.last_model: str | None = None  # model id the server reported in its last good response
        self.last_usage: dict = {}          # provider-reported token usage and finish_reason of the last good response

    def reasoning_for(self, model: str) -> tuple[str | None, bool | None]:
        """(reasoning_effort, include_reasoning) sent with `model`. Reasoning models (gpt-oss) spend max_tokens on hidden
        reasoning. For those, default to the cheapest effort and drop the reasoning text from the reply; any other model
        is sent neither. The config's own `reasoning_effort` / `include_reasoning` belong to the provider's own `model`: a
        task's other model (llm.models) gets the name-based default, because Groq refuses `reasoning_effort` for a model
        that does not reason (llama-3.3-70b-versatile, the judge) with a 400."""
        reasoning = "gpt-oss" in model
        if model != self.cfg.get("model"):
            return ("low" if reasoning else None), (False if reasoning else None)
        return (self.cfg.get("reasoning_effort", "low" if reasoning else None),
                self.cfg.get("include_reasoning", False if reasoning else None))

    def key(self, model: str | None = None) -> str:
        """What the daily counts and the pacer are filed under: the provider's name for its own model, else
        "<provider>/<model>" (a provider limits every model on its own)."""
        return self.name if not model or model == self.cfg["model"] else f"{self.name}/{model}"

    def pacer_for(self, model: str | None = None) -> Pacer:
        k = self.key(model)
        if k == self.name:
            return self.pacer
        if k not in self._pacers:
            self._pacers[k] = Pacer(self.cfg.get("tpm", DEFAULT_TPM), self.cfg.get("rpm", DEFAULT_RPM),
                                    clock=lambda: _clock())
        return self._pacers[k]

    def available(self, model: str | None = None) -> bool:
        env = self.cfg.get("api_key_env")
        if env and not os.environ.get(env):
            return False
        key, tpd = self.key(model), self.cfg.get("tpd")
        if tpd and self.db.llm_tokens_today(key) >= tpd:
            return False
        return self.db.llm_calls_today(key) < self.cfg.get("rpd", 10**9)

    def tokens_left(self, model: str | None = None) -> int | None:
        """Tokens left today under this provider's tpd (for `model`: that model's own count); None when it has no daily
        token cap."""
        tpd = self.cfg.get("tpd")
        return max(tpd - self.db.llm_tokens_today(self.key(model)), 0) if tpd else None

    def max_tokens_for(self, task: str) -> int:
        return int(self.max_tokens.get(task) or self.max_tokens.get("default") or DEFAULT_MAX_TOKENS)

    def overflow(self, system: str, user: str, task: str) -> str | None:
        """Why this request cannot go to this provider: its estimated input + completion cap exceeds `context_tokens`
        (a server that truncates a long prompt gives a wrong answer without saying so). None when it fits, or when the
        provider names no context size."""
        limit = self.cfg.get("context_tokens")
        if not limit:
            return None
        need = estimate_tokens(system, user) + self.max_tokens_for(task)
        if need <= limit:
            return None
        return (f"{self.name}: skipped, {task} needs ~{need:,} tokens (input ~{need - self.max_tokens_for(task):,} + "
                f"max_tokens {self.max_tokens_for(task):,}) but its context is {limit:,}")

    def _pace(self, cost: int, model: str | None = None) -> None:
        """Sleep until the provider's token/request window admits `cost` tokens. A wait beyond max_wait (a spent daily
        request budget) is not served: raise as a 429 so the router moves to the next provider."""
        wait = self.pacer_for(model).wait_needed(cost)
        if wait > self.max_wait:
            msg = (f"{self.name}: pacing would wait {wait:.0f}s (more than llm.max_retry_wait_s={self.max_wait:.0f}) "
                   f"before a ~{cost}-token call")
            self.db.log("", "llm_error", {"provider": self.name, "status": 429, "body": msg})
            raise LLMError(msg, 429)
        if wait > 0:
            _pace_sleep(wait + 0.05)

    def _post(self, body: dict, cost: int) -> requests.Response:
        model = body.get("model")
        self._pace(cost, model)
        headers = {"Content-Type": "application/json"}
        env = self.cfg.get("api_key_env")
        if env:
            headers["Authorization"] = f"Bearer {os.environ[env]}"
        pacer = self.pacer_for(model)
        pacer.sent(cost)
        r = requests.post(f"{self.cfg['base_url'].rstrip('/')}/chat/completions",
                          headers=headers, json=body, timeout=self.timeout)
        pacer.received(r.headers)
        self.db.llm_count(self.key(model))
        return r

    def _send(self, body: dict, cost: int) -> requests.Response:
        """POST, retrying a 429 up to len(BACKOFF_S) times. A Retry-After longer than max_wait (a spent daily quota,
        say) is not waited for: the 429 is returned at once so the router can try the next provider."""
        for i in range(len(BACKOFF_S) + 1):
            r = self._post(body, cost)
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

    def chat(self, system: str, user: str, task: str = "", max_tokens: int | None = None,
             model: str | None = None, temperature: float | None = None) -> str:
        """One completion. `model` overrides the provider's own model for this call (llm.models); `temperature` the configured one."""
        cap = max_tokens or self.max_tokens_for(task)
        model = model or self.cfg["model"]
        effort, include = self.reasoning_for(model)
        body: dict[str, Any] = {
            "model": model,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": cap,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.seed is not None:
            body["seed"] = self.seed
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if effort:
            body["reasoning_effort"] = effort
        if include is not None:
            body["include_reasoning"] = bool(include)
        cost = estimate_tokens(system, user) + cap            # what Groq admits the call against: input + max_tokens
        r = self._send(body, cost)
        if r.status_code == 400 and "response_format" in body:
            self._record(r, "retrying without response_format")
            plain = {k: v for k, v in body.items() if k != "response_format"}
            r = self._send(plain, cost)
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
        usage = data.get("usage") or {}
        self.last_usage = {"max_tokens": cap, "est_input_tokens": cost - cap,
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens"),
                           # hidden reasoning is part of completion_tokens; Groq reports it separately when it can
                           "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                           "total_tokens": usage.get("total_tokens"),
                           "finish_reason": data["choices"][0].get("finish_reason")}
        if usage.get("total_tokens"):
            self.db.llm_add_tokens(self.key(model), usage["total_tokens"])
        return data["choices"][0]["message"]["content"]


class Router:
    def __init__(self, settings, db: DB, only: str | None = None):
        """`only` pins every call to the provider of that name: the others are not even built, so nothing can fall back
        to them. Raises ValueError, naming the configured providers, if there is no such provider."""
        c = settings["llm"]
        self.db = db
        # a provider's own `seed` (null = never send one) wins over llm.seed
        self.providers = [Provider(p, db, c.get("temperature", 0.3), c.get("timeout_s", 120),
                                   c.get("max_retry_wait_s", 60), c.get("max_tokens"), p.get("seed", c.get("seed")))
                          for p in c["providers"]]
        self.pinned = only
        self.task_providers: dict[str, list[str]] = {t: list(n) for t, n in (c.get("task_providers") or {}).items()}
        # llm.models: {task: {provider, model}}: that task uses `model` on `provider` (other providers keep their own)
        self.task_models: dict[str, dict] = {}
        self.unknown_models: list[str] = []
        known = [q["name"] for q in c["providers"]]
        for task, m in (c.get("models") or {}).items():
            if not isinstance(m, dict) or not m.get("provider") or not m.get("model"):
                raise ValueError(f"llm.models.{task}: expected {{provider: <name>, model: <model id>}}, got {m!r}")
            if m["provider"] not in known:       # not an error here (a run may use other providers): doctor reports it
                self.unknown_models.append(f"llm.models.{task}: no LLM provider named {m['provider']!r}; configured: "
                                           + ", ".join(known))
                continue
            self.task_models[task] = {"provider": str(m["provider"]), "model": str(m["model"])}
        if only is not None:
            self.providers = [p for p in self.providers if p.name == only]
            if not self.providers:
                raise ValueError(f"no LLM provider named {only!r}; configured: "
                                 + ", ".join(p["name"] for p in c["providers"]))
        self.usage: list[dict] = []     # one entry per answered call; tailor_job copies its slice into report.json
        self._allowed: set[str] | None = None   # providers the current job may use (set by begin_job)
        self.last_raw: str | None = None        # the reply text of the last answered call, kept even if it was not valid JSON

    def model_for(self, p: Provider, task: str) -> str | None:
        """The model `task` uses on provider `p` when llm.models names one for that pair, else None (p's own model)."""
        m = self.task_models.get(task)
        return m["model"] if m and m["provider"] == p.name else None

    def serving(self, task: str) -> list[tuple[Provider, str]]:
        """Every (provider, model) this task could be sent to, in the order json() tries them: the providers it is restricted
        to (llm.task_providers), else all, each with the task's own model on that provider (llm.models) or its own."""
        only = self.task_providers.get(task)
        return [(p, self.model_for(p, task) or p.cfg["model"]) for p in self.providers if only is None or p.name in only]

    def begin_job(self, est_tokens: int, task: str | None = None) -> list[str]:
        """Check the daily token budgets before a job starts, so a job is never begun that cannot be finished.
        Providers that can take `est_tokens` more today stay in play for this job, in config order; one whose tpd
        would be exceeded is skipped (the next provider, e.g. a local Ollama, takes the job). Returns their names.
        `task` (finishing a letter: "cover_letter") checks the budget of the model that task uses, not the default one.
        Raises BudgetExhausted, with each provider's reason, if none can."""
        ok, why = [], []
        for p in self.providers:
            model = self.model_for(p, task) if task else None
            left = p.tokens_left(model)
            if not p.available(model):
                why.append(f"{p.name}: unavailable (no key, daily request or token quota used)")
            elif left is not None and left < est_tokens:
                why.append(f"{p.name}: {p.cfg['tpd'] - left:,} of {p.cfg['tpd']:,} tokens used today, "
                           f"{left:,} left, a job needs ~{est_tokens:,}")
            else:
                ok.append(p.name)
        if not ok:
            raise BudgetExhausted("; ".join(why))
        self._allowed = set(ok)
        return ok

    def end_job(self) -> None:
        self._allowed = None

    def json(self, task: str, system: str, user: str, context: dict | None = None) -> dict:
        """`context` is the task's structured context (what a mock reads); its `temperature`, when it has one, is this call's sampling temperature."""
        errors = []
        self.last_raw = None
        temperature = (context or {}).get("temperature")
        only = self.task_providers.get(task)          # None: any provider may take this task
        failed_for_real = False                        # an answer that was wrong, not a provider that was unavailable
        for p in self.providers:
            if only is not None and p.name not in only:
                errors.append(f"{p.name}: {task} is restricted to {', '.join(only)}")
                continue
            if self._allowed is not None and p.name not in self._allowed:
                errors.append(f"{p.name}: skipped for this job (daily token budget)")
                continue
            model = self.model_for(p, task)               # None: this provider's own model
            if not p.available(model):
                errors.append(f"{p.name}: unavailable (no key, daily request or token quota used"
                              + (f" for {model}" if model else "") + ")")
                continue
            too_big = p.overflow(system, user, task)
            if too_big:
                self.db.log("", "llm_skip", {"provider": p.name, "task": task, "reason": too_big})
                errors.append(too_big)
                continue
            for attempt in range(2):
                try:
                    text = p.chat(system, user, task, model=model, temperature=temperature)
                    self.last_raw = text
                    self.usage.append({"task": task, "provider": p.name,
                                       "model": p.last_model or model or p.cfg.get("model"), **p.last_usage})
                    self.db.llm_task_add(task, f"{p.name}/{self.usage[-1]['model']}", p.last_usage.get("total_tokens") or 0)    # llm/usage.py
                    try:
                        return extract_json(text)
                    except LLMError as e:
                        if p.last_usage.get("finish_reason") == "length":
                            raise LLMError(f"{p.name}: {task} output cut off at max_tokens={p.last_usage['max_tokens']}"
                                           f" — raise llm.max_tokens.{task} in settings.yaml") from e
                        raise
                except (LLMError, requests.RequestException) as e:
                    errors.append(str(e))
                    if getattr(e, "status", None) == 429 or "output cut off" in str(e):
                        failed_for_real = failed_for_real or "output cut off" in str(e)
                        break          # quota (already backed off) or a cap the same call would hit again: next provider
                    failed_for_real = True
                    _sleep(2 * (attempt + 1))
        if only is not None and not failed_for_real:
            raise TaskUnavailable(f"{task} can only run on {', '.join(only)}, and none can take it now: "
                                  + " | ".join(errors))
        raise LLMError("all providers failed: " + " | ".join(errors))
