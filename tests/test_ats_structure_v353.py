"""v3.5.3 (10 Nov 2026, fix 4): the CV templates print every role as employer (and place) on line 1, then the title and the dates on line 2, so that
`pdftotext -raw` reads employer -> title -> dates -> bullets. The role-order check looks for a role inside the Experience section only, each part as
whole words: the 10 Nov RightShip CV got a false CRITICAL because its profile said "Freelance AI/ML engineering ...", which the check took for the
Freelance role's employer and title (a bullet then sat "between" them). Another order is a warning (`role-sequence`), never critical."""
from pathlib import Path

import pytest

from careerops import ats_structure as AS
from careerops.llm.mock import MockLLM
from careerops.tailoring import tailor
from conftest import fixture_profile_settings

ROLES = [{"id": "acme", "title": "Data Scientist", "org": "Acme Analytics", "dates": "May 2026 – Sep 2026"},
         {"id": "freelance", "title": "AI/ML Engineer", "org": "Freelance", "dates": "Jan 2025 – Aug 2025"}]
PROSE = "Built and evaluated models end to end, and wrote up what they could and could not do. "


def cv(experience: str) -> str:
    return ("Ada Lovelace\nAI Engineer | LLM agent checked by ~65 tests\nBristol, UK | ada.lovelace@example.org | +44 7700 900123\nPROFILE\n"
            "Freelance AI/ML engineering across scikit-learn, Keras and LangChain. " + PROSE * 3 + "\nSKILLS\nPython, SQL, R, scikit-learn\n"
            "WORK EXPERIENCE\n" + experience + "EDUCATION\nUniversity of Northfield\nMSc Data Science Sep 2025 – Sep 2026\n")


NEW = ("Acme Analytics · Bristol, UK\nData Scientist May 2026 – Sep 2026\n• " + PROSE + "\nFreelance · Chennai, India\n"
       "AI/ML Engineer Jan 2025 – Aug 2025\n• Built scikit-learn pipelines served as FastAPI services.\n")
OLD = ("Acme Analytics · Bristol, UK May 2026 – Sep 2026\nData Scientist\n• " + PROSE + "\nFreelance · Chennai, India Jan 2025 – Aug 2025\n"
       "AI/ML Engineer\n• Built scikit-learn pipelines served as FastAPI services.\n")


def test_a_profile_that_names_the_freelance_role_is_not_its_heading():
    """The false CRITICAL of 10 Nov: "Freelance AI/ML engineering" in the profile is neither the employer nor the title (whole words, Experience only)."""
    text = cv(NEW)
    seq: list[str] = []
    assert AS.role_order_problems(text, ROLES, text, seq) == [] and seq == []
    st = AS.score(AS.Extract(text, text), roles=ROLES)
    assert not [i for i in st.issues if i.code in ("role-order", "role-sequence")]


def test_the_old_employer_dates_title_order_is_a_warning_not_critical():
    text = cv(OLD)
    st = AS.score(AS.Extract(text, text), roles=ROLES)
    assert not [i for i in st.critical if i.code == "role-order"]
    warn = next(i for i in st.issues if i.code == "role-sequence")
    assert warn.severity == AS.WARNING and "Freelance: read as employer -> dates -> title" in warn.message


def test_a_bullet_between_a_roles_employer_and_title_inside_experience_is_still_critical():
    broken = NEW.replace("Freelance · Chennai, India\nAI/ML Engineer", "Freelance · Chennai, India\n• A stray line.\nAI/ML Engineer")
    text = cv(broken)
    st = AS.score(AS.Extract(text, text), roles=ROLES)
    hit = next(i for i in st.critical if i.code == "role-order")
    assert "Freelance: a bullet comes between" in hit.message


def test_a_long_title_is_matched_on_its_first_characters_across_a_wrap():
    roles = [{"org": "AstraZeneca", "title": "Data Scientist, MSc Dissertation Project", "dates": "May 2026 – Sep 2026"}]
    text = cv("AstraZeneca · Bristol, UK (Remote)\nData Scientist, MSc Dissertati\non Project May 2026 – Sep 2026\n• " + PROSE + "\n")
    seq: list[str] = []
    assert AS.role_order_problems(text, roles, text, seq) == [] and seq == []


# ------------------------------------------------------------------ the rendered templates (xelatex)
def _job() -> dict:
    return {"id": "manual:roles353", "source": "manual", "company": "Northwind Health AI", "title": "AI Engineer", "location": "Bristol, UK",
            "url": "u", "apply_url": "u",
            "description": (Path(__file__).parent / "fixtures" / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")}


@pytest.mark.slow
@pytest.mark.parametrize("one_page", [False, True])
def test_every_role_of_a_rendered_cv_reads_employer_title_dates_then_bullets(tmp_path, fb_fixture, one_page):
    s = fixture_profile_settings()
    rep = tailor.tailor_job(_job(), fb_fixture, MockLLM(), s, tmp_path, one_page=one_page)
    ex = AS.extract(rep["cv_pdf"])
    roles = AS.roles_on_cv(fb_fixture, ex.layout)
    assert len(roles) >= 2
    seq: list[str] = []
    assert AS.role_order_problems(ex.raw, roles, ex.layout, seq) == [] and seq == []
    flat = AS._norm(AS.section(ex.raw, "experience"))                     # dashes made one ("Jun 2022 - Sep 2022" on the one-page CV)
    for r in roles:                                                        # employer, then its title, then its dates, then its first bullet
        at = flat.index(AS._norm(r["org"]))
        title = flat.index(AS._norm(r["title"].split(" (")[0]), at)
        dates = flat.index(AS._norm(r["dates"]), title)
        assert at < title < dates < flat.index("•", dates), r
    st = AS.score(ex, roles=roles)
    assert not [i for i in st.issues if i.code in ("role-order", "role-sequence")]
