"""v2.8, fixes from the 5 Nov daily run. (1) The alert block of the shortlist: ranked by A, Fr and V then the title match (alerts have no ad text), E shown as `E ?`,
the same job in two alerts one row with both links, a CV-Library job page marked `agency or board-only`. (2) Slug guessing: names and slugs that are not companies
are not asked, "no board" is kept per (provider, slug), a robots.txt redirect on a company-per-host provider is board none and not a refusal. (3) At most
discover.ats.max_guesses_per_provider tokens per provider per run, with the guesses and requests per provider in the report. (4) Why board_link and board_known
resolved nothing: five example alerts with what was tried. No network: `Net` answers."""
import json
from datetime import datetime, timezone

import pytest
import requests
from netfake import Net, Resp
from test_board_check import TODAY, cfg, fetcher as fx, make  # noqa: F401  (cfg: a settings copy with no listed boards and no alert probes)
from test_shortlist import build, fb, inbox, roles, run_with  # noqa: F401  (fixtures and helpers of the shortlist tests)
from test_shortlist_visibility_v27 import link, queue

from careerops import cli
from careerops.discovery import ats as ats_mod, boards as boards_mod, discover as disc, more_ats, shortlist, triage
from careerops.discovery.boards import BoardIndex
from careerops.discovery.discover import Discover
from careerops.discovery.http import Blocked, RobotsRedirected
from careerops.discovery.seeds import Seed

ALL_PROVIDERS = {"greenhouse", "lever", "lever_eu", "ashby", "workable"}          # v3.0: Teamtailor and Recruitee (one host per company) are not guessed any more


@pytest.fixture
def cfg_all(cfg):
    """The test settings with the v2.6 providers that are guessed by name switched on (the shipped test settings ask none of them)."""
    cfg.raw["discover"]["ats"]["providers"] = ["workable", "teamtailor", "recruitee"]
    return cfg


def ashby_tokens(net) -> list[str]:
    """The board tokens that were asked of Ashby (its address ends in the token)."""
    return [u.rsplit("/", 1)[-1] for u in net.urls() if "api.ashbyhq.com" in u]


def robots_redirect(url, **kw):
    """A provider whose robots.txt only redirects to its own site: what a company-per-host provider answers for a name nobody has."""
    return Resp("", 301, {"Location": "https://www.example-provider.com/"}) if url.endswith("/robots.txt") else Resp("", 404)


# ------------------------------------------------------------------ 1. the alert block
def test_alerts_are_ranked_by_a_fr_and_v_and_e_is_written_as_unknown(settings, db, fb, inbox):
    queue(settings, {"company": "Mid", "title": "Forward Deployed Engineer", "link": link(1)})                      # A4, a day old
    queue(settings, {"company": "Top", "title": "Gen AI Engineer", "link": link(2)}, days_ago=8)                    # A5, a week old
    queue(settings, {"company": "Low", "title": "Computer Vision Engineer", "link": link(3)})                       # A3, a day old
    plan = build(settings, db, fb)
    assert [a.company for a in plan.alert_rows] == ["Top", "Mid", "Low"]
    ranks = [a.rank for a in plan.alert_rows]
    assert ranks == sorted(ranks, reverse=True) and plan.alert_rows[0].rank == pytest.approx(plan.alert_rows[0].t.alert_rank())
    lines = shortlist.alert_lines(plan)[1:]
    assert all(" E ? " in line and " E3 " not in line for line in lines)                                              # not the neutral 3 it is scored with
    assert "A5 E ? V3.5" in lines[0]


def test_e_is_still_a_number_for_an_ad_that_has_text():
    t = triage.Triage(a=5, e=3.8, v=3.5, l=5, fr=4, p=3, t=4.1)
    assert t.text() == "T 4.1 = A5 E3.8 V3.5 L5 Fr4 P3" and t.text(e_unknown=True) == "T 4.1 = A5 E ? V3.5 L5 Fr4 P3"
    assert t.alert_rank() == pytest.approx(0.30 * 5 + 0.10 * 4 + 0.15 * 3.5)


def test_a_tie_on_a_fr_and_v_goes_to_the_better_title_match(settings, db, fb, inbox, monkeypatch):
    monkeypatch.setattr(shortlist.cand, "title_relevance", lambda title, s: 0.9 if "Remote" in title else 0.5)
    queue(settings, {"company": "Alpha", "title": "Data Scientist", "link": link(1)}, {"company": "Zed", "title": "Data Scientist (Remote)", "link": link(2)})
    plan = build(settings, db, fb)
    assert [a.company for a in plan.alert_rows] == ["Zed", "Alpha"] and plan.alert_rows[0].rank == plan.alert_rows[1].rank


def test_the_rank_leaves_out_what_an_alert_cannot_tell():
    """E is a constant for an alert and L / P are not what the order is about: two alerts that differ only there rank alike."""
    a = triage.Triage(a=4, e=3, v=3.5, l=5, fr=4, p=3, t=3.7)
    b = triage.Triage(a=4, e=1, v=3.5, l=2, fr=4, p=1, t=2.4)
    assert a.alert_rank() == b.alert_rank() and a.t != b.t
    assert triage.Triage(a=5, e=3, v=3.5, l=5, fr=3, p=3, t=0).alert_rank() > triage.Triage(a=4, e=3, v=3.5, l=5, fr=5, p=3, t=0).alert_rank()      # A outweighs Fr


def test_the_same_job_in_two_alerts_is_one_row_with_both_links(settings, db, fb, inbox):
    indeed = "https://uk.indeed.com/viewjob?jk=abc123def456"
    queue(settings, {"company": "IBM", "link": link(1), "source": "linkedin"}, {"company": "IBM", "link": indeed, "source": "indeed"},
          {"company": "Other", "link": link(2)})
    plan = build(settings, db, fb)
    assert plan.alert_total == 2
    (ibm,) = [a for a in plan.alert_rows if a.company == "IBM"]
    assert ibm.all_links == [link(1), indeed] and ibm.boards == ["linkedin", "indeed"]
    text = ibm.line(1)
    assert f"IBM | Data Scientist | linkedin, indeed | {link(1)} · {indeed}" in text
    assert f"open {link(1)} (or {indeed}) in your browser" in ibm.steps(1)


def test_dismissing_a_merged_row_dismisses_both_links(settings, db, fb, inbox):
    queue(settings, {"company": "IBM", "link": link(1)}, {"company": "IBM", "link": "https://uk.indeed.com/viewjob?jk=abc123def456", "source": "indeed"},
          {"company": "Other", "link": link(2)})
    run_with(settings, db, fb, "s c1", "q")
    assert [a.company for a in build(settings, db, fb).alert_rows] == ["Other"]


def test_rows_that_differ_in_company_or_title_are_not_merged(settings, db, fb, inbox):
    queue(settings, {"company": "IBM", "title": "Data Scientist", "link": link(1)}, {"company": "IBM", "title": "AI Engineer", "link": link(2)},
          {"company": "IBM Research", "title": "Data Scientist", "link": link(3)})
    assert build(settings, db, fb).alert_total == 3


def test_a_cv_library_job_page_is_marked_agency_or_board_only_and_the_block_says_so(settings, db, fb, inbox):
    page, tracker = "https://www.cv-library.co.uk/job/219034011", "https://clicks.cv-library.co.uk/f/a/abc~~/A~/B~~"
    queue(settings, {"company": "Agency Co", "source": "cvlibrary", "link": page}, {"company": "Tracked", "source": "cvlibrary", "link": tracker},
          {"company": "Board", "link": link(1)})
    plan = build(settings, db, fb)
    text = "\n".join(shortlist.alert_lines(plan))
    assert f"Agency Co | Data Scientist | cvlibrary [agency or board-only] | {page}" in text
    assert f"Tracked | Data Scientist | cvlibrary | {tracker}" in text and f"Board | Data Scientist | linkedin | {link(1)}" in text
    assert "[agency or board-only] = a CV-Library job page: the employer's own board was not found" in text
    (agency,) = [a for a in plan.alert_rows if a.company == "Agency Co"]
    assert agency.board_only and "agency or board-only, the employer's own board was not found" in agency.steps(1)
    assert [a.board_only for a in plan.alert_rows if a.company != "Agency Co"] == [False, False]


def test_no_note_without_a_cv_library_page(settings, db, fb, inbox):
    queue(settings, {"company": "Board", "link": link(1)})
    assert "agency or board-only" not in "\n".join(shortlist.alert_lines(build(settings, db, fb)))


# ------------------------------------------------------------------ 2. slugs that are not company names
@pytest.mark.parametrize("name", ["Data Scientist London SW1A 2AA", "Acme Analytics £50k", "Machine Learning Engineer 60,000 benefits"])
def test_a_name_that_is_not_a_company_is_asked_of_no_provider(cfg, name):
    net, index = Net(strict=False), BoardIndex(cfg)
    f = boards_mod.find(Seed(name, "alert"), fx(net), index)
    assert net.urls(True) == [] and f.tokens == [] and f.rejected and index.reject_summary()["count"] == len(f.rejected)
    assert "rejected:" in f.line() and f.tried_text().startswith("not asked, not a company name")


def test_a_slug_over_thirty_characters_is_not_asked_but_the_short_forms_of_the_name_are(cfg):
    net, index = Net(strict=False), BoardIndex(cfg)
    f = boards_mod.find(Seed("Imperial College Healthcare NHS Foundation Trust", "archive"), fx(net), index)
    assert f.tokens == ["imperial", "imperialcollege"] and len(f.rejected) == 2 and all("longer than 30" in why for _, why in f.rejected)
    assert ashby_tokens(net) == ["imperial", "imperialcollege"]


def test_the_rules_in_one_place():
    assert ats_mod.slug_reject_reason("a" * 31) and not ats_mod.slug_reject_reason("a" * 30)
    assert ats_mod.slug_reject_reason("salary60000") and ats_mod.slug_reject_reason("sw1a2aa")
    assert ats_mod.name_reject_reason("Data Scientist SW1A 2AA") == "the name contains a postcode"
    assert ats_mod.name_reject_reason("Hiring now £60k") == "the name contains a salary"
    for ok in ("Bet365", "360Learning", "N26 Bank", "AI21 Labs", "Web3 Studio", "10x Genomics", "Monzo Bank Ltd"):          # three digits or fewer, and no postcode shape
        assert ats_mod.name_reject_reason(ok) is None, ok


def test_a_folder_named_in_ignore_companies_is_never_asked(cfg):
    cfg.raw["discover"]["ignore_companies"] = ["New folder", "Acme"]
    net, index = Net(strict=False), BoardIndex(cfg)
    f = boards_mod.find(Seed("New folder", "alert"), fx(net), index)
    assert net.urls(True) == [] and "discover.ignore_companies" in f.rejected[0][1]
    g = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)                  # the company is not ignored, but its slug `acme` is a folder that is
    assert g.tokens == ["acmeanalytics", "acme-analytics"] and [t for t, _ in g.rejected] == ["acme"]
    assert ashby_tokens(net) == ["acmeanalytics", "acme-analytics"]


def test_a_slug_already_verified_for_the_company_is_asked_even_when_it_looks_odd(cfg):
    net = Net(strict=False)
    f = boards_mod.find(Seed("Whatever Ltd", "archive", hint="x12345678"), fx(net), BoardIndex(cfg))
    assert f.tokens[0] == "x12345678" and "x12345678" in ashby_tokens(net)


def test_an_alert_naming_a_postcode_costs_no_probe(cfg, db):
    cfg.raw["discover"]["alert_probes_per_run"] = 3
    d = make(cfg, db, Net(strict=False))
    assert d.lookup.boards_for("Data Scientist SW1A 2AA") == [] and d.lookup.probes == 3
    d.lookup.boards_for("Real Company Ltd")
    assert d.lookup.probes == 2


# ------------------------------------------------------------------ "no board" is kept per (provider, slug)
def test_no_board_is_believed_after_three_separate_runs_and_not_asked_again_after_that(cfg_all):
    cfg = cfg_all
    net = Net(strict=False)
    for run in (1, 2, 3):                                                                                         # v3.0: three separate runs must each find no board
        index = BoardIndex(cfg)
        f = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)
        counts = index.guess_counts()
        assert set(counts) == ALL_PROVIDERS and all(v["guesses"] == 3 and v["cached"] == 0 for v in counts.values())       # three tokens each, asked every time
        assert f.cached == set() and index.misses("ashby", "acme") == run
        again_now = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)                             # the same run: an answered token is not asked twice
        assert again_now.cached == {"acmeanalytics", "acme-analytics", "acme"} and index.guess_counts()["ashby"]["guesses"] == 3
        index.save()
    before = len(net.urls(True))
    fourth = BoardIndex(cfg)                                                                                      # the fourth run: "no board" is believed
    f = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), fourth)
    assert len(net.urls(True)) == before and f.boards == [] and f.cached == {"acmeanalytics", "acme-analytics", "acme"}
    assert all(v["guesses"] == 0 and v["cached"] == 3 for v in fourth.guess_counts().values())


def tenants_down(url, **kw):
    return Resp("", 404) if url.endswith("/robots.txt") else requests.ConnectionError("no such host")


def test_a_provider_with_one_host_per_company_is_not_guessed_at_all(cfg_all):
    cfg = cfg_all
    net = Net(strict=False).add("teamtailor.com", tenants_down).add("recruitee.com", tenants_down)
    index = BoardIndex(cfg)
    boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)                    # v3.0: Teamtailor and Recruitee are named in the settings and still never asked
    assert not [u for u in net.urls(True) if "teamtailor" in u or "recruitee" in u]
    assert index.more == ("workable",) and index.not_guessed == ["teamtailor", "recruitee"]
    assert any("teamtailor, recruitee in discover.ats.providers are no longer guessed" in n for n in index.notes())


def test_an_offline_run_remembers_nothing(cfg_all):
    cfg = cfg_all
    net = Net(strict=False).add("", tenants_down)
    boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), BoardIndex(cfg))
    after = BoardIndex(cfg)
    assert after.data == {} and after.get("teamtailor", "acmeanalytics") is None


# ------------------------------------------------------------------ a robots.txt that redirects
def test_a_redirected_robots_txt_on_a_fixed_host_is_still_a_refusal(cfg_all):
    cfg = cfg_all
    net = Net(strict=False).add("apply.workable.com", robots_redirect)
    index = BoardIndex(cfg)
    boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)
    assert index.refused_counts().get("workable", 0) >= 1                                   # Workable has one host: a redirect there is not "no such company"


def test_the_fetcher_raises_robots_redirected_which_is_still_a_blocked():
    net = Net().add("tenant.teamtailor.com/robots.txt", robots_redirect)
    assert issubclass(RobotsRedirected, Blocked)
    with pytest.raises(RobotsRedirected, match="robots.txt"):
        fx(net).request("https://tenant.teamtailor.com/jobs.rss")
    with pytest.raises(Blocked):
        more_ats.fetch("workable", "tenant", fx(Net().add("apply.workable.com/robots.txt", robots_redirect)))       # a fixed host: the refusal stands
    assert more_ats.fetch("teamtailor", "tenant", fx(Net().add("tenant.teamtailor.com/robots.txt", robots_redirect))).found is False


# ------------------------------------------------------------------ 3. the cap
def test_each_provider_is_asked_at_most_the_cap_a_run_and_the_next_run_carries_on(cfg_all):
    cfg = cfg_all
    cfg.raw["discover"]["ats"]["max_guesses_per_provider"] = 4
    cfg.raw["discover"]["ats"]["misses_needed"] = 1                                      # the count of runs is test_careers_v30's; here a miss is believed at once
    names = [f"Company{c}" for c in "ABCDEFGH"]                                          # one token each
    net, index = Net(strict=False), BoardIndex(cfg)
    findings = [boards_mod.find(Seed(n, "archive"), fx(net), index) for n in names]
    counts = index.guess_counts()
    assert set(counts) == ALL_PROVIDERS and all(v["guesses"] == 4 and v["over_cap"] == 4 for v in counts.values())
    assert len(ashby_tokens(net)) == 4 and index.get("ashby", "companye") is None             # not asked, so not remembered
    assert "not asked (guess limit)" in findings[-1].line() and findings[-1].cached == set()
    assert any("guesses stopped at the limit of 4 a run" in n and "4 token(s) left" in n for n in index.notes())
    second = BoardIndex(cfg)                                                              # the next run: the first four are cached, the other four are asked
    for n in names:
        boards_mod.find(Seed(n, "archive"), fx(net), second)
    assert sorted(ashby_tokens(net)) == sorted(n.lower() for n in names)                   # every token asked of Ashby exactly once over the two runs
    assert all(v["guesses"] == 4 and v["cached"] == 4 for v in second.guess_counts().values())


def test_the_default_cap_is_twenty_it_cannot_be_set_higher_and_zero_asks_nothing(cfg_all):
    cfg = cfg_all
    cfg.raw["discover"]["ats"].pop("max_guesses_per_provider")
    assert BoardIndex(cfg).max_guesses == boards_mod.MAX_GUESSES == 20
    cfg.raw["discover"]["ats"]["max_guesses_per_provider"] = 60                          # v3.0: "at most 20 per provider per run"
    assert BoardIndex(cfg).max_guesses == boards_mod.GUESS_CEILING == 20
    cfg.raw["discover"]["ats"]["max_guesses_per_provider"] = 0
    net = Net(strict=False)
    f = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), BoardIndex(cfg))
    assert net.urls(True) == [] and f.capped == ALL_PROVIDERS


def test_the_run_record_and_the_report_carry_the_guesses_and_requests_per_provider(cfg, db):
    d = make(cfg, db, Net(strict=False))
    boards_mod.find(Seed("Acme Analytics", "archive"), d.f, d.index)
    boards_mod.find(Seed("Data Scientist SW1A 2AA", "alert"), d.f, d.index)
    d.finish()
    rec = d.run.as_json()
    assert rec["guess_cap"] == 20 and rec["guesses"]["ashby"]["guesses"] == 3 and rec["guesses"]["ashby"]["requests"] >= 3
    assert rec["slug_rejected"]["count"] >= 1 and "postcode" in rec["slug_rejected"]["examples"][0]
    first, second = disc.guess_lines(rec)
    assert first.startswith("Slug guesses per provider this run (limit 20 each): ") and "ashby 3 guess(es)," in first and "request(s)" in first
    assert second.startswith("Slugs not asked because they are not company names: ") and "postcode" in second
    assert disc.guess_lines({}) == []


# ------------------------------------------------------------------ 4. why board_link and board_known resolved nothing
def resolve(d, company, title, link_, source="linkedin", kind=None):
    return d.resolve_link("gmail", kind or source, company, title, "London, UK", link_, f"gmail:{source}", source_id=source)


@pytest.fixture
def six(cfg_all, db):
    cfg = cfg_all
    cfg.raw["discover"]["alert_probes_per_run"] = 5
    net = Net(strict=False)
    d = make(cfg, db, net)
    resolve(d, "Zeta Analytics", "Data Scientist", "https://www.linkedin.com/jobs/view/1001")
    resolve(d, "", "Data Scientist", "https://uk.indeed.com/viewjob?jk=abc123def456", source="indeed")
    resolve(d, "Data Scientist SW1A 2AA", "Data Scientist", "https://www.linkedin.com/jobs/view/1002")
    resolve(d, "Acme Analytics", "Data Scientist", "https://apply.workable.com/acme/j/ABCDEF1234/", source="reed")
    resolve(d, "Mu Systems", "Data Scientist", "https://www.cv-library.co.uk/job/219034011", source="cv-library", kind="cvlibrary")
    resolve(d, "Nu Works", "Data Scientist", "https://clicks.cv-library.co.uk/f/a/abc~~/A~/B~~", source="cv-library", kind="cvlibrary")
    d.run.count("gmail", "alerts", 6)
    d.finish()
    return d, net


def test_every_unresolved_alert_keeps_what_was_tried_and_why(six):
    d, net = six
    rows = {r["company"]: r for r in d.run.unresolved}
    assert [r["category"] for r in d.run.unresolved] == ["no_board", "no_company", "not_a_company", "no_board", "no_board", "no_board"]
    zeta = rows["Zeta Analytics"]
    assert zeta["tried"][0] == "reed: not a Reed job link"
    assert zeta["tried"][1] == "board_link: www.linkedin.com is never fetched by this tool, and the link names no employer board"
    assert zeta["tried"][2].startswith("board_known: no board is listed for 'Zeta Analytics'")
    assert ("board_guessed: tokens zetaanalytics, zeta-analytics, zeta asked of greenhouse, lever, lever_eu, ashby, workable: none is a board"
            in zeta["tried"][2]) and zeta["why"] == "no public job board known for Zeta Analytics"
    assert rows[""]["tried"][-1] == "board_known / board_guessed: the alert names no company" and rows[""]["why"] == "no company given"
    assert "not asked, not a company name: " in rows["Data Scientist SW1A 2AA"]["tried"][-1] and "postcode" in rows["Data Scientist SW1A 2AA"]["tried"][-1]
    assert rows["Acme Analytics"]["tried"][1].startswith("board_link: the workable/acme board the link names was read; no open posting has the link's id")
    assert [r["cv"] for r in d.run.unresolved] == ["", "", "", "", "agency or board-only", "tracker link"]
    assert not [u for u in net.urls(True) if any(h in u for h in ("linkedin.com", "indeed.", "cv-library"))]                  # none of those hosts is asked


def test_the_report_names_five_examples_when_board_link_and_board_known_resolved_none(six):
    d, _ = six
    rec = d.run.as_json()
    lines = disc.alert_why_lines(rec)
    assert lines[0] == "Why board_link and board_known resolved 0 of 6 alert(s) this run (6 unresolved; 5 example(s) below):"
    assert lines[1].startswith("  unresolved by reason: no board 4") and "no company 1" in lines[1] and "not a company 1" in lines[1]
    assert len([line for line in lines if line[:5].strip().rstrip(".").isdigit()]) == 5
    text = "\n".join(lines)
    assert "1. Zeta Analytics | Data Scientist | https://www.linkedin.com/jobs/view/1001" in text
    assert "2. (no company) | Data Scientist | https://uk.indeed.com/viewjob?jk=abc123def456" in text and "3. Data Scientist SW1A 2AA |" in text
    assert "     - reed: not a Reed job link" in text and "     failed: no public job board known for Zeta Analytics" in text
    assert disc.diverse(d.run.unresolved, 3)[2]["category"] == "not_a_company"                                              # one of each way of failing comes first


def test_the_report_is_silent_when_both_steps_resolved_some_and_names_the_one_that_did_not(six):
    d, _ = six
    rec = d.run.as_json()
    both = json.loads(json.dumps(rec))
    both["sources"]["gmail"].update(resolved_via_board_link=1, resolved_via_board_known=2)
    assert disc.alert_why_lines(both) == []
    one = json.loads(json.dumps(rec))
    one["sources"]["gmail"]["resolved_via_board_known"] = 1
    assert disc.alert_why_lines(one)[0].startswith("Why board_link resolved 0 of 6 alert(s)")
    assert disc.alert_why_lines({"sources": {"gmail": {"alerts": 3}}}) == []                                                # nothing unresolved: nothing to explain


def test_the_trace_is_in_the_saved_run_and_in_the_report_without_a_run_in_hand(six, cfg):
    d, _ = six
    disc.save_run(cfg, d.run)
    saved = disc.read_runs(cfg)[-1]
    assert saved["unresolved_total"] == 6 and saved["unresolved_kinds"] == {"no_board": 4, "no_company": 1, "not_a_company": 1}
    out = disc.format_report(cfg)
    assert "Why board_link and board_known resolved 0 of 6 alert(s)" in out and "Slug guesses per provider this run" in out


def test_discover_prints_the_trace_after_its_run(settings, db, monkeypatch, capsys):
    from netfake import Resp as R
    from careerops.discovery import http
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli, "_print_health", lambda *a, **k: [])
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: R("", 404))

    def fake_gmail(self):
        self.run.count("gmail", "alerts")
        self.run.unresolved.append(Discover.unresolved_row("gmail", "linkedin", "linkedin", "Zeta Analytics", "Data Scientist", "London", link(1),
                                                           ["reed: not a Reed job link", "board_link: x"], "no public job board known for Zeta Analytics", "no_board"))
    monkeypatch.setattr(Discover, "from_gmail", fake_gmail)
    cli.main(["discover", "--gmail-alerts"])
    out = capsys.readouterr().out
    assert "Why board_link and board_known resolved 0 of 1 alert(s) this run" in out and "failed: no public job board known for Zeta Analytics" in out
