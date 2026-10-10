"""Rule gates."""
import pytest

from careerops.discovery import gates
from careerops.discovery import register as sponsors


def _idx(db, settings):
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    return sponsors.SponsorIndex(db)


def test_gate_passes_good_job(db, settings, job):
    v = gates.triage(job, settings, _idx(db, settings))
    assert v.passed, v.reasons
    assert v.sponsor_name == "Northwind Health AI Limited"


@pytest.mark.parametrize("sentence", [
    "Applicants must have the right to work in the UK without sponsorship.",
    "We do not offer visa sponsorship for this role.", "We cannot sponsor visas.", "No sponsorship is available.",
    "Candidates must not require sponsorship now or in the future.", "Unfortunately sponsorship is not available."])
def test_a_jd_that_says_no_sponsorship_is_still_eligible(db, settings, job, sentence):
    """Sponsorship is a weight (priority.py), never a filter: I hold the UK Graduate visa."""
    job["description"] += "\n" + sentence
    v = gates.triage(job, settings, _idx(db, settings))
    assert v.passed and v.reasons == [] and gates.verdict_status(v) == "triaged"
    assert not any("sponsor" in f.lower() for f in v.flags)


def test_no_gate_rule_mentions_sponsorship(settings):
    assert not [r for r in settings["gates"]["jd_rules"] if "sponsor" in r[0].lower() or "sponsor" in r[2].lower()]


def test_an_employer_off_the_register_and_an_agency_are_never_skipped(db, settings, job):
    idx = _idx(db, settings)
    for j in (dict(job, company="Not A Sponsor Ltd"), dict(job, company="Acme Recruitment Ltd", agency=1),
              dict(job, company="Not A Sponsor Ltd", description=job["description"] + "\nNo visa sponsorship.")):
        v = gates.triage(j, settings, idx)
        assert v.passed and gates.verdict_status(v) == "triaged"
    assert gates.triage(job, settings, None).passed                              # no register loaded at all


def test_gate_skips_sre_people_mgmt_and_senior(db, settings, job):
    idx = _idx(db, settings)
    j = dict(job, title="Senior AI Engineer")
    assert any("level excluded" in r for r in gates.triage(j, settings, idx).reasons)
    j = dict(job, description=job["description"] + "\nYou will have 3 direct reports.")
    assert not gates.triage(j, settings, idx).passed
    j = dict(job, description=job["description"] + "\nOwn site reliability for the platform.")
    assert not gates.triage(j, settings, idx).passed


def test_gate_flags_years_and_unsponsored_company(db, settings, job):
    idx = _idx(db, settings)
    j = dict(job, description=job["description"].replace("1+ years", "5+ years"))
    v = gates.triage(j, settings, idx)
    assert v.passed and any("5+ years" in f for f in v.flags)
    j = dict(job, company="Not A Sponsor Ltd")
    v = gates.triage(j, settings, idx)
    assert v.passed and gates.NOT_ON_REGISTER in v.flags               # flagged for the reviewer, never skipped


def test_truncated_jd_needs_full_text(db, settings, job):
    j = dict(job, source="adzuna", description=job["description"][:400])
    v = gates.triage(j, settings, _idx(db, settings))
    assert v.needs_full_jd


def test_years_regex():
    assert gates.min_years_required("We need 3-5 years of professional experience") == 3
    assert gates.min_years_required("2+ years' hands-on ML experience") == 2
    assert gates.min_years_required("founded 10 years ago") is None
