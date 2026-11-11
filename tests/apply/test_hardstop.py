"""The hard stops `live` runs again on every approved file before tailoring (apply/hardstop.py, v3.5.4): a fee-charging training scheme, an excluded level
(chief / head / vp / director / principal / staff and roles.levels.exclude) and the blacklist move the file to jds_done/skipped_agent/ with its reason and
an AGENT-SKIP line in the decision log, before any model call. `recheck --apply` does the same for the files already approved; `unskip --shortlist` brings
them back. No network, no model."""
import json

import pytest

from careerops.apply import hardstop
from careerops.apply.live import Live
from careerops.discovery import blacklist, decide, shortlist
from triage_fakes import FILLER, NOW, ad_text, write_ad
from test_volume import Spy

ITOL_EXTRA = "Please note this is a training course and fees apply. Job guaranteed or your money back."


@pytest.fixture(autouse=True)
def own_blacklist(monkeypatch, tmp_path):
    monkeypatch.setattr(blacklist, "path_for", lambda s: tmp_path / "blacklist.yaml")


@pytest.fixture
def approved(settings):
    folder = shortlist.approved_root(settings) / "2026-11-05"
    write_ad(folder, "itol.txt", company="ITOL Recruit", title="Trainee AI Engineer", extra=ITOL_EXTRA)
    write_ad(folder, "chambers.txt", company="Chambers and Partners", title="Chief AI Engineer")
    write_ad(folder, "good.txt", company="Goodco", title="Data Scientist", extra=FILLER * 6)
    return folder


def stopped(settings):
    return sorted(p.name for p in hardstop.jd_files(settings))


def test_the_training_scheme_and_the_chief_title_are_stopped_and_a_good_ad_is_not(settings, approved):
    got = {st.path.name: st.reason for st in hardstop.find(sorted(approved.iterdir()), settings)}
    assert set(got) == {"itol.txt", "chambers.txt"}
    assert got["itol.txt"].startswith("training scheme (fees)") and "chief" in got["chambers.txt"]


def test_the_blacklist_stops_an_ad(settings, tmp_path):
    (tmp_path / "blacklist.yaml").write_text("blacklist:\n  - {name: Badco, scope: company, since: 2026-01-01, reason: ghosted}\n", encoding="utf-8")
    p = write_ad(tmp_path / "ads", "bad.txt", company="Badco", title="Data Scientist")
    st = hardstop.reason_of(p, settings)
    assert st is not None and "blacklist" in st.reason and "ghosted" in st.reason


def test_live_files_the_stopped_ads_away_before_tailoring_and_tailors_the_rest(settings, db, approved):
    spy = Spy()
    live = Live(db, settings, spy.hooks(), out=(lines := []).append, require_approval=True)
    live.run()
    tailored = [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"]
    assert tailored == ["Goodco"], "\n".join(lines)                             # no model call was spent on the stopped two
    assert stopped(settings) == ["chambers.txt", "itol.txt"]
    assert not (approved / "itol.txt").exists() and not (approved / "chambers.txt").exists()
    note = (hardstop.folder(settings) / "itol.reason.txt").read_text(encoding="utf-8")
    assert "training scheme (fees)" in note
    log = {e["title"]: e for e in decide.read_log(settings)}
    assert log["Trainee AI Engineer"]["decision"] == decide.SKIP and log["Chief AI Engineer"]["reason"].startswith("hard stop before tailoring")
    assert any("hard stop:" in line and "chambers.txt" in line for line in lines)


def test_a_dry_run_names_the_stops_and_moves_nothing(settings, db, approved):
    spy = Spy()
    live = Live(db, settings, spy.hooks(), out=(lines := []).append, require_approval=True, dry_run=True)
    live.run()
    assert stopped(settings) == [] and (approved / "itol.txt").exists()
    assert any("would move to" in line and "itol.txt" in line for line in lines)
    assert [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"] == ["Goodco"]


def test_include_unapproved_keeps_its_own_rule_triage(settings, db):
    inbox = settings.path("jds") / "inbox"
    write_ad(inbox, "chambers.txt", company="Chambers and Partners", title="Chief AI Engineer")
    Live(db, settings, Spy().hooks(), out=lambda s: None, require_approval=False).run()
    assert stopped(settings) == []                                              # the rule triage files it under skipped/, as before


def test_a_job_you_promoted_is_never_stopped(settings, approved):
    decide.log(settings, [{"key": "chambersandpartners|chief ai engineer", "company": "Chambers and Partners", "title": "Chief AI Engineer",
                           "decision": decide.PROMOTED, "by": "user"}], NOW)
    assert [st.path.name for st in hardstop.find(sorted(approved.iterdir()), settings)] == ["itol.txt"]


def test_recheck_lists_the_stops_then_apply_moves_them_and_unskip_brings_them_back(settings, db, approved):
    dry = "\n".join(decide.recheck(settings, db, {}, now=NOW))
    assert "2 stopped (dry run" in dry and "would move to" in dry and stopped(settings) == []
    done = "\n".join(decide.recheck(settings, db, {}, now=NOW, apply=True))
    assert "moved to jds_done/skipped_agent/" in done and stopped(settings) == ["chambers.txt", "itol.txt"]
    back = "\n".join(decide.reset(settings, match="Chief", now=NOW))
    assert "inbox/chambers.txt" in back and stopped(settings) == ["itol.txt"]
    assert (settings.path("jds") / "inbox" / "chambers.txt").exists()
    assert (hardstop.folder(settings) / "chambers.reason.txt").exists()          # the note stays as the record: nothing is deleted


def test_a_stopped_file_is_never_overwritten(settings, approved):
    hardstop.folder(settings).mkdir(parents=True)
    (hardstop.folder(settings) / "itol.txt").write_text(ad_text(company="ITOL Recruit", title="Old"), encoding="utf-8")
    hardstop.file_away(settings, hardstop.find([approved / "itol.txt"], settings), now=NOW)
    assert stopped(settings) == ["itol-2.txt", "itol.txt"]
    assert json.loads(decide.log_path(settings).read_text(encoding="utf-8").splitlines()[-1])["file"].endswith("itol-2.txt")
