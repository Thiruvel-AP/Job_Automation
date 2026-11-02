"""v2.4.1: Greenhouse's EU region (job-boards.eu.greenhouse.io, boards-api.eu.greenhouse.io). RightShip's board lives there; the US-only host list sent its link
down the generic page path (no job API, no first_published date). Saved fixture replies only; no network."""
import pytest
import requests
from test_intake import DIR, GH_URL, Resp, web  # noqa: F401  (web is a fixture)

from careerops.apply import ats_vendor
from careerops.discovery import intake

EU_URL = "https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101"


@pytest.mark.parametrize("url", [EU_URL, "https://boards.eu.greenhouse.io/rightship/jobs/4982444101?gh_src=x"])
def test_the_eu_board_host_is_a_greenhouse_target_with_the_eu_api(url):
    t = intake.parse_url(url)
    assert (t.kind, t.board, t.ad_id, t.api_host) == ("greenhouse", "rightship", "4982444101", "boards-api.eu.greenhouse.io")


def test_the_us_host_keeps_the_us_api():
    assert intake.parse_url(GH_URL).api_host == "boards-api.greenhouse.io"


def test_the_embedded_eu_form_is_read_too():
    t = intake.parse_url("https://boards.eu.greenhouse.io/embed/job_app?for=rightship&token=4982444101")
    assert (t.kind, t.board, t.ad_id, t.api_host) == ("greenhouse", "rightship", "4982444101", "boards-api.eu.greenhouse.io")


def test_the_jd_is_fetched_from_the_eu_api_not_the_us_one(web):
    web.add("boards-api.eu.greenhouse.io", "greenhouse_job.json")
    ad = intake.fetch_ad(EU_URL)
    assert web.urls == ["https://boards-api.eu.greenhouse.io/v1/boards/rightship/jobs/4982444101"]
    assert ad.source == "greenhouse" and ad.ad_id == "4982444101" and ad.text


def test_an_eu_api_that_does_not_answer_falls_back_to_the_boards_own_page(web):
    web.add("boards-api.eu.greenhouse.io", Resp("{}", 404))
    web.add("job-boards.eu.greenhouse.io/rightship", Resp("<html><head><title>AI Engineer - RightShip</title></head><body><main><h1>AI Engineer</h1>"
                                                           + "<p>" + "Build and evaluate LLM agents for maritime safety. " * 80 + "</p></main></body></html>"))
    ad = intake.fetch_ad(EU_URL)
    assert web.urls[0].startswith("https://boards-api.eu.greenhouse.io/") and web.urls[-1] == EU_URL
    assert len(ad.text) > 2000


def test_an_eu_api_host_that_cannot_be_reached_at_all_falls_back_but_a_disallow_does_not(web, monkeypatch):
    page = Resp("<html><head><title>AI Engineer - RightShip</title></head><body><main><h1>AI Engineer</h1><p>" + "Build LLM agents for maritime safety. " * 90
                + "</p></main></body></html>")
    web.add("job-boards.eu.greenhouse.io/rightship", page)
    real = intake._get

    def get(url, **kw):
        if "boards-api.eu.greenhouse.io" in url:
            raise requests.ConnectionError("name not resolved")    # the host does not exist / cannot be reached
        return real(url, **kw)
    monkeypatch.setattr(intake, "_get", get)
    assert len(intake.fetch_ad(EU_URL).text) > 2000 and web.urls[-1] == EU_URL
    monkeypatch.setattr(intake, "_get", real)
    intake._default_robots.memory.clear()
    web.robots("boards-api.eu.greenhouse.io", "User-agent: *\nDisallow: /\n", 200)                 # it answers, and it disallows
    web.urls.clear()
    with pytest.raises(intake.RobotsRefused):
        intake.fetch_ad(EU_URL)
    assert EU_URL not in web.urls


def test_a_us_board_that_fails_is_still_an_error_and_a_refusal_is_never_worked_around(web):
    web.add("boards-api.greenhouse.io", Resp("{}", 404))
    with pytest.raises(intake.IntakeError, match="HTTP 404"):
        intake.fetch_ad(GH_URL)
    assert web.urls == ["https://boards-api.greenhouse.io/v1/boards/monzo/jobs/4412345"]
    web2 = web
    web2.add("boards-api.eu.greenhouse.io", Resp("forbidden", 403))
    with pytest.raises(intake.BlockedError):
        intake.fetch_ad(EU_URL)
    assert not any(u == EU_URL for u in web2.urls)


def test_a_job_you_already_approved_is_resumed_not_fetched_into_the_inbox_again(web, settings):
    web.add("boards-api.eu.greenhouse.io", "greenhouse_job.json")
    path, created, ad = intake.add_url(settings, EU_URL)
    inbox_copy = path
    assert created and inbox_copy.parent.name == "inbox"
    day = settings.path("jds") / "approved" / "2026-10-31"
    day.mkdir(parents=True)
    approved = day / inbox_copy.name
    approved.write_bytes(inbox_copy.read_bytes())
    inbox_copy.unlink()                                                        # the ad moved to approved/ when you approved it
    again, created2, _ = intake.add_url(settings, EU_URL)
    assert again == approved and created2 is False and not list((settings.path("jds") / "inbox").glob("*.txt"))


def test_an_eu_board_address_is_not_rewritten_to_the_us_host():
    assert ats_vendor.vendor_of(EU_URL) == "greenhouse"
    assert ats_vendor.direct_url(EU_URL, kind="greenhouse") == EU_URL
    assert ats_vendor.direct_url("https://boards.eu.greenhouse.io/embed/job_app?for=rightship&token=4982444101", kind="greenhouse") == EU_URL
