"""v3.5.2: the v3.3 ATS structure line (score, grade, lints, title / years / degree) is on EVERY review screen, not only for a job in the review
band (it was missing from the 9 Nov 17:31 RightShip run). The check itself is faked; the job is the fixture job tailored with MockLLM."""
import pytest

from careerops.review import gaps, screeners
from careerops.review import workflow as review

LINE = "ATS structure 95/A (0 critical) | lints: none | title match: yes | years: experience 3+ met | degree met"


class _Check:
    def lines(self):
        return [LINE, "  WARNING x"]


def test_summary_prints_the_line_with_bank_when_fb_is_not_given(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(screeners, "check", lambda rep, s, fb, job: seen.append(fb) or _Check())
    assert review.ats_check_lines({}, settings, {"experience": []}, {}) == [LINE, "  WARNING x"]
    assert review.ats_check_lines({}, settings, None, {}) == []                       # nothing to check against: nothing printed
    assert seen == [{"experience": []}]


@pytest.mark.slow
@pytest.mark.parametrize("in_band", [False, True])
def test_the_review_screen_shows_the_ats_structure_line_in_and_out_of_the_review_band(tailored, settings, db, job, monkeypatch, capsys, in_band):
    _, fb, _ = tailored
    monkeypatch.setattr(screeners, "check", lambda rep, s, bank, j: _Check() if bank else pytest.fail("checked without the fact bank"))
    monkeypatch.setattr(gaps, "applies", lambda rep, band: in_band)
    answers = iter(["s"])
    assert review.review_job(db, db.job(job["id"]), settings, fb, ask=lambda p="": next(answers)) in ("skipped", "skipped (user)")
    out = capsys.readouterr().out
    assert LINE in out
