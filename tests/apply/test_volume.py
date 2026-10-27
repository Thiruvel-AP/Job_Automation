"""Volume caps (apply/volume.py) and where `live` enforces them: the daily cap (a stop with the count) and the per-company limit and cooldown (checked again right
before a form is opened), plus `live` working only on approved ads. No browser, no network, no model."""
from datetime import timedelta

import pytest

from careerops import cli
from careerops.apply import volume
from careerops.apply.live import Hooks, Live, StopSession
from careerops.config import load_yaml
from careerops.discovery import shortlist
from conftest import PROFILE_FIX
from triage_fakes import NOW, write_ad

DAY = NOW.date()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(volume, "today", lambda: DAY)


def applied(db, n, company="Acme", *, days_ago=0, title="Data Scientist", status="applied"):
    jid = f"manual:{company.lower().replace(' ', '')}{n}"
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": title, "description": "x"})
    db.update_job(jid, status=status, applied_at=(NOW.replace(hour=9) - timedelta(days=days_ago)).isoformat(timespec="seconds"))
    return jid


def outcome(db, jid, event, on):
    db.conn.execute("INSERT INTO outcomes (job_id, event, on_date, source) VALUES (?,?,?,'manual')", (jid, event, on))
    db.conn.commit()


# ------------------------------------------------------------------ the daily cap
def test_the_daily_cap_counts_todays_submits_by_anyone(settings, db):
    for i in range(4):
        applied(db, i, f"Company{i}")
    applied(db, 9, "Old Co", days_ago=1)                                  # yesterday does not count
    assert volume.applied_today(db, DAY) == 4 and volume.daily_block(db, settings, DAY) is None


def test_the_fifth_submit_reaches_the_cap_and_the_message_names_the_count(settings, db):
    for i in range(5):
        applied(db, i, f"Company{i}")
    why = volume.daily_block(db, settings, DAY)
    assert why is not None and "5 of 5" in why and "daily_max_applications" in why


def test_the_cap_is_the_setting(settings, db):
    settings.raw["targeting"]["daily_max_applications"] = 2
    applied(db, 1, "A")
    assert volume.daily_block(db, settings, DAY) is None
    applied(db, 2, "B")
    assert "2 of 2" in volume.daily_block(db, settings, DAY)


# ------------------------------------------------------------------ per company
def test_two_open_applications_at_a_company_block_a_third_across_roles(settings, db):
    applied(db, 1, "Acme", title="Data Scientist")
    assert volume.company_block(db, settings, "Acme", DAY) is None
    applied(db, 2, "Acme Ltd", title="AI Engineer")
    why = volume.company_block(db, settings, "ACME", DAY)
    assert why is not None and "2 open application(s)" in why and "per_company_max_open" in why
    assert volume.company_block(db, settings, "Other Co", DAY) is None


def test_a_reply_ends_openness(settings, db):
    a, b = applied(db, 1, "Acme"), applied(db, 2, "Acme", title="AI Engineer")
    outcome(db, a, "acknowledged", "2026-10-26")
    assert volume.company_block(db, settings, "Acme", DAY) is None
    outcome(db, b, "interview", "2026-10-26")
    assert volume.company_block(db, settings, "Acme", DAY) is None


def test_an_application_older_than_the_no_response_window_is_no_longer_open(settings, db):
    applied(db, 1, "Acme", days_ago=40)
    applied(db, 2, "Acme", days_ago=60, title="AI Engineer")
    assert volume.company_block(db, settings, "Acme", DAY) is None


def test_a_rejection_starts_the_cooldown_and_it_ends(settings, db):
    a = applied(db, 1, "Acme", days_ago=12)
    outcome(db, a, "rejected", "2026-10-20")
    assert "cooling down" in volume.company_block(db, settings, "Acme", DAY)
    assert volume.company_block(db, settings, "Acme", DAY + timedelta(days=22)) is not None       # 18 Nov: the day before it ends (19 Nov)
    assert volume.company_block(db, settings, "Acme", DAY + timedelta(days=23)) is None
    settings.raw["targeting"]["per_company_cooldown_days"] = 0
    assert volume.company_block(db, settings, "Acme", DAY) is None


def test_the_archive_counts_too_but_not_what_a_database_job_already_accounts_for(settings, db):
    import os
    when = (NOW - timedelta(days=2)).timestamp()
    for role in ("Data Scientist", "AI Engineer"):
        f = settings.path("applications") / "Acme" / role
        f.mkdir(parents=True)
        os.utime(f, (when, when))
    apps = volume.applications(db, settings)
    assert len([a for a in apps if a.source == "archive"]) == 2
    assert "2 open" in volume.company_block(db, settings, "Acme Limited", DAY)
    old = settings.path("applications") / "Zeta" / "Old role"
    old.mkdir(parents=True)
    os.utime(old, (when - 86400 * 400, when - 86400 * 400))
    assert volume.company_block(db, settings, "Zeta", DAY) is None                                # a year-old submission is not open
    jid = applied(db, 1, "Beta")
    mine = settings.path("applications_auto") / "Beta" / "Data Scientist"
    mine.mkdir(parents=True)
    db.update_job(jid, application_dir=str(mine))
    assert len([a for a in volume.applications(db, settings) if folder_is(a, "Beta")]) == 1       # the database job and its folder are one application


def folder_is(a, company):
    return a.company == company


# ------------------------------------------------------------------ live: the cap and the limit where the form opens
class Spy:
    """Hooks that record what live asks of them; nothing real happens."""

    def __init__(self):
        self.calls, self.asked = [], []

    def hooks(self) -> Hooks:
        def ask(prompt):
            self.asked.append(prompt)
            return "n"

        def rec(name):
            return lambda *a, **k: self.calls.append((name, a[0] if a else None))
        return Hooks(ask=ask, tailor=rec("tailor"), review=rec("review"), preflight=rec("preflight"), assist=rec("assist"), mark_applied=rec("mark_applied"))


def live_for(db, settings, spy, **kw):
    lines = []
    return Live(db, settings, spy.hooks(), out=lines.append, **kw), lines


def approved_job(db, jid="manual:acme9", company="Acme"):
    db.upsert_job({"id": jid, "source": "manual", "company": company, "title": "Data Scientist", "description": "x", "url": "https://acme.example/9"})
    db.update_job(jid, status="approved")
    return db.job(jid)


def test_the_sixth_submit_in_a_day_is_refused_the_fifth_is_not(settings, db):
    for i in range(4):
        applied(db, i, f"Company{i}")
    spy = Spy()
    live, _ = live_for(db, settings, spy)
    job = approved_job(db)
    live.dry_run = False
    r = type("R", (), {"note": "", "visa": []})()
    assert live._apply(job["id"], job, r) == "ready_not_submitted"                       # 4 done: the 5th goes on (and the test says no at the prompt)
    assert spy.asked and "Open the application form?" in spy.asked[0]
    applied(db, 5, "Company5")
    spy.asked.clear()
    spy.calls.clear()
    with pytest.raises(StopSession) as e:
        live._apply(job["id"], job, r)
    assert "5 of 5" in e.value.reason and spy.asked == [] and not spy.calls


def test_a_third_open_application_at_one_company_is_refused_before_a_form_opens(settings, db):
    applied(db, 1, "Acme")
    applied(db, 2, "Acme Ltd", title="AI Engineer")
    spy = Spy()
    live, lines = live_for(db, settings, spy)
    job = approved_job(db)
    r = type("R", (), {"note": "", "visa": []})()
    assert live._apply(job["id"], job, r) == "held (company limit)"
    assert spy.asked == [] and ("assist", job["id"]) not in spy.calls and "HELD" in "\n".join(lines)
    assert db.job(job["id"])["status"] == "approved" and "2 open application(s)" in r.note
    settings.raw["targeting"]["per_company_max_open"] = 3
    assert live._apply(job["id"], job, r) == "ready_not_submitted"                       # three are allowed: it goes on to the prompt
    assert spy.asked


def test_the_auto_path_is_blocked_by_the_company_limit_too(settings, db):
    applied(db, 1, "Acme")
    applied(db, 2, "Acme", title="AI Engineer")
    spy = Spy()
    live, lines = live_for(db, settings, spy)
    live.h.auto_gate = lambda jid: type("G", (), {"items": [], "ok": True, "line": lambda self: ""})()
    live.h.auto_apply = lambda jid: pytest.fail("auto-apply started at a company over its limit")
    live.h.preflight = lambda jid: pytest.fail("preflight ran")
    job = approved_job(db)
    r = type("R", (), {"note": "", "visa": []})()
    assert live._try_auto(job["id"], r) is None
    assert r.note.startswith("AUTO BLOCKED: Acme: 2 open application(s)")


def test_live_stops_at_the_daily_cap_before_tailoring_anything(settings, db):
    for i in range(5):
        applied(db, i, f"Company{i}")
    write_ad(shortlist.approved_root(settings) / "2026-10-27", "a.txt", company="Newco")
    spy = Spy()
    live, lines = live_for(db, settings, spy, require_approval=True)
    assert live.run() == 0
    assert spy.calls == [] and "daily cap reached: 5 of 5" in "\n".join(lines)


# ------------------------------------------------------------------ live: only approved ads
@pytest.fixture
def two_ads(settings):
    jd = (PROFILE_FIX.parent / "tailoring" / "jd_northwind.txt").read_text(encoding="utf-8")
    head = "Company: {c}\nTitle: AI Engineer\nURL: https://example.com/{c}\nLocation: Bristol, UK\n\n"
    inbox, approved = settings.path("jds") / "inbox", shortlist.approved_root(settings) / "2026-10-27"
    inbox.mkdir(parents=True)
    approved.mkdir(parents=True)
    (inbox / "unapproved.txt").write_text(head.format(c="Unapproved Ltd") + jd, encoding="utf-8")
    (approved / "approved.txt").write_text(head.format(c="Approved Ltd") + jd, encoding="utf-8")
    return inbox, approved


def tailored_companies(spy, db):
    return [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"]


def test_an_unapproved_job_is_never_tailored(settings, db, two_ads):
    spy = Spy()
    live, lines = live_for(db, settings, spy, require_approval=True)
    live.run()
    assert tailored_companies(spy, db) == ["Approved Ltd"]
    assert "Unapproved Ltd" not in chr(10).join(lines)
    assert (two_ads[0] / "unapproved.txt").exists()                                       # still in the inbox, untouched


def test_include_unapproved_takes_the_whole_inbox_and_nothing_else(settings, db, two_ads):
    spy = Spy()
    live, _ = live_for(db, settings, spy, require_approval=False)
    live.run()
    assert tailored_companies(spy, db) == ["Unapproved Ltd"]


def test_nothing_approved_says_how_to_approve(settings, db, two_ads):
    for p in two_ads[1].iterdir():
        p.rename(two_ads[0] / ("moved-" + p.name))
    spy = Spy()
    live, lines = live_for(db, settings, spy, require_approval=True)
    live.run()
    assert spy.calls == [] and "careerops shortlist" in "\n".join(lines)


def test_the_inbox_listing_shows_t_with_its_factors(settings, db, tmp_path, monkeypatch):
    fb = load_yaml(PROFILE_FIX / "fact_bank.yaml")
    from careerops.discovery import gates
    gates.load_roles(settings, fb)
    folder = shortlist.approved_root(settings) / "2026-10-27"
    write_ad(folder, "a.txt", company="Alpha")
    write_ad(folder, "b.txt", company="Beta", title="Machine Learning Engineer")
    spy = Spy()
    live, lines = live_for(db, settings, spy, require_approval=True)
    live._by_priority(shortlist.approved_files(settings))
    text = "\n".join(lines)
    assert text.count("| T ") >= 2 and " = A5 E" in text and " = A4 E" in text


# ------------------------------------------------------------------ the command
@pytest.fixture
def cfg(tmp_path, settings):
    import yaml
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return p


def test_include_unapproved_is_not_allowed_in_auto_mode(cfg, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "live", "--auto", "--include-unapproved"])
    assert "--include-unapproved is not allowed with --auto" in capsys.readouterr().err
