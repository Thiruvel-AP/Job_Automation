"""`careerops live --auto`: the gate decides, per job, between the automatic fill-and-submit and the normal review prompt. Fakes for the gate and the
form (the real ones have their own tests: test_autogate.py, tests/test_autoapply.py); the archive, inbox and database are under tmp_path."""
import pytest

from careerops.apply import autogate, autolog
from careerops.apply.errors import ArchiveError
from careerops.apply.live import Hooks, Live
from careerops.autoapply import AutoResult
from test_live import Fake, archive, inbox, put, yes       # noqa: F401  (fixtures and helpers of the live tests)

BLOCK = autogate.Gates([autogate.Gate("ATS", False, "ATS 58 < 70"), autogate.Gate("level", False, "level: senior"), autogate.Gate("kind", True)])
PASS = autogate.Gates([autogate.Gate("ATS", True), autogate.Gate("level", True)])


class AutoFake(Fake):
    def __init__(self, db, settings, answers=None, *, gates=PASS, result=None, **kw):
        super().__init__(db, settings, answers, **kw)
        self.gates = gates
        self.result = result or AutoResult(autolog.SUBMITTED, "the confirmation page appeared", ["form.png", "done.png"],
                                           "auto-submitted 2026-10-21 10:00: https://x/confirm", ["Email <- email"], ["County"])
        self.marks: list[str] = []
        self.sink = None

    def review(self, jid):
        if self.sink:
            self.sink("<<REVIEW PROMPT>>")
        return super().review(jid)

    def auto_gate(self, jid):
        self._enter("auto_gate", jid)
        return self.gates

    def auto_apply(self, jid):
        self._enter("auto_apply", jid)
        return self.result

    def hooks(self):
        h = super().hooks()
        return Hooks(ask=h.ask, tailor=h.tailor, review=h.review, preflight=h.preflight, assist=h.assist, mark_applied=h.mark_applied,
                     auto_gate=self.auto_gate, auto_apply=self.auto_apply)


def go(db, settings, fake, **kw):
    lines: list[str] = []
    fake.sink = lines.append
    live = Live(db, settings, fake.hooks(), out=lines.append, resume_cmd="careerops live --auto", **kw)
    return live, lines, live.run()


def one(inbox, jd):
    put(inbox, "a.txt", jd, company="Acme", n=1)


# ------------------------------------------------------------------ a failed gate: review prompt, no auto
def test_a_failed_gate_never_reaches_the_form_and_prints_every_failure_before_the_review_prompt(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, yes(), gates=BLOCK)
    live, lines, code = go(db, settings, fake)
    assert code == 0 and "auto_apply" not in fake.names()
    assert fake.names() == ["tailor", "auto_gate", "review", "preflight", "assist", "mark_applied"]          # assist for everything the gate refused
    assert "  AUTO BLOCKED: ATS 58 < 70; level: senior" in lines
    assert lines.index("  AUTO BLOCKED: ATS 58 < 70; level: senior") < lines.index("<<REVIEW PROMPT>>")
    assert live.results[0].note == "AUTO BLOCKED: ATS 58 < 70; level: senior"


def test_a_blocked_job_the_user_skips_in_review_is_skipped_as_always(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, gates=BLOCK, review="skipped")
    live, lines, code = go(db, settings, fake)
    assert live.results[0].status == "skipped" and fake.names() == ["tailor", "auto_gate", "review"]


def test_a_preflight_refusal_blocks_auto_and_hands_the_job_to_review(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, yes())
    fake.preflight_error = ArchiveError("archive root missing")
    live, lines, code = go(db, settings, fake)
    assert "auto_apply" not in fake.names() and "  AUTO BLOCKED: archive: archive root missing" in lines
    assert "review" in fake.names()


# ------------------------------------------------------------------ every gate passed
def test_a_job_that_passes_every_gate_is_filled_submitted_and_marked_applied_without_a_review_prompt(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings)
    live, lines, code = go(db, settings, fake)
    assert code == 0 and fake.names() == ["tailor", "auto_gate", "preflight", "auto_apply", "mark_applied"]
    assert live.results[0].status == "applied" and db.job(live.results[0].job_id)["status"] == "applied"
    assert fake.prompts == [] and not (inbox / "a.txt").exists() and len(list((settings.path("jds_done") / "applied").iterdir())) == 1
    assert any("form.png" in l for l in lines) and any("submitted" in l.lower() for l in lines)


def test_the_confirmation_reference_goes_to_mark_applied(db, settings, inbox, archive, jd):
    one(inbox, jd)
    seen = {}

    class Spy(AutoFake):
        def mark_applied(self, jid, confirmation):
            seen["c"] = confirmation
            return super().mark_applied(jid, confirmation)
    go(db, settings, Spy(db, settings))
    assert seen["c"] == "auto-submitted 2026-10-21 10:00: https://x/confirm"


def test_no_submit_fills_and_photographs_but_the_job_stays_unapplied_and_in_the_inbox(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, result=AutoResult(autolog.NOT_SUBMITTED, "--no-submit", ["form.png"], "", ["a"], []))
    live, lines, code = go(db, settings, fake)
    assert fake.names() == ["tailor", "auto_gate", "preflight", "auto_apply"] and live.results[0].status == "auto_filled (not submitted)"
    assert db.job(live.results[0].job_id)["status"] == "needs_review" and (inbox / "a.txt").exists()
    assert any("form.png" in l for l in lines)


def test_dry_run_never_calls_the_form(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings)
    live, lines, code = go(db, settings, fake, dry_run=True)
    assert fake.names() == ["tailor", "auto_gate"] and live.results[0].status == "auto_ready (dry run)"
    assert any("AUTO READY" in l for l in lines) and (inbox / "a.txt").exists()
    assert db.job(live.results[0].job_id)["status"] == "needs_review"


def test_dry_run_with_a_failed_gate_still_reviews_and_stops_before_assist(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, gates=BLOCK)
    live, lines, code = go(db, settings, fake, dry_run=True)
    assert fake.names() == ["tailor", "auto_gate", "review"] and live.results[0].status == "approved (dry run)"


# ------------------------------------------------------------------ the form could not be finished
def test_a_captcha_or_unknown_question_makes_the_job_needs_user_and_nothing_is_recorded_as_applied(db, settings, inbox, archive, jd):
    one(inbox, jd)
    res = AutoResult(autolog.NEEDS_USER, "a CAPTCHA / bot check is on the page (iframe)", ["stopped.png"])
    fake = AutoFake(db, settings, result=res)
    live, lines, code = go(db, settings, fake)
    jid = live.results[0].job_id
    assert live.results[0].status == "needs_user" and "mark_applied" not in fake.names()
    assert db.job(jid)["status"] == "needs_user" and "CAPTCHA" in db.job(jid)["reason"]
    assert (inbox / "a.txt").exists()
    assert any(f"careerops apply --id {jid}" in l for l in lines) and any(f"careerops mark-applied --id {jid}" in l for l in lines)


def test_a_needs_user_job_is_not_filled_again_on_the_next_run(db, settings, inbox, archive, jd):
    one(inbox, jd)
    res = AutoResult(autolog.NEEDS_USER, "unknown question", [])
    go(db, settings, AutoFake(db, settings, result=res))
    fake = AutoFake(db, settings, result=res)
    live, lines, code = go(db, settings, fake)
    assert "auto_apply" not in fake.names() and live.results[0].status == "needs_user"


def test_submit_unconfirmed_is_never_marked_applied_nor_retried(db, settings, inbox, archive, jd):
    one(inbox, jd)
    res = AutoResult(autolog.UNCONFIRMED, "no confirmation page within 30 s: check the form", ["form.png", "after.png"])
    fake = AutoFake(db, settings, result=res)
    live, lines, code = go(db, settings, fake)
    jid = live.results[0].job_id
    assert live.results[0].status == "submit_unconfirmed" and "mark_applied" not in fake.names()
    assert db.job(jid)["status"] == "submit_unconfirmed" and not (inbox / "a.txt").exists()
    assert any("check" in l.lower() and f"mark-applied --id {jid}" in l for l in lines)
    again = AutoFake(db, settings, result=res)
    put(inbox, "a.txt", jd, company="Acme", n=1)
    go(db, settings, again)
    assert again.calls == []                                           # the same JD is "already done"


def test_declining_submit_falls_through_to_the_review_prompt(db, settings, inbox, archive, jd):
    one(inbox, jd)
    res = AutoResult(autolog.DECLINED, "you answered N at Submit?", ["form.png"])
    fake = AutoFake(db, settings, yes(), result=res)
    live, lines, code = go(db, settings, fake)
    assert fake.names() == ["tailor", "auto_gate", "preflight", "auto_apply", "review", "preflight", "assist", "mark_applied"]


def test_an_error_while_driving_the_page_falls_through_to_the_review_prompt(db, settings, inbox, archive, jd):
    one(inbox, jd)
    res = AutoResult(autolog.ERROR, "TimeoutError: page.goto", [])
    fake = AutoFake(db, settings, yes(), result=res)
    live, lines, code = go(db, settings, fake)
    assert any("AUTO FAILED: TimeoutError" in l for l in lines) and "review" in fake.names() and "mark_applied" in fake.names()


def test_a_wrong_file_or_unknown_country_from_the_form_step_is_a_block_not_a_crash(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings, yes())
    fake.raise_in["auto_apply"] = ArchiveError("the work location of this job is unknown")
    live, lines, code = go(db, settings, fake)
    assert code == 0 and any("AUTO BLOCKED: the work location" in l for l in lines) and "review" in fake.names()


def test_ctrl_c_while_the_form_is_open_says_the_form_may_have_been_submitted(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = AutoFake(db, settings)
    fake.raise_in["auto_apply"] = KeyboardInterrupt()
    live, lines, code = go(db, settings, fake)
    text = "\n".join(lines)
    assert code == 130 and "may already have been submitted" in text and "applied_auto.csv" in text and "mark-applied --id" in text


# ------------------------------------------------------------------ without --auto nothing changes
def test_a_session_without_the_auto_hooks_is_the_old_live(db, settings, inbox, archive, jd):
    one(inbox, jd)
    fake = Fake(db, settings, yes())
    live = Live(db, settings, fake.hooks(), out=lambda s: None)
    assert live.run() == 0 and fake.names() == ["tailor", "review", "preflight", "assist", "mark_applied"]


def test_a_job_the_user_already_approved_goes_through_the_assist_not_auto(db, settings, inbox, archive, jd):
    one(inbox, jd)
    first = Fake(db, settings, yes())
    Live(db, settings, first.hooks(), out=lambda s: None, dry_run=True).run()           # approved in a dry run
    fake = AutoFake(db, settings, yes())
    live, lines, code = go(db, settings, fake)
    assert "auto_gate" not in fake.names() and "assist" in fake.names()
