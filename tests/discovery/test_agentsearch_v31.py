"""v3.1 part 2: agent search finds employer boards (`search-plan`, the CSV of URLs, `ingest-search`), and `daily` reads them from then on. No network (Net answers),
no model, no search engine: the CSV is written by the test."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from careerops import cli
from careerops.discovery import agentsearch as ag
from careerops.discovery import ats as ats_mod
from careerops.discovery import discover as disc
from careerops.discovery import workday
from careerops.discovery.ats import Board
from netfake import Net, Resp
from test_board_check import TODAY, cfg, fetcher as fx, routes  # noqa: F401  (cfg: test settings with no listed boards)
from test_quality_round import board as gh_jobs, gh
from test_shortlist import fb, roles  # noqa: F401  (the fact bank fixture and the blacklist path)

UUID1 = "0a1b2c3d-1111-2222-3333-444455556666"
EXCLUSIONS = "-senior -lead -principal -staff -manager -chief -director -intern"


def alert(company, title="Data Scientist", location="London", n=1):
    return SimpleNamespace(company=company, title=title, location=location, link=f"https://www.linkedin.com/jobs/view/{n}", t=SimpleNamespace(t=4.0), rank=3.0)


def plan(cfg, db, fb, alerts=(), days=0):
    return ag.build_plan(cfg, db, fb, today=TODAY.date() + timedelta(days=days), alerts=list(alerts))


def write_csv(cfg, rows, name=None, header="query_id,url"):
    p = ag.search_dir(cfg) / (name or f"{TODAY.date().isoformat()}.csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(header + "\n" + "".join(",".join(r) + "\n" for r in rows), encoding="utf-8")
    return p


def ingest(cfg, db, fb, net, rows, queries=None, **kw):
    ing = ag.Ingest(cfg, db, fx(net), fb=fb, today=TODAY, **kw)
    return ing, ing.run(rows, queries or {})


def inbox(cfg):
    return sorted((cfg.path("jds") / "inbox").glob("*.txt"))


# ------------------------------------------------------------------ 2a. the plan
def test_the_plan_has_twelve_role_queries_and_one_workday_query_with_the_exclusions(cfg, db, fb):
    p = plan(cfg, db, fb)
    roles_ = [q for q in p.queries if q.kind == "role"]
    assert len(roles_) == 12 and [q.kind for q in p.queries].count("workday") == 1 and len(p.queries) == 13
    assert {q.family for q in roles_} == {"data_scientist", "ai_engineer", "genai_engineer", "ml_engineer"}
    first = roles_[0]
    assert first.id == "role:data_scientist:greenhouse"
    assert first.text == ('(site:job-boards.greenhouse.io OR site:job-boards.eu.greenhouse.io) ("Data Scientist" OR "Junior Data Scientist") '
                          '("London" OR "Bristol" OR "Manchester" OR "Cambridge" OR "Edinburgh" OR "United Kingdom" OR "UK") ' + EXCLUSIONS)
    assert all(q.text.endswith(EXCLUSIONS) for q in roles_)
    assert any(q.text.startswith("(site:jobs.lever.co OR site:jobs.eu.lever.co)") for q in roles_)
    assert any(q.text.startswith("site:jobs.ashbyhq.com (") for q in roles_)
    assert p.queries[-1].text == 'site:myworkdayjobs.com ("Data Scientist" OR "Machine Learning Engineer") "United Kingdom"'


def test_the_plan_never_has_more_than_twenty_queries_and_alerts_come_first(cfg, db, fb):
    p = plan(cfg, db, fb, [alert(f"Employer {c}", n=i) for i, c in enumerate("ABCDEFGHIJ")])
    kinds = [q.kind for q in p.queries]
    assert len(p.queries) == 20 and kinds.count("alert") == 8 and kinds.count("role") == 11 and kinds.count("workday") == 1
    assert kinds[:8] == ["alert"] * 8
    assert [c for c, w in p.skipped_alerts if "over the limit" in w] == ["Employer I", "Employer J"]


def test_an_alert_query_names_the_company_and_the_board_sites(cfg, db, fb):
    q = plan(cfg, db, fb, [alert("M&G"), alert("M&G", "Data Scientist II", n=2)]).queries[0]
    assert q.kind == "alert" and q.id == "alert:mandg" and [a["title"] for a in q.alerts] == ["Data Scientist", "Data Scientist II"]
    assert q.text == ('"M&G" careers (site:greenhouse.io OR site:lever.co OR site:ashbyhq.com OR site:myworkdayjobs.com OR site:workable.com '
                      'OR site:teamtailor.com)')


def test_agencies_places_and_companies_with_a_board_get_no_alert_query(cfg, db, fb):
    cfg.raw["sources"]["greenhouse"] = [{"slug": "monzo", "name": "Monzo"}]
    p = plan(cfg, db, fb, [alert("Hays Recruitment"), alert("Wandsworth, Greater London"), alert("Monzo"), alert("Entain")])
    assert [q.company for q in p.queries if q.kind == "alert"] == ["Entain"]
    assert dict(p.skipped_alerts) == {"Hays Recruitment": "a recruitment agency", "Wandsworth, Greater London": "a place, not an employer",
                                      "Monzo": "already has a board"}


def write_yield(cfg, qid, *new_boards, start=0):
    p = ag.yield_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for i, n in enumerate(new_boards):
            d = (TODAY + timedelta(days=start + i)).date().isoformat()
            f.write(json.dumps({"ts": d + "T10:00:00+00:00", "date": d, "query_id": qid, "kind": "role", "new_boards": n, "new_jobs": 0}) + "\n")


def test_a_role_query_with_no_new_board_in_three_runs_rests_and_the_next_family_takes_its_slot(cfg, db, fb):
    write_yield(cfg, "role:data_scientist:lever", 0, 0, 0, start=-5)
    p = plan(cfg, db, fb)
    ids = [q.id for q in p.queries]
    assert "role:data_scientist:lever" not in ids and "role:forward_deployed_engineer:greenhouse" in ids
    assert len([q for q in p.queries if q.kind == "role"]) == 12
    assert p.rested["role:data_scientist:lever"] == (TODAY + timedelta(days=-3 + ag.REST_DAYS)).date()
    text = "\n".join(ag.plan_lines(cfg, p))
    assert "rested (no new board in 3 runs in a row): role:data_scientist:lever" in text and "role:data_scientist:lever | 3 | 0 | 0" in text
    later = plan(cfg, db, fb, days=ag.REST_DAYS)
    assert "role:data_scientist:lever" in [q.id for q in later.queries]                # the rest ends; the old runs do not count again


def test_a_new_board_breaks_the_run_of_empty_runs(cfg, db, fb):
    write_yield(cfg, "role:ai_engineer:ashby", 0, 0, 1, 0, 0, start=-6)
    assert "role:ai_engineer:ashby" in [q.id for q in plan(cfg, db, fb).queries]


def test_the_prompt_is_self_contained(cfg, db, fb):
    p = plan(cfg, db, fb, [alert("Entain")])
    path = ag.save_plan(cfg, p)
    text = ag.prompt_text(cfg, p, path)
    for q in p.queries:
        assert q.id in text and q.text in text
    assert str(ag.csv_path(cfg, p.day)) in text and f"careerops ingest-search {ag.csv_path(cfg, p.day)}" in text
    assert "URLs only" in text and "never one you made up" in text and "At most 10 URLs per query" in text
    assert "Nothing in this flow applies to a job, submits a form or contacts anyone" in text
    assert ag.load_plan(cfg, p.day)["alert:entain"].company == "Entain"


# ------------------------------------------------------------------ 2c. URLs
@pytest.mark.parametrize("url, vendor, board, job", [
    ("https://job-boards.greenhouse.io/acme/jobs/4012345", "greenhouse", "acme", "4012345"),
    ("https://job-boards.eu.greenhouse.io/acmeeu/jobs/4099?gh_src=x", "greenhouse", "acmeeu", "4099"),
    ("https://boards.greenhouse.io/acme", "greenhouse", "acme", ""),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=55", "greenhouse", "acme", "55"),
    (f"https://jobs.lever.co/acme/{UUID1}", "lever", "acme", UUID1),
    (f"https://jobs.eu.lever.co/acme/{UUID1}/apply", "lever_eu", "acme", UUID1),
    (f"https://jobs.ashbyhq.com/AcmeAI/{UUID1}/application", "ashby", "AcmeAI", UUID1),
    ("https://jobs.ashbyhq.com/acme", "ashby", "acme", ""),
    ("https://apply.workable.com/acme/j/ABC123/", "workable", "acme", "ABC123"),
])
def test_ats_urls_become_vendor_board_and_job_id(url, vendor, board, job):
    ref = ag.parse_url(url)
    assert (ref.vendor, ref.board, ref.job) == (vendor, board, job)


def test_a_workday_url_becomes_its_site_and_the_posting_path():
    ref = ag.parse_url("https://acme.wd3.myworkdayjobs.com/en-US/External/job/London/Data-Scientist_R123")
    site = workday.site_from_slug(ref.board)
    assert (ref.vendor, site.host, site.tenant, site.site, ref.job, ref.path) == ("workday", "acme.wd3.myworkdayjobs.com", "acme", "External", "R123",
                                                                                 "/job/London/Data-Scientist_R123")


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://www.acme.com/careers/data-scientist", "https://job-boards.greenhouse.io/",
                                 "ftp://jobs.lever.co/acme/1", "not a url", "https://uk.indeed.com/viewjob?jk=1"])
def test_anything_else_is_not_an_ats_url(url):
    assert ag.parse_url(url) is None


# ------------------------------------------------------------------ 2c. ingest
def acme_net(net=None):
    return routes(net or Net(strict=False), **gh("acme", "Acme Analytics", (4012345, "Data Scientist", "London, UK"), (4012346, "Senior Data Scientist", "London, UK")))


def test_a_csv_of_mixed_urls_writes_the_verified_job_and_drops_what_is_not_an_ats_link(cfg, db, fb):
    net = acme_net()
    rows = [("role:data_scientist:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012345"),
            ("role:data_scientist:greenhouse", "https://www.linkedin.com/jobs/view/9"),
            ("role:data_scientist:greenhouse", "https://www.acme.com/careers")]
    ing, rep = ingest(cfg, db, fb, net, rows)
    assert (rep.urls, rep.ats_urls, rep.dropped_urls) == (3, 1, 2)
    assert rep.new_boards == ["greenhouse/acme (Acme Analytics; role:data_scientist:greenhouse)"] and [n for n, _, _ in rep.jobs_written] == ["Acme Analytics | Data Scientist"]
    assert len(inbox(cfg)) == 1 and "Company: Acme Analytics" in inbox(cfg)[0].read_text(encoding="utf-8")
    assert not [u for u in net.urls() if "linkedin" in u or "acme.com" in u]                       # a URL that is not an ATS link is never fetched
    assert rep.requests == {"boards-api.greenhouse.io": 3}                                          # robots.txt, the board's name, its job list
    text = "\n".join(rep.lines())
    assert "3 URL(s) read · 1 ATS URL(s) · 2 not an ATS link (dropped)" in text and "boards: 1 new · 0 known · 0 rejected" in text
    assert "jobs written: 1 (tier 0" in text and "requests per host: boards-api.greenhouse.io 3" in text


def test_a_new_board_is_added_to_the_cache_once_and_daily_reads_it_from_then_on(cfg, db, fb):
    net = acme_net()
    rows = [("role:ai_engineer:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012345")]
    ingest(cfg, db, fb, net, rows)
    entry = json.loads((cfg.path("discover") / "boards.json").read_text(encoding="utf-8"))["greenhouse:acme"]
    assert entry["found"] and entry["source"] == "search" and entry["query_id"] == "role:ai_engineer:greenhouse" and entry["date"] == "2026-10-10"
    _, again = ingest(cfg, db, fb, net, rows + [("role:ml_engineer:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012346")])
    assert again.new_boards == [] and again.known_boards == ["greenhouse/acme"]
    assert [u for u in net.urls() if u.endswith("/v1/boards/acme")] == ["https://boards-api.greenhouse.io/v1/boards/acme"]    # its name was asked once
    assert ("Acme Analytics | Senior Data Scientist", "level excluded (\\bsenior\\b)") in again.jobs_not_written
    assert Board("greenhouse", "acme", "Acme Analytics", "search") in ats_mod.configured_boards(cfg)
    d = disc.Discover(cfg, db, fx(net), today=TODAY, say=lambda s: None)
    assert ("greenhouse", "acme") in {(b.ats, b.slug) for b in d.lookup.boards}                      # an alert of the company resolves on it (board_known)


def test_at_most_ten_urls_a_query_and_repeats_are_read_once(cfg, db, fb):
    net = acme_net()
    rows = [("role:x", f"https://www.example.com/{i}") for i in range(12)] + [("role:x", "https://www.example.com/1")]
    _, rep = ingest(cfg, db, fb, net, rows)
    assert rep.over_limit == 2 and rep.duplicates == 1 and net.urls(True) == []


def test_an_ashby_401_for_a_board_name_that_does_not_exist_rejects_the_board_and_stops_nothing(cfg, db, fb):
    net = Net(strict=False).add("api.ashbyhq.com", lambda url, **kw: Resp("", 404) if url.endswith("robots.txt") else Resp("", 401))
    ing, rep = ingest(cfg, db, fb, net, [("role:ai_engineer:ashby", f"https://jobs.ashbyhq.com/nobody/{UUID1}")])
    assert rep.rejected_boards == [("ashby/nobody", "the API says there is no such board")] and "api.ashbyhq.com" not in ing.f.backed_off


def test_a_board_robots_txt_disallows_is_not_asked(cfg, db, fb):
    net = Net(strict=False).add("api.lever.co/robots.txt", "User-agent: *\nDisallow: /\n")
    _, rep = ingest(cfg, db, fb, net, [("role:x", f"https://jobs.lever.co/acme/{UUID1}")])
    assert rep.rejected_boards == [("lever/acme", "robots.txt of api.lever.co disallows its API")]
    assert [u for u in net.urls() if "api.lever.co" in u] == []


# ------------------------------------------------------------------ alert queries resolve alerts
def oritain_plan():
    return {"alert:oritain": ag.Query("alert:oritain", "alert", '"Oritain" careers (...)', company="Oritain",
                                      alerts=[{"title": "Data Scientist", "location": "London", "link": "https://www.linkedin.com/jobs/view/77"}])}


def test_an_alert_query_that_finds_the_companys_board_resolves_the_alert(cfg, db, fb):
    net = routes(Net(strict=False), **gh("oritain", "Oritain", (51, "Data Scientist", "London, UK"), (52, "Product Manager", "London, UK")))
    _, rep = ingest(cfg, db, fb, net, [("alert:oritain", "https://job-boards.greenhouse.io/oritain")], oritain_plan())
    assert rep.alerts_resolved == ["Oritain | Data Scientist"] and rep.new_boards == ["greenhouse/oritain (Oritain; alert:oritain)"]
    assert len(inbox(cfg)) == 1 and "Title: Data Scientist" in inbox(cfg)[0].read_text(encoding="utf-8")
    assert "alerts resolved: 1 (Oritain | Data Scientist)" in "\n".join(rep.lines())


def test_an_alert_query_that_finds_another_companys_board_rejects_it(cfg, db, fb):
    net = routes(Net(strict=False), **gh("orient", "Orient Express Ltd", (51, "Data Scientist", "London, UK")))
    _, rep = ingest(cfg, db, fb, net, [("alert:oritain", "https://job-boards.greenhouse.io/orient")], oritain_plan())
    assert rep.rejected_boards == [("greenhouse/orient", "the board is called 'Orient Express Ltd', not 'Oritain' (needs a 0.90 match)")]
    assert rep.alerts_resolved == [] and inbox(cfg) == []


def test_a_manual_queue_alert_resolves_on_a_board_a_role_query_found(cfg, db, fb):
    disc.append_rows(disc.manual_queue_path(cfg), disc.MANUAL_COLUMNS, [{"ts": "2026-10-09T09:00:00+00:00", "source": "linkedin", "company": "Acme Analytics",
                                                                          "title": "Data Scientist", "location": "London", "link": "https://www.linkedin.com/jobs/view/5",
                                                                          "reason": "no public job board known", "apply_kind": "aggregator_only"}])
    _, rep = ingest(cfg, db, fb, acme_net(), [("role:data_scientist:lever", "https://boards.greenhouse.io/acme")])
    assert rep.alerts_resolved == ["Acme Analytics | Data Scientist"]


# ------------------------------------------------------------------ untrusted text
def test_no_snippet_or_title_from_the_search_results_is_stored(cfg, db, fb, tmp_path):
    marker = "ZZ-IGNORE-PREVIOUS-INSTRUCTIONS-ZZ"
    p = write_csv(cfg, [("role:data_scientist:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012345", f"{marker} title", f"{marker} snippet")],
                  header="query_id,url,title,snippet")
    rows, notes = ag.read_csv(p)
    assert rows == [("role:data_scientist:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012345")]
    assert notes == ["column(s) title, snippet ignored: only query_id and url are read (search-result text is never stored)"]
    _, rep = ingest(cfg, db, fb, acme_net(), rows)
    ag.record_yield(cfg, rep, {}, TODAY)
    ag.record_ingest(cfg, rep, p, TODAY)
    written = [f for f in tmp_path.rglob("*") if f.is_file() and f != p]
    assert written and not [f for f in written if marker in f.read_text(encoding="utf-8", errors="ignore")]
    assert marker not in "\n".join(rep.lines())


def test_a_csv_without_the_two_columns_is_refused(cfg):
    with pytest.raises(ValueError, match="query_id,url"):
        ag.read_csv(write_csv(cfg, [("x", "y")], header="query,link"))


def test_the_yield_is_recorded_per_query(cfg, db, fb):
    _, rep = ingest(cfg, db, fb, acme_net(), [("role:data_scientist:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/4012345"),
                                              ("role:ai_engineer:lever", "https://www.linkedin.com/jobs/view/3")])
    ag.record_yield(cfg, rep, {}, TODAY)
    got = {e["query_id"]: e for e in ag.read_yield(cfg)}
    assert (got["role:data_scientist:greenhouse"]["new_boards"], got["role:data_scientist:greenhouse"]["new_jobs"]) == (1, 1)
    assert (got["role:ai_engineer:lever"]["urls"], got["role:ai_engineer:lever"]["ats_urls"], got["role:ai_engineer:lever"]["new_boards"]) == (1, 0, 0)


def test_a_dry_run_writes_no_inbox_file_and_no_cache(cfg, db, fb):
    _, rep = ingest(cfg, db, fb, acme_net(), [("role:x", "https://job-boards.greenhouse.io/acme/jobs/4012345")], dry_run=True)
    assert inbox(cfg) == [] and not (cfg.path("discover") / "boards.json").exists() and len(rep.jobs_written) == 1


# ------------------------------------------------------------------ the commands
def test_the_commands_plan_prompt_and_ingest(cfg, db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ctx", lambda args: (cfg, db))
    cli.main(["search-plan"])
    out = capsys.readouterr().out
    assert "q01 role:data_scientist:greenhouse" in out and "plan saved:" in out and "yield: no search has been ingested yet" in out
    cli.main(["search-plan", "--emit-prompt"])
    assert "careerops ingest-search" in capsys.readouterr().out
    p = write_csv(cfg, [("role:data_scientist:greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")])
    cli.main(["ingest-search", str(p), "--mock"])                     # no network: the board is reported as not read
    out = capsys.readouterr().out
    assert "1 URL(s) read · 1 ATS URL(s)" in out and "rejected: greenhouse/acme: " in out and "--mock" in out
