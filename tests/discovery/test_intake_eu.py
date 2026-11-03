"""Greenhouse's EU region (job-boards.eu.greenhouse.io). RightShip's board lives there. v2.4.1 sent its link to a boards-api.eu host that does not exist; the
fix round after v2.5 reads it from the one API, boards-api.greenhouse.io, which answers for both regions (checked live on 3 Nov 2026). Saved replies only; no network."""
import json

import pytest
import requests
from test_intake import DIR, GH_URL, Resp, _cfg, web  # noqa: F401  (web is a fixture)

from careerops.apply import ats_vendor
from careerops.discovery import intake

EU_URL = "https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101"


@pytest.mark.parametrize("url", [EU_URL, "https://boards.eu.greenhouse.io/rightship/jobs/4982444101?gh_src=x"])
def test_the_eu_board_host_is_a_greenhouse_target_read_from_the_one_api(url):
    t = intake.parse_url(url)
    assert (t.kind, t.board, t.ad_id, t.api_host) == ("greenhouse", "rightship", "4982444101", "boards-api.greenhouse.io")


def test_the_us_host_keeps_the_us_api():
    assert intake.parse_url(GH_URL).api_host == "boards-api.greenhouse.io"


def test_the_embedded_eu_form_is_read_too():
    t = intake.parse_url("https://boards.eu.greenhouse.io/embed/job_app?for=rightship&token=4982444101")
    assert (t.kind, t.board, t.ad_id, t.api_host) == ("greenhouse", "rightship", "4982444101", "boards-api.greenhouse.io")


API = "https://boards-api.greenhouse.io/v1/boards/rightship/jobs/4982444101"


def test_an_eu_link_gives_the_company_and_title_from_the_job_api_not_the_page(web):
    """The bug: `live --url` on job-boards.eu.greenhouse.io asked a boards-api.eu host that does not resolve, fell back to the script-built page, and had no
    company or title. The job reply names both (RightShip's, as the live API answered on 3 Nov 2026)."""
    web.add("boards-api.greenhouse.io", "greenhouse_job_rightship.json")
    ad = intake.fetch_ad(EU_URL)
    assert web.urls == [API]                                        # one request, to the one API: no boards-api.eu host, no page
    assert (ad.company, ad.title, ad.location, ad.url) == ("RightShip", "AI Engineer", "London, UK", EU_URL)
    assert ad.source == "greenhouse" and ad.ad_id == "4982444101" and ad.posted.startswith("2026-10-29") and ad.text


def test_the_ad_of_the_eu_link_has_the_job_id_the_database_already_knows():
    """manual:ff68fffd015a is RightShip | AI Engineer | the EU link: a fetch that reads the right name gives back the same job."""
    from careerops.discovery import manual
    assert manual.job_id_for("RightShip", "AI Engineer", EU_URL) == "manual:ff68fffd015a"


def test_the_company_name_comes_from_the_board_api_when_the_job_reply_has_none(web):
    body = json.loads((DIR / "greenhouse_job_rightship.json").read_text(encoding="utf-8"))
    del body["company_name"]
    web.add("boards-api.greenhouse.io/v1/boards/rightship/jobs/", Resp(json.dumps(body)))
    web.add("boards-api.greenhouse.io/v1/boards/rightship", Resp(json.dumps({"name": "RightShip", "content": ""})))
    ad = intake.fetch_ad(EU_URL)
    assert ad.company == "RightShip"                                # the board's own spelling, not the slug "Rightship"
    assert web.urls == [API, "https://boards-api.greenhouse.io/v1/boards/rightship"]


def test_a_board_that_does_not_answer_leaves_the_slug_as_the_name(web):
    body = json.loads((DIR / "greenhouse_job_rightship.json").read_text(encoding="utf-8"))
    del body["company_name"]
    web.add("boards-api.greenhouse.io/v1/boards/rightship/jobs/", Resp(json.dumps(body)))
    web.add("boards-api.greenhouse.io/v1/boards/rightship", Resp("{}", 404))
    assert intake.fetch_ad(EU_URL).company == "Rightship"


def test_a_closed_eu_posting_is_an_error_and_the_boards_page_is_not_read_instead(web):
    web.add("boards-api.greenhouse.io", Resp("{}", 404))
    with pytest.raises(intake.IntakeError, match="HTTP 404"):
        intake.fetch_ad(EU_URL)
    assert web.urls == [API]                                        # no fallback to job-boards.eu.greenhouse.io/...


def test_a_disallow_or_a_block_is_never_worked_around(web):
    web.robots("boards-api.greenhouse.io", "User-agent: *\nDisallow: /\n", 200)
    with pytest.raises(intake.RobotsRefused):
        intake.fetch_ad(EU_URL)
    assert web.urls == []
    intake._default_robots.memory.clear()
    web.robots("boards-api.greenhouse.io", "", 404)
    web.add("boards-api.greenhouse.io", Resp("forbidden", 403))
    with pytest.raises(intake.BlockedError):
        intake.fetch_ad(EU_URL)
    assert EU_URL not in web.urls


def test_live_url_on_the_rightship_eu_link_reads_company_and_title(web, settings, monkeypatch, capsys):
    from careerops import cli
    _cfg(monkeypatch, settings)
    web.add("boards-api.greenhouse.io", "greenhouse_job_rightship.json")
    seen = {}

    class FakeLive:
        def __init__(self, db, s, hooks, **kw):
            seen.update(kw)

        def run(self):
            return 0
    monkeypatch.setattr("careerops.apply.live.Live", FakeLive)
    cli.main(["live", "--url", EU_URL])
    assert "(RightShip | AI Engineer)" in capsys.readouterr().out
    assert seen["only"].read_text(encoding="utf-8").splitlines()[:3] == ["Company: RightShip", "Title: AI Engineer", f"URL: {EU_URL}"]


def test_a_us_board_that_fails_is_still_an_error_and_a_refusal_is_never_worked_around(web):
    web.add("boards-api.greenhouse.io", Resp("{}", 404))
    with pytest.raises(intake.IntakeError, match="HTTP 404"):
        intake.fetch_ad(GH_URL)
    assert web.urls == ["https://boards-api.greenhouse.io/v1/boards/monzo/jobs/4412345"]


def test_a_job_you_already_approved_is_resumed_not_fetched_into_the_inbox_again(web, settings):
    web.add("boards-api.greenhouse.io", "greenhouse_job_rightship.json")
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
