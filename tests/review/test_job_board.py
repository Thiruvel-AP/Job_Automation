"""Jobs that came from a job board or recruitment agency are flagged on the review screen: the sponsor register match on
them may name the agency, not the employer."""
import pytest

from careerops.review import workflow as review


@pytest.mark.parametrize("url, board", [
    ("https://www.cv-library.co.uk/job/12345/data-scientist", "cvlibrary"),
    ("https://www.reed.co.uk/jobs/data-scientist/5551", "reed"),
    ("https://www.totaljobs.com/job/data-scientist/x-job1", "totaljobs"),
    ("https://uk.indeed.com/viewjob?jk=abc", "indeed"),
    ("https://www.linkedin.com/jobs/view/3999", "linkedin"),
    ("https://jobs.lever.co/northwind/0f3b2c1d", None),
    ("https://careers.freedomfinance.com/jobs/1", None),               # 'reed' inside another word is not Reed
    ("https://example.com/indeed", None),                              # only the host counts
    ("", None),
])
def test_job_board_is_judged_by_the_host(url, board):
    assert review.job_board({"url": url, "apply_url": None, "source": "manual"}) == board


def test_the_apply_url_and_the_source_name_count_too():
    assert review.job_board({"url": "https://acme.example/jobs/1", "apply_url": "https://www.reed.co.uk/x", "source": "adzuna"}) == "reed"
    assert review.job_board({"url": None, "apply_url": None, "source": "CV-Library"}) == "cvlibrary"


@pytest.mark.slow
def test_the_review_screen_carries_the_board_warning_only_for_a_board_job(tailored, db, job):
    rep, _, _ = tailored
    assert review.BOARD_BANNER not in review.summary(db.job(job["id"]), rep)
    db.update_job(job["id"], url="https://www.totaljobs.com/job/1", apply_url="https://www.totaljobs.com/job/1")
    text = review.summary(db.job(job["id"]), rep)
    assert "agency/job board — sponsor check may refer to the agency, apply by hand on the site" in text
