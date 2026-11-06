"""v3.1 part 1: the faults the `careerops daily` run of 5 Nov 2026 showed. Fee-charging training schemes are not jobs; the per-company cap counts the rows of
the list itself and one job is one row; "Chief" and the other executive words are levels; a US-only location list is out of the UK; Ashby's 401 for a board
name that does not exist is "no board"; a company is asked of a provider only when the public board list names it; every row says how it is applied to.
No network (Net answers), no model."""
from datetime import timedelta

import pytest

from careerops import targeting
from careerops.discovery import ats as ats_mod, blacklist, boards as boards_mod, dataset as dataset_mod, discover as disc, gates, priority, region, shortlist, triage
from careerops.discovery.ats import Board
from careerops.discovery.blocklog import BlockLog
from careerops.discovery.boards import BoardIndex
from careerops.discovery.http import Blocked, FetchError, Fetcher, Soft401
from careerops.discovery.robots import RobotsCache
from careerops.discovery.seeds import Seed
from netfake import Net, Resp, fixture_json
from test_board_check import cfg, fetcher as fx  # noqa: F401  (cfg: test settings with no listed boards)
from test_careers_v30 import lists
from test_shortlist import apply_today, build, fb, inbox, roles, run_with  # noqa: F401  (fixtures and helpers of the shortlist tests)
from triage_fakes import NOW, ad_text, write_ad

ITOL_TEXT = ("Trainee AI Engineer - No Experience Needed! Please note this is a training course and fees apply. "
             "Job guaranteed on completion or get your money back.")


# ------------------------------------------------------------------ 1. fee-charging training schemes
@pytest.mark.parametrize("text, said", [
    ("Please note this is a training course and fees apply.", "training course and fees apply"),
    ("Course fees are payable in instalments.", "Course fees"),
    ("A job guarantee once you finish.", "job guarantee"),
    ("Job guaranteed on completion.", "Job guaranteed"),
    ("or get your money back", "money back"),
    ("Fees apply to this programme.", "Fees apply"),
])
def test_each_fee_phrase_marks_a_training_scheme_and_quotes_it(text, said):
    assert blacklist.training_scheme("Some Academy", text) == f'the ad says "{said}"'


def test_funded_training_or_a_traineeship_counts_only_with_a_fee():
    assert blacklist.training_scheme("X", "Our AI traineeship: a fee of £995 is due on enrolment.") == 'the ad says "traineeship" and "fee"'
    assert blacklist.training_scheme("X", "Funded training for the right person; a small admin fee applies.") is not None
    assert blacklist.training_scheme("X", "Funded training and a salary of £35,000.") is None
    assert blacklist.training_scheme("X", "Join our graduate traineeship in London.") is None
    assert blacklist.training_scheme("X", "We pay conference fees and give you a learning budget.") is None


def test_itol_recruit_is_a_training_scheme_by_name_whatever_the_ad_says():
    assert "ITOL Recruit" in blacklist.TRAINING_SCHEME_COMPANIES
    assert blacklist.training_scheme("ITOL Recruit", "Junior AI Developer, London") is not None
    assert blacklist.training_scheme("ITOL Recruit Ltd", "") is not None                     # Ltd / case are ignored, as for the blacklist file


def test_disqualify_gives_the_blacklist_reason_training_scheme(settings, db):
    job = {"id": "x", "company": "Some Academy", "title": "Data Science Trainee", "description": ITOL_TEXT, "url": ""}
    d = triage.disqualify(job, settings, db, archive=[], others=[])
    assert d.stage == "blacklist" and d.code == blacklist.TRAINING_SCHEME == "training_scheme"
    assert d.line().startswith("training scheme (fees): the ad says ")


def test_the_five_itol_ads_are_disqualified_and_reported_as_training_schemes(settings, db, fb, inbox):
    for i, title in enumerate(("Trainee AI Engineer", "Data Science Trainee", "Junior AI Developer", "Trainee AI Engineer", "Trainee AI Engineer")):
        write_ad(inbox, f"itol{i}.txt", company="ITOL Recruit", title=title, url=f"https://www.reed.co.uk/jobs/x/{5000 + i}", extra=ITOL_TEXT)
    write_ad(inbox, "real.txt", company="Real Co")
    plan = build(settings, db, fb)
    assert [r.job["company"] for r in plan.rows] == ["Real Co"]
    assert len(plan.dropped) == 5 and all("training scheme (fees)" in why for _, _, why in plan.dropped)
    text = "\n".join(shortlist.lines(plan, targeting.config(settings)))
    assert text.count("  disqualified: training scheme (fees)") == 5 and "5 disqualified" in text


# ------------------------------------------------------------------ 2. the per-company cap counts the list; one job is one row
def test_a_company_gets_at_most_two_rows_in_one_list(settings, db, fb, inbox):
    for i, title in enumerate(("Data Scientist", "Machine Learning Engineer", "AI Engineer")):
        write_ad(inbox, f"acme{i}.txt", company="Acme", title=title, age_days=1 + i * 0.1)
    write_ad(inbox, "other.txt", company="Other", age_days=3)
    plan = build(settings, db, fb)
    assert [r.job["company"] for r in plan.rows].count("Acme") == 2 and "Other" in [r.job["company"] for r in plan.rows]
    assert len(plan.held) == 1 and "already in this list" in plan.held[0][1] and "per_company_max_open is 2" in plan.held[0][1]
    assert "1 at a company limit" in "\n".join(shortlist.lines(plan, targeting.config(settings)))


def test_an_open_application_and_a_row_in_the_list_count_together(settings, db, fb, inbox):
    apply_today(db, "manual:1", "Acme", title="Data Engineer")
    for i, title in enumerate(("Data Scientist", "Machine Learning Engineer")):
        write_ad(inbox, f"acme{i}.txt", company="Acme", title=title, age_days=1 + i * 0.1)
    plan = build(settings, db, fb)
    assert [r.job["company"] for r in plan.rows] == ["Acme"]
    assert "and 1 open application(s)" in plan.held[0][1]


def test_the_same_company_title_and_location_is_one_row_with_every_link(settings, db, fb, inbox):
    urls = [f"https://www.reed.co.uk/jobs/trainee/{n}" for n in (1, 2, 3)]
    for n, u in enumerate(urls):
        write_ad(inbox, f"dup{n}.txt", company="Acme", title="Data Scientist", url=u, age_days=1 + n * 0.1)
    plan = build(settings, db, fb)
    [row] = plan.rows
    assert row.links == urls and len(row.dups) == 2 and plan.held == []
    assert "| 3 links: " + " · ".join(urls) in row.line(1)


def test_approving_a_merged_row_approves_one_file_and_files_the_copies_as_duplicates(settings, db, fb, inbox):
    for n in range(3):
        write_ad(inbox, f"dup{n}.txt", company="Acme", title="Data Scientist", url=f"https://boards.greenhouse.io/acme/jobs/{n}", age_days=1 + n * 0.1)
    run_with(settings, db, fb, "a")
    approved = list((shortlist.approved_root(settings) / NOW.date().isoformat()).glob("*.txt"))
    dups = [p for p in (settings.path("jds_done") / shortlist.DUPLICATE).glob("*.txt") if not p.name.endswith(".reason.txt")]
    assert len(approved) == 1 and len(dups) == 2 and not list(inbox.glob("*.txt"))
    assert approved[0].name in (settings.path("jds_done") / shortlist.DUPLICATE / (dups[0].stem + ".reason.txt")).read_text(encoding="utf-8")


def test_skipping_a_merged_row_skips_every_copy(settings, db, fb, inbox):
    for n in range(2):
        write_ad(inbox, f"dup{n}.txt", company="Acme", title="Data Scientist", url=f"https://boards.greenhouse.io/acme/jobs/{n}", age_days=1 + n * 0.1)
    run_with(settings, db, fb, "s 1", "q")
    skipped = [p for p in (settings.path("jds_done") / "skipped").glob("*.txt") if not p.name.endswith(".reason.txt")]
    assert len(skipped) == 2 and not list(inbox.glob("*.txt"))


def test_another_location_is_another_row(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme", title="Data Scientist")
    (inbox / "b.txt").write_text(ad_text(company="Acme", title="Data Scientist", url="https://boards.greenhouse.io/acme/jobs/9").replace(
        "Location: London, UK", "Location: Manchester, UK"), encoding="utf-8")
    assert len(build(settings, db, fb).rows) == 2


# ------------------------------------------------------------------ 3. chief, head of, vp, director, principal, staff are levels
@pytest.mark.parametrize("title", ["Chief AI Engineer", "Head of Data Science", "VP Machine Learning", "Director of AI", "Principal Data Scientist",
                                   "Staff Machine Learning Engineer"])
def test_executive_titles_are_level_excluded_even_when_the_list_does_not_name_them(settings, db, title):
    settings.raw["roles"]["levels"]["exclude"] = [r"\blead\b"]                    # the user's list names none of these
    assert gates.level_excluded(title, settings) is not None
    d = triage.disqualify({"id": "x", "company": "Acme", "title": title, "description": "x", "url": ""}, settings, db, archive=[], others=[])
    assert d.stage == "level"


def test_chief_ai_engineer_is_disqualified_like_lead_and_scores_l1():
    from careerops.config import load_settings
    s = load_settings()
    assert gates.level_excluded("Lead AI Engineer", s) and gates.level_excluded("Chief AI Engineer", s)
    assert priority.level_of("Chief AI Engineer") == "manager" and triage.level_score("Chief AI Engineer") == 1.0 == triage.level_score("Lead AI Engineer")
    assert gates.level_excluded("AI Engineer", s) is None and gates.level_excluded("Staffing Data Analyst", s) is None


# ------------------------------------------------------------------ 4. a US-only location list is out of the UK
DATABRICKS = ("Alabama; Berkeley, California; Connecticut; Kansas City, Missouri; Maryland; New Hampshire; New Jersey; North Carolina; Remote - California; "
              "Remote - Colorado; Remote - Illinois; Remote - Massachusetts; Remote - Missouri; Remote - New York; Remote - Pennsylvania; Remote - Virginia; "
              "Remote - Washington; Remote - Washington D.C.; Rhode Island; San Diego, California; South Carolina")


def test_the_databricks_location_list_is_out_of_the_uk_whatever_the_ad_says():
    m = region.match(DATABRICKS, "We hire across the UK and the US. Right to work in the UK is welcome.", ("uk",))
    assert m.verdict == region.OUT and "no option of the 21 places is in United Kingdom" in m.evidence


@pytest.mark.parametrize("place", ["Remote - California", "Remote - Washington D.C.", "Remote (Texas)", "Remote - New York", "Remote - San Diego", "Remote - WA"])
def test_remote_dash_a_us_state_or_city_is_a_us_place(place):
    assert region.remote_country(place)[0] == "US"
    assert region.match(place, "Right to work in the UK required.", ("uk",)).verdict == region.OUT
    assert region.match(place, "", ("us",)).verdict == region.IN


@pytest.mark.parametrize("place, verdict", [
    ("Remote", region.IN),                         # a bare Remote: the ad's text decides (it says UK)
    ("Remote; EMEA", region.IN),                   # nothing but such options: the ad's text decides
    ("London; Remote - California", region.IN),    # a UK option is enough
    ("Remote - UK", region.IN),
    ("New York; Remote", region.OUT),              # a list with no UK place is out, even with "Remote"
    ("New York", region.OUT),                      # regression: "New York" matched the UK city York
    ("York", region.IN),
])
def test_only_a_place_made_of_remote_or_emea_goes_to_the_ad_text(place, verdict):
    assert region.match(place, "You must have the right to work in the UK.", ("uk",)).verdict == verdict


def test_a_bare_remote_with_nothing_in_the_ad_is_still_unknown_not_out():
    assert region.match("Remote", "We are a friendly team.", ("uk",)).verdict == region.UNKNOWN


# ------------------------------------------------------------------ 5. Ashby's 401
def ashby_fetcher(net, tmp_path):
    return Fetcher(RobotsCache(), get=net.get, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0, blocklog=BlockLog(tmp_path / "blocked_hosts.json"))


def test_a_401_for_a_guessed_ashby_name_is_no_board_and_does_not_stop_the_host(cfg, tmp_path):
    net = Net(strict=False).add("api.ashbyhq.com", Resp("", 401))
    f = ashby_fetcher(net, tmp_path)
    index = BoardIndex(cfg)                               # the test settings guess (blind_guesses: true)
    found = boards_mod.find(Seed("Nobody Labs", "archive"), f, index)
    asked = [u for u in net.urls() if "api.ashbyhq.com" in u]
    assert found.boards == [] and len(asked) == 3 and not found.errors           # every token was asked: the host was not stopped
    assert "api.ashbyhq.com" not in f.backed_off and f.soft_401s == asked
    assert index.get("ashby", "nobodylabs") is False                            # remembered as "no board" for this run
    assert not (tmp_path / "blocked_hosts.json").exists()


def test_a_401_on_a_known_ashby_board_stops_the_host_only_at_the_third_in_a_row(tmp_path):
    net = Net(strict=False).add("api.ashbyhq.com", Resp("", 401))
    f = ashby_fetcher(net, tmp_path)
    for slug in ("one", "two"):
        with pytest.raises(Soft401) as e:
            ats_mod.board_postings(Board("ashby", slug, slug.title(), "config"), f)
        assert not isinstance(e.value, Blocked) and "api.ashbyhq.com" not in f.backed_off
    with pytest.raises(Blocked):
        ats_mod.board_postings(Board("ashby", "three", "Three", "config"), f)
    assert f.backed_off["api.ashbyhq.com"] == 401
    entry = BlockLog(tmp_path / "blocked_hosts.json").hosts()["api.ashbyhq.com"]
    assert entry["url"] == "https://api.ashbyhq.com/posting-api/job-board/three" == f.refused_url["api.ashbyhq.com"]


def test_any_other_answer_resets_the_run_of_401s(tmp_path):
    replies = iter([Resp("", 401), Resp("", 401), Resp(fixture_json("ashby_acme.json")), Resp("", 401), Resp("", 401)])
    net = Net(strict=False).add("api.ashbyhq.com", lambda url, **kw: Resp("", 404) if url.endswith("/robots.txt") else next(replies))
    f = ashby_fetcher(net, tmp_path)
    for slug in ("a", "b", "c", "d", "e"):
        try:
            ats_mod.board_postings(Board("ashby", slug, "Acme", "config"), f)
        except FetchError as e:
            assert not isinstance(e, Blocked)
    assert "api.ashbyhq.com" not in f.backed_off


def test_a_401_elsewhere_still_stops_the_host_at_once_and_its_url_is_logged(tmp_path):
    net = Net(strict=False).add("boards-api.greenhouse.io", Resp("", 401))
    f = ashby_fetcher(net, tmp_path)
    with pytest.raises(Blocked):
        ats_mod.board_postings(Board("greenhouse", "acme", "Acme", "config"), f)
    log = BlockLog(tmp_path / "blocked_hosts.json")
    assert log.hosts()["boards-api.greenhouse.io"]["url"] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert "(to https://boards-api.greenhouse.io/v1/boards/acme/jobs)" in log.describe("boards-api.greenhouse.io")


# ------------------------------------------------------------------ 6. the public list instead of blind guesses
@pytest.fixture
def listed(cfg):
    cfg.raw["discover"]["ats"].pop("blind_guesses", None)                    # the built-in default: no blind guesses
    cfg.raw["discover"]["ats"].pop("max_guesses_per_provider", None)
    cfg.raw["discover"]["dataset"] = {"enabled": True}
    return cfg


def index_with(cfg, net):
    index = BoardIndex(cfg)
    index.dataset = dataset_mod.Dataset(cfg, fx(net))
    return index


def asked(net):
    return [u for u in net.urls() if "companies.json" not in u]


def test_by_default_nothing_is_guessed(listed):
    assert boards_mod.MAX_GUESSES == 0 and BoardIndex(listed).blind is False and BoardIndex(listed).max_guesses == 0
    listed.raw["discover"]["ats"]["max_guesses_per_provider"] = 60                    # the user's old value counts only with blind_guesses: true
    assert BoardIndex(listed).max_guesses == 0
    listed.raw["discover"]["ats"]["blind_guesses"] = True
    assert BoardIndex(listed).max_guesses == boards_mod.GUESS_CEILING == 20


def test_a_company_the_list_does_not_hold_is_asked_of_no_provider(listed):
    net = lists(Net(strict=False), greenhouse=["someoneelse"])
    index = index_with(listed, net)
    f = boards_mod.find(Seed("Nobody Labs", "alert"), fx(net), index)
    assert asked(net) == [] and f.boards == [] and f.not_listed and not f.capped
    assert "not in the public board list" in f.tried_text() and "nothing is guessed" in f.tried_text()
    assert "guesses stopped at the limit" not in "\n".join(index.notes())


def test_a_name_the_list_holds_gets_one_verifying_request_to_that_provider_only(listed):
    net = lists(Net(strict=False), lever=["acme-analytics"]).add("api.lever.co", lambda url, **kw: Resp(fixture_json("lever_acme.json"))
                                                                     if url.endswith("/postings/acme-analytics") else Resp("", 404))
    index = index_with(listed, net)
    f = boards_mod.find(Seed("Acme Analytics Ltd", "archive"), fx(net), index)
    assert [(b.ats, b.slug) for b in f.boards] == [("lever", "acme-analytics")]
    assert asked(net) == ["https://api.lever.co/v0/postings/acme-analytics"]
    c = index.guess_counts()["lever"]
    assert (c["guesses"], c["list_matched"], c["list_verified"], c["list_failed"]) == (0, 1, 1, 0)


def test_a_090_board_name_match_counts_and_a_weaker_one_does_not(listed):
    net = lists(Net(strict=False), ashby=["JazzPharmaceutical", "jazzhands"]).add("api.ashbyhq.com", lambda url, **kw: Resp(fixture_json("ashby_acme.json"))
                                                                                    if url.endswith("/JazzPharmaceutical") else Resp("", 404))
    index = index_with(listed, net)
    assert index.dataset.match("Jazz Pharmaceuticals") == {"ashby": ("JazzPharmaceutical", pytest.approx(0.973, abs=0.01))}
    assert index.dataset.match("Jazz") == {}                                       # short names must match exactly
    f = boards_mod.find(Seed("Jazz Pharmaceuticals", "alert"), fx(net), index)
    assert [(b.ats, b.slug) for b in f.boards] == [("ashby", "JazzPharmaceutical")] and f.unverified == []


def test_m_and_g_matches_mandg_or_mg():
    assert dataset_mod.name_forms("M&G plc") == ["mandg", "mg"]


def test_a_listed_name_whose_board_is_gone_is_counted_as_failed(listed):
    net = lists(Net(strict=False), greenhouse=["acmeanalytics"]).add("boards-api.greenhouse.io", Resp("", 404))
    index = index_with(listed, net)
    f = boards_mod.find(Seed("Acme Analytics", "archive"), fx(net), index)
    assert f.boards == [] and index.guess_counts()["greenhouse"]["list_failed"] == 1
    lines = disc.guess_lines({"guesses": index.guess_counts(), "guess_cap": 0})
    assert any("Matched from the public board list" in l and "greenhouse 1 matched, 0 verified, 1 failed" in l for l in lines)


def wd_fetcher(net, posts):
    def post(url, json=None, headers=None, **kw):
        posts.append(url)
        return Resp({"total": 1, "jobPostings": [{"title": "Data Scientist", "externalPath": "/job/London/DS_R1", "locationsText": "London"}], "facets": []})
    return Fetcher(RobotsCache(), get=net.get, post=post, sleep=lambda s: None, clock=lambda: 0.0, gap_s=0.0)


def test_a_workday_tenant_the_list_names_is_verified_through_cxs_and_read(listed):
    net = lists(Net(strict=False), workday=["acmeanalytics|wd3|External"])
    posts: list[str] = []
    index = index_with(listed, net)
    f = boards_mod.find(Seed("Acme Analytics", "archive"), wd_fetcher(net, posts), index)
    assert [b.ats for b in f.boards] == ["workday"] and posts == ["https://acmeanalytics.wd3.myworkdayjobs.com/wday/cxs/acmeanalytics/External/jobs"]
    c = index.guess_counts()["workday"]
    assert (c["list_matched"], c["list_verified"], c["list_failed"]) == (1, 1, 0)
    again = boards_mod.find(Seed("Acme Analytics", "archive"), wd_fetcher(net, posts), index)
    assert [b.ats for b in again.boards] == ["workday"] and len(posts) == 1        # verified once: kept in boards.json


# ------------------------------------------------------------------ 7. how each row is applied to
@pytest.mark.parametrize("job, route", [
    ({"url": "https://boards.greenhouse.io/acme/jobs/1"}, "employer form (greenhouse)"),
    ({"url": "https://jobs.lever.co/acme/1"}, "employer form (lever)"),
    ({"url": "https://jobs.ashbyhq.com/acme/1"}, "employer form (ashby)"),
    ({"url": "https://acme.wd3.myworkdayjobs.com/External/job/1"}, "employer form (workday)"),
    ({"url": "https://www.reed.co.uk/jobs/data-scientist/123", "source": "reed"}, "Reed page (manual)"),
    ({"url": "https://www.reed.co.uk/jobs/data-scientist/123", "source": "reed", "apply_url": "https://www.reed.co.uk/jobs/data-scientist/123"},
     "Reed page (manual)"),
    ({"url": "https://www.reed.co.uk/jobs/data-scientist/123", "source": "reed", "apply_url": "https://job-boards.greenhouse.io/acme/jobs/9"},
     "employer form (greenhouse)"),
    ({"url": "https://www.reed.co.uk/jobs/data-scientist/123", "source": "reed", "apply_url": "https://apply.workable.com/acme/j/ABC123/"},
     "employer form (workable)"),
    ({"url": "https://www.reed.co.uk/jobs/data-scientist/123", "source": "reed", "apply_url": "https://careers.acme.com/apply/1"}, "unknown"),
    ({"url": "https://careers.acme.com/jobs/1"}, "unknown"),
    ({}, "unknown"),
])
def test_the_apply_route(job, route):
    assert shortlist.apply_route(job) == route


def test_a_reed_row_says_reed_page_and_one_with_an_ats_external_url_says_employer_form(settings, db, fb, inbox):
    plain = ad_text(company="Acme", title="Data Scientist", url="https://www.reed.co.uk/jobs/data-scientist/111").replace("Location:", "Source: reed\nLocation:", 1)
    ext = ad_text(company="Beta", title="Data Scientist", url="https://www.reed.co.uk/jobs/data-scientist/222").replace(
        "Location:", "Source: reed\nApply URL: https://jobs.ashbyhq.com/beta/0a1b2c3d-1111-2222-3333-444455556666\nLocation:", 1)
    (inbox / "a.txt").parent.mkdir(parents=True, exist_ok=True)
    (inbox / "a.txt").write_text(plain, encoding="utf-8")
    (inbox / "b.txt").write_text(ext, encoding="utf-8")
    by = {r.job["company"]: r.line(1) for r in build(settings, db, fb).rows}
    assert "| apply: Reed page (manual)" in by["Acme"] and "| apply: employer form (ashby)" in by["Beta"]
