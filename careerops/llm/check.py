"""`careerops llm-check`: one tiny real request per provider. Reports; never prints a key or a header."""
from __future__ import annotations

import os
import time

import requests

from .router import LLMError, Provider, Router, extract_json

SYSTEM = "Reply with a JSON object only, no other text."
USER = 'Return exactly {"ok": true}'
CHECK_TIMEOUT_S = 60       # a tiny request: a model that needs longer than this to answer is as good as unreachable (a cold
                           # local model loading onto the GPU is the slow case)

# Every task the pipeline sends to a model. A task listed in llm.models / llm.task_providers but not here is checked too.
TASKS = ("extract_keywords", "rephrase_bullets", "cover_letter", "judge")

# The kinds `kind` can take, and what to do about each.
ADVICE = {
    "model_not_found": "the provider does not have this model id: check the id (console.groq.com/docs/models, `ollama list`) "
                       "and llm.providers / llm.models in config/settings.yaml",
    "auth": "the key was refused or is missing: check the key in .env (never in settings.yaml)",
    "timeout": f"no answer within {CHECK_TIMEOUT_S} s: the model is overloaded or still loading; try again",
    "unreachable": "the server cannot be reached: start it (ollama serve) or check base_url",
    "rate_limited": "the provider answered 429: the quota is spent or the rate is too high; try later",
    "bad_json": "the model answered, but not with JSON",
    "http_error": "the provider answered with an error",
}


def classify(status: int | None, message: str, exc: Exception | None = None) -> str:
    """What kind of failure this was: model_not_found | auth | timeout | unreachable | rate_limited | bad_json | http_error."""
    text = (message or "").lower()
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "unreachable"
    if status in (401, 403) or "no key:" in text:
        return "auth"
    if status == 429:
        return "rate_limited"
    if (status in (400, 404) and ("model" in text and any(w in text for w in ("not found", "does not exist", "decommissioned",
                                                                              "not exist", "unknown", "invalid")))) \
            or "model_not_found" in text or "model_decommissioned" in text:
        return "model_not_found"
    if status is None and "valid json" in text:
        return "bad_json"
    return "http_error" if status else "unreachable"


def check_provider(p: Provider, model: str | None = None, task: str | None = None) -> dict:
    """{provider, configured_model, reachable, ok, model, latency_ms, json_ok, json_mode, error, task, status, kind}.
    `json_mode` is "accepted" | "rejected" | "off" (not configured) | "unknown" (no answer to judge it by);
    "rejected" means the provider returned HTTP 400 for response_format but answered valid JSON without it.
    `model` checks that model on this provider instead of its own (a task's llm.models entry); `task` is then named.
    `kind` is "" when it worked, else classify()'s word for what went wrong; `status` is the HTTP status when there was one."""
    out = {"provider": p.name, "configured_model": model or p.cfg["model"], "reachable": False, "ok": False, "model": None,
           "latency_ms": None, "json_ok": False, "json_mode": "unknown", "error": None, "task": task, "status": None, "kind": ""}
    env = p.cfg.get("api_key_env")
    if env and not os.environ.get(env):
        out.update(error=f"no key: {env} is not set", kind="auth")
        return out
    p.json_mode_rejected = False
    timeout, p.timeout = p.timeout, min(p.timeout, CHECK_TIMEOUT_S)
    start = time.perf_counter()
    try:
        text = p.chat(SYSTEM, USER, "llm_check", max_tokens=200, model=model)   # reasoning models spend part of it thinking
    except LLMError as e:                      # the server answered, with an error status
        out.update(reachable=True, error=str(e), latency_ms=_ms(start), status=e.status, kind=classify(e.status, str(e)))
        return out
    except requests.RequestException as e:     # no answer at all
        out.update(error=f"{type(e).__name__}: {e}", latency_ms=_ms(start), kind=classify(None, str(e), e))
        return out
    finally:
        p.timeout = timeout
    out.update(reachable=True, model=p.last_model, latency_ms=_ms(start))
    try:
        out["json_ok"] = extract_json(text) is not None
    except LLMError as e:
        out.update(error=str(e), kind="bad_json")
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


def tasks_to_check(router: Router) -> list[str]:
    """TASKS, then any other task the settings give a model or a provider restriction."""
    extra = [t for t in [*router.task_models, *router.task_providers] if t not in TASKS]
    return [*TASKS, *dict.fromkeys(extra)]


def check_tasks(router: Router, tasks: list[str] | None = None) -> list[dict]:
    """One check for every (task, provider, model) the router could send a task to, in the router's order: the providers a
    task is restricted to (llm.task_providers), else all, each with the task's own model (llm.models) or the provider's. A
    model that serves several tasks is asked once and its answer is reported for each task."""
    done: dict[tuple[str, str], dict] = {}
    rows = []
    for task in tasks or tasks_to_check(router):
        for p, model in router.serving(task):
            k = (p.name, model)
            if k not in done:
                done[k] = check_provider(p, None if model == p.cfg["model"] else model)
            rows.append({**done[k], "task": task})
    return rows


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
