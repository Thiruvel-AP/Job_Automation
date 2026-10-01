"""Rule gates."""
from careerops.discovery import gates
from careerops.discovery import register as sponsors


def _idx(db, settings):
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    return sponsors.SponsorIndex(db)


def test_gate_passes_good_job(db, settings, job):
    v = gates.triage(job, settings, _idx(db, settings))
    assert v.passed, v.reasons
    assert v.sponsor_name == "Northwind Health AI Limited"


def test_gate_skips_no_sponsorship(db, settings, job):
    job["description"] += "\nApplicants must have the right to work in the UK without sponsorship."
    v = gates.triage(job, settings, _idx(db, settings))
    assert not v.passed and any("without sponsorship" in r for r in v.reasons)


def test_gate_skips_sre_people_mgmt_and_senior(db, settings, job):
    idx = _idx(db, settings)
    j = dict(job, title="Senior AI Engineer")
    assert any("title excluded" in r for r in gates.triage(j, settings, idx).reasons)
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
    assert not gates.triage(j, settings, idx).passed


def test_truncated_jd_needs_full_text(db, settings, job):
    j = dict(job, source="adzuna", description=job["description"][:400])
    v = gates.triage(j, settings, _idx(db, settings))
    assert v.needs_full_jd


def test_years_regex():
    assert gates.min_years_required("We need 3-5 years of professional experience") == 3
    assert gates.min_years_required("2+ years' hands-on ML experience") == 2
    assert gates.min_years_required("founded 10 years ago") is None
