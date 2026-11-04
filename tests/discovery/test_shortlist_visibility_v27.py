"""v2.7 shortlist visibility: (1) it says when the alerts were last read, and `careerops daily` is discover + shortlist; (2) alerts that could not be traced to the
employer are their own block with `c n` / `s c n`; (3) `shortlist --all` lists every undecided in-region ad again. No network, no model."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from test_shortlist import build, five, fb, inbox, names, roles, run_with  # noqa: F401  (fixtures and helpers of the shortlist tests)
from triage_fakes import NOW, write_ad

from careerops import cli
from careerops.discovery import discover as disc, shortlist
from careerops.discovery.http import Fetcher
from careerops.discovery.robots import RobotsCache
from netfake import Net

READ, LATER = "2026-10-26T08:00:00+00:00", "2026-10-27T08:00:00+00:00"


def local(ts: str) -> str:
    return f"{datetime.fromisoformat(ts).astimezone():%Y-%m-%d %H:%M}"


def write_runs(settings, *runs):
    path = settings.path("discover") / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"dry_run": False, "sources": {}, "failures": [], "notes": [], **r}) + "\n" for r in runs), encoding="utf-8")


def config_file(settings, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------ 1. what it did not read
def test_the_last_run_without_the_alerts_is_said_with_the_time_they_were_last_read(settings):
    write_runs(settings, {"ts": READ, "asked": ["ats", "gmail_alerts"]}, {"ts": LATER, "asked": ["ats"]})
    assert shortlist.unread_lines(settings) == [f"alerts not read since {local(READ)}: run discover --gmail-alerts"]


def test_nothing_is_said_when_the_last_run_read_them(settings):
    write_runs(settings, {"ts": READ, "asked": ["ats"]}, {"ts": LATER, "asked": ["gmail_alerts"]})
    assert shortlist.unread_lines(settings) == []


def test_a_run_whose_gmail_failed_did_not_read_them(settings):
    write_runs(settings, {"ts": READ, "asked": ["gmail_alerts"]},
               {"ts": LATER, "asked": ["gmail_alerts", "ats"], "failures": ["gmail: not signed in to Gmail: run `careerops discover --gmail-auth` first"]})
    assert shortlist.unread_lines(settings) == [f"alerts not read since {local(READ)}: run discover --gmail-alerts"]


def test_no_run_ever_read_them(settings):
    assert shortlist.unread_lines(settings) == ["alerts not read: no recorded discover run has read them yet: run discover --gmail-alerts"]
    write_runs(settings, {"ts": LATER, "asked": ["ats"]})
    assert shortlist.unread_lines(settings) == ["alerts not read: no recorded discover run has read them yet: run discover --gmail-alerts"]


def test_a_run_recorded_before_runs_said_what_they_were_asked_for_counts_by_its_gmail_counts(settings):
    write_runs(settings, {"ts": READ, "sources": {"gmail": {"alerts": 4}}}, {"ts": LATER, "sources": {"ats": {"seen": 9}}})
    assert shortlist.unread_lines(settings) == [f"alerts not read since {local(READ)}: run discover --gmail-alerts"]
    write_runs(settings, {"ts": READ, "sources": {"ats": {"seen": 9}}}, {"ts": LATER, "sources": {"gmail": {"alerts": 4}}})
    assert shortlist.unread_lines(settings) == []


def test_the_run_record_says_what_the_run_was_asked_for():
    assert disc.Run(asked=["ats", "gmail_alerts"]).as_json()["asked"] == ["ats", "gmail_alerts"]
    assert disc.last_alert_read([]) == (False, None)


def test_the_line_comes_first_whatever_else_is_printed(settings, db, fb, inbox):
    five(inbox)
    out = []
    shortlist.run(settings, db, fb, ask=None, out=out.append, now=NOW, archive=[], preface=["alerts not read since X: run discover --gmail-alerts"])
    assert out[0].splitlines()[0] == "alerts not read since X: run discover --gmail-alerts" and "Shortlist:" in out[0]
    out2 = []
    shortlist.run(settings, db, fb, ask=None, out=out2.append, now=NOW, archive=[])                    # with no preface the output is what it was
    assert out2[0].startswith("Shortlist:")


def test_the_command_prints_it_and_prints_nothing_when_the_alerts_were_read(settings, db, inbox, tmp_path, capsys):
    path = config_file(settings, tmp_path)
    write_runs(settings, {"ts": READ, "asked": ["gmail_alerts"]}, {"ts": LATER, "asked": ["ats"]})
    cli.main(["--config", path, "shortlist", "--dry-run", "--no-liveness"])
    assert f"alerts not read since {local(READ)}: run discover --gmail-alerts" in capsys.readouterr().out
    write_runs(settings, {"ts": READ, "asked": ["ats"]}, {"ts": LATER, "asked": ["ats", "gmail_alerts"]})
    cli.main(["--config", path, "shortlist", "--dry-run", "--no-liveness"])
    assert "alerts not read" not in capsys.readouterr().out


# ------------------------------------------------------------------ `careerops daily`
@pytest.fixture
def calls(monkeypatch):
    got = []
    monkeypatch.setattr(cli, "cmd_discover", lambda a: got.append(("discover", a)))
    monkeypatch.setattr(cli, "cmd_shortlist", lambda a: got.append(("shortlist", a)))
    return got


def test_daily_is_discover_with_ats_alerts_and_report_then_the_shortlist(settings, tmp_path, calls):
    cli.main(["--config", config_file(settings, tmp_path), "daily", "--region", "uk", "--size", "sme", "--yes", "--limit", "5", "--no-liveness", "--all"])
    assert [n for n, _ in calls] == ["discover", "shortlist"]
    d, s = calls[0][1], calls[1][1]
    assert d.ats and d.gmail_alerts and d.report and not (d.reed or d.adzuna or d.jobbank or d.gmail_auth or d.mock)
    assert (d.region, d.limit, d.yes, d.no_liveness, d.dry_run) == ("uk", 5, True, True, False)
    assert (s.region, s.size, s.yes, s.all, s.no_liveness, s.mock, s.dry_run) == ("uk", "sme", True, True, True, False, False)


def test_a_dry_run_is_a_dry_run_for_both(settings, tmp_path, calls):
    cli.main(["--config", config_file(settings, tmp_path), "daily", "--dry-run"])
    assert calls[0][1].dry_run and calls[1][1].dry_run


def test_a_discover_that_fails_does_not_stop_the_shortlist_and_its_exit_code_stays(settings, tmp_path, monkeypatch, capsys):
    got = []

    def boom(args):
        got.append("discover")
        raise SystemExit(1)
    monkeypatch.setattr(cli, "cmd_discover", boom)
    monkeypatch.setattr(cli, "cmd_shortlist", lambda a: got.append("shortlist"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", config_file(settings, tmp_path), "daily"])
    assert e.value.code == 1 and got == ["discover", "shortlist"]
    assert "discover ended with exit code 1" in capsys.readouterr().err


def test_daily_has_no_mock_because_discover_reads_the_network(settings, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", config_file(settings, tmp_path), "daily", "--mock"])
    assert e.value.code == 2


def test_discover_records_what_it_was_asked_for(settings, db, monkeypatch):
    from careerops.discovery.discover import Discover
    from netfake import Resp
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli, "_print_health", lambda *a, **k: [])
    monkeypatch.setattr(Discover, "from_gmail", lambda self: None)
    monkeypatch.setattr(Discover, "from_boards", lambda self: None)
    monkeypatch.setattr(Discover, "ats_report", lambda self: [])
    from careerops.discovery import http
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp("", 404))
    cli.main(["discover", "--ats"])
    assert shortlist.unread_lines(settings)                                          # the last run did not ask for the alerts
    cli.main(["discover", "--gmail-alerts"])
    assert disc.read_runs(settings)[-1]["asked"] == ["gmail_alerts"] and shortlist.unread_lines(settings) == []


# ------------------------------------------------------------------ 2. alerts that could not be traced to the employer
def link(n: int) -> str:
    return f"https://www.linkedin.com/jobs/view/{1000 + n}"


def queue(settings, *rows, overflow=False, days_ago=1.0):
    base = {"ts": (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds"), "source": "linkedin", "company": "", "title": "Data Scientist",
            "location": "London, UK", "link": "", "reason": "no public job board known for the company", "apply_kind": "aggregator_only"}
    columns = disc.OVERFLOW_COLUMNS if overflow else disc.MANUAL_COLUMNS
    disc.append_rows(disc.overflow_path(settings) if overflow else disc.manual_queue_path(settings), columns, [{**base, **r} for r in rows])


def twelve(settings):
    queue(settings, *({"company": f"Co{i:02d}", "link": link(i)} for i in range(1, 10)))                                   # a day old: the best T
    queue(settings, {"company": "Co10", "link": link(10)}, days_ago=5)                                                       # a little older
    queue(settings, {"company": "Co11", "link": link(11)}, {"company": "Co12", "link": link(12)}, days_ago=20, overflow=True)    # older still, in the overflow file


def test_the_block_lists_the_best_ten_by_t_as_company_title_board_link(settings, db, fb, inbox):
    twelve(settings)
    plan = build(settings, db, fb)
    assert [a.company for a in plan.alert_rows] == [f"Co{i:02d}" for i in range(1, 11)] and plan.alert_total == 12
    lines = shortlist.alert_lines(plan)
    assert lines[0].startswith("From alerts, apply on the board (12, the top 10 by T): company | title | board | link")
    assert "[c n]" in lines[0] and "careerops live --clipboard" in lines[0]
    assert lines[1].startswith("  c1. Co01 | Data Scientist | linkedin | https://www.linkedin.com/jobs/view/1001  (T ") and len(lines) == 11
    ts = [a.t.t for a in plan.alert_rows]
    assert ts == sorted(ts, reverse=True)


def test_a_short_block_just_counts_its_rows(settings, db, fb, inbox):
    queue(settings, {"company": "Alpha", "link": link(1)}, {"company": "Beta", "link": link(2), "source": "indeed", "apply_kind": "workday"})
    plan = build(settings, db, fb)
    text = "\n".join(shortlist.alert_lines(plan))
    assert text.startswith("From alerts, apply on the board (2):") and "Beta | Data Scientist | indeed/workday | " in text


def test_the_block_is_printed_with_the_list_and_when_nothing_is_new(settings, db, fb, inbox):
    five(inbox)
    queue(settings, {"company": "Zeta", "link": link(1)})
    plan, out, _ = run_with(settings, db, fb, "q")
    assert "From alerts, apply on the board (1):" in "\n".join(out) and "  c1. Zeta | Data Scientist | linkedin" in "\n".join(out)
    _, again, seen = run_with(settings, db, fb, "q")                                                                          # nothing new: only the block is asked about
    assert again[0].startswith("No new jobs since") and "c1. Zeta" in again[0] and seen and seen[0].startswith("Nothing left to approve") and "[c n]" in seen[0]


def test_an_alert_mail_with_no_company_or_no_link_is_not_a_job(settings, db, fb, inbox):
    queue(settings, {"company": "", "title": "Job alert: data scientist", "link": "https://mail.google.com/mail/u/0/#all/abc"},
          {"company": "NoLink", "link": ""}, {"company": "Fine", "link": link(1)}, {"company": "Fine again", "link": link(1)})
    assert [a.company for a in build(settings, db, fb).alert_rows] == ["Fine"]                                                # the same link twice is one row


def test_what_is_in_hand_decided_or_dismissed_is_not_listed(settings, db, fb, inbox):
    write_ad(inbox, "in.txt", company="Inboxed", age_days=1)
    write_ad(shortlist.approved_root(settings) / "2026-10-27", "ap.txt", company="Approved", age_days=1)
    write_ad(settings.path("jds_done") / "skipped", "sk.txt", company="Skipped", url=link(3))
    db.upsert_job({"id": "manual:d1", "source": "manual", "company": "InDb", "title": "Data Scientist", "description": "x"})
    shortlist.log_events(settings, [{"event": "parked", "key": shortlist._key("Dismissed", "Data Scientist"), "company": "Dismissed", "title": "Data Scientist"}], NOW)
    queue(settings, *({"company": c, "link": link(i)} for i, c in enumerate(["Inboxed", "Approved", "Skipped", "InDb", "Dismissed", "Fresh"], 1)))
    assert [a.company for a in build(settings, db, fb).alert_rows] == ["Fresh"]


def test_a_job_you_skipped_with_only_the_same_company_and_title_is_listed_as_a_possible_repost(settings, db, fb, inbox):
    db.upsert_job({"id": "manual:g1", "source": "manual", "company": "Gone", "title": "Data Scientist", "description": "x"})
    db.update_job("manual:g1", status="skipped")
    queue(settings, {"company": "Gone", "link": link(1)})
    (row,) = build(settings, db, fb).alert_rows
    assert "possible repost of a job you skipped" in row.line(1)


def test_the_region_and_the_age_rules_apply_and_a_blacklisted_company_is_left_out(settings, db, fb, inbox, monkeypatch, tmp_path):
    from careerops.discovery import blacklist
    (tmp_path / "bl.yaml").write_text("blacklist:\n  - {name: Blocked Ltd, scope: company, reason: test}\n", encoding="utf-8")
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "bl.yaml")
    queue(settings, {"company": "Abroad", "link": link(1), "location": "New York, NY"}, {"company": "Home", "link": link(2)},
          {"company": "Blocked Ltd", "link": link(3)})
    queue(settings, {"company": "Stale", "link": link(4)}, days_ago=90)
    assert [a.company for a in build(settings, db, fb, regions=("uk",)).alert_rows] == ["Home"]
    assert sorted(a.company for a in build(settings, db, fb).alert_rows) == ["Abroad", "Home"]                              # no region chosen: no region filter


def test_the_board_page_is_never_fetched(settings, db, fb, inbox):
    queue(settings, {"company": "Alpha", "link": link(1)}, {"company": "Beta", "link": "https://uk.indeed.com/viewjob?jk=abc", "source": "indeed"})
    net = Net(strict=True)
    fetcher = Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)
    plan = shortlist.build(settings, db, fb, fetcher=fetcher, now=NOW, archive=[])
    assert len(plan.alert_rows) == 2 and net.requests == []


def test_the_block_is_printed_in_a_dry_run_and_without_a_terminal(settings, db, fb, inbox):
    queue(settings, {"company": "Alpha", "link": link(1)})
    for kw in ({"dry_run": True, "ask": lambda p: pytest.fail("asked in a dry run")}, {"ask": None}):
        out = []
        shortlist.run(settings, db, fb, out=out.append, now=NOW, archive=[], **kw)
        assert "From alerts, apply on the board (1)" in "\n".join(out)


def test_the_parser_knows_the_alert_answers():
    p = shortlist.parse
    assert p("c2", 5, 3) == ("copy", [2]) and p("c 2", 5, 3) == ("copy", [2]) and p("C 1 3", 5, 3) == ("copy", [1, 3])
    assert p("s c1", 5, 3) == ("dismiss", [1]) and p("s c 2", 5, 3) == ("dismiss", [2]) and p("s c1 c2", 5, 3) == ("bad", [])
    assert p("c4", 5, 3) == ("bad", []) and p("c", 5, 3) == ("bad", []) and p("s c9", 5, 3) == ("bad", [])
    assert p("c1", 5) == ("bad", []) and p("c 1", 5)[0] == "bad"                                                   # no alert block: not an answer
    assert p("s 2", 5, 3) == ("skip", [2]) and p("1 3", 5, 3) == ("approve", [1, 3]) and p("a", 5, 3) == ("all", [])


def test_c_says_what_to_do_and_asks_nothing_of_the_network(settings, db, fb, inbox):
    queue(settings, {"company": "Alpha", "link": link(1)}, {"company": "Beta", "link": link(2)})
    plan, out, seen = run_with(settings, db, fb, "c1", "c 2", "c9", "q")
    text = "\n".join(out)
    assert f"open {link(1)} in your browser, copy the whole ad, then run  careerops live --clipboard" in text and f"open {link(2)} in your browser" in text
    assert "Company: Alpha" in text and "never opens or fetches that page" in text and "not understood" in text
    assert not shortlist.approved_files(settings)


def test_s_c_dismisses_an_alert_for_good_and_unskip_shortlist_brings_it_back(settings, db, fb, inbox):
    queue(settings, {"company": "Alpha", "link": link(1)}, {"company": "Beta", "link": link(2)})
    plan, out, _ = run_with(settings, db, fb, "s c1", "c1", "q")
    text = "\n".join(out)
    assert "dismissed 1: not listed again" in text and "c1: already dismissed" in text
    assert [a.company for a in build(settings, db, fb).alert_rows] == ["Beta"]
    parked = [json.loads(line) for line in shortlist.log_path(settings).read_text(encoding="utf-8").splitlines()]
    assert [e["company"] for e in parked if e["event"] == "parked"] == ["Alpha"]
    assert shortlist.restore(settings, match="alpha") == ["offered again: Alpha | Data Scientist (was parked)"]
    assert sorted(a.company for a in build(settings, db, fb).alert_rows) == ["Alpha", "Beta"]


def test_with_ads_to_approve_the_alert_prompt_follows_the_approvals(settings, db, fb, inbox):
    five(inbox)
    queue(settings, {"company": "Zeta", "link": link(1)})
    plan, out, seen = run_with(settings, db, fb, "a", "c1", "q")
    assert len(shortlist.approved_files(settings)) == 5 and seen[0].startswith("[a] approve all") and "[c n] how to apply on the board" in seen[0]
    assert seen[1].startswith("Nothing left to approve") and "[s c n]" in seen[1]


# ------------------------------------------------------------------ 3. --all
def park_five(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    for i in range(2):
        write_ad(inbox, f"n{i}.txt", company=f"Newco{i}", age_days=1)
        run_with(settings, db, fb, "q")


def test_all_also_lists_the_ads_parked_after_three_showings(settings, db, fb, inbox):
    park_five(settings, db, fb, inbox)
    assert len(build(settings, db, fb).parked) == 5 and "Alpha" not in names(build(settings, db, fb))
    plan = build(settings, db, fb, show_all=True)
    assert "Alpha" in names(plan) and plan.parked == [] and plan.show_all


def test_all_prints_the_list_even_when_nothing_is_new_and_still_takes_choices(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    _, plain, seen_plain = run_with(settings, db, fb)
    assert plain[0].startswith("No new jobs since") and seen_plain == []
    answers = iter(["1", "q"])
    plan = shortlist.run(settings, db, fb, ask=lambda p: next(answers), out=(out := []).append, now=NOW, archive=[], show_all=True)
    text = "\n".join(out)
    assert "Shortlist:" in text and "new since last shortlist: 0 (--all" in text and text.count("(shown 1×)") == 5 and plan.new == 0
    assert len(shortlist.approved_files(settings)) == 1


def test_all_is_a_look_not_a_showing_so_it_never_parks_anything(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    before = shortlist.log_path(settings).read_text(encoding="utf-8")
    for _ in range(4):
        shortlist.run(settings, db, fb, ask=lambda p: "q", out=lambda s: None, now=NOW, archive=[], show_all=True)
    assert shortlist.log_path(settings).read_text(encoding="utf-8") == before and not build(settings, db, fb).parked


def test_all_keeps_every_other_rule_out_of_region_decided_and_capped(settings, db, fb, inbox):
    five(inbox)
    write_ad(inbox, "skipped.txt", company="Gone", age_days=1)
    write_ad(settings.path("jds_done") / "skipped", "old.txt", company="Gone")
    assert "Gone" not in names(build(settings, db, fb, show_all=True))
    assert len(build(settings, db, fb, show_all=True).rows) == 5
    plan = build(settings, db, fb, show_all=True, regions=("us",))                                        # London ads are outside the United States: parked (region), not listed
    assert plan.rows == [] and len(plan.region_parked) >= 5
    for i in range(12):
        write_ad(inbox, f"x{i}.txt", company=f"Extra{i}", age_days=1)
    assert len(build(settings, db, fb, show_all=True).rows) == 8                                         # targeting.shortlist_size still caps the list


def test_the_command_has_all_and_passes_it_on(settings, db, fb, inbox, tmp_path, capsys):
    path = config_file(settings, tmp_path)
    for i in range(3):
        write_ad(inbox, f"{i}.txt", company=f"Company{i}", age_days=1, base=datetime.now(timezone.utc))
    cli.main(["--config", path, "shortlist", "--dry-run", "--no-liveness", "--all"])
    assert "(--all: every undecided in-region ad is listed" in capsys.readouterr().out
