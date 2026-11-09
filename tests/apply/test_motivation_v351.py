"""9 Nov 2026 dry run: RightShip (a maritime safety and risk company) was told "I am drawn to applied AI in domains where correctness matters, such
as healthcare and biomedical research" and "Longer term I want to grow towards research-oriented work". The motivation answer now uses a
`motivations:` line only when it matches the company's domain (job ad and company page), never a research-ambition line for an industry role,
names the employer in every sentence about past work, and takes at most one sentence from a CV bullet, in plain words. MockLLM only."""
import pytest
from test_freetext import BOOK, REP, llm_with, make  # noqa: F401  (make is a fixture)
from test_motivation_v34 import model

from careerops.apply import answers as answers_mod
from careerops.apply import dryrun
from careerops.apply import freetext as ft
from careerops.tailoring import companypage

Q = "What motivated you to apply for this position?"
MOTIVATIONS = [
    "I want to build AI systems that are trustworthy in production, not just demos: tested, measured and safe when their dependencies fail.",
    "I enjoy taking a loosely defined problem from prototype to something a team uses in production.",
    "I am drawn to applied AI in domains where correctness matters, such as healthcare and biomedical research.",
    "Longer term I want to grow towards research-oriented work in data and AI.",
]
BOOK4 = answers_mod.AnswerBook(BOOK.entries, MOTIVATIONS, BOOK.style)
SEA_JD = (
    "The Company. Seaworthy is the maritime industry's leading safety and risk intelligence platform. We exist to advance a Zero Harm maritime "
    "industry by enabling better safety decisions across the ecosystem, for safer people and safer vessels. "
    "The Role. As an AI Engineer you will build LLM-based applications and agents over maritime data such as inspection reports, vessel records "
    "and documents, with retrieval, structured outputs, tool use and evaluation. You will work in Python with LangChain and LangGraph, ship to "
    "production with code review and version control, and design guardrails for agents that answer questions about vessel risk. "
    "What we offer. A Healthy Living Allowance, private medical insurance, a wellbeing day and an annual incentive. Research tells us that "
    "women and under-represented groups apply only when they meet every requirement: please apply anyway. "
) * 2
SEA_JOB = {"id": "manual:sea1", "company": "Seaworthy", "title": "AI Engineer", "description": SEA_JD}
SEA_LETTER = {"status": "ok", "evidence": [{"id": "az1"}],
              "parts": {"P1": "Seaworthy is building agents over vessel records; at Northwind Research Lab I built an LLM agent for researchers.",
                        "P5": "Seaworthy's focus on safer vessels is why I applied."}}


def sea(make, llm=None, rep=None, book=BOOK4, title="AI Engineer"):
    return make(llm or llm_with("x"), job={**SEA_JOB, "title": title}, rep=rep if rep is not None else {**REP, "letter": SEA_LETTER}, book=book)


# ------------------------------------------------------------------ the motivations: only lines that fit the company
def test_a_line_that_names_another_industry_or_research_ambition_is_left_out_for_a_maritime_company(make):
    f = sea(make)
    assert f.motivation_lines() == MOTIVATIONS[:2]
    dropped = " | ".join(f.motivations_dropped)
    assert "names health" in dropped and "research ambition" in dropped


def test_job_benefits_and_a_diversity_note_are_not_the_companys_domain():
    assert ft.industries(SEA_JD) == {"maritime"}
    assert "health" in ft.industries("We build clinical tools for oncology researchers.")


def test_a_health_company_keeps_the_health_line(make):
    f = make(llm_with("x"), book=BOOK4)                                 # Northwind Health AI: clinical decision support for oncology
    assert MOTIVATIONS[2] in f.motivation_lines() and MOTIVATIONS[3] not in f.motivation_lines()


def test_the_company_page_counts_too(make, tmp_path):
    (tmp_path / companypage.FILE).write_text("https://seaworthy.example/about\n2026-11-09\n\nWe also run clinical trials logistics for hospitals.",
                                             encoding="utf-8")
    f = sea(make, rep={**REP, "letter": SEA_LETTER, "out_dir": str(tmp_path)})
    assert MOTIVATIONS[2] in f.motivation_lines()


def test_a_research_role_keeps_the_research_line(make):
    assert MOTIVATIONS[3] in sea(make, title="Research Scientist, Machine Learning").motivation_lines()
    assert ft.research_role("AI Engineer", "University of Bristol") and not ft.research_role("AI Engineer", "Seaworthy")


def test_the_maritime_answer_names_no_other_industry_and_no_research_ambition(make):
    f = sea(make, model("x"))
    p = f.propose(Q)
    assert p.origin == "draft" and p.text, p.problems
    assert "healthcare" not in p.text and "biomedical" not in p.text and "Longer term" not in p.text and "research-oriented" not in p.text
    assert "At Northwind Research Lab, I" in p.text                                         # the one bullet names its employer
    assert f.motivation_log[-1]["motivations_left_out"]


# ------------------------------------------------------------------ the checks on any motivation answer
def problems(make, text):
    return sea(make)._motivation_problems(text)


def test_a_sentence_about_past_work_names_its_employer(make):
    assert any("names no employer" in p for p in problems(make, "I built an LLM agent that researchers trusted."))
    assert problems(make, "At Northwind Research Lab I built an LLM agent that researchers trusted.") == []
    assert problems(make, "In my Customer churn prediction project, I explained the three strongest churn drivers.") == []
    assert problems(make, "I want to build AI systems that are safe when their dependencies fail.") == []     # not about past work


def test_no_other_industry_outside_a_sentence_about_past_work(make):
    assert any("names an industry the company is not in (health)" in p
               for p in problems(make, "I am drawn to applied AI where correctness matters, such as healthcare."))
    assert problems(make, "At Northwind Research Lab I built an LLM agent for a cancer research platform.") == []


def test_at_most_one_sentence_from_a_bullet_and_in_plain_words(make):
    two = ("At Northwind Research Lab, I wrote a three-tier test framework of ~65 tests across 8 files. "
           "At Northwind Research Lab, I deployed the agent as a Docker container on AWS Lambda with ECR image pipelines.")
    probs = problems(make, two)
    assert any("2 sentences are taken from CV bullets (at most 1)" in p for p in probs), probs
    jargon = ("At Northwind Research Lab, I designed a deterministic guard architecture with live API resolution and a local 19,213-gene "
              "parquet fallback behind circuit breakers.")
    assert any("not in plain words" in p for p in problems(make, jargon))


def test_a_fluent_rewrite_that_names_another_industry_falls_back(make, settings):
    settings.raw["letter"]["fluency"] = True
    bad = ("Seaworthy's work on safer vessels is the work I want to do next. I want AI systems that are trustworthy in production, not just "
           "demos, and safe when their dependencies fail. At Northwind Research Lab I wrote ~65 automated tests across 8 files, so the team "
           "could trust the agent. I am drawn to applied AI in domains where correctness matters, such as healthcare and biomedical research.")
    f = sea(make, model(bad))
    p = f.propose(Q)
    assert "healthcare" not in p.text
    info = f.motivation_log[-1].get("fluency") or f.fluency_info
    assert info["source"] == "fallback" and any("names an industry" in x for x in info["problems"][0]), info


def test_the_dry_run_record_lists_the_motivations_left_out(make, tmp_path):
    f = sea(make, model("x"))
    f.propose(Q)

    class Page:
        def screenshot(self, **k):
            raise RuntimeError("no page")
    dryrun.record(tmp_path / "rec", Page(), SEA_JOB, {"out_dir": str(tmp_path / "o" / "job")}, [], cv=None, letter=None, letter_text=None,
                  freetext=f, out=lambda _: None)
    text = (tmp_path / "rec" / "motivation.txt").read_text(encoding="utf-8")
    assert "MOTIVATIONS LEFT OUT:" in text and "research ambition" in text
