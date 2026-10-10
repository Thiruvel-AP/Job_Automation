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
PROFILE_FIX = FIX / "profile"          # fictional fact_bank.yaml + applicant.yaml: what every test uses as "the profile"

# ---- guard: no test may open anything under the real data/ folder (careerops.db, outputs/, exports/ ...) ----------
# A 2026-10-03 `careerops run --mock` against the real data/ overwrote a real tailored job (see docs/status.md); tests
# must use tmp_path. An audit hook sees every open() / os.open() / sqlite3.connect(), including ones made in C.
REAL_DATA = os.path.realpath(ROOT / "data")
REAL_PROFILE = tuple(os.path.realpath(ROOT / "config" / "profile" / n) for n in ("fact_bank.yaml", "applicant.yaml"))
_guard = {"on": False, "hits": [], "profile_hits": [], "heavy": []}


def _under_real_data(target) -> bool:
    if isinstance(target, (bytes, str, os.PathLike)):
        p = os.path.realpath(os.fsdecode(target))
        return p == REAL_DATA or p.startswith(REAL_DATA + os.sep)
    return False                                           # an int file descriptor, ":memory:" objects, None


def _is_real_profile(target) -> bool:
    return isinstance(target, (bytes, str, os.PathLike)) and os.path.realpath(os.fsdecode(target)) in REAL_PROFILE


def _heavy(event, args) -> str | None:
    """What a subprocess is, if it is the kind that makes a test slow and memory-hungry: xelatex (PDF rendering) or the
    Playwright driver (Chromium)."""
    if event != "subprocess.Popen" or len(args) < 2 or not args[1]:
        return None
    exe = os.path.basename(os.fsdecode(args[1][0])).lower()
    if exe.startswith("xelatex"):
        return "xelatex"
    return "playwright" if "playwright" in " ".join(os.fsdecode(a) for a in args[1][:2]).lower() else None


def _audit(event, args):
    if _guard["on"] and event == "subprocess.Popen":
        kind = _heavy(event, args)
        if kind and kind not in _guard["heavy"]:
            _guard["heavy"].append(kind)
    if _guard["on"] and event in ("open", "sqlite3.connect") and args and _under_real_data(args[0]):
        _guard["hits"].append(f"{event}: {os.fsdecode(args[0])}")
        raise PermissionError(f"test touched the real data/ folder ({event} {os.fsdecode(args[0])}); use tmp_path")
    if _guard["on"] and event == "open" and args and _is_real_profile(args[0]):
        _guard["profile_hits"].append(f"{event}: {os.fsdecode(args[0])}")
        raise PermissionError(f"test read the real profile ({os.fsdecode(args[0])}); use tests/fixtures/profile/")


sys.addaudithook(_audit)           # audit hooks cannot be removed; the flag above scopes it to running tests


@pytest.fixture(autouse=True)
def slow_tests_are_marked(request):
    """A test that runs xelatex or drives Chromium must carry @pytest.mark.slow, so `pytest -m "not slow"` stays a light
    run (under ~2 GB RAM) however many PDFs the rest of the suite renders. Set CAREEROPS_SLOW_LOG=<file> to append the
    offenders' node ids to a file instead of failing (how the markers were first found)."""
    _guard["heavy"].clear()
    yield
    heavy, _guard["heavy"] = list(_guard["heavy"]), []
    if heavy and request.node.get_closest_marker("slow") is None:
        log = os.environ.get("CAREEROPS_SLOW_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(request.node.nodeid + "\n")
        else:
            pytest.fail(f"this test runs {' and '.join(heavy)}: mark it @pytest.mark.slow (or the module pytestmark)",
                        pytrace=False)


@pytest.fixture(autouse=True)
def no_provider_keys(monkeypatch):
    """No test may reach a real provider by accident (doctor / llm-check send a tiny request when a key is set): a test that
    needs a key sets its own fake one."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_real_data():
    """Fail any test that opens a path under the real data/ folder, even if the code under test swallows the error."""
    _guard["hits"].clear()
    _guard["profile_hits"].clear()
    _guard["on"] = True
    yield
    _guard["on"] = False
    hits, _guard["hits"] = list(_guard["hits"]), []
    profile_hits, _guard["profile_hits"] = list(_guard["profile_hits"]), []
    if hits:
        pytest.fail("test opened the real data/ folder:\n  " + "\n  ".join(hits), pytrace=False)
    if profile_hits:
        pytest.fail("test read the real fact bank / applicant (config/profile/); use tests/fixtures/profile/:\n  "
                    + "\n  ".join(profile_hits), pytrace=False)


@contextmanager
def real_data_hits():
    """For the guard's own test: yields the list of hits and lets the probing test pass."""
    try:
        yield _guard["hits"]
    finally:
        _guard["hits"].clear()                              # expected hits: the probing test is not a failure


@contextmanager
def real_profile_hits():
    """For the guard's own test: yields the list of real-profile reads and lets the probing test pass."""
    try:
        yield _guard["profile_hits"]
    finally:
        _guard["profile_hits"].clear()


# A letter sentence that is mostly one evidence bullet's own words is a paste and is rejected (letter_rules.paste_overlap).
# Test drafts that must pass say the same thing in other words; the keys are the starts of the fixture bullets.
_RESTATED = {
    "Built a tool-calling LLM agent": "At the university I built a tool-calling LLM agent for a cancer research platform, "
                                      "and it is the piece of work that I am proudest of.",
    "Built a churn classifier": "In a separate project that I enjoyed I built a churn classifier with scikit-learn and "
                                "XGBoost, and it is another example of how I work.",
}


def restate(bullet: str) -> str:
    """A first-person sentence for a fixture bullet that shares well under the paste limit of its words."""
    for start, sentence in _RESTATED.items():
        if bullet.startswith(start):
            return sentence
    raise KeyError(f"no restatement for {bullet[:40]!r}: add it to _RESTATED in tests/conftest.py")


def fixture(area: str, name: str) -> Path:
    return FIX / area / name


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings()
    raw = copy.deepcopy(s.raw)
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge"):   # register_dir: M1 writes snapshots
        raw["paths"][k] = str(tmp_path / Path(raw["paths"][k]).name)
    # The archive root is the user's real folder of submitted CVs (see CLAUDE.md): tests never get near it.
    raw["paths"]["applications"] = str(tmp_path / "Companies")
    raw["paths"]["tmp"] = str(tmp_path / "stage")
    raw["paths"]["calibration"] = str(tmp_path / "calibration")
    raw["paths"]["frozen_keywords"] = str(tmp_path / "frozen_keywords")      # never the tracked config/calibration/keywords
    (tmp_path / "Companies").mkdir()                      # only `careerops init` creates the real one
    # A fictional profile, never the user's real one (config/profile/): the real bank changes as the user adds skills
    # (it gained Kubernetes), and tests about gaps and guards must not move with it.
    raw["paths"]["fact_bank"] = str(PROFILE_FIX / "fact_bank.yaml")
    raw["paths"]["applicant"] = str(PROFILE_FIX / "applicant.yaml")
    # The shipped default is letter.mode: off (no letter is asked for). The suite's letter tests are about the drafting, so the
    # fixture turns it on; tests/tailoring/test_letter_mode.py checks the shipped default and the off path.
    raw["letter"]["mode"] = "draft"
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


# two keyword answers that differ: every term is verbatim in jd_northwind.txt
FIRST_KEYWORDS = {"required": ["LangChain", "scikit-learn", "GitHub Actions"], "preferred": ["CI/CD"]}
SECOND_KEYWORDS = {"required": ["Lambda", "IAM", "tool calling"], "preferred": ["S3"]}


class FlipLLM(MockLLM):
    """extract_keywords answers FIRST_KEYWORDS, then SECOND_KEYWORDS, then FIRST ... (one answer per call) and counts its
    calls in `extractions`, so a second extraction of the same JD shows as different keywords. `fail` makes the call raise."""

    def __init__(self, fail=False):
        super().__init__()
        self.extractions = 0
        self.fail = fail
        self.handlers["extract_keywords"] = self._extract

    def _extract(self, ctx):
        self.extractions += 1
        if self.fail:
            raise RuntimeError("provider is down")
        return {**(FIRST_KEYWORDS if self.extractions % 2 else SECOND_KEYWORDS),
                "company_hook": "Northwind builds oncology tools.", "team": "", "context_terms": []}


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
    """`--one-page`: the code selection for the broad JD overflows one page, so the shrink loop has work to do."""
    from careerops.tailoring.render import pdf_text
    rep = tailor_job(broad_job, fb_fixture, broad_llm, settings, settings.path("outputs"), one_page=True)
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
