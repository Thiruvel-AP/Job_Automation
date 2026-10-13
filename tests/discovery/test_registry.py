"""The source registry (config/sources.yaml, discovery/registry.py): the shipped file, validation, matching alert emails, and which
source a found job counts for. No network."""
import shutil
from pathlib import Path

import pytest
import yaml

from careerops.config import ROOT
from careerops.discovery import countries, registry
from careerops.discovery.candidate import Candidate
from careerops.discovery.registry import Registry, RegistryError, Source

SHIPPED = ROOT / "config" / "sources.yaml"

# the sources the registry was asked to seed, by the name the health table shows
ASKED = ["LinkedIn", "CV-Library", "AIApply", "Greenhouse boards", "Indeed UK", "Glassdoor UK", "Welcome to the Jungle", "Wellfound",
         "YC Work at a Startup", "EURES", "StepStone DE", "StepStone NL", "IrishJobs", "Job Bank Canada", "SEEK AU", "SEEK NZ",
         "MyCareersFuture", "GOV.UK Find a Job", "NHS Jobs", "OpenAI", "Google DeepMind", "BlackRock", "JPMorgan Chase", "Reed"]


@pytest.fixture
def shipped(settings) -> Registry:
    shutil.copy(SHIPPED, Path(settings.raw["paths"]["sources"]))
    return registry.load(settings)


def entry(**kw) -> dict:
    return {"id": "x", "name": "X", "url": "https://x.example", "country": "GB", "method": "gmail_alerts", "domains": ["x.example"], **kw}


def parse(*entries) -> Registry:
    return registry.parse({"sources": list(entries)})


# ------------------------------------------------------------------ the shipped file
def test_the_shipped_registry_has_every_asked_for_source_once(shipped):
    assert [s.name for s in shipped] == ASKED
    assert len({s.id for s in shipped}) == len(shipped)


def test_every_entry_has_the_asked_for_fields_and_a_known_method(shipped):
    for s in shipped:
        assert s.id and s.name and s.url.startswith("https://") and s.country and s.method in registry.METHODS
        assert s.country.lower() == "multi" or countries.canon(s.country), s.id


def test_methods_are_the_ones_the_task_names(shipped):
    by = {s.name: s.method for s in shipped}
    assert by["AIApply"] == "dedupe_only" and by["Job Bank Canada"] == "jobbank_csv" and by["Reed"] == "reed"
    assert by["Greenhouse boards"] == "greenhouse" and by["OpenAI"] == "ashby"
    assert all(by[n] == "gmail_alerts" for n in ("LinkedIn", "CV-Library", "Indeed UK", "Glassdoor UK", "Welcome to the Jungle",
                                                 "Wellfound", "YC Work at a Startup", "EURES", "StepStone DE", "StepStone NL",
                                                 "IrishJobs", "SEEK AU", "SEEK NZ", "MyCareersFuture", "GOV.UK Find a Job",
                                                 "NHS Jobs", "BlackRock", "JPMorgan Chase"))


def test_openai_is_the_ashby_board_openai_and_deepmind_fell_back_to_gmail_alerts_because_no_greenhouse_token_answers(shipped):
    openai, dm = shipped.get("openai"), shipped.get("google-deepmind")
    assert (openai.method, openai.token) == ("ashby", "openai")
    assert dm.method == "gmail_alerts" and not dm.token and dm.domains
    assert "404" in dm.note and "deepmind" in dm.note.lower()                    # why: the tokens that were tried
    assert [(b.ats, b.slug) for b in shipped.boards()] == [("ashby", "openai")]


def test_every_gmail_source_names_sender_domains_and_job_bank_names_its_dataset(shipped):
    assert all(s.domains for s in shipped if s.method == "gmail_alerts")
    assert shipped.get("jobbank-ca").dataset == "ea639e28-c0fc-48bf-b5dd-b8899bd43072" and shipped.get("jobbank-ca").country == "CA"


def test_non_uk_one_country_sources_know_their_country(shipped):
    assert [shipped.get(i).home_country for i in ("seek-au", "seek-nz", "stepstone-de", "irishjobs", "jobbank-ca", "linkedin", "cv-library")] \
        == ["Australia", "New Zealand", "Germany", "Ireland", "Canada", "", "United Kingdom"]


def test_the_sources_that_must_never_be_fetched_are_alert_only(shipped):
    from careerops.discovery.robots import never_fetch
    assert never_fetch(shipped.get("linkedin").url) and shipped.get("linkedin").method == "gmail_alerts"


def test_a_settings_file_without_the_key_or_the_file_is_an_empty_registry(settings):
    assert len(registry.load(settings)) == 0                                     # the fixture points at a file that is not there
    del settings.raw["paths"]["sources"]
    assert len(registry.load(settings)) == 0


# ------------------------------------------------------------------ validation: every mistake names the entry
@pytest.mark.parametrize("bad, message", [
    (entry(method="scrape"), "method 'scrape'"),
    (entry(domains=[]), "needs 'domains'"),
    (entry(domains=["not a domain"]), "not a domain name"),
    (entry(country="Atlantis"), "country 'Atlantis'"),
    (entry(url="x.example"), "url must start"),
    (entry(token="abc"), "'token' only goes with"),
    (entry(parser="mystery"), "parser must be one of"),
    (entry(id="Bad Id"), "id must be"),
    (entry(name=""), "'name' is missing"),
    (entry(method="jobbank_csv", domains=[], dataset="nope"), "needs 'dataset'"),
    (entry(method="ashby", domains=[], token="no spaces allowed"), "is not a board token"),
])
def test_a_bad_entry_is_refused_with_its_name_and_the_reason(bad, message):
    with pytest.raises(RegistryError, match=message):
        parse(bad)


def test_a_repeated_id_is_refused():
    with pytest.raises(RegistryError, match="used twice"):
        parse(entry(), entry())


def test_a_file_that_is_not_yaml_is_refused_without_a_traceback(settings):
    Path(settings.raw["paths"]["sources"]).write_text("sources: [unclosed", encoding="utf-8")
    with pytest.raises(RegistryError, match="cannot be read"):
        registry.load(settings)


def test_disabled_sources_are_kept_but_not_active():
    reg = parse(entry(enabled=False), entry(id="y", domains=["y.example"]))
    assert [s.id for s in reg] == ["x", "y"] and [s.id for s in reg.active()] == ["y"]
    assert reg.match_sender("a@x.example") is None and reg.match_sender("a@y.example").id == "y"


# ------------------------------------------------------------------ which source an alert email is from
def test_a_sender_matches_by_domain_including_subdomains():
    reg = parse(entry(id="li", domains=["linkedin.com"]), entry(id="cv", domains=["cv-library.co.uk"]))
    assert reg.match_sender("LinkedIn <jobs-noreply@linkedin.com>").id == "li"
    assert reg.match_sender("alerts@mail.cv-library.co.uk").id == "cv"
    assert reg.match_sender("Mum <mum@gmail.com>") is None and reg.match_sender("") is None
    assert reg.match_sender("a@notlinkedin.com") is None                         # a domain, not a suffix of text


def test_a_shared_sender_needs_its_subject_words_and_the_longer_domain_wins():
    reg = parse(entry(id="dm", domains=["greenhouse.io"], subject_contains=["DeepMind"]),
                entry(id="other", domains=["greenhouse.io"], subject_contains=["Acme"]),
                entry(id="gen", domains=["mail.greenhouse.io"]))
    assert reg.match_sender("n@greenhouse.io", "New jobs at DeepMind").id == "dm"
    assert reg.match_sender("n@greenhouse.io", "Acme: 3 new jobs").id == "other"
    assert reg.match_sender("n@greenhouse.io", "Weekly digest") is None
    assert reg.match_sender("n@mail.greenhouse.io", "Weekly digest").id == "gen"            # the longer domain


def test_restricting_to_some_sources_leaves_the_others_unmatched():
    reg = parse(entry(id="a", domains=["a.example"]), entry(id="b", domains=["b.example"], parser="indeed"))
    assert reg.match_sender("x@a.example", only=["b"]) is None
    assert reg.match_sender("x@b.example", only=["indeed"]).id == "b"            # by kind (the dedicated parser's name)


# ------------------------------------------------------------------ which source a found job counts for
def cand(**kw) -> Candidate:
    return Candidate(**{"source": "greenhouse", "company": "Acme", "title": "Data Scientist", "location": "London", "url": "u",
                        "description": "d", **kw})


def test_a_job_counts_for_the_alert_it_came_from_the_board_token_or_the_method(shipped):
    assert shipped.attribute(cand(origin="gmail:linkedin")).id == "linkedin"
    assert shipped.attribute(cand(origin="gmail:cvlibrary", source="cvlibrary")).id == "cv-library"
    assert shipped.attribute(cand(origin="gmail:glassdoor-uk")).id == "glassdoor-uk"
    assert shipped.attribute(cand(source="ashby", board="ashby:openai")).id == "openai"
    assert shipped.attribute(cand(source="greenhouse", board="greenhouse:acme")).id == "greenhouse"       # the tokenless entry
    assert shipped.attribute(cand(source="reed")).id == "reed"
    assert shipped.attribute(cand(source="cvlibrary")).id == "cv-library"
    assert shipped.attribute(cand(source="adzuna")) is None                      # not in the registry: counted as "other"
    assert shipped.attribute(cand(source="lever", board="lever:acme")) is None   # no lever entry


def test_legacy_fetch_labels_map_to_the_same_sources(shipped):
    assert shipped.label_source("ashby:openai").id == "openai"
    assert shipped.label_source("greenhouse:anything").id == "greenhouse"
    assert shipped.label_source("adzuna:data scientist") is None


def test_the_registry_file_is_plain_yaml_with_one_top_level_key():
    assert list(yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))) == ["sources"]
