"""Per-country visa answers (apply/visa.py): answers = visa.by_country[W] else .default; {country} in the summary; a remote role at home is only
ever called visa.remote.label; W unknown → `needs_country`, which never reaches apply; the review screen shows W, its evidence and the
booleans; `set-country` gives a job its place. No browser, no network, no model."""
import copy
import re

import pytest
import yaml
from conftest import PROFILE_FIX

from careerops import cli
from careerops.apply import visa
from careerops.apply.errors import ArchiveError
from careerops.db import DB
from careerops.discovery import workcountry
from careerops.discovery.workcountry import Rules

ADS = "Acme builds forecasting tools for retailers. You will join a small team of data scientists and ship models. " * 12
HOME_WORD = re.compile(r"(?<![\w])(?:united kingdom|uk|u\.k\.|gb|great britain|britain|england)(?![\w])", re.I)


@pytest.fixture
def applicant():
    return yaml.safe_load((PROFILE_FIX / "applicant.yaml").read_text(encoding="utf-8"))


def mk(code, mode="onsite", evidence="x"):
    """A Work for `code` the way the resolver builds it (home is GB)."""
    return workcountry._mk(Rules(home="GB", label="Remote"), code, mode, evidence)


def job(location="", ad="", **kw):
    return {"id": "j1", "company": "Acme", "title": "Data Scientist", "location": location, "description": ADS + ad, "out_dir": None, **kw}


# ------------------------------------------------------------------ which answers W gets
def test_the_home_office_gets_the_home_block(applicant):
    p = visa.plan_for(applicant, mk("GB"))
    assert p.source == "by_country.GB" and [p.answer(k) for k in visa.KEYS] == ["Yes", "No", "Yes"]
    assert p.summary.startswith("I have completed my MSc")


def test_unrestricted_remote_gets_the_home_answers_and_the_source_does_not_name_the_country(applicant):
    p = visa.plan_for(applicant, mk("GB", "remote", "Location: Remote"))
    assert [p.answer(k) for k in visa.KEYS] == ["Yes", "No", "Yes"] and p.source == "the home block"
    assert p.work.shown == "Remote" and "Remote" in p.work.line() and not HOME_WORD.search(p.describe() + p.work.line())


def test_a_place_with_its_own_block_uses_it_whatever_the_key_is_called(applicant):
    for key in ("IE", "Ireland", "ie"):
        a = copy.deepcopy(applicant)
        a["visa"]["by_country"][key] = {"right_to_work": True, "sponsorship_now": False, "sponsorship_future": False, "summary": "Irish."}
        p = visa.plan_for(a, mk("IE"))
        assert p.source == f"by_country.{key}" and p.answer("right_to_work") == "Yes" and p.summary == "Irish.", key


def test_any_other_place_gets_the_default_with_it_named_in_the_summary(applicant):
    p = visa.plan_for(applicant, mk("DE", "remote"))
    assert p.source == "by_country.default"
    assert [p.answer(k) for k in visa.KEYS] == ["No", "Yes", "Yes"]                    # right to work no, sponsorship now yes, future yes
    assert p.summary == "I would need a work visa for Germany and am open to sponsorship."
    assert "default" in p.describe() and "sponsorship now Yes" in p.describe()
    assert visa.plan_for(applicant, mk("EU", "remote")).summary == "I would need a work visa for EU and am open to sponsorship."


def test_a_missing_home_block_falls_to_the_default_which_is_why_doctor_fails_on_it(applicant):
    del applicant["visa"]["by_country"]["GB"]
    assert visa.plan_for(applicant, mk("GB")).source == "by_country.default"


def test_without_a_default_a_place_with_no_block_has_no_answers(applicant):
    del applicant["visa"]["by_country"]["default"]
    assert visa.plan_for(applicant, mk("IE")) is None
    assert visa.plan_for(applicant, mk("GB")) is not None


def test_an_unknown_place_has_no_plan_and_no_applicant_visa_block_means_none(applicant):
    assert visa.plan_for(applicant, workcountry._mk(Rules("GB"), None, "unknown", reason="x")) is None
    assert visa.plan_for({}, mk("GB")) is None and visa.plan_for(None, mk("GB")) is None


def test_only_real_booleans_are_answers(applicant):
    applicant["visa"]["by_country"]["GB"].update(right_to_work="Yes", sponsorship_now=None)
    p = visa.plan_for(applicant, mk("GB"))
    assert [p.answer(k) for k in visa.KEYS] == [None, None, "Yes"] and "ASK" in p.describe()


# ------------------------------------------------------------------ W unknown never reaches apply
def test_require_plan_refuses_an_unknown_place_and_says_how_to_fix_it(applicant):
    with pytest.raises(visa.WorkCountryError, match="set-country") as e:
        visa.require_plan(job(location=""), applicant)
    assert isinstance(e.value, ArchiveError)                                          # the callers that catch ArchiveError refuse it too


def test_require_plan_refuses_a_place_with_no_block_and_no_default(applicant):
    del applicant["visa"]["by_country"]["default"]
    with pytest.raises(visa.WorkCountryError, match="no visa.by_country block for Ireland and no visa.by_country.default"):
        visa.require_plan(job(location="Dublin, Ireland"), applicant)


def test_require_plan_returns_the_plan_for_a_known_place(applicant):
    p = visa.require_plan(job(location="Dublin, Ireland"), applicant)
    assert p.work.country == "IE" and p.source == "by_country.default"
    assert visa.require_plan(job(location="Remote"), applicant).work.home_remote


def test_assist_refuses_before_a_browser_is_opened(applicant, monkeypatch, tmp_path):
    import playwright.sync_api as pw
    from careerops.apply.assist import assist
    monkeypatch.setattr(pw, "sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("a browser was opened")))
    with pytest.raises(visa.WorkCountryError):
        assist({**job(location=""), "apply_url": "https://x.example/apply", "url": "https://x.example"}, {}, applicant, tmp_path)


def test_apply_refuses_a_needs_country_job_and_opens_nothing(settings, db, monkeypatch, capsys):
    db.upsert_job({"id": "j1", "source": "manual", "company": "Acme", "title": "Data Scientist", "location": "", "url": "https://x.example",
                   "apply_url": "https://x.example", "description": ADS})
    db.update_job("j1", status="approved", out_dir="/nowhere")
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(cli.review, "load_report", lambda j: {})
    monkeypatch.setattr(cli, "export", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", lambda p="": (_ for _ in ()).throw(AssertionError("asked to open a form")))
    with pytest.raises(SystemExit) as e:
        cli.main(["apply", "--id", "j1"])
    assert e.value.code == 1 and "REFUSED j1" in capsys.readouterr().err


# ------------------------------------------------------------------ the review screen
def test_the_review_screen_shows_w_its_evidence_and_the_booleans(applicant):
    lines = visa.review_lines(job(location="Dublin, Ireland"), applicant)
    assert lines[0] == 'Work location: Ireland (IE), onsite — evidence: "Location: Dublin, Ireland"'
    assert lines[1].startswith("Visa answers (by_country.default): right to work No") and "sponsorship now Yes" in lines[1]


def test_the_review_screen_for_a_remote_role_at_home_says_only_the_label(applicant):
    lines = visa.review_lines(job(location="Remote (UK)", ad="Open to candidates in the UK."), applicant)
    assert lines[0].startswith("Work location: Remote — evidence: ") and "[home]" in lines[0]
    assert lines[1] == "Visa answers (the home block): right to work Yes · sponsorship now No · sponsorship in the future Yes"
    assert not HOME_WORD.search("\n".join(lines))
    assert visa.displayed_location(job(location="Remote (UK)"), applicant) == "Remote"
    assert visa.displayed_location(job(location="Bristol, UK"), applicant) == "Bristol, UK"
    assert visa.displayed_location(job(location="Remote - US", ad="Candidates must be located in the United States."), applicant) == "Remote - US"


def test_the_review_screen_says_when_the_place_is_unknown_or_has_no_answers(applicant):
    (line,) = visa.review_lines(job(location=""), applicant)
    assert line.startswith("Work location: UNKNOWN") and "needs_country" in line
    del applicant["visa"]["by_country"]["default"]
    assert visa.review_lines(job(location="Dublin"), applicant)[1] == \
        "Visa answers: NONE (no visa.by_country block for Ireland and no default in applicant.yaml)"


@pytest.mark.slow
def test_the_summary_prints_the_work_location_lines_and_the_header_location(settings, tailored):
    from careerops.review import workflow as review
    rep, _, _ = tailored
    db = DB(settings.path("db"))
    text = review.summary(db.job("manual:northwind1"), rep, settings)
    assert 'Work location: United Kingdom (GB), onsite — evidence: "Location: Bristol, UK"' in text
    assert "Visa answers (by_country.GB): right to work Yes · sponsorship now No · sponsorship in the future Yes" in text
    assert "— AI Engineer  [Bristol, UK]" in text
    db.update_job("manual:northwind1", location="Remote (UK)", work_location="Remote")
    remote = review.summary(db.job("manual:northwind1"), rep, settings)
    assert "— AI Engineer  [Remote]" in remote and "Work location: Remote — evidence:" in remote
    assert not HOME_WORD.search(remote.split("Profile:")[0].replace("Northwind Health AI", ""))      # nothing above the CV paths names the home country


# ------------------------------------------------------------------ triage, the label column and set-country
@pytest.fixture
def run(settings, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    return lambda *argv: cli.main(list(argv))


def add(db, jid="j1", location="", status="new", ad=ADS):
    db.upsert_job({"id": jid, "source": "manual", "company": "Acme", "title": "Data Scientist", "location": location,
                   "url": "https://x.example/1", "apply_url": "https://x.example/1", "description": ad})
    db.update_job(jid, status=status)


def test_a_job_with_no_place_is_needs_country_and_set_country_triages_it_again(settings, db, run, capsys):
    add(db)
    cli.triage_new(db, settings)
    j = db.job("j1")
    assert j["status"] == "needs_country" and j["work_country"] is None and j["reason"].startswith("work country unknown")
    run("set-country", "--id", "j1", "--country", "Ireland")
    j = db.job("j1")
    assert (j["status"], j["work_country"]) == ("triaged", "IE") and j["work_evidence"].startswith("set by you")
    assert "work location set by you: Ireland." in capsys.readouterr().out


def test_set_country_never_prints_the_home_country(settings, db, run, capsys):
    add(db, location="Remote")
    cli.triage_new(db, settings)
    run("set-country", "--id", "j1", "--country", "GB")
    out = capsys.readouterr().out
    assert "work location set by you." in out and not HOME_WORD.search(out)


def test_set_country_takes_a_region(settings, db, run):
    add(db)
    run("set-country", "--id", "j1", "--country", "eu")
    assert db.job("j1")["work_country"] == "EU"


def test_retriage_keeps_the_place_you_set(settings, db, run):
    add(db)
    cli.triage_new(db, settings)
    run("set-country", "--id", "j1", "--country", "DE")
    cli.retriage(db, settings)
    assert db.job("j1")["work_country"] == "DE" and db.job("j1")["status"] == "triaged"


def test_retriage_gives_a_needs_country_job_a_second_look(settings, db):
    add(db)
    cli.triage_new(db, settings)
    db.update_job("j1", location="Bristol, UK")
    counts = cli.retriage(db, settings)
    assert db.job("j1")["status"] == "triaged" and db.job("j1")["work_country"] == "GB" and counts["triaged"] == 1


def test_triage_stores_w_and_for_a_remote_role_at_home_only_the_label(settings, db):
    add(db, location="Bristol, UK")
    add(db, "j2", location="Dublin, Ireland")
    add(db, "j3", location="Remote (UK)", ad=ADS + "This role is fully remote and open to candidates in the UK.")
    add(db, "j4", location="Remote - US", ad=ADS + "Candidates must be located in the United States.")
    cli.triage_new(db, settings)
    j1, j2, j3, j4 = (db.job(i) for i in ("j1", "j2", "j3", "j4"))
    assert (j1["status"], j1["work_country"], j1["work_evidence"], j1["work_location"]) == ("triaged", "GB", "Location: Bristol, UK", None)
    # the location rule is the user's own and is unchanged: Ireland is outside it unless applicant.yaml has an IE block
    assert (j2["status"], j2["work_country"]) == ("skipped", "IE") and "not in allow-list" in j2["reason"]
    assert (j3["status"], j3["work_country"], j3["work_location"]) == ("triaged", "GB", "Remote")
    assert "[home]" in j3["work_evidence"] and not HOME_WORD.search(j3["work_evidence"])
    assert (j4["work_country"], j4["work_location"]) == ("US", None)


def test_the_excel_export_shows_the_label_not_the_ads_words_for_a_remote_role_at_home(settings, db):
    from openpyxl import load_workbook
    from careerops.reporting.excel import export
    add(db, location="Remote (UK)", ad=ADS + "This role is fully remote and open to candidates in the UK.")
    add(db, "j2", location="Bristol, UK")
    cli.triage_new(db, settings)
    export(db, settings.path("excel"))
    ws = load_workbook(settings.path("excel"))["Jobs"]
    col = [c.value for c in ws[1]].index("Location")
    assert sorted(str(r[col].value) for r in ws.iter_rows(min_row=2)) == ["Bristol, UK", "Remote"]


def test_set_country_refuses_an_unknown_place_an_unknown_job_and_an_applied_job(settings, db, run, capsys):
    add(db)
    for argv in (("--id", "j1", "--country", "Narnia"), ("--id", "nope", "--country", "DE")):
        with pytest.raises(SystemExit) as e:
            run("set-country", *argv)
        assert e.value.code == 1
    add(db, "j2", status="applied")
    with pytest.raises(SystemExit):
        run("set-country", "--id", "j2", "--country", "DE")
    err = capsys.readouterr().err
    assert "not a country or region this tool knows" in err and "no such job" in err and "already applied" in err
    assert db.job("j1")["work_country"] is None and db.job("j2")["work_country"] is None


def test_a_needs_country_job_is_never_picked_for_tailoring(settings, db):
    from careerops.tailoring.queue import select_for_run
    add(db)
    add(db, "j2", location="Bristol, UK")
    cli.triage_new(db, settings)
    assert db.job("j1")["status"] == "needs_country" and db.job("j2")["status"] == "triaged"
    assert [j["id"] for j in select_for_run(db.jobs("triaged"), 10, settings)] == ["j2"]       # what `tailor` selects from


def test_the_schema_change_is_additive(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL, "
                      "location TEXT, url TEXT, apply_url TEXT, description TEXT, posted_at TEXT, first_seen TEXT NOT NULL, "
                      "last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new');"
                      "INSERT INTO jobs (id, source, company, title, first_seen, last_seen) VALUES ('a','m','Acme','DS','t','t');")
    con.commit()
    con.close()
    db = DB(p)
    assert {"work_country", "work_evidence", "work_location"} <= {r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert db.job("a")["company"] == "Acme" and db.job("a")["work_country"] is None
