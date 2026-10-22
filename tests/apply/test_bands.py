"""ATS bands (settings.yaml -> apply.bands): after tailoring, the STRICT CV score S and the honest ceiling C decide what happens. S >= auto: the
auto-apply gates run; review <= S < auto: the review prompt; S < review and C >= rescue_ceiling: review with the flag "CV under-uses the fact
bank"; S < review and C below that: `skipped (ats)`, no letter, no form, no prompt. Fakes only: no model, no browser, no xelatex."""
import json
from pathlib import Path

import pytest

from careerops import bands, cli
from careerops.apply import decided
from careerops.apply.live import FINISHED, Live
from test_live import Fake, archive, filed, inbox, put, session  # noqa: F401  (archive and inbox are fixtures)


# ------------------------------------------------------------------ the setting
def test_the_shipped_settings_hold_the_bands(settings):
    assert settings.raw["apply"]["bands"] == {"auto": 70, "review": 50, "rescue_ceiling": 60}
    assert bands.config(settings) == bands.Bands(70.0, 50.0, 60.0)


def test_without_the_key_the_built_in_defaults_apply(settings):
    del settings.raw["apply"]["bands"]
    assert bands.config(settings) == bands.Bands(70.0, 50.0, 60.0)


@pytest.mark.parametrize("value", [{"auto": 40, "review": 50, "rescue_ceiling": 60}, {"auto": "x", "review": 50, "rescue_ceiling": 60},
                                   {"auto": 70, "review": -1, "rescue_ceiling": 60}, {"auto": 70, "review": 50, "rescue_ceiling": 101},
                                   {"auto": True, "review": 50, "rescue_ceiling": 60}, [70, 50, 60]])
def test_a_band_that_makes_no_sense_is_refused_with_its_name(settings, value):
    settings.raw["apply"]["bands"] = value
    with pytest.raises(bands.BandsError, match="apply.bands"):
        bands.config(settings)


# ------------------------------------------------------------------ each band
B = bands.Bands(70.0, 50.0, 60.0)


@pytest.mark.parametrize("strict, ceiling, outcome", [
    (100, 100, "auto"), (70, 75, "auto"), (70, 10, "auto"), (69.9, 90, "review"), (50, 50, "review"), (50, 0, "review"),
    (49.9, 60, "rescue"), (32, 100, "rescue"), (0, 60, "rescue"),
    (49.9, 59.9, "skip"), (32, 41, "skip"), (0, 0, "skip"),
])
def test_each_band(strict, ceiling, outcome):
    v = bands.classify(strict, ceiling, B)
    assert v.outcome == outcome and v.strict == strict and v.ceiling == ceiling
    assert v.letter == (outcome != "skip")                      # a letter is drafted only for a job in review or auto


def test_a_rescued_job_carries_the_red_flag_with_both_numbers():
    v = bands.classify(32, 74, B)
    assert v.flag == "CV under-uses the fact bank (strict 32, ceiling 74)"
    assert bands.classify(60, 74, B).flag == "" and bands.classify(80, 90, B).flag == ""


def test_a_skipped_job_has_its_reason_and_its_line():
    v = bands.classify(32, 41, B)
    assert v.reason == "ATS S < 50"
    assert v.skip_line("Valarian", "MLOps Engineer") == "SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer"
    assert bands.classify(49.6, 41, B).skip_line("A", "B") == "SKIPPED (ATS 49.6 < 50): A | B"        # a number just under the line is shown as it is


def test_the_verdict_survives_report_json():
    v = bands.classify(32, 74, B)
    assert bands.Verdict.from_dict(json.loads(json.dumps(v.to_dict()))) == v


def test_the_band_line_names_the_band_and_the_numbers():
    assert bands.classify(63, 78, B).line() == "ATS band: strict 63, ceiling 78 -> review (50 to under 70)"
    assert bands.classify(82, 90, B).line() == "ATS band: strict 82, ceiling 90 -> auto (70 or more: the auto-apply gates run)"
    assert bands.classify(32, 78, B).line().startswith("ATS band: strict 32, ceiling 78 -> review, flagged 'CV under-uses the fact bank'")
    assert bands.classify(32, 41, B).line() == "ATS band: strict 32, ceiling 41 -> skipped (under 50, ceiling under 60): no letter, no form"


# ------------------------------------------------------------------ live: a skipped job costs nothing more
class AtsFake(Fake):
    """A tailoring step that ends in the given strict score: `skipped (ats)` below 50, else needs_review."""

    def __init__(self, db, settings, strict_by_company, **kw):
        super().__init__(db, settings, **kw)
        self.strict = strict_by_company

    def tailor(self, jid):
        self._enter("tailor", jid)
        job = self.db.job(jid)
        s = self.strict[job["company"]]
        if s < 50:
            self.db.update_job(jid, status=bands.ATS_SKIP, reason="ATS S < 50", score=s / 100, out_dir=str(self.s.path("outputs") / "x"))
        else:
            self.db.update_job(jid, status="needs_review", score=s / 100, out_dir=str(self.s.path("outputs") / "x"))


def test_a_job_below_the_band_is_skipped_with_one_line_and_no_prompt_review_form_or_letter(db, settings, inbox, archive, jd):
    f = put(inbox, "valarian.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    fake = AtsFake(db, settings, {"Valarian": 32})
    live, lines = session(db, settings, fake)
    assert live.run() == 0
    assert "SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer" in lines
    assert fake.names() == ["tailor"] and fake.prompts == []                    # no review, no preflight, no assist, no question
    job = db.job(next(j for n, j in fake.calls))
    assert job["status"] == "skipped (ats)" and job["reason"] == "ATS S < 50"
    assert not f.exists() and [p.name for p in filed(settings, "skipped") if not p.name.endswith(".reason.txt")] == ["valarian.txt"]
    reason = settings.path("jds_done") / "skipped" / "valarian.reason.txt"
    assert reason.read_text(encoding="utf-8").strip() == "ATS S < 50"
    assert [r.status for r in live.results] == ["skipped (ats)"] and "skipped (ats)" in FINISHED


def test_the_summary_counts_the_skipped_jobs_and_lists_each(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    put(inbox, "b.txt", jd, company="Brightly", title="Data Scientist", n=2)
    put(inbox, "c.txt", jd, company="Cardiff", title="ML Engineer", n=3)
    fake = AtsFake(db, settings, {"Valarian": 32, "Brightly": 41, "Cardiff": 66}, answers={"Open the application form": ["n"]})
    live, lines = session(db, settings, fake)
    live.run()
    text = "\n".join(lines)
    assert "Skipped by the ATS band: 2" in text
    assert "SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer" in text and "SKIPPED (ATS 41 < 50): Brightly | Data Scientist" in text
    assert "SKIPPED (ATS 66" not in text
    assert [n for n, _ in fake.calls].count("review") == 1                      # the 66 is reviewed as always


def test_a_job_in_the_review_band_goes_to_the_review_prompt_as_before(db, settings, inbox, archive, jd):
    put(inbox, "a.txt", jd, company="Cardiff", title="ML Engineer", n=3)
    fake = AtsFake(db, settings, {"Cardiff": 50}, review="skipped")
    live, lines = session(db, settings, fake)
    live.run()
    assert fake.names() == ["tailor", "review"] and not any(l.startswith("SKIPPED (ATS") for l in lines)


def test_a_dry_run_names_the_skip_and_leaves_the_file_where_it_is(db, settings, inbox, archive, jd):
    f = put(inbox, "valarian.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    fake = AtsFake(db, settings, {"Valarian": 32})
    live, lines = session(db, settings, fake, dry_run=True)
    live.run()
    assert "SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer" in lines and f.exists() and filed(settings, "skipped") == []
    live2, lines2 = session(db, settings, AtsFake(db, settings, {"Valarian": 32}))        # the real run files it, without tailoring again
    live2.run()
    assert not f.exists() and any(p.name == "valarian.txt" for p in filed(settings, "skipped"))


def test_a_skipped_ats_job_is_not_asked_about_or_worked_on_by_the_next_session(db, settings, inbox, archive, jd):
    put(inbox, "valarian.txt", jd, company="Valarian", title="MLOps Engineer", n=1)
    session(db, settings, AtsFake(db, settings, {"Valarian": 32}))[0].run()
    again = AtsFake(db, settings, {"Valarian": 32})
    session(db, settings, again)[0].run()
    assert again.calls == []                                                    # its JD is in jds_done/skipped: nothing is left in the inbox
