"""v2.5 apply/listing_keys.py: the ATS-native triple, the URL key (only a narrow list of tracking parameters stripped) and the fingerprint."""
import pytest

from careerops.apply import listing_keys as LK


@pytest.mark.parametrize("url,key", [
    ("https://job-boards.greenhouse.io/anthropic/jobs/5423029008", "ats:greenhouse:anthropic:5423029008"),
    ("https://job-boards.eu.greenhouse.io/rightship/jobs/4982444101?gh_src=x", "ats:greenhouse:rightship:4982444101"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", "ats:greenhouse:acme:123"),
    ("https://jobs.lever.co/valarian/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab/apply", "ats:lever:valarian:0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"),
    ("https://jobs.ashbyhq.com/elevenlabs/5c2d1f0e-1111-2222-3333-444455556666", "ats:ashby:elevenlabs:5c2d1f0e-1111-2222-3333-444455556666"),
    ("https://acme.example/careers/1", ""),
])
def test_the_ats_key_is_the_vendor_board_and_job_id(url, key):
    assert LK.ats_key(url) == key


def test_the_ats_key_of_a_database_id():
    assert LK.ats_from_id("greenhouse:anaplan:8654662002") == "ats:greenhouse:anaplan:8654662002"
    assert LK.ats_from_id("manual:69406a1aef2d") == ""


def test_a_url_differing_only_in_utm_source_has_the_same_key():
    a = LK.url_key("https://job-boards.greenhouse.io/anthropic/jobs/5423029008")
    assert a == LK.url_key("http://Job-Boards.Greenhouse.io/anthropic/jobs/5423029008/?utm_source=linkedin&utm_medium=x#apply")
    assert a == LK.url_key("https://job-boards.greenhouse.io/anthropic/jobs/5423029008?gclid=1&fbclid=2&gh_src=3&lever-source=4")


def test_a_url_differing_in_gh_jid_has_another_key():
    assert LK.url_key("https://acme.example/careers?gh_jid=123") != LK.url_key("https://acme.example/careers?gh_jid=456")
    assert LK.url_key("https://acme.example/careers?b=2&a=1") == LK.url_key("https://acme.example/careers?a=1&b=2")     # sorted, kept


def test_the_eu_host_is_kept():
    assert LK.url_key("https://job-boards.eu.greenhouse.io/rightship/jobs/1") != LK.url_key("https://job-boards.greenhouse.io/rightship/jobs/1")


def test_the_fingerprint_is_family_title_and_country():
    assert LK.fingerprint("Anthropic", "Forward Deployed Engineer", "London, UK") == "anthropic|forward deployed engineer|GB"
    assert LK.fingerprint("Anthropic", "Forward Deployed Engineer", "Munich, Germany").endswith("|DE")
    assert LK.fingerprint("", "AI Engineer", "UK") == ""


def test_only_the_ats_and_url_keys_are_strong():
    k = LK.keys_for("Anthropic", "Forward Deployed Engineer", "https://job-boards.greenhouse.io/anthropic/jobs/5423029008", None, "London, UK")
    assert k.strong() == ["ats:greenhouse:anthropic:5423029008", "https://job-boards.greenhouse.io/anthropic/jobs/5423029008"]
    assert k.fingerprint and k.fingerprint not in k.strong()
