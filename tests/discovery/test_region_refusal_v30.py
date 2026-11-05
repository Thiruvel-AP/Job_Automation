"""v3.0 (D4): an EU job whose ad says it does not sponsor is parked, never skipped; a UK job that says so keeps the lower weight. No network."""
from careerops.discovery import region

# ------------------------------------------------------------------ an EU job that refuses sponsorship is parked
NO_SPONSOR = "We are a small team. We do not offer visa sponsorship for this role."


def test_an_eu_job_that_refuses_sponsorship_is_parked_with_the_sentence_as_evidence():
    m = region.match("Berlin, Germany", NO_SPONSOR, ["eu"])
    assert m.verdict == region.OUT and "EU job that refuses sponsorship" in m.evidence and "do not offer visa sponsorship" in m.evidence
    assert region.job_match({"location": "Dublin, Ireland", "description": NO_SPONSOR}, ["eu"]).verdict == region.OUT        # Ireland is in the EU
    assert region.match("Amsterdam, Netherlands", NO_SPONSOR, ["eu", "uk"]).verdict == region.OUT


def test_an_eu_job_that_sponsors_or_says_nothing_is_kept():
    assert region.match("Berlin, Germany", "Visa sponsorship is available.", ["eu"]).verdict == region.IN
    assert region.match("Berlin, Germany", "Join our team.", ["eu"]).verdict == region.IN
    assert region.match("Berlin, Germany", "", ["eu"]).verdict == region.IN


def test_a_uk_job_that_refuses_sponsorship_is_not_parked_it_only_loses_weight():
    assert region.match("London, UK", NO_SPONSOR, ["uk"]).verdict == region.IN
    assert region.match("London, UK", NO_SPONSOR, ["uk", "eu"]).verdict == region.IN
    from careerops.discovery import priority
    assert priority.says_no_sponsorship(NO_SPONSOR)                                  # the UK rule: the weight (priority.sponsor_weights), unchanged


def test_a_job_in_the_selected_region_but_not_the_eu_is_untouched():
    assert region.match("New York, USA", NO_SPONSOR, ["us"]).verdict == region.IN
    assert region.match("Berlin, Germany", NO_SPONSOR, ["uk"]).verdict == region.OUT            # out of region anyway: parked for the old reason


def _job(db, jid, location, description):
    db.upsert_job({"id": jid, "source": "manual", "company": "Co " + jid, "title": "Data Scientist", "location": location, "description": description})
    db.update_job(jid, status="triaged")


def test_the_parked_queue_holds_the_eu_refusal_never_skips_it_and_brings_it_back_when_the_ad_changes(db):
    _job(db, "a", "Berlin, Germany", NO_SPONSOR)
    _job(db, "b", "Berlin, Germany", "Join our team. Visa sponsorship is available.")
    _job(db, "c", "London, UK", NO_SPONSOR)
    st = region.park_queue(db, ["eu", "uk"])
    assert (st.parked, st.kept) == (1, 2)
    assert db.job("a")["status"] == "parked (region)" and db.job("a")["parked_from"] == "triaged" and "refuses sponsorship" in db.job("a")["region_evidence"]
    assert db.job("b")["status"] == "triaged" and db.job("c")["status"] == "triaged"             # the UK job that says "no sponsorship" is not parked
    db.update_job("a", description="We sponsor visas for the right people.")                       # the ad now sponsors: back to where it was
    assert region.park_queue(db, ["eu", "uk"]).back == 1 and db.job("a")["status"] == "triaged"
