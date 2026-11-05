"""v2.7 item 5: a board slug is GUESSED from a company name only for the providers whose reply names the company (Workable, SmartRecruiters, Teamtailor,
Recruitee); Personio and Pinpoint are used only from a real link; and every refused request is counted per provider, run over run. No network."""
from datetime import datetime, timezone

import pytest
from netfake import Net, Resp
from test_board_check import cfg  # noqa: F401  (a settings copy with no listed boards and no alert probes)

from careerops.discovery import boards as boards_mod, discover as disc, more_ats
from careerops.discovery.discover import Discover, Run
from careerops.discovery.http import Blocked, Fetcher
from careerops.discovery.robots import RobotsCache
from careerops.discovery.seeds import Seed

TODAY = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)


def fx(net, **kw):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, **kw)


def seed(name="Acme Analytics"):
    return Seed(name, "archive")


# ------------------------------------------------------------------ which providers are guessed
def test_only_the_providers_whose_reply_names_the_company_are_guessed():
    assert more_ats.NAME_CHECKED == ("workable", "smartrecruiters", "teamtailor", "recruitee")
    assert more_ats.LINK_ONLY == ("personio", "pinpoint") and more_ats.CONFIG_ONLY == ("eploy",)
    assert {"personio", "pinpoint", "eploy"} <= set(more_ats.SINGLE)                      # still boards this tool can read: from a link or sources:


def test_a_personio_slug_is_never_guessed_even_when_the_settings_name_it(cfg):
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "personio", "recruitee"]
    index = boards_mod.BoardIndex(cfg)
    assert index.more == ("workable",) and index.not_guessed == ["recruitee"]          # v3.0: Recruitee has one host per company: never guessed
    net = Net(strict=False)
    boards_mod.find(seed(), fx(net), index)
    assert net.urls() and not [u for u in net.urls(True) if "personio" in u]
    problems = [p for p in more_ats.config_problems(cfg) if "personio" in p]
    assert problems and "never guessed" in problems[0] and "remove it" in problems[0]


def test_the_default_providers_ask_no_personio_host_either(cfg):
    cfg.raw["discover"]["ats"].pop("providers", None)
    index = boards_mod.BoardIndex(cfg)
    assert index.more == ("workable", "smartrecruiters")                                # v3.0: NAME_CHECKED without Teamtailor and Recruitee (one host per company)
    net = Net(strict=False)
    boards_mod.find(seed("Acme Analytics Ltd"), fx(net), index)
    assert not [u for u in net.urls(True) if "personio" in u or "pinpointhq" in u]


def test_personio_and_pinpoint_boards_are_still_found_from_a_real_link():
    p = more_ats.link_board("https://acme-analytics.jobs.personio.com/job/8800001?language=en")
    assert (p.ats, p.slug, p.ref) == ("personio", "acme-analytics.jobs.personio.com", "8800001")
    q = more_ats.link_board("https://acme.pinpointhq.com/postings/7e0d2a1c-1f2e-4c1b-9a0e-1b2c3d4e5f60")
    assert (q.ats, q.slug) == ("pinpoint", "acme") and q.ref == "7e0d2a1c-1f2e-4c1b-9a0e-1b2c3d4e5f60"


def test_a_board_you_name_under_sources_is_still_read(cfg):
    cfg.raw["sources"]["personio"] = [{"slug": "acme-analytics", "name": "Acme Analytics"}]
    from careerops.discovery import ats as ats_mod
    assert ("personio", "acme-analytics") in {(b.ats, b.slug) for b in ats_mod.configured_boards(cfg)}


# ------------------------------------------------------------------ refused requests per provider
def test_every_refusal_is_counted_per_provider_whether_it_stops_the_provider_or_not(cfg):
    index = boards_mod.BoardIndex(cfg)
    index.stop("workable", Blocked("HTTP 429", 429))
    index.stop("workable", Blocked("robots.txt disallows it"))
    index.stop("teamtailor", Blocked("robots.txt disallows it"))
    assert index.refused_counts() == {"teamtailor": 1, "workable": 2}
    assert "workable" in index.stopped and "teamtailor" not in index.stopped              # the old behaviour is unchanged


def test_a_refused_greenhouse_request_is_counted_too(cfg):
    index = boards_mod.BoardIndex(cfg)
    net = Net(strict=False).add("boards-api.greenhouse.io", Resp("", 403))
    boards_mod.find(seed(), fx(net), index)
    assert index.refused_counts().get("greenhouse", 0) >= 1


def test_the_run_record_carries_the_counts_of_the_run(cfg, db):
    d = Discover(cfg, db, fx(Net(strict=False)), today=TODAY, say=lambda s: None)
    d.index.refusals["workable"] = 3
    d.finish()
    assert d.run.refused == {"workable": 3} and d.run.as_json()["refused"] == {"workable": 3}


def test_the_line_shows_before_and_after_per_provider():
    runs = [{"refused": {"workable": 31, "personio": 52, "teamtailor": 2}}, {"refused": {"workable": 0, "teamtailor": 2}}]
    assert disc.refused_lines(runs) == ["Refused requests per provider, before -> after: personio 52 -> 0 · teamtailor 2 -> 2 · workable 31 -> 0"]
    assert disc.refused_lines(runs[:1], {"workable": 4}) == ["Refused requests per provider, before -> after: "
                                                            "personio 52 -> 0 · teamtailor 2 -> 0 · workable 31 -> 4"]            # the run in hand against the last one recorded


def test_a_run_recorded_before_the_counts_existed_is_read_from_its_notes():
    old = {"notes": ["ats: workable refused 31 request(s) (not asked again where it said no): robots.txt disallows", "ats: personio refused 21 request(s) (x)",
                     "ats: personio refused 5 request(s) (y)", "ats: recruitee stopped for this run: HTTP 429", "unrelated"]}
    assert disc.refused_of(old) == {"workable": 31, "personio": 26, "recruitee": 1}
    assert disc.refused_lines([old, {"refused": {}}]) == ["Refused requests per provider, before -> after: personio 26 -> 0 · recruitee 1 -> 0 · workable 31 -> 0"]


def test_one_run_alone_and_a_clean_pair_are_said_plainly():
    assert disc.refused_lines([{"refused": {"workable": 2}}]) == ["Refused requests per provider (first run recorded): workable 2"]
    assert disc.refused_lines([{"refused": {}}, {"refused": {}}]) == ["Refused requests per provider, before -> after: none in either run"]
    assert disc.refused_lines([]) == []


def test_the_report_and_the_run_print_the_line(cfg, db, capsys):
    for refused in ({"workable": 31, "personio": 52}, {"workable": 0}):
        run = Run(started=TODAY.isoformat(timespec="seconds"))
        run.refused = refused
        disc.save_run(cfg, run)
    assert "Refused requests per provider, before -> after: personio 52 -> 0 · workable 31 -> 0" in disc.format_report(cfg)


def test_the_discover_command_prints_the_line_and_records_what_it_was_asked_for(cfg, db, monkeypatch, capsys):
    from careerops import cli
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli, "_print_health", lambda *a, **k: [])
    monkeypatch.setattr(Discover, "from_gmail", lambda self: None)
    monkeypatch.setattr(Discover, "from_boards", lambda self: setattr(self.index, "refusals", {"workable": 7}))
    monkeypatch.setattr(Discover, "ats_report", lambda self: [])
    from careerops.discovery import http
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp("", 404))
    disc.save_run(cfg, Run(started="2026-11-03T09:30:00+00:00", refused={"workable": 31}))
    cli.main(["discover", "--ats", "--gmail-alerts"])
    assert "Refused requests per provider, before -> after: workable 31 -> 7" in capsys.readouterr().out
    last = disc.read_runs(cfg)[-1]
    assert last["asked"] == ["gmail_alerts", "ats"] and last["refused"] == {"workable": 7}
