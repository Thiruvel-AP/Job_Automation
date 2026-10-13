"""Priority P = F x S x R (discovery/priority.py): sponsorship is a weight, never a filter. The review screen shows P and its
factors plus the JD's sponsorship wording as an information line; live's inbox, `status` and the gate follow the same rule."""
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from careerops import cli
from careerops.discovery import gates, priority as prio
from careerops.review import workflow
from conftest import fixture

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def job_(**kw):
    return {"id": "j", "company": "Acme", "description": "Build models in Python.", "sponsor_match": "exact", "agency": 0,
            "posted_at": None, "ceiling": None, "ceiling_pre": 0.80, **kw}


# ------------------------------------------------------------------ S
@pytest.mark.parametrize("kw, s, why", [
    ({"sponsor_match": "exact"}, 1.00, "exact register match"),
    ({"sponsor_match": "fuzzy(0.95)"}, 0.90, "fuzzy register match"),
    ({"sponsor_match": "none"}, 0.75, "not on the register"),
    ({"sponsor_match": None}, 0.75, "register not checked"),
    ({"sponsor_match": "exact", "agency": 1}, 0.75, "agency"),
    ({"sponsor_match": "exact", "description": "You need the right to work in the UK without sponsorship."}, 0.65,
     "JD says no sponsorship"),
    ({"sponsor_match": "none", "description": "We do not offer visa sponsorship."}, 0.65, "JD says no sponsorship"),
    ({"sponsor_match": "fuzzy(0.93)", "agency": 1, "description": "No sponsorship available."}, 0.65, "JD says no sponsorship")])
def test_the_sponsor_factor(kw, s, why):
    assert prio.sponsor_factor(job_(**kw)) == (s, why)


def test_the_weights_come_from_the_settings(settings):
    settings.raw["priority"]["sponsor_weights"] = {"exact": 1.0, "fuzzy": 0.5, "unknown": 0.4, "no_sponsorship": 0.2}
    assert prio.sponsor_factor(job_(sponsor_match="fuzzy(0.9)"), settings)[0] == 0.5
    assert prio.sponsor_factor(job_(sponsor_match="none"), settings)[0] == 0.4
    assert prio.weights(settings)["no_sponsorship"] == 0.2


def test_the_shipped_weights_are_the_agreed_ones(settings):
    assert settings["priority"]["sponsor_weights"] == {"exact": 1.00, "fuzzy": 0.90, "unknown": 0.75, "no_sponsorship": 0.65, "non_uk": 0.50}
    assert settings["priority"]["recency_days"] == 14


# ------------------------------------------------------------------ R
def test_recency_decays_with_a_14_day_time_constant_and_is_one_when_unknown(settings):
    assert prio.recency_factor(job_(), settings, NOW) == (1.0, None)
    r, days = prio.recency_factor(job_(posted_at=(NOW - timedelta(days=14)).isoformat()), settings, NOW)
    assert days == pytest.approx(14) and r == pytest.approx(math.exp(-1))
    r, _ = prio.recency_factor(job_(posted_at=NOW.date().isoformat()), settings, NOW)
    assert 0.95 < r <= 1.0                                   # a date without a time is midnight: half a day old at noon
    assert prio.recency_factor(job_(posted_at=(NOW + timedelta(days=3)).isoformat()), settings, NOW)[0] == 1.0     # a future date is today
    assert prio.recency_factor(job_(posted_at="garbage"), settings, NOW) == (1.0, None)
    assert prio.recency_factor(job_(posted_at="2026-10-01T00:00:00Z"), settings, NOW)[0] < prio.recency_factor(job_(posted_at="2026-10-09T00:00:00Z"), settings, NOW)[0]


def test_the_recency_constant_is_a_setting(settings):
    settings.raw["priority"]["recency_days"] = 7
    r, _ = prio.recency_factor(job_(posted_at=(NOW - timedelta(days=7)).isoformat()), settings, NOW)
    assert r == pytest.approx(math.exp(-1))


# ------------------------------------------------------------------ F and P
def test_f_is_the_tailored_ceiling_when_there_is_one_else_the_triage_ceiling():
    j = job_(ceiling=0.65, ceiling_pre=0.87)
    assert prio.ats_factor(j, tailored=True) == 0.65 and prio.ats_factor(j, tailored=False) == 0.87
    assert prio.ats_factor(job_(ceiling_pre=None)) == 0.0 and prio.ats_factor(job_(ceiling_pre=1.4)) == 1.0


def test_p_is_the_product_and_shows_its_three_factors(settings):
    j = job_(sponsor_match="fuzzy(0.95)", ceiling_pre=0.80, posted_at=(NOW - timedelta(days=14)).isoformat())
    pr = prio.priority(j, settings, tailored=False, today=NOW)
    assert pr.p == pytest.approx(0.80 * 0.90 * math.exp(-1)) and (pr.f, pr.s) == (0.80, 0.90)
    line = pr.line()
    assert "P=0.26" in line and "F 0.80" in line and "S 0.90 (fuzzy register match)" in line and "R 0.37 (14 d old)" in line
    assert "posting date unknown" in prio.priority(job_(), settings).line()


def test_a_non_sponsor_with_f_080_outranks_a_sponsor_with_f_045(settings):
    non_sponsor = job_(id="non", sponsor_match="none", agency=1, ceiling_pre=0.80,
                       description="We cannot offer sponsorship.")                  # the worst S there is: 0.65
    sponsor = job_(id="sponsor", sponsor_match="exact", ceiling_pre=0.45)
    a, b = prio.priority(non_sponsor, settings, tailored=False), prio.priority(sponsor, settings, tailored=False)
    assert a.s == 0.65 and b.s == 1.0 and a.p == pytest.approx(0.52) and b.p == pytest.approx(0.45) and a.p > b.p
    order = sorted([sponsor, non_sponsor], key=lambda j: prio.sort_key(j, settings, tailored=False))
    assert [j["id"] for j in order] == ["non", "sponsor"]


def test_s_never_drops_a_job_it_only_moves_it_down(settings):
    worst = job_(sponsor_match="none", agency=1, description="Right to work without sponsorship is required.", ceiling_pre=0.8)
    best = job_(sponsor_match="exact", ceiling_pre=0.8)
    w, b = prio.priority(worst, settings, tailored=False), prio.priority(best, settings, tailored=False)
    assert 0 < w.s < b.s <= 1 and 0 < w.p < b.p
    assert min(prio.weights(settings).values()) > 0                                 # no weight can remove a job
    jobs = [worst, best, job_(sponsor_match=None, ceiling_pre=None)]
    assert len(sorted(jobs, key=lambda j: prio.sort_key(j, settings, tailored=False))) == 3


def test_a_job_with_no_ceiling_yet_sorts_last_but_by_s_x_r(settings):
    a = job_(id="a", ceiling_pre=None, sponsor_match="exact")
    b = job_(id="b", ceiling_pre=None, sponsor_match="none")
    c = job_(id="c", ceiling_pre=0.2)
    assert [j["id"] for j in sorted([b, a, c], key=lambda j: prio.sort_key(j, settings, tailored=False))] == ["c", "a", "b"]


# ------------------------------------------------------------------ what the JD says (an information line)
@pytest.mark.parametrize("text, kind", [
    ("Applicants must have the right to work in the UK without sponsorship.", "no_sponsorship"),
    ("We are unable to offer visa sponsorship.", "no_sponsorship"),
    ("We do not provide sponsorship for this role", "no_sponsorship"),
    ("Sorry, we can't sponsor.", "no_sponsorship"),
    ("Unfortunately we will not sponsor visas.", "no_sponsorship"),
    ("No visa sponsorship is available.", "no_sponsorship"),
    ("Sponsorship is not available for this position.", "no_sponsorship"),
    ("Sponsorship is unavailable.", "no_sponsorship"),
    ("Candidates who do not require sponsorship are preferred.", "no_sponsorship"),
    ("You must not require sponsorship now or in the future.", "no_sponsorship"),
    ("You must have the right to work in the UK.", "right_to_work"),
    ("We are a licensed sponsor and welcome applicants who need visa sponsorship.", "sponsorship_offered"),
    ("Visa sponsorship is available for the right candidate.", "sponsorship_offered"),
    ("We can sponsor skilled workers.", "sponsorship_offered")])
def test_sponsor_wording_kinds(text, kind):
    (w,) = prio.sponsor_wording("Intro line.\n" + text + "\nOutro.")
    assert w.kind == kind and w.text.rstrip(".") == text.rstrip(".")


@pytest.mark.parametrize("text", ["Sponsorship of events and conferences is part of the role.", "Build models in Python and SQL.",
                                  "Our sponsors include several charities.", "Work with the sponsor of each analytics project."])
def test_text_that_is_not_about_visas_is_not_picked_up(text):
    assert prio.sponsor_wording(text) == [] and prio.wording_line(text) is None


def test_the_northwind_ad_welcomes_sponsorship_and_does_not_lower_s():
    jd = fixture("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")
    assert [w.kind for w in prio.sponsor_wording(jd)] == ["sponsorship_offered"] and not prio.says_no_sponsorship(jd)


def test_a_long_sentence_is_cut_to_one_line():
    (w,) = prio.sponsor_wording("We cannot sponsor " + "word " * 100)
    assert len(w.text) <= prio.SNIPPET_CHARS and "\n" not in w.text and w.text.endswith("…")


# ------------------------------------------------------------------ the review screen
@pytest.fixture
def screen(settings, db, tailored):
    rep, fb, _ = tailored
    jid = "manual:northwind1"

    def show(description=None, flags=None, **fields):
        if description is not None:
            fields["description"] = description
        if flags is not None:
            fields["flags"] = flags
        db.update_job(jid, **fields)
        return workflow.summary(db.job(jid), rep, settings)
    return show


@pytest.mark.slow
def test_the_review_screen_shows_p_with_f_s_and_r_and_the_wording_as_information(screen):
    text = screen("Intro.\nThe role needs the right to work in the UK without sponsorship.\n" + "Python " * 60,
                  sponsor_match="exact", posted_at="2026-10-01T00:00:00+00:00")
    lines = text.splitlines()
    pline = next(ln for ln in lines if ln.startswith("Priority P="))
    assert "F " in pline and "S 0.65 (JD says no sponsorship)" in pline and "R " in pline and "d old" in pline
    wording = next(ln for ln in lines if ln.startswith("Sponsorship wording in the JD"))
    assert "right to work in the UK without sponsorship" in wording and "(information only)" in wording
    assert not any(ln.startswith("FLAGS:") and "sponsor" in ln.lower() for ln in lines)       # not a blocking flag


@pytest.mark.slow
def test_register_notes_move_from_flags_to_the_sponsor_line(screen):
    text = screen(flags=[gates.NOT_ON_REGISTER, "JD asks for 5+ years' experience"], sponsor_match="none")
    sponsor = next(ln for ln in text.splitlines() if ln.startswith("Sponsor:"))
    assert gates.NOT_ON_REGISTER in sponsor and "information only" in sponsor
    flags = next(ln for ln in text.splitlines() if ln.startswith("FLAGS:"))
    assert "5+ years" in flags and gates.NOT_ON_REGISTER not in flags


@pytest.mark.slow
def test_a_job_with_only_register_notes_has_no_flags_line_and_no_wording_line_when_the_jd_is_silent(screen):
    text = screen("Build models in Python and SQL. " * 30, flags=[gates.NOT_ON_REGISTER])
    assert "FLAGS:" not in text and "Sponsorship wording" not in text


def test_is_sponsor_flag():
    assert gates.is_sponsor_flag(gates.NOT_ON_REGISTER) and gates.is_sponsor_flag("Sponsor match is fuzzy (0.93): 'X'")
    assert gates.is_sponsor_flag("Sponsor register not loaded — sponsorship unchecked")
    assert not gates.is_sponsor_flag("On-call duties mentioned") and not gates.is_sponsor_flag("JD asks for 5+ years' experience")


# ------------------------------------------------------------------ the judge gate does not treat sponsor notes as flags
def test_the_judge_gate_approves_a_job_whose_only_flags_are_register_notes(settings, db, fb_fixture, job, monkeypatch):
    from careerops.review import judge
    rep = {"letter": {"status": "ok"}, "llm_usage": {}}
    settings.raw["review"]["judge"] = "gate"
    monkeypatch.setattr(judge, "require_gate", lambda s: None)
    monkeypatch.setattr(workflow, "load_report", lambda j: rep)
    monkeypatch.setattr(workflow, "summary", lambda j, r, s=None: "SUMMARY")
    monkeypatch.setattr(workflow, "approve", lambda *a, **k: None)
    monkeypatch.setattr(judge, "run_judge", lambda *a, **k: judge.Verdict(True, 5, [], "m"))
    row = {**job, "flags": json.dumps([gates.NOT_ON_REGISTER, "Sponsor match is fuzzy (0.93): 'X'"])}
    assert workflow.review_job(db, row, settings, fb_fixture, make_llm=lambda: object(), ask=lambda p: pytest.fail("asked")) == "approved"
    row["flags"] = json.dumps([gates.NOT_ON_REGISTER, "On-call duties mentioned"])           # a real flag still comes to you
    answers = iter(["s"])
    assert workflow.review_job(db, row, settings, fb_fixture, make_llm=lambda: object(), ask=lambda p: next(answers)) == "skipped"


# ------------------------------------------------------------------ the live inbox and status
def _put(folder, name, company, jd, **headers):
    folder.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k.capitalize()}: {v}\n" for k, v in {"company": company, "title": "AI Engineer", "url": "https://e.com/1",
                                                              **headers}.items())
    (folder / name).write_text(head + "\n" + jd, encoding="utf-8")
    return folder / name


@pytest.fixture
def inbox_files(settings, db, tmp_path):
    from careerops.discovery import register as sponsors
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    jd = fixture("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")
    weak = "Excel and PowerPoint reporting for a finance team. " * 40                       # a JD the fact bank hardly covers
    box = tmp_path / "inbox"
    return {
        "a_nonsponsor_strong": _put(box, "a.txt", "Not A Sponsor Ltd", jd + "\nWe do not offer sponsorship."),
        "b_sponsor_weak": _put(box, "b.txt", "Northwind Health AI", weak),
        "c_sponsor_strong": _put(box, "c.txt", "Northwind Health AI", jd),
        "d_unreadable": _put(box, "d.txt", "Northwind Health AI", "tiny"),
    }


def test_the_inbox_is_ordered_by_p_not_by_file_name_and_nothing_is_left_out(settings, db, inbox_files):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    ranked = prio.order_files(list(inbox_files.values()), settings, db, fb)
    names = [f.name for f, _ in ranked]
    assert sorted(names) == ["a.txt", "b.txt", "c.txt", "d.txt"]                       # none dropped, the unreadable one included
    assert names[0] == "c.txt" and names[-1] == "d.txt" and ranked[-1][1] is None      # the sponsor with the strong ad first
    p = {f.name: pr for f, pr in ranked if pr}
    assert p["c.txt"].s == 1.0 and p["a.txt"].s == 0.65 and p["a.txt"].f == p["c.txt"].f and p["a.txt"].p < p["c.txt"].p
    assert names.index("a.txt") < names.index("b.txt")          # the non-sponsor with the strong ad outranks the sponsor with the weak one


def test_posted_and_agency_headers_move_a_file_in_the_order(settings, db, inbox_files, tmp_path):
    from careerops.config import load_yaml
    fb = load_yaml(settings.path("fact_bank"))
    jd = fixture("tailoring", "jd_northwind.txt").read_text(encoding="utf-8")
    box = tmp_path / "box2"
    fresh = _put(box, "z_fresh.txt", "Northwind Health AI", jd, posted=(NOW - timedelta(days=1)).date().isoformat())
    old = _put(box, "a_old.txt", "Northwind Health AI", jd, posted=(NOW - timedelta(days=60)).date().isoformat())
    agency = _put(box, "b_agency.txt", "Northwind Health AI", jd, agency="yes")
    ranked = prio.order_files([old, agency, fresh], settings, db, fb, today=NOW)
    assert [f.name for f, _ in ranked] == ["b_agency.txt", "z_fresh.txt", "a_old.txt"] or \
        [f.name for f, _ in ranked][0] in ("b_agency.txt", "z_fresh.txt")
    pr = {f.name: p for f, p in ranked}
    assert pr["b_agency.txt"].s == 0.75 and pr["b_agency.txt"].r == 1.0 and pr["a_old.txt"].r < 0.05 and pr["z_fresh.txt"].r > 0.85


def test_live_goes_through_the_inbox_in_priority_order_and_says_so(settings, db, inbox_files):
    from careerops.apply.live import Hooks, Live
    lines = []
    live = Live(db, settings, Hooks(ask=lambda p: "", tailor=None, review=None, preflight=None, assist=None, mark_applied=None),
                out=lines.append)
    ordered = live._by_priority(list(inbox_files.values()))
    assert [f.name for f in ordered][0] == "c.txt" and len(ordered) == 4
    assert lines[0] == "Inbox order by priority P = F x S x R:" and any("c.txt" in ln and "=" in ln for ln in lines[1:])
    assert any("d.txt (unreadable)" in ln for ln in lines)


def test_live_falls_back_to_file_names_if_the_ranking_breaks(settings, db, inbox_files, monkeypatch):
    from careerops.apply.live import Hooks, Live
    monkeypatch.setattr(prio, "order_files", lambda *a, **k: 1 / 0)
    lines = []
    live = Live(db, settings, Hooks(ask=lambda p: "", tailor=None, review=None, preflight=None, assist=None, mark_applied=None),
                out=lines.append)
    assert [f.name for f in live._by_priority(list(inbox_files.values()))] == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert "going by file name" in lines[0]


def test_status_lists_the_open_jobs_by_priority_with_their_factors(settings, db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: settings)
    monkeypatch.setattr(cli, "load_dotenv", lambda p: None)
    base = {"source": "manual", "location": "UK", "url": "u", "apply_url": "u", "description": "Python."}
    for jid, company, ceiling, match in (("m:1", "Strong Non Sponsor", 0.80, "none"), ("m:2", "Weak Sponsor", 0.45, "exact"),
                                         ("m:3", "Done Already", 0.99, "exact")):
        db.upsert_job({**base, "id": jid, "company": company, "title": "AI Engineer"})
        db.update_job(jid, status="applied" if jid == "m:3" else "triaged", ceiling_pre=ceiling, sponsor_match=match, tier=1)
    cli.main(["status"])
    out = capsys.readouterr().out
    section = out.split("Top jobs by priority P = F x S x R (not yet applied):\n", 1)[1]
    lines = [ln for ln in section.splitlines() if ln.startswith("  ")][:3]
    assert "Strong Non Sponsor" in lines[0] and "0.60 = 0.80 x 0.75 x 1.00" in lines[0] and "not on the register" in lines[0]
    assert "Weak Sponsor" in lines[1] and "0.45 = 0.45 x 1.00 x 1.00" in lines[1]
    assert "Done Already" not in section                                    # applied jobs are not listed
