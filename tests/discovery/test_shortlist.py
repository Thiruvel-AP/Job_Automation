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
    write_ad(inbox, "a.txt", company="Acme", title="Machine Learning Engineer")                # another role: the applied one is excluded as decided
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
    assert "have: python, sql, scikit-learn" in line and "lack: spark, airflow" in line and line.endswith("| apply: employer form (greenhouse)")


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


@pytest.mark.parametrize("bad", ["", "9", "0", "x 2", "s", "s 9", "1 x"])
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


# ------------------------------------------------------------------ repeats: what is never offered again
def test_approved_jobs_waiting_in_approved_are_not_listed_again(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "1 2", "q")
    approved = {p.name for p in shortlist.approved_files(settings)}
    assert len(approved) == 2
    approved_keys = [shortlist.decided.file_key(p) for p in shortlist.approved_files(settings)]
    waiting_company = shortlist.decided.header_of(shortlist.approved_files(settings)[0])["company"]
    write_ad(inbox, "copy.txt", company=waiting_company, age_days=1)                       # the same job found again, under another file name
    write_ad(inbox, "new.txt", company="Newco", age_days=1)
    plan = build(settings, db, fb)
    assert "Newco" in names(plan) and plan.approved >= 1 and waiting_company not in names(plan)
    assert all(shortlist.decided.key_of(r.job["company"], r.job["title"]) not in approved_keys for r in plan.rows)
    assert {p.name for p in shortlist.approved_files(settings)} == approved                # nothing was taken away


@pytest.mark.parametrize("folder", ["skipped", "applied", "closed"])
def test_a_job_that_is_skipped_applied_or_closed_is_not_listed(settings, db, fb, inbox, folder):
    write_ad(settings.path("jds_done") / folder, "old.txt", company="Gone")
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    write_ad(inbox, "fresh.txt", company="Fresh", age_days=1)
    plan = build(settings, db, fb)
    assert names(plan) == ["Fresh"] and plan.decided == 1


def test_a_job_the_database_has_skipped_is_not_listed(settings, db, fb, inbox):
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    write_ad(inbox, "fresh.txt", company="Fresh", age_days=1)
    from careerops.apply import decided
    hdr = decided.header_of(inbox / "again.txt")
    db.upsert_job({"id": "manual:g1", "source": "manual", "company": "Gone", "title": "Data Scientist", "description": "x", "url": hdr.get("url")})
    db.update_job("manual:g1", status="skipped")
    assert names(build(settings, db, fb)) == ["Fresh"]                     # the same address: decided


def test_a_skipped_job_with_only_the_same_company_and_title_is_listed_as_a_possible_repost(settings, db, fb, inbox):
    db.upsert_job({"id": "manual:g1", "source": "manual", "company": "Gone", "title": "Data Scientist", "description": "x"})
    db.update_job("manual:g1", status="skipped")
    write_ad(inbox, "again.txt", company="Gone", age_days=1)
    write_ad(inbox, "fresh.txt", company="Fresh", age_days=1)
    plan = build(settings, db, fb)
    assert sorted(names(plan)) == ["Fresh", "Gone"]                        # v2.5: company + title alone never skip
    gone = next(r for r in plan.rows if r.job["company"] == "Gone")
    assert "possible repost of a job you skipped" in gone.tags()


# ------------------------------------------------------------------ [x]
def test_x_skips_every_job_left_in_the_list_and_keeps_the_approved_ones(settings, db, fb, inbox):
    five(inbox)
    plan, out, seen = run_with(settings, db, fb, "1 2", "x")
    assert len(seen) == 2                                                                    # [x] ended the session: no third prompt
    skipped = settings.path("jds_done") / "skipped"
    gone = sorted(p.name for p in skipped.glob("*.txt") if not p.name.endswith(".reason.txt"))
    assert gone == sorted(r.path.name for r in plan.rows[2:]) and len(shortlist.approved_files(settings)) == 2 and not list(inbox.iterdir())
    assert all("skipped (user, shortlist)" in (skipped / (n[:-4] + ".reason.txt")).read_text(encoding="utf-8") for n in gone)
    assert any("skipped 3" in line for line in out)
    company = plan.rows[4].job["company"]
    write_ad(inbox, "again.txt", company=company, age_days=1)                                # discovered again: still never offered
    assert company not in names(build(settings, db, fb))


def test_x_alone_skips_the_whole_list(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "x")
    assert not list(inbox.iterdir()) and not shortlist.approved_files(settings)
    assert len(shortlist.skipped_here(settings)) == 5


def test_unskip_shortlist_brings_skipped_ads_back_and_they_are_offered_again(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "x")
    assert shortlist.restore(settings, match="alpha") == ["back in the inbox: Alpha | Data Scientist (0.txt)"]
    assert names(build(settings, db, fb)) == ["Alpha"]
    assert len(shortlist.skipped_here(settings)) == 4
    assert len(shortlist.restore(settings)) == 4 and len(build(settings, db, fb).rows) == 5


# ------------------------------------------------------------------ counting and parking
def log_lines(settings):
    return [json.loads(l) for l in shortlist.log_path(settings).read_text(encoding="utf-8").splitlines()]


def test_each_list_you_were_asked_about_is_logged_with_times_shown(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Alpha", age_days=1)
    run_with(settings, db, fb, "q")
    write_ad(inbox, "b.txt", company="Beta", age_days=1)
    run_with(settings, db, fb, "q")
    shown = [e for e in log_lines(settings) if e["event"] == "shown" and e["company"] == "Alpha"]
    assert [e["times_shown"] for e in shown] == [1, 2]
    assert shortlist.log_path(settings).parent == settings.path("jds") and shortlist.log_path(settings).name == "shortlist_log.jsonl"


def test_new_since_last_shortlist_and_the_shown_mark(settings, db, fb, inbox):
    five(inbox)
    _, out, _ = run_with(settings, db, fb, "q")
    assert "new since last shortlist: 5" in "\n".join(out) and "(shown" not in "\n".join(out)
    write_ad(inbox, "n.txt", company="Newco", age_days=1)
    plan, out, _ = run_with(settings, db, fb, "q")
    text = "\n".join(out)
    assert "new since last shortlist: 1" in text
    assert text.count("(shown 1×)") == 5 and [r.shown for r in plan.rows].count(0) == 1


def test_the_third_showing_without_a_decision_parks_the_job(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")                                                          # 1st
    write_ad(inbox, "n1.txt", company="Newco1", age_days=1)
    run_with(settings, db, fb, "q")                                                          # 2nd
    write_ad(inbox, "n2.txt", company="Newco2", age_days=1)
    plan, out, _ = run_with(settings, db, fb, "q")                                           # 3rd: five were shown 3 times, none decided
    text = "\n".join(out)
    assert "(shown 2×)" in text and "parked 5" in text
    assert [e["company"] for e in log_lines(settings) if e["event"] == "parked"].count("Alpha") == 1
    write_ad(inbox, "n3.txt", company="Newco3", age_days=1)
    plan, out, _ = run_with(settings, db, fb, "q")
    text = "\n".join(out)
    assert sorted(names(plan)) == ["Newco1", "Newco2", "Newco3"]                              # the parked five are not in the list
    assert "parked: 5" in text and "Alpha" not in text
    assert len(list(inbox.iterdir())) == 8                                                   # parked ads stay in the inbox


def test_a_decision_stops_the_count_from_parking_a_job(settings, db, fb, inbox):
    write_ad(inbox, "a.txt", company="Alpha", age_days=1)
    run_with(settings, db, fb, "q")                                                          # 1st
    write_ad(inbox, "n0.txt", company="Newco0", age_days=1)
    run_with(settings, db, fb, "q")                                                          # 2nd
    write_ad(inbox, "n1.txt", company="Newco1", age_days=1)
    plan, _, _ = run_with(settings, db, fb, "1", "q")                                        # 3rd: Alpha is approved
    assert plan.rows[0].job["company"] == "Alpha" and len(shortlist.approved_files(settings)) == 1
    assert not [e for e in log_lines(settings) if e["event"] == "parked"]


def test_unskip_shortlist_offers_a_parked_job_again_from_zero(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    for i in range(2):
        write_ad(inbox, f"n{i}.txt", company=f"Newco{i}", age_days=1)
        run_with(settings, db, fb, "q")
    assert len(build(settings, db, fb).parked) == 5
    assert shortlist.restore(settings, match="alpha") == ["offered again: Alpha | Data Scientist (was parked)"]
    plan = build(settings, db, fb)
    assert "Alpha" in names(plan) and len(plan.parked) == 4 and [r.shown for r in plan.rows if r.job["company"] == "Alpha"] == [0]


def test_a_dry_run_and_a_run_without_a_terminal_log_nothing(settings, db, fb, inbox):
    five(inbox)
    shortlist.run(settings, db, fb, ask=None, out=lambda s: None, now=NOW, archive=[], dry_run=True)
    shortlist.run(settings, db, fb, ask=None, out=lambda s: None, now=NOW, archive=[])
    assert not shortlist.log_path(settings).exists()


# ------------------------------------------------------------------ nothing new
def test_an_empty_shortlist_says_so_and_names_the_commands(settings, db, fb, inbox):
    plan, out, seen = run_with(settings, db, fb)
    assert out == ["No new jobs yet. Run careerops discover --ats --gmail-alerts."] and seen == []


def test_nothing_new_since_the_last_list_prints_the_message_instead_of_the_list(settings, db, fb, inbox):
    five(inbox)
    run_with(settings, db, fb, "q")
    plan, out, seen = run_with(settings, db, fb)
    assert out == [f"No new jobs since {NOW.astimezone():%Y-%m-%d %H:%M}. Run careerops discover --ats --gmail-alerts."] and seen == []
    assert len(list(inbox.iterdir())) == 5
    assert len([e for e in log_lines(settings) if e["event"] == "shown"]) == 5                # the unseen list was not counted


def test_the_parser_knows_x():
    assert shortlist.parse("x", 5) == ("rest", []) and shortlist.parse("X", 5) == ("rest", []) and shortlist.parse("x 2", 5)[0] == "bad"


def test_unskip_shortlist_lists_then_brings_back_through_the_command(settings, db, fb, inbox, tmp_path, capsys):
    import yaml
    five(inbox)
    run_with(settings, db, fb, "x")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    cli.main(["--config", str(cfg), "unskip", "--shortlist"])
    listed = capsys.readouterr().out
    assert "Alpha | Data Scientist" in listed and "--match TEXT" in listed and len(list(inbox.iterdir())) == 0
    cli.main(["--config", str(cfg), "unskip", "--shortlist", "--match", "beta"])
    assert "back in the inbox: Beta | Data Scientist" in capsys.readouterr().out and len(list(inbox.iterdir())) == 1
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "unskip"])


def test_a_listed_only_ad_is_never_offered_so_it_cannot_sit_in_approved_for_ever(settings, db, fb, inbox):
    p = write_ad(inbox, "ny.txt", company="Newyork")
    p.write_text(p.read_text(encoding="utf-8").replace("Title:", "Status: listed_only\nCountry: United States\nTitle:", 1), encoding="utf-8")
    write_ad(inbox, "uk.txt", company="Ukco")
    plan = build(settings, db, fb)
    assert names(plan) == ["Ukco"] and plan.region_parked == ["Newyork | Data Scientist"]
