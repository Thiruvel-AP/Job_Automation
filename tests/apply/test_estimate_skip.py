"""The estimate stop `live` runs on every approved file before tailoring (apply/estimate_skip.py, v3.5.5), the P x W order of the inbox, and the optional senior
stop (`roles.levels.skip_senior_before_tailoring`, apply/hardstop.py). An ad whose estimate F is under `priority.min_estimate` moves to
jds_done/skipped_estimate/ with its reason and a `skipped_estimate` line in the decision log, before any model call; `unskip --shortlist` brings it back and
it is never stopped again. No network, no model: the estimate is stood in for by company, the way test_live_order.py does."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from careerops.apply import estimate_skip, hardstop
from careerops.discovery import blacklist, decide, gates, shortlist
from triage_fakes import FILLER, NOW, write_ad
from test_live import Fake, archive, session  # noqa: F401  (archive is a fixture: the archive root live checks around every job)

ESTIMATES = {"Lowco": 0.30, "Edgeco": 0.45, "Hico": 0.80, "Thinco": 0.10, "Newco": 0.50, "Oldco": 0.80}
THIN = {"Thinco"}                              # an ad too thin to read: its F is shown, never acted on


@pytest.fixture(autouse=True)
def estimates(monkeypatch, settings, tmp_path):
    settings.raw["priority"]["min_estimate"] = 0.45                  # the fixture keeps the stop off for every other test
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")
    which = lambda text: next(k for k in ESTIMATES if f"Company: {k}\n" in text)         # noqa: E731
    monkeypatch.setattr(gates, "pre_ceiling", lambda text, fb, s: ESTIMATES[which(text)])
    monkeypatch.setattr(gates, "pre_estimate", lambda text, fb, s: SimpleNamespace(reliable=which(text) not in THIN, ceiling=ESTIMATES[which(text)]))


@pytest.fixture
def approved(settings):
    folder = shortlist.approved_root(settings) / "2026-11-05"
    for name, company in (("lowco.txt", "Lowco"), ("edgeco.txt", "Edgeco"), ("hico.txt", "Hico"), ("thinco.txt", "Thinco")):
        write_ad(folder, name, company=company, title="Data Scientist", extra=FILLER * 6)
    return folder


def stopped(settings):
    return sorted(p.name for p in estimate_skip.jd_files(settings))


def go(db, settings, **kw):
    """One live session with fakes that tailor (the job moves on) and skip at review; (the fake, the printed lines)."""
    fake = Fake(db, settings, review="skipped")
    live, lines = session(db, settings, fake, **kw)
    live.run()
    return fake, lines


def tailored(db, fake):
    return sorted(db.job(jid)["company"] for name, jid in fake.calls if name == "tailor")


def test_a_low_estimate_is_filed_away_before_any_model_call(settings, db, archive, approved):
    fake, lines = go(db, settings, require_approval=True)
    assert tailored(db, fake) == ["Edgeco", "Hico", "Thinco"], "\n".join(lines)    # Edgeco is AT the line, Thinco has too few terms to read: both go on
    assert stopped(settings) == ["lowco.txt"] and not (approved / "lowco.txt").exists()
    note = (estimate_skip.folder(settings) / "lowco.reason.txt").read_text(encoding="utf-8")
    assert "estimate F 0.30 is under the 0.45 line" in note and "unskip --shortlist" in note
    entry = next(e for e in decide.read_log(settings) if e["company"] == "Lowco")
    assert entry["decision"] == decide.ESTIMATE_SKIP == "skipped_estimate" and entry["by"] == "agent" and entry["reason"].startswith("estimate stop before tailoring")
    assert any("estimate stop:" in ln and "lowco.txt" in ln and "jds_done/skipped_estimate/" in ln for ln in lines)


def test_the_stop_is_held_so_the_shortlist_keeps_it_out_until_unskip(settings, db, archive, approved):
    go(db, settings, require_approval=True)
    assert decide.ESTIMATE_SKIP in decide.HELD and decide.LABEL[decide.ESTIMATE_SKIP] == "ESTIMATE-SKIP"
    assert any(k.startswith("lowco") for k in decide.held_keys(settings))
    assert any("ESTIMATE-SKIP" in ln and "Lowco" in ln for ln in decide.held_lines(settings))


def test_a_dry_run_names_the_stop_and_moves_nothing(settings, db, archive, approved):
    fake, lines = go(db, settings, require_approval=True, dry_run=True)
    assert stopped(settings) == [] and (approved / "lowco.txt").exists() and decide.read_log(settings) == []
    assert any("would move to" in ln and "lowco.txt" in ln for ln in lines)
    assert "Lowco" not in tailored(db, fake)


def test_only_the_approved_list_is_stopped_your_own_choice_stands(settings, db, archive):
    inbox = settings.path("jds") / "inbox"
    write_ad(inbox, "lowco.txt", company="Lowco", title="Data Scientist", extra=FILLER * 6)
    write_ad(inbox, "hico.txt", company="Hico", title="Data Scientist", extra=FILLER * 6)
    fake, _ = go(db, settings, require_approval=False)                                          # live --include-unapproved
    assert stopped(settings) == [] and tailored(db, fake) == ["Hico", "Lowco"]


def test_zero_switches_the_stop_off(settings, db, archive, approved):
    settings.raw["priority"]["min_estimate"] = 0
    fake, _ = go(db, settings, require_approval=True)
    assert stopped(settings) == [] and "Lowco" in tailored(db, fake)


def test_one_approved_file_is_checked_too(settings, db, archive):
    folder = shortlist.approved_root(settings) / "2026-11-05"
    write_ad(folder, "lowco.txt", company="Lowco", title="Data Scientist", extra=FILLER * 6)
    fake, _ = go(db, settings, require_approval=True)
    assert stopped(settings) == ["lowco.txt"] and tailored(db, fake) == []


def test_the_threshold_comes_from_the_settings(settings):
    assert estimate_skip.threshold(settings) == 0.45
    del settings.raw["priority"]["min_estimate"]
    assert estimate_skip.threshold(settings) == estimate_skip.DEFAULT_MIN == 0.45


def test_a_job_you_promoted_is_never_stopped(settings, approved):
    ranked = [(approved / "lowco.txt", SimpleNamespace(f=0.30, f_known=True))]
    assert [st.path.name for st in estimate_skip.find(ranked, settings)] == ["lowco.txt"]
    decide.log(settings, [{"key": "lowco|data scientist", "company": "Lowco", "title": "Data Scientist", "decision": decide.PROMOTED, "by": "user"}], NOW)
    assert estimate_skip.find(ranked, settings) == []


def test_unskip_brings_the_file_back_and_it_is_not_stopped_a_second_time(settings, db, archive, approved):
    go(db, settings, require_approval=True)
    assert stopped(settings) == ["lowco.txt"]
    back = "\n".join(decide.reset(settings, match="Lowco", now=NOW))
    assert "moved skipped_estimate/lowco.txt -> inbox/lowco.txt" in back and stopped(settings) == []
    inbox_file = settings.path("jds") / "inbox" / "lowco.txt"
    assert inbox_file.exists() and (estimate_skip.folder(settings) / "lowco.reason.txt").exists()          # the note stays as the record
    ranked = [(inbox_file, SimpleNamespace(f=0.30, f_known=True))]
    assert estimate_skip.find(ranked, settings) == []                                                       # your override: never again
    assert decide.held_keys(settings) == set()


def test_a_stopped_file_is_never_overwritten(settings, approved):
    estimate_skip.folder(settings).mkdir(parents=True)
    (estimate_skip.folder(settings) / "lowco.txt").write_text("an earlier file", encoding="utf-8")
    stops = estimate_skip.find([(approved / "lowco.txt", SimpleNamespace(f=0.30, f_known=True))], settings)
    estimate_skip.file_away(settings, stops, now=NOW)
    assert stopped(settings) == ["lowco-2.txt", "lowco.txt"]
    assert json.loads(decide.log_path(settings).read_text(encoding="utf-8").splitlines()[-1])["file"].endswith("lowco-2.txt")


def test_an_ad_with_an_unknown_estimate_is_not_stopped(settings, approved):
    ranked = [(approved / "thinco.txt", SimpleNamespace(f=0.0, f_known=False)), (approved / "hico.txt", None)]
    assert estimate_skip.find(ranked, settings) == []


# ------------------------------------------------------------------ the order is P x W: a better fit one week old goes before a poorer fit from today
def test_live_tailors_a_much_better_older_ad_before_a_poorer_fresh_one(settings, db, archive):
    inbox = settings.path("jds") / "inbox"
    now = datetime.now(timezone.utc)
    write_ad(inbox, "a_new.txt", company="Newco", title="Data Scientist", age_days=0.2, base=now, extra=FILLER * 6)           # tier 0, W 1.00: 0.50 -> 0.50
    write_ad(inbox, "b_old.txt", company="Oldco", title="Data Scientist", age_days=12, base=now, extra=FILLER * 6)           # tier 3, W 0.85: 0.80 -> 0.68
    fake, lines = go(db, settings, require_approval=False)
    assert [db.job(jid)["company"] for name, jid in fake.calls if name == "tailor"] == ["Oldco", "Newco"]        # before v3.5.5 the tier came first: Newco, Oldco
    listing = [ln for ln in lines if "estimate P" in ln]
    assert "x W 0.85 (T3) = order" in listing[0] and "x W 1.00 (T0) = order" in listing[1] and any(ln.startswith("Inbox order by the ESTIMATE of P x W") for ln in lines)


# ------------------------------------------------------------------ roles.levels.skip_senior_before_tailoring
@pytest.fixture
def no_senior_rule(settings):
    """The fixture's `exclude` already lists senior (as the old shipped file did); the real one does not, which is the case the flag is for."""
    levels = settings.raw["roles"]["levels"]
    levels["exclude"] = [p for p in levels["exclude"] if "senior" not in p]


def test_a_senior_title_is_stopped_only_when_the_flag_is_on(settings, tmp_path, no_senior_rule):
    ads = tmp_path / "ads"
    senior = write_ad(ads, "senior.txt", company="Seniorco", title="Senior Data Scientist", extra=FILLER * 6)
    sr = write_ad(ads, "sr.txt", company="Srco", title="Sr. Machine Learning Engineer", extra=FILLER * 6)
    plain = write_ad(ads, "plain.txt", company="Plainco", title="Data Scientist", extra=FILLER * 6)
    assert settings.raw["roles"]["levels"]["skip_senior_before_tailoring"] is False and hardstop.skip_senior(settings) is False
    assert hardstop.reason_of(senior, settings) is None and hardstop.reason_of(sr, settings) is None             # off by default: a senior role stays yours to judge
    settings.raw["roles"]["levels"]["skip_senior_before_tailoring"] = True
    assert "senior level" in hardstop.reason_of(senior, settings).reason and "senior level" in hardstop.reason_of(sr, settings).reason
    assert hardstop.reason_of(plain, settings) is None
    del settings.raw["roles"]["levels"]["skip_senior_before_tailoring"]                                            # a missing key counts as off
    assert hardstop.reason_of(senior, settings) is None


def test_live_files_a_senior_ad_away_before_tailoring_when_the_flag_is_on(settings, db, archive, no_senior_rule):
    settings.raw["roles"]["levels"]["skip_senior_before_tailoring"] = True
    folder = shortlist.approved_root(settings) / "2026-11-05"
    write_ad(folder, "senior.txt", company="Hico", title="Senior Data Scientist", extra=FILLER * 6)
    write_ad(folder, "plain.txt", company="Edgeco", title="Data Scientist", extra=FILLER * 6)
    fake, lines = go(db, settings, require_approval=True)
    assert tailored(db, fake) == ["Edgeco"] and [p.name for p in hardstop.jd_files(settings)] == ["senior.txt"]
    log = next(e for e in decide.read_log(settings) if e["title"] == "Senior Data Scientist")
    assert log["decision"] == decide.SKIP and "senior level" in log["reason"]
    assert "unskip --shortlist" in (hardstop.folder(settings) / "senior.reason.txt").read_text(encoding="utf-8")
