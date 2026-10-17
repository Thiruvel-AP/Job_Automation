"""The golden Anaplan application (tests/golden/anaplan/): the quality bar a pipeline run for this job must reach.

The CV, the two letters and the ad were made by hand on 2026-10-15 (REQ 26490, Data Scientist, Functional Analytics). Checks:
  * the ATS scorer gives strict 72.5 and claimable 72.5 on cv.txt, within 0.1            (added with careerops/ats.py)
  * the hook ("prototype to production", "~65") is in the headline, in profile line 1 and in the first AstraZeneca bullet
  * the golden CV is two pages
  * both golden letters are at most 250 words (and, after the letter step, so are the generated ones)

The PDF check runs xelatex and reads it back with pdfinfo; a missing tool makes it fail, not skip. No network, no model.
"""
import re
from pathlib import Path

import pytest

from careerops.tailoring.render import compile_tex, page_count

GOLDEN = Path(__file__).parent / "golden" / "anaplan"
HOOK_PROBLEM = "prototype to production"       # what the ad hires for ("transition your models from prototype into ... production")
HOOK_NUMBER = "~65"                            # verbatim from the fact bank bullet (~65 tests across 8 files)
HEADLINE_MAX = 95


def _plain(tex: str) -> str:
    """LaTeX source as the words a reader sees: \\num{..} unwrapped, \\textasciitilde as ~, escapes removed."""
    t = tex.replace(r"\textasciitilde", "~").replace(r"\,\textperiodcentered\,", "·")
    t = re.sub(r"\\(?:num|textbf|textit)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\([&%$#_])", r"\1", t)
    return re.sub(r"\s+", " ", t.replace("{", "").replace("}", "")).strip()


def golden_headline() -> str:
    tex = (GOLDEN / "cv.tex").read_text(encoding="utf-8")
    m = re.search(r"\\fontsize\{12\}\{14\}\\selectfont\\color\{navy\}\s*(.+?)\}\\\\", tex)
    assert m, "the golden CV has no 12pt navy headline line under the name"
    return _plain(m.group(1))


def golden_profile_line1() -> str:
    tex = (GOLDEN / "cv.tex").read_text(encoding="utf-8")
    body = re.search(r"\\section\{Profile\}\s*(.+?)\\section", tex, re.S).group(1)
    return re.split(r"(?<=\.)\s+", _plain(body), maxsplit=1)[0]


def golden_first_az_bullet() -> str:
    tex = (GOLDEN / "cv.tex").read_text(encoding="utf-8")
    block = tex.split(r"\role{AstraZeneca}", 1)[1]
    return _plain(re.search(r"\\item\s+(.+)", block).group(1))


def words(tex: str) -> int:
    """Words of the letter body as the pipeline counts them (tailor._letter): the paragraphs through the call to action,
    greeting and sign-off excluded."""
    body = re.search(r"\n\s*Dear [^\n]+\n\n(.+?)(?=Kind regards)", tex, re.S).group(1)
    return len(_plain(body).split())


@pytest.mark.parametrize("where, text", [("headline", golden_headline), ("profile line 1", golden_profile_line1),
                                         ("first AstraZeneca bullet", golden_first_az_bullet)])
def test_hook_is_in_all_three_places(where, text):
    t = text()
    assert HOOK_PROBLEM in t.lower(), f"{where} does not name the problem: {t!r}"
    assert HOOK_NUMBER in t, f"{where} does not carry the number {HOOK_NUMBER}: {t!r}"


def test_golden_headline_is_one_short_line():
    h = golden_headline()
    assert len(h) <= HEADLINE_MAX, f"{len(h)} characters: {h!r}"


@pytest.mark.slow
def test_golden_cv_is_two_pages(tmp_path):
    pdf = compile_tex((GOLDEN / "cv.tex").read_text(encoding="utf-8"), tmp_path, "cv")
    assert page_count(pdf) == 2


@pytest.mark.parametrize("name", ["letter_A.tex", "letter_B.tex"])
def test_golden_letters_are_at_most_250_words(name):
    n = words((GOLDEN / name).read_text(encoding="utf-8"))
    assert n <= 250, f"{name}: {n} words"


def test_golden_letter_b_has_exactly_one_honest_gap():
    a, b = ((GOLDEN / f"letter_{x}.tex").read_text(encoding="utf-8") for x in "AB")
    assert "honest gap" not in a.lower() and b.lower().count("honest gap") == 1


# ---------------------------------------------------------------- the ATS bar (careerops/ats.py)
def _load_keywords():
    import yaml

    from careerops.tailoring.scorer import Keyword

    rows = yaml.safe_load((GOLDEN / "keywords.yaml").read_text(encoding="utf-8"))
    kws = [Keyword(r["term"], float(r["weight"]), r["weight"] >= 2, list(r.get("alts") or [])) for r in rows]
    return kws, [r["term"] for r in rows if r["claimable"]]


def _letter_text(name: str) -> str:
    tex = (GOLDEN / name).read_text(encoding="utf-8")
    return _plain(re.search(r"\\begin\{document\}(.+?)\\end\{document\}", tex, re.S).group(1))


def _reference():
    import importlib.util

    spec = importlib.util.spec_from_file_location("reference_ats_weights", GOLDEN / "reference_ats_weights.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CV_TXT = (GOLDEN / "cv.txt").read_text(encoding="utf-8")


def test_ats_strict_and_claimable_are_72_5_on_the_golden_cv():
    from careerops import ats

    kws, claimable = _load_keywords()
    s = ats.scores(CV_TXT, kws, claimable)
    assert s.strict == pytest.approx(72.5, abs=0.1)
    assert s.ceiling == pytest.approx(72.5, abs=0.1)
    assert s.gate == "ok" and s.negated == []


def test_the_keyword_file_agrees_with_the_reference_scorer_hit_for_hit():
    """keywords.yaml spells the reference's regexes out as words: without negation both give the same score on every golden text."""
    from careerops.tailoring.scorer import score

    kws, _ = _load_keywords()
    ref = _reference()
    for text in (CV_TXT, _letter_text("letter_A.tex"), _letter_text("letter_B.tex"),
                 CV_TXT + "\n" + _letter_text("letter_A.tex"), CV_TXT + "\n" + _letter_text("letter_B.tex")):
        assert 100 * score(text, kws).literal == pytest.approx(ref.score(text)[0], abs=0.01)


def test_a_gap_the_letter_admits_is_not_a_hit():
    """The reference reads CV + letter B as 82.5 because "I have not yet used LightGBM" and "not retail demand" count as hits;
    ats.py does not count them, so CV + B is 75.0, the same as CV + A (only 'cold start' is new in both)."""
    from careerops import ats

    kws, claimable = _load_keywords()
    ref = _reference()
    a, b = _letter_text("letter_A.tex"), _letter_text("letter_B.tex")
    assert ref.score(CV_TXT + "\n" + b)[0] == pytest.approx(82.5)
    sa, sb = ats.scores(CV_TXT, kws, claimable, a), ats.scores(CV_TXT, kws, claimable, b)
    assert sa.combined == pytest.approx(75.0, abs=0.1) and sb.combined == pytest.approx(75.0, abs=0.1)
    assert {"LightGBM", "retail", "demand"} <= set(sb.letter_negated) and sa.letter_negated == []
    assert sb.letter < ref.score(b)[0]                                  # the letter alone is lower too
    assert sb.strict == sa.strict == pytest.approx(72.5, abs=0.1)       # the CV number never moves with the letter


# ---------------------------------------------------------------- the pipeline's output for the Anaplan ad
def test_the_golden_diff_lists_lines_only_in_one_or_only_in_the_other():
    from careerops import golden

    a = "PROFILE\nData scientist who ships.\n  Bristol\n• Built a thing that\n  wraps over two lines.\n• Only here."
    b = "PROFILE\nData scientist who ships.\n• Built a thing that wraps over two lines.\n• Only there."
    only_a, only_b = golden.diff_lines(a, b)
    assert only_a == ["Bristol", "• Only here."] and only_b == ["• Only there."]
    assert "Lines only in the pipeline CV (2):" in golden.report(a, b) and "  - • Only there." in golden.report(a, b)
    assert golden.diff_lines(CV_TXT, CV_TXT) == ([], [])


@pytest.mark.slow
def test_the_pipeline_output_for_the_anaplan_ad_reaches_the_bar(settings, fb_fixture, tmp_path):
    """The whole pipeline on the golden ad with the fictional fact bank and MockLLM (the real bank is never read by a test): a two-page
    CV with the hook in all three places, letters A and B of at most 250 words, the three ATS numbers and the note."""
    from pathlib import Path

    from careerops import hook as hooks
    from careerops.llm import MockLLM
    from careerops.tailoring import tailor
    from careerops.tailoring.render import pdf_text

    settings.raw["letter"]["mode"] = "hook"
    job = {"id": "greenhouse:anaplan:8654662002", "source": "greenhouse", "company": "Anaplan",
           "title": "Data Scientist, Functional Analytics (REQ 26490)", "location": "London, UK", "url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002",
           "apply_url": "https://job-boards.greenhouse.io/anaplan/jobs/8654662002", "description": (GOLDEN / "jd.txt").read_text(encoding="utf-8"),
           "posted_at": "2026-08-07T09:00:00Z", "posted_at_source": "greenhouse.first_published"}
    rep = tailor.tailor_job(job, fb_fixture, MockLLM(), settings, settings.path("outputs"))
    assert rep["cv_pages"] == 2 and page_count(Path(rep["cv_pdf"])) == 2
    hk = hooks.Hook.from_dict(rep["hook"])
    assert rep["hook_placed"] == {"headline": True, "profile": True, "bullet": True, "number_in_pdf": True} and len(hk.headline) <= HEADLINE_MAX
    letter = rep["letters"]["A"]                                              # B is not made any more: no model writes a gap sentence
    assert letter["status"] == "ok" and len(" ".join(letter["paragraphs"]).split()) <= 250, (letter["reason"], letter["problems"])
    assert letter["paragraphs"][0] == hk.letter_sentence and rep["letters"]["B"]["status"] == "none"
    a = rep["ats"]
    assert {"strict", "ceiling", "combined", "letter"} <= set(a) and all(a[k] is not None for k in ("strict", "ceiling", "combined", "letter"))
    assert a["gate"] in ("ok", "above", "retry", "stretch") and (Path(rep["out_dir"]) / "note.md").is_file()
    assert "Posting age" in (Path(rep["out_dir"]) / "note.md").read_text(encoding="utf-8")
    from careerops import golden
    only_pipeline, only_golden = golden.diff_lines(pdf_text(Path(rep["cv_pdf"])), CV_TXT)      # (another fact bank: they differ; the lists exist)
    assert only_pipeline and only_golden
