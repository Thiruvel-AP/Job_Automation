"""Company size (discovery/size.py): SME or large, from the user's overrides, the ad's text, then Companies House. The Companies House API is a fake
`Net`: no test touches the network, and only GET is ever sent."""
from datetime import date, timedelta

import pytest

from careerops.discovery import size as Z
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net

TODAY = date(2026, 10, 31)


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def ch_net(companies: dict[str, list[tuple[str, str, str | None]]]) -> Net:
    """companies: searched name -> [(title, status, accounts type)]; each result gets a company number of its own."""
    net = Net(strict=False)
    found: dict[str, list[dict]] = {}
    n = 0
    for name, items in companies.items():
        rows = []
        for title, status, kind in items:
            n += 1
            number = f"{n:08d}"
            rows.append({"title": title, "company_status": status, "company_number": number})
            net.add(f"/company/{number}", {"company_name": title, "accounts": {"last_accounts": {"type": kind}} if kind else {}})
        found[Z.norm(name)] = rows
    net.add("search/companies", lambda url, params=None, **kw: {"items": found.get(Z.norm(params["q"]), [])})
    return net


def resolver(settings, net, **kw):
    kw.setdefault("key", "test-key")
    return Z.Resolver(settings, fetcher=fetcher(net), today=TODAY, **kw)


# ------------------------------------------------------------------ the ad's text
@pytest.mark.parametrize("text,size,label", [
    ("We are a team of 40 people building data tools.", "sme", "small"), ("team of 40", "sme", "small"), ("a 30-person startup", "sme", "small"),
    ("We are 8 people in a shared office", "sme", "micro"), ("about 120 employees in 3 offices", "sme", "medium"),
    ("1,200 employees across the world", "large", "large"), ("We have 251 staff", "large", "large"), ("500+ people", "large", "large"),
    ("50-200 employees", "sme", "medium"), ("between 200 and 600 employees: 200-600 employees", "large", "large"),     # a range counts its upper value
    ("We just closed our Series A round", "sme", "small"), ("Series B startup", "sme", "small"), ("a seed-stage company", "sme", "small"),
    ("you will join the founding team", "sme", "small"),
    ("We are a FTSE 100 company", "large", "large"), ("a Fortune 500 firm", "large", "large"), ("listed on NASDAQ", "large", "large"),
    ("LSE-listed", "large", "large"), ("offices in 14 countries", "large", "large"), ("10,000+ colleagues", "large", "large"),
])
def test_what_an_ad_says_about_its_size(text, size, label):
    s = Z.from_text(text)
    assert s is not None and (s.size, s.label) == (size, label) and s.evidence.startswith("ad: ")


@pytest.mark.parametrize("text", [
    "You will join a team of 5 data scientists.", "offices in 3 countries", "10,000+ customers trust us", "work with 5 people", "", "nothing about size",
])
def test_text_that_is_not_about_the_company_size_says_nothing(text):
    assert Z.from_text(text) is None


def test_an_ad_that_says_both_small_and_large_is_unknown():
    s = Z.from_text("Series B startup, listed on NASDAQ")
    assert s.size == "unknown" and "both" in s.evidence


def test_the_headcount_limits_are_the_companies_act_ones():
    assert [Z.from_headcount(n, "").label for n in (10, 11, 50, 51, 250, 251)] == ["micro", "small", "small", "medium", "medium", "large"]


def test_names_are_normalised_for_matching():
    assert Z.norm("Graphcore Ltd.") == Z.norm("GRAPHCORE LIMITED") == "graphcore"
    assert Z.norm("Smith & Nephew PLC (UK)") == "smith nephew" and Z.norm("Acme UK Limited") == "acme"


# ------------------------------------------------------------------ Companies House
@pytest.mark.parametrize("kind,size,label", [("micro-entity", "sme", "micro"), ("small", "sme", "small"), ("total-exemption-small", "sme", "small"),
                                             ("total-exemption-full", "sme", "small"), ("audited-abridged", "sme", "small"),
                                             ("unaudited-abridged", "sme", "small"), ("dormant", "sme", "micro"), ("medium", "sme", "medium"),
                                             ("group", "large", "large")])
def test_the_accounts_type_decides(settings, kind, size, label):
    net = ch_net({"Acme Data": [("ACME DATA LIMITED", "active", kind)]})
    got = resolver(settings, net).resolve("Acme Data Ltd")
    assert (got.size, got.label) == (size, label) and "Companies House" in got.evidence and kind in got.evidence
    assert all(r["auth"] == ("test-key", "") for r in net.requests if "company-information" in r["url"])


def test_full_accounts_are_unknown_because_a_small_company_may_file_them_too(settings):
    got = resolver(settings, ch_net({"Acme Data": [("ACME DATA LIMITED", "active", "full")]})).resolve("Acme Data")
    assert got.size == "unknown" and "full (large?)" in got.evidence


def test_two_active_companies_with_the_name_are_never_guessed_between(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "small"), ("Acme Ltd", "active", "group")]})
    got = resolver(settings, net).resolve("Acme")
    assert got.size == "unknown" and "2 active" in got.evidence
    assert not any("/company/" in r["url"] for r in net.requests)                 # the profile of neither was read


def test_a_dissolved_namesake_does_not_count_and_a_different_name_is_not_a_match(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "dissolved", "group"), ("ACME LTD", "active", "micro-entity"), ("ACME HOLDINGS LIMITED", "active", "group")]})
    assert resolver(settings, net).resolve("Acme").label == "micro"


def test_no_match_at_all_is_unknown(settings):
    got = resolver(settings, ch_net({"Acme": []})).resolve("Acme")
    assert got.size == "unknown" and "0 active" in got.evidence


def test_the_api_is_only_ever_read_and_only_by_the_polite_client(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "small")]})
    resolver(settings, net).resolve("Acme")
    hosts = {r["url"].split("/")[2] for r in net.requests}
    assert hosts == {"api.company-information.service.gov.uk"} and len(net.requests) == 2        # one search, one profile (robots.txt is not asked: a credentialed API)


# ------------------------------------------------------------------ order of evidence
def test_an_override_beats_every_other_source(settings):
    settings.path("company_size_overrides").write_text("Acme Data: large\n", encoding="utf-8")
    net = ch_net({"Acme Data": [("ACME DATA LIMITED", "active", "micro-entity")]})
    got = resolver(settings, net).resolve("Acme Data Ltd", "We are a team of 40 people and just raised our Series A.")
    assert (got.size, got.label) == ("large", "large") and got.evidence.startswith("override: Acme Data: large")
    assert net.requests == []


def test_an_override_can_name_a_sub_label_and_a_bad_line_is_a_note_not_a_crash(settings):
    settings.path("company_size_overrides").write_text("Alpha: small\nBeta: huge\n", encoding="utf-8")
    r = resolver(settings, ch_net({}))
    assert r.resolve("Alpha").label == "small" and r.resolve("Beta").size == "unknown"
    assert any("Beta" in n for n in r.notes)


def test_the_ads_own_text_comes_before_companies_house(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "group")]})
    got = resolver(settings, net).resolve("Acme", "We are a team of 40 people.")
    assert got.size == "sme" and got.evidence.startswith("ad: ") and net.requests == []


def test_an_answer_is_cached_for_90_days_in_the_jsonl(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "small")]})
    resolver(settings, net).resolve("Acme")
    assert len(net.requests) == 2 and settings.path("company_size").read_text(encoding="utf-8").count("\n") == 1
    again = resolver(settings, net)
    assert again.resolve("Acme").label == "small" and len(net.requests) == 2                       # from the cache
    assert Z.Resolver(settings, key="", today=TODAY + timedelta(days=89)).resolve("Acme").evidence.startswith("Companies House")     # still fresh at 89 days
    old = Z.Resolver(settings, fetcher=fetcher(net), key="test-key", today=TODAY + timedelta(days=91))
    assert old.resolve("Acme").label == "small" and len(net.requests) == 4                         # older than 90 days: asked again


def test_an_unknown_answer_is_cached_too_so_it_is_not_asked_again(settings):
    net = ch_net({"Acme": []})
    resolver(settings, net).resolve("Acme")
    n = len(net.requests)
    resolver(settings, net).resolve("Acme")
    assert len(net.requests) == n


# ------------------------------------------------------------------ no key, limits, refusals
def test_without_a_key_companies_house_is_skipped_and_nothing_crashes(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "small")]})
    r = Z.Resolver(settings, fetcher=fetcher(net), key="", today=TODAY)
    got = r.resolve("Acme")
    assert got.size == "unknown" and Z.KEY_ENV in got.evidence and not r.has_key and net.requests == []
    assert not settings.path("company_size").exists()                                                # nothing cached: a key added later is used


def test_the_per_run_lookup_limit_leaves_the_rest_for_the_next_run(settings):
    net = ch_net({"Acme": [("ACME LIMITED", "active", "small")], "Beta": [("BETA LIMITED", "active", "group")]})
    r = resolver(settings, net, max_lookups=1)
    assert r.resolve("Acme").size == "sme" and r.resolve("Beta").size == "unknown"
    assert r.skipped_for_limit == 1 and "Beta" not in settings.path("company_size").read_text(encoding="utf-8")


def test_a_refusal_stops_the_host_for_the_rest_of_the_run(settings):
    net = Net(strict=False).add("search/companies", "", status=429)
    r = resolver(settings, net)
    assert r.resolve("Acme").size == "unknown" and r.blocked and any("refused" in n for n in r.notes)
    n = len(net.requests)
    assert r.resolve("Beta").size == "unknown" and len(net.requests) == n                           # never asked again, never worked around


def test_a_network_error_is_unknown_and_not_cached(settings):
    net = Net(strict=False).add("search/companies", "oops", status=500)
    r = resolver(settings, net)
    assert r.resolve("Acme").size == "unknown" and not settings.path("company_size").exists()


# ------------------------------------------------------------------ display
def test_how_a_size_is_shown():
    assert Z.Size("sme", "small").tag == "SME·small" and Z.Size("large", "large").tag == "Large" and Z.NONE.tag == "size?"
    assert Z.Size("sme", "medium").cell == "SME (medium)" and Z.NONE.cell == "Unknown" and Z.bucket_of("SME (small)") == "sme" and Z.bucket_of("Large") == "large"
    assert Z.counts_text(Z.counts([Z.Size("sme", "small"), Z.NONE, Z.NONE])) == "SME 1 · Large 0 · Unknown 2"
