"""v3.0 discovery: the careers addresses you give (careers_urls.yaml), a miss believed only after three separate runs, the public board list used before any guess
(at most 20 guesses a provider, never on a provider with one host per company), `search-plan` and its doctor line. No network: `Net` answers."""
import csv
import json
from datetime import datetime, timedelta, timezone

import pytest
from netfake import Net, Resp, fixture_json
from test_board_check import cfg, fetcher as fx, folder, make  # noqa: F401  (cfg: a settings copy with no listed boards and no alert probes)

from careerops import cli, setup
from careerops.discovery import ats as ats_mod, boards as boards_mod, careers, dataset as dataset_mod, searchplan
from careerops.discovery.boards import BoardIndex
from careerops.discovery.seeds import Seed


def write_careers(cfg, text):
    p = careers.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def lists(net, **named):
    """The four files of the public board list, each an (empty) list unless the test gives it."""
    for name in ("greenhouse", "lever", "ashby", "workday"):
        net.add(f"{name}_companies.json", named.get(name, []))
    return net


# ------------------------------------------------------------------ careers_urls.yaml
def test_a_missing_file_is_no_entries_and_no_problem(cfg):
    assert careers.load(cfg) == ([], []) and careers.boards(cfg) == [] and careers.keys(cfg) == set() and careers.problems(cfg) == []


def test_the_file_is_read_in_three_shapes(cfg):
    write_careers(cfg, "companies:\n  - {company: Mistral AI, url: 'https://jobs.lever.co/mistral'}\n")
    assert [(e.company, e.url) for e in careers.load(cfg)[0]] == [("Mistral AI", "https://jobs.lever.co/mistral")]
    write_careers(cfg, "- {company: Mistral AI, url: 'https://jobs.lever.co/mistral'}\n- {company: Acme, url: 'https://acme.teamtailor.com'}\n")
    assert [e.company for e in careers.load(cfg)[0]] == ["Mistral AI", "Acme"]
    write_careers(cfg, "Mistral AI: https://jobs.lever.co/mistral\nAcme: https://acme.teamtailor.com\n")
    assert [e.company for e in careers.load(cfg)[0]] == ["Mistral AI", "Acme"]


@pytest.mark.parametrize("url, ats, slug", [
    ("https://boards.greenhouse.io/acme", "greenhouse", "acme"),
    ("https://job-boards.greenhouse.io/Acme/jobs/123", "greenhouse", "Acme"),
    ("https://jobs.lever.co/mistral", "lever", "mistral"),
    ("https://jobs.eu.lever.co/acme/", "lever_eu", "acme"),
    ("https://jobs.ashbyhq.com/AlephAlpha", "ashby", "AlephAlpha"),
    ("https://apply.workable.com/acme/", "workable", "acme"),
    ("https://careers.smartrecruiters.com/Acme", "smartrecruiters", "acme"),
    ("https://acme.teamtailor.com/jobs", "teamtailor", "acme"),
    ("https://acme.recruitee.com", "recruitee", "acme"),
    ("https://acme.pinpointhq.com/", "pinpoint", "acme"),
    ("https://acme.jobs.personio.de/", "personio", "acme.jobs.personio.de"),
])
def test_an_address_gets_the_reader_its_host_names(url, ats, slug):
    board, why = careers.board_of(url, "Acme")
    assert why == "" and (board.ats, board.slug, board.name, board.origin) == (ats, slug, "Acme", "careers_url")


def test_a_workday_address_becomes_a_workday_board():
    board, _ = careers.board_of("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", "NVIDIA")
    assert board.ats == "workday" and board.slug.startswith("nvidia.wd5.myworkdayjobs.com~nvidia~NVIDIAExternalCareerSite")


def test_eploy_is_recognised_from_its_posting_shape_or_from_provider_eploy():
    assert careers.board_of("https://jobs.bhf.org.uk/vacancies/18829/personal-assistant.html", "BHF")[0].ats == "eploy"
    b = careers.board_of("https://jobs.ecotricity.co.uk/", "Ecotricity", "eploy")[0]
    assert (b.ats, b.slug) == ("eploy", "jobs.ecotricity.co.uk")
    assert careers.board_of("https://jobs.ecotricity.co.uk/", "Ecotricity")[0] is None


@pytest.mark.parametrize("url, part", [
    ("https://careers.example.com/jobs", "no reader for careers.example.com yet"),
    ("https://boards.greenhouse.io/", "no company name in it"),
    ("https://acme.wd5.myworkdayjobs.com/", "needs the site name"),
    ("https://www.linkedin.com/company/acme/jobs", "linkedin.com"),
    ("https://uk.indeed.com/cmp/Acme", "never fetched"),
    ("https://www.cv-library.co.uk/company/acme", "never fetched"),
    ("not a url", "not an http(s) address"),
])
def test_an_address_no_reader_understands_says_why_and_nothing_is_guessed(url, part):
    board, why = careers.board_of(url, "Acme")
    assert board is None and part in why


def test_problems_name_bad_entries_duplicates_and_unreadable_addresses(cfg):
    write_careers(cfg, "companies:\n  - {company: Acme, url: 'https://jobs.lever.co/acme'}\n  - {company: acme ltd, url: 'https://jobs.lever.co/acme2'}\n"
                       "  - {company: Beta}\n  - {company: Gamma, url: 'https://gamma.example.com'}\n")
    probs = careers.problems(cfg)
    assert any("listed twice" in p for p in probs) and any("needs both company and url" in p for p in probs)
    assert any("Gamma" in p and "no reader for gamma.example.com" in p for p in probs)
    assert [b.slug for b in careers.boards(cfg)] == ["acme"]                       # the first entry of a duplicate wins
    write_careers(cfg, "companies: [unclosed")
    assert careers.load(cfg)[0] == [] and "not valid YAML" in careers.problems(cfg)[0]


def test_configured_boards_carry_the_careers_boards_under_the_name_in_the_file(cfg):
    write_careers(cfg, "companies:\n  - {company: Mistral AI, url: 'https://jobs.lever.co/mistral'}\n")
    got = [b for b in ats_mod.configured_boards(cfg) if b.origin == "careers_url"]
    assert [(b.ats, b.slug, b.name) for b in got] == [("lever", "mistral", "Mistral AI")]
    assert (("lever", "mistral") in {(b.ats, b.slug) for b in ats_mod.all_boards(cfg, None)})


def test_a_company_with_an_address_is_read_from_it_and_never_guessed(cfg, db):
    folder(cfg, "Mistral AI")
    write_careers(cfg, "companies:\n  - {company: Mistral AI, url: 'https://jobs.lever.co/mistral'}\n")
    net = Net(strict=False).add("api.lever.co/v0/postings/mistral", fixture_json("lever_acme.json"))
    d = make(cfg, db, net)
    d.from_boards()
    assert all("mistral" in u for u in net.urls()) and not [u for u in net.urls() if "mistralai" in u or "mistral-ai" in u or "greenhouse" in u or "ashby" in u]
    assert [f.boards[0].slug for f in d.findings if f.boards] == ["mistral"] and len(d.findings) == 1
    assert any("careers_urls.yaml: 1 address(es), 1 with a reader" in line for line in d.ats_report())
    f = boards_mod.find(Seed("Mistral AI", "archive"), fx(net), BoardIndex(cfg))
    assert f.tokens == [] and "careers_urls.yaml" in f.skipped and f.tried_text() == f.skipped


# ------------------------------------------------------------------ a miss is believed after three separate runs
def test_a_miss_counts_once_a_run_and_a_board_found_resets_it(cfg):
    key = "lever:x"
    idx = BoardIndex(cfg)
    idx.put("lever", "x", False)
    idx.put("lever", "x", False)                                                   # the same run again: still one miss
    assert idx.misses("lever", "x") == 1 and idx.get("lever", "x") is False        # not asked twice in the run that missed it
    idx.save()
    for expected in (2, 3):
        idx = BoardIndex(cfg)
        assert idx.get("lever", "x") is None                                       # a new run: fewer than three misses, ask again
        idx.put("lever", "x", False)
        assert idx.misses("lever", "x") == expected
        idx.save()
    assert BoardIndex(cfg).get("lever", "x") is False                              # three runs have each found none: believed
    idx = BoardIndex(cfg)
    idx.put("lever", "x", True)
    assert idx.misses("lever", "x") == 0 and idx.get("lever", "x") is True and "misses" not in idx.data[key]


def test_an_entry_from_before_v3_counts_as_one_miss_and_an_old_one_is_asked_again(cfg):
    idx = BoardIndex(cfg)
    now = idx.clock()
    idx.data["lever:old"] = {"ts": now.isoformat(timespec="seconds"), "found": False}
    idx.data["lever:stale"] = {"ts": (now - timedelta(days=40)).isoformat(timespec="seconds"), "found": False, "misses": 3}
    assert idx.misses("lever", "old") == 1 and idx.get("lever", "old") is None
    assert idx.get("lever", "stale") is None                                       # older than board_cache_days: asked again whatever the count
    idx.put("lever", "old", False)
    assert idx.misses("lever", "old") == 2
    idx.put("lever", "stale", False)
    assert idx.misses("lever", "stale") == 3                                       # the count does not restart: one miss re-believes it


def test_misses_needed_is_a_setting_and_at_least_one(cfg):
    cfg.raw["discover"]["ats"]["misses_needed"] = 0
    assert BoardIndex(cfg).misses_needed == 1
    cfg.raw["discover"]["ats"]["misses_needed"] = 5
    assert BoardIndex(cfg).misses_needed == 5


# ------------------------------------------------------------------ the public board list
def lever_acme_at(slug):
    return lambda url, **kw: Resp(fixture_json("lever_acme.json")) if url.endswith("/postings/" + slug) else Resp("", 404)


def test_the_list_is_read_once_a_week_keeps_the_case_and_shows_its_licence(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    net = lists(Net(strict=False), ashby=["AlephAlpha"], lever=["mistral"], greenhouse=["acme"], workday=["kyndryl|wd5|KyndrylProfessionalCareers", "broken"])
    d = dataset_mod.Dataset(cfg, fx(net))
    assert d.hits("alephalpha") == {"ashby": "AlephAlpha"} and d.hits("Mistral") == {"lever": "mistral"} and d.hits("nobody") == {}
    assert d.workday_urls("Kyndryl") == ["https://kyndryl.wd5.myworkdayjobs.com/KyndrylProfessionalCareers"]
    assert len([u for u in net.urls() if "companies.json" in u]) == 4 and d.requests == 4
    assert "CC BY-NC 4.0" in d.summary() and "Feashliaa/job-board-aggregator" in d.summary() and "raw.githubusercontent.com" in dataset_mod.BASE
    again = dataset_mod.Dataset(cfg, fx(net))                                       # fresh files on disk: nothing is asked
    assert again.hits("acme") == {"greenhouse": "acme"} and len([u for u in net.urls() if "companies.json" in u]) == 4


def test_a_stale_copy_is_asked_again_and_a_refusal_stops_the_download_but_the_old_copy_serves(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True, "refresh_days": 1}
    d = dataset_mod.Dataset(cfg, fx(lists(Net(strict=False), lever=["mistral"])))
    d.ensure()
    later = datetime.now(timezone.utc) + timedelta(days=3)
    net = Net(strict=False).add("companies.json", Resp("", 429))
    stale = dataset_mod.Dataset(cfg, fx(net), clock=lambda: later)
    assert stale.hits("mistral") == {"lever": "mistral"}                            # the older copy is better than none
    assert len([u for u in net.urls() if "companies.json" in u]) == 1 and any("stopped for this run" in n for n in stale.notes)


def test_without_a_copy_and_without_a_network_the_list_is_off_and_nothing_breaks(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    d = dataset_mod.Dataset(cfg, fx(Net(strict=False).add("companies.json", Resp("", 500))))
    assert d.hits("mistral") == {} and d.enabled is False
    assert dataset_mod.Dataset(cfg, None).hits("mistral") == {}                     # no fetcher (search-plan): disk only
    cfg.raw["discover"]["dataset"] = {"enabled": False}
    off = dataset_mod.Dataset(cfg, fx(Net()))
    assert off.hits("mistral") == {} and off.workday_urls("kyndryl") == []


def test_a_name_in_the_list_is_checked_with_one_request_and_nothing_else_is_guessed(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    net = lists(Net(strict=False), lever=["acmeanalytics"]).add("api.lever.co", lever_acme_at("acmeanalytics"))
    index = BoardIndex(cfg)
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    f = boards_mod.find(Seed("Acme Analytics Ltd", "archive"), fx(net), index)
    assert [(b.ats, b.slug) for b in f.boards] == [("lever", "acmeanalytics")] and f.listed == {"lever"}
    assert [u for u in net.urls() if "companies.json" not in u] == ["https://api.lever.co/v0/postings/acmeanalytics"]
    counts = index.guess_counts()["lever"]
    assert counts["guesses"] == 0 and counts["checked_from_list"] == 1 and "in the public board list: lever" in f.line()


def test_a_board_already_found_is_kept_when_the_list_places_the_name_elsewhere_or_the_provider_is_not_guessed(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "teamtailor", "recruitee"]
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    net = lists(Net(strict=False), greenhouse=["acmeanalytics"])                       # the list says Greenhouse; two boards were found earlier elsewhere
    index = BoardIndex(cfg)
    index.put("ashby", "acmeanalytics", True)
    index.put("teamtailor", "acmeanalytics", True)
    index.set_name("teamtailor", "acmeanalytics", "Acme Analytics")
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    f = boards_mod.find(Seed("Acme Analytics Ltd", "archive"), fx(net), index)
    assert sorted((b.ats, b.slug) for b in f.boards) == [("ashby", "acmeanalytics"), ("teamtailor", "acmeanalytics")]
    asked = [u for u in net.urls() if "companies.json" not in u]
    assert asked == ["https://boards-api.greenhouse.io/v1/boards/acmeanalytics/jobs"]  # only the list's own provider was asked; the known boards cost no request


def test_a_provider_that_said_no_this_run_still_has_the_boards_it_was_known_to_have(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable"]
    net = Net(strict=False)
    index = BoardIndex(cfg)
    index.put("workable", "acmeanalytics", True)
    index.set_name("workable", "acmeanalytics", "Acme Analytics")
    index.stopped["workable"] = "HTTP 429"                                             # Workable answered 429 earlier in the run
    f = boards_mod.find(Seed("Acme Analytics Ltd", "archive"), fx(net), index)
    assert [(b.ats, b.slug) for b in f.boards] == [("workable", "acmeanalytics")]       # still "with a board" in the report
    assert not [u for u in net.urls(True) if "workable" in u]                           # and nothing was asked of it
    other = boards_mod.find(Seed("Beta Works", "archive"), fx(net), index)
    assert other.boards == [] and not [u for u in net.urls(True) if "workable" in u]    # an unknown name is not guessed on a stopped provider


def test_the_list_gives_the_slug_as_written_so_a_camel_case_ashby_board_is_found(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    net = lists(Net(strict=False), ashby=["AlephAlpha"])
    net.add("api.ashbyhq.com", lambda url, **kw: Resp(fixture_json("ashby_acme.json")) if url.endswith("/job-board/AlephAlpha") else Resp("", 404))
    index = BoardIndex(cfg)
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    f = boards_mod.find(Seed("Aleph Alpha", "archive"), fx(net), index)
    assert [(b.ats, b.slug) for b in f.boards] == [("ashby", "AlephAlpha")]
    assert index.slug("ashby", "alephalpha") == "AlephAlpha" and BoardIndex(cfg).slug("ashby", "alephalpha") == "AlephAlpha"       # remembered for the next run


def test_checks_of_names_in_the_list_have_their_own_limit(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    cfg.raw["discover"]["ats"]["dataset_checks_per_provider"] = 1
    net = lists(Net(strict=False), lever=["alphalabs", "betalabs"]).add("api.lever.co", lambda url, **kw: Resp("", 404))
    index = BoardIndex(cfg)
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    first = boards_mod.find(Seed("Alpha Labs", "archive"), fx(net), index)
    second = boards_mod.find(Seed("Beta Labs", "archive"), fx(net), index)
    assert first.capped == set() and second.capped == {"lever"} and index.over_directed["lever"] >= 1
    assert any("checks of names in the public board list stopped at 1 a run" in n for n in index.notes())


def test_a_name_not_in_the_list_is_still_guessed_and_a_workday_site_is_shown_never_read(cfg):
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    net = lists(Net(strict=False), workday=["zetaworks|wd3|ZWCareers"])
    index = BoardIndex(cfg)
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    f = boards_mod.find(Seed("Zeta Works", "archive"), fx(net), index)
    assert f.boards == [] and f.suggested == ["https://zetaworks.wd3.myworkdayjobs.com/ZWCareers"]
    assert index.guess_counts()["ashby"]["guesses"] == 3                            # the list had nothing for it: guessed as before
    assert not [u for u in net.urls(True) if "myworkdayjobs" in u]
    assert "the list suggests (not read; check, then add to careers_urls.yaml)" in f.line()


# ------------------------------------------------------------------ search-plan
def test_search_plan_lists_the_companies_with_no_board_and_no_address(cfg, db):
    for name in ("Mistral AI", "Kyndryl", "Has Address Ltd", "Has Board"):
        folder(cfg, name)
    write_careers(cfg, "companies:\n  - {company: Has Address Ltd, url: 'https://jobs.lever.co/hasaddress'}\n")
    boards_dir = cfg.path("discover") / "boards"
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / "greenhouse-hasboard.json").write_text(json.dumps({"name": "Has Board", "postings": []}), encoding="utf-8")
    ds = cfg.path("discover") / "dataset"
    ds.mkdir(parents=True, exist_ok=True)
    (ds / "lever_companies.json").write_text(json.dumps(["mistral"]), encoding="utf-8")
    (ds / "workday_companies.json").write_text(json.dumps(["kyndryl|wd5|KyndrylProfessionalCareers"]), encoding="utf-8")
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    items, total = searchplan.pending(cfg, db, limit=None)
    assert [i.name for i in items] == ["Kyndryl", "Mistral AI"] and total == 2
    by = {i.name: i for i in items}
    assert by["Mistral AI"].listed == {"mistral": {"lever": "mistral"}}
    assert by["Kyndryl"].workday == ["https://kyndryl.wd5.myworkdayjobs.com/KyndrylProfessionalCareers"]
    text = "\n".join(searchplan.plan_lines(items, total, cfg))
    assert "2 compan(ies) with no board and no entry in careers_urls.yaml" in text and "lever/mistral" in text and "check it is theirs" in text
    assert "one WebSearch per company" in text and "never writes that file" in text
    assert len(searchplan.pending(cfg, db, limit=1)[0]) == 1


def test_search_plan_check_says_which_reader_each_address_gets_and_prints_the_lines_to_paste(cfg, tmp_path):
    write_careers(cfg, "companies:\n  - {company: Already There, url: 'https://jobs.lever.co/there'}\n")
    p = tmp_path / "proposed.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "url", "evidence"])
        w.writerow(["Mistral AI", "https://jobs.lever.co/mistral", "search result jobs.lever.co/mistral"])
        w.writerow(["NVIDIA", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", ""])
        w.writerow(["British Heart Foundation", "https://jobs.bhf.org.uk/vacancies/18829/x.html", ""])
        w.writerow(["Google", "https://www.google.com/about/careers/applications/jobs/results", ""])
        w.writerow(["Already There", "https://jobs.lever.co/there", ""])
        w.writerow(["", "https://x.example.com", ""])
    lines = searchplan.check_lines(cfg, searchplan.read_proposals(p))
    text = "\n".join(lines)
    assert lines[0] == "search-plan --check: 6 proposed address(es), 3 ready to paste"
    assert "lever           Mistral AI: https://jobs.lever.co/mistral | board mistral" in text and "seen: search result" in text
    assert "NO READER  Google" in text and "no reader for www.google.com yet" in text and "(already in careers_urls.yaml)" in text
    assert "skipped (needs company and url)" in text
    assert "  - {company: 'Mistral AI', url: 'https://jobs.lever.co/mistral'}" in lines
    assert "  - {company: 'British Heart Foundation', url: 'https://jobs.bhf.org.uk/vacancies/18829/x.html', provider: eploy}" in lines
    assert not [x for x in lines if "Already There'" in x and x.startswith("  - {")]
    assert careers.path(cfg).read_text(encoding="utf-8").count("Mistral") == 0                    # nothing was written


def test_the_search_plan_command_prints_and_checks(cfg, db, tmp_path, monkeypatch, capsys):
    folder(cfg, "Mistral AI")
    monkeypatch.setattr(cli, "_ctx", lambda args: (cfg, db))
    cli.main(["search-plan"])
    out = capsys.readouterr().out
    assert "1 compan(ies) with no board and no entry in careers_urls.yaml" in out and "Mistral AI" in out
    p = tmp_path / "p.csv"
    p.write_text("company,url\nMistral AI,https://jobs.lever.co/mistral\n", encoding="utf-8")
    cli.main(["search-plan", "--check", str(p)])
    assert "1 ready to paste" in capsys.readouterr().out


def test_doctor_names_a_careers_address_no_reader_understands(cfg):
    write_careers(cfg, "companies:\n  - {company: Gamma, url: 'https://gamma.example.com'}\n  - {company: Acme, url: 'https://jobs.lever.co/acme'}\n")
    rows = {name.split(":")[0].split(" (")[0]: (ok, hint) for name, ok, hint in setup.careers_checks(cfg)}
    ok, hint = rows["careers_urls.yaml"]
    assert ok is setup.WARN and "no reader for gamma.example.com" in hint
    assert any(name.startswith("public board list") and "CC BY-NC 4.0" in hint for name, _, hint in setup.careers_checks(cfg))
    assert any(name.startswith("Reed API key") for name, _, _ in setup.careers_checks(cfg))
