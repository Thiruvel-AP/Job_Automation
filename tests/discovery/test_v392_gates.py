"""v3.9.2: the blacklist and course-fee phrases are applied before any ad is written; the shortlist applies the knock-out list and an ATS-ceiling floor and
fills the daily slots by (score, ceiling, freshness); the judge is shown every bullet of the fact bank and told which ones the CV prints."""
from datetime import date
from types import SimpleNamespace

import pytest

from careerops.config import load_yaml
from careerops.discovery import blacklist, decide, discover as disc, gates
from careerops.discovery.candidate import Candidate
from careerops.review import judge as review_judge
from conftest import PROFILE_FIX
from test_agent_decide_v32 import FakeLLM, LONG, ad, approved_files, run  # noqa: F401  (fixtures below)
from test_discover import fetcher, make, TODAY  # noqa: F401
from netfake import Net

DESC = " ".join(["You will build forecasting models in Python and SQL for the pricing team and ship them behind a service every week."] * 6)


@pytest.fixture
def cfg(settings):
    settings.raw["sources"].update(greenhouse=[], lever=[], lever_eu=[], ashby=[])
    settings.raw["discover"]["sources"] = []
    settings.raw["discover"]["listed_only"] = {"enabled": False}
    return settings


def cand(company, text=DESC, title="Data Scientist", n=1):
    return Candidate(source="reed", company=company, title=title, location="London", url=f"https://example.org/{n}", description=text, posted="2026-10-09",
                     posted_source="ad")


# ------------------------------------------------------------------ 1. the blacklist at discovery
def test_an_itol_recruit_ad_is_never_written(cfg, db):
    d = make(cfg, db, Net(strict=False))
    assert d.accept(cand("ITOL Recruit"), "reed") == "dropped"
    assert not list((cfg.path("jds") / "inbox").glob("*.txt")) and d.run.sources["reed"]["blacklisted"] == 1 and d.run.written == []


def test_an_ad_that_says_fees_apply_is_never_written(cfg, db):
    d = make(cfg, db, Net(strict=False))
    assert d.accept(cand("Fine Co", DESC + " This is a training course and fees apply.", n=2), "ats") == "dropped"
    assert d.run.sources["ats"]["blacklisted"] == 1 and not list((cfg.path("jds") / "inbox").glob("*.txt"))
    assert d.accept(cand("Fine Co", n=3), "ats") == "written"                      # the same company without the phrase is written


def test_an_employer_on_your_blacklist_is_never_written_by_any_source(cfg, db, tmp_path, monkeypatch):
    (tmp_path / "blacklist.yaml").write_text("blacklist:\n  - {name: Acme Recruitment, scope: company, since: 2026-03-01, reason: ghosted me}\n", encoding="utf-8")
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")
    d = make(cfg, db, Net(strict=False))
    for label in ("reed", "ats", "gmail", "gmail_label"):
        assert d.accept(cand("Acme Recruitment Ltd", n=hash(label) % 1000), label) == "dropped"
        assert d.run.sources[label]["blacklisted"] == 1
    assert len(d.run.blacklisted) == 4 and "ghosted me" in d.run.blacklisted[0][2]


def test_reed_never_asks_for_the_details_of_a_blacklisted_employer(cfg, db, monkeypatch):
    d = make(cfg, db, Net(strict=False))
    assert d.wanted_cheap(cand("ITOL Recruit"), "reed") is False and d.run.sources["reed"]["blacklisted"] == 1


def test_the_existing_itol_files_move_out_of_the_inbox_and_the_subfolders_never_deleted(cfg, db):
    inbox = cfg.path("jds") / "inbox"
    for sub, name, company, text in (("", "itol-1.txt", "ITOL Recruit", DESC), ("2026-11-17", "itol-2.txt", "AI Jobs at ITOL Recruit", DESC),
                                     ("", "fees.txt", "Other Co", DESC + " Please note this is a training course and fees apply."), ("", "ok.txt", "Real Co", DESC)):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
        (inbox / sub / name).write_text(f"Company: {company}\nTitle: Trainee AI Engineer\nURL: https://x.example/{name}\n\n{text}\n", encoding="utf-8")
    assert len(disc.move_blacklisted_inbox(cfg, dry_run=True)) == 3 and (inbox / "itol-1.txt").exists()
    moved = disc.move_blacklisted_inbox(cfg)
    dest = cfg.path("jds_done") / "skipped_blacklist"
    assert len(moved) == 3 and sorted(p.name for p in dest.glob("*.txt") if not p.name.endswith(".reason.txt")) == ["fees.txt", "itol-1.txt", "itol-2.txt"]
    assert [p.name for p in inbox.rglob("*.txt")] == ["ok.txt"] and (dest / "itol-1.reason.txt").is_file()


# ------------------------------------------------------------------ 2. the shortlist
CLEARANCE = LONG + " To gain SC Clearance you will normally need at least 5 years' UK residency."


@pytest.fixture
def agent(settings, tmp_path, monkeypatch):
    fb = load_yaml(PROFILE_FIX / "fact_bank.yaml")
    gates.load_roles(settings, fb)
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")
    settings.raw["agent"] = {"decide": "veto", "max_evals_per_day": 12}
    return fb


def test_a_clearance_ad_is_skipped_at_the_shortlist_with_no_model_call(settings, db, agent):
    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    inbox = settings.path("jds") / "inbox"
    ad(inbox, "virtru.txt", "Virtru", title="Forward Deployed Engineer", extra=CLEARANCE)
    llm = FakeLLM()
    d = run(settings, db, agent, llm)
    assert llm.usage == [] and not approved_files(settings) and not (inbox / "virtru.txt").exists()
    assert (settings.path("jds_done") / "skipped_knockout" / "virtru.txt").is_file()
    assert any("5 years' UK residency" in why for _, _, why in d.plan.dropped)                 # the matched sentence is shown


def test_a_promoted_clearance_ad_is_not_knocked_out(settings):
    from careerops.apply import decided
    from careerops.discovery import shortlist
    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    job = {"company": "Virtru", "title": "Forward Deployed Engineer", "description": CLEARANCE}
    key = "|".join(decided.key_of("Virtru", "Forward Deployed Engineer"))
    assert shortlist._knockout(job, settings, set()) is not None and shortlist._knockout(job, settings, {key}) is None


@pytest.mark.parametrize("ceiling,kept", [(49.0, False), (49.9, False), (50.0, True), (91.0, True), (None, True)])
def test_a_ceiling_under_50_is_never_auto_approved(settings, ceiling, kept):
    got, why = decide.ceiling_floor(decide.AUTO, "", ceiling, settings)
    assert (got == decide.AUTO) is kept
    if not kept:
        assert got == decide.MARGINAL and f"{ceiling:.0f}%" in why and "never auto-approved" in why


def test_a_ready_job_is_floored_too_and_a_marginal_one_is_left_alone(settings):
    assert decide.ceiling_floor(decide.READY, "", 30.0, settings)[0] == decide.MARGINAL
    assert decide.ceiling_floor(decide.SKIP, "x", 30.0, settings) == (decide.SKIP, "x")


def item(name, score, ceiling, age=1.0):
    return decide.Item(name, name, "Data Scientist", decide.AUTO, ev=SimpleNamespace(global_score=score), ceiling=ceiling, age_days=age)


def test_with_five_slots_a_4_0_job_at_ceiling_91_beats_a_4_0_job_at_ceiling_49_for_the_last_slot(settings, db):
    day = date(2026, 11, 18)
    settings.raw["targeting"] = {**(settings.raw.get("targeting") or {}), "daily_max_applications": 5}
    items = [item(f"A{n}", 4.6, 90.0) for n in range(4)] + [item("low-ceiling", 4.0, 49.0), item("high-ceiling", 4.0, 91.0)]
    decide.caps(items, settings, db, day)
    by = {i.company: i.decision for i in items}
    assert by["high-ceiling"] == decide.AUTO and by["low-ceiling"] == decide.WAITING


def test_equal_score_and_ceiling_go_to_the_fresher_job_and_a_higher_score_still_wins_first(settings, db):
    day = date(2026, 11, 18)
    settings.raw["targeting"] = {**(settings.raw.get("targeting") or {}), "daily_max_applications": 1}
    items = [item("old", 4.0, 80.0, age=40), item("new", 4.0, 80.0, age=1)]
    decide.caps(items, settings, db, day)
    assert {i.company: i.decision for i in items} == {"new": decide.AUTO, "old": decide.WAITING}
    items = [item("hi-score-low-ceiling", 4.4, 55.0), item("lo-score-hi-ceiling", 4.0, 100.0)]
    decide.caps(items, settings, db, day)
    assert {i.company: i.decision for i in items} == {"hi-score-low-ceiling": decide.AUTO, "lo-score-hi-ceiling": decide.WAITING}


# ------------------------------------------------------------------ 3. the judge sees every bullet
def test_a_letter_claim_backed_only_by_an_unprinted_bullet_is_in_the_judges_input_and_labelled(fb_fixture):
    exp = fb_fixture["experience"][0]
    printed, unprinted = exp["bullets"][0], exp["bullets"][-1]
    assert printed["id"] != unprinted["id"]
    rep = {"context": {"experience": [{**exp, "bullet_ids": [printed["id"]], "bullets": [printed["text"]]}], "projects": []},
           "letter": {"status": "ok", "paragraphs": ["Hi team."], "evidence": []}}
    lines = review_judge.source_lines(fb_fixture, rep, ["Hi team."])
    assert f"[{printed['id']}] {printed['text']}" in lines                                    # printed: unlabelled, as before
    mine = next(x for x in lines if x.startswith(f"[{unprinted['id']} "))
    assert review_judge.NOT_PRINTED in mine and unprinted["text"] in mine
    from careerops.tailoring.tailor import bullet_index
    assert {i for i in bullet_index(fb_fixture) if any(x.startswith(f"[{i}") for x in lines)} == set(bullet_index(fb_fixture))      # every bullet of the bank
    assert not any("terms" in x for x in lines if x.startswith("["))


def test_the_prompts_tell_the_judge_an_unprinted_bullet_is_a_supported_source():
    from careerops.config import ROOT
    assert review_judge.NOT_PRINTED in review_judge.USER
    assert review_judge.NOT_PRINTED in (ROOT / "careerops" / "prompts" / "judge_review.md").read_text(encoding="utf-8")
