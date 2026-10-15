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
