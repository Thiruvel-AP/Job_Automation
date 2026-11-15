"""In auto mode a knock-out always goes to the human (autoapply.fill_form): nothing is uploaded, drafted or submitted. Chromium, a fixture form."""
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright
from careerops.apply.browser import CHROMIUM_ARGS

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import filler
from careerops.apply.visa import plan_for
from careerops.discovery import workcountry
from careerops.discovery.workcountry import Rules

pytestmark = pytest.mark.slow

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
PLAN = plan_for(APPLICANT, workcountry._mk(Rules(home="GB", label="Remote"), "GB", "onsite", "test"))
JOB = {"id": "greenhouse:graphcore:1", "company": "Graphcore", "title": "ML Engineer", "source": "greenhouse"}


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        yield browser
        browser.close()


def test_the_auto_apply_stops_at_a_knock_out_before_filling_anything(chromium, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4")
    page = chromium.new_page()
    page.goto((FIX / "knock_form.html").resolve().as_uri())
    out: list[str] = []
    ctx = filler.Ctx(JOB, APPLICANT, answers_mod.AnswerBook(), Path("answers.yaml"), PLAN, str(cv), None, None, ask=lambda p: pytest.fail("asked: " + p),
                     out=out.append, history=lambda c: False, result=type("R", (), {"filled": [], "blank": []})())
    with pytest.raises(filler.NeedsUser, match="KNOCK-OUT.*sponsorship"):
        autoapply.fill_form(page, ctx)
    assert any(line.strip().startswith("KNOCK-OUT:") for line in out)
    assert page.evaluate("() => [first_name.value, email.value, (resume.files[0] || {}).name || '']") == ["", "", ""]
    assert page.evaluate("() => window.__submits") == 0
    page.close()
