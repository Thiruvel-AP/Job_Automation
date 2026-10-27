"""The document check (tailoring/doccheck.py, Document rules A1 to A7): every CV rule on the finished context, the .tex and the PDF text, in one
`DOC CHECK: pass` or a list of failures. A failure blocks `[a]pprove` (the user types `override doc check`) and always blocks auto-submit.
The CV's .tex is the real template rendered to a string; the PDF text is a string; no network, no model, no xelatex."""
import copy

import pytest

from careerops.tailoring import doccheck, render, tailor
from careerops.tailoring.doccheck import DocCheck

HOOK = {"hook_id": "h_agent", "entry_id": "az", "entry_kind": "experience", "proof_bullet_id": "az1",
        "headline": "LLM agent that recovers from tool failure, ~65-test evals",
        "profile_line": "AI engineer who builds the harness around the model: at Northwind I built a tool-calling agent that recovers when its tools fail.",
        "letter_sentence": "At Northwind I built a tool-calling agent that recovers when its tools fail, tested by ~65 tests."}
FAMILY_LINE = "Builds the modelling a brief needs and reports its limits plainly."


def sentence(n: int, tag: str) -> str:
    return " ".join([tag.capitalize(), *(f"{tag}{i}" for i in range(2, n + 1))]) + "."


PROFILE = " ".join([HOOK["profile_line"], sentence(40, "bodyone"), sentence(45, "bodytwo")])
CERTS = ["Model Context Protocol: Advanced Topics — Anthropic, Jun 2026", "AWS Academy Cloud Foundations, Nov 2025"]


def project(pid, n=2, **kw):
    return {"id": pid, "name": f"Project {pid}", "stack": "Python", "github": f"https://github.com/u/{pid}",
            "bullet_ids": [f"{pid}b{i}" for i in range(n)], "bullets": [f"Built part {i} of {pid}." for i in range(n)], **kw}


def make_rep(**over):
    ctx = {"id": {"name": "Ada Lovelace", "location": "Bristol, UK", "email": "ada@example.org", "headline": "Data Scientist"},
           "hook_headline": HOOK["headline"], "hook_number": "65", "profile": PROFILE,
           "skills": {"ML": ["Python", "scikit-learn"], "Cloud": ["AWS Lambda", "Docker"]},
           "experience": [{"id": "az", "org": "Northwind", "dates": "2025", "title": "Engineer", "bullet_ids": ["az1", "az2", "az3"],
                           "bullets": ["Built the agent.", "Wrote the tests.", "Shipped it."]}],
           "projects": [project("p1"), project("p2"), project("p3")],
           "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": list(CERTS), "achievements": []}
    rep = {"context": ctx, "hook": dict(HOOK), "headline": "Data Scientist", "family_line": FAMILY_LINE,
           "skills": {"family": None}, "letter": {"status": "off", "paragraphs": []}}
    for k, v in over.items():
        rep[k] = v
    return rep


def tex_of(rep) -> str:
    return render.env().get_template("cv2.tex.j2").render(**render.with_public_links(rep["context"]))


def run(rep, settings, tex=None, **kw) -> DocCheck:
    return doccheck.check(rep, settings, tex=tex if tex is not None else tex_of(rep), cv_text=kw.pop("cv_text", ""), **kw)


def test_a_clean_cv_passes_and_prints_pass(settings):
    out = run(make_rep(), settings)
    assert out.ok and out.failures == [] and out.text() == "DOC CHECK: pass"


def test_a_failure_prints_the_list(settings):
    rep = make_rep()
    rep["context"]["profile"] = HOOK["profile_line"]
    out = run(rep, settings)
    assert not out.ok and out.text().splitlines()[0] == "DOC CHECK: FAILED (1)" and out.text().splitlines()[1].startswith("  - PROFILE ")


# ------------------------------------------------------------------ A1: one hook entry, or the family headline
def test_the_headline_the_first_profile_sentence_and_the_first_bullet_come_from_the_same_hook(settings):
    assert run(make_rep(), settings).ok
    rep = make_rep()
    rep["context"]["hook_headline"] = "Forecasting that survives a held-out year"
    assert any(f.startswith("HOOK headline") for f in run(rep, settings).failures)


def test_a_headline_the_length_fit_cut_short_is_still_the_hooks(settings):
    rep = make_rep()
    rep["context"]["hook_headline"] = "LLM agent that recovers from tool failure"
    assert run(rep, settings).ok


def test_a_profile_that_does_not_open_with_the_hooks_line_fails(settings):
    rep = make_rep()
    rep["context"]["profile"] = " ".join([FAMILY_LINE, sentence(110, "x")])
    assert any(f.startswith("HOOK profile") for f in run(rep, settings).failures)


def test_the_hooks_proof_bullet_must_be_first_in_its_role(settings):
    rep = make_rep()
    rep["context"]["experience"][0]["bullet_ids"] = ["az2", "az1", "az3"]
    assert any(f.startswith("HOOK first bullet") for f in run(rep, settings).failures)
    rep = make_rep()
    rep["hook"].update(entry_kind="project", entry_id="p2", proof_bullet_id="p2b0")
    assert any(f.startswith("HOOK first bullet") for f in run(rep, settings).failures) is False                     # p2's first bullet is p2b0


def test_without_a_hook_the_family_headline_and_the_family_line_stand(settings):
    rep = make_rep(hook=None)
    rep["context"].pop("hook_headline")
    rep["context"]["profile"] = " ".join([FAMILY_LINE, sentence(110, "x")])
    assert run(rep, settings).ok
    rep["context"]["id"]["headline"] = "Some Other Title"
    assert any(f.startswith("HOOK headline") for f in run(rep, settings).failures)


# ------------------------------------------------------------------ A2: the profile
@pytest.mark.parametrize("n,ok", [(99, False), (100, True), (150, True), (151, False)])
def test_the_profile_is_100_to_150_words(settings, n, ok):
    rep = make_rep()
    extra = n - len(HOOK["profile_line"].split())
    rep["context"]["profile"] = " ".join([HOOK["profile_line"], sentence(extra, "pad")])
    found = [f for f in run(rep, settings).failures if f.startswith("PROFILE")]
    assert (found == []) is ok
    if n == 99:
        assert found == ["PROFILE 99/100 words: add profile sentences to fact_bank.yaml → profile.sentences"]


# ------------------------------------------------------------------ A3: skills
def test_a_repeat_in_the_skills_section_fails_and_names_the_bank_family(settings):
    rep = make_rep()
    rep["context"]["skills"] = {"Cloud & Deployment": "AWS (Lambda, S3), Docker", "Software": "Python, Lambda"}
    rep["skills"] = {"family": "ai_engineer"}
    assert 'SKILLS repeat "Lambda" in Cloud & Deployment and Software: fix in fact_bank.yaml families.ai_engineer.skill_lines' in run(rep, settings).failures
    assert rep["context"]["skills"]["Software"] == "Python, Lambda"                                      # never rewritten


# ------------------------------------------------------------------ A4: projects
def test_at_most_three_projects(settings):
    rep = make_rep()
    rep["context"]["projects"].append(project("p4"))
    assert "PROJECTS 4 projects on the CV (at most 3)" in run(rep, settings).failures


def test_a_project_has_one_to_three_bullets(settings):
    rep = make_rep()
    rep["context"]["projects"][0] = project("p1", n=4)
    assert 'PROJECTS "Project p1" has 4 bullets (1 to 3)' in run(rep, settings).failures
    rep["context"]["projects"][0] = project("p1", n=0)
    assert 'PROJECTS "Project p1" has 0 bullets (1 to 3)' in run(rep, settings).failures


def test_a_private_project_whose_url_is_printed_fails_and_a_public_one_without_its_url_fails(settings):
    rep = make_rep()
    rep["context"]["projects"][0]["public"] = False
    leaky = render.env().get_template("cv2.tex.j2").render(**rep["context"])                       # rendered without with_public_links
    assert 'PROJECTS "Project p1" prints https://github.com/u/p1 but the fact bank says public: false' in run(rep, settings, tex=leaky).failures
    assert run(rep, settings).ok                                                                      # the real render hides it
    rep = make_rep()
    hidden = tex_of(rep).replace("https://github.com/u/p2", "")
    assert 'PROJECTS "Project p2" does not print its public URL https://github.com/u/p2' in run(rep, settings, tex=hidden).failures


# ------------------------------------------------------------------ A6: certifications
def test_certifications_must_be_one_list_item_each(settings):
    assert run(make_rep(), settings).ok
    rep = make_rep()
    good = tex_of(rep)
    cut = good.index("\\section{Certifications")
    joined = "\\section{Certifications \\& Achievements}\n\\begin{itemize}\n  \\item A \\textperiodcentered\\, B\n\\end{itemize}\n\\end{document}\n"
    bad = good[:cut] + joined
    found = run(rep, settings, tex=bad).failures
    assert "CERTIFICATIONS 1 list item(s) for 2 certification(s): one item each" in found
    assert "CERTIFICATIONS are joined with a middle dot" in found


def test_an_achievement_line_counts_as_an_item_beside_the_certifications(settings):
    rep = make_rep()
    rep["context"]["achievements"] = [{"id": "x", "name": "Datathon", "award": "3rd place", "text": "Detected anomalies.", "result": ""}]
    assert run(rep, settings).ok


def test_no_certifications_no_section_no_failure(settings):
    rep = make_rep()
    rep["context"]["certifications"] = []
    assert run(rep, settings).ok


def test_the_check_changes_nothing_in_the_report(settings):
    rep = make_rep()
    before = copy.deepcopy(rep)
    run(rep, settings)
    assert rep == before


# ------------------------------------------------------------------ the cover letter (B1 to B5)
from careerops.tailoring import letter_reuse as RU  # noqa: E402

AD = ("Acme Health builds software that helps hospitals schedule operating theatres. We are hiring a data scientist to design and develop "
      "forecasting models that reduce cancelled operations.")
BANK = {"experience": [{"id": "az", "org": "Northwind", "bullets": [
            {"id": "az1", "text": "Built a tool-calling agent that recovers when its tools fail, tested by ~65 tests.", "terms": ["agent"]},
            {"id": "az2", "text": "Wrote the evaluation harness in pytest.", "terms": ["pytest"]}]}],
        "projects": [], "skills": {"Core": ["Python", "SQL", "Docker"]}, "deny_terms": [], "identity": {}}
OPENING = ('At Northwind I built a tool-calling agent that recovers when its tools fail, tested by ~65 tests, the kind of work your ad describes: '
           '"design and develop forecasting models that reduce cancelled operations".')
CLOSING = "Could we take 20 minutes to talk about forecasting models?"
WORDS = "separately I keep each explanation short and name the trade offs openly so reviewers can challenge them early".split()


def filler(n: int, seed: int = 1) -> str:
    import random

    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def letter_paragraphs(total: int = 400) -> list[str]:
    fixed = len(" ".join([OPENING, CLOSING]).split())
    each = (total - fixed - 25) // 3
    return [OPENING, "Acme Health builds software that helps hospitals schedule operating theatres. " + filler(each, 2),
            "On forecasting, I built the tool-calling agent. " + filler(each, 3), "On testing, I wrote the harness in pytest. " + filler(each, 4), CLOSING]


def letter_rep(paragraphs=None, **over):
    rep = make_rep()
    rep["keywords"] = []
    rep["letter"] = {"status": "ok", "mode": "company", "paragraphs": paragraphs or letter_paragraphs(), "evidence": [{"id": "az1", "text": ""}, {"id": "az2", "text": ""}]}
    rep["context"]["experience"][0]["bullet_ids"] = ["az1", "az2", "az3"]
    rep["context"]["skills"] = {"Core": ["Python", "SQL", "Docker"]}
    rep.update(over)
    return rep


def letter_tex(rep) -> str:
    h = tailor.letter_header(rep["context"], [])
    return render.env().get_template("cover_letter.tex.j2").render(id=rep["context"]["id"], company="Acme", paragraphs=rep["letter"]["paragraphs"],
                                                                   date="27 October 2026", headline=h["headline"], headline_number=h["number"])


def run_letter(rep, settings, **kw) -> list[str]:
    kw.setdefault("letter_tex", letter_tex(rep))
    kw.setdefault("texts", (AD, ""))
    out = run(rep, settings, fb=BANK, **kw)
    return [f for f in out.failures if f.startswith("LETTER")]


def test_a_good_company_letter_passes_every_letter_rule(settings):
    assert run_letter(letter_rep(), settings) == []


@pytest.mark.parametrize("total,ok", [(299, False), (300, True), (500, True), (501, False)])
def test_the_letter_is_300_to_500_words(settings, total, ok):
    rep = letter_rep(letter_paragraphs(total))
    n = len(" ".join(rep["letter"]["paragraphs"]).split())
    rep["letter"]["paragraphs"][3] += " " + " ".join(["more"] * (total - n)) if total > n else ""
    n = len(" ".join(rep["letter"]["paragraphs"]).split())
    found = [f for f in run_letter(rep, settings) if "words" in f]
    assert (found == []) is ok, (n, found)
    if not ok:
        assert found == [f"LETTER {n} words: must be between 300 and 500"]


def test_a_letter_that_is_not_in_company_mode_or_not_ok_is_not_judged_by_these_rules(settings):
    rep = letter_rep()
    rep["letter"]["mode"] = "hook"
    assert run_letter(rep, settings) == []
    rep = letter_rep()
    rep["letter"] = {"status": "none", "paragraphs": []}
    assert run_letter(rep, settings) == []


def test_the_letter_header_must_carry_the_cvs_headline_and_contact_line(settings):
    rep = letter_rep()
    assert run_letter(rep, settings) == []
    h = tailor.letter_header(rep["context"], [])
    bare = letter_tex(rep).replace(render.hook_tex(h["headline"], h["number"], "num"), "")
    assert "LETTER header: the CV's hook headline is not under the name" in run_letter(rep, settings, letter_tex=bare)
    no_contact = letter_tex(rep).replace(render.contact_line(rep["context"]["id"]), "")
    assert "LETTER header: the CV's contact line is not under the name" in run_letter(rep, settings, letter_tex=no_contact)


def test_a_skill_the_cv_lacks_and_a_bullet_it_does_not_print_fail(settings):
    rep = letter_rep()
    rep["letter"]["paragraphs"][3] += " I also used Docker for this."
    rep["context"]["skills"] = {"Core": ["Python", "SQL"]}
    assert any("names 'docker', which is not in this CV's Skills or bullets" in f for f in run_letter(rep, settings))
    rep = letter_rep()
    rep["context"]["experience"][0]["bullet_ids"] = ["az1", "az3"]
    assert "LETTER P3/P4 draw on bullet az2, which is not printed on this job's CV" in run_letter(rep, settings)


def test_a_top_heavy_body_fails(settings):
    rep = letter_rep()
    rep["letter"]["paragraphs"][1] = " ".join(["Acme Health builds software for hospitals."] * 8)
    assert any(f.startswith("LETTER body: ") and "must (shorten P2)" in f for f in run_letter(rep, settings))


@pytest.mark.parametrize("first,expect", [
    ('I am writing to apply because at Northwind I shipped ~65 tests: "design and develop forecasting models".', "starts with a banned phrase"),
    ('At Northwind I shipped tests: "design and develop forecasting models".', "no number from the fact bank"),
    ('At Northwind I shipped ~65 tests: "ship quantum toasters to the moon".', "is not in the ad or the company's own page"),
    ("At Northwind I shipped ~65 tests.", "no problem phrase from the ad or the company's page")])
def test_the_opening_rules(settings, first, expect):
    rep = letter_rep()
    rep["letter"]["paragraphs"][0] = first
    assert any(expect in f for f in run_letter(rep, settings)), run_letter(rep, settings)


def test_a_phrase_from_the_stored_company_page_is_accepted(settings):
    rep = letter_rep()
    rep["letter"]["paragraphs"][0] = 'At Northwind I shipped ~65 tests, the kind of work your own careers page describes: "cut theatre waiting lists".'
    assert any("is not in the ad" in f for f in run_letter(rep, settings))
    assert [f for f in run_letter(rep, settings, texts=(AD, "We cut theatre waiting lists in every trust.")) if "opening" in f] == []


def test_a_body_that_copies_an_earlier_letter_fails_and_names_it(settings):
    rep = letter_rep()
    earlier = [RU.Earlier("acme_ds_1", " ".join(rep["letter"]["paragraphs"][1:4]))]
    found = run_letter(rep, settings, earlier=earlier)
    assert len(found) == 1 and found[0].startswith("LETTER reuse: the body is 1.00 like the letter 'acme_ds_1' (limit 0.30)")
    assert run_letter(rep, settings, earlier=[RU.Earlier("far", "nothing alike here at all in these few words")]) == []
