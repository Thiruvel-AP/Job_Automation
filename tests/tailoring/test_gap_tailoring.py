"""Tailoring for the gap report: `boost` ranks the claimable keywords first in the first attempt (review's `r`), and a typed provisional fact is
the only way a gap keyword reaches the CV (review's `f`): the bank the job is tailored with carries it, any other job's does not."""
import pytest

from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import provisional as prov
from careerops.tailoring import tailor as tailor_module
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import tailor_job

pytestmark = pytest.mark.slow          # these tests render PDFs with xelatex

FACT = "Scheduled a nightly Dagster pipeline that retrained a churn model, which cut manual reruns to zero."


def llm():
    return MockLLM({"extract_keywords": lambda ctx: {"required": ["Python", "SQL", "Dagster"], "preferred": ["LangChain"], "company_hook": "", "team": ""}})


@pytest.fixture
def gap_job(job):
    return {**job, "description": job["description"] + "\n\nRequirements\nDagster orchestration experience\nSQL\n"}


def cv_text(rep) -> str:
    return pdf_text(__import__("pathlib").Path(rep["cv_pdf"]))


def test_a_gap_keyword_is_never_on_the_cv_without_a_typed_fact(settings, gap_job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    rep = tailor_job(gap_job, fb, llm(), settings, tmp_path / "out", boost=["SQL"])
    assert "dagster" not in cv_text(rep).lower()                                      # nothing evidences it: it is not claimed, not even when boosting
    assert "Dagster" not in rep["ats"]["hits"] and "Dagster" not in rep["claimable"]
    assert rep["provisional"] == []


def test_a_typed_fact_puts_its_keyword_on_the_cv_for_its_own_job_only(settings, gap_job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    store = prov.Store(tmp_path / "proposed.yaml")
    store.add("Dagster", FACT, "az", gap_job["id"])
    mine, _ = prov.augment(fb, store.for_job(gap_job["id"]))
    rep = tailor_job(gap_job, mine, llm(), settings, tmp_path / "mine", boost=["SQL"])
    assert FACT in cv_text(rep).replace("\n", " ") or "Dagster" in cv_text(rep)
    assert "Dagster" in rep["ats"]["hits"]
    assert [(p["id"], p["keyword"], p["on_cv"]) for p in rep["provisional"]] == [("prov1", "Dagster", True)]
    # the same JD as another job: tailored with that job's bank, the fact is not there
    other, _ = prov.augment(fb, store.for_job("manual:other"))
    rep2 = tailor_job({**gap_job, "id": "manual:other"}, other, llm(), settings, tmp_path / "other")
    assert "dagster" not in cv_text(rep2).lower() and rep2["provisional"] == []


def test_boost_counts_the_claimable_keywords_double_or_more_in_the_first_ranking(settings, gap_job, tmp_path, monkeypatch):
    seen = []
    real = tailor_module.select_cv

    def spy(fb, kws, *a, **k):
        seen.append({kw.term: kw.weight for kw in kws})
        return real(fb, kws, *a, **k)

    monkeypatch.setattr(tailor_module, "select_cv", spy)
    fb = load_yaml(settings.path("fact_bank"))
    tailor_job(gap_job, fb, llm(), settings, tmp_path / "plain")
    plain = seen[0]
    seen.clear()
    tailor_job(gap_job, fb, llm(), settings, tmp_path / "boosted", boost=["SQL"])
    boosted = seen[0]
    sql = next(t for t in plain if t.lower() == "sql")                                  # the JD's own spelling, whatever the taxonomy calls it
    assert boosted[sql] == plain[sql] * tailor_module.FORCE_FACTOR
    assert {t: w for t, w in boosted.items() if t != sql} == {t: w for t, w in plain.items() if t != sql}      # only the named keyword moves


def test_the_report_keeps_the_scoring_keywords_unboosted(settings, gap_job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    plain = tailor_job(gap_job, fb, llm(), settings, tmp_path / "plain")
    boosted = tailor_job(gap_job, fb, llm(), settings, tmp_path / "boosted", boost=["SQL"])
    assert plain["keywords"] == boosted["keywords"]                                    # the ranking is boosted; the score is always the JD's own weights


def test_a_typed_fact_is_on_the_cv_even_in_a_project_the_selection_would_not_pick(settings, gap_job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    plain = tailor_job(gap_job, fb, llm(), settings, tmp_path / "plain", one_page=True)        # one page: only two projects fit
    shown = {x["id"] for x in plain["context"]["projects"]}
    unused = next(p["id"] for p in reversed(fb["projects"]) if p["id"] not in shown and not p.get("award"))     # an award project goes to Achievements
    store = prov.Store(tmp_path / "proposed.yaml")
    store.add("Dagster", FACT, unused, gap_job["id"])
    mine, _ = prov.augment(fb, store.for_job(gap_job["id"]))
    rep = tailor_job(gap_job, mine, llm(), settings, tmp_path / "mine", one_page=True)
    assert unused in [p["id"] for p in rep["context"]["projects"]] and rep["provisional"][0]["on_cv"] is True
    assert "Dagster" in cv_text(rep) and rep["cv_pages"] == 1      # forced in, and the CV still fits one page: other bullets made room


def test_force_provisional_leaves_a_bank_without_typed_facts_alone(settings, gap_job):
    fb = load_yaml(settings.path("fact_bank"))
    sel = {"experience": {"az": ["az1"]}, "projects": ["churn"], "project_bullets": {"churn": ["churn1"]}}
    assert tailor_module.force_provisional(fb, sel) is sel


def test_shrink_never_drops_a_protected_bullet():
    from careerops.tailoring.scorer import Keyword
    kws = [Keyword("Python", 2, True)]
    ctx = {"experience": [{"id": "a", "bullet_ids": ["a1", "a2", "prov1"], "bullets": ["x", "y", "z"]}],
           "projects": []}
    from careerops.tailoring.selection import Layout
    layout = Layout((1, 3), (1, 3), 0, None)
    while tailor_module._shrink(ctx, kws, layout, {"prov1"}):
        pass
    assert ctx["experience"][0]["bullet_ids"] == ["prov1"]

