"""`careerops shortlist` (discovery/shortlist.py): the best few inbox ads are OFFERED, the human approves, and only approved ads reach data/jds/approved/. Caps,
cooldown, the age rule, the hard stops and the choices. No network, no model."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from careerops import cli, targeting
from careerops.config import load_yaml
from careerops.discovery import blacklist, gates, liveness, shortlist
from careerops.discovery import register as sponsors
from conftest import PROFILE_FIX
from triage_fakes import NOW, FakeIndex, write_ad


@pytest.fixture
def fb():
    return load_yaml(PROFILE_FIX / "fact_bank.yaml")


@pytest.fixture(autouse=True)
def roles(settings, fb, monkeypatch, tmp_path):
    gates.load_roles(settings, fb)
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")


@pytest.fixture
def inbox(settings):
    return settings.path("jds") / "inbox"


def build(settings, db, fb, **kw):
    return shortlist.build(settings, db, fb, now=NOW, archive=[], **kw)


def names(plan):
    return [r.job["company"] for r in plan.rows]


def apply_today(db, job_id, company, *, days_ago=0, title="Data Scientist"):
    db.upsert_job({"id": job_id, "source": "manual", "company": company, "title": title, "description": "x"})
    when = NOW.replace(hour=9) - timedelta(days=days_ago)
    db.update_job(job_id, status="applied", applied_at=when.isoformat(timespec="seconds"))


# ------------------------------------------------------------------ what is offered
def test_the_best_t_comes_first_then_the_freshest(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Alpha", required=("Python", "SQL", "scikit-learn", "Spark"), age_days=2)       # E 4: 0.25 lower
    write_ad(inbox, "b.txt", company="Beta", age_days=2)
    write_ad(inbox, "c.txt", company="Gamma", age_days=1)                                                            # the same T as Beta, but fresher
    write_ad(inbox, "d.txt", company="Delta", age_days=6)                                                            # Fr 4: 0.1 lower
    plan = build(settings, db, fb)
    assert names(plan) == ["Gamma", "Beta", "Delta", "Alpha"]
    ts = [r.t.t for r in plan.rows]
    assert ts == sorted(ts, reverse=True) and ts[0] == ts[1]


def test_at_most_shortlist_size_rows_are_offered(settings, db, fb, inbox):
    for i in range(12):
        write_ad(inbox, f"{i:02d}.txt", company=f"Company{i}", age_days=1)
    assert len(build(settings, db, fb).rows) == 8
    settings.raw["targeting"]["shortlist_size"] = 3
    assert len(build(settings, db, fb).rows) == 3


def test_a_job_below_min_triage_score_is_never_offered(settings, db, fb, inbox):
    write_ad(inbox, "good.txt", company="Good")
    write_ad(inbox, "bad.txt", company="Bad", title="Plumber", required=("Spark", "Airflow"), age_days=40)
    plan = build(settings, db, fb)
    assert names(plan) == ["Good"] and plan.below_min == 1
    settings.raw["targeting"]["min_triage_score"] = 1.0
    assert sorted(names(build(settings, db, fb))) == ["Bad", "Good"]


def test_every_scored_ad_is_remembered_in_the_database(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Alpha")
    plan = build(settings, db, fb)
    row = db.conn.execute("SELECT * FROM triage_scores").fetchone()
    assert row["company"] == "Alpha" and row["t"] == pytest.approx(plan.rows[0].t.t, abs=1e-3)
    assert set(json.loads(row["factors"])) == {"A", "E", "V", "L", "Fr", "P"}


# ------------------------------------------------------------------ the age rule
@pytest.fixture
def exact_register(monkeypatch):
    monkeypatch.setattr(sponsors, "load_index", lambda db: FakeIndex(exact=["Old Ideal", "Old Meh"]))


def test_an_old_posting_needs_t_of_4_5(settings, db, fb, inbox, exact_register):
    write_ad(inbox, "ideal.txt", company="Old Ideal", age_days=90, salary="£70,000 - £90,000")            # A5 E5 V5 L5 Fr2 P5 = 4.7
    write_ad(inbox, "meh.txt", company="Old Meh", age_days=90, salary="£70,000 - £90,000", required=("Python", "SQL", "scikit-learn", "Spark"))   # E4: 4.45
    plan = build(settings, db, fb)
    assert names(plan) == ["Old Ideal"] and plan.too_old == 1
    assert plan.rows[0].t.t >= targeting.OLD_POSTING_MIN_T == 4.5


def test_the_age_limit_is_a_setting(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Alpha", age_days=45)
    settings.raw["targeting"]["max_posting_age_days"] = 30
    assert build(settings, db, fb).too_old == 1
    settings.raw["targeting"]["max_posting_age_days"] = 60
    assert names(build(settings, db, fb)) == ["Alpha"]


# ------------------------------------------------------------------ company limits
def test_a_company_at_its_open_limit_is_left_out(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme", title="Machine Learning Engineer")
    write_ad(inbox, "b.txt", company="Other")
    apply_today(db, "manual:1", "Acme")
    assert sorted(names(build(settings, db, fb))) == ["Acme", "Other"]                       # one open of two allowed
    apply_today(db, "manual:2", "Acme Ltd", title="AI Engineer")
    plan = build(settings, db, fb)
    assert names(plan) == ["Other"] and plan.held and "2 open application(s)" in plan.held[0][1]


def test_a_company_inside_its_cooldown_is_left_out_until_it_ends(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme")
    apply_today(db, "manual:1", "Acme", days_ago=10)
    db.conn.execute("INSERT INTO outcomes (job_id, event, on_date, source) VALUES ('manual:1', 'rejected', '2026-10-20', 'manual')")
    db.conn.commit()
    plan = build(settings, db, fb)
    assert names(plan) == [] and "cooling down" in plan.held[0][1] and "2026-11-19" in plan.held[0][1]
    settings.raw["targeting"]["per_company_cooldown_days"] = 5
    assert names(build(settings, db, fb)) == ["Acme"]


def test_archive_folders_for_the_company_count_as_applications(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme")
    when = (NOW - timedelta(days=1)).timestamp()
    for role in ("Data Scientist", "AI Engineer"):
        folder = settings.path("applications_auto") / "Acme Ltd" / role
        folder.mkdir(parents=True)
        os.utime(folder, (when, when))                                    # made yesterday, by the test clock
    plan = build(settings, db, fb)
    assert names(plan) == [] and "2 open application(s)" in plan.held[0][1]


# ------------------------------------------------------------------ hard stops
def test_a_disqualified_ad_is_filed_with_its_reason_and_never_offered(settings, db, fb, inbox):
    settings.raw["targeting"]["dealbreakers"] = ["people management"]
    write_ad(inbox, "boss.txt", company="Boss", extra="You will line-manage a team of 4 data scientists.")
    write_ad(inbox, "senior.txt", company="Sen", title="Senior Data Scientist")
    write_ad(inbox, "fine.txt", company="Fine")
    shortlist.run(settings, db, fb, ask=None, now=NOW, archive=[])
    assert not (inbox / "boss.txt").exists() and not (inbox / "senior.txt").exists() and (inbox / "fine.txt").exists()
    done = settings.path("jds_done") / "skipped"
    assert (done / "boss.txt").is_file() and (done / "senior.txt").is_file()
    assert 'DEALBREAKER "people management" ← "You will line-manage a team of 4 data scientists."' in (done / "boss.reason.txt").read_text(encoding="utf-8")
    assert "level excluded" in (done / "senior.reason.txt").read_text(encoding="utf-8")


def test_a_closed_posting_goes_to_closed_and_is_replaced_by_the_next_ad(settings, db, fb, inbox, monkeypatch):
    write_ad(inbox, "a.txt", company="Alpha", age_days=0.5)
    write_ad(inbox, "b.txt", company="Beta", age_days=1.5)
    monkeypatch.setattr(liveness, "check", lambda url, f, cache=None, now=None: liveness.Result(
        liveness.CLOSED if "alpha" in url else liveness.OPEN, "404") if hasattr(liveness, "Result") else None)
    plan = shortlist.build(settings, db, fb, fetcher=object(), cache=object(), now=NOW, archive=[])
    assert names(plan) == ["Beta"] and plan.dropped[0][1] == "closed"
    shortlist.apply_drops(settings, plan)
    assert (settings.path("jds_done") / "closed" / "a.txt").is_file()


def test_a_dry_run_moves_nothing(settings, db, fb, inbox):
    write_ad(inbox, "senior.txt", company="Sen", title="Senior Data Scientist")
    shortlist.run(settings, db, fb, ask=lambda p: pytest.fail("asked in a dry run"), now=NOW, dry_run=True, archive=[])
    assert (inbox / "senior.txt").exists()


# ------------------------------------------------------------------ the rows
def test_a_row_shows_company_role_age_t_and_factors_keywords_and_apply_kind(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme", age_days=3, required=("Python", "SQL", "scikit-learn", "Spark", "Airflow", "Kubernetes"), apply_kind="greenhouse")
    [row] = build(settings, db, fb).rows
    line = row.line(1)
    assert line.startswith(" 1. Acme | Data Scientist | 3 d | T ") and " = A5 E3 " in line and "Fr5" in line
    assert "have: python, sql, scikit-learn" in line and "lack: spark, airflow" in line and line.endswith("| greenhouse")


def test_the_apply_kind_comes_from_the_address_when_the_ad_has_no_header(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Acme", url="https://jobs.lever.co/acme/123")
    assert build(settings, db, fb).rows[0].kind == "lever"


# ------------------------------------------------------------------ the choices
def run_with(settings, db, fb, *answers):
    it = iter(answers)
    seen = []

    def ask(prompt):
        seen.append(prompt)
        return next(it)
    out = []
    plan = shortlist.run(settings, db, fb, ask=ask, out=out.append, now=NOW, archive=[])
    return plan, out, seen


def five(inbox):
    for i, c in enumerate(("Alpha", "Beta", "Gamma", "Delta", "Eps")):
        write_ad(inbox, f"{i}.txt", company=c, age_days=1 + i * 0.1)


def test_a_approves_every_row_into_the_dated_folder(settings, db, fb, inbox):
    five(inbox)
    plan, out, _ = run_with(settings, db, fb, "a")
    folder = settings.path("jds") / "approved" / "2026-10-27"
    assert sorted(p.name for p in folder.iterdir()) == ["0.txt", "1.txt", "2.txt", "3.txt", "4.txt"] and not list(inbox.iterdir())
    assert len(shortlist.approved_files(settings)) == 5


def test_numbers_approve_just_those(settings, db, fb, inbox):
    five(inbox)
    plan, out, seen = run_with(settings, db, fb, "1 3 5", "q")
    approved = {p.name for p in shortlist.approved_files(settings)}
    assert approved == {r.path.name for n, r in enumerate(plan.rows, 1) if n in (1, 3, 5)}
    assert len(list(inbox.iterdir())) == 2 and len(seen) == 2                       # the other two are still waiting


def test_s_skips_a_job_for_good(settings, db, fb, inbox):
    five(inbox)
    plan, out, _ = run_with(settings, db, fb, "s 2", "q")
    skipped = settings.path("jds_done") / "skipped"
    name = plan.rows[1].path.name
    assert (skipped / name).is_file() and "skipped at the shortlist" in (skipped / (name[:-4] + ".reason.txt")).read_text(encoding="utf-8")
    assert not shortlist.approved_files(settings) and not (inbox / name).exists()
    from careerops.apply import decided
    assert decided.group_files(skipped, decided.key_of(plan.rows[1].job["company"], plan.rows[1].job["title"]))        # discover will not bring it back


def test_q_leaves_everything_where_it_is(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    assert len(list(inbox.iterdir())) == 5 and not shortlist.approved_files(settings)


@pytest.mark.parametrize("bad", ["", "9", "0", "x", "s", "s 9", "1 x"])
def test_an_answer_that_is_not_understood_changes_nothing_and_asks_again(settings, db, fb, inbox, bad):
    five(inbox)
    plan, out, seen = run_with(settings, db, fb, bad, "q")
    assert len(seen) == 2 and not shortlist.approved_files(settings) and len(list(inbox.iterdir())) == 5
    assert any("not understood" in line for line in out)


def test_ctrl_c_or_end_of_input_approves_nothing_more(settings, db, fb, inbox):
    five(inbox)

    def eof(prompt):
        raise EOFError
    shortlist.run(settings, db, fb, ask=eof, out=lambda s: None, now=NOW, archive=[])
    assert not shortlist.approved_files(settings) and len(list(inbox.iterdir())) == 5


def test_without_a_terminal_the_list_is_printed_and_nothing_is_approved(settings, db, fb, inbox):
    five(inbox)
    out = []
    shortlist.run(settings, db, fb, ask=None, out=out.append, now=NOW, archive=[])
    assert "no terminal" in "\n".join(out) and not shortlist.approved_files(settings)


def test_the_parser():
    assert shortlist.parse("a", 5) == ("all", []) and shortlist.parse("Q", 5) == ("quit", [])
    assert shortlist.parse("1, 3 5", 5) == ("approve", [1, 3, 5]) and shortlist.parse("s 2", 5) == ("skip", [2])
    assert shortlist.parse("6", 5)[0] == "bad" and shortlist.parse("s", 5)[0] == "bad"


# ------------------------------------------------------------------ the command and the settings
def test_the_command_prints_at_most_eight_rows_with_factors(settings, db, fb, inbox, tmp_path, capsys):
    import yaml
    for i in range(11):
        write_ad(inbox, f"{i:02d}.txt", company=f"Company{i}", age_days=1, base=datetime.now(timezone.utc))        # the command uses the real clock
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "shortlist", "--dry-run", "--no-liveness"])
    out = capsys.readouterr().out
    rows = [l for l in out.splitlines() if l[:3].strip().rstrip(".").isdigit() and " | T " in l]
    assert len(rows) == 8 and all(" = A" in l for l in rows)
    assert len(list(inbox.iterdir())) == 11                                  # dry run: nothing moved


def test_the_shortlist_on_the_mock_root_uses_no_network_and_moves_nothing(settings, tmp_path, capsys):
    import yaml
    settings.raw["paths"]["mock_root"] = str(tmp_path / "mock")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "shortlist", "--mock"])
    assert "isolated data root" in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("shortlist_size", 0), ("min_triage_score", 6), ("min_triage_score", "high"), ("daily_max_applications", 0),
                                       ("per_company_max_open", 1.5), ("max_posting_age_days", -1), ("dealbreakers", "people management"),
                                       ("dealbreakers", [3])])
def test_a_targeting_value_that_cannot_be_used_is_refused_by_name(settings, key, value):
    settings.raw["targeting"][key] = value
    with pytest.raises(targeting.TargetingError, match=key):
        targeting.config(settings)


def test_the_shipped_defaults_and_an_absent_block():
    assert targeting.config(type("S", (), {"raw": {}})()) == targeting.Targeting()
    t = targeting.Targeting()
    assert (t.shortlist_size, t.min_triage_score, t.daily_max_applications, t.per_company_max_open, t.per_company_cooldown_days, t.max_posting_age_days,
            t.dealbreakers) == (8, 3.5, 5, 2, 30, 60, ())


def test_the_test_settings_carry_the_block_with_the_agreed_values(settings):
    assert targeting.config(settings) == targeting.Targeting()
