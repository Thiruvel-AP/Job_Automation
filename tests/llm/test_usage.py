"""Token visibility: the tokens used TODAY for the cover_letter task, per model, and a warning when a provider's daily budget (tpd) is under 20 %
left. The router files every answered call under (day, task, model); the counts survive the process. A fake local server stands in for Groq."""
import pytest

from careerops.llm import Router, usage
from test_router_models import KEY, LETTER_MODEL, groq_settings, server  # noqa: F401  (fixtures)


def test_the_router_files_each_answered_call_under_its_task_and_model(groq_settings, db):
    r = Router(groq_settings, db)
    r.json("cover_letter", "s", "u")
    r.json("cover_letter", "s", "u")
    r.json("extract_keywords", "s", "u")
    assert db.llm_task_today("cover_letter") == [(KEY, 2, 200)]
    assert db.llm_task_today("extract_keywords") == [("groq/openai/gpt-oss-20b", 1, 100)]


def test_yesterdays_tokens_are_not_todays(groq_settings, db):
    db.conn.execute("INSERT INTO llm_task_usage (day, task, model, calls, tokens) VALUES ('2000-01-01', 'cover_letter', ?, 5, 9999)", (KEY,))
    db.conn.commit()
    assert db.llm_task_today("cover_letter") == []


def test_the_lines_list_each_model_with_its_tokens_and_calls(db):
    db.llm_task_add("cover_letter", f"groq/{LETTER_MODEL}", 12_345)
    db.llm_task_add("cover_letter", f"groq/{LETTER_MODEL}", 100)
    db.llm_task_add("cover_letter", "groq/openai/gpt-oss-20b", 800)
    db.llm_task_add("judge", "ollama/qwen2.5-7b-8k", 5_000)
    assert usage.task_lines(db, "cover_letter") == [
        "Tokens used today for cover_letter:",
        f"  groq/{LETTER_MODEL}: 12,445 tokens in 2 call(s)",
        "  groq/openai/gpt-oss-20b: 800 tokens in 1 call(s)"]


def test_with_no_call_today_the_line_says_none(db):
    assert usage.task_lines(db, "cover_letter") == ["Tokens used today for cover_letter: none yet"]


# ------------------------------------------------------------------ the 20 % warning
def spent(db, key: str, tokens: int) -> None:
    db.llm_count(key)
    db.llm_add_tokens(key, tokens)


def test_a_provider_with_more_than_20_percent_left_is_not_warned_about(groq_settings, db):
    tpd = groq_settings["llm"]["providers"][0]["tpd"]
    spent(db, KEY, int(tpd * 0.80))                                   # exactly 20 % left
    assert usage.budget_warnings(groq_settings, db) == []


def test_under_20_percent_left_warns_with_the_numbers(groq_settings, db):
    tpd = groq_settings["llm"]["providers"][0]["tpd"]
    spent(db, KEY, int(tpd * 0.80) + 1)
    [w] = usage.budget_warnings(groq_settings, db)
    assert w.startswith(f"WARNING: {KEY} has {tpd - int(tpd * 0.80) - 1:,} of {tpd:,} tokens left today (19%)") and "under 20%" in w


def test_each_model_is_counted_under_its_own_key(groq_settings, db):
    tpd = groq_settings["llm"]["providers"][0]["tpd"]
    spent(db, "groq", tpd - 10)                                       # the provider's own model is nearly out ...
    [w] = usage.budget_warnings(groq_settings, db)
    assert w.startswith("WARNING: groq has 10 of") and KEY not in w     # ... the letter model (its own key) is not


def test_a_provider_without_a_daily_cap_never_warns(settings, db):
    settings.raw["llm"]["providers"] = [p for p in settings.raw["llm"]["providers"] if p["name"] == "ollama"]
    settings.raw["llm"]["models"] = {}
    spent(db, "ollama", 10**9)
    assert usage.budget_warnings(settings, db) == []


def test_the_threshold_is_a_setting(groq_settings, db):
    tpd = groq_settings["llm"]["providers"][0]["tpd"]
    spent(db, KEY, int(tpd * 0.60))
    assert usage.budget_warnings(groq_settings, db) == []
    groq_settings.raw["llm"]["warn_budget_below"] = 0.5
    assert len(usage.budget_warnings(groq_settings, db)) == 1


def test_the_shipped_threshold_is_20_percent(settings):
    assert settings["llm"]["warn_budget_below"] == 0.2


def test_report_prints_the_letter_tokens_then_any_warning(groq_settings, db):
    tpd = groq_settings["llm"]["providers"][0]["tpd"]
    db.llm_task_add("cover_letter", KEY, 700)
    spent(db, KEY, tpd - 5)
    lines: list[str] = []
    usage.report(db, groq_settings, out=lines.append)
    assert lines[0] == "Tokens used today for cover_letter:" and lines[1].startswith(f"  {KEY}: ")
    assert lines[-1].startswith(f"WARNING: {KEY} has 5 of")


def test_report_never_raises(db):
    class Broken:
        raw = {}

        def __getitem__(self, k):
            raise RuntimeError("settings are broken")
    lines: list[str] = []
    usage.report(db, Broken(), out=lines.append)                    # a failing count must not stop a run
    assert any("could not be read" in l for l in lines)
