import copy
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from careerops.config import ROOT, SETTINGS_ENV, Settings, load_settings
from careerops.config import load_yaml
from careerops.db import DB
from careerops.llm import MockLLM
from careerops.tailoring.tailor import tailor_job

FIX = Path(__file__).parent / "fixtures"
PROFILE_FIX = FIX / "profile"          # fictional fact_bank.yaml + applicant.yaml: what every test uses as "the profile"
SETTINGS_FIX = FIX / "config" / "settings.yaml"     # a bare load_settings() reads this, never the real config/settings.yaml
os.environ[SETTINGS_ENV] = str(SETTINGS_FIX)
# apply/stale.py: no test asks git when the tailoring code last changed (fixture PDFs would look stale against today's commits);
# the stale-output tests set CAREEROPS_CODE_TIME themselves.
os.environ["CAREEROPS_CODE_TIME"] = "off"

# ---- guard: no test may open anything under the real data/ folder (careerops.db, outputs/, exports/ ...) ----------
# A 2026-10-03 `careerops run --mock` against the real data/ overwrote a real tailored job (see docs/status.md); tests
# must use tmp_path. An audit hook sees every open() / os.open() / sqlite3.connect(), including ones made in C.
REAL_DATA = os.path.realpath(ROOT / "data")
REAL_PROFILE = tuple(os.path.realpath(ROOT / "config" / "profile" / n)
                     for n in ("fact_bank.yaml", "applicant.yaml", "answers.yaml", "answer.yaml"))
# The real config/settings.yaml is the user's to change (apply.mode: auto, their paths): no test may read it. A test that wants
# settings loads the test copy (a bare load_settings() already does), or writes its own file in tmp_path.
REAL_SETTINGS = os.path.realpath(ROOT / "config" / "settings.yaml")
_guard = {"on": False, "hits": [], "profile_hits": [], "settings_hits": [], "heavy": []}


def _under_real_data(target) -> bool:
    if isinstance(target, (bytes, str, os.PathLike)):
        p = os.path.realpath(os.fsdecode(target))
        return p == REAL_DATA or p.startswith(REAL_DATA + os.sep)
    return False                                           # an int file descriptor, ":memory:" objects, None


def _is_real_profile(target) -> bool:
    return isinstance(target, (bytes, str, os.PathLike)) and os.path.realpath(os.fsdecode(target)) in REAL_PROFILE


def _is_real_settings(target) -> bool:
    return isinstance(target, (bytes, str, os.PathLike)) and os.path.realpath(os.fsdecode(target)) == REAL_SETTINGS


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
    if _guard["on"] and event == "open" and args and _is_real_settings(args[0]):
        _guard["settings_hits"].append(f"{event}: {os.fsdecode(args[0])}")
        raise PermissionError(f"test read the real settings ({os.fsdecode(args[0])}); use tests/fixtures/config/settings.yaml or a tmp copy")
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
def liveness_without_network(monkeypatch, tmp_path_factory):
    """The gate in front of tailoring (discovery/pretailor.py) checks that a posting is still open. No test may reach a real site for that: the default
    fetcher answers every address with an open job page, and the 6 hour cache is a throwaway file. A test of the check itself passes its own fetcher."""
    from types import SimpleNamespace

    from careerops.discovery import liveness, pretailor
    from careerops.discovery.http import Fetcher
    from careerops.discovery.robots import RobotsCache

    page = "<html><body><h1>Job</h1><p>" + "A job description. " * 20 + "</p><a href='/apply'>Apply now</a></body></html>"

    def get(url, **kw):
        if url.endswith("/robots.txt"):
            return SimpleNamespace(status_code=404, text="", ok=False, headers={}, content=b"", url=url)
        return SimpleNamespace(status_code=200, text=page, ok=True, headers={}, content=page.encode(), url=url)
    monkeypatch.setattr(pretailor, "default_fetcher", lambda s: Fetcher(RobotsCache(), get=get, sleep=lambda x: None, clock=lambda: 0.0, gap_s=0.0))
    scratch = tmp_path_factory.mktemp("liveness") / "liveness.json"
    monkeypatch.setattr(liveness, "cache_for", lambda s: liveness.Cache(scratch))


@pytest.fixture(autouse=True)
def the_document_check_is_not_every_tests_subject(monkeypatch):
    """The document check (tailoring/doccheck.py) reads a finished job's files and holds a CV to the Document rules; a test about something else (a
    review prompt, a live session, an auto-submit gate) uses the small fixture banks, which are not written to pass it. Its own tests call
    `doccheck.check` with a report they build, or put their own `for_report` in place."""
    from careerops.tailoring import doccheck

    monkeypatch.setattr(doccheck, "for_report", lambda *a, **k: doccheck.DocCheck())


@pytest.fixture(autouse=True)
def no_real_data():
    """Fail any test that opens a path under the real data/ folder, even if the code under test swallows the error."""
    _guard["hits"].clear()
    _guard["profile_hits"].clear()
    _guard["settings_hits"].clear()
    _guard["on"] = True
    yield
    _guard["on"] = False
    hits, _guard["hits"] = list(_guard["hits"]), []
    profile_hits, _guard["profile_hits"] = list(_guard["profile_hits"]), []
    settings_hits, _guard["settings_hits"] = list(_guard["settings_hits"]), []
    if hits:
        pytest.fail("test opened the real data/ folder:\n  " + "\n  ".join(hits), pytrace=False)
    if profile_hits:
        pytest.fail("test read the real fact bank / applicant (config/profile/); use tests/fixtures/profile/:\n  "
                    + "\n  ".join(profile_hits), pytrace=False)
    if settings_hits:
        pytest.fail("test read the real config/settings.yaml; use tests/fixtures/config/settings.yaml or a tmp copy:\n  "
                    + "\n  ".join(settings_hits), pytrace=False)


@contextmanager
def real_data_hits():
    """For the guard's own test: yields the list of hits and lets the probing test pass."""
    try:
        yield _guard["hits"]
    finally:
        _guard["hits"].clear()                              # expected hits: the probing test is not a failure


@contextmanager
def real_settings_hits():
    """For the guard's own test: yields the list of real-settings reads and lets the probing test pass."""
    try:
        yield _guard["settings_hits"]
    finally:
        _guard["settings_hits"].clear()


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


def fixture_profile_settings() -> Settings:
    """The shipped settings with only the two profile paths on the fictional profile, for module-scoped fixtures and helpers that
    cannot take the `settings` fixture. A bare load_settings() hands tailor_job the user's real applicant.yaml (the letter's
    relocation line is read from it)."""
    s = load_settings()
    s.raw["paths"]["fact_bank"] = str(PROFILE_FIX / "fact_bank.yaml")
    s.raw["paths"]["applicant"] = str(PROFILE_FIX / "applicant.yaml")
    return s


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings()
    raw = copy.deepcopy(s.raw)
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge", "robots", "secrets", "discover", "aiapply_applied", "outreach", "answers_library", "jds_done", "letter_feedback"):   # register_dir: M1 writes snapshots
        raw["paths"][k] = str(tmp_path / Path(raw["paths"][k]).name)
    # The archive root is the user's real folder of submitted CVs (see CLAUDE.md): tests never get near it.
    raw["paths"]["applications"] = str(tmp_path / "Companies")          # read-only: history, company list, duplicate check
    raw["paths"]["applications_auto"] = str(tmp_path / "Auto")          # the only archive folder the pipeline writes to
    raw["paths"]["tmp"] = str(tmp_path / "stage")
    # No source registry unless a test brings one (tests/discovery/test_registry.py copies the shipped config/sources.yaml): the
    # shipped one names OpenAI's Ashby board and 20 alert senders, which no other test wants to read or to fake.
    raw["paths"]["sources"] = str(tmp_path / "sources.yaml")
    raw["paths"]["calibration"] = str(tmp_path / "calibration")
    raw["paths"]["facts_proposed"] = str(tmp_path / "facts" / "proposed.yaml")      # the facts a test user types in review: never data/facts/
    raw["paths"]["state"] = str(tmp_path / "state")                                  # targeting.json (the last region / size): never data/state/
    raw["paths"]["search"] = str(tmp_path / "search")                                # v3.1 agent search plans, CSVs and yield: never data/search/
    raw["paths"]["evals"] = str(tmp_path / "evals")                                  # v3.2 the agent's evaluations and decisions: never data/evals/
    raw["paths"]["company_size"] = str(tmp_path / "company_size.jsonl")              # the 90-day size cache and the user's overrides: never data/
    raw["paths"]["company_size_overrides"] = str(tmp_path / "company_size_overrides.yaml")
    raw["paths"]["frozen_keywords"] = str(tmp_path / "frozen_keywords")      # never the tracked config/calibration/keywords
    (tmp_path / "Companies").mkdir()                      # the user's own folder: init never creates it
    (tmp_path / "Auto").mkdir()                           # only `careerops init` creates the real one
    # A fictional profile, never the user's real one (config/profile/): the real bank changes as the user adds skills
    # (it gained Kubernetes), and tests about gaps and guards must not move with it.
    raw["paths"]["fact_bank"] = str(PROFILE_FIX / "fact_bank.yaml")
    raw["paths"]["applicant"] = str(PROFILE_FIX / "applicant.yaml")
    raw["paths"]["answers"] = str(PROFILE_FIX / "answers.yaml")      # read only: a test that saves an answer copies it to tmp_path first
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


def with_results(fb: dict) -> dict:
    """A copy of the bank with a `result:` the text does not say on every bullet. Since the round of 17 Oct a rephrase call is made only
    for a bullet like that (a bullet is no longer asked to introduce a keyword from its `terms:`), so the tests that need the model to
    be asked something about a bullet use this."""
    fb = copy.deepcopy(fb)
    for sec in ("experience", "projects"):
        for e in fb.get(sec, []):
            for b in e["bullets"]:
                b.setdefault("result", "kept in daily use")
    return fb


def _fabricating_llm():
    """A model that tries to lie: rewordings with an invented number and a deny-listed tool, and a keyword that is not in the JD."""
    def rephrase(ctx):
        out = {}
        for i, b in enumerate(ctx["bullets"]):
            if i == 0:                                   # the lie: a deny-listed tool and a number the bullet lacks
                out[b["id"]] = "Deployed the agent with Docker and Kubernetes on AWS Lambda, cutting cost 40%."
            else:                                        # an honest rewording: the same fact plus the result the user wrote for it
                out[b["id"]] = b["text"].rstrip(".") + ", " + b.get("result", "kept in daily use") + "."
        return out

    def keywords(ctx):
        return {"required": ["Python", "LLM agents", "made-up keyword not in JD", "Kubernetes"], "preferred": [],
                "company_hook": "Northwind builds clinical decision-support tools for oncology researchers."}

    return MockLLM({"rephrase_bullets": rephrase, "extract_keywords": keywords})


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
def one_page_run(settings, fb_fixture, broad_job, broad_llm, monkeypatch):
    """`--one-page`: the code selection for the broad JD overflows one page, so the shrink loop has work to do. The layout is
    roomier than production's ONE_PAGE (at least 4 / 2 bullets per entry, topped up whether or not they add keywords): the
    selection also ranks by the JD's domain and topics now, and picks bullets short enough to fit one page without a shrink."""
    from careerops.tailoring import tailor as tailor_module
    from careerops.tailoring.render import pdf_text
    from careerops.tailoring.selection import Layout
    monkeypatch.setattr(tailor_module, "ONE_PAGE", Layout((4, 5), (2, 3), 2, None))
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
