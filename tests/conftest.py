import copy
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from careerops.config import ROOT, Settings, load_settings
from careerops.config import load_yaml
from careerops.db import DB
from careerops.llm import MockLLM
from careerops.tailoring.tailor import tailor_job

FIX = Path(__file__).parent / "fixtures"

# ---- guard: no test may open anything under the real data/ folder (careerops.db, outputs/, exports/ ...) ----------
# A 2026-10-03 `careerops run --mock` against the real data/ overwrote a real tailored job (see docs/status.md); tests
# must use tmp_path. An audit hook sees every open() / os.open() / sqlite3.connect(), including ones made in C.
REAL_DATA = os.path.realpath(ROOT / "data")
_guard = {"on": False, "hits": []}


def _under_real_data(target) -> bool:
    if isinstance(target, (bytes, str, os.PathLike)):
        p = os.path.realpath(os.fsdecode(target))
        return p == REAL_DATA or p.startswith(REAL_DATA + os.sep)
    return False                                           # an int file descriptor, ":memory:" objects, None


def _audit(event, args):
    if _guard["on"] and event in ("open", "sqlite3.connect") and args and _under_real_data(args[0]):
        _guard["hits"].append(f"{event}: {os.fsdecode(args[0])}")
        raise PermissionError(f"test touched the real data/ folder ({event} {os.fsdecode(args[0])}); use tmp_path")


sys.addaudithook(_audit)           # audit hooks cannot be removed; the flag above scopes it to running tests


@pytest.fixture(autouse=True)
def no_real_data():
    """Fail any test that opens a path under the real data/ folder, even if the code under test swallows the error."""
    _guard["hits"].clear()
    _guard["on"] = True
    yield
    _guard["on"] = False
    hits, _guard["hits"] = list(_guard["hits"]), []
    if hits:
        pytest.fail("test opened the real data/ folder:\n  " + "\n  ".join(hits), pytrace=False)


@contextmanager
def real_data_hits():
    """For the guard's own test: yields the list of hits and lets the probing test pass."""
    try:
        yield _guard["hits"]
    finally:
        _guard["hits"].clear()                              # expected hits: the probing test is not a failure


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
    raw["paths"]["calibration"] = str(tmp_path / "calibration")
    raw["paths"]["frozen_keywords"] = str(tmp_path / "frozen_keywords")      # never the tracked config/calibration/keywords
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
    """A model that tries to lie: rewordings with an invented number and a deny-listed tool, a profile with an invented
    years-of-experience claim, and a keyword that is not in the JD."""
    def rephrase(ctx):
        out = {}
        for i, b in enumerate(ctx["bullets"]):
            if i == 0:                                   # the lie: a deny-listed tool and a number the bullet lacks
                out[b["id"]] = "Deployed the agent with Docker and Kubernetes on AWS Lambda, cutting cost 40%."
            else:                                        # an honest rewording: the same fact plus the keywords it may take
                out[b["id"]] = b["text"].rstrip(".") + ", covering " + ", ".join(b["introduce"]) + "."
        return out

    def profile(ctx):
        return {"facts": ["F1", "F2"], "profile": "AI engineer with 5 years of production experience in Kubernetes."}

    def keywords(ctx):
        return {"required": ["Python", "LLM agents", "made-up keyword not in JD", "Kubernetes"], "preferred": [],
                "company_hook": "Northwind builds clinical decision-support tools for oncology researchers."}

    return MockLLM({"rephrase_bullets": rephrase, "tailor_profile": profile, "extract_keywords": keywords})


BROAD_JD_TERMS = ["Python", "SQL", "pandas", "scikit-learn", "XGBoost", "PyTorch", "LangChain", "Docker", "MLflow",
                  "pytest", "FastAPI", "AWS", "IAM", "RAG", "MCP", "statistics", "reporting", "communication",
                  "stakeholders", "documentation", "reproducibility", "evaluation", "forecasting", "SHAP", "R"]


@pytest.fixture
def broad_job(job) -> dict:
    """A job whose JD names 25 skills that between them overlap nearly every bullet of the fixture fact bank."""
    return {**job, "description": "Requirements\n" + "\n".join(BROAD_JD_TERMS) + "\n"}


@pytest.fixture
def broad_llm(spy_llm):
    """A SpyLLM whose keyword answer is exactly those 25 skills (all verbatim in `broad_job`'s JD)."""
    return spy_llm({"extract_keywords": lambda ctx: {"required": BROAD_JD_TERMS, "preferred": [], "company_hook": "",
                                                    "team": ""}})


@pytest.fixture
def one_page_run(settings, fb_fixture, broad_job, broad_llm):
    """The code selection for the broad JD overflows one page, so the shrink loop has work to do."""
    from careerops.tailoring.render import pdf_text
    rep = tailor_job(broad_job, fb_fixture, broad_llm, settings, settings.path("outputs"))
    return rep, pdf_text(Path(rep["cv_pdf"]))


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
