"""Which applicant-tracking system a host belongs to, and the address of its real application form (apply/ats_vendor.py). No browser, no network."""
import pytest

from careerops.apply import ats_vendor, formfind

EPIC = "https://www.epicgames.com/site/en-US/careers/jobs/5678901004?gh_jid=5678901004&utm_source=x"


@pytest.mark.parametrize("url,vendor", [
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/acme/99ab", "lever"),
    ("https://jobs.eu.lever.co/acme/99ab", "lever"),
    ("https://jobs.ashbyhq.com/acme/1234", "ashby"),
    ("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/x_R1", "workday"),
    ("https://wd5.myworkdaysite.com/recruiting/acme/jobs", "workday"),
    ("https://acme.icims.com/jobs/12/x/job", "icims"),
    ("https://career5.successfactors.com/career?company=x", "successfactors"),
    ("https://career5.successfactors.eu/career?company=x", "successfactors"),
    ("https://acme.jobs2web.com/job/London-Data-Scientist/123/", "successfactors"),
    ("https://jobs.smartrecruiters.com/Acme/1-x", "smartrecruiters"),
    ("https://apply.workable.com/acme/j/ABC/", "workable"),
    ("https://acme.bamboohr.com/careers/12", "bamboohr"),
    ("https://acme.taleo.net/careersection/2/jobdetail.ftl", "taleo"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1", "oracle"),
    ("https://eeho.fa.em3.oraclecloud.com/x", "oracle"),
])
def test_a_host_maps_to_its_vendor(url, vendor):
    assert ats_vendor.vendor_of(url) == vendor


def test_any_other_host_returns_the_host_itself():
    assert ats_vendor.vendor_of("https://careers.example.com/jobs/1") == "careers.example.com"
    assert ats_vendor.vendor_of("careers.example.com") == "careers.example.com"
    assert ats_vendor.vendor_of("") == "" and ats_vendor.vendor_of(None) == ""


def test_a_lookalike_host_is_not_a_vendor():
    assert ats_vendor.vendor_of("https://notgreenhouse.io/x") == "notgreenhouse.io"
    assert ats_vendor.vendor_of("https://greenhouse.io.evil.example/x") == "greenhouse.io.evil.example"
    assert ats_vendor.vendor_of("https://myworkdayjobs.com.evil.example/x") == "myworkdayjobs.com.evil.example"


def test_jobs2web_gives_successfactors():
    assert ats_vendor.vendor_of("https://jobs2web.com/x") == "successfactors"


def test_an_epic_style_greenhouse_job_on_a_company_domain_resolves_to_the_job_boards_url_from_the_job_board():
    job = {"board": "greenhouse:epicgames", "apply_url": EPIC}
    assert ats_vendor.direct_url(EPIC, job=job) == "https://job-boards.greenhouse.io/epicgames/jobs/5678901004"


def test_the_token_can_be_read_from_a_board_postings_id():
    assert ats_vendor.direct_url(EPIC, job={"id": "greenhouse:epicgames:5678901004"}) == "https://job-boards.greenhouse.io/epicgames/jobs/5678901004"
    assert ats_vendor.direct_url(EPIC, job={"id": "lever:epicgames:5678901004"}) == EPIC
    assert ats_vendor.direct_url(EPIC, job={"id": "manual:abc"}) == EPIC


def test_the_token_can_also_be_a_known_board_that_the_company_domain_names():
    assert ats_vendor.direct_url(EPIC, tokens=["monzo", "epicgames"]) == "https://job-boards.greenhouse.io/epicgames/jobs/5678901004"


def test_the_token_can_be_the_company_name_when_it_is_a_known_board():
    assert ats_vendor.direct_url("https://careers.example.com/open?gh_jid=77", job={"company": "Epic Games"}, tokens=["epicgames"]) == \
        "https://job-boards.greenhouse.io/epicgames/jobs/77"


def test_without_a_token_the_page_is_left_as_it_is():
    assert ats_vendor.direct_url(EPIC) == EPIC
    assert ats_vendor.direct_url(EPIC, tokens=["monzo"]) == EPIC


def test_a_board_that_is_not_greenhouse_is_not_used_as_a_greenhouse_token():
    assert ats_vendor.direct_url(EPIC, job={"board": "lever:epicgames"}) == EPIC


def test_a_greenhouse_board_address_stays_as_it_is_and_gh_jid_is_ignored_there():
    assert ats_vendor.direct_url("https://boards.greenhouse.io/acme/jobs/1") == "https://boards.greenhouse.io/acme/jobs/1"
    assert ats_vendor.direct_url("https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x") == "https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x"


def test_an_embed_address_is_resolved_to_its_board():
    assert ats_vendor.direct_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=123") == "https://job-boards.greenhouse.io/acme/jobs/123"


def test_lever_opens_apply_and_ashby_application():
    assert ats_vendor.direct_url("https://jobs.lever.co/acme/99ab") == "https://jobs.lever.co/acme/99ab/apply"
    assert ats_vendor.direct_url("https://jobs.lever.co/acme/99ab/apply") == "https://jobs.lever.co/acme/99ab/apply"
    assert ats_vendor.direct_url("https://jobs.ashbyhq.com/acme/1-2/") == "https://jobs.ashbyhq.com/acme/1-2/application"
    assert ats_vendor.direct_url("https://jobs.ashbyhq.com/acme/1-2/application") == "https://jobs.ashbyhq.com/acme/1-2/application"


def test_the_recorded_apply_kind_beats_the_host_for_lever_and_ashby():
    assert ats_vendor.direct_url("https://careers.example.com/j/9", kind="lever") == "https://careers.example.com/j/9/apply"
    assert ats_vendor.direct_url("https://careers.example.com/j/9", kind="ashby") == "https://careers.example.com/j/9/application"


def test_any_other_vendor_is_left_alone():
    for u in ("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/x_R1", "https://careers.example.com/jobs/1", "", None):
        assert ats_vendor.direct_url(u) == (u or "")


def test_formfind_opens_the_direct_address():
    assert formfind.form_url(EPIC, None, {"board": "greenhouse:epicgames"}) == "https://job-boards.greenhouse.io/epicgames/jobs/5678901004"
    assert formfind.form_url("https://jobs.lever.co/acme/99ab") == "https://jobs.lever.co/acme/99ab/apply"
