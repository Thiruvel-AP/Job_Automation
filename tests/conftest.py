import copy
import shutil
from pathlib import Path

import pytest

from careerops.config import ROOT, Settings, load_settings
from careerops.config import load_yaml
from careerops.db import DB
from careerops.llm import MockLLM
from careerops.tailoring.tailor import tailor_job

FIX = Path(__file__).parent / "fixtures"


def fixture(area: str, name: str) -> Path:
    return FIX / area / name


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings()
    raw = copy.deepcopy(s.raw)
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv"):
        raw["paths"][k] = str(tmp_path / Path(raw["paths"][k]).name)
    # The archive root is the user's real folder of submitted CVs (see CLAUDE.md): tests never get near it.
    raw["paths"]["applications"] = str(tmp_path / "Companies")
    raw["paths"]["tmp"] = str(tmp_path / "stage")
    (tmp_path / "Companies").mkdir()                      # only `careerops init` creates the real one
    raw["paths"]["fact_bank"] = str(ROOT / "config/profile/fact_bank.yaml")
    raw["paths"]["applicant"] = str(ROOT / "config/profile/applicant.yaml")
    shutil.copy(fixture("discovery", "sponsors.csv"), raw["paths"]["sponsors_csv"])
    return Settings(raw=raw, root=ROOT)


class SpyLLM(MockLLM):
    """MockLLM that keeps every prompt and context it was sent, so tests can assert on what would leave the machine."""

    def __init__(self, handlers=None):
        super().__init__(handlers)
        self.sent: list[dict] = []

    def json(self, task, system, user, context=None):
        self.sent.append({"task": task, "system": system, "user": user, "context": context or {}})
        return super().json(task, system, user, context)


@pytest.fixture
def spy_llm():
    """The SpyLLM class: `llm = spy_llm({"cover_letter": handler})`, then read `llm.sent`."""
    return SpyLLM


@pytest.fixture
def fb_fixture() -> dict:
    """Fictional fact bank (tests/fixtures/tailoring/fact_bank_fixture.yaml). CV-2 tests use this, never the real one."""
    return load_yaml(fixture("tailoring", "fact_bank_fixture.yaml"))


@pytest.fixture
def db(settings) -> DB:
    return DB(settings.path("db"))


@pytest.fixture
def jd() -> str:
    return fixture("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")


@pytest.fixture
def job(jd) -> dict:
    return {"id": "manual:northwind1", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "https://example.com/job", "apply_url": "https://example.com/job",
            "description": jd}


def _fabricating_llm():
    """A model that tries to lie: invents a number, a deny-listed tool and an unevidenced keyword."""
    def tailor(ctx):
        out = MockLLM().json("tailor_cv", "", "", ctx)
        out["experience"]["az"] = ["az1", "az3", "az2"]          # make sure the lies are on the page
        out["rephrase"] = {
            "az3": "Deployed the agent with Docker and Kubernetes on AWS Lambda, cutting cost 40%.",
            "az1": "Built a tool-calling LLM agent (LangChain, AWS Bedrock) with agentic workflows for a "
                   "multi-omics cancer research platform with three tools.",
        }
        out["profile"] = "AI engineer with 5 years of production experience in Kubernetes."
        return out

    def keywords(ctx):
        return {"required": ["Python", "LLM agents", "made-up keyword not in JD"], "preferred": ["Kubernetes"],
                "company_hook": "Northwind builds clinical decision-support tools for oncology researchers."}

    return MockLLM({"tailor_cv": tailor, "extract_keywords": keywords})


@pytest.fixture
def tailored(settings, db, job, tmp_path):
    fb = load_yaml(settings.path("fact_bank"))
    db.upsert_job(job)
    llm = _fabricating_llm()
    rep = tailor_job(job, fb, llm, settings, settings.path("outputs"))
    f = rep["final"]
    db.update_job(job["id"], status="needs_review", keywords=rep["keywords"], ceiling=rep["ceiling"],
                  score=f["score"], score_stemmed=f["score_stemmed"], letter_score=rep["letter"]["score"],
                  attempts=len(rep["attempts"]), out_dir=rep["out_dir"])
    return rep, fb, llm
