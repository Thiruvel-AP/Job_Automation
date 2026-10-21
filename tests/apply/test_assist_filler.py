"""The assist (a human submits) and the auto-apply fill a form with ONE filler (apply/filler.py): applicant.yaml for the standard fields
(full name, first name, last name, e-mail, phone, LinkedIn, GitHub, portfolio, location), then answers.yaml. Free-text questions are left
to the person. A fixture Ashby form in headless Chromium; Chromium missing = the tests fail."""
import ast
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE_FIX
from playwright.sync_api import sync_playwright

from careerops import autoapply
from careerops.apply import answers as answers_mod
from careerops.apply import assist as assist_mod
from careerops.apply import filler

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"
APPLICANT = yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))
BOOK = answers_mod.load(PROFILE_FIX / "answers.yaml")
JOB = {"id": "ashby:limbic:177a9a1afcc1", "company": "Limbic", "title": "Senior AI ML Engineer", "source": "ashby"}
VALUES_JS = "() => Object.fromEntries([...document.querySelectorAll('input,textarea')].map(e => [e.name, e.type === 'file' ? (e.files[0]||{}).name || '' : e.value]))"

pytestmark = pytest.mark.slow          # these tests drive Chromium


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(chromium, tmp_path):
    pg = chromium.new_page()
    pg.goto((FIX / "ashby_form.html").resolve().as_uri())
    yield pg
    pg.close()


@pytest.fixture
def cv(tmp_path):
    p = tmp_path / "Ada Lovelace CV.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(p)


def test_the_assist_fills_name_first_name_and_last_name_on_an_ashby_form(page, cv):
    report = assist_mod.fill_page(page, APPLICANT, cv, None, job=None, book=BOOK)
    v = page.evaluate(VALUES_JS)
    assert v["_systemfield_name"] == "Ada Lovelace"
    assert (v["first_name"], v["last_name"]) == ("Ada", "Lovelace")
    assert v["_systemfield_email"] == APPLICANT["email"] and v["phone"] == APPLICANT["phone"]
    assert v["linkedin"] == APPLICANT["linkedin"] and v["github"] == APPLICANT["github"]
    assert v["_systemfield_resume"] == Path(cv).name
    assert v["company_name"] == ""                                     # "Company name" is not the applicant's name
    assert not any("Name" in x for x in report["required_empty"])


def test_the_assist_answers_from_answers_yaml_and_leaves_a_free_text_question_to_the_person(page, cv):
    report = assist_mod.fill_page(page, APPLICANT, cv, None, job=None, book=BOOK)
    v = page.evaluate(VALUES_JS)
    assert v["notice"] == "Available immediately"
    assert v["llm"] == ""
    assert any("Describe an LLM feature" in x for x in report["required_empty"])


def test_without_an_answers_file_only_the_standard_fields_are_filled(page, cv):
    assist_mod.fill_page(page, APPLICANT, cv, None, job=None)
    v = page.evaluate(VALUES_JS)
    assert v["_systemfield_name"] == "Ada Lovelace" and v["notice"] == ""


def test_the_auto_apply_fills_the_same_form_the_same_way(page, cv):
    ctx = filler.Ctx(JOB, APPLICANT, BOOK, Path("answers.yaml"), None, cv, None, None, ask=lambda p: "", out=lambda s: None,
                     history=lambda c: False, result=type("R", (), {"filled": [], "blank": []})())
    with pytest.raises(filler.NeedsUser):                            # at the free-text question, as it always did
        autoapply.fill_form(page, ctx)
    v = page.evaluate(VALUES_JS)
    assert v["_systemfield_name"] == "Ada Lovelace" and (v["first_name"], v["last_name"]) == ("Ada", "Lovelace")


def test_there_is_one_filler_both_modules_use():
    sources = {m: Path(m.__file__).read_text(encoding="utf-8") for m in (assist_mod, autoapply)}
    assert all("filler.fill_field" in src for src in sources.values())
    assert "label_map" not in sources[assist_mod] and "label_map" not in sources[autoapply], "the standard fields live in apply/filler.py"
    for src in sources.values():
        names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
        assert not names & {"_decide", "_choice", "_select"}, "the field handlers live in apply/filler.py"
