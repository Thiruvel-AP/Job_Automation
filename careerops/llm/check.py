"""`careerops llm-check`: one tiny real request per provider. Reports; never prints a key or a header."""
from __future__ import annotations

import os
import time

import requests

from .router import LLMError, Provider, Router, extract_json

SYSTEM = "Reply with a JSON object only, no other text."
USER = 'Return exactly {"ok": true}'


def check_provider(p: Provider, model: str | None = None, task: str | None = None) -> dict:
    """{provider, configured_model, reachable, ok, model, latency_ms, json_ok, json_mode, error}.
    `json_mode` is "accepted" | "rejected" | "off" (not configured) | "unknown" (no answer to judge it by);
    "rejected" means the provider returned HTTP 400 for response_format but answered valid JSON without it.
    `model` checks that model on this provider instead of its own (a task's llm.models entry); `task` is then named."""
    out = {"provider": p.name, "configured_model": model or p.cfg["model"], "reachable": False, "ok": False, "model": None,
           "latency_ms": None, "json_ok": False, "json_mode": "unknown", "error": None, "task": task}
    env = p.cfg.get("api_key_env")
    if env and not os.environ.get(env):
        out["error"] = f"no key: {env} is not set"
        return out
    p.json_mode_rejected = False
    start = time.perf_counter()
    try:
        text = p.chat(SYSTEM, USER, "llm_check", max_tokens=200, model=model)   # reasoning models spend part of it thinking
    except LLMError as e:                      # the server answered, with an error status
        out.update(reachable=True, error=str(e), latency_ms=_ms(start))
        return out
    except requests.RequestException as e:     # no answer at all
        out.update(error=f"{type(e).__name__}: {e}", latency_ms=_ms(start))
        return out
    out.update(reachable=True, model=p.last_model, latency_ms=_ms(start))
    try:
        out["json_ok"] = extract_json(text) is not None
    except LLMError as e:
        out["error"] = str(e)
    configured = bool(p.cfg.get("json_mode"))
    out["json_mode"] = ("rejected" if p.json_mode_rejected else "accepted" if configured and out["json_ok"]
                        else "off" if not configured else "unknown")
    out["ok"] = out["json_ok"]
    return out


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def check_task_models(router: Router) -> list[dict]:
    """One check per llm.models entry whose provider is in play, on that provider with that model."""
    by_name = {p.name: p for p in router.providers}
    return [check_provider(by_name[m["provider"]], m["model"], task)
            for task, m in router.task_models.items() if m["provider"] in by_name]


def check_all(router: Router) -> list[dict]:
    return [check_provider(p) for p in router.providers] + check_task_models(router)


def format_row(r: dict) -> list[str]:
    task = f" ({r['task']})" if r.get("task") else ""
    lines = [f"{'[ok]' if r['ok'] else '[!!]'} {r['provider']}{task}"
             f"  reachable={'yes' if r['reachable'] else 'no'}"
             f"  model={r['model'] or '-'} (configured {r['configured_model']})"
             f"  latency={'-' if r['latency_ms'] is None else str(r['latency_ms']) + 'ms'}"
             f"  json={'ok' if r['json_ok'] else 'no'}  json_mode={r['json_mode']}"]
    if r["json_mode"] == "rejected":
        lines.append(f"     → {r['provider']} rejects response_format (HTTP 400): set `json_mode: false` for it "
                     "in config/settings.yaml → llm.providers")
    if r["error"]:
        lines.append(f"     → {r['error']}")
    return lines
