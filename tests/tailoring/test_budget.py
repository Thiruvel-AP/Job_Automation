"""tailor_cv prompts stay within the per-call token budget (estimated input + max_tokens <= llm.max_call_tokens)."""
import copy
from pathlib import Path

import pytest

from careerops.llm.pacing import estimate_tokens
from careerops.tailoring import budget, tailor
from careerops.tailoring.scorer import Keyword, taxonomy_keywords

LIMIT = 7500


def _kws(job):
    return taxonomy_keywords(job["description"])


def _sent(llm):
    return [s for s in llm.sent if s["task"] == "tailor_cv"]


def _cost(call, settings):
    return estimate_tokens(call["system"], call["user"]) + settings["llm"]["max_tokens"]["tailor_cv"]


def _big_bank(fb, experiences=8, bullets=6, projects=8, words=60):
    """The fixture fact bank blown up to the size of a long career: many entries, long bullets."""
    big = copy.deepcopy(fb)
    filler = " ".join(["delivered measurable improvements across several workstreams"] * (words // 6))
    big["experience"] = [{"id": f"x{i}", "title": f"Role {i}", "org": f"Org {i}", "dates": "2020", "location": "UK",
                          "bullets": [{"id": f"x{i}b{j}", "text": f"Built system {i}.{j} with Python. {filler}",
                                       "terms": ["python"] + (["langchain"] if j == 0 else [])}
                                      for j in range(bullets)]} for i in range(experiences)]
    big["projects"] = [{"id": f"q{i}", "name": f"Project {i}", "stack": "Python", "github": None,
                        "bullets": [{"id": f"q{i}b{j}", "text": f"Project {i} step {j}. {filler}",
                                     "terms": ["pytorch"] if j == 0 else []} for j in range(5)]}
                       for i in range(projects)]
    return big


def test_fixture_fact_bank_prompt_fits_the_budget(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    for call in _sent(llm):
        assert _cost(call, settings) <= LIMIT
    p = rep["attempts"][0]["prompt"]
    assert p["est_input_tokens"] + p["max_tokens"] <= LIMIT and p["max_tokens"] == 3000
    assert p["over_budget"] is False and 0 < p["bullets_sent"] <= p["bullets_total"]


def test_the_report_logs_the_estimated_prompt_tokens_per_attempt(settings, fb_fixture, job, spy_llm):
    llm = spy_llm()
    rep = tailor.tailor_job(job, fb_fixture, llm, settings, settings.path("outputs"))
    sent = _sent(llm)
    assert len(sent) == len(rep["attempts"])
    for call, rec in zip(sent, rep["attempts"]):
        assert rec["prompt"]["est_input_tokens"] == estimate_tokens(call["system"], call["user"])


def test_a_real_sized_fact_bank_is_cut_down_to_fit(settings, fb_fixture, job, spy_llm):
    big = _big_bank(fb_fixture)
    llm = spy_llm()
    prop, meta = tailor._proposal(job, big, _kws(job), llm, "", settings, "AI Engineer", None)
    call = _sent(llm)[0]
    assert _cost(call, settings) <= LIMIT and meta["over_budget"] is False
    assert meta["bullets_total"] == 8 * 6 + 8 * 5 and meta["bullets_sent"] < meta["bullets_total"]


def test_the_real_fact_bank_fits_too(settings, job, spy_llm):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    llm = spy_llm()
    _, meta = tailor._proposal(job, fb, _kws(job), llm, "", settings, "AI Engineer", None)
    assert _cost(_sent(llm)[0], settings) <= LIMIT and not meta["over_budget"]


def test_top_14_by_weighted_overlap_plus_the_best_bullet_of_every_experience(fb_fixture):
    kws = [Keyword("langchain", 2.0, True), Keyword("pytorch", 2.0, True), Keyword("sql", 1.0, False),
           Keyword("pytest", 1.0, False)]
    ids, must = budget.select_bullets(fb_fixture, kws, top_n=14)
    sc = budget.bullet_scores(fb_fixture, kws)
    ranked = sorted(sc, key=lambda b: -sc[b])
    assert set(ranked[:14]) <= set(ids)                                          # the top 14 are all there
    for e in fb_fixture["experience"]:                                           # one per experience entry, the best
        best = max((b["id"] for b in e["bullets"]), key=lambda b: (sc[b], -list(sc).index(b)))
        assert best in ids and best in must
    assert ids == sorted(ids, key=lambda b: -sc[b]) or len(set(sc[b] for b in ids)) < len(ids)   # rank order
    assert len(ids) <= 14 + len(fb_fixture["experience"])


def test_overlap_counts_a_bullets_terms_as_well_as_its_text(fb_fixture):
    fb = copy.deepcopy(fb_fixture)
    fb["experience"][0]["bullets"][0]["text"] = "Did some work."
    fb["experience"][0]["bullets"][0]["terms"] = ["langchain"]
    sc = budget.bullet_scores(fb, [Keyword("langchain", 2.0, True)])
    assert sc["a1"] > 0 and sc["a2"] == 0


def test_the_cv_needs_two_projects_so_two_are_always_sent(fb_fixture):
    kws = [Keyword("pytorch", 2.0, True)]                           # every match is in project p2 only
    ids, must = budget.select_bullets(fb_fixture, kws, top_n=1)
    owners = {p["id"] for p in fb_fixture["projects"] for b in p["bullets"] if b["id"] in ids}
    assert len(owners) >= 2
    v = budget.view(fb_fixture, set(ids))
    assert len(v["projects"]) >= 2 and len(v["experience"]) == len(fb_fixture["experience"])


def test_preferred_projects_only_break_ties_never_outrank_relevance(fb_fixture):
    kws = [Keyword("pytorch", 2.0, True)]                         # only p2 matches
    assert budget.rank_projects(fb_fixture, kws, preferred=["p4", "p3"])[0] == "p2"     # relevance beats the prior
    none = [Keyword("zzz-no-match", 1.0, False)]                  # all equal: the preferred ones lead, in bank order
    assert budget.rank_projects(fb_fixture, none, preferred=["p4", "p3"])[:2] == ["p3", "p4"]
    assert budget.rank_projects(fb_fixture, none)[:2] == ["p1", "p2"]                   # no prior: plain bank order
    ids, _ = budget.select_bullets(fb_fixture, kws, top_n=3, preferred=["p4"])
    assert ids.index("p2a") < ids.index("p4a")                     # the match leads; the prior only orders the rest


def test_the_prompt_lists_projects_most_relevant_first_and_calls_the_prior_a_tie_breaker(settings, fb_fixture, job,
                                                                                       spy_llm):
    llm = spy_llm()
    kws = [Keyword("pytorch", 3.0, True), Keyword("scikit-learn", 1.0, False)]      # p2 matches best, then p1
    tailor._proposal(job, fb_fixture, kws, llm, "", settings, "AI Engineer", ["p4", "ghost"])
    user = _sent(llm)[0]["user"]
    projects = user[user.index("PROJECTS:"):]
    assert projects.index("p2:") < projects.index("p1:")           # p2 matches pytorch, p1 does not
    assert "a tie-breaker only" in user and "p4" in user.split("tie-breaker only")[1].split("\n")[0]
    assert "ghost" not in user                                      # an id the model cannot see is not suggested


def test_when_still_over_budget_the_lowest_ranked_bullets_go_first_then_keywords(fb_fixture):
    big = _big_bank(fb_fixture, experiences=6, bullets=4, projects=4, words=120)
    kws = [Keyword(f"term{i}", 1.0, False) for i in range(60)] + [Keyword("langchain", 2.0, True)]
    build = lambda v, k: ("S", "\n".join(b["text"] for e in v["experience"] + v["projects"] for b in e["bullets"])
                          + " ".join(x.term for x in k))                          # noqa: E731
    _, ids_must = budget.select_bullets(big, kws)
    v, k, meta = budget.fit(big, kws, build, 3000, 3000 + 4000)
    assert meta["est_input_tokens"] + 3000 <= 7000 and not meta["over_budget"]
    sc = budget.bullet_scores(big, kws)
    kept = {b["id"] for sec in ("experience", "projects") for e in v[sec] for b in e["bullets"]}
    dropped = set(sc) - kept
    assert dropped and ids_must <= kept                                          # the must-haves survive the cut
    assert max(sc[b] for b in dropped) <= min(sc[b] for b in kept - ids_must)    # only the lowest-ranked were dropped


def test_if_even_the_essentials_do_not_fit_it_says_so_instead_of_failing(fb_fixture):
    build = lambda v, k: ("S", "x " * 40000)                                       # noqa: E731
    _, _, meta = budget.fit(fb_fixture, [Keyword("python", 2.0, True)], build, 3000, 7500)
    assert meta["over_budget"] is True


def test_call_limits_read_settings_with_safe_defaults(settings):
    assert budget.call_limits(settings, "tailor_cv") == (3000, 7500)
    assert budget.call_limits(settings, "no_such_task") == (1000, 7500)
    settings.raw["llm"].pop("max_tokens"); settings.raw["llm"].pop("max_call_tokens")
    assert budget.call_limits(settings, "tailor_cv") == (1000, 7500)


def test_top_bullets_setting_controls_how_many_are_sent(settings, fb_fixture, job, spy_llm):
    kws = _kws(job)
    sizes = {}
    for n in (3, 14, 999):
        settings.raw["llm"]["top_bullets"] = n
        _, meta = tailor._proposal(job, fb_fixture, kws, spy_llm(), "", settings, "AI Engineer", None)
        sizes[n] = meta["bullets_sent"]
    total = sum(len(e["bullets"]) for sec in ("experience", "projects") for e in fb_fixture[sec])
    assert sizes[3] < sizes[14] < sizes[999] == total                  # 3 = the must-haves (one per experience entry...)
    assert sizes[3] >= len(fb_fixture["experience"]) + 2               # ...plus what two projects need
