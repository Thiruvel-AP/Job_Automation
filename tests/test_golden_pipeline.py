"""The golden Anaplan bar, tested on what the PIPELINE writes (fix round 2).

tests/test_golden_anaplan.py checks the hand-made golden files themselves; that proves the bar exists, not that the pipeline reaches it.
This file runs `tailor_job` on the golden ad (tests/golden/anaplan/jd.txt) with a deterministic MockLLM and a fictional fact bank of the
real bank's shape (tests/fixtures/golden/fact_bank_anaplan.yaml; a test never reads the real profile) and asserts the hook rules on the
CV and both letters that came out:

  headline       a line under the name, 12pt, the hook_library entry's headline word for word (with the number)
  profile        sentence 1 is the entry's profile_line, then profile_facts sentences
  first bullet   the first bullet of the most relevant role is the proof bullet
  letters        (letter.mode hook) letter A is assembled by code: sentence 1 is the entry's letter_line, at most 250 words, no pasted
                 `terms:` list; B is none (no model writes a gap sentence)
  CV text        no hyphen outside the exception list; the forecasting project is on the CV, the SwiftUI bullet is not preferred over it

No network, no real model; xelatex is run (slow) and a missing tool fails the test.
"""
import re
from pathlib import Path

import pytest
import yaml

from careerops import hook as H
from careerops.config import load_yaml
from careerops.llm import MockLLM
from careerops.tailoring import letters as L
from careerops.tailoring import tailor
from careerops.tailoring import typography
from careerops.tailoring.letter_rules import term_dump_problems
from careerops.tailoring.render import pdf_text
from careerops.tailoring.scorer import Keyword

HERE = Path(__file__).parent
GOLDEN = HERE / "golden" / "anaplan"
FIXTURE = HERE / "fixtures" / "golden" / "fact_bank_anaplan.yaml"
NUMBER = "~65"

# the user's hook_library, for this fictional bank (the pipeline picks one entry per ad by the keyword weight of its terms)
LIBRARY = [{
    "id": "h_prod_tests", "proof": "az4",
    "terms": ["production", "deployment", "testing", "tests", "mlops", "ci/cd", "reliability", "evaluation", "quality", "aws", "lambda", "prototype"],
    "headline": "ML from prototype to production: deployed on AWS behind ~65 tests",
    "profile_line": "Data scientist who takes models from prototype to production: at Northfield Pharma Research I deployed an agent on "
                    "AWS Lambda behind ~65 tests.",
    "letter_line": "Anaplan needs models taken from prototype to production, and at Northfield Pharma Research I did that for an agent, "
                   "behind ~65 tests.",
}]


def _plain(tex: str) -> str:
    t = tex.replace(r"\textasciitilde{}", "~").replace(r"\textasciitilde", "~").replace(r"\,\textperiodcentered\,", "·")
    t = re.sub(r"\\(?:num|textbf|textit)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\([&%$#_])", r"\1", t)
    return re.sub(r"\s+", " ", t.replace("{", "").replace("}", "")).strip()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One pipeline run on the golden ad: (report, fact bank, job, keywords as the run saw them)."""
    from conftest import fixture_profile_settings

    settings = fixture_profile_settings()
    tmp = tmp_path_factory.mktemp("golden_pipeline")
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge", "robots", "secrets",
              "discover", "aiapply_applied", "calibration", "frozen_keywords"):
        settings.raw["paths"][k] = str(tmp / Path(settings.raw["paths"][k]).name)
    settings.raw["paths"]["applications"] = str(tmp / "Companies")
    settings.raw["paths"]["tmp"] = str(tmp / "stage")
    settings.raw["letter"]["mode"] = "hook"
    fb = {**load_yaml(FIXTURE), "hook_library": [dict(e) for e in LIBRARY]}
    jd = (GOLDEN / "jd.txt").read_text(encoding="utf-8")
    low = jd.lower()
    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    required = [r["term"] for r in rows if r["weight"] >= 2 and r["term"].lower() in low]
    preferred = [r["term"] for r in rows if r["weight"] < 2 and r["term"].lower() in low]
    llm = MockLLM({"extract_keywords": lambda ctx: {"required": required, "preferred": preferred, "team": "Functional Analytics",
                                                    "company_hook": "Anaplan builds scenario planning software.", "context_terms": []}})
    job = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan",
           "title": "Data Scientist, Functional Analytics (REQ 26490)", "location": "London, UK",
           "url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002",
           "apply_url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002", "description": jd,
           "posted_at": "2026-08-07T09:00:00Z", "posted_at_source": "greenhouse.first_published"}
    rep = tailor.tailor_job(job, fb, llm, settings, settings.path("outputs"))
    return rep, fb, job, [Keyword(**k) for k in rep["keywords"]]


# ------------------------------------------------------------------------------------------------------------- the hook on the CV
@pytest.mark.slow
def test_the_hook_came_from_the_library_and_names_the_numbered_bullet_of_the_most_relevant_role(run):
    rep, fb, job, kws = run
    hk = H.Hook.from_dict(rep["hook"])
    assert hk.source == "library" and hk.hook_id == "h_prod_tests" and hk.number == NUMBER and hk.proof_bullet_id == "az4", (hk.source, hk.notes, hk.proof_bullet_id)
    assert (hk.headline, hk.profile_line, hk.letter_sentence) == (LIBRARY[0]["headline"], LIBRARY[0]["profile_line"], LIBRARY[0]["letter_line"])
    assert hk.entry_id == hk.relevant_role_id == H.most_relevant_role(fb, H.bullet_hook_scores(fb, kws)) == "az"
    assert rep["hook_notes"] == [] and not any(w.startswith("hook:") for w in rep["analysis_warnings"])
    assert rep["hook_placed"] == {"headline": True, "profile": True, "bullet": True, "number_in_pdf": True}


@pytest.mark.slow
def test_headline_a_12pt_line_under_the_name_with_the_number(run):
    rep, *_ = run
    hk = H.Hook.from_dict(rep["hook"])
    tex = Path(rep["cv_pdf"]).with_suffix(".tex").read_text(encoding="utf-8")
    m = re.search(r"\\fontsize\{12\}\{14\}\\selectfont\\color\{navy\}\s*(.+?)\}\\\\", tex)
    assert m, "no 12pt navy headline line in the generated CV"
    headline = _plain(m.group(1))
    assert NUMBER in headline and len(headline) <= H.HEADLINE_MAX and headline == _flat(hk.headline)
    lines = [_flat(x) for x in pdf_text(Path(rep["cv_pdf"])).splitlines() if x.strip()]
    assert lines[0].startswith("Ada") and lines[1] == headline, lines[:3]                     # the name, then the headline: one line


@pytest.mark.slow
def test_profile_sentence_one_is_the_library_line_and_the_rest_are_profile_facts(run):
    rep, fb, job, kws = run
    profile = rep["context"]["profile"]
    assert profile.startswith(LIBRARY[0]["profile_line"]) and NUMBER in profile
    rest = profile[len(LIBRARY[0]["profile_line"]):].split(". ")
    assert 1 <= len([s for s in rest if s.strip()]) <= 2 and all(any(f.rstrip(".") in s for f in fb["profile_facts"]) for s in rest if s.strip())
    assert not H.vague_words(profile) and "skilled" not in profile.lower() and profile.lower().count("data scientist") == 1
    assert _flat(LIBRARY[0]["profile_line"]) in _flat(pdf_text(Path(rep["cv_pdf"])))


@pytest.mark.slow
def test_first_bullet_of_the_most_relevant_role_is_the_proof_bullet(run):
    rep, fb, job, kws = run
    hk = H.Hook.from_dict(rep["hook"])
    role = next(e for e in rep["context"]["experience"] if e["id"] == H.most_relevant_role(fb, H.bullet_hook_scores(fb, kws)))
    assert role["bullet_ids"][0] == hk.proof_bullet_id and NUMBER in role["bullets"][0]
    assert role["bullet_ids"][0] == "az4" and role is rep["context"]["experience"][0]


@pytest.mark.slow
def test_every_proof_number_survives_into_the_pdf(run):
    rep, *_ = run
    assert NUMBER in _flat(pdf_text(Path(rep["cv_pdf"])))


# ---------------------------------------------------------------------------------------------------------------- the letters
@pytest.mark.slow
def test_sentence_one_of_the_letter_is_the_library_line_with_the_number(run):
    rep, *_ = run
    hk = H.Hook.from_dict(rep["hook"])
    letter = rep["letters"]["A"]
    assert letter["status"] == "ok", (letter["reason"], letter["problems"])
    first = re.split(r"(?<=[.!?])\s+", letter["paragraphs"][0], maxsplit=1)[0]
    assert NUMBER in first and letter["paragraphs"][0] == hk.letter_sentence == LIBRARY[0]["letter_line"]
    assert NUMBER in _flat(pdf_text(Path(letter["pdf"])))                              # and it is in the letter PDF itself


@pytest.mark.slow
def test_the_letter_is_short_prose_not_a_list_of_terms(run):
    rep, fb, job, kws = run
    letter = rep["letters"]["A"]
    assert letter["words"] <= 250 and len(" ".join(letter["paragraphs"]).split()) <= 250
    assert term_dump_problems(letter["paragraphs"], fb) == []
    assert letter["paragraphs"][-1].endswith("?") and "20 minutes" in letter["paragraphs"][-1]
    assert "keen to apply" not in " ".join(letter["paragraphs"]).lower()


@pytest.mark.slow
def test_there_is_no_letter_b_because_no_model_writes_a_gap_sentence(run):
    rep, fb, job, kws = run
    a, b = rep["letters"]["A"], rep["letters"]["B"]
    assert rep["letters"]["choice"] == "A" and a["status"] == "ok" and b["status"] == "none" and "no model" in b["reason"]
    assert not any("have not used" in p for p in a["paragraphs"])
    learn = {r["term"] for r in L.learn_list(kws, rep["claimable"], fb["deny_terms"])}
    assert "dbt" in learn and not Path(str(b["pdf"])).is_file()                      # the learn list is still in the note; no sentence uses it


@pytest.mark.slow
def test_the_letter_is_a_file_under_letters_a_and_none_is_written_for_b(run):
    rep, *_ = run
    assert rep["letter_choice"] == "A" and rep["letters"]["A"]["variant"] == "A" and rep["letters"]["B"]["variant"] == "B"
    assert Path(rep["letters"]["A"]["pdf"]).is_file() and Path(rep["letters"]["A"]["pdf"]).parent.name == "A"
    assert rep["letters"]["B"]["pdf"] is None
    assert "Could we find twenty minutes" not in " ".join(rep["letters"]["A"]["paragraphs"])          # the old closing line is not used


# ------------------------------------------------------------------------------------------------------------------- the CV text
@pytest.mark.slow
def test_the_cv_has_no_hyphen_outside_the_exception_list(run):
    rep, fb, job, kws = run
    text = pdf_text(Path(rep["cv_pdf"]))
    keep = tailor.hyphen_keywords(kws)
    assert typography.audit(text, keep=keep)["hyphens"] == {}
    for word in ("class-imbalance", "cross-validation", "19,213-gene", "5-star", "e-commerce", "per-gene", "comparable-company"):
        assert word not in text


@pytest.mark.slow
def test_the_forecasting_project_is_on_the_cv_and_the_swiftui_bullet_is_not_ahead_of_it(run):
    rep, fb, job, kws = run
    ids = {b for e in rep["context"]["projects"] for b in e["bullet_ids"]}
    assert "p_bt1" in ids, ids
    from careerops.tailoring.budget import relevance_scores

    sc = relevance_scores(fb, kws, job["description"])
    assert sc["p_bt1"] > sc["av7"]


@pytest.mark.slow
def test_three_ats_numbers_the_note_and_the_report_are_written(run):
    rep, *_ = run
    a = rep["ats"]
    assert {"strict", "ceiling", "combined", "letter"} <= set(a) and all(a[k] is not None for k in ("strict", "ceiling", "combined", "letter"))
    out = Path(rep["out_dir"])
    assert (out / "note.md").is_file() and (out / "report.json").is_file()
    assert rep["cv_pages"] == 2


# ---------------------------------------------------------------------------------- the failure of the real run, reproduced
def _run_on(bank: dict, tmp_path, llm: MockLLM):
    from conftest import fixture_profile_settings

    settings = fixture_profile_settings()
    for k in ("db", "outputs", "excel", "preferences", "sponsors_csv", "register_dir", "jds", "live", "judge", "robots", "secrets",
              "discover", "aiapply_applied", "calibration", "frozen_keywords"):
        settings.raw["paths"][k] = str(tmp_path / Path(settings.raw["paths"][k]).name)
    settings.raw["paths"]["applications"] = str(tmp_path / "Companies")
    settings.raw["paths"]["tmp"] = str(tmp_path / "stage")
    settings.raw["letter"]["mode"] = "hook"
    job = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan", "title": "Data Scientist, Functional Analytics",
           "location": "London, UK", "url": "https://example.org/anaplan", "apply_url": "https://example.org/anaplan",
           "description": (GOLDEN / "jd.txt").read_text(encoding="utf-8")}
    return tailor.tailor_job(job, bank, llm, settings, settings.path("outputs"))


@pytest.mark.slow
def test_when_no_hook_qualifies_the_run_says_so_and_makes_no_old_style_letter(tmp_path):
    fb = load_yaml(FIXTURE)
    fb.pop("hook_library", None)                                              # nothing writes a hook: no library, no hook
    rep = _run_on(fb, tmp_path, MockLLM())
    assert rep["hook"] is None and rep["hook_notes"] and any(w.startswith("hook: no library hook qualifies") for w in rep["analysis_warnings"])
    assert rep["hook_family_headline"] == rep["headline"] and "hook_headline" not in rep["context"]
    assert rep["letters"]["choice"] == "none" and rep["letter_pdf"] is None
    assert rep["letters"]["A"]["status"] == rep["letters"]["B"]["status"] == "none" and "no hook" in rep["letters"]["A"]["reason"]
    assert rep["letter"]["status"] == "none"
    from careerops.review import workflow

    lines = workflow.hook_lines(rep)
    assert lines[0].startswith("Hook: none qualifies") and any(x.startswith("  why:") for x in lines), lines          # the review screen says why
