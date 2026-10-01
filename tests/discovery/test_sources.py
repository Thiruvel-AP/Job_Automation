"""Job-source parsers on recorded payload shapes."""
from careerops.discovery import sources


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
