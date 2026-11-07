"""v3.2.1 follow-ups to the agent's decisions: the security-clearance / UK-residence hard disqualifier, the effective levels.exclude in the header,
"no salary given" is no longer a soft flag, a vetoed slot goes to the best WAITING job at the next run, the apply route (and the Workday hand-off) on approved
lines, and `careerops recheck`. Fixtures and a fake LLM only."""
from datetime import timedelta

import pytest
import yaml

from careerops import cli
from careerops.discovery import agent_triage as qt
from careerops.discovery import decide, gates, triage
from test_agent_decide_v32 import LONG, FakeLLM, ad, answer, approved_files, by, fb, inbox, roles, run   # noqa: F401 - shared fixtures
from triage_fakes import NOW, write_ad


def _q(settings, fb, inbox, **kw):
    p = write_ad(inbox, kw.pop("name", "x.txt"), company=kw.pop("company", "Acme"), extra=kw.pop("extra", LONG), age_days=kw.pop("age_days", 1), **kw)
    job = triage.inbox_job(p, settings, fb, None)
    return qt.quick(job, triage.score(job, settings, fb, floor=None, now=NOW), settings, now=NOW)


# ------------------------------------------------------------------ 1. security clearance / UK residence
@pytest.mark.parametrize("sentence", [
    "You will need SC clearance for this role.",
    "DV clearance is required.",
    "This role requires you to obtain a Security Check (SC) level security clearance.",
    "Candidates must be eligible for security clearance.",
    "You must have resided in the UK for the last 5 years.",
    "applicants will typically need to have been continuously resident in the UK for a minimum of 5 years immediately prior to applying.",
    "BPSS + 5 years of UK residency is needed.",
])
def test_a_stated_clearance_or_residence_period_caps_the_quick_score(settings, fb, inbox, sentence):
    q = _q(settings, fb, inbox, extra=LONG + " " + sentence)
    hit = [h for h in q.hard if h.startswith(qt.CLEARANCE_LABEL)]
    assert hit and sentence.split()[0] in hit[0] and q.score <= 2.5 and q.verdict == "FAIL"


@pytest.mark.parametrize("sentence", ["SC clearance desirable.", "No security clearance is required.", "We work with the Security Council of a charity.",
                                      "Our team has lived in the UK and abroad."])
def test_a_denied_optional_or_unrelated_mention_is_no_hit(sentence):
    assert qt.clearance_sentence(LONG + " " + sentence) is None


def test_clearance_reaches_the_evaluation_cap_too(settings, db, fb, inbox):
    ad(inbox, "c.txt", "Defenceco", extra=LONG + " This role requires you to obtain a Security Check (SC) level security clearance.")
    llm = FakeLLM(lambda ctx: answer(4.8))
    d = run(settings, db, fb, llm)
    assert [i.decision for i in d.items] == [decide.SKIP] and not llm.calls          # stopped by the quick score: no tokens spent
    assert "security clearance / residency" in d.items[0].reason


# ------------------------------------------------------------------ 2. levels.exclude in the header
def test_the_header_shows_the_effective_levels_exclude(settings):
    got = decide.level_lines(settings)
    assert got[0].startswith("levels.exclude (effective): senior, sr., staff") and "+ always: chief, head of, vp" in got[0]
    assert got[1] == "  senior: excluded (Senior, Sr. and Snr titles alike)"
    settings.raw["roles"]["levels"]["exclude"] = [r"\bsr\.?\b", r"\blead\b"]                 # the shape of your settings since 3 Nov
    got = decide.level_lines(settings)
    assert got[0].startswith("levels.exclude (effective): sr., lead + always") and "senior: NOT excluded" in got[1] and r"'\bsenior\b'" in got[1]


@pytest.mark.parametrize("title", ["Senior Data Scientist", "Sr. Data Scientist", "Snr Data Scientist", "Lead Data Scientist"])
def test_senior_and_sr_follow_levels_exclude_like_lead(settings, fb, inbox, title):
    q = _q(settings, fb, inbox, title=title)
    assert any(h.startswith("level excluded") for h in q.hard) and q.score <= 2.5
    settings.raw["roles"]["levels"]["exclude"] = [r"\blead\b"]
    q2 = _q(settings, fb, inbox, title=title, name="y.txt")
    assert any(h.startswith("level excluded") for h in q2.hard) == title.startswith("Lead")


def test_the_digest_prints_the_levels_header(settings, db, fb, inbox):
    ad(inbox, "a.txt", "Alpha")
    out = decide.lines(run(settings, db, fb, FakeLLM()))
    assert out[2].startswith("levels.exclude (effective):") and out[3].startswith("  senior:")


# ------------------------------------------------------------------ 3. no "no salary given" flag
def test_no_salary_is_not_a_soft_flag_any_more(settings, fb, inbox):
    q = _q(settings, fb, inbox)                                       # no Salary header, no £ figure
    assert q.comp == 3 and not any("salary" in f for f in q.flags)
    assert q.score == qt.formula(q.archetype, q.comp, q.location, q.cv_match, len(q.flags))


# ------------------------------------------------------------------ 4. a veto frees a slot for the best WAITING job, at the next run
def test_a_vetoed_slot_goes_to_the_best_waiting_job_at_the_next_run(settings, db, fb, inbox):
    for i in range(6):
        ad(inbox, f"old{i}.txt", f"Old{i}", age_days=1 + i)
    score = lambda ctx: answer(4.8 if ctx["company"].startswith("New") else 4.1)          # noqa: E731
    d1 = run(settings, db, fb, FakeLLM(score))
    assert len(by(d1, decide.AUTO)) == 5 and [i.company for i in by(d1, decide.WAITING)] == ["Old5"]
    out = decide.veto(settings, db, "1", now=NOW)
    assert "goes to the best WAITING job at the next run from 2026-10-28 (not today" in out[1]
    same_day = run(settings, db, fb, FakeLLM(score), now=NOW + timedelta(hours=1))         # not today: today's 5 are counted
    assert not [i for i in same_day.items if i.decision in decide.APPROVING]
    assert any("freed by your veto today" in l and "next run, not today" in l for l in decide.lines(same_day))
    for i in range(5):                                                                      # tomorrow: five better new jobs
        ad(inbox, f"new{i}.txt", f"New{i}")
    d2 = run(settings, db, fb, FakeLLM(score), now=NOW + timedelta(days=1))
    approved = [i for i in d2.items if i.decision in decide.APPROVING]
    assert len(approved) == 5                                                               # the cap is never raised
    old5 = next(i for i in approved if i.company == "Old5")
    assert old5.freed_by and "the slot your veto of" in old5.reason
    assert any(l.startswith("slot freed by your veto of") and "-> approved Old5 | Data Scientist" in l for l in decide.lines(d2))
    assert len([i for i in d2.items if i.company.startswith("New") and i.decision == decide.WAITING]) == 1
    d3 = run(settings, db, fb, FakeLLM(score), now=NOW + timedelta(days=2))                # the slot was used once
    assert not decide.freed_slots(settings, (NOW + timedelta(days=2)).date()) and not any(i.freed_by for i in d3.items)


def test_without_a_veto_the_waiting_job_has_no_priority(settings, db, fb, inbox):
    for i in range(6):
        ad(inbox, f"old{i}.txt", f"Old{i}", age_days=1 + i)
    score = lambda ctx: answer(4.8 if ctx["company"].startswith("New") else 4.1)          # noqa: E731
    run(settings, db, fb, FakeLLM(score))
    for i in range(5):
        ad(inbox, f"new{i}.txt", f"New{i}")
    d2 = run(settings, db, fb, FakeLLM(score), now=NOW + timedelta(days=1))
    assert [i.company for i in d2.items if i.decision in decide.APPROVING and i.company.startswith("Old")] == []


# ------------------------------------------------------------------ 5. the apply route on approved lines
def test_an_approved_line_shows_its_apply_route(settings, db, fb, inbox):
    ad(inbox, "g.txt", "Greenco")
    ad(inbox, "w.txt", "Workco", url="https://workco.wd3.myworkdayjobs.com/en-US/careers/job/London/Data-Scientist_R123")
    lines = decide.lines(run(settings, db, fb, FakeLLM(lambda ctx: answer(4.3))))
    g = next(l for l in lines if "| Greenco |" in l)
    w = next(l for l in lines if "| Workco |" in l)
    assert "| Greenco | Data Scientist | apply: employer form (greenhouse) | why:" in g and "hand-off" not in g
    assert "| apply: employer form (workday) | hand-off: Workday needs a candidate account (never signed in by code) | why:" in w


def test_a_line_that_is_not_approved_shows_no_route(settings, db, fb, inbox):
    ad(inbox, "a.txt", "Alpha")
    lines = decide.lines(run(settings, db, fb, FakeLLM(lambda ctx: answer(3.7))))
    assert not any("apply:" in l for l in lines if "| Alpha |" in l)


# ------------------------------------------------------------------ recheck
def test_recheck_applies_a_new_hard_stop_to_waiting_and_marginal_lines(settings, db, fb, inbox):
    ad(inbox, "m.txt", "Margco")
    ad(inbox, "k.txt", "Keepco")
    run(settings, db, fb, FakeLLM(lambda ctx: answer(3.7 if ctx["company"] == "Margco" else 3.8)))      # both MARGINAL, held
    p = inbox / "m.txt"
    p.write_text(p.read_text(encoding="utf-8") + "\nYou must have resided in the UK for the last 5 years.\n", encoding="utf-8")   # the ad as the new rule reads it
    dry = decide.recheck(settings, db, fb, now=NOW)
    assert any("Margco | Data Scientist (MARGINAL): security clearance / residency" in l and "dry run" in l for l in dry)
    assert decide.latest(settings)[decide.last_digest(settings)[0]["key"]]["decision"] == decide.MARGINAL      # a dry run writes nothing
    out = decide.recheck(settings, db, fb, now=NOW, apply=True)
    assert not any("Keepco" in l for l in out if "->" in l)
    row = next(r for r in decide.last_digest(settings) if r["company"] == "Margco")
    assert row["decision"] == decide.SKIP and "recheck" in row["reason"]
    assert decide.latest(settings)[row["key"]]["decision"] == decide.SKIP                                      # held: `unskip --shortlist` reverses it


def test_recheck_counts_quick_score_moves_without_an_evaluation(settings, db, fb, inbox):
    ad(inbox, "p.txt", "Pipeco", title="Plumber", age_days=70)
    llm = FakeLLM()
    d = run(settings, db, fb, llm)
    assert [i.decision for i in d.items] == [decide.SKIP] and not llm.calls
    settings.raw["targeting"]["priority_companies"] = ["Pipeco"]
    out = decide.recheck(settings, db, fb, now=NOW)
    assert "  quick-score FAIL lines (1): -> PASS 1, -> MARGINAL 0, still FAIL 0" in out and not llm.calls


def test_recheck_only_names_an_approved_line(settings, db, fb, inbox):
    ad(inbox, "a.txt", "Alpha")
    run(settings, db, fb, FakeLLM(lambda ctx: answer(4.3)))
    p = next(settings.path("jds").glob("approved/*/a.txt"))
    p.write_text(p.read_text(encoding="utf-8") + "\nDV clearance is required.\n", encoding="utf-8")
    out = decide.recheck(settings, db, fb, now=NOW, apply=True)
    assert any("Alpha | Data Scientist (AUTO-APPROVED)" in l and "careerops veto 1" in l for l in out)
    assert approved_files(settings) == ["a.txt"] and decide.last_digest(settings)[0]["decision"] == decide.AUTO


def test_recheck_cli(settings, inbox, tmp_path, capsys, monkeypatch):
    ad(inbox, "a.txt", "Alpha")
    settings.raw["agent"] = {"decide": "veto"}
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: FakeLLM(lambda ctx: answer(3.7)))
    cli.main(["--config", str(cfg), "shortlist", "--no-liveness"])
    capsys.readouterr()
    monkeypatch.setattr(cli, "_llm", lambda *a: pytest.fail("recheck calls no model"))
    cli.main(["--config", str(cfg), "recheck"])
    out = capsys.readouterr().out
    assert out.startswith("Recheck of the agent digest of") and "zero tokens" in out
