"""Wrong-company boards (discovery/boards.py): a board found only through a shortened slug (the first word of the name, its first two
words, or the name without Group / Holdings / Technologies) is the company's only when the board's own name matches the company at
discover.ats.board_name_match (0.9). Otherwise it is recorded as `board_unverified`, no ad is written from it, and the report lists it.
discover.ignore_companies skips non-companies. No network: `Net` answers."""
from datetime import datetime, timezone

import pytest

from careerops.discovery import ats as ats_mod, boards as boards_mod
from careerops.discovery.discover import Discover
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from careerops.discovery.seeds import Seed
from netfake import Net, Resp
from test_quality_round import board as gh_jobs, gh        # noqa: F401  (a Greenhouse reply builder shared with the quality-round tests)

TODAY = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
UK_DS = ("1", "Data Scientist", "London, UK")


def fetcher(net):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"].update(alert_probes_per_run=0, sources=[], listed_only={"enabled": False})
    return settings


def make(cfg, db, net, **kw):
    return Discover(cfg, db, fetcher(net), today=TODAY, say=lambda s: None, **kw)


def routes(net, **boards):
    for part, reply in boards.items():
        net.add(part.replace("__", "/"), reply)
    return net


def ashby_board(*postings):
    return {"jobs": [{"id": i, "title": t, "location": loc, "jobUrl": f"https://jobs.ashbyhq.com/x/{i}", "publishedAt": "2026-10-08T09:00:00Z",
                      "descriptionPlain": "We build forecasting tools for retailers. " * 20, "isListed": True} for i, t, loc in postings]}


def ashby_at(net, slug, body):
    """Ashby answers for exactly this board name (a route part would also match a longer token that starts with it)."""
    return net.add("api.ashbyhq.com", lambda url, **kw: Resp(body) if url.rstrip("/").endswith("/job-board/" + slug) else Resp("", 404))


def folder(cfg, name):
    (cfg.path("applications") / name).mkdir()


# ------------------------------------------------------------------ the rule
@pytest.mark.parametrize("name, full", [
    ("Monzo Bank Ltd", {"monzobank", "monzo-bank"}),
    ("Octagon Group", {"octagongroup", "octagon-group"}),               # not "octagon": that word-dropped form is a shortened slug
    ("Arch Capital Group", {"archcapitalgroup", "arch-capital-group"}),
    ("Applied Scientist", {"appliedscientist", "applied-scientist"}),
    ("The Trade Desk Limited", {"tradedesk", "trade-desk"}),
    ("Wise", {"wise"})])
def test_the_full_name_slugs_are_the_joined_and_the_hyphenated_name(name, full):
    assert ats_mod.full_name_tokens(name) == full


def test_a_shortened_slug_with_a_different_board_name_is_rejected_and_recorded(cfg, db):
    folder(cfg, "Octagon Group")
    net = routes(Net(strict=False), **gh("octagon", "Octagon Software", UK_DS))
    d = make(cfg, db, net)
    d.from_boards()
    (f,) = d.findings
    assert f.boards == [] and [(b.ats, b.slug) for b, _ in f.unverified] == [("greenhouse", "octagon")]
    assert "Octagon Software" in f.unverified[0][1] and "board_unverified" in f.line()
    assert d.run.written == [] and d.run.sources["ats"]["board_unverified"] == 1          # no ad is written from it
    assert not (cfg.path("jds") / "inbox").exists() or not list((cfg.path("jds") / "inbox").glob("*.txt"))


def test_a_full_name_slug_is_accepted_without_a_name_check(cfg, db):
    folder(cfg, "Maple Analytics")
    net = routes(Net(strict=False), **gh("mapleanalytics", "Some Other Name", UK_DS))     # the board's name is not even asked
    d = make(cfg, db, net)
    d.from_boards()
    (f,) = d.findings
    assert [(b.ats, b.slug) for b in f.boards] == [("greenhouse", "mapleanalytics")] and f.unverified == []
    assert len(d.run.written) == 1
    assert not [u for u in net.urls() if u.endswith("/boards/mapleanalytics")]            # only the jobs list was read


def test_a_shortened_slug_whose_board_name_matches_is_accepted(cfg, db):
    folder(cfg, "Maple Analytics Ltd")
    net = routes(Net(strict=False), **gh("maple", "Maple Analytics", UK_DS))               # "maple analytics" == "maple analytics"
    d = make(cfg, db, net)
    d.from_boards()
    (f,) = d.findings
    assert [(b.ats, b.slug) for b in f.boards] == [("greenhouse", "maple")] and f.unverified == [] and len(d.run.written) == 1


def test_the_match_is_at_nine_tenths_so_a_one_letter_difference_passes_and_a_longer_name_does_not(cfg, db):
    assert boards_mod.names_match("Maple Analytic", "Maple Analytics Ltd", 0.9)
    assert not boards_mod.names_match("Arch", "Arch Capital Group", 0.9)
    assert not boards_mod.names_match("Focus", "Focus Group", 0.9)
    assert not boards_mod.names_match("", "Anything", 0.9)


def test_a_board_whose_api_gives_no_name_cannot_vouch_for_a_shortened_slug(cfg, db):
    folder(cfg, "Applied Scientist")
    net = ashby_at(Net(strict=False), "applied", ashby_board(UK_DS))
    d = make(cfg, db, net)
    d.from_boards()
    (f,) = d.findings
    assert f.boards == [] and [(b.ats, b.slug) for b, _ in f.unverified] == [("ashby", "applied")]
    assert "no company name" in f.unverified[0][1] and d.run.written == []


def test_the_register_verified_slug_is_never_shortened(cfg, db):
    seed = Seed("Northwind Health AI", "archive", hint="nwh")
    net = routes(Net(strict=False), **gh("nwh", "NWH", UK_DS))
    f = boards_mod.find(seed, fetcher(net), boards_mod.BoardIndex(cfg))
    assert [(b.ats, b.slug) for b in f.boards] == [("greenhouse", "nwh")] and f.unverified == []


def test_the_report_lists_every_unverified_board_and_how_to_accept_one(cfg, db):
    folder(cfg, "Octagon Group")
    folder(cfg, "Focus Group")
    net = ashby_at(routes(Net(strict=False), **gh("octagon", "Octagon Software", UK_DS)), "focus", ashby_board(UK_DS))
    d = make(cfg, db, net)
    d.from_boards()
    report = "\n".join(d.ats_report())
    assert "2 board(s) rejected by the board-name check (board_unverified)" in report
    assert "board_unverified: greenhouse/octagon" in report and "board_unverified: ashby/focus" in report
    assert "sources:" in report                                              # where to put a board that is the right company


def test_an_unverified_board_is_remembered_so_the_name_is_asked_once(cfg, db):
    folder(cfg, "Octagon Group")
    net = routes(Net(strict=False), **gh("octagon", "Octagon Software", UK_DS))
    make(cfg, db, net).from_boards()
    first = len(net.urls())
    make(cfg, db, net).from_boards()                                         # a second run: the index and the name are cached
    assert len([u for u in net.urls() if u.endswith("/boards/octagon")]) == 1 and len(net.urls()) <= first + 1


# ------------------------------------------------------------------ discover.ignore_companies
def test_ignored_companies_are_not_looked_for_and_the_archive_folder_stays(cfg, db):
    for name in ("New folder", "AI Engineer", "Global Job Hunter", "Monzo"):
        folder(cfg, name)
    cfg.raw["discover"]["ignore_companies"] = ["new folder", "AI Engineer", "Global  Job Hunter"]       # case and spacing do not matter
    net = Net(strict=False)
    d = make(cfg, db, net)
    d.from_boards()
    assert [f.seed.name for f in d.findings] == ["Monzo"]
    assert not [u for u in net.urls() if "global" in u or "/new" in u or "aiengineer" in u]
    assert sorted(p.name for p in cfg.path("applications").iterdir()) == ["AI Engineer", "Global Job Hunter", "Monzo", "New folder"]


def test_the_shipped_ignore_list_is_empty_for_the_user_to_fill(settings):
    assert settings["discover"]["ignore_companies"] == []
    assert settings["discover"]["ats"]["board_name_match"] == 0.9


# ------------------------------------------------------------------ the same check for an alert's company
def test_an_alerts_company_found_only_by_its_first_word_is_not_resolved_to_a_different_company(cfg, db):
    net = ashby_at(Net(strict=False), "focus", ashby_board(("5", "Data Scientist", "London")))
    d = make(cfg, db, net)
    d.lookup.probes = d.lookup.probes_allowed = 5
    assert d.lookup.find("Focus Group", "Data Scientist", "London") is None
    assert [(b.ats, b.slug) for f in d.lookup.findings for b, _ in f.unverified] == [("ashby", "focus")]
