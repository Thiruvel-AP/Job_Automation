"""Region targeting (discovery/region.py): which places count for the region you chose. Fixtures only, no network, no model."""
import pytest

from careerops.discovery import region as R

NEUTRAL = "We build forecasting tools for scientists. A friendly team, flexible hours and a quiet office."


# ------------------------------------------------------------------ the spec's cases
@pytest.mark.parametrize("location,body", [
    ("London, England", ""), ("Remote - UK", ""), ("Bristol, United Kingdom", ""), ("GB", ""), ("GBR", ""), ("London or New York", ""),
    ("Remote", "You must have the right to work in the UK."),
    ("Remote, UK", ""), ("Remote (UK)", ""), ("London, GB", ""), ("Newcastle upon Tyne", ""), ("Edinburgh", ""),
    ("Remote", "This role is UK-based, with some travel."), ("Remote", "We work in the UK time zone."),
    ("", "Candidates must be located in the United Kingdom."), ("Remote", "Open to UK residents only."), ("Remote", "This role is remote within the UK only."),
])
def test_places_in_the_uk(location, body):
    m = R.match(location, body, "uk")
    assert m.verdict == R.IN and m.evidence


@pytest.mark.parametrize("location,body", [
    ("Cambridge, MA", ""), ("London, Ontario", ""), ("Birmingham, AL", ""), ("Perth, WA", ""), ("Bengaluru", ""), ("Berlin", ""),
    ("Remote", "US-based candidates only. " + NEUTRAL), ("Remote", "You must be based in the US. " + NEUTRAL),
    ("Remote - EMEA", "EU work authorisation required. " + NEUTRAL), ("Remote", "Remote within the United States only. " + NEUTRAL),
    ("Remote", "Available in Canada only."),
    ("Dublin, OH", ""), ("Manchester, NH", ""), ("Bristol, CT", ""), ("Dublin", ""), ("New York, NY", ""),
])
def test_places_outside_the_uk(location, body):
    m = R.match(location, body, "uk")
    assert m.verdict == R.OUT and m.evidence


@pytest.mark.parametrize("location,body", [("Remote", NEUTRAL), ("", NEUTRAL), ("Europe", NEUTRAL), ("Anywhere", "")])
def test_a_place_that_does_not_say_with_a_neutral_ad_is_unknown_and_kept(location, body):
    m = R.match(location, body, "uk")
    assert m.verdict == R.UNKNOWN and m.flag == "region?"


def test_conflicting_statements_in_the_ad_are_unknown_not_a_guess():
    m = R.match("Remote", "You need the right to work in the UK. US-based candidates only.", "uk")
    assert m.verdict == R.UNKNOWN and "both" in m.evidence


# ------------------------------------------------------------------ word boundaries
def test_india_does_not_match_indianapolis_and_newcastle_matches_newcastle_upon_tyne():
    assert R.match("Indianapolis, IN", "", "in").verdict == R.OUT                       # `in` = India (ad-hoc country region); IN here is Indiana
    assert R.match("Indianapolis", "", "in").verdict == R.OUT
    assert R.match("India", "", "in").verdict == R.IN
    assert R.match("Bangalore, IN", "", "in").verdict == R.IN                           # ", IN" after a city of India is India
    assert R._phrase("Newcastle").search("Newcastle upon Tyne") and R._phrase("India").search("Indianapolis") is None
    assert R.match("Newcastle upon Tyne", "", "uk").verdict == R.IN


def test_a_short_capitalised_abbreviation_must_be_capitalised():
    assert R._phrase("UK").search("Remote UK") and R._phrase("UK").search("stuck in a rut") is None and R._phrase("UK").search("uk") is None


def test_dublin_ohio_is_not_dropped_for_us_and_not_taken_for_ireland():
    assert R.match("Dublin, OH", "", "us").verdict == R.IN
    assert R.match("Dublin, OH", "", "ie").verdict == R.OUT
    assert R.match("Dublin", "", "ie").verdict == R.IN
    assert R.match("Dublin, Ireland", "", "ie").verdict == R.IN
    assert R.match("Toronto, CA", "", "us").verdict == R.OUT                           # ", CA" after a city of Canada is Canada, not California
    assert R.match("Mountain View, CA", "", "us").verdict == R.IN


# ------------------------------------------------------------------ ISO codes, several regions, any
def test_iso_codes_become_country_names_before_matching():
    assert R.expand_iso("GB") == "United Kingdom" and R.expand_iso("GBR") == "United Kingdom"
    assert R.expand_iso("London, GBR") == "London, United Kingdom"
    assert R.expand_iso("Remote - DE") == "Remote - Germany" and R.expand_iso("Remote, CA") == "Remote, Canada"
    assert R.expand_iso("Birmingham, AL") == "Birmingham, AL" and R.expand_iso("Dublin, OH") == "Dublin, OH"        # a US state is not a country


def test_a_source_given_country_rides_with_the_place():
    assert R.match("Hull", "", "uk").verdict == R.OUT                                  # a town the lists do not know
    assert R.match("Hull", "", "uk", country="GB").verdict == R.IN
    assert R.match("", "", "uk", country="GBR").verdict == R.IN


def test_several_regions_pass_a_job_that_passes_any_one_of_them():
    assert R.match("Dublin", "", ("uk", "ie")).verdict == R.IN and R.match("Dublin", "", "uk").verdict == R.OUT
    assert R.match("Berlin", "", "eu").verdict == R.IN and R.match("Dublin, Ireland", "", "eu").verdict == R.IN
    assert R.match("Berlin", "", ("uk", "ie")).verdict == R.OUT


def test_any_turns_the_filter_off():
    assert R.match("Bengaluru", "", "any").verdict == R.IN and R.match("", "", "all").verdict == R.IN


def test_the_selection_is_validated():
    assert R.parse_selection("uk, ie") == ("uk", "ie") and R.parse_selection("UK,uk") == ("uk",) and R.parse_selection("all") == ("any",)
    assert R.parse_selection("de") == ("de",) and R.match("Munich", "", "de").verdict == R.IN
    with pytest.raises(R.RegionError, match="unknown region"):
        R.parse_selection("narnia")
    with pytest.raises(R.RegionError, match="no region"):
        R.parse_selection(" ,")


def test_a_group_such_as_emea_is_ambiguous_for_the_uk_but_the_eu_names_itself_for_the_eu():
    assert R.match("Remote - EMEA", "", "uk").verdict == R.UNKNOWN
    assert R.match("Remote - EU", "", "eu").verdict == R.IN


def test_region_of_names_the_region_for_the_reports():
    assert [R.region_of({"location": p, "description": NEUTRAL}) for p in ("London, UK", "Dublin", "Berlin", "New York, NY", "Bengaluru", "Remote")] == \
        ["uk", "ie", "eu", "us", "other", "unknown"]


# ------------------------------------------------------------------ sources
def test_a_source_runs_when_one_of_its_regions_is_selected_and_no_tag_means_any():
    assert R.source_runs((), ("uk",)) and R.source_runs(("any",), ("uk",)) and R.source_runs(("au",), ("any",))
    assert not R.source_runs(("au",), ("uk",)) and R.source_runs(("au",), ("au",)) and R.source_runs(("uk", "au"), ("uk",))
    assert R.source_runs(("de",), ("eu",)) and not R.source_runs(("de",), ("uk", "ie")) and R.source_runs(("ie",), ("uk", "ie"))


def test_the_regions_lines_to_add_to_sources_yaml_are_suggested_never_written():
    assert R.suggested_tag("GB") == ["uk"] and R.suggested_tag("IE") == ["ie"] and R.suggested_tag("AU") == ["au"] and R.suggested_tag("multi") == ["any"]
    from careerops.discovery import registry
    reg = registry.parse({"sources": [
        {"id": "seek-au", "name": "Seek", "url": "https://x.example", "country": "AU", "method": "gmail_alerts", "domains": ["seek.com.au"]},
        {"id": "tagged", "name": "T", "url": "https://x.example", "country": "GB", "method": "gmail_alerts", "domains": ["t.example.com"], "regions": ["uk"]}]})
    assert R.untagged_lines(reg) == ["  seek-au: regions: [au]"]
    assert reg.get("seek-au").region_tags == ("any",) and reg.get("tagged").region_tags == ("uk",)


# ------------------------------------------------------------------ the queue: parked, never skipped
def _job(db, jid, location, status="triaged", description=NEUTRAL):
    db.upsert_job({"id": jid, "source": "manual", "company": "Co " + jid, "title": "Data Scientist", "location": location, "description": description})
    db.update_job(jid, status=status)


def test_the_queue_migration_parks_out_of_region_jobs_and_never_skips_them(db):
    _job(db, "a", "London, UK")
    _job(db, "b", "New York, NY")
    _job(db, "c", "Remote")
    _job(db, "d", "New York, NY", status="applied")                 # a job in hand is never touched
    _job(db, "e", "Berlin", status="skipped")                       # a skip stays permanent
    st = R.park_queue(db, ("uk",))
    assert (st.parked, st.back, st.unknown, st.kept) == (1, 0, 1, 1)
    assert db.job("b")["status"] == "parked (region)" and db.job("b")["parked_from"] == "triaged"
    assert [db.job(j)["status"] for j in "acde"] == ["triaged", "triaged", "applied", "skipped"]
    assert db.job("c")["region_match"] == "unknown" and db.job("a")["region_match"] == "in" and "New York" in db.job("b")["region_evidence"]
    assert R.park_queue(db, ("uk",)).parked == 1 and db.job("b")["parked_from"] == "triaged"          # again: nothing moves twice


def test_switching_the_region_brings_the_parked_jobs_back_to_their_old_status(db):
    _job(db, "b", "New York, NY", status="needs_jd")
    R.park_queue(db, ("uk",))
    assert db.job("b")["status"] == "parked (region)"
    st = R.park_queue(db, ("any",))
    assert st.back == 1 and db.job("b")["status"] == "needs_jd" and db.job("b")["parked_from"] is None
    R.park_queue(db, ("uk",))
    R.park_queue(db, ("us",))
    assert db.job("b")["status"] == "needs_jd"
