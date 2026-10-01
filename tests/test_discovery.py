"""Discovery side: sponsor register, source parsers, rule gates."""
from careerops import gates, sources, sponsors


# ---------------------------------------------------------------- sponsors
def test_parse_filters_route_and_splits_trading_names(settings):
    rows = sponsors.parse(settings.path("sponsors_csv"), "Skilled Worker")
    norms = {r[0] for r in rows}
    assert "monzo bank" in norms
    assert {"acme analytics", "dataco"} <= norms          # T/A split
    assert not any("globex" in n for n in norms)          # GBM route excluded


def test_match_methods(db, settings):
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    idx = sponsors.SponsorIndex(db)
    assert idx.match("Northwind Health AI")[2] == "exact"
    assert idx.match("Northwind Health AI Ltd.")[2] == "exact"
    assert idx.match("DataCo")[0] == "Acme Analytics Limited T/A DataCo"
    name, score, method = idx.match("Monzo")
    assert (name, method) == ("Monzo Bank Ltd", "prefix")
    assert idx.match("Totally Unknown Startup")[2] == "none"
    # ambiguous prefix ('data lab' → 'the data lab' & 'data lab partners') must not guess
    assert idx.match("Data")[2] == "none"


def test_normalise():
    assert sponsors.normalise("The Data Lab Ltd") == "data lab"
    assert sponsors.normalise("Smith & Co (UK) Limited") == "smith and"


# ---------------------------------------------------------------- sources
def test_greenhouse_parser(monkeypatch):
    payload = {"jobs": [{"id": 7, "title": "Machine Learning Engineer", "location": {"name": "London"},
                         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
                         "content": "&lt;p&gt;Build &lt;b&gt;models&lt;/b&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
                         "updated_at": "2026-09-30T10:00:00Z"}]}
    monkeypatch.setattr(sources, "_get", lambda url, **kw: payload)
    (j,) = list(sources.greenhouse({"slug": "acme", "name": "Acme"}))
    assert j["id"] == "greenhouse:acme:7" and j["company"] == "Acme"
    assert "Build models" in j["description"] and "- Python" in j["description"]


def test_lever_parser(monkeypatch):
    payload = [{"id": "abc", "text": "Data Scientist", "categories": {"location": "Bristol"},
                "hostedUrl": "https://jobs.lever.co/x/abc", "applyUrl": "https://jobs.lever.co/x/abc/apply",
                "descriptionPlain": "Intro", "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
                "additionalPlain": "Visa sponsorship available", "createdAt": 1727700000000}]
    monkeypatch.setattr(sources, "_get", lambda url, **kw: payload)
    (j,) = list(sources.lever("x"))
    assert j["apply_url"].endswith("/apply")
    assert "Requirements" in j["description"] and "SQL" in j["description"]
    assert j["posted_at"].startswith("2024-09-30")


def test_fetch_all_survives_a_dead_board(monkeypatch, settings):
    settings.raw["sources"]["greenhouse"] = ["dead", "alive"]
    def fake(url, **kw):
        if "dead" in url:
            raise RuntimeError("404")
        return {"jobs": [{"id": 1, "title": "AI Engineer", "content": "x"}]}
    monkeypatch.setattr(sources, "_get", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    out = list(sources.fetch_all(settings))
    assert isinstance(out[0][1], Exception) and out[1][1]["title"] == "AI Engineer"


# ---------------------------------------------------------------- gates
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
