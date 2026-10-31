"""`careerops shortlist` with a region and a company size (discovery/shortlist.py, region.py, size.py): out-of-region ads are PARKED (not scored, not skipped, back
with a later --region); the sizes of the queue are counted before the choice; `sme` / `large` keep a bucket and list the unknown ones under [u]; `*-first` reorders and
drops nobody. No network (the size resolver has no Companies House key) and no model."""
from datetime import datetime, timezone

import pytest
import yaml

from careerops import cli
from careerops.apply import decided
from careerops.config import load_yaml
from careerops.discovery import blacklist, gates, shortlist
from careerops.discovery import size as Z
from conftest import PROFILE_FIX
from triage_fakes import NOW, write_ad


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb, monkeypatch, tmp_path):
    gates.load_roles(settings, fb)
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")                 # the CLI loads .env: a real key there must never reach a test


@pytest.fixture
def inbox(settings):
    return settings.path("jds") / "inbox"


def ad(inbox, name, company, place="London, UK", **kw):
    p = write_ad(inbox, name, company=company, **kw)
    p.write_text(p.read_text(encoding="utf-8").replace("Location: London, UK", f"Location: {place}"), encoding="utf-8")
    return p


def sized(settings):
    settings.path("company_size_overrides").write_text("Acme: small\nBigco: large\n", encoding="utf-8")
    return Z.Resolver(settings, key="", today=NOW.date())


def build(settings, db, fb, **kw):
    return shortlist.build(settings, db, fb, now=NOW, archive=[], **kw)


def names(plan):
    return [r.job["company"] for r in plan.rows]


class Typed:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


# ------------------------------------------------------------------ region: parked, never skipped
def test_an_ad_outside_the_region_is_parked_not_scored_and_not_skipped(settings, db, fb, inbox):
    ad(inbox, "a.txt", "Acme")
    ad(inbox, "ny.txt", "Newyork", place="New York, NY")
    plan = build(settings, db, fb, regions=("uk",))
    assert names(plan) == ["Acme"] and plan.region_parked == ["Newyork | Data Scientist"] and plan.scored == 1
    assert [r[0] for r in db.conn.execute("SELECT company FROM triage_scores")] == ["Acme"]            # never scored
    assert (inbox / "ny.txt").exists() and decided.txt_files(decided.decision_dir(settings, "skipped")) == []        # never filed as skipped
    assert any("parked (region): 1 outside United Kingdom: not scored, not skipped" in l for l in shortlist.lines(plan, shortlist.targeting_mod.config(settings)))


def test_switching_the_region_brings_the_parked_ad_back(settings, db, fb, inbox):
    ad(inbox, "a.txt", "Acme")
    ad(inbox, "ny.txt", "Newyork", place="New York, NY")
    assert names(build(settings, db, fb, regions=("uk",))) == ["Acme"]
    assert sorted(names(build(settings, db, fb, regions=("any",)))) == ["Acme", "Newyork"]
    plan = build(settings, db, fb, regions=("us",))
    assert names(plan) == ["Newyork"] and plan.region_parked == ["Acme | Data Scientist"]
    assert sorted(names(build(settings, db, fb))) == ["Acme", "Newyork"]                                  # no region filter at all: the old behaviour


def test_an_ad_whose_place_does_not_say_is_kept_and_flagged_region_unknown(settings, db, fb, inbox):
    ad(inbox, "r.txt", "Remotely", place="Remote")
    plan = build(settings, db, fb, regions=("uk",))
    assert names(plan) == ["Remotely"] and plan.region_unknown == 1 and plan.rows[0].region.verdict == "unknown"
    assert plan.rows[0].line(1).endswith("[region?]")
    assert any("region unknown for 1 ad(s): kept, flagged [region?]" in l for l in shortlist.lines(plan, shortlist.targeting_mod.config(settings)))
    row = db.conn.execute("SELECT region_match, region_evidence FROM triage_scores WHERE company='Remotely'").fetchone()
    assert row["region_match"] == "unknown" and "does not say" in row["region_evidence"]


def test_a_remote_ad_that_names_the_uk_is_in_and_one_that_excludes_it_is_parked(settings, db, fb, inbox):
    ad(inbox, "ok.txt", "Welcome", place="Remote", extra="You must have the right to work in the UK.")
    ad(inbox, "no.txt", "Nope", place="Remote", extra="US-based candidates only.")
    plan = build(settings, db, fb, regions=("uk",))
    assert names(plan) == ["Welcome"] and plan.region_parked == ["Nope | Data Scientist"] and plan.region_unknown == 0


def test_a_job_abroad_in_a_region_you_chose_is_weighted_unknown_not_not_home(settings, db, fb, inbox):
    ad(inbox, "ny.txt", "Newyork", place="New York, NY")
    plain = build(settings, db, fb)                                                                       # no region chosen: the job is "not home"
    chosen = build(settings, db, fb, regions=("us",))                                                     # you chose the US: the UK register cannot say
    assert plain.rows[0].t.v == pytest.approx(1.0) and chosen.rows[0].t.v == pytest.approx(3.5)
    assert chosen.rows[0].t.t > plain.rows[0].t.t


# ------------------------------------------------------------------ size
@pytest.fixture
def trio(inbox):
    ad(inbox, "acme.txt", "Acme", required=("Python", "SQL", "scikit-learn", "Spark"))                 # E 4: its T is 0.25 below the others
    ad(inbox, "bigco.txt", "Bigco")
    ad(inbox, "mystery.txt", "Mystery")                                                                   # no override, no key: size unknown


def test_the_sizes_of_the_queue_are_counted_before_the_choice_and_each_ad_is_tagged(settings, db, fb, trio):
    seen = []
    plan = build(settings, db, fb, regions=("uk",), size_resolver=sized(settings), choose_size=lambda c: (seen.append(dict(c)), "both")[1])
    assert seen == [{"sme": 1, "large": 1, "unknown": 1}] and plan.size_counts == {"sme": 1, "large": 1, "unknown": 1}
    tags = {r.job["company"]: r.tags() for r in plan.rows}
    assert tags == {"Acme": " [SME·small]", "Bigco": " [Large]", "Mystery": " [size?]"}
    assert all(l.rstrip().endswith("]") for l in shortlist.lines(plan, shortlist.targeting_mod.config(settings)) if " | T " in l)
    row = db.conn.execute("SELECT size, size_label, size_evidence FROM triage_scores WHERE company='Acme'").fetchone()
    assert (row["size"], row["size_label"]) == ("sme", "small") and row["size_evidence"].startswith("override")


def test_without_a_resolver_nothing_about_size_is_asked_or_shown(settings, db, fb, trio):
    called = []
    plan = build(settings, db, fb, regions=("uk",), choose_size=lambda c: called.append(c) or "sme")
    assert called == [] and plan.size_mode is None and all(r.size is None and r.tags() == "" for r in plan.rows)


def test_sme_keeps_the_sme_bucket_and_lists_the_unknown_ones_apart(settings, db, fb, trio):
    plan = build(settings, db, fb, size_mode="sme", size_resolver=sized(settings))
    assert names(plan) == ["Acme"] and [r.job["company"] for r in plan.unknown_rows] == ["Mystery"] and plan.size_hidden == 1
    text = "\n".join(shortlist.lines(plan, shortlist.targeting_mod.config(settings)))
    assert "Size unknown (1): [u] include them" in text and "1 in the other size bucket" in text
    assert names(build(settings, db, fb, size_mode="large", size_resolver=sized(settings))) == ["Bigco"]


def test_the_unknown_ones_do_not_use_the_daily_slots_until_u(settings, db, fb, trio, inbox):
    settings.raw["targeting"]["shortlist_size"] = 2
    ad(inbox, "acme2.txt", "Acme", title="Machine Learning Engineer")
    plan = build(settings, db, fb, size_mode="sme", size_resolver=sized(settings))
    assert names(plan) == ["Acme", "Acme"] and len(plan.rows) == 2 and len(plan.unknown_rows) == 1


def test_both_changes_nothing_and_the_first_modes_reorder_without_dropping_anyone(settings, db, fb, trio):
    assert names(build(settings, db, fb, size_mode="both", size_resolver=sized(settings))) == ["Bigco", "Mystery", "Acme"]
    sme = build(settings, db, fb, size_mode="sme-first", size_resolver=sized(settings))                   # T + 0.3 for SME beats Acme's 0.25 lower T
    large = build(settings, db, fb, size_mode="large-first", size_resolver=sized(settings))
    assert names(sme) == ["Acme", "Bigco", "Mystery"] and names(large) == ["Bigco", "Mystery", "Acme"]
    assert sme.unknown_rows == [] and sme.size_hidden == 0 and sorted(names(sme)) == sorted(names(large)) == ["Acme", "Bigco", "Mystery"]
    assert sme.rows[0].t.t == pytest.approx(sme.rows[2].t.t - 0.25, abs=0.01) or sme.rows[0].t.t < sme.rows[1].t.t       # the displayed T is not changed


def test_the_company_caps_still_apply_to_every_size_mode(settings, db, fb, trio):
    db.upsert_job({"id": "m:1", "source": "manual", "company": "Acme", "title": "AI Engineer", "description": "x"})
    db.upsert_job({"id": "m:2", "source": "manual", "company": "Acme Ltd", "title": "Data Analyst", "description": "x"})
    for jid in ("m:1", "m:2"):
        db.update_job(jid, status="applied", applied_at=NOW.replace(hour=9).isoformat(timespec="seconds"))
    plan = build(settings, db, fb, size_mode="sme-first", size_resolver=sized(settings))
    assert "Acme" not in names(plan) and plan.held and "2 open application(s)" in plan.held[0][1]


# ------------------------------------------------------------------ the prompt: u and i
def run(settings, db, fb, ask, **kw):
    out = []
    kw.setdefault("regions", ("uk",))
    plan = shortlist.run(settings, db, fb, ask=ask, out=out.append, now=NOW, archive=[], **kw)
    return plan, "\n".join(out)


def test_pressing_u_adds_the_size_unknown_ads_to_the_list(settings, db, fb, trio):
    ask = Typed("u", "q")
    plan, out = run(settings, db, fb, ask, size_mode="sme", size_resolver=sized(settings))
    assert "[u] include the size-unknown ads" in ask.prompts[0] and "Size unknown (1): [u] include them" in out
    assert " 2. Mystery | Data Scientist" in out and "[size?]" in out
    log = (settings.path("jds") / shortlist.LOG).read_text(encoding="utf-8")
    assert '"company": "Mystery"' in log and plan.unknown_rows == []


def test_without_u_the_unknown_ads_are_never_logged_as_shown(settings, db, fb, trio):
    run(settings, db, fb, Typed("q"), size_mode="sme", size_resolver=sized(settings))
    assert "Mystery" not in (settings.path("jds") / shortlist.LOG).read_text(encoding="utf-8")


def test_u_cannot_overfill_the_daily_list(settings, db, fb, trio):
    settings.raw["targeting"]["shortlist_size"] = 1
    plan, out = run(settings, db, fb, Typed("u", "q"), size_mode="sme", size_resolver=sized(settings))
    assert "the list is full (1)" in out and [r.job["company"] for r in plan.unknown_rows] == ["Mystery"]


def test_u_files_a_closed_posting_away_instead_of_offering_it(settings, db, fb, trio, inbox, monkeypatch):
    monkeypatch.setattr(shortlist, "make_alive", lambda fetcher, cache, now: (lambda job: "the posting is closed (HTTP 404)" if job["company"] == "Mystery" else None))
    plan, out = run(settings, db, fb, Typed("u", "q"), size_mode="sme", size_resolver=sized(settings))
    assert "none of them is still open" in out and not (inbox / "mystery.txt").exists()
    assert any(p.name.startswith("mystery") for p in decided.txt_files(decided.decision_dir(settings, "closed")))


def test_i_prints_the_evidence_of_the_size_and_the_region(settings, db, fb, trio):
    ask = Typed("i", "q")
    _, out = run(settings, db, fb, ask, size_mode="both", size_resolver=sized(settings))
    assert "[i] evidence" in ask.prompts[0] and "size SME·small: override: Acme: small" in out and 'region in: location "London, UK": always_allow: UK' in out


def test_parse_knows_u_and_i():
    assert shortlist.parse("u", 3) == ("unknown", []) and shortlist.parse("i", 3) == ("info", []) and shortlist.parse("U", 3) == ("unknown", [])
    assert shortlist.parse("a", 3) == ("all", []) and shortlist.parse("2", 3) == ("approve", [2])


# ------------------------------------------------------------------ the command
def _cfg(settings, tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return str(cfg)


def test_the_command_prints_the_region_the_size_and_the_parked_count(settings, inbox, tmp_path, capsys):
    now = datetime.now(timezone.utc)
    ad(inbox, "a.txt", "Acme", base=now, age_days=1)
    ad(inbox, "ny.txt", "Newyork", place="New York, NY", base=now, age_days=1)
    cli.main(["--config", _cfg(settings, tmp_path), "shortlist", "--dry-run", "--no-liveness", "--region", "uk", "--size", "both"])
    out = capsys.readouterr().out
    assert "region: uk (from --region flag)" in out and "size: both (from --size flag)" in out
    assert "parked (region): 1 outside United Kingdom" in out and " 1. Acme | Data Scientist" in out and "[size?]" in out
    assert Z.KEY_ENV in out                                                                              # no key: said, not crashed
    assert len(list(inbox.iterdir())) == 2                                                               # dry run: nothing moved


def test_the_command_in_an_unattended_run_never_prompts(settings, inbox, tmp_path, capsys, monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail(f"a prompt appeared: {prompt!r}"))
    now = datetime.now(timezone.utc)
    ad(inbox, "a.txt", "Acme", base=now, age_days=1)
    cli.main(["--config", _cfg(settings, tmp_path), "shortlist", "--dry-run", "--no-liveness"])
    out = capsys.readouterr().out
    assert "region: uk (from default)" in out and "size: both (from default)" in out


@pytest.mark.parametrize("flag,value", [("--region", "narnia"), ("--size", "medium")])
def test_a_bad_region_or_size_is_refused(settings, tmp_path, flag, value):
    with pytest.raises(SystemExit):
        cli.main(["--config", _cfg(settings, tmp_path), "shortlist", "--dry-run", "--no-liveness", flag, value])


def test_a_non_uk_region_says_the_visa_check_does_not_apply(settings, inbox, tmp_path, capsys):
    now = datetime.now(timezone.utc)
    ad(inbox, "ny.txt", "Newyork", place="New York, NY", base=now, age_days=1)
    cli.main(["--config", _cfg(settings, tmp_path), "shortlist", "--dry-run", "--no-liveness", "--region", "us"])
    out = capsys.readouterr().out
    assert "non-UK region: visa/right-to-work not checked" in out and "Newyork" in out
