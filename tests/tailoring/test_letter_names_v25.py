"""v2.5: the letter's per-paragraph name guard (company_letter.user_problems -> grounding_problems) gets the real ad and this job's CV names, as
name_problems does; a hyphenated compound ("Python-driven") is checked by its name part ("Python"). Both ways: a name nothing allowed says is still
rejected. No model, no network."""
from careerops.tailoring import company_letter as CL
from careerops.tailoring.grounding import grounding_problems, name_allowed, name_head, norm_literal

from test_company_letter import P3, P4, TEAM, check, letter_parts, world  # noqa: F401  (world is a fixture)

EVIDENCE = [{"text": "Built a churn model on 7,043 customer records", "terms": []}]


def _flags(problems: list[str], name: str) -> bool:
    return any(f"'{name}' (name)" in p for p in problems)


def test_name_head_takes_the_name_part_of_a_hyphenated_compound():
    assert name_head("Python-driven") == "Python"
    assert name_head("AWS-hosted") == "AWS"
    assert name_head("Scikit-Learn") == "Scikit-Learn"          # a compound of names stays whole
    assert name_head("Python") == "Python"


def test_a_hyphenated_compound_is_allowed_by_its_name_part_and_only_by_it():
    allowed = norm_literal("Skills: Python, SQL, Docker")
    assert name_allowed(allowed, "Python-driven")
    assert not name_allowed(allowed, "Rust-driven")
    assert not name_allowed(allowed, "Rust")


def test_grounding_lets_python_driven_through_when_python_is_allowed_and_rejects_rust_driven():
    ok = grounding_problems(["I built a Python-driven churn model on 7,043 customer records."], EVIDENCE, "", extra="Python")
    assert not _flags(ok, "Python-driven"), ok
    bad = grounding_problems(["I built a Rust-driven churn model on 7,043 customer records."], EVIDENCE, "", extra="Python")
    assert _flags(bad, "Rust-driven"), bad


def _user(w, p3: str, jd: str = "", cv_names: str = "") -> list[str]:
    opening = f"{w.plan.p1} {w.plan.p2 or ''}"
    return CL.user_problems([p3, P4], w.plan.groups, opening, w.fb, w.kws, w.plan.reqs, w.job["company"], w.job["title"], jd=jd, cv_names=cv_names)


def test_the_per_paragraph_guard_allows_a_name_the_ad_gives(world):
    p3 = P3 + " The Zephyrine reviews kept it honest."
    assert _flags(_user(world, p3), "Zephyrine")                                   # no ad, no CV: rejected, as before
    assert not _flags(_user(world, p3, jd="Our teams use Zephyrine reviews every week."), "Zephyrine")


def test_the_per_paragraph_guard_allows_a_name_this_jobs_cv_prints(world):
    p3 = P3 + " The Zephyrine reviews kept it honest."
    assert not _flags(_user(world, p3, cv_names="Python SQL Zephyrine"), "Zephyrine")
    assert _flags(_user(world, p3, cv_names="Python SQL"), "Zephyrine")         # a name on neither is still rejected


def test_the_per_paragraph_guard_checks_a_compounds_name_part_against_the_cv(world):
    assert not _flags(_user(world, P3 + " The Zephyrine-driven reviews kept it honest.", cv_names="Zephyrine"), "Zephyrine-driven")
    assert _flags(_user(world, P3 + " The Quorvane-driven reviews kept it honest.", cv_names="Zephyrine"), "Quorvane-driven")


def test_check_passes_the_ad_and_the_cv_names_to_the_per_paragraph_guard(world, monkeypatch):
    seen = {}
    real = CL.user_problems

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(CL, "user_problems", spy)
    CL.check(letter_parts(world), world.job, world.fb, world.kws, world.plan, TEAM, world.settings, claimable=world.claimable,
             cv={"ids": [e["id"] for e in world.plan.evidence], "text": "", "names": "Python XGBoost Zephyrine"})
    assert world.job["description"][:200] in seen["jd"]
    assert seen["cv_names"] == "Python XGBoost Zephyrine"
