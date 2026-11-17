"""v3.7.1: the knock-out check before tailoring (apply/jd_knockout.py, not the form pre-scan of apply/knockout.py; settings `knockouts.skip_before_tailoring`, empty by default so nothing changes). An ad
that STATES a listed knock-out (its sentence is quoted) moves to jds_done/skipped_knockout/ with its reason before any model call; a dry run only names it.
No network, no model."""
import pytest

from careerops.apply import jd_knockout as knockout
from careerops.apply.live import Live
from careerops.discovery import decide, shortlist
from triage_fakes import FILLER, write_ad
from test_volume import Spy

SC = "To gain SC Clearance you will normally need at least 5 years' UK residency."
YEARS = "You will have 5+ years of experience building machine learning models in production."


# ------------------------------------------------------------------ the matchers
@pytest.mark.parametrize("text", [
    SC, "SC clearance is required: candidates must have lived in the UK for the last 5 years.",
    "DV clearance needs 10 years of UK residency.", "eSC vetting requires three years' continuous UK residence.",
    "Security clearance (SC) applies and you must have been resident in the UK for 5 years.",
])
def test_security_clearance_with_a_residency_period_matches(text):
    got = knockout.match(f"About us. {FILLER}\n\n{text}\n\nBenefits: pension.", ["security_clearance_residency"])
    assert len(got) == 1 and got[0].kind == "security_clearance_residency" and got[0].sentence.rstrip(".") in text


@pytest.mark.parametrize("text", [
    "SC clearance is desirable but not essential.",                       # a clearance with no residency period
    "Candidates must have been resident in the UK for 5 years.",          # a residency period with no clearance
    "We have 5 years of experience selling clearance stock to SC customers.",
    "You will need the right to work in the UK.",
])
def test_a_clearance_alone_or_a_residency_alone_is_no_knock_out(text):
    assert knockout.match(f"{FILLER}\n\n{text}\n", ["security_clearance_residency"]) == []


@pytest.mark.parametrize("text", [
    "5+ years of experience in data science.", "You have at least 5 years of commercial machine learning experience.",
    "Minimum of 5 years' experience with Python.", "8+ years in analytics.", "Five or more years of hands-on experience.",
])
def test_five_or_more_years_stated_in_the_requirements_matches(text):
    got = knockout.match(f"Requirements:\n- {text}\n", ["years_experience_min_5"])
    assert len(got) == 1 and got[0].kind == "years_experience_min_5"


@pytest.mark.parametrize("text", [
    "2+ years of experience in data science.", "3-5 years of experience.", "You have at least 3 years of experience.",
    "5+ years of experience desirable.", "We have been helping clients for 5+ years.", "Founded over 5 years ago, we are growing.",
    "To gain SC Clearance you will normally need at least 5 years' UK residency.",
])
def test_fewer_years_a_range_an_optional_ask_company_history_or_residency_is_no_knock_out(text):
    assert knockout.match(f"Requirements:\n- {text}\n", ["years_experience_min_5"]) == []


def test_only_the_listed_knock_outs_are_checked_and_an_empty_list_checks_nothing():
    text = f"{SC}\n{YEARS}\n"
    assert {m.kind for m in knockout.match(text, ["security_clearance_residency", "years_experience_min_5"])} == {"security_clearance_residency", "years_experience_min_5"}
    assert [m.kind for m in knockout.match(text, ["years_experience_min_5"])] == ["years_experience_min_5"]
    assert knockout.match(text, []) == []
    assert knockout.match(text, ["no_such_knock_out"]) == []


# ------------------------------------------------------------------ live
@pytest.fixture
def approved(settings):
    folder = shortlist.approved_root(settings) / "2026-11-16"
    write_ad(folder, "sirius.txt", company="Sirius Analysis", title="Data Scientist", extra=SC + " " + FILLER * 4)
    write_ad(folder, "good.txt", company="Goodco", title="Data Scientist", extra=FILLER * 6)
    return folder


def stopped(settings):
    return sorted(p.name for p in knockout.jd_files(settings))


def run(db, settings, **kw):
    spy = Spy()
    lines: list[str] = []
    Live(db, settings, spy.hooks(), out=lines.append, require_approval=True, **kw).run()
    return spy, "\n".join(lines)


def test_with_the_setting_on_the_sc_ad_is_skipped_before_any_model_call_and_the_rest_is_tailored(settings, db, approved):
    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    spy, out = run(db, settings)
    assert [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"] == ["Goodco"], out      # 0 calls were spent on Sirius
    assert stopped(settings) == ["sirius.txt"] and not (approved / "sirius.txt").exists()
    note = (knockout.folder(settings) / "sirius.reason.txt").read_text(encoding="utf-8")
    assert "security_clearance_residency" in note and SC.rstrip(".") in note
    assert SC.rstrip(".") in out and "skipped_knockout" in out                                              # the matched sentence is printed
    log = {e["company"]: e for e in decide.read_log(settings)}
    assert log["Sirius Analysis"]["decision"] == decide.SKIP and log["Sirius Analysis"]["reason"].startswith("knock-out before tailoring")


@pytest.mark.parametrize("value", [None, [], ["years_experience_min_5"]])
def test_with_the_setting_off_or_another_knock_out_listed_behaviour_is_unchanged(settings, db, value):
    folder = shortlist.approved_root(settings) / "2026-11-16"
    write_ad(folder, "sirius.txt", company="Sirius Analysis", title="Data Scientist", extra=SC + " " + FILLER * 4)
    if value is not None:
        settings.raw["knockouts"] = {"skip_before_tailoring": value}
    spy, out = run(db, settings)
    assert [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"] == ["Sirius Analysis"], out      # tailored as before
    assert stopped(settings) == [] and "knock-out" not in out


def test_a_dry_run_only_names_the_ad_and_moves_nothing(settings, db, approved):
    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    spy, out = run(db, settings, dry_run=True)
    assert [db.job(jid)["company"] for name, jid in spy.calls if name == "tailor"] == ["Goodco"]
    assert (approved / "sirius.txt").exists() and stopped(settings) == [] and decide.read_log(settings) == []
    assert "dry run" in out and SC.rstrip(".") in out and "would move" in out


def test_a_file_named_with_url_is_checked_too(settings, db, tmp_path):
    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    from careerops.apply import decided

    p = write_ad(decided.inbox_dir(settings), "sirius.txt", company="Sirius Analysis", title="Data Scientist", extra=SC + " " + FILLER * 4)
    spy = Spy()
    Live(db, settings, spy.hooks(), out=(lines := []).append, only=p).run()
    assert [n for n, _ in spy.calls if n == "tailor"] == [] and stopped(settings) == ["sirius.txt"], "\n".join(lines)


def test_unskip_brings_a_knocked_out_ad_back_to_the_inbox(settings, db, approved):
    from careerops.apply import decided

    settings.raw["knockouts"] = {"skip_before_tailoring": ["security_clearance_residency"]}
    run(db, settings)
    assert stopped(settings) == ["sirius.txt"]
    lines = decide.reset(settings, match="Sirius")
    assert stopped(settings) == [] and (decided.inbox_dir(settings) / "sirius.txt").exists() and any("skipped_knockout" in l for l in lines)
