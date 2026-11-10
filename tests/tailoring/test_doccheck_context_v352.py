"""v3.5.2 (10 Nov 2026), honesty over lint: an entry the fact bank marks as dissertation, coursework, volunteer or hackathon shows that word in its
own CV heading, and a degree whose display_line names a dissertation keeps a dissertation line in Education; the AstraZeneca title prints as the
fact bank writes it. The document check reads report contexts built here."""
from careerops.tailoring import doccheck, render
from careerops.tailoring import sections as S

AZ = {"id": "az", "org": "AstraZeneca", "location": "Bristol, UK (Remote)", "dates": "May 2026 – Sep 2026", "title": "Data Scientist",
      "employment_type": "indirect contract (MSc Dissertation)", "bullets": []}
COVERFIT = {"id": "coverfit", "name": "CoverFit — insurance benchmarking API (Bristol × UWE AI Hackathon)", "stack": "FastAPI, pandas", "bullets": []}
LSDE = {"id": "lsde", "name": "Auto-scaled word-frequency pipeline on AWS", "stack": "EC2, SQS", "github": None, "badge": "MSc Coursework", "bullets": []}
MSC = {"degree": "MSc Data Science", "display_org": "University of Bristol, UK", "dates": "Sep 2025 – Sep 2026",
       "display_line": "MSc Data Science (AI & Text Analytics); dissertation with AstraZeneca affiliation (GeneTraceAI)"}


def bank(az=AZ, projects=(COVERFIT, LSDE), education=(MSC,)):
    return {"experience": [az], "projects": list(projects), "education": list(education)}


def rep(experience, projects=(), education_compact=None, extra=None):
    ctx = {"experience": [dict(e, bullets=list(b)) for e, b in experience], "projects": [dict(p, bullets=["Built it."]) for p in projects],
           "education_compact": education_compact if education_compact is not None else S.compact_education([MSC])[0]}
    if extra is not None:
        ctx["additional_experience"] = extra
    return {"context": ctx}


def test_marks_are_read_from_the_fact_banks_own_fields():
    assert doccheck.marks(AZ) == [("dissertation", "employment_type", "indirect contract (MSc Dissertation)")]
    assert doccheck.marks(COVERFIT)[0][0] == "hackathon" and doccheck.marks(LSDE)[0][0] == "coursework"
    assert doccheck.marks({"title": "Data Consultant", "employment_type": "internship"}) == []
    assert doccheck.marks({"employment_type": "volunteering"})[0][0] == "volunteer"


def test_the_9_nov_cv_fails_a_title_with_no_dissertation_even_when_a_bullet_says_dissertations():
    """The RightShip CV of 9 Nov 2026: "Data Scientist" under AstraZeneca; a bullet that happens to say "dissertation's" does not count."""
    out = doccheck.context_problems(rep([(AZ, ["Wrote the dissertation's AI-agent sections."])]), bank())
    assert len(out) == 1 and out[0].startswith('CONTEXT experience "AstraZeneca" is marked dissertation') and "experience id `az`" in out[0]


def test_the_title_the_user_wrote_passes_and_prints_word_for_word():
    az = dict(AZ, title="Data Scientist, MSc Dissertation Project")
    assert doccheck.context_problems(rep([(az, ["Built an agent."])]), bank(az)) == []
    assert render.role_line(az) == "Data Scientist, MSc Dissertation Project"
    from test_cv_rules import ctx_with
    ctx = {**ctx_with([]), "experience": [dict(az, bullets=["Built an agent."])]}
    tex = render.env().get_template("cv2.tex.j2").render(**render.with_public_links(ctx))
    assert "Data Scientist, MSc Dissertation Project" in tex


def test_a_hackathon_or_coursework_project_shows_its_word_in_its_heading():
    assert doccheck.context_problems(rep([], [COVERFIT, LSDE]), bank()) == []
    renamed = dict(COVERFIT, name="CoverFit — insurance benchmarking API")              # the context dropped from the printed name
    out = doccheck.context_problems(rep([], [renamed]), bank(projects=(COVERFIT,)))
    assert len(out) == 1 and "marked hackathon" in out[0] and "projects id `coverfit`" in out[0]


def test_the_additional_experience_line_counts_as_the_entrys_heading():
    line = ["Data Scientist, MSc Dissertation Project, AstraZeneca (Bristol, May 2026 – Sep 2026)"]
    assert doccheck.context_problems(rep([], extra=line), bank()) == []
    out = doccheck.context_problems(rep([], extra=["Data Scientist, AstraZeneca (Bristol, May 2026 – Sep 2026)"]), bank())
    assert out and "Additional experience line" in out[0]


def test_education_must_keep_its_dissertation_line():
    assert doccheck.context_problems(rep([]), bank()) == []
    no_line = [r for r in S.compact_education([MSC])[0] if r["kind"] != "dissertation"]
    out = doccheck.context_problems(rep([], education_compact=no_line), bank())
    assert out == ['CONTEXT education "MSc Data Science" names a dissertation in the fact bank but the CV\'s Education has no dissertation line']


def test_the_document_check_fails_on_it_and_needs_the_fact_bank(settings):
    r = {**rep([(AZ, ["Built an agent."])]), "one_page": True}
    r["context"].update(hook_headline="", profile="")
    assert any(f.startswith("CONTEXT") for f in doccheck.check(r, settings, fb=bank()).failures)
    assert not any(f.startswith("CONTEXT") for f in doccheck.check(r, settings).failures)          # no fact bank: nothing to compare with
