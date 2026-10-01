import copy
import shutil
from pathlib import Path

import pytest

from careerops.config import ROOT, Settings, load_settings
from careerops.db import DB

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings()
    raw = copy.deepcopy(s.raw)
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv"):
        raw["paths"][k] = str(tmp_path / Path(raw["paths"][k]).name)
    raw["paths"]["fact_bank"] = str(ROOT / "config/profile/fact_bank.yaml")
    raw["paths"]["applicant"] = str(ROOT / "config/profile/applicant.yaml")
    shutil.copy(FIX / "sponsors.csv", raw["paths"]["sponsors_csv"])
    return Settings(raw=raw, root=ROOT)


@pytest.fixture
def db(settings) -> DB:
    return DB(settings.path("db"))


@pytest.fixture
def jd() -> str:
    return (FIX / "jd_northwind.txt").read_text(encoding="utf-8")


@pytest.fixture
def job(jd) -> dict:
    return {"id": "manual:northwind1", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer",
            "location": "Bristol, UK", "url": "https://example.com/job", "apply_url": "https://example.com/job",
            "description": jd}
