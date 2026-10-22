"""Token visibility. Every answered call is filed in the database under (day, task, model) by the router (`DB.llm_task_add`); this module prints
what that comes to.

  * the tokens used TODAY for the cover_letter task, per model ("groq/openai/gpt-oss-120b: 12,445 tokens in 2 call(s)");
  * a WARNING for every provider/model whose daily token budget (`tpd`) has less than `llm.warn_budget_below` (default 20 %) left.

The budgets are the ones the router already enforces (`Provider.tokens_left`): the provider's own model is counted under the provider's name, any
other model (llm.models) under "<provider>/<model>".
"""
from __future__ import annotations

from typing import Callable

DEFAULT_BELOW = 0.20


def task_lines(db, task: str = "cover_letter") -> list[str]:
    rows = db.llm_task_today(task)
    if not rows:
        return [f"Tokens used today for {task}: none yet"]
    return [f"Tokens used today for {task}:"] + [f"  {model}: {tokens:,} tokens in {calls} call(s)" for model, calls, tokens in rows]


def below(settings) -> float:
    try:
        v = float(settings["llm"].get("warn_budget_below", DEFAULT_BELOW))
    except (TypeError, ValueError):
        return DEFAULT_BELOW
    return v if 0 <= v <= 1 else DEFAULT_BELOW


def budget_warnings(settings, db) -> list[str]:
    """One line for each (provider, model) with a daily token cap and under `llm.warn_budget_below` of it left."""
    llm = settings["llm"]
    models = {p["name"]: [] for p in llm["providers"]}
    for m in (llm.get("models") or {}).values():
        if isinstance(m, dict) and m.get("provider") in models and m.get("model"):
            models[m["provider"]].append(str(m["model"]))
    limit, out = below(settings), []
    for p in llm["providers"]:
        tpd = p.get("tpd")
        if not tpd:
            continue
        keys = [p["name"]] + [f"{p['name']}/{m}" for m in dict.fromkeys(models[p["name"]]) if m != p.get("model")]
        for key in keys:
            left = max(tpd - db.llm_tokens_today(key), 0)
            if left < limit * tpd:
                out.append(f"WARNING: {key} has {left:,} of {tpd:,} tokens left today ({int(100 * left / tpd)}%): under {int(round(100 * limit))}% of its daily budget")
    return out


def report(db, settings, out: Callable[[str], None] = print, task: str = "cover_letter") -> None:
    """Print the day's tokens for `task` per model, then any budget warning. Never raises: a count that cannot be read must not stop a run."""
    try:
        for line in task_lines(db, task):
            out(line)
    except Exception as e:  # noqa: BLE001
        out(f"(tokens used today for {task} could not be read: {e})")
    try:
        for line in budget_warnings(settings, db):
            out(line)
    except Exception as e:  # noqa: BLE001
        out(f"(the daily token budgets could not be read: {e})")
