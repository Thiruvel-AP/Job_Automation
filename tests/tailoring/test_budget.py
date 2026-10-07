"""Every model call stays well inside the provider's per-minute budget (Groq free tier: 8K TPM, admitted as estimated
input + max_tokens), however large the fact bank is: code selects the bullets and the rephrase calls carry <= 5 of them,
so the size of the bank never reaches a prompt."""
import copy

from careerops.llm.pacing import estimate_tokens
from careerops.tailoring import tailor

LIMIT = 7500                         # Groq's 8000 TPM minus a margin for the estimate's error


def _cost(call, settings):
    caps = settings["llm"]["max_tokens"]
    return estimate_tokens(call["system"], call["user"]) + (caps.get(call["task"]) or caps["default"])


def _big_bank(fb, experiences=8, bullets=6, projects=8, words=60):
    """The fixture fact bank blown up to the size of a long career: many entries, long bullets."""
    big = copy.deepcopy(fb)
    big["skills"] = {}                    # a keyword the skills section already lists is not "missed", so nothing would be asked
    filler = " ".join(["delivered measurable improvements across several workstreams"] * (words // 6))
    big["experience"] = [{"id": f"x{i}", "title": f"Role {i}", "org": f"Org {i}", "dates": "2020", "location": "UK",
                          "bullets": [{"id": f"x{i}b{j}", "text": f"Built system {i}.{j} with Python. {filler}",
                                       "terms": ["python", "langchain", "docker"] if j == 0 else ["docker"]}
                                      for j in range(bullets)]} for i in range(experiences)]
    big["projects"] = [{"id": f"q{i}", "name": f"Project {i}", "stack": "Python", "github": None,
                        "bullets": [{"id": f"q{i}b{j}", "text": f"Project {i} step {j}. {filler}",
                                     "terms": ["pytorch"] if j == 0 else []} for j in range(5)]}
                       for i in range(projects)]
    return big


def _all_calls_fit(llm, settings):
    assert llm.sent
    for call in llm.sent:
        assert _cost(call, settings) <= LIMIT, (call["task"], _cost(call, settings))


def test_fixture_fact_bank_calls_fit_the_budget(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    _all_calls_fit(llm, settings)
    assert {s["task"] for s in llm.sent} >= {"extract_keywords", "tailor_profile", "cover_letter"}


def test_a_real_sized_fact_bank_does_not_make_any_call_bigger(settings, fb_fixture, job, spy_llm):
    big = _big_bank(fb_fixture)
    llm = spy_llm()
    tailor.tailor_job(job, big, llm, settings, settings.path("outputs"))
    _all_calls_fit(llm, settings)
    batches = [s for s in llm.sent if s["task"] == "rephrase_bullets"]
    assert batches and all(len(s["context"]["bullets"]) <= tailor.REPHRASE_BATCH == 5 for s in batches)
    small = spy_llm()
    tailor.tailor_job({**job, "id": "manual:small"}, fb_fixture, small, settings, settings.path("outputs"))
    biggest = lambda l: max(estimate_tokens(s["system"], s["user"]) for s in l.sent if s["task"] == "rephrase_bullets")
    assert biggest(llm) < 3 * biggest(small)                       # five long bullets, not eight careers


def test_the_real_fact_bank_fits_too(settings, job, spy_llm):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    llm = spy_llm()
    tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    _all_calls_fit(llm, settings)


def test_the_unused_tailor_cv_budget_settings_are_gone(settings):
    assert "top_bullets" not in settings["llm"] and "max_call_tokens" not in settings["llm"]
    assert "tailor_cv" not in settings["llm"]["max_tokens"] and "rephrase_bullets" in settings["llm"]["max_tokens"]
